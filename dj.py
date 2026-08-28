"""DJ transitions: join track A into track B with a proper DJ-style transition.

Core ideas:
  - comb-scored BPM + bar grid; transitions land on bar lines, loops snap to
    real onsets (transients), not just the estimated grid
  - A hands over before its outro fade at a vocal-phrase boundary; B skips its
    quiet intro; B is loudness-matched to A
  - B is tempo-stretched ONLY for styles where the tracks overlap; hard styles
    (tapestop/backspin/looproll/riser/cut/echo) keep B at its native tempo
  - preview mode renders short clips of the SAME transition in many styles at
    once, so you can audition with your ears before the full render
"""
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import numpy as np
from flask import Blueprint, request, jsonify, send_from_directory

from separator import get_separator, sep_lock

APP_DIR = Path(__file__).parent
DJ_DIR = APP_DIR / "djmixes"
DL_DIR = APP_DIR / "downloads"
DJ_DIR.mkdir(exist_ok=True)

bp = Blueprint("dj", __name__)
jobs = {}

STYLES = ("automix", "acapella", "tapestop", "looproll", "backspin", "riser",
          "neural", "bassswap", "crossfade", "filter", "echo", "cut")
STEM_STYLES = ("automix", "neural", "bassswap", "acapella")
HARD_STYLES = ("cut", "echo", "tapestop", "looproll", "backspin", "riser")

AFMT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def _resolve(url_path: str):
    if url_path.startswith("/downloads/"):
        p = (DL_DIR / Path(url_path).name).resolve()
        if p.parent == DL_DIR.resolve() and p.is_file():
            return p
    return None


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-400:])


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())


def _extract(src: Path, start: float, dur: float, out: Path):
    """Accurate-seek extraction to 44.1k stereo wav."""
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}",
          "-ac", "2", "-ar", "44100", str(out)])
    return out


def _fold_ratio(ratio: float) -> float:
    while ratio > 1.5:
        ratio /= 2
    while ratio < 0.667:
        ratio *= 2
    return ratio


def _active_median(r: np.ndarray) -> float:
    active = r[r > r.max() * 0.06] if r.max() > 0 else r
    return float(np.median(active)) if len(active) else 0.0


def _mean_rms(r: np.ndarray, win: float, t0: float, t1: float) -> float:
    i0, i1 = int(t0 / win), max(int(t0 / win) + 1, int(t1 / win))
    seg = r[i0:i1]
    return float(seg.mean()) if len(seg) else 0.0


def _separate_window(src: Path, start: float, dur: float, work: Path, tag: str) -> dict:
    from demucs.api import save_audio
    import separator as sep_mod
    seg = work / f"{tag}_seg.wav"
    _extract(src, start, dur, seg)
    with sep_lock:
        sep_mod.progress["pct"] = 0.0
        sep = get_separator()
        _, stems = sep.separate_audio_file(seg)
    out = {}
    for k, v in stems.items():
        p = work / f"{tag}_{k}.wav"
        save_audio(v, str(p), samplerate=sep.samplerate)
        out[k] = p
    return out


def _slice(src: Path, start: float, dur: float, out: Path):
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", str(out)])


def _stem_envelopes(style: str, T: float, bar: float):
    def fade(kind, st, d):
        st = max(0.0, min(st, T - 0.1))
        d = max(0.1, min(d, T - st))
        return f"afade=t={kind}:st={st:.3f}:d={d:.3f}"

    if style == "automix":
        swap = T / 2
        a = {
            "vocals": fade("out", 0, bar),
            "other": fade("out", bar, 2 * bar),
            "bass": fade("out", swap - 0.15, 0.3),
            "drums": fade("out", swap, bar),
        }
        b = {
            "other": fade("in", bar, 2 * bar),
            "drums": f"volume=0:enable='lt(t,{swap:.3f})'",
            "bass": fade("in", swap - 0.05, 0.25),
            "vocals": fade("in", swap + bar, 2 * bar),
        }
    elif style == "acapella":
        swap = 0.4 * T
        quick = min(bar, 0.15 * T)
        a = {
            "vocals": fade("out", 0.72 * T, 0.2 * T),
            "drums": fade("out", 0, quick),
            "bass": fade("out", 0, quick),
            "other": fade("out", 0, quick),
        }
        b = {
            "drums": f"volume=0:enable='lt(t,{swap:.3f})'",
            "bass": fade("in", swap - 0.05, 0.25),
            "other": fade("in", swap, bar),
            "vocals": fade("in", 0.78 * T, 0.18 * T),
        }
    elif style == "neural":
        a = {
            "vocals": fade("out", 0, 0.3 * T),
            "other": fade("out", 0.25 * T, 0.5 * T),
            "bass": fade("out", 0.5 * T - 0.15, 0.3),
            "drums": fade("out", 0.5 * T, 0.5 * T),
        }
        b = {
            "drums": fade("in", 0, 0.4 * T),
            "bass": fade("in", 0.5 * T - 0.05, 0.25),
            "other": fade("in", 0.15 * T, 0.45 * T),
            "vocals": fade("in", 0.3 * T, 0.5 * T),
        }
    else:  # bassswap
        a = {
            "vocals": fade("out", 0, T),
            "other": fade("out", 0, T),
            "drums": fade("out", 0, T),
            "bass": fade("out", 0.5 * T - 0.15, 0.3),
        }
        b = {
            "vocals": fade("in", 0, T),
            "other": fade("in", 0, T),
            "drums": fade("in", 0, T),
            "bass": fade("in", 0.5 * T - 0.05, 0.25),
        }
    return a, b


def _render_style(style, A, cut, B, b_start, delay_s, T, bar, bpm, med_a,
                  sa, sb, work, out_path, tmax=None):
    """Render one transition. A/B are audio files; cut/b_start/delay_s are in
    those files' local clocks. sa/sb: stem dicts already sliced to T (or None)."""
    import analysis
    import djfx
    X = 0.05
    delay_ms = max(0, int(delay_s * 1000))
    tail = ["-t", f"{tmax:.3f}"] if tmax else []
    enc = ["-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]

    if style in STEM_STYLES:
        env_a, env_b = _stem_envelopes(style, T, bar)
        order = ["vocals", "drums", "bass", "other"]
        cmd = ["ffmpeg", "-y", "-i", str(A)]
        for k in order:
            cmd += ["-i", str(sa[k])]
        for k in order:
            cmd += ["-i", str(sb[k])]
        cmd += ["-i", str(B)]
        fa = [f"[{i + 1}:a]{env_a[k]}[sa{i}]" for i, k in enumerate(order)]
        fb = [f"[{i + 5}:a]{env_b[k]}[sb{i}]" for i, k in enumerate(order)]
        fc = ";".join(fa + fb) + (
            f";[sa0][sa1][sa2][sa3]amix=inputs=4:normalize=0[atrans]"
            f";[sb0][sb1][sb2][sb3]amix=inputs=4:normalize=0[btrans]"
            f";[0:a]atrim=0:{cut + X:.3f}[ahead]"
            f";[ahead][atrans]acrossfade=d={X}[aall]"
            f";[9:a]atrim={b_start + T - X:.3f},asetpts=PTS-STARTPTS[brest]"
            f";[btrans][brest]acrossfade=d={X}[ball]"
            f";[ball]adelay={delay_ms}|{delay_ms}[bd]"
            f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]"
        )
        _run(cmd + ["-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "crossfade":
        fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
              f";[0:a]atrim=0:{cut + T:.3f}[a]"
              f";[a][b1]acrossfade=d={T:.3f}:c1=qsin:c2=qsin[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "filter":
        steps = 10
        cmds = []
        for i in range(steps):
            t2 = cut + T * i / steps
            f = 30 * (3000 / 30) ** (i / (steps - 1))
            cmds.append(f"{t2:.3f} highpass@sw f {f:.0f}")
        send = ";".join(cmds)
        fc = (f"[0:a]atrim=0:{cut + T:.3f},asendcmd=c='{send}',highpass@sw=f=20,"
              f"afade=t=out:st={cut + 0.6 * T:.3f}:d={0.4 * T:.3f}[a]"
              f";[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={0.6 * T:.3f},"
              f"adelay={delay_ms}|{delay_ms}[b]"
              f";[a][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "echo":
        beat_ms = int(60000 / bpm)
        tail_d = max(2.0, T)
        pre = 2 * 60 / bpm
        fc = (f"[0:a]atrim=0:{cut - pre:.3f}[ahead]"
              f";[0:a]atrim={cut - pre:.3f}:{cut + 0.05:.3f},asetpts=PTS-STARTPTS,"
              f"apad=pad_dur={tail_d:.3f},aecho=0.8:0.7:{beat_ms}|{beat_ms * 2}:0.5|0.3[atail]"
              f";[ahead][atail]concat=n=2:v=0:a=1[aall]"
              f";[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
              f"adelay={delay_ms}|{delay_ms}[b]"
              f";[aall][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style in ("tapestop", "backspin", "looproll"):
        if style == "tapestop":
            fx_dur = min(1.2, max(0.5, bar / 2))
            seg = work / "fx_src.wav"
            _extract(A, cut - fx_dur, fx_dur, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.tape_stop(y, sr, fx_dur), sr)
        elif style == "backspin":
            fx_dur = min(1.0, max(0.45, bar / 2))
            src_len = min(max(0.5, cut - fx_dur), 3.5 * fx_dur)
            seg = work / "fx_src.wav"
            _extract(A, cut - fx_dur - src_len, src_len, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.backspin(y, sr, fx_dur), sr)
        else:  # looproll
            fx_dur = 2 * bar
            beat = bar / 4
            # snap the loop source to a real transient so every repeat hits
            target = cut - fx_dur
            snapped = analysis.snap_to_beat(A, target, window=0.35 * beat)
            seg = work / "fx_src.wav"
            _extract(A, snapped, 2 * beat, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.loop_roll(y, sr, beat), sr)
        head_end = cut - fx_dur
        fc = (f"[0:a]atrim=0:{head_end:.3f},{AFMT}[h]"
              f";[1:a]{AFMT}[fx]"
              f";[h][fx]concat=n=2:v=0:a=1[aall]"
              f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
              f"adelay={delay_ms}|{delay_ms}[bd]"
              f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(chunk), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "riser":
        rise_dur = 4 * bar
        rise = work / "riser.wav"
        djfx.save(rise, djfx.riser(44100, rise_dur, gain=1.2 * float(med_a + 1e-3)), 44100)
        rise_at = max(0, int((cut - rise_dur) * 1000))
        fc = (f"[0:a]atrim=0:{cut:.3f},afade=t=out:st={cut - 0.05:.3f}:d=0.05[a]"
              f";[1:a]{AFMT},adelay={rise_at}|{rise_at}[r]"
              f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
              f"adelay={delay_ms}|{delay_ms}[bd]"
              f";[a][r][bd]amix=inputs=3:duration=longest:normalize=0,alimiter=limit=0.97[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(rise), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    else:  # cut
        fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
              f";[0:a]atrim=0:{cut:.3f}[a]"
              f";[a][b1]acrossfade=d=0.03[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)


def _prep(job, a_path, b_path, beats, need_stretch):
    """Shared analysis: grids, loudness, matched/plain B, entry points."""
    import analysis
    ctx = {}
    job["stage"] = "Reading the beat grids..."
    grid_a = analysis.beat_grid(a_path)
    grid_b = analysis.beat_grid(b_path)
    job["a_facts"] = analysis.analyze(a_path)
    job["b_facts"] = analysis.analyze(b_path)
    ctx["bpm"] = grid_a["bpm"] or 120
    ctx["T"] = max(2.0, beats * 60.0 / ctx["bpm"])
    ctx["bar"] = grid_a["bar_len"]
    ctx["grid_a"] = grid_a

    work = Path(tempfile.mkdtemp(prefix="dj_"))
    ctx["work"] = work

    r_a, win_a = analysis.rms_profile(a_path)
    r_b0, _ = analysis.rms_profile(b_path)
    ctx["r_a"], ctx["win_a"] = r_a, win_a
    ctx["med_a"] = _active_median(r_a)
    med_b = _active_median(r_b0)
    gain_b = float(np.clip(ctx["med_a"] / med_b, 0.5, 2.0)) if med_b > 0 else 1.0

    job["stage"] = "Preparing track B..."
    ratio = 1.0
    if need_stretch and grid_a["bpm"] and grid_b["bpm"]:
        ratio = _fold_ratio(grid_a["bpm"] / grid_b["bpm"])
    ctx["ratio"] = ratio
    job["stretch"] = round(ratio, 4)

    def make_b(stretched, override_orig=None):
        out = work / ("b_matched.wav" if stretched else "b_plain.wav")
        stretching = stretched and abs(ratio - 1) > 0.005
        af = (f"atempo={ratio:.4f}," if stretching else "") + f"volume={gain_b:.3f}"
        _run(["ffmpeg", "-y", "-i", str(b_path), "-af", af, "-ac", "2", "-ar", "44100", str(out)])
        grid = analysis.beat_grid(out)

        def snap(t):
            k = round((t - grid["bar"]) / grid["bar_len"])
            return max(0.0, grid["bar"] + k * grid["bar_len"])

        if override_orig is not None:
            # user-picked entry point, given on B's ORIGINAL timeline
            t_local = float(override_orig) / ratio if stretching else float(override_orig)
            return {"file": out, "b_start": snap(t_local)}

        # structure-aware entry: the first section that plays at full energy
        b_start = None
        try:
            secs = analysis.detect_sections(out)
        except Exception:
            secs = []
        for s in secs:
            if s["start"] > 90:
                break
            if s["energy"] >= 0.7:
                b_start = snap(s["start"])
                break
        if b_start is None:  # fallback: first loud bar
            r_b, win_b = analysis.rms_profile(out)
            med = _active_median(r_b)
            b_start = 0.0
            t = grid["bar"]
            dur = _duration(out)
            while t < min(60, dur - 8):
                if _mean_rms(r_b, win_b, t, t + 2 * grid["bar_len"]) >= 0.6 * med:
                    b_start = t
                    break
                t += grid["bar_len"]
        return {"file": out, "b_start": b_start}
    ctx["make_b"] = make_b
    return ctx


def _manual_cut(ctx, a_path, style, T, wanted):
    """Snap a user-picked exit point to the bar grid, keeping it renderable."""
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    grid_a, bar = ctx["grid_a"], ctx["bar"]
    wanted = min(max(wanted, 10.0), target)
    k = round((wanted - grid_a["bar"]) / bar)
    ctx["cut_reason"] = "manual"
    return max(10.0, min(grid_a["bar"] + k * bar, target))


def _pick_cut(ctx, a_path, style, T):
    import analysis
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    if target < 10:
        raise ValueError("track A is too short for this transition length")
    grid_a, bar = ctx["grid_a"], ctx["bar"]
    look = min(T, 8.0)

    def snap(t):
        k = round((t - grid_a["bar"]) / bar)
        return grid_a["bar"] + k * bar

    def alive(t):
        return _mean_rms(ctx["r_a"], ctx["win_a"], t, t + look) >= 0.45 * ctx["med_a"]

    # structure-aware: hand over at the END of the last high-energy section
    # (i.e. right after the final chorus/drop, not inside the outro)
    try:
        secs = analysis.detect_sections(a_path)
    except Exception:
        secs = []
    ctx["a_sections"] = secs
    for s in reversed(secs):
        if s["energy"] < 0.55 or s["end"] < 20:
            continue
        cut = snap(min(s["end"], target))
        while cut > s["start"] + bar and not alive(cut):
            cut -= bar
        if cut > 20 and alive(cut):
            ctx["cut_reason"] = "end of last high-energy section"
            return cut

    # fallback: latest loud bar before the target
    k = int((target - grid_a["bar"]) // bar)
    cut = grid_a["bar"] + k * bar
    while cut > 20 and not alive(cut):
        cut -= bar
    ctx["cut_reason"] = "loudness fallback"
    return cut


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        import analysis
        if opts.get("preview"):
            return _preview_job(job, a_path, b_path, opts)

        style = opts.get("style", "automix")
        if style not in STYLES:
            raise ValueError(f"unknown style: {style}")
        beats = int(opts.get("beats") or 32)
        ctx = _prep(job, a_path, b_path, beats, need_stretch=style not in HARD_STYLES)
        T, bar, bpm, work = ctx["T"], ctx["bar"], ctx["bpm"], ctx["work"]

        if style == "automix" and not (0.9 <= ctx["ratio"] <= 1.12):
            job["fallback"] = f"crossfade (tempos too far apart, x{ctx['ratio']:.2f})"
            style = "crossfade"
            T = min(T, 4 * bar)

        bvar = ctx["make_b"](stretched=style not in HARD_STYLES,
                             override_orig=opts.get("b_start"))
        b_matched, b_start = bvar["file"], bvar["b_start"]
        job["b_skip"] = round(b_start, 2)

        if opts.get("cut") is not None:
            cut = _manual_cut(ctx, a_path, style, T, float(opts["cut"]))
        else:
            cut = _pick_cut(ctx, a_path, style, T)

        sa_cut = sb = None
        delay_s = cut
        if style in STEM_STYLES:
            dur_a = _duration(a_path)
            win_start = max(0.0, min(cut - 6 * bar, dur_a - T - 2) - 2 * bar)
            job["stage"] = "Extracting stems from track A (Demucs)..."
            sa = _separate_window(a_path, win_start, dur_a - win_start, work, "a")

            r_v, win_v = analysis.rms_profile(sa["vocals"])
            med_v = _active_median(r_v)
            if med_v > 1e-4 and style != "acapella" and opts.get("cut") is None:
                cand = cut
                for _ in range(8):
                    local = cand - win_start
                    if local < 0:
                        break
                    voc = _mean_rms(r_v, win_v, max(0, local - 0.3), local + 0.75 * bar)
                    ok = _mean_rms(ctx["r_a"], ctx["win_a"], cand, cand + min(T, 8.0)) >= 0.45 * ctx["med_a"]
                    if voc < 0.35 * med_v and ok:
                        cut = cand
                        break
                    cand -= bar

            job["stage"] = "Extracting stems from track B (Demucs)..."
            sb = _separate_window(b_matched, b_start, T, work, "b")
            delta, conf = analysis.align_beats(a_path, cut, b_matched, b_start,
                                               min(T, 4 * bar), bpm)
            job["nudge_ms"] = int(delta * 1000)
            job["beat_confidence"] = round(conf, 2)
            delay_s = cut + delta
            if style == "automix" and conf < 0.25:
                job["fallback"] = "crossfade (low beat-match confidence)"
                style = "crossfade"
                T = min(T, 4 * bar)
            else:
                sa_cut = {}
                for k2, p in sa.items():
                    out = work / f"a_{k2}_cut.wav"
                    _slice(p, cut - win_start, T, out)
                    sa_cut[k2] = out
        job["transition_at"] = round(cut, 2)
        job["cut_reason"] = ctx.get("cut_reason")

        out_name = f"{a_path.stem}_to_{b_path.stem}_{style}.mp3"
        out_path = DJ_DIR / out_name
        job["stage"] = "Rendering the transition..."
        _render_style(style, a_path, cut, b_matched, b_start, delay_s, T, bar, bpm,
                      ctx["med_a"], sa_cut, sb, work, out_path)
        job["file"] = f"/djmixes/{out_name}"
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


PREVIEW_STYLES = ("automix", "acapella", "tapestop", "looproll", "backspin",
                  "riser", "echo", "cut")


def _preview_job(job, a_path, b_path, opts):
    """Render ~30s clips of the same transition in many styles at once."""
    import analysis
    beats = int(opts.get("beats") or 32)
    styles = [s for s in (opts.get("styles") or PREVIEW_STYLES) if s in STYLES]
    ctx = _prep(job, a_path, b_path, beats, need_stretch=True)
    T, bar, bpm, work = ctx["T"], ctx["bar"], ctx["bpm"], ctx["work"]

    b_soft = ctx["make_b"](stretched=True, override_orig=opts.get("b_start"))
    b_hard = (ctx["make_b"](stretched=False, override_orig=opts.get("b_start"))
              if any(s in HARD_STYLES for s in styles) else b_soft)

    if opts.get("cut") is not None:
        cut = _manual_cut(ctx, a_path, "automix", T, float(opts["cut"]))
    else:
        cut = _pick_cut(ctx, a_path, "automix", T)
    job["transition_at"] = round(cut, 2)

    PRE = 12.0
    a_clip = _extract(a_path, cut - PRE, PRE + T + 2, work / "a_clip.wav")
    clip_soft = _extract(b_soft["file"], b_soft["b_start"], T + 20, work / "b_clip_soft.wav")
    clip_hard = (_extract(b_hard["file"], b_hard["b_start"], T + 20, work / "b_clip_hard.wav")
                 if b_hard is not b_soft else clip_soft)

    sa_cut = sb = None
    delta = 0.0
    if any(s in STEM_STYLES for s in styles):
        job["stage"] = "Extracting stems from track A (Demucs)..."
        sa_cut = _separate_window(a_clip, PRE, T, work, "a")
        job["stage"] = "Extracting stems from track B (Demucs)..."
        sb = _separate_window(clip_soft, 0, T, work, "b")
        delta, conf = analysis.align_beats(a_clip, PRE, clip_soft, 0, min(T, 4 * bar), bpm)
        job["beat_confidence"] = round(conf, 2)

    uid = uuid.uuid4().hex[:6]
    previews = []
    for i, style in enumerate(styles):
        job["stage"] = f"Rendering previews... ({i + 1}/{len(styles)})"
        try:
            B = clip_hard if style in HARD_STYLES else clip_soft
            delay_s = PRE if style in HARD_STYLES else PRE + delta
            out = DJ_DIR / f"preview_{uid}_{style}.mp3"
            _render_style(style, a_clip, PRE, B, 0.0, delay_s, T, bar, bpm,
                          ctx["med_a"], sa_cut, sb, work, out, tmax=PRE + T + 15)
            previews.append({"style": style, "file": f"/djmixes/{out.name}"})
        except Exception as e:
            previews.append({"style": style, "error": str(e)})
    job["previews"] = previews
    job["stage"] = "Done! Transition hits at 0:12 in every clip."


@bp.post("/dj/inspect")
def dj_inspect():
    """Analysis for the transition editor: grids, sections and proposed points
    for both tracks, all on the ORIGINAL files' timelines."""
    import analysis
    data = request.get_json(silent=True) or {}
    a = _resolve(data.get("a_file", ""))
    b = _resolve(data.get("b_file", ""))
    if a is None or b is None:
        return jsonify(error="pick both tracks first"), 400
    beats = int(data.get("beats") or 32)
    try:
        grid_a = analysis.beat_grid(a)
        grid_b = analysis.beat_grid(b)
        bpm = grid_a["bpm"] or 120
        T = max(2.0, beats * 60.0 / bpm)
        r_a, win_a = analysis.rms_profile(a)
        ctx = {"grid_a": grid_a, "bar": grid_a["bar_len"],
               "r_a": r_a, "win_a": win_a, "med_a": _active_median(r_a)}
        cut = _pick_cut(ctx, a, "automix", T)

        secs_b = analysis.detect_sections(b)
        b_start = None
        for s in secs_b:
            if s["start"] > 90:
                break
            if s["energy"] >= 0.7:
                k = round((s["start"] - grid_b["bar"]) / grid_b["bar_len"])
                b_start = max(0.0, grid_b["bar"] + k * grid_b["bar_len"])
                break
        if b_start is None:
            b_start = grid_b["bar"]

        return jsonify(
            T=round(T, 2),
            a={"duration": round(_duration(a), 2), "bpm": grid_a["bpm"],
               "bar_phase": round(grid_a["bar"], 3), "bar_len": round(grid_a["bar_len"], 4),
               "sections": ctx.get("a_sections", analysis.detect_sections(a)),
               "cut": round(cut, 2), "cut_reason": ctx.get("cut_reason")},
            b={"duration": round(_duration(b), 2), "bpm": grid_b["bpm"],
               "bar_phase": round(grid_b["bar"], 3), "bar_len": round(grid_b["bar_len"], 4),
               "sections": secs_b, "b_start": round(b_start, 2)},
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


@bp.post("/dj/start")
def dj_start():
    data = request.get_json(silent=True) or {}
    a = _resolve(data.get("a_file", ""))
    b = _resolve(data.get("b_file", ""))
    if a is None or b is None:
        return jsonify(error="pick both tracks first"), 400
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"stage": "Starting...", "done": False, "error": None, "file": None}
    threading.Thread(target=process_job, args=(job_id, a, b, data), daemon=True).start()
    return jsonify(job=job_id)


@bp.get("/dj/status/<job_id>")
def dj_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="unknown job"), 404
    out = dict(job)
    if "Demucs" in (out.get("stage") or ""):
        import separator as sep_mod
        out["pct"] = sep_mod.progress.get("pct")
    return jsonify(out)


@bp.get("/djmixes/<path:name>")
def dj_file(name):
    return send_from_directory(DJ_DIR, name)
