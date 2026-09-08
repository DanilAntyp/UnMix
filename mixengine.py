"""Candidate-based DJ mixing with gradual EQ and measurements of rendered audio.

Scores are engineering diagnostics, not a prediction of a listener's taste.
Saved auditions retain their exact PCM transition for reproducible exports.
"""
import json
import re
import subprocess
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

SR = 44100
VERSION = 3
# Lossy encoding can overshoot a PCM sample ceiling; leave extra headroom.
OUTPUT_LIMITER = ("aresample=176400,alimiter=limit=0.90:level=false:latency=true,"
                  "aresample=44100,volume=0.90")
METHODS = {"original": "Original audio · gradual EQ",
           "natural": "Original recordings · phrase handover",
           "club": "Original recordings · club blend",
           "native": "Original recordings · native-tempo handover",
           "vocal_assist": "Vocal handover · gradual EQ",
           "stems": "Separated instruments · gradual blend"}


def read_audio(path, start, duration):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-ss", f"{max(0, start):.6f}",
           "-t", f"{duration:.6f}", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode:
        raise RuntimeError("could not decode transition audio")
    return np.frombuffer(r.stdout, dtype="<f4").reshape(-1, 2).copy()


def bands(y, sr=SR):
    """Complementary bands: summing them reconstructs the unmodified signal."""
    low = sosfilt(butter(2, 180, fs=sr, output="sos"), y, axis=0)
    below_high = sosfilt(butter(2, 3000, fs=sr, output="sos"), y, axis=0)
    high = y - below_high
    return low, below_high - low, high


def ramp(t, start, end):
    x = np.clip((t - start) / max(end - start, 1e-6), 0, 1)
    return x * x * (3 - 2 * x)


def eq_gains(n, sr, duration, bar, shape="gradual", clash=False):
    t = np.arange(n) / sr
    width = min(2 * bar, 0.5 * duration)
    center = duration * (0.55 if shape == "late_bass" else 0.5)
    low = ramp(t, center - width / 2, center + width / 2)
    mid = ramp(t, 0.15 * duration, 0.90 * duration)
    high = ramp(t, 0, duration)
    a, b = [1 - low, 1 - mid, 1 - high], [low, mid, high]
    if clash:
        # Exchange melodic content in sequence; keep percussion flowing.
        a[1] = 1 - ramp(t, 0.15 * duration, 0.5 * duration)
        b[1] = ramp(t, 0.5 * duration, 0.9 * duration)
    return a, b


def parse_vocal_envelope(expression, t):
    match = re.fullmatch(r"afade=t=(in|out):st=([\d.]+):d=([\d.]+)", expression)
    if not match:
        raise ValueError("invalid vocal envelope")
    kind, start, duration = match.groups()
    value = np.clip((t - float(start)) / float(duration), 0, 1)
    return value if kind == "in" else 1 - value


def chroma(y, sr=SR):
    import analysis
    mono = y[::2].mean(axis=1)
    spec = analysis._stft_mag(mono)
    freq = np.fft.rfftfreq(analysis.N_FFT, 1 / (sr / 2))
    mask = (freq > 65) & (freq < 1500)
    notes = np.round(69 + 12 * np.log2(freq[mask] / 440)).astype(int) % 12
    values = np.stack([spec[:, mask][:, notes == pc].sum(axis=1) for pc in range(12)], axis=1)
    # Resolve harmony locally in half-second frames, not a single song key.
    step = max(1, round(0.5 * (sr / 2) / analysis.HOP))
    return np.stack([values[i:i + step].mean(axis=0) for i in range(0, len(values), step)])


def harmonic_conflict(a, b, overlap=None):
    ca, cb = chroma(a), chroma(b)
    n = min(len(ca), len(cb))
    ca, cb = ca[:n], cb[:n]
    ca /= np.maximum(ca.sum(axis=1, keepdims=True), 1e-9)
    cb /= np.maximum(cb.sum(axis=1, keepdims=True), 1e-9)
    interval = (np.arange(12)[:, None] - np.arange(12)[None, :]) % 12
    dissonance = np.array([0, 1, .65, .15, .1, .1, 1, .1, .1, .15, .65, 1])[interval]
    roughness = np.einsum("ni,ij,nj->n", ca, dissonance, cb)
    strength = np.clip((np.minimum(ca.max(axis=1), cb.max(axis=1)) - 1 / 12) * 4, 0, 1)
    conflict = np.clip((roughness - .28) * 3, 0, 1) * strength
    if overlap is not None:
        # Match chroma's approximately half-second analysis frames. Muted
        # future phrases must not veto an otherwise safe audible handover.
        weights = np.asarray(overlap, dtype=float)
        positions = np.linspace(0, max(0, len(weights) - 1), n)
        conflict *= np.interp(positions, np.arange(len(weights)), weights)
    return float(np.mean(conflict))


def rhythm_conflict(a, b, beat, sr=SR):
    """Penalize near-but-misaligned kick attacks; different patterns are allowed."""
    from scipy.signal import find_peaks
    def attacks(y):
        low = sosfilt(butter(2, 180, fs=sr, output="sos"), y.mean(axis=1))
        hop = max(1, round(sr * .01))
        n = len(low) // hop
        energy = np.sqrt(np.mean(low[:n * hop].reshape(n, hop) ** 2, axis=1))
        rise = np.maximum(0, np.diff(energy, prepend=energy[0]))
        peak = float(rise.max()) if len(rise) else 0
        if peak < 1e-5:
            return np.array([])
        return find_peaks(rise, height=peak * .25, distance=max(1, round(beat * 25)))[0] * .01
    pa, pb = attacks(a), attacks(b)
    if len(pa) < 2 or len(pb) < 2:
        return 0.0
    distances = np.min(abs(pa[:, None] - pb[None, :]), axis=1)
    close = distances < beat * .35
    return float(np.mean(np.clip((distances[close] - .025) / (beat * .2), 0, 1))) if close.any() else 0.0


def block_rms(y, seconds=.25, sr=SR):
    hop = max(1, round(sr * seconds))
    n = len(y) // hop
    return np.sqrt(np.mean(y[:n * hop].reshape(n, hop, -1) ** 2, axis=(1, 2)) + 1e-12)


def rendered_metrics(y, a, b, bar, sr=SR):
    """Measure un-limited candidate audio so overload cannot hide behind a limiter."""
    level = block_rms(y, sr=sr)
    left = float(np.sqrt(np.mean(a[:max(1, round(bar * sr))] ** 2) + 1e-12))
    right = float(np.sqrt(np.mean(b[-max(1, round(bar * sr)):] ** 2) + 1e-12))
    expected = np.linspace(left, right, len(level))
    db = 20 * np.log10(np.maximum(level, 1e-6) / np.maximum(expected, 1e-6))
    low, mid, high = bands(y, sr)
    band_db = np.stack([20 * np.log10(np.maximum(block_rms(z, seconds=bar / 2, sr=sr), 1e-6))
                        for z in (low, mid, high)])
    jumps = np.abs(np.diff(band_db, axis=1))
    return {
        "level_surge_db": float(max(0, np.percentile(db, 90) - 2)),
        "level_dip_db": float(max(0, -np.percentile(db, 10) - 3)),
        "band_jump_db": float(max(0, np.percentile(jumps, 90) - 4)) if jumps.size else 0.0,
        "peak_excess": float(max(0, np.max(np.abs(y)) - .97)),
    }


def candidate_score(metrics):
    return (metrics["level_surge_db"] * .5 + metrics["level_dip_db"] * .7
            + metrics["band_jump_db"] * .3 + metrics["peak_excess"] * 3
            + metrics.get("vocal_overlap", 0) * 8
            + metrics.get("harmonic_conflict", 0) * 3
            + metrics.get("rhythm_conflict", 0) * 2
            + metrics.get("separation_residual", 0) * 2)


def synthesize(a, b, sa, sb, duration, bar, method, shape, clash, vocal_env):
    n = min(len(a), len(b), round(duration * SR))
    a, b = a[:n], b[:n]
    ga, gb = eq_gains(n, SR, duration, bar, shape, clash)
    t = np.arange(n) / SR
    if method == "original":
        ia, ib = a, b
        voices = 0
    else:
        av, bv = sa["vocals"][:n], sb["vocals"][:n]
        if method == "vocal_assist":
            ia, ib = a - av, b - bv
        else:
            ia = sum(sa[k][:n] for k in ("drums", "bass", "other"))
            ib = sum(sb[k][:n] for k in ("drums", "bass", "other"))
        voices = av * parse_vocal_envelope(vocal_env[0], t)[:, None]
        voices += bv * parse_vocal_envelope(vocal_env[1], t)[:, None]
    mixed = sum(x * g[:, None] for x, g in zip(bands(ia), ga))
    mixed += sum(x * g[:, None] for x, g in zip(bands(ib), gb))
    return (mixed + voices).astype(np.float32), ga, gb


class StemBank:
    def __init__(self, work, bar):
        self.work, self.bar, self.windows = work, bar, []

    def get(self, path, start, duration, job):
        import dj
        for p, lo, data in self.windows:
            end = lo + len(data["vocals"]) / SR
            if p == path and lo <= start and end >= start + duration - .025:
                i = round((start - lo) * SR)
                return {k: v[i:i + round(duration * SR)] for k, v in data.items()}
        lo = max(0, start - 2 * self.bar)
        span = min(dj._duration(path) - lo, duration + start - lo + 2 * self.bar)
        job["stage"] = "Separating vocal and rhythm detail (Demucs)..."
        paths = dj._separate_window(path, lo, span, self.work, f"bank{len(self.windows)}")
        data = {k: sf.read(p, dtype="float32", always_2d=True)[0] for k, p in paths.items()}
        if lo + len(data["vocals"]) / SR < start + duration - .025:
            raise ValueError("not enough separated audio for this candidate")
        self.windows.append((path, lo, data))
        return self.get(path, start, duration, job)


def activity(y):
    rms = block_rms(y, .02)
    active = rms > max(.0015, float(np.percentile(rms, 95)) * .16)
    hits = np.flatnonzero(active)
    for l, r in zip(hits[:-1], hits[1:]):
        if r - l < 9:
            active[l:r] = True
    return active, .02


def fingerprint(path):
    p = Path(path).resolve()
    return [str(p), p.stat().st_mtime_ns, p.stat().st_size]


def plan_candidates(ctx, A, B, bvar, opts):
    import dj
    k, bar, bpm = bvar["k"], ctx["bar"], ctx["bpm"]
    gb = dj._grid_scaled(ctx["grid_b_orig"], k)
    raw = opts.get("beats")
    lengths = (8, 16, 32) if raw in (None, "", 0, "auto") else (int(raw),)
    if any(n not in (4, 8, 16, 32, 64) for n in lengths):
        raise ValueError("choose 4, 8, 16, 32 or 64 beats")
    va, vb = dj._vocal_curve(A), dj._vocal_curve(ctx["original_b"])
    duration_b = dj._duration(B)
    choices = []
    for nb in lengths:
        T = max(2, nb * 60 / bpm)
        exits = ([(dj._manual_cut(ctx, A, "automix", T, float(opts["cut"])), None, "manual")]
                 if opts.get("cut") is not None else dj._exit_candidates(ctx, A, "automix", T))
        entries = dj._entry_candidates(bvar, "automix", T)
        for ca, _, ra in exits:
            for cb, _, rb in entries:
                if cb < 0 or cb + T + .1 > duration_b:
                    continue
                aligned = dj._align_transition(A, ca, B, cb, T, bpm, ctx["grid_a"], gb)
                if aligned[1] < .35:
                    continue
                delta = aligned[0]
                cb = max(0, cb - delta)
                if cb + T + .1 > duration_b:
                    continue
                score = (dj._energy_handover(ctx, ca, cb, k, T, "automix")
                         + dj._dead_air(ctx, ca, cb, k, T)
                         + .5 * dj._vocal_clash(va, vb, ca, cb, k, T, "crossfade")
                         + .04 * nb / 8)
                choices.append(dict(cut=ca, b_start=cb, duration=T, beats=nb, plan_score=score,
                                    cut_reason=ra, entry_plan=rb, nudge_ms=round(delta * 1000)))
    choices.sort(key=lambda x: (x["plan_score"], -x["cut"], x["b_start"]))
    selected = []
    for c in choices:
        if all(abs(c["cut"] - p["cut"]) + abs(c["b_start"] - p["b_start"]) > bar
               or c["beats"] != p["beats"] for p in selected):
            selected.append(c)
        if len(selected) == 4:
            break
    return selected


def compare(job, A, original_b, opts, ctx):
    import dj
    ctx["original_b"] = original_b
    bvar = ctx["make_b"](True, opts.get("b_start"))
    B, k = bvar["file"], bvar["k"]
    bank = StemBank(ctx["work"], ctx["bar"])
    plans = plan_candidates(ctx, A, B, bvar, opts)
    rendered, failures = [], []
    evaluated = 0
    for index, plan in enumerate(plans):
        cut, start, T = plan["cut"], plan["b_start"], plan["duration"]
        job.update(stage=f"Comparing musical placements ({index + 1}/{len(plans)})...",
                   pct=10 + 65 * index / max(1, len(plans)))
        a, b = read_audio(A, cut, T), read_audio(B, start, T)
        n = min(len(a), len(b))
        a, b = a[:n], b[:n]
        la, lb = dj._loudness(A, cut, T), dj._loudness(B, start, T)
        gain_db = float(np.clip(la - lb, -3, 3)) if la is not None and lb is not None else 0
        gain = 10 ** (gain_db / 20)
        b *= gain
        sa = sb = env = None
        residual = 0.0
        try:
            sa = bank.get(A, cut, T, job)
            sb = {name: y * gain for name, y in bank.get(B, start, T, job).items()}
            n = min(n, *(len(y) for y in (*sa.values(), *sb.values())))
            a, b = a[:n], b[:n]
            sa, sb = {k: v[:n] for k, v in sa.items()}, {k: v[:n] for k, v in sb.items()}
            av, bv = activity(sa["vocals"]), activity(sb["vocals"])
            env = dj._vocal_handoff_curves(av, bv, T)
            residual = max(float(np.sqrt(np.mean((sum(s.values())[:n] - raw) ** 2))
                                 / max(np.sqrt(np.mean(raw ** 2)), 1e-6))
                           for s, raw in ((sa, a), (sb, b)))
        except Exception as e:
            failures.append(f"stem analysis: {str(e)[:160]}")
            sa = sb = env = None
            av = dj._curve_segment(dj._vocal_curve(A), cut, T) if dj._vocal_curve(A) is not None else None
            vb = dj._vocal_curve(original_b)
            bv = dj._curve_segment((vb[0], vb[1] / k), start, T) if vb is not None else None
        melodic_a = sa["other"] + sa["bass"] if sa else a
        melodic_b = sb["other"] + sb["bass"] if sb else b
        harmony = harmonic_conflict(melodic_a, melodic_b)
        rhythm = rhythm_conflict(sa["drums"] if sa else a, sb["drums"] if sb else b, 60 / ctx["bpm"])
        for method in METHODS:
            if method != "original" and (env is None or residual > .3):
                continue
            for shape in ("gradual", "late_bass"):
                mixed, ga, gb = synthesize(a, b, sa, sb, T, ctx["bar"], method, shape,
                                           harmony > .25, env)
                evaluated += 1
                metrics = rendered_metrics(mixed, a, b, ctx["bar"])
                overlap = 1.0
                chopped = False
                if av is not None and bv is not None:
                    t = np.arange(0, n / SR, .02)
                    m = len(t)
                    active_a = np.interp(t, np.arange(len(av[0])) * av[1], av[0]) > .5
                    active_b = np.interp(t, np.arange(len(bv[0])) * bv[1], bv[0]) > .5
                    wa = np.interp(t, np.arange(n) / SR, ga[1])
                    wb = np.interp(t, np.arange(n) / SR, gb[1])
                    if method != "original":
                        wa, wb = parse_vocal_envelope(env[0], t), parse_vocal_envelope(env[1], t)
                    # Fraction of voiced frames in which both singers are prominent.
                    both = active_a & active_b
                    overlap = float(np.mean(both * np.minimum(wa, wb))) if m else 0
                    if method == "original":
                        chopped = (bool(np.mean(av[0][-max(1, int(.25 / av[1])):]) > .5)
                                   or bool(np.mean(bv[0][:max(1, int(.25 / bv[1]))]) > .5))
                if method == "original" and (overlap > .12 or chopped):
                    continue
                metrics.update(vocal_overlap=overlap, harmonic_conflict=harmony * (.3 if harmony > .25 else 1),
                               rhythm_conflict=rhythm, separation_residual=residual if method != "original" else 0)
                if (metrics["level_dip_db"] > 4 or metrics["level_surge_db"] > 4
                        or metrics["rhythm_conflict"] > .65):
                    continue
                score = candidate_score(metrics) + plan["plan_score"] * .2 + (0.04 if method != "original" else 0)
                out = ctx["work"] / f"candidate{len(rendered)}.wav"
                sf.write(out, mixed, SR, subtype="FLOAT")
                rendered.append({**plan, "method": method, "shape": shape, "gain_db": gain_db,
                                 "score": score, "metrics": metrics, "pcm": out, "ratio": k})
    rendered.sort(key=lambda c: c["score"])
    # Offer a competitive alternative method before filling with placements.
    # Do not promote a much worse candidate merely to show three method names.
    best = rendered[:1]
    for c in rendered[1:]:
        if c["score"] <= best[0]["score"] + 1.5 and all(c["method"] != x["method"] for x in best):
            best.append(c)
        if len(best) == 3:
            break
    for c in rendered:
        if len(best) == 3:
            break
        if all((c["method"], c["cut"], c["b_start"], c["duration"])
               != (x["method"], x["cut"], x["b_start"], x["duration"]) for x in best):
            best.append(c)
    job["candidates_evaluated"] = evaluated
    job["candidates_accepted"] = len(rendered)
    if failures:
        job.setdefault("analysis_notes", []).extend(sorted(set(failures)))
    return best, B


def assemble(A, B, c, pcm, out, preview=False):
    import dj
    cut, start, T = c["cut"], c["b_start"], c["duration"]
    # Exact adjacent samples, with short same-source overlap at both seams.
    x = min(.04, T / 4)
    fc = (f"[0:a]atrim=0:{cut + x:.6f},asetpts=PTS-STARTPTS[a];"
          f"[1:a]atrim={start + T - x:.6f},asetpts=PTS-STARTPTS,volume={c['gain_db']:.6f}dB[b];"
          f"[2:a]asetpts=PTS-STARTPTS[t];[a][t]acrossfade=d={x}:c1=tri:c2=tri[at];"
          f"[at][b]acrossfade=d={x}:c1=tri:c2=tri,{OUTPUT_LIMITER}[out]")
    trim = ["-ss", f"{max(0, cut - 12):.6f}", "-t", f"{min(cut, 12) + T + 15:.6f}"] if preview else []
    dj._run(["ffmpeg", "-y", "-i", str(A), "-i", str(B), "-i", str(pcm),
             "-filter_complex", fc, "-map", "[out]", *trim, "-c:a", "libmp3lame", "-b:a", "320k", str(out)])


def save_candidates(job, A, original_b, B, candidates, opts):
    import dj
    import shutil
    root = dj.DJ_DIR / ".candidates"
    root.mkdir(exist_ok=True)
    previews = []
    for i, candidate in enumerate(candidates):
        token = uuid.uuid4().hex
        c = {k: v for k, v in candidate.items() if k not in ("pcm", "b_audio")}
        pcm = root / f"{token}.wav"
        shutil.copyfile(candidate["pcm"], pcm)
        manifest = {"version": VERSION, "a": fingerprint(A), "b": fingerprint(original_b), "plan": c}
        (root / f"{token}.json").write_text(json.dumps(manifest))
        preview = dj.DJ_DIR / f"preview_{token}_automix.mp3"
        assemble(A, candidate.get("b_audio", B), c, pcm, preview, preview=True)
        previews.append({"candidate": token, "style": "automix", "style_used": "cut" if c["method"] == "native" else "automix",
                         "name": METHODS[c["method"]], "recommended": i == 0,
                         "file": f"/djmixes/{preview.name}", "transition_at": min(12, c["cut"]),
                         "source_transition_at": c["cut"], "b_skip": c.get("b_original_start", c["b_start"]),
                         "transition_duration": c["duration"], "beats_used": c["beats"],
                         "method": c["method"], "metrics": c["metrics"]})
        previews[-1].update(mode=c.get("mode", "creative"), strategy=c.get("strategy"),
                            source_b_start=c.get("b_original_start"),
                            local_drift_ms=c.get("local_drift_ms"))
    return previews


def load_candidate(token, A, B):
    import dj
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("invalid saved transition")
    root = dj.DJ_DIR / ".candidates"
    try:
        manifest = json.loads((root / f"{token}.json").read_text())
    except (OSError, ValueError):
        raise ValueError("saved transition is unavailable; make new previews")
    if manifest.get("version") != VERSION:
        raise ValueError("mixer updated; make new previews")
    if manifest.get("a") != fingerprint(A) or manifest.get("b") != fingerprint(B):
        raise ValueError("tracks changed; make new previews for this pair")
    pcm = root / f"{token}.wav"
    if not pcm.is_file():
        raise ValueError("saved transition audio is unavailable; make new previews")
    return manifest["plan"], pcm


def run(job, A, original_b, opts, preview=False):
    import dj
    import structure
    mode = opts.get("mode", "natural")
    if opts.get("candidate"):
        plan, _ = load_candidate(opts["candidate"], A, original_b)
        mode = plan.get("mode", "creative")
    if mode not in ("natural", "club", "creative"):
        raise ValueError("unknown mixing mode")
    if mode != "creative":
        import naturalmix
        return naturalmix.run(job, A, original_b, opts, preview)
    ctx = None
    try:
        selected = opts.get("candidate")
        saved = load_candidate(selected, A, original_b) if selected else None
        ctx = dj._prep(job, A, original_b, 16)
        if abs(ctx["ratio"] - 1) > dj.MAX_TEMPO_CHANGE:
            if saved:
                raise ValueError("tempo analysis changed; make new previews")
            job["fallback"] = "clean switch — tempo difference is too large for a natural blend"
            prepared, ctx = ctx, None
            return dj._mix_pair_legacy(job, A, original_b, {**opts, "style": "cut"}, preview, prepared)
        if saved:
            candidate, pcm = saved
            matched = ctx["make_b"](True)
            B = matched["file"]
            if abs(matched["k"] - candidate["ratio"]) > .0001:
                raise ValueError("tempo analysis changed; make new previews")
            previews = None
        else:
            job["stage"] = "Mapping musical phrases (first analysis can take a few minutes)..."
            structures = [structure.for_mix(p, compute_missing=True) for p in (A, original_b)]
            job["structure_analysis"] = [s["source"] for s in structures]
            notes = sorted({s["note"] for s in structures if s.get("note")})
            if notes:
                job["analysis_notes"] = notes
            ctx["a_sections"] = structures[0]["sections"]
            ctx["b_sections"] = structures[1]["sections"]
            candidates, B = compare(job, A, original_b, opts, ctx)
            if not candidates:
                job["fallback"] = "clean switch — no tested overlap preserved vocals and rhythm"
                prepared, ctx = ctx, None
                return dj._mix_pair_legacy(job, A, original_b, {**opts, "style": "cut"}, preview, prepared)
            job["stage"] = "Saving the best transition comparisons..."
            previews = save_candidates(job, A, original_b, B, candidates, opts)
            candidate, pcm = candidates[0], candidates[0]["pcm"]
        c = candidate
        job.update(style_used="automix", style_chosen="automix", method=c["method"],
                   auto_reason=METHODS[c["method"]], stretch=round(c["ratio"], 4), beats_used=c["beats"],
                   key_action="original pitch preserved; local harmony checked",
                   source_transition_at=c["cut"], transition_at=c["cut"], b_skip=c["b_start"],
                   transition_duration=c["duration"], cut_reason=c["cut_reason"],
                   local_gain_db=c["gain_db"], metrics=c["metrics"], candidate=selected or previews[0]["candidate"])
        if preview:
            if previews is None:
                name = f"preview_{selected}_automix.mp3"
                assemble(A, B, c, pcm, dj.DJ_DIR / name, preview=True)
                previews = [{"candidate": selected, "style": "automix", "style_used": "automix",
                             "name": METHODS[c["method"]], "file": f"/djmixes/{name}",
                             "transition_at": min(12, c["cut"]), "transition_duration": c["duration"]}]
            import mix_preferences
            approved = mix_preferences.get_reference(A, original_b)
            if approved and all(p['candidate'] != approved['candidate'] for p in previews):
                previews.append(approved)
            job.update(previews=previews, file=previews[0]["file"], transition_at=previews[0]["transition_at"])
        else:
            name = f"{A.stem[:65]}_to_{original_b.stem[:65]}_automix_{uuid.uuid4().hex[:8]}.mp3"
            assemble(A, B, c, pcm, dj.DJ_DIR / name)
            job["file"] = f"/djmixes/{name}"
        job.update(stage="Done! Compared rendered transitions; audition the alternatives to choose your favorite.", pct=100)
    finally:
        if ctx:
            dj._cleanup(ctx["work"])
