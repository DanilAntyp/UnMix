"""Beat timing estimated over bars, with explicit phase ambiguity handling."""
import numpy as np


def bar_period(downbeats):
    t = np.asarray(downbeats, dtype=float)
    t = t[np.isfinite(t)]
    if len(t) < 3 or np.any(np.diff(t) <= 0):
        return None
    # Spans rather than the median of quantized 10 ms beat intervals.
    step = min(8, len(t) - 1)
    periods = (t[step:] - t[:-step]) / step
    median = float(np.median(periods))
    stable = periods[abs(periods - median) < median * .06]
    return float(np.median(stable)) if len(stable) else median


def normalize(grid):
    g = dict(grid)
    period = bar_period(g.get('downbeats') or [])
    if period:
        g.update(raw_bpm=g.get('raw_bpm', g.get('bpm')), bpm=240 / period,
                 beat_len=period / 4, bar_len=period)
    return g


def resolve(grid, phrases):
    """Require a reason to select a bar phase, not merely two similar BPMs."""
    g = normalize(grid)
    if 'phase_disagreement_ms' in grid:
        return g
    primary = np.asarray(g.get('downbeats') or [], dtype=float)
    secondary = np.asarray(phrases.get('downbeats') or [], dtype=float)
    g['phase_confidence'] = .6 if len(primary) >= 4 else 0
    g['timing_source'] = 'beat tracker'
    if len(secondary) < 4:
        return g
    if len(primary) < 4:
        g.update(downbeats=secondary.tolist(), bar=float(secondary[0]),
                 phase_confidence=.7, timing_source='phrase tracker')
        return normalize(g)
    period = bar_period(secondary)
    near = np.min(abs(primary[:, None] - secondary[None, :]), axis=1)
    disagreement = float(np.median(near))
    g['phase_disagreement_ms'] = round(disagreement * 1000)
    if disagreement < period * .08:
        g['phase_confidence'] = .95
        return g
    boundaries = np.array([s['start'] for s in phrases.get('sections', [])
                           if s['start'] > period and s.get('label') not in ('start', 'end')])
    def boundary_error(times):
        return float(np.median(np.min(abs(boundaries[:, None] - times[None, :]), axis=1)))
    # Structural evidence resolves the phase only when multiple boundaries
    # clearly support it. Otherwise do not beat-blend this track automatically.
    if len(boundaries) >= 3 and boundary_error(secondary) < period * .08 and boundary_error(primary) > period * .2:
        g.update(downbeats=secondary.tolist(), bar=float(secondary[0]),
                 phase_confidence=.75, timing_source='structure-anchored bar phase')
        return normalize(g)
    g.update(phase_confidence=0, timing_source='unresolved bar-phase disagreement')
    return g


def local_pair(ga, gb, a_start, b_start, bars):
    """Fit a constant local stretch and measure every bar's residual error."""
    if min(ga.get('phase_confidence', .6), gb.get('phase_confidence', .6)) < .5:
        return None
    a, b = (np.asarray(g.get('downbeats') or [], dtype=float) for g in (ga, gb))
    if min(len(a), len(b)) < bars + 1:
        return None
    ia, ib = int(np.argmin(abs(a - a_start))), int(np.argmin(abs(b - b_start)))
    aa, bb = a[ia:ia + bars + 1], b[ib:ib + bars + 1]
    if min(len(aa), len(bb)) != bars + 1:
        return None
    aa, bb = aa - aa[0], bb - bb[0]
    duration = float(aa[-1])
    if duration <= 0 or np.any(np.diff(aa) <= 0) or np.any(np.diff(bb) <= 0):
        return None
    ratio = float(bb[-1] / duration)
    errors = bb / ratio - aa
    drift = float(np.max(abs(errors)))
    # A stable average is not enough: reject local drift and missed bars.
    if drift > min(.06, duration / bars / 32):
        return None
    return dict(cut=float(a[ia]), b_original_start=float(b[ib]), duration=duration,
                ratio=ratio, local_drift_ms=round(drift * 1000, 2),
                local_bpm=240 * bars / duration)
