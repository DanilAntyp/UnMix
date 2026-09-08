"""Original-master DJ mixing. Separated audio is analysis-only in these modes.

Timing and musical constraints decide whether a blend is possible. Signal
metrics are guards, not a claim that a candidate will please a listener.
"""
import tempfile
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter

import mix_timing as timing
import mixengine as me


def shelf_cut(y, db=-18, frequency=180):
    """RBJ low shelf, not complementary subtraction bands with uneven gains."""
    amplitude = 10 ** (db / 40)
    w = 2 * np.pi * frequency / me.SR
    c, s = np.cos(w), np.sin(w)
    alpha = s / 2 * np.sqrt(2)
    q = 2 * np.sqrt(amplitude) * alpha
    b = amplitude * np.array([(amplitude + 1) - (amplitude - 1) * c + q,
                              2 * ((amplitude - 1) - (amplitude + 1) * c),
                              (amplitude + 1) - (amplitude - 1) * c - q])
    a = np.array([(amplitude + 1) + (amplitude - 1) * c + q,
                  -2 * ((amplitude - 1) + (amplitude + 1) * c),
                  (amplitude + 1) + (amplitude - 1) * c - q])
    return lfilter(b / a[0], a / a[0], y, axis=0)


def vocal_lines(y):
    """Conservative line regions: a syllabic pause is not a mix-out point."""
    active, win = me.activity(y)
    hits = np.flatnonzero(active)
    for l, r in zip(hits[:-1], hits[1:]):
        if (r - l) * win <= .65:
            active[l:r + 1] = True
    return active, win


def handover(av, bv, duration, bar, a_offset=0, b_offset=0):
    """Return master-fader timing only when complete vocal lines can survive."""
    def times(curve, offset):
        values, win = curve
        return np.arange(len(values)) * win - offset, np.asarray(values, dtype=bool)
    ta, va = times(av, a_offset)
    tb, vb = times(bv, b_offset)
    # Entry must not bisect B. A may leave after an earlier complete line:
    # later A lines are irrelevant once its master fader is fully down.
    if vb[(tb >= -.25) & (tb < 0)].any() and vb[(tb >= 0) & (tb < .12)].any():
        return None
    b_hits = tb[vb & (tb >= 0) & (tb < duration)]
    first = float(b_hits[0]) if len(b_hits) else duration
    deadline = min(duration, first - .08) if len(b_hits) else duration
    # Find gaps between whole detected lines, not just the last line in T.
    # Include post-exit context so a line crossing T cannot look complete.
    active_times = ta[va & (ta >= 0)]
    gaps, left = [], 0.0
    for t in active_times:
        right = min(float(t) - .08, deadline)
        if right - left >= .3:
            gaps.append((left, right))
        left = max(left, float(t) + av[1] + .15)
        if t > deadline:
            break
    if deadline - left >= .3:
        gaps.append((left, deadline))
    if not gaps:
        return None
    # Prefer the latest safe handover, but never fade through a sung line.
    left, end = gaps[-1]
    start = max(left, end - min(bar, duration * .45))
    earlier = ta[va & (ta >= 0) & (ta < start)]
    last = float(earlier[-1] + av[1]) if len(earlier) else 0
    b_in = max(.015, min(duration * .45, first - .06))
    return dict(a_fade_start=start, a_fade_end=end, b_fade_end=b_in,
                outgoing_line_end=last, incoming_line_start=first)


def render_original(a, b, plan, shape):
    n = min(len(a), len(b), round(plan['duration'] * me.SR))
    a, b = a[:n], b[:n]
    t = np.arange(n) / me.SR
    ga = 1 - me.ramp(t, plan['a_fade_start'], plan['a_fade_end'])
    gb = me.ramp(t, 0, plan['b_fade_end'])
    center = (plan['a_fade_start'] + plan['a_fade_end']) / 2
    # Bass moves when dominance moves, not at an arbitrary 50% of every mix.
    width = min(plan['duration'] * .25, 240 / plan['local_bpm'])
    if shape == 'phrase_swap':
        bar = 240 / plan['local_bpm']
        downbeats = np.arange(0, plan['duration'] + .001, bar)
        valid = downbeats[(downbeats >= plan['a_fade_start']) & (downbeats <= plan['a_fade_end'])]
        if len(valid):
            center = float(valid[np.argmin(abs(valid - center))])
            width = .08
    transfer = me.ramp(t, center - width / 2, center + width / 2)
    # Until the swap A retains its body; B enters as supporting percussion.
    aa = a * (1 - transfer[:, None]) + shelf_cut(a) * transfer[:, None]
    bb = shelf_cut(b) * (1 - transfer[:, None]) + b * transfer[:, None]
    return (aa * ga[:, None] + bb * gb[:, None]).astype(np.float32)


def plan_candidates(A, B, ga, gb, pa, pb, opts, profiles):
    import dj
    mode = opts.get('mode', 'natural')
    raw = opts.get('beats')
    lengths = ((32, 16) if mode == 'natural' else (64, 32)) if raw in (None, '', 'auto', 0) else (int(raw),)
    if any(n not in (4, 8, 16, 32, 64) for n in lengths):
        raise ValueError('choose 4, 8, 16, 32 or 64 beats')
    duration_a, duration_b = dj._duration(A), dj._duration(B)
    agrid, bgrid = ga.get('downbeats') or [], gb.get('downbeats') or []
    if not agrid or not bgrid:
        return []
    first_core = next((s['start'] for s in pb['sections'] if s.get('label') in ('verse', 'chorus')
                       and s['energy'] > .55 and s['start'] > gb['bar_len']), 90)
    # Preserve B's first main section. Do not automatically jump to a later hook.
    b_limit = min(first_core, 90) if mode == 'natural' else min(120, duration_b / 2)
    choices = []
    va, vb = dj._vocal_curve(A), dj._vocal_curve(B)
    for beats in lengths:
        bars = beats // 4
        expected = bars * ga['bar_len']
        ends = [s['end'] for s in pa['sections'] if s['end'] > max(20, duration_a * .5)
                and s.get('label') not in ('start', 'end')]
        # Verse/chorus labels are macrosections, not the only musical phrases.
        for s in pa['sections']:
            anchor = int(np.argmin(abs(np.asarray(agrid) - s['start'])))
            ends.extend(t for t in agrid[anchor + 4::4]
                        if max(s['start'], duration_a * .5) < t < s['end'])
        exits = [max(0, e - expected) for e in ends]
        if opts.get('cut') is not None:
            exits = [float(opts['cut'])]
        entries = [float(bgrid[0])]
        for s in pb['sections']:
            if s['start'] <= b_limit + gb['bar_len']:
                entries += [s['start'], s['start'] - bars * gb['bar_len']]
                anchor = int(np.argmin(abs(np.asarray(bgrid) - s['start'])))
                entries.extend(t for t in bgrid[anchor + 4::4]
                               if s['start'] < t < min(s['end'], b_limit))
        if opts.get('b_start') is not None:
            entries = [float(opts['b_start'])]
        for cut in exits:
            for start in entries:
                if start < 0 or (opts.get('b_start') is None and start > b_limit):
                    continue
                c = timing.local_pair(ga, gb, cut, start, bars)
                if not c or abs(c['ratio'] - 1) > dj.MAX_TEMPO_CHANGE:
                    continue
                ca, cb, T, k = c['cut'], c['b_original_start'], c['duration'], c['ratio']
                if ca < 10 or ca + T > duration_a - .1 or cb + T * k + 1 > duration_b:
                    continue
                # Prevent drifting across the structural boundary we planned to leave.
                if opts.get('cut') is None and min(abs(ca + T - e) for e in ends) > ga['bar_len'] * .15:
                    continue
                energy = dj._energy_handover(profiles, ca, cb / k, k, T, 'automix')
                score = energy + .3 * (duration_a - ca - T) / duration_a + .2 * cb / duration_b
                role = next((s.get('label') for s in pb['sections'] if s['start'] - .1 <= cb < s['end']), None)
                score += 1.5 if role in ('verse', 'chorus') else 0
                score += 3 * dj._vocal_clash(va, vb, ca, cb / k, k, T, 'crossfade')
                if beats != lengths[0]:
                    score += .12  # prefer a full phrase, not universally the shortest fade
                choices.append({**c, 'beats': beats, 'plan_score': score, 'mode': mode,
                                'cut_reason': 'complete musical phrase', 'entry_plan': 'instrumental runway',
                                'strategy': 'phrase blend', 'method': mode})
    choices.sort(key=lambda c: c['plan_score'])
    return diverse_plans(choices, ga['bar_len'])


def diverse_plans(choices, bar, limit=10):
    """One rejected incoming vocal phrase must not consume every audition."""
    groups = {}
    for c in choices:
        groups.setdefault(round(c['b_original_start'], 3), []).append(c)
    selected = []
    while groups and len(selected) < limit:
        for key in list(groups):
            group = groups[key]
            while group:
                c = group.pop(0)
                if all(abs(c['cut'] - p['cut']) + abs(c['b_original_start'] - p['b_original_start']) > bar
                       or c['beats'] != p['beats'] for p in selected):
                    selected.append(c)
                    break
            if not group:
                del groups[key]
            if len(selected) == limit:
                break
    return selected


def prepare_b(path, ratio, work):
    import dj
    out = work / f'b_{ratio:.8f}.wav'
    if not out.exists():
        af = f'atempo={ratio:.8f}' if abs(ratio - 1) > .00001 else 'anull'
        dj._run(['ffmpeg', '-y', '-i', str(path), '-af', af, '-ac', '2', '-ar', str(me.SR),
                 '-c:a', 'pcm_f32le', str(out)])
    return out


def arrangement_check(profiles, plan, env, bar):
    """Reject a large intensity-role mismatch at the actual master handover."""
    import dj
    cut, start, ratio = plan['cut'], plan['b_original_start'], plan['ratio']
    center = (env['a_fade_start'] + env['a_fade_end']) / 2
    before = dj._local_energy(profiles['energy_a'], max(0, cut - bar), cut)
    outgoing = dj._local_energy(profiles['energy_a'], cut + max(0, center - bar), cut + center)
    incoming = dj._local_energy(profiles['energy_b'], start + center * ratio,
                                start + (center + bar) * ratio)
    mismatch, collapse = abs(incoming - outgoing), max(0, before - outgoing)
    return mismatch <= .4 and collapse <= .35, dict(energy_mismatch=mismatch, outgoing_collapse=collapse)


def native_candidate(A, B, work, opts, reason):
    import dj
    T = .08
    cut = float(opts['cut']) if opts.get('cut') is not None else max(0, dj._duration(A) - T)
    start = float(opts['b_start']) if opts.get('b_start') is not None else 0
    cut = float(np.clip(cut, 0, max(0, dj._duration(A) - T)))
    start = float(np.clip(start, 0, max(0, dj._duration(B) - T)))
    a, b = me.read_audio(A, cut, T), me.read_audio(B, start, T)
    n = min(len(a), len(b))
    t = np.arange(n) / me.SR
    fade = me.ramp(t, 0, T)
    pcm = work / 'native.wav'
    sf.write(pcm, a[:n] * (1 - fade[:, None]) + b[:n] * fade[:, None], me.SR, subtype='FLOAT')
    return dict(cut=cut, b_original_start=start, b_start=start, duration=T, beats=0,
                ratio=1.0, gain_db=0.0, pcm=pcm, b_audio=B, method='native', shape='native',
                mode=opts.get('mode', 'natural'), strategy='native-tempo handover',
                cut_reason=reason, plan_score=0, score=0, metrics={}, local_drift_ms=None)


def inspect(A, B, opts):
    """Cheap, provisional markers from the same planner as the renderer."""
    import analysis
    import dj
    import structure
    if opts.get('mode', 'natural') not in ('natural', 'club'):
        raise ValueError('unknown mixing mode')
    pa, pb = [structure.for_mix(p) for p in (A, B)]
    ga, gb = [timing.resolve(dj._load_grid(analysis, p), phrases) for p, phrases in ((A, pa), (B, pb))]
    profiles = {'energy_a': analysis.energy_profile(A), 'energy_b': analysis.energy_profile(B), 'bar': ga['bar_len']}
    choices = plan_candidates(A, B, ga, gb, pa, pb, opts, profiles)
    c = choices[0] if choices else {'cut': max(0, dj._duration(A) - .08), 'b_original_start': 0,
                                    'duration': .08, 'beats': 0}
    def deck(path, grid, phrases):
        return dict(duration=dj._duration(path), bpm=round(grid['bpm'], 3), bar_phase=grid['bar'],
                    bar_len=grid['bar_len'], downbeats=grid.get('downbeats'), sections=phrases['sections'])
    return dict(style_used='automix' if choices else 'cut', T=c['duration'], beats=c['beats'],
                length_reason='provisional phrase; vocal lines checked during render',
                entry_plan='instrumental runway' if choices else 'native playback',
                a={**deck(A, ga, pa), 'cut': c['cut'], 'cut_reason': 'provisional complete phrase'},
                b={**deck(B, gb, pb), 'b_start': c['b_original_start']})


def compare(job, A, B, opts, work, ga, gb, pa, pb):
    import analysis
    import dj
    profiles = {'energy_a': analysis.energy_profile(A), 'energy_b': analysis.energy_profile(B),
                'bar': ga['bar_len']}
    plans = plan_candidates(A, B, ga, gb, pa, pb, opts, profiles)
    bank = me.StemBank(work, ga['bar_len'])
    candidates, rejected = [], []
    checks = []
    evaluated = 0
    for index, p in enumerate(plans):
        job.update(stage=f'Checking complete vocal lines and original audio ({index + 1}/{len(plans)})...',
                   pct=15 + 65 * index / max(1, len(plans)))
        T, cut = p['duration'], p['cut']
        check = dict(cut=cut, b_start=p['b_original_start'], beats=p['beats'], decision='checking')
        checks.append(check)
        matched = prepare_b(B, p['ratio'], work)
        start = p['b_original_start'] / p['ratio']
        bar = 240 / p['local_bpm']
        a, b = me.read_audio(A, cut, T), me.read_audio(matched, start, T)
        pre_a, pre_b = min(bar, cut), min(bar, start)
        try:
            sa = bank.get(A, cut - pre_a, min(T + pre_a + bar, dj._duration(A) - cut + pre_a), job)
            sb = bank.get(matched, start - pre_b, min(T + pre_b + bar, dj._duration(matched) - start + pre_b), job)
            env = handover(vocal_lines(sa['vocals']), vocal_lines(sb['vocals']), T, bar, pre_a, pre_b)
            if env is None:
                check['decision'] = 'no complete vocal-line handover'
                rejected.append('no complete vocal-line handover')
                continue
            arrangement_ok, energy_metrics = arrangement_check(profiles, p, env, bar)
            check.update(energy_metrics)
            if not arrangement_ok:
                check['decision'] = 'breakdown/drop mismatch or outgoing energy collapse'
                rejected.append(check['decision'])
                continue
            ia, ib = round(pre_a * me.SR), round(pre_b * me.SR)
            span = round(T * me.SR)
            frames = np.arange(0, T, .5)
            gaud = 1 - me.ramp(frames, env['a_fade_start'], env['a_fade_end'])
            gbud = me.ramp(frames, 0, env['b_fade_end'])
            harmony = me.harmonic_conflict((sa['other'] + sa['bass'])[ia:ia + span],
                                           (sb['other'] + sb['bass'])[ib:ib + span],
                                           overlap=np.minimum(gaud, gbud))
            audible_end = max(1, round(env['a_fade_end'] * me.SR))
            rhythm = me.rhythm_conflict(sa['drums'][ia:ia + audible_end], sb['drums'][ib:ib + audible_end], bar / 4)
            check.update(harmonic_conflict=harmony, rhythm_conflict=rhythm, **env)
            if harmony > .35 or rhythm > .4:
                check['decision'] = 'competing melody or unstable rhythm'
                rejected.append('competing melody or unstable rhythm')
                continue
        except (RuntimeError, ValueError, OSError) as exc:
            check['decision'] = 'analysis unavailable'
            rejected.append(f'vocal analysis unavailable: {str(exc)[:100]}')
            continue
        # Match the established track to B after its introduction, not its quiet intro.
        la, lb = dj._loudness(A, max(0, cut - bar), bar * 2), dj._loudness(matched, start + T, bar * 2)
        gain_db = float(np.clip(la - lb, -2.5, 2.5)) if la is not None and lb is not None else 0
        b *= 10 ** (gain_db / 20)
        previous_mixed = None
        for shape in ('smooth', 'phrase_swap'):
            plan = {**p, **env, 'gain_db': gain_db, 'b_start': start, 'shape': shape, 'b_audio': matched}
            mixed = render_original(a, b, plan, shape)
            if previous_mixed is not None and np.array_equal(mixed, previous_mixed):
                continue  # no valid bass-swap bar: do not offer identical A/B versions
            previous_mixed = mixed
            evaluated += 1
            metrics = me.rendered_metrics(mixed, a, b, bar)
            reduction = max(0.0, float(20 * np.log10(max(float(np.max(abs(mixed))), 1e-9) / .9)))
            metrics.update(vocal_overlap=0, harmonic_conflict=harmony, rhythm_conflict=rhythm,
                           local_drift_ms=p['local_drift_ms'], limiter_reduction_db=reduction, **energy_metrics)
            if metrics['level_dip_db'] > 3 or metrics['level_surge_db'] > 3:
                check['decision'] = 'audible level discontinuity'
                rejected.append('audible level discontinuity')
                continue
            if reduction > 3:
                check['decision'] = 'excessive limiter reduction'
                rejected.append(check['decision'])
                continue
            pcm = work / f'natural{len(candidates)}.wav'
            sf.write(pcm, mixed, me.SR, subtype='FLOAT')
            plan.update(pcm=pcm, metrics=metrics, score=p['plan_score'] + me.candidate_score(metrics))
            candidates.append(plan)
            check['decision'] = 'accepted'
    job.update(candidates_evaluated=evaluated, candidates_accepted=len(candidates),
               rejected_reasons=sorted(set(rejected)), placement_checks=checks)
    # Listening preferences can break ties, but never bypass the above guards.
    import mix_preferences
    mix_preferences.rank(candidates, job)
    best = []
    for c in candidates:
        if all((c['cut'], c['b_start'], c['shape']) != (x['cut'], x['b_start'], x['shape']) for x in best):
            best.append(c)
        if len(best) == 3:
            break
    return best


def run(job, A, B, opts, preview=False):
    import analysis
    import dj
    import structure
    work = Path(tempfile.mkdtemp(prefix='natural_dj_'))
    try:
        selected = opts.get('candidate')
        if selected:
            c, pcm = me.load_candidate(selected, A, B)
            matched = prepare_b(B, c['ratio'], work)
            c = {**c, 'pcm': pcm, 'b_audio': matched}
            candidates = [c]
        else:
            job['stage'] = 'Resolving musical phrases and bar phase...'
            pa, pb = [structure.for_mix(p, compute_missing=True) for p in (A, B)]
            ga, gb = [timing.resolve(dj._load_grid(analysis, p), phrases) for p, phrases in ((A, pa), (B, pb))]
            job['timing'] = [{k: g.get(k) for k in ('bpm', 'raw_bpm', 'timing_source',
                              'phase_confidence', 'phase_disagreement_ms')} for g in (ga, gb)]
            job['structure_analysis'] = [pa['source'], pb['source']]
            job['analysis_notes'] = [p['note'] for p in (pa, pb) if p.get('note')]
            candidates = compare(job, A, B, opts, work, ga, gb, pa, pb)
            if not candidates:
                reason = 'No confident phrase blend; preserved the original recordings at native tempo'
                job['fallback'] = reason
                candidates = [native_candidate(A, B, work, opts, reason)]
            c = candidates[0]
        if preview:
            previews = me.save_candidates(job, A, B, c['b_audio'], candidates, opts)
            # A plain playback control is honest; it is not a human-DJ reference.
            if c['method'] != 'native':
                baseline = native_candidate(A, B, work, {}, 'native playback reference')
                control = me.save_candidates(job, A, B, B, [baseline], opts)[0]
                control.update(name='Native playback reference', recommended=False, reference=True)
                previews.append(control)
            import mix_preferences
            approved = mix_preferences.get_reference(A, B)
            if approved and all(p['candidate'] != approved['candidate'] for p in previews):
                previews.append(approved)
            job.update(previews=previews, file=previews[0]['file'], candidate=previews[0]['candidate'])
        else:
            if not selected:
                previews = me.save_candidates(job, A, B, c['b_audio'], [c], opts)
                selected = previews[0]['candidate']
            out = dj.DJ_DIR / f'{A.stem[:55]}_to_{B.stem[:55]}_{c["mode"]}_{uuid.uuid4().hex[:8]}.mp3'
            me.assemble(A, c['b_audio'], c, c['pcm'], out)
            job.update(file=f'/djmixes/{out.name}', candidate=selected)
        job.update(style_used='cut' if c['method'] == 'native' else 'automix',
                   mode=c['mode'], method=c['method'], strategy=c['strategy'],
                   style_chosen='automix', auto_reason=me.METHODS[c['method']],
                   stretch=round(c['ratio'], 5), beats_used=c['beats'],
                   source_transition_at=c['cut'], transition_at=min(12, c['cut']) if preview else c['cut'],
                   transition_duration=c['duration'], b_skip=c['b_original_start'],
                   local_gain_db=c['gain_db'], metrics=c['metrics'],
                   key_action='original pitch and recordings preserved', pct=100,
                   stage='Done. Original-audio auditions are ready; compare them with the playback reference.')
    finally:
        dj._cleanup(work)
