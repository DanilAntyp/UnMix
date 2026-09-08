import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import dj
import mix_timing as timing
import mix_preferences as preferences
import mixengine as me
import naturalmix as natural
from test_dj_transitions import vocal_curve


class TimingTests(unittest.TestCase):
    def test_search_does_not_spend_every_candidate_on_one_incoming_phrase(self):
        choices = [dict(cut=i * 8, b_original_start=40, beats=16) for i in range(20)]
        choices += [dict(cut=80, b_original_start=0, beats=16)]
        selected = natural.diverse_plans(choices, 2)
        self.assertEqual(len(selected), 10)
        self.assertEqual(selected[1]['b_original_start'], 0)

    def grid(self, period=240 / 124, count=80):
        return dict(bpm=125, beat_len=.48, bar_len=1.94, bar=0,
                    downbeats=np.round(np.arange(count) * period, 2).tolist())

    def test_quantized_beat_bpm_is_repaired_without_reanalysis(self):
        fixed = timing.normalize(self.grid())
        self.assertAlmostEqual(fixed['bpm'], 124, delta=.04)
        self.assertEqual(fixed['raw_bpm'], 125)

    def test_half_bar_disagreement_requires_structural_evidence(self):
        grid = self.grid(2)
        phrases = {'downbeats': (np.arange(80) * 2 + 1).tolist(), 'sections': []}
        ambiguous = timing.resolve(grid, phrases)
        self.assertEqual(ambiguous['phase_confidence'], 0)
        phrases['sections'] = [{'start': t, 'label': 'verse'} for t in (9, 25, 41, 57)]
        fixed = timing.resolve(grid, phrases)
        self.assertEqual(fixed['bar'], 1)
        self.assertEqual(fixed['phase_disagreement_ms'], 1000)
        self.assertGreater(fixed['phase_confidence'], .5)

    def test_local_ratio_uses_bar_positions_not_reported_bpm(self):
        ga, gb = self.grid(240 / 124), self.grid(240 / 122.88)
        p = timing.local_pair(ga, gb, ga['downbeats'][12], gb['downbeats'][8], 8)
        self.assertAlmostEqual(p['ratio'], 124 / 122.88, delta=.001)
        self.assertLess(p['local_drift_ms'], 20)

    def test_local_drift_and_unresolved_phase_are_rejected(self):
        ga, gb = self.grid(2), self.grid(2)
        gb['downbeats'][14] += .2
        self.assertIsNone(timing.local_pair(ga, gb, 20, 20, 8))
        gb = self.grid(2)
        gb['phase_confidence'] = 0
        self.assertIsNone(timing.local_pair(ga, gb, 20, 20, 8))


class OriginalAudioTests(unittest.TestCase):
    def test_breakdown_to_drop_is_not_fixed_by_loudness_matching(self):
        plan = dict(cut=10, b_original_start=0, ratio=1)
        env = dict(a_fade_start=4, a_fade_end=6)
        profiles = dict(energy_a=(np.ones(40) * .2, 1), energy_b=(np.ones(40) * .9, 1))
        self.assertFalse(natural.arrangement_check(profiles, plan, env, 2)[0])
        profiles['energy_a'] = (np.ones(40) * .8, 1)
        self.assertTrue(natural.arrangement_check(profiles, plan, env, 2)[0])

    def test_lines_cannot_be_cut_at_short_syllabic_gaps(self):
        av = vocal_curve([(0, 3.9), (4.3, 7)])
        bv = vocal_curve([(4.1, 7)])
        self.assertIsNone(natural.handover(av, bv, 4, 2))

    def test_outgoing_complete_line_gets_full_master_gain(self):
        av, bv = vocal_curve([(0, 4)]), vocal_curve([(6, 8)])
        env = natural.handover(av, bv, 8, 2)
        self.assertIsNotNone(env)
        self.assertGreater(env['a_fade_start'], 4)
        self.assertLess(env['a_fade_end'], 6)
        self.assertLess(env['b_fade_end'], 6)

    def test_new_outgoing_phrase_after_exit_need_not_be_played(self):
        av = vocal_curve([(0, 4), (8, 10)], duration=12)
        bv = vocal_curve([(6, 10)], duration=12)
        self.assertIsNotNone(natural.handover(av, bv, 8, 2))

    def test_can_leave_after_earlier_complete_line_before_later_vocals(self):
        av = vocal_curve([(0, 3), (6, 10)], duration=12)
        bv = vocal_curve([(5, 10)], duration=12)
        env = natural.handover(av, bv, 8, 2)
        self.assertIsNotNone(env)
        self.assertGreater(env['a_fade_start'], 3)
        self.assertLess(env['a_fade_end'], 5)

    def test_original_renderer_preserves_the_two_endpoints(self):
        sr = me.SR
        t = np.arange(sr * 8) / sr
        a = np.column_stack([.2 * np.sin(2 * np.pi * 440 * t)] * 2)
        b = np.column_stack([.2 * np.sin(2 * np.pi * 660 * t)] * 2)
        plan = dict(duration=8, local_bpm=120, a_fade_start=5, a_fade_end=7, b_fade_end=3)
        out = natural.render_original(a, b, plan, 'smooth')
        np.testing.assert_allclose(out[:200], a[:200], atol=1e-5)
        np.testing.assert_allclose(out[-sr:], b[-sr:], atol=1e-7)

    def test_shelf_does_not_drain_high_frequencies(self):
        t = np.arange(me.SR) / me.SR
        ratios = []
        for hz in (40, 6000):
            y = np.sin(2 * np.pi * hz * t)[:, None]
            z = natural.shelf_cut(y)
            ratios.append(np.sqrt(np.mean(z[10000:] ** 2) / np.mean(y[10000:] ** 2)))
        self.assertLess(ratios[0], .2)
        self.assertGreater(ratios[1], .99)


class ListeningTests(unittest.TestCase):
    def test_model_requires_held_out_success_and_current_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feedback, model = root / 'feedback.jsonl', root / 'model.json'
            rows = [dict(candidate=f'{pair}_{n}', a=f'a{pair}', b=f'b{pair}',
                         mode='natural', method='natural', verdict=1 if n % 2 else -1,
                         metrics={'level_dip_db': 0 if n % 2 else 8})
                    for pair in range(4) for n in range(8)]
            feedback.write_text('\n'.join(map(json.dumps, rows)))
            with patch.object(dj, 'FEEDBACK_FILE', feedback), patch.object(preferences, 'MODEL_FILE', model):
                report = preferences.train_from_feedback()
                self.assertTrue(report['trained'])
                self.assertGreater(report['held_out_accuracy'], report['majority_baseline'])
                self.assertIsNotNone(preferences.read_model())
                feedback.write_text(feedback.read_text() + '\n')
                self.assertIsNone(preferences.read_model())
                for row in rows:
                    row['metrics'] = {}
                feedback.write_text('\n'.join(map(json.dumps, rows)))
                self.assertFalse(preferences.train_from_feedback()['trained'])
                self.assertIsNone(preferences.read_model())

    def test_no_fake_trained_model_from_a_few_ratings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(dj, 'FEEDBACK_FILE', path / 'feedback.jsonl'), \
                 patch.object(preferences, 'MODEL_FILE', path / 'model.json'):
                report = preferences.train_from_feedback()
                self.assertFalse(report['trained'])
                self.assertFalse((path / 'model.json').exists())
                job = {}
                candidates = [{'score': 2}, {'score': 1}]
                preferences.rank(candidates, job)
                self.assertEqual(candidates[0]['score'], 1)
                self.assertIn('not trained', job['ranking_source'])

    def test_negative_feedback_is_deduplicated_per_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feedback.jsonl'
            entry = {'candidate': 'x', 'mode': 'natural', 'method': 'natural',
                     'a': 'a', 'b': 'b', 'verdict': -1}
            path.write_text('\n'.join(json.dumps(entry) for _ in range(30)))
            with patch.object(dj, 'FEEDBACK_FILE', path):
                result = preferences.train_from_feedback()
            self.assertEqual(result['ratings'], 1)
            self.assertFalse(result['trained'])


class NaturalExportTests(unittest.TestCase):
    def compare_fixture(self, root, level):
        t = np.arange(4 * me.SR) / me.SR
        original = np.column_stack([level * np.sin(2 * np.pi * 6000 * t)] * 2).astype(np.float32)
        silent_stems = {k: np.zeros((8 * me.SR, 2), dtype=np.float32)
                        for k in ('vocals', 'bass', 'drums', 'other')}
        plan = dict(cut=10, b_original_start=2, duration=4, ratio=1, beats=8,
                    local_bpm=120, local_drift_ms=0, mode='natural', plan_score=0)
        env = dict(a_fade_start=2.3, a_fade_end=2.8, b_fade_end=1,
                   outgoing_line_end=2.1, incoming_line_start=3)
        with patch.object(natural, 'plan_candidates', return_value=[plan]), \
             patch.object(natural, 'prepare_b', return_value=root / 'b.wav'), \
             patch.object(natural, 'handover', return_value=env), \
             patch.object(me.StemBank, 'get', return_value=silent_stems), \
             patch.object(me, 'read_audio', return_value=original), \
             patch.object(me, 'rendered_metrics', return_value=dict(level_dip_db=0, level_surge_db=0,
                          band_jump_db=0, peak_excess=0)), \
             patch('analysis.energy_profile', return_value=(np.ones(100), 1)), \
             patch.object(dj, '_duration', return_value=100), patch.object(dj, '_loudness', return_value=0):
            job = {}
            candidates = natural.compare(job, root / 'a.wav', root / 'b.wav', {}, root,
                                         {'bar_len': 2}, {}, {}, {})
        return candidates, job

    def test_analysis_stems_never_replace_originals_and_identical_shapes_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates, job = self.compare_fixture(Path(directory), .2)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(job['candidates_evaluated'], 1)
            audio, _ = natural.sf.read(candidates[0]['pcm'])
            self.assertGreater(float(np.max(abs(audio))), .19)

    def test_heavy_limiter_reduction_is_rejected_even_if_loudness_metrics_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates, job = self.compare_fixture(Path(directory), .9)
            self.assertEqual(candidates, [])
            self.assertIn('excessive limiter reduction', job['rejected_reasons'])

    def test_selected_candidate_reuses_pcm_and_local_ratio_without_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b, matched, pcm = [root / name for name in ('a.wav', 'b.wav', 'matched.wav', 'saved.wav')]
            plan = dict(mode='natural', method='natural', strategy='phrase blend', cut=120,
                        b_start=8 / 1.0123, b_original_start=8, duration=16, beats=32,
                        ratio=1.0123, gain_db=-1, metrics={})
            job = {}
            with patch.object(me, 'load_candidate', return_value=(plan, pcm)), \
                 patch.object(natural, 'prepare_b', return_value=matched) as prepare, \
                 patch.object(me, 'assemble') as assemble, \
                 patch.object(natural, 'compare', side_effect=AssertionError('must reuse audition')), \
                 patch('structure.for_mix', side_effect=AssertionError('must not analyze')):
                me.run(job, a, b, {'candidate': 'a' * 32}, preview=False)
            self.assertEqual(prepare.call_args.args[:2], (b, 1.0123))
            self.assertEqual(assemble.call_args.args[:2], (a, matched))
            self.assertEqual(assemble.call_args.args[3], pcm)
            self.assertEqual(assemble.call_args.args[2]['b_start'], 8 / 1.0123)
            self.assertEqual(job['candidate'], 'a' * 32)
            self.assertEqual(job['source_transition_at'], 120)


if __name__ == '__main__':
    unittest.main()
