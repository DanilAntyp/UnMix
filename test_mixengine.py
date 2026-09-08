"""Checks for rendered-candidate mixing (no model download required)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import dj
import mixengine as me
import structure


def tone(hz=220, duration=4, level=.2):
    t = np.arange(round(duration * me.SR)) / me.SR
    return np.column_stack([level * np.sin(2 * np.pi * hz * t)] * 2)


class SignalTests(unittest.TestCase):
    def test_eq_bands_reconstruct_original_without_phase_damage(self):
        y = np.random.default_rng(7).normal(0, .1, (44100, 2))
        np.testing.assert_allclose(sum(me.bands(y)), y, atol=1e-12)

    def test_bass_transfer_is_gradual_and_complementary(self):
        a, b = me.eq_gains(me.SR * 8, me.SR, 8, 2)
        for left, right in zip(a, b):
            np.testing.assert_allclose(left + right, 1)
        low = b[0]
        self.assertGreater(np.count_nonzero((low > .1) & (low < .9)) / me.SR, 2)
        self.assertLess(np.max(np.diff(low)), .00002)

    def test_actual_output_surge_dip_and_overload_get_penalized(self):
        y = tone()
        clean = me.rendered_metrics(y, y, y, 2)
        hot = me.rendered_metrics(y * 8, y, y, 2)
        hole = me.rendered_metrics(y * .1, y, y, 2)
        self.assertGreater(hot["peak_excess"], .5)
        self.assertGreater(hot["level_surge_db"], 10)
        self.assertGreater(hole["level_dip_db"], 10)
        self.assertLess(me.candidate_score(clean), me.candidate_score(hot))
        self.assertLess(me.candidate_score(clean), me.candidate_score(hole))

    def test_local_harmony_distinguishes_unison_from_semitone_clash(self):
        a = tone(440)
        self.assertGreater(me.harmonic_conflict(a, tone(440 * 2 ** (1 / 12))),
                           me.harmonic_conflict(a, a) + .25)

    def test_muted_phrases_do_not_count_as_harmonic_overlap(self):
        a, b = tone(440), tone(440 * 2 ** (1 / 12))
        self.assertEqual(me.harmonic_conflict(a, b, overlap=np.zeros(8)), 0)
        self.assertAlmostEqual(me.harmonic_conflict(a, b, overlap=np.ones(8)), me.harmonic_conflict(a, b))

    def test_misaligned_kick_attacks_score_worse_than_aligned(self):
        t = np.arange(me.SR * 4) / me.SR
        pulse = np.sin(2 * np.pi * 75 * t) * np.exp(-((t % .5) / .025))
        a = np.column_stack([pulse] * 2)
        b = np.roll(a, round(.08 * me.SR), axis=0)
        self.assertGreater(me.rhythm_conflict(a, b, .5), me.rhythm_conflict(a, a, .5) + .3)

    def test_selective_vocal_envelopes_never_overlap(self):
        from test_dj_transitions import vocal_curve
        env = dj._vocal_handoff_curves(vocal_curve([(0, 2)]), vocal_curve([(3, 6)]), 8)
        t = np.arange(me.SR * 8) / me.SR
        a, b = [me.parse_vocal_envelope(e, t) for e in env]
        self.assertEqual(np.max(a * b), 0)
        self.assertEqual(a[me.SR], 1)
        self.assertEqual(b[me.SR * 4], 1)

    def test_original_method_does_not_require_separated_audio(self):
        y = tone()
        mixed, _, _ = me.synthesize(y, y, None, None, 4, 2, "original", "gradual", False, None)
        np.testing.assert_allclose(mixed, y, atol=1e-7)


class SavedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dj_candidate_test_")
        self.root = Path(self.temp.name)
        self.patch = patch.object(dj, "DJ_DIR", self.root)
        self.patch.start()
        self.a, self.b = self.root / "a.wav", self.root / "b.wav"
        self.a.touch()
        self.b.touch()
        self.token = "a" * 32
        self.cache = self.root / ".candidates"
        self.cache.mkdir()
        self.pcm = self.cache / f"{self.token}.wav"
        self.pcm.touch()
        self.plan = {"method": "original", "shape": "gradual", "metrics": {},
                     "cut": 16, "b_start": 4, "beats": 8}
        manifest = {"version": me.VERSION, "a": me.fingerprint(self.a),
                    "b": me.fingerprint(self.b), "plan": self.plan}
        (self.cache / f"{self.token}.json").write_text(json.dumps(manifest))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_tokens_are_scoped_and_source_changes_invalidate_saved_audio(self):
        plan, pcm = me.load_candidate(self.token, self.a, self.b)
        self.assertEqual(plan, self.plan)
        self.assertEqual(pcm, self.pcm)
        with self.assertRaisesRegex(ValueError, "invalid"):
            me.load_candidate("../private", self.a, self.b)
        self.a.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "tracks changed"):
            me.load_candidate(self.token, self.a, self.b)

    def test_missing_pcm_gives_actionable_error(self):
        self.pcm.unlink()
        with self.assertRaisesRegex(ValueError, "make new previews"):
            me.load_candidate(self.token, self.a, self.b)

    def test_reference_is_pair_scoped_and_invalidated_with_source(self):
        import mix_preferences
        self.plan.update(duration=4)
        manifest = {'version': me.VERSION, 'a': me.fingerprint(self.a),
                    'b': me.fingerprint(self.b), 'plan': self.plan}
        (self.cache / f'{self.token}.json').write_text(json.dumps(manifest))
        (self.root / f'preview_{self.token}_automix.mp3').touch()
        with patch.object(mix_preferences, 'REFERENCE_FILE', self.root / 'references.json'):
            mix_preferences.set_reference(self.a, self.b, self.token)
            reference = mix_preferences.get_reference(self.a, self.b)
            self.assertEqual(reference['candidate'], self.token)
            self.assertIsNone(mix_preferences.get_reference(self.b, self.a))
            self.a.write_bytes(b'changed')
            self.assertIsNone(mix_preferences.get_reference(self.a, self.b))

    def test_blind_pack_preserves_audio_and_never_overwrites_experiments(self):
        import dj_benchmark
        preview = self.root / f'preview_{self.token}_automix.mp3'
        preview.write_bytes(b'candidate audio')
        reference = self.root / 'human-reference.mp3'
        reference.write_bytes(b'reference audio')
        output = self.root / 'experiment'
        result = dj_benchmark.export_pack(self.a, self.b, [self.token], reference, output, seed=7)
        self.assertEqual(len(result['versions']), 2)
        for entry in result['versions']:
            self.assertEqual((output / entry['file']).read_bytes(), Path(entry['path']).read_bytes())
        with self.assertRaises(FileExistsError):
            dj_benchmark.export_pack(self.a, self.b, [self.token], reference, output)

    def test_feedback_saves_specific_method_not_only_automix(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(dj.bp)
        target = self.root / "feedback.jsonl"
        with patch.object(dj, "DL_DIR", self.root), patch.object(dj, "FEEDBACK_FILE", target), \
             patch("analysis.analyze", return_value={}):
            response = app.test_client().post("/dj/feedback", json={
                "style": "automix", "verdict": 1, "candidate": self.token,
                "a_file": "/downloads/a.wav", "b_file": "/downloads/b.wav"})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(target.read_text())
        self.assertEqual(saved["method"], "original")
        self.assertEqual(saved["candidate"], self.token)

    def test_cached_neural_phrases_feed_the_planner(self):
        cached = {"segments": [{"start": 2, "end": 8, "label": "chorus"}], "downbeats": [2, 4, 6]}
        with patch.object(structure, "get_cached", return_value=cached), \
             patch("analysis.energy_profile", return_value=(np.ones(10), 1)), \
             patch("analysis.detect_sections", side_effect=AssertionError("must use cached phrases")):
            result = structure.for_mix(self.a)
        self.assertEqual(result["source"], "neural")
        self.assertEqual(result["sections"][0]["label"], "chorus")


if __name__ == "__main__":
    unittest.main()
