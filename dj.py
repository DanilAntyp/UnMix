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


# where the rhythm-section swap sits inside the transition window, per style
SWAP_FRAC = {"automix": 0.5, "neural": 0.5, "bassswap": 0.5, "acapella": 0.4}

FEEDBACK_FILE = APP_DIR / "feedback.jsonl"


def _feedback_bias() -> dict:
    """Aggregate 👍/👎 verdicts per style from past listening sessions."""
    bias = {}
    try:
        import json as _json
        for line in FEEDBACK_FILE.read_text().splitlines():
            try:
                d = _json.loads(line)
                bias[d["style"]] = bias.get(d["style"], 0) + int(d["verdict"])
            except Exception:
                continue
    except OSError:
        pass
    return bias


def _auto_style(ctx, job, analysis, a_path, b_path) -> str:
    """Pick a transition style from the pair's character: tempo feasibility,
    key compatibility, aggressiveness — nudged by accumulated 👍/👎 feedback."""
    sa = analysis.style_signals(a_path)
    sb = analysis.style_signals(b_path)

    def hard(s):
        # calibrated on rap vs pop/funk/rock: hard tracks sit at >=2.8 hits/sec
        return s["onset_density"] >= 2.8

    ratio_ok = 0.9 <= ctx["ratio"] <= 1.12
    if not ratio_ok:
        cand, why = (("tapestop", "tempos too far apart + hard-hitting tracks")
                     if hard(sa) or hard(sb)
                     else ("echo", "tempos too far apart — clean hand-off"))
    elif hard(sa) and hard(sb):
        cand, why = "looproll", "both tracks are hard-hitting — stutter into the drop"
    elif (ctx["shift"] == 0 and not ctx["clash"] and abs(ctx["ratio"] - 1) <= 0.02
          and sa["mid_ratio"] >= 0.16 and sb["mid_ratio"] >= 0.16):
        cand, why = "acapella", "keys match and vocals are prominent — acapella bridge"
    elif ctx["clash"]:
        cand, why = "automix", "keys clash — rhythm-only blend"
    else:
        cand, why = "automix", "compatible pair — smooth blend"

    bias = _feedback_bias()
    if bias.get(cand, 0) <= -2:  # you kept disliking this one — try the next best
        for alt in ("automix", "tapestop", "echo", "crossfade"):
            if alt != cand and bias.get(alt, 0) > -2:
                why += f" (switched from {cand}: your 👎 history)"
                cand = alt
                break
    job["auto_reason"] = why
    return cand


def _load_grid(analysis, path: Path, job=None, label="") -> dict:
    """Real downbeats from madmom when possible, comb-grid fallback otherwise."""
    try:
        if job is not None:
            job["stage"] = f"Tracking beats{label} (neural)..."
        return analysis.madmom_grid(path)
    except Exception:
        g = dict(analysis.beat_grid(path))
        g["downbeats"] = None
        return g


def _grid_scaled(grid: dict, ratio: float) -> dict:
    """Grid of the tempo-stretched file: atempo=ratio maps t -> t/ratio."""
    if abs(ratio - 1) < 0.005:
        return grid
    g = {"bpm": round(grid["bpm"] * ratio, 2), "beat_len": grid["beat_len"] / ratio,
         "bar_len": grid["bar_len"] / ratio, "bar": grid["bar"] / ratio}
    g["downbeats"] = ([round(t / ratio, 3) for t in grid["downbeats"]]
                      if grid.get("downbeats") else None)
    return g


def _snap_grid(t, grid):
    """Nearest downbeat (real, per-beat) or uniform-grid fallback."""
    db = grid.get("downbeats")
    if db:
        arr = np.asarray(db)
        return float(arr[np.argmin(np.abs(arr - t))])
    k = round((t - grid["bar"]) / grid["bar_len"])
    return max(0.0, grid["bar"] + k * grid["bar_len"])


def _phrase_candidates(t, sections, grid, phrase_bars=4):
    """Phrase boundaries near t: every `phrase_bars`-th downbeat, anchored at
    the downbeat nearest to the start of the section containing t."""
    db = grid.get("downbeats")
    if db:
        arr = np.asarray(db)
        anchor_i = 0
        for s in sections or []:
            if s["start"] <= t + 1e-6:
                anchor_i = int(np.argmin(np.abs(arr - s["start"])))
            else:
                break
        return arr[anchor_i % phrase_bars::phrase_bars]
    # uniform fallback
    anchor = grid["bar"]
    for s in sections or []:
        if s["start"] <= t + 1e-6:
            k = round((s["start"] - grid["bar"]) / grid["bar_len"])
            anchor = grid["bar"] + k * grid["bar_len"]
        else:
            break
    phrase = phrase_bars * grid["bar_len"]
    lo = anchor - phrase * 40
    return np.array([lo + i * phrase for i in range(90)])


def _phrase_snap(t, sections, grid, phrase_bars=4):
    cands = _phrase_candidates(t, sections, grid, phrase_bars)
    return float(cands[np.argmin(np.abs(cands - t))])


def _phrase_prev(t, sections, grid, phrase_bars=4):
    cands = _phrase_candidates(t, sections, grid, phrase_bars)
    below = cands[cands < t - 1e-3]
    return float(below[-1]) if len(below) else t - phrase_bars * grid["bar_len"]


def _find_drop(sections, limit=120.0):
    """B's 'drop': the first strong section arriving after a quieter one, or
    the first full-energy section as a fallback."""
    prev_e = None
    for s in sections or []:
        if s["start"] > limit:
            break
        if prev_e is not None and s["energy"] >= 0.75 and s["energy"] - prev_e >= 0.12:
            return s["start"]
        prev_e = s["energy"]
    for s in sections or []:
        if s["start"] > 90:
            break
        if s["energy"] >= 0.7:
            return s["start"]
    return None


def _stem_envelopes(style: str, T: float, bar: float, clash: bool = False):
    def fade(kind, st, d):
        st = max(0.0, min(st, T - 0.1))
        d = max(0.1, min(d, T - st))
        return f"afade=t={kind}:st={st:.3f}:d={d:.3f}"

    if style == "automix":
        swap = T / 2
        a = {
            "vocals": fade("out", 0, bar),
            "other": fade("out", bar, min(2 * bar, swap - bar) if clash else 2 * bar),
            "bass": fade("out", swap - 0.15, 0.3),
            "drums": fade("out", swap, bar),
        }
        b = {
            # keys clash -> B's melodic layer waits for the swap so the two
            # harmonies never sound together; only rhythm crosses over
            "other": fade("in", swap if clash else bar, bar if clash else 2 * bar),
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
            "other": fade("in", 0.5 * T if clash else 0.15 * T, 0.45 * T),
            "vocals": fade("in", 0.5 * T if clash else 0.3 * T, 0.5 * T),
        }
    else:  # bassswap
        a = {
            "vocals": fade("out", 0, T),
            "other": fade("out", 0, T),
            "drums": fade("out", 0, T),
            "bass": fade("out", 0.5 * T - 0.15, 0.3),
        }
        b = {
            "vocals": fade("in", 0.5 * T if clash else 0, T),
            "other": fade("in", 0.5 * T if clash else 0, T),
            "drums": fade("in", 0, T),
            "bass": fade("in", 0.5 * T - 0.05, 0.25),
        }
    return a, b


def _vocal_safe_start(vocals_path, default_st, T):
    """First vocal-phrase START in B's transition stem at/after default_st, so
    B's vocal never fades in mid-word. None = keep the default envelope."""
    import analysis
    try:
        r, win = analysis.rms_profile(vocals_path)
    except Exception:
        return None
    med = _active_median(r)
    if med < 1e-4:
        return None
    active = r >= 0.35 * med
    gap = max(1, int(0.6 / win))
    for i in range(int(default_st / win), len(r)):
        if active[i] and not active[max(0, i - gap):i].any():
            t = i * win
            return round(t, 3) if t < T - 0.6 else None
    return None


VOCAL_DEFAULT_ST = {"automix": lambda T, bar: T / 2 + bar,
                    "neural": lambda T, bar: 0.3 * T,
                    "acapella": lambda T, bar: 0.78 * T}


def _render_style(style, A, cut, B, b_start, delay_s, T, bar, bpm, med_a,
                  sa, sb, work, out_path, tmax=None, clash=False):
    """Render one transition. A/B are audio files; cut/b_start/delay_s are in
    those files' local clocks. sa/sb: stem dicts already sliced to T (or None)."""
    import analysis
    import djfx
    X = 0.05
    delay_ms = max(0, int(delay_s * 1000))
    tail = ["-t", f"{tmax:.3f}"] if tmax else []
    enc = ["-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]

    if style in STEM_STYLES:
        env_a, env_b = _stem_envelopes(style, T, bar, clash)
        # B-side vocal safety: start B's vocal on its own phrase, not mid-word
        vd = VOCAL_DEFAULT_ST.get(style)
        if vd and sb:
            v = _vocal_safe_start(sb["vocals"], vd(T, bar), T)
            if v is not None:
                env_b["vocals"] = (f"volume=0:enable='lt(t,{v:.3f})',"
                                   f"afade=t=in:st={v:.3f}:d=0.5")
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
    grid_a = _load_grid(analysis, a_path, job, " of track A")
    grid_b = _load_grid(analysis, b_path, job, " of track B")
    ctx["grid_b_orig"] = grid_b
    job["a_facts"] = analysis.analyze(a_path)
    job["b_facts"] = analysis.analyze(b_path)
    ctx["shift"], ctx["clash"] = analysis.harmony_plan(
        job["a_facts"].get("camelot"), job["b_facts"].get("camelot"))
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

    def make_b(stretched, override_orig=None, fast=False):
        import shutil as _sh
        out = work / ("b_matched.wav" if stretched else "b_plain.wav")
        stretching = stretched and abs(ratio - 1) > 0.005
        shift = ctx.get("shift", 0) if stretched else 0  # only blends overlap harmonically
        # rubberband earns its runtime only for audible stretches or pitch
        # shifts; tiny tempo nudges sound identical through atempo
        need_rb = (stretching and abs(ratio - 1) > 0.04) or shift
        rb = None if fast else _sh.which("rubberband")
        if rb and need_rb:
            # rubberband (R2 engine — good transients, reasonable speed);
            # also does clean pitch shifting for key matching
            raw = work / "b_raw.wav"
            _run(["ffmpeg", "-y", "-i", str(b_path), "-ac", "2", "-ar", "44100", str(raw)])
            cmd = [rb, "-t", f"{1 / ratio:.6f}" if stretching else "1.0"]
            if shift:
                cmd += ["-p", str(shift)]
            rbo = work / "b_rb.wav"
            r = subprocess.run(cmd + [str(raw), str(rbo)], capture_output=True, text=True)
            if r.returncode == 0:
                _run(["ffmpeg", "-y", "-i", str(rbo), "-af", f"volume={gain_b:.3f}", str(out)])
                job["stretch_tool"] = "rubberband"
            else:
                rb = None
        if not (rb and need_rb):
            af = (f"atempo={ratio:.4f}," if stretching else "")
            if shift:
                f = 2 ** (shift / 12)
                af += f"asetrate=44100*{f:.6f},aresample=44100,atempo={1 / f:.6f},"
            af += f"volume={gain_b:.3f}"
            _run(["ffmpeg", "-y", "-i", str(b_path), "-af", af, "-ac", "2", "-ar", "44100", str(out)])
        if shift:
            job["key_action"] = f"B pitch-shifted {shift:+d} st for key match"
        elif ctx.get("clash") and stretched:
            job["key_action"] = "keys clash — B melodic layers held until the swap"
        grid = _grid_scaled(ctx["grid_b_orig"], ratio if stretching else 1.0)

        def snap(t):
            return _snap_grid(t, grid)

        if override_orig is not None:
            # user-picked entry point, given on B's ORIGINAL timeline
            t_local = float(override_orig) / ratio if stretching else float(override_orig)
            return {"file": out, "b_start": snap(t_local), "manual": True}

        try:
            # sections of the ORIGINAL B (cached across jobs), scaled in time
            secs0 = analysis.detect_sections(b_path)
            k = ratio if stretching else 1.0
            secs = [{"start": round(s["start"] / k, 2), "end": round(s["end"] / k, 2),
                     "energy": s["energy"]} for s in secs0]
        except Exception:
            secs = []
        drop = _find_drop(secs)
        loud = None
        if drop is None:  # fallback: first loud bar
            r_b, win_b = analysis.rms_profile(out)
            med = _active_median(r_b)
            loud = 0.0
            t = grid["bar"]
            dur = _duration(out)
            while t < min(60, dur - 8):
                if _mean_rms(r_b, win_b, t, t + 2 * grid["bar_len"]) >= 0.6 * med:
                    loud = t
                    break
                t += grid["bar_len"]
        return {"file": out, "snap": snap, "sections": secs,
                "drop": snap(drop) if drop is not None else None, "loud": loud}
    ctx["make_b"] = make_b
    return ctx


def _entry_for(bvar, style, T):
    c = _entry_candidates(bvar, style, T)[0]
    return c[0], c[2]


def _manual_cut(ctx, a_path, style, T, wanted):
    """Snap a user-picked exit point to the nearest real downbeat."""
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    wanted = min(max(wanted, 10.0), target)
    ctx["cut_reason"] = "manual"
    return max(10.0, min(_snap_grid(wanted, ctx["grid_a"]), target))


def _exit_candidates(ctx, a_path, style, T, limit=3):
    """Up to `limit` phrase-snapped exit points of A (latest first), each with
    the energy of the section it leaves — used for energy-continuity pairing."""
    import analysis
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    if target < 10:
        raise ValueError("track A is too short for this transition length")
    grid_a, bar = ctx["grid_a"], ctx["bar"]
    look = min(T, 8.0)

    def alive(t):
        return _mean_rms(ctx["r_a"], ctx["win_a"], t, t + look) >= 0.45 * ctx["med_a"]

    try:
        secs = analysis.detect_sections(a_path)
    except Exception:
        secs = []
    ctx["a_sections"] = secs
    cands = []
    for s in reversed(secs):
        if len(cands) >= limit:
            break
        if s["energy"] < 0.55 or s["end"] < 20:
            continue
        cut = _phrase_snap(min(s["end"], target), secs, grid_a)
        while cut > target:
            cut = _phrase_prev(cut, secs, grid_a)
        while cut > s["start"] + bar and not alive(cut):
            cut = _phrase_prev(cut, secs, grid_a)
        if cut > 20 and alive(cut):
            cands.append((cut, s["energy"], "end of last high-energy phrase"
                          if not cands else "end of an earlier phrase"))
    if not cands:
        cut = _phrase_snap(target, secs, grid_a)
        while cut > target:
            cut = _phrase_prev(cut, secs, grid_a)
        while cut > 20 and not alive(cut):
            cut = _phrase_prev(cut, secs, grid_a)
        cands = [(cut, None, "loudness fallback")]
    return cands


def _pick_cut(ctx, a_path, style, T):
    cut, _, reason = _exit_candidates(ctx, a_path, style, T)[0]
    ctx["cut_reason"] = reason
    return cut


def _entry_candidates(bvar, style, T, limit=3):
    """Up to `limit` entry points of B (earliest first) with section energies."""
    if bvar.get("manual"):
        return [(bvar["b_start"], None, "manual")]
    frac = SWAP_FRAC.get(style)

    def entry(t):
        off = frac * T if frac else 0.0
        return bvar["snap"](max(0.0, t - off))

    cands = []
    prev_e = None
    for s in bvar.get("sections") or []:
        if s["start"] > 120 or len(cands) >= limit:
            break
        jump = prev_e is not None and s["energy"] - prev_e >= 0.10
        if s["energy"] >= 0.65 and (jump or s["energy"] >= 0.75):
            cands.append((entry(s["start"]), s["energy"],
                          "drop-to-drop" if frac else "on the drop"))
        prev_e = s["energy"]
    if not cands:
        base = bvar["drop"] if bvar.get("drop") is not None else (bvar.get("loud") or 0.0)
        cands = [(entry(base), None, "first loud section")]
    return cands


def _choose_pair(a_cands, b_cands):
    """Energy continuity: pick the exit/entry pair whose section energies match,
    with a mild preference for the latest exit and earliest entry."""
    best = None
    for ia, (ca, ea, ra) in enumerate(a_cands):
        for ib, (cb, eb, rb) in enumerate(b_cands):
            if ea is None or eb is None:
                score = 0.5 + 0.04 * ia + 0.04 * ib
            else:
                score = abs(ea - eb) + 0.04 * ia + 0.04 * ib
            if best is None or score < best[0]:
                best = (score, ca, cb, ra, rb, ea, eb)
    return best


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        import analysis
        if opts.get("preview"):
            return _preview_job(job, a_path, b_path, opts)

        style = opts.get("style", "automix")
        if style != "auto" and style not in STYLES:
            raise ValueError(f"unknown style: {style}")
        beats = int(opts.get("beats") or 32)
        ctx = _prep(job, a_path, b_path, beats,
                    need_stretch=(style == "auto" or style not in HARD_STYLES))
        if style == "auto":
            style = _auto_style(ctx, job, analysis, a_path, b_path)
            job["style_chosen"] = style
        T, bar, bpm, work = ctx["T"], ctx["bar"], ctx["bpm"], ctx["work"]

        if style == "automix" and not (0.9 <= ctx["ratio"] <= 1.12):
            job["fallback"] = f"crossfade (tempos too far apart, x{ctx['ratio']:.2f})"
            style = "crossfade"
            T = min(T, 4 * bar)

        bvar = ctx["make_b"](stretched=style not in HARD_STYLES,
                             override_orig=opts.get("b_start"))
        b_matched = bvar["file"]
        manual_cut = opts.get("cut")

        if manual_cut is None and not bvar.get("manual"):
            # joint choice: pair the exit and entry whose energies match
            a_cands = _exit_candidates(ctx, a_path, style, T)
            b_cands = _entry_candidates(bvar, style, T)
            _, cut, b_start, reason_a, plan_b, ea, eb = _choose_pair(a_cands, b_cands)
            ctx["cut_reason"] = reason_a
            job["entry_plan"] = plan_b
            if ea is not None and eb is not None:
                job["energy_match"] = f"exit {ea:.2f} ↔ entry {eb:.2f}"
        else:
            b_start, job["entry_plan"] = _entry_for(bvar, style, T)
            if manual_cut is not None:
                cut = _manual_cut(ctx, a_path, style, T, float(manual_cut))
            else:
                cut = _pick_cut(ctx, a_path, style, T)
        job["b_skip"] = round(b_start, 2)

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
                      ctx["med_a"], sa_cut, sb, work, out_path, clash=ctx.get("clash", False))
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

    b_soft_var = ctx["make_b"](stretched=True, override_orig=opts.get("b_start"), fast=True)
    b_hard_var = (ctx["make_b"](stretched=False, override_orig=opts.get("b_start"), fast=True)
                  if any(s in HARD_STYLES for s in styles) else b_soft_var)
    b_soft = {"file": b_soft_var["file"], "b_start": _entry_for(b_soft_var, "automix", T)[0]}
    b_hard = {"file": b_hard_var["file"], "b_start": _entry_for(b_hard_var, "cut", T)[0]}

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
                          ctx["med_a"], sa_cut, sb, work, out, tmax=PRE + T + 15,
                          clash=ctx.get("clash", False))
            previews.append({"style": style, "file": f"/djmixes/{out.name}"})
        except Exception as e:
            previews.append({"style": style, "error": str(e)})
    job["previews"] = previews
    job["stage"] = "Done! Transition hits at 0:12 in every clip."


@bp.post("/dj/feedback")
def dj_feedback():
    """👍/👎 on a preview/result; stored with pair context to tune defaults."""
    import json as _json
    import time as _time
    data = request.get_json(silent=True) or {}
    style = data.get("style")
    verdict = data.get("verdict")
    if style not in STYLES or verdict not in (1, -1):
        return jsonify(error="bad feedback"), 400
    entry = {"ts": int(_time.time()), "style": style, "verdict": verdict,
             "a": Path(data.get("a_file", "")).name, "b": Path(data.get("b_file", "")).name,
             "beats": data.get("beats")}
    try:
        import analysis
        for k, f in (("a_facts", data.get("a_file")), ("b_facts", data.get("b_file"))):
            p = _resolve(f or "")
            if p is not None:
                entry[k] = analysis.analyze(p)
    except Exception:
        pass
    with open(FEEDBACK_FILE, "a") as fh:
        fh.write(_json.dumps(entry) + "\n")
    return jsonify(ok=True, bias=_feedback_bias())


def _set_job(job, paths, beats):
    """Playlist AutoMix: order tracks by BPM/key compatibility, then chain them
    with stem-blend joins into one continuous set."""
    import analysis
    try:
        n = len(paths)
        facts, grids = [], []
        for i, p in enumerate(paths):
            job["stage"] = f"Analyzing tracks... ({i + 1}/{n})"
            grids.append(_load_grid(analysis, p))
            facts.append(analysis.analyze(p))

        # ordering: greedy chain minimizing tempo distance + key penalty
        import math
        bpms = [g["bpm"] or 120 for g in grids]
        start = min(range(n), key=lambda i: abs(bpms[i] - float(np.median(bpms))))
        order, used = [start], {start}
        while len(order) < n:
            cur = order[-1]

            def cost(j):
                r = _fold_ratio(bpms[cur] / bpms[j])
                shift, clash = analysis.harmony_plan(facts[cur].get("camelot"),
                                                     facts[j].get("camelot"))
                return abs(math.log(r)) * 3 + (1.0 if clash else 0.25 * abs(shift))
            nxt = min((j for j in range(n) if j not in used), key=cost)
            order.append(nxt)
            used.add(nxt)
        job["order"] = [Path(paths[i]).stem for i in order]

        work = Path(tempfile.mkdtemp(prefix="djset_"))
        X = 0.05
        r0, _ = analysis.rms_profile(paths[order[0]])
        med0 = _active_median(r0)

        # build tempo-chained, gain-matched versions of every track
        matched, mgrids = [], []
        target_bpm = bpms[order[0]]
        for k, i in enumerate(order):
            job["stage"] = f"Tempo-chaining... ({k + 1}/{n})"
            ratio = 1.0 if k == 0 else _fold_ratio(target_bpm / bpms[i])
            ri, _ = analysis.rms_profile(paths[i])
            gain = float(np.clip(med0 / max(_active_median(ri), 1e-6), 0.5, 2.0))
            out = work / f"m{k}.wav"
            af = (f"atempo={ratio:.4f}," if abs(ratio - 1) > 0.005 else "") + f"volume={gain:.3f}"
            _run(["ffmpeg", "-y", "-i", str(paths[i]), "-af", af, "-ac", "2", "-ar", "44100", str(out)])
            matched.append(out)
            mgrids.append(_grid_scaled(grids[i], ratio if abs(ratio - 1) > 0.005 else 1.0))
            target_bpm = bpms[i] * ratio

        pieces = []           # wav pieces to be joined with tiny crossfades
        tracklist = [{"title": Path(paths[order[0]]).stem, "at": 0.0}]
        pos = 0.0
        prev_start = 0.0
        for k in range(1, n):
            A, B = matched[k - 1], matched[k]
            ga, gb = mgrids[k - 1], mgrids[k]
            bpm = ga["bpm"] or 120
            T = max(2.0, beats * 60.0 / bpm)
            job["stage"] = f"Join {k}/{n - 1}: picking points..."
            r_a, win_a = analysis.rms_profile(A)
            ctx = {"grid_a": ga, "bar": ga["bar_len"], "r_a": r_a, "win_a": win_a,
                   "med_a": _active_median(r_a)}
            a_cands = _exit_candidates(ctx, A, "automix", T)
            secs_b = analysis.detect_sections(B)
            bvar = {"snap": lambda t, g=gb: _snap_grid(t, g), "sections": secs_b,
                    "drop": _find_drop(secs_b), "loud": gb["bar"]}
            b_cands = _entry_candidates(bvar, "automix", T)
            _, cut, b_start, _, _, _, _ = _choose_pair(a_cands, b_cands)
            clash = analysis.harmony_plan(facts[order[k - 1]].get("camelot"),
                                                  facts[order[k]].get("camelot"))[1]

            job["stage"] = f"Join {k}/{n - 1}: stems (Demucs)..."
            sa = _separate_window(A, cut, T, work, f"a{k}")
            sb = _separate_window(B, b_start, T, work, f"b{k}")
            delta = analysis.align_beats(A, cut, B, b_start, min(T, 4 * ga["bar_len"]), bpm)[0]

            job["stage"] = f"Join {k}/{n - 1}: rendering..."
            env_a, env_b = _stem_envelopes("automix", T, ga["bar_len"], clash)
            v = _vocal_safe_start(sb["vocals"], T / 2 + ga["bar_len"], T)
            if v is not None:
                env_b["vocals"] = f"volume=0:enable='lt(t,{v:.3f})',afade=t=in:st={v:.3f}:d=0.5"
            join = work / f"join{k}.wav"
            order_s = ["vocals", "drums", "bass", "other"]
            cmd = ["ffmpeg", "-y"]
            for s in order_s:
                cmd += ["-i", str(sa[s])]
            for s in order_s:
                cmd += ["-i", str(sb[s])]
            fa = [f"[{i2}:a]{env_a[s]}[sa{i2}]" for i2, s in enumerate(order_s)]
            fb = [f"[{i2 + 4}:a]{env_b[s]},adelay={max(0, int(delta * 1000))}|{max(0, int(delta * 1000))}[sb{i2}]"
                  for i2, s in enumerate(order_s)]
            fc = ";".join(fa + fb) + (
                ";[sa0][sa1][sa2][sa3]amix=inputs=4:normalize=0[at]"
                ";[sb0][sb1][sb2][sb3]amix=inputs=4:normalize=0[bt]"
                ";[at][bt]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(cmd + ["-filter_complex", fc, "-map", "[out]", str(join)])

            head = work / f"head{k}.wav"
            _extract(matched[k - 1], prev_start, cut - prev_start, head)
            pieces += [head, join]
            pos += (cut - prev_start) + T / 2
            tracklist.append({"title": Path(paths[order[k]]).stem, "at": round(pos, 1)})
            pos += T / 2 - X * 2
            prev_start = b_start + T - X

        tail = work / "tail.wav"
        _extract(matched[-1], prev_start, _duration(matched[-1]) - prev_start, tail)
        pieces.append(tail)

        job["stage"] = "Stitching the set..."
        acc = pieces[0]
        for i, p in enumerate(pieces[1:]):
            nxt = work / f"acc{i}.wav"
            _run(["ffmpeg", "-y", "-i", str(acc), "-i", str(p),
                  "-filter_complex", f"[0:a][1:a]acrossfade=d={X}[out]",
                  "-map", "[out]", str(nxt)])
            acc = nxt
        out_name = f"set_{uuid.uuid4().hex[:6]}.mp3"
        out_path = DJ_DIR / out_name
        _run(["ffmpeg", "-y", "-i", str(acc), "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        job["tracklist"] = tracklist
        job["file"] = f"/djmixes/{out_name}"
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


@bp.post("/dj/set/start")
def dj_set_start():
    data = request.get_json(silent=True) or {}
    paths = [_resolve(f) for f in (data.get("files") or [])]
    if any(p is None for p in paths) or len(paths) < 2:
        return jsonify(error="pick at least two tracks"), 400
    beats = int(data.get("beats") or 32)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"stage": "Starting...", "done": False, "error": None, "file": None}
    threading.Thread(target=_set_job, args=(jobs[job_id], paths, beats), daemon=True).start()
    return jsonify(job=job_id)


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
        grid_a = _load_grid(analysis, a)
        grid_b = _load_grid(analysis, b)
        bpm = grid_a["bpm"] or 120
        T = max(2.0, beats * 60.0 / bpm)
        r_a, win_a = analysis.rms_profile(a)
        ctx = {"grid_a": grid_a, "bar": grid_a["bar_len"],
               "r_a": r_a, "win_a": win_a, "med_a": _active_median(r_a)}
        cut = _pick_cut(ctx, a, "automix", T)

        secs_b = analysis.detect_sections(b)
        drop = _find_drop(secs_b)
        ratio = 1.0
        if grid_a["bpm"] and grid_b["bpm"]:
            ratio = _fold_ratio(grid_a["bpm"] / grid_b["bpm"])
        if drop is not None:
            # drop-to-drop default: enter early so B's drop lands on the swap
            # (offset converted to B's original timeline)
            b_start = _snap_grid(max(0.0, _snap_grid(drop, grid_b) - 0.5 * T * ratio), grid_b)
        else:
            b_start = grid_b["bar"]

        return jsonify(
            T=round(T, 2),
            a={"duration": round(_duration(a), 2), "bpm": grid_a["bpm"],
               "bar_phase": round(grid_a["bar"], 3), "bar_len": round(grid_a["bar_len"], 4),
               "downbeats": grid_a.get("downbeats"),
               "sections": ctx.get("a_sections", analysis.detect_sections(a)),
               "cut": round(cut, 2), "cut_reason": ctx.get("cut_reason")},
            b={"duration": round(_duration(b), 2), "bpm": grid_b["bpm"],
               "bar_phase": round(grid_b["bar"], 3), "bar_len": round(grid_b["bar_len"], 4),
               "downbeats": grid_b.get("downbeats"),
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
