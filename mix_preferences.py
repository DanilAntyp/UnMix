"""Small, opt-in listening preference model; never a pretrained DJ judge.

Training needs diverse explicit listener verdicts and must beat a majority
baseline on held-out song pairs. Until then only diagnostic ordering is used.
"""
import hashlib
import json
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
MODEL_FILE = ROOT / 'mix_preference_model.json'
REFERENCE_FILE = ROOT / 'mix_references.json'
_lock = threading.Lock()
FEATURE_VERSION = 1


def features(c):
    m = c.get('metrics', {})
    return np.array([1, min(m.get('level_dip_db', 0), 8) / 8,
                     min(m.get('level_surge_db', 0), 8) / 8,
                     min(m.get('band_jump_db', 0), 12) / 12,
                     m.get('harmonic_conflict', 0), m.get('rhythm_conflict', 0),
                     c.get('beats', 16) / 64, abs(c.get('ratio', 1) - 1) / .06,
                     float(c.get('shape') == 'phrase_swap'),
                     float(c.get('mode') == 'club')], dtype=float)


def read_model():
    import dj
    try:
        model = json.loads(MODEL_FILE.read_text())
        digest = hashlib.sha256(dj.FEEDBACK_FILE.read_bytes()).hexdigest()
        if (model.get('version') == FEATURE_VERSION and model.get('trained')
                and model.get('feedback_digest') == digest
                and len(model['weights']) == 10 and np.isfinite(model['weights']).all()):
            return model
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def rank(candidates, job):
    model = read_model()
    for c in candidates:
        if model:
            logit = float(features(c) @ np.asarray(model['weights']))
            preference = float(1 / (1 + np.exp(-np.clip(logit, -30, 30))))
            c['listener_preference'] = preference
            c['score'] -= .5 * preference
    candidates.sort(key=lambda c: c['score'])
    job['ranking_source'] = 'validated listener preferences + diagnostics' if model else 'diagnostic ordering; not trained on listening preferences'


def _fit(x, y):
    weights = np.zeros(x.shape[1])
    for _ in range(500):
        pred = 1 / (1 + np.exp(-np.clip(x @ weights, -30, 30)))
        regularization = weights.copy()
        regularization[0] = 0
        weights -= .2 * (x.T @ (pred - y) / len(y) + .03 * regularization)
    return weights


def train_from_feedback():
    import dj
    with _lock:
        latest = {}
        feedback_bytes = b''
        try:
            feedback_bytes = dj.FEEDBACK_FILE.read_bytes()
            for line in feedback_bytes.decode().splitlines():
                try:
                    d = json.loads(line)
                    if (d.get('candidate') and d.get('mode') in ('natural', 'club')
                            and d.get('method') != 'native' and d.get('verdict') in (-1, 1)
                            and isinstance(d.get('a'), str) and isinstance(d.get('b'), str)
                            and np.isfinite(features(d)).all()):
                        latest[d['candidate']] = d
                except (ValueError, TypeError, AttributeError):
                    continue
        except (OSError, UnicodeError):
            pass
        rows = list(latest.values())
        pairs = sorted({(r['a'], r['b']) for r in rows},
                       key=lambda p: hashlib.sha256(str(p).encode()).hexdigest())
        positives = sum(r['verdict'] == 1 for r in rows)
        report = {'ratings': len(rows), 'pairs': len(pairs), 'trained': False}
        if len(rows) < 24 or len(pairs) < 4 or min(positives, len(rows) - positives) < 6:
            return {**report, 'reason': 'Need 24 distinct ratings across 4 pairs, with at least 6 likes and 6 dislikes'}
        holdout = set(pairs[::3])
        train = [r for r in rows if (r['a'], r['b']) not in holdout]
        test = [r for r in rows if (r['a'], r['b']) in holdout]
        x = np.stack([features(r) for r in train])
        y = np.array([r['verdict'] == 1 for r in train], dtype=float)
        if len(set(y)) < 2 or len(test) < 6:
            return {**report, 'reason': 'Need a more diverse held-out listening sample'}
        weights = _fit(x, y)
        test_y = np.array([r['verdict'] == 1 for r in test])
        predicted = np.stack([features(r) for r in test]) @ weights >= 0
        accuracy = float(np.mean(predicted == test_y))
        baseline = float(max(np.mean(test_y), 1 - np.mean(test_y)))
        report.update(held_out_accuracy=accuracy, majority_baseline=baseline)
        if accuracy < max(.65, baseline + .05):
            return {**report, 'reason': 'Preferences did not generalize to held-out song pairs; model not promoted'}
        weights = _fit(np.stack([features(r) for r in rows]),
                       np.array([r['verdict'] == 1 for r in rows], dtype=float))
        model = {**report, 'trained': True, 'version': FEATURE_VERSION, 'weights': weights.tolist(),
                 'feedback_digest': hashlib.sha256(feedback_bytes).hexdigest()}
        temporary = MODEL_FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(model))
        temporary.replace(MODEL_FILE)
        return model


def pair_key(A, B):
    return hashlib.sha256(f'{Path(A).resolve()}|{Path(B).resolve()}'.encode()).hexdigest()


def set_reference(A, B, token):
    import mixengine
    mixengine.load_candidate(token, A, B)
    with _lock:
        try:
            refs = json.loads(REFERENCE_FILE.read_text())
        except (OSError, ValueError):
            refs = {}
        refs[pair_key(A, B)] = token
        temporary = REFERENCE_FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(refs))
        temporary.replace(REFERENCE_FILE)


def get_reference(A, B):
    import dj
    import mixengine
    try:
        token = json.loads(REFERENCE_FILE.read_text())[pair_key(A, B)]
        plan, _ = mixengine.load_candidate(token, A, B)
        file = dj.DJ_DIR / f'preview_{token}_automix.mp3'
        if file.is_file():
            return {'candidate': token, 'style': 'automix', 'style_used': 'cut' if plan['method'] == 'native' else 'automix',
                    'reference': True, 'name': 'Your approved listening reference',
                    'file': f'/djmixes/{file.name}', 'transition_at': min(12, plan['cut']),
                    'transition_duration': plan['duration'], 'method': plan['method']}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
