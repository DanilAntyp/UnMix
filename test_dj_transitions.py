"""Audio regressions: run with .venv/bin/python -m unittest test_dj_transitions."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

import analysis
import dj


def vocal_curve(spans, duration=8, win=0.02):
    values = np.zeros(round(duration / win), dtype=bool)
    for start, end in spans:
        values[round(start / win):round(end / win)] = True
    return values, win


class VocalTests(unittest.TestCase):
    def test_overlap_finishes_before_chorus_turns_into_breakdown(self):
        grid = {"bar": 0, "bar_len": 2, "downbeats": list(range(0, 90, 2))}
        sections = [{"start": 20, "end": 48, "energy": 1, "label": "chorus"},
                    {"start": 48, "end": 80, "energy": 0.35, "label": "break"}]
        ctx = {"grid_a": grid, "bar": 2, "r_a": np.ones(100), "win_a": 1, "med_a": 1}
        with patch.object(dj, "_duration", return_value=80), \
             patch.object(analysis, "detect_sections", return_value=sections):
            choices = dj._exit_candidates(ctx, None, "automix", 8)
        self.assertTrue(choices)
        self.assertTrue(all(cut + 8 <= 48 for cut, _, _ in choices))

    def test_chill_to_drop_loses_to_energy_matched_placement(self):
        energy = np.ones(100)
        energy[48:70] = 0.25
        ctx = {"energy_a": (energy, 1), "energy_b": (np.ones(100), 1), "bar": 2}
        good = dj._energy_handover(ctx, 36, 0, 1, 8, "automix")
        bad = dj._energy_handover(ctx, 48, 0, 1, 8, "automix")
        self.assertGreater(bad, good + 4)
        choice = dj._choose_pair([(48, 1, "bad"), (36, 1, "good")], [(0, 1, "drop")],
                                lambda a, b: dj._energy_handover(ctx, a, b, 1, 8, "automix"))
        self.assertEqual(choice[1], 36)

    def test_energy_comparison_maps_stretched_b_back_to_original_clock(self):
        b = np.ones(100)
        b[20:30] = 0.25
        ctx = {"energy_a": (np.full(100, 0.25), 1), "energy_b": (b, 1), "bar": 2}
        self.assertAlmostEqual(dj._energy_handover(ctx, 20, 10, 2, 8, "cut"), 0)

    def test_singers_finish_and_enter_on_complete_phrases(self):
        env = dj._vocal_handoff_curves(vocal_curve([(0, 2)]),
                                      vocal_curve([(0, 1), (3, 6)]), 8)
        self.assertIsNotNone(env)
        end = float(re.search(r"st=([\d.]+)", env[0])[1])
        start = float(re.search(r"st=([\d.]+)", env[1])[1])
        self.assertGreaterEqual(end, 2)
        self.assertGreater(start, end + 0.06)
        self.assertLessEqual(start + 0.03, 3)

    def test_continuous_singing_cannot_be_made_safe_by_fading(self):
        for a, b in [([(0, 8)], [(0, 8)]), ([(0, 2)], [(0, 8)])]:
            with self.subTest(a=a, b=b):
                self.assertIsNone(dj._vocal_handoff_curves(vocal_curve(a), vocal_curve(b), 8))

    def test_silent_outgoing_vocal_allows_incoming_pickup(self):
        env = dj._vocal_handoff_curves(vocal_curve([]), vocal_curve([(0, 8)]), 8)
        self.assertIsNotNone(env)
        self.assertIn("st=0.000", env[1])

    def test_no_phrase_before_end_requires_silent_return_to_original(self):
        self.assertIsNotNone(dj._vocal_handoff_curves(vocal_curve([(0, 2)]),
                                                    vocal_curve([(0, 1)]), 8))

    def test_auto_prefers_short_blend_when_candidates_are_equally_clean(self):
        candidates = lambda T: [(12, 0.8, "phrase")]
        beats, _, _ = dj._plan_join(candidates, candidates, (8, 16, 32), 120,
                                   lambda T: lambda a, b: 0, False)
        self.assertEqual(beats, 8)

    def test_short_track_skips_unrenderable_lengths(self):
        beats, _, _ = dj._plan_join(lambda T: [(12, 1, "exit")],
                                   lambda T: [(0, 1, "entry")] if T < 5 else [],
                                   (8, 16, 32), 120, lambda T: lambda a, b: 0, False)
        self.assertEqual(beats, 8)

    def test_different_drum_patterns_can_share_a_stable_downbeat_grid(self):
        grid = {"bar_len": 2, "downbeats": list(range(0, 32, 2))}
        with patch.object(analysis, "align_beats", return_value=(0.08, 0.05)):
            delta, confidence = dj._align_transition(None, 12, None, 0, 8, 120, grid, grid)
        self.assertEqual(delta, 0)
        self.assertGreater(confidence, 0.9)

    def test_downbeat_drift_overrides_a_spurious_correlation(self):
        ga = {"bar_len": 2, "downbeats": list(range(0, 32, 2))}
        gb = {"bar_len": 2.06, "downbeats": [i * 2.06 for i in range(16)]}
        with patch.object(analysis, "align_beats", return_value=(0, 0.9)):
            self.assertLess(dj._align_transition(None, 12, None, 0, 8, 120, ga, gb)[1], 0.35)


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.structure_patch = patch("structure.for_mix", side_effect=lambda path, **kwargs: {
            "sections": analysis.detect_sections(path), "source": "spectral"})
        self.structure_patch.start()
        self.temp = tempfile.TemporaryDirectory(prefix="dj_regression_")
        self.root = Path(self.temp.name)
        self.sr = 44100
        t = np.arange(self.sr * 40) / self.sr
        self.tone = np.column_stack([0.15 * np.sin(2 * np.pi * 220 * t)] * 2)
        self.A, self.B = self.root / "A.wav", self.root / "B.wav"
        sf.write(self.A, self.tone, self.sr, subtype="FLOAT")
        sf.write(self.B, self.tone, self.sr, subtype="FLOAT")

    def tearDown(self):
        self.structure_patch.stop()
        self.temp.cleanup()

    def separate(self, src, start, duration, work, tag):
        y, sr = sf.read(src, always_2d=True)
        y = y[round(start * sr):round((start + duration) * sr)]
        stems = {}
        for name in ("vocals", "drums", "bass", "other"):
            p = work / f"{tag}_{name}.wav"
            sf.write(p, y if name == "drums" else np.zeros_like(y), sr, subtype="FLOAT")
            stems[name] = p
        return stems

    def test_drum_handover_has_no_double_level_or_energy_hole(self):
        sa = self.separate(self.A, 12, 4, self.root, "a")
        sb = self.separate(self.B, 0, 4, self.root, "b")
        out = self.root / "mix.mp3"
        dj._render_style("automix", self.A, 12, self.B, 0, 12, 4, 2, 120,
                         0.1, sa, sb, self.root, out)
        y = analysis._load_mono(out, 30)
        levels = [np.sqrt(np.mean(y[int(s * analysis.SR):int((s + 0.1) * analysis.SR)] ** 2))
                  for s in np.arange(12.2, 15.7, 0.1)]
        self.assertLess(max(levels) / min(levels), 1.08)
        self.assertLess(np.max(np.abs(y)), 0.2)

    def test_energy_detects_arrangement_change_at_similar_loudness(self):
        sr = self.sr
        t = np.arange(sr * 20) / sr
        quiet = 0.15 * np.sin(2 * np.pi * 700 * t)
        driven = quiet + 0.18 * np.sin(2 * np.pi * 70 * t) * np.exp(-((t % 0.5) / 0.08))
        driven *= np.sqrt(np.mean(quiet ** 2) / np.mean(driven ** 2))
        sf.write(self.A, np.r_[driven, quiet], sr, subtype="FLOAT")
        profile = analysis.energy_profile(self.A)
        self.assertGreater(dj._local_energy(profile, 4, 16), dj._local_energy(profile, 24, 36) + 0.2)

    def test_beat_alignment_rejects_drift_and_silence(self):
        def click_track(period, offset=0):
            y = np.zeros(self.sr * 16, dtype=np.float32)
            pulse = np.sin(2 * np.pi * 180 * np.arange(1600) / self.sr) * np.exp(-np.arange(1600) / 180)
            for t in np.arange(0.5 + offset, 15.8, period):
                idx = round(t * self.sr)
                y[idx:idx + len(pulse)] += pulse[:len(y) - idx]
            return y
        sf.write(self.A, click_track(0.5), self.sr)
        sf.write(self.B, click_track(0.5, 0.04), self.sr)
        delta, confidence = analysis.align_beats(self.A, 0, self.B, 0, 16, 120)
        self.assertLess(delta, 0)
        self.assertGreater(confidence, 0.35)
        sf.write(self.B, click_track(0.52), self.sr)
        self.assertLess(analysis.align_beats(self.A, 0, self.B, 0, 16, 120)[1], 0.35)
        sf.write(self.B, np.zeros(self.sr * 16), self.sr)
        self.assertEqual(analysis.align_beats(self.A, 0, self.B, 0, 16, 120)[1], 0)

    def test_preview_and_export_share_cues_and_audio(self):
        grid = {"bpm": 120, "beat_len": 0.5, "bar_len": 2, "bar": 0,
                "downbeats": list(range(0, 40, 2))}
        sections = [{"start": 0, "end": 40, "energy": 1, "label": "chorus"}]
        opts = {"style": "automix", "beats": 8, "cut": 16, "b_start": 0}
        with patch.object(dj, "DJ_DIR", self.root), \
             patch.object(dj, "_load_grid", return_value=grid), \
             patch.object(analysis, "analyze", return_value={"camelot": "8B"}), \
             patch.object(analysis, "detect_sections", return_value=sections), \
             patch.object(analysis, "align_beats", return_value=(0, 0.95)), \
             patch.object(dj, "_vocal_curve", return_value=vocal_curve([], 40)), \
             patch.object(dj, "_separate_window", side_effect=self.separate):
            full, preview = {}, {}
            dj._mix_pair(full, self.A, self.B, opts)
            dj._mix_pair(preview, self.A, self.B, opts, preview=True)
            # Exporting an audition must reuse its audio, even if search logic
            # changes or the user adjusts the generic controls afterwards.
            import mixengine
            chosen = {}
            with patch.object(mixengine, "compare", side_effect=AssertionError("must not replan")):
                dj._mix_pair(chosen, self.A, self.B,
                             {**opts, "cut": 28, "candidate": preview["previews"][0]["candidate"]})
        self.assertEqual(full["style_used"], "automix")
        self.assertEqual(preview["style_used"], full["style_used"])
        self.assertEqual(preview["b_skip"], full["b_skip"])
        self.assertEqual(preview["transition_at"], 12)
        self.assertEqual(chosen["source_transition_at"], full["source_transition_at"])
        a = analysis._load_mono(self.root / Path(full["file"]).name, 40)
        b = analysis._load_mono(self.root / Path(preview["file"]).name, 40)
        a = a[4 * analysis.SR:4 * analysis.SR + len(b)]
        # Independent MP3 encodings differ slightly at their clip boundaries.
        self.assertLess(np.sqrt(np.mean((a[analysis.SR:-analysis.SR] - b[analysis.SR:-analysis.SR]) ** 2)), 0.003)
        selected = analysis._load_mono(self.root / Path(chosen["file"]).name, 40)
        exported = analysis._load_mono(self.root / Path(full["file"]).name, 40)
        np.testing.assert_allclose(selected, exported, atol=1e-6)

    def test_incompatible_tempo_uses_native_b_and_short_handover(self):
        grid_a = {"bpm": 120, "beat_len": 0.5, "bar_len": 2, "bar": 0}
        grid_b = {"bpm": 90, "beat_len": 2 / 3, "bar_len": 8 / 3, "bar": 0}
        with patch.object(dj, "DJ_DIR", self.root), \
             patch.object(dj, "_load_grid", side_effect=[grid_a, grid_b]), \
             patch.object(analysis, "analyze", return_value={"camelot": "8B"}), \
             patch.object(dj, "_vocal_curve", return_value=vocal_curve([], 40)):
            result = {}
            dj._mix_pair(result, self.A, self.B,
                         {"style": "automix", "cut": 16, "b_start": 0, "beats": "auto"}, preview=True)
        self.assertEqual(result["style_used"], "cut")
        self.assertEqual(result["stretch"], 1)
        self.assertIn("tempo", result["fallback"])

    def test_playlist_uses_shared_vocal_and_rhythm_handover(self):
        grid = {"bpm": 120, "beat_len": 0.5, "bar_len": 2, "bar": 0,
                "downbeats": list(range(0, 40, 2))}
        sections = [{"start": 0, "end": 40, "energy": 1, "label": "chorus"}]
        with patch.object(dj, "DJ_DIR", self.root), \
             patch.object(dj, "_load_grid", return_value=grid), \
             patch.object(analysis, "analyze", return_value={"camelot": "8B"}), \
             patch.object(analysis, "detect_sections", return_value=sections), \
             patch.object(analysis, "align_beats", return_value=(0, 0.95)), \
             patch.object(dj, "_vocal_curve", return_value=vocal_curve([], 40)), \
             patch.object(dj, "_separate_window", side_effect=self.separate):
            result = {}
            dj._set_job(result, [self.A, self.B], "auto", ordered=True)
        self.assertNotIn("error", result)
        self.assertEqual(result["join_styles_used"], ["automix"])
        self.assertEqual(result["join_beats"], [8])
        self.assertTrue((self.root / Path(result["file"]).name).is_file())


if __name__ == "__main__":
    unittest.main()
