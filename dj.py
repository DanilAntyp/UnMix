"""DJ transitions: join track A into track B with a proper DJ-style transition.

AutoMix pipeline (what makes it sound right):
  - comb-scored BPM + bar grid on both tracks; everything lands on bar lines
  - A hands over at a VOCAL PHRASE boundary before its outro fade (the vocal
    stem's energy is used to find a gap, so no phrase gets cut mid-word)
  - B skips its quiet intro and enters at its first strong bar
  - B is tempo-stretched to A and gain-matched to A's loudness
  - B's beats are micro-nudged onto A's (onset cross-correlation)
  - choreography keeps dual-rhythm overlap to ~1 bar and never overlaps vocals;
    the bass swap is a fast crossfade, not a hard mute
  - risk detector: if tempos are too far apart or the beat alignment is not
    confident, automix falls back to a short clean crossfade instead of mush

Stems are separated only from the needed windows, so jobs stay fast.
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
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
          "-ac", "2", "-ar", "44100", str(seg)])
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


def _fx_chunk(src: Path, start: float, src_dur: float, work: Path, fn) -> Path:
    """Extract [start, start+src_dur] of src and run a djfx function over it."""
    import djfx
    seg_wav = work / "fx_src.wav"
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{max(0, start):.3f}", "-t", f"{src_dur:.3f}",
          "-ac", "2", "-ar", "44100", str(seg_wav)])
    y, sr = djfx.load(seg_wav)
    out = work / "fx_out.wav"
    djfx.save(out, fn(y, sr), sr)
    return out


AFMT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def _stem_envelopes(style: str, T: float, bar: float):
    def fade(kind, st, d):
        st = max(0.0, min(st, T - 0.1))
        d = max(0.1, min(d, T - st))
        return f"afade=t={kind}:st={st:.3f}:d={d:.3f}"

    if style == "automix":
        # The only moment two rhythm sections coexist is ~1 bar around the
        # swap; the two vocals never overlap; the bass swap is a fast fade.
        swap = T / 2
        a = {
            "vocals": fade("out", 0, bar),
            "other": fade("out", bar, 2 * bar),
            "bass": fade("out", swap - 0.15, 0.3),
            "drums": fade("out", swap, bar),
        }
        b = {
            "other": fade("in", bar, 2 * bar),
            "drums": f"volume=0:enable='lt(t,{swap:.3f})'",  # the beat drops on the bar
            "bass": fade("in", swap - 0.05, 0.25),
            "vocals": fade("in", swap + bar, 2 * bar),
        }
    elif style == "acapella":
        # A's instruments leave fast, A's vocal stands NAKED, then B's beat
        # drops underneath it; A vocal bows out, B vocal takes over.
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


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        import analysis
        style = opts.get("style", "automix")
        if style not in STYLES:
            raise ValueError(f"unknown style: {style}")
        beats = int(opts.get("beats") or 32)

        job["stage"] = "Reading the beat grids..."
        grid_a = analysis.beat_grid(a_path)
        grid_b = analysis.beat_grid(b_path)
        job["a_facts"] = analysis.analyze(a_path)
        job["b_facts"] = analysis.analyze(b_path)
        bpm = grid_a["bpm"] or 120
        T = max(2.0, beats * 60.0 / bpm)
        bar_a = grid_a["bar_len"]

        work = Path(tempfile.mkdtemp(prefix="dj_"))

        # loudness match B to A
        r_a, win_a = analysis.rms_profile(a_path)
        r_b0, _ = analysis.rms_profile(b_path)
        med_a, med_b = _active_median(r_a), _active_median(r_b0)
        gain_b = float(np.clip(med_a / med_b, 0.5, 2.0)) if med_b > 0 else 1.0

        # tempo-match (and gain-match) B
        job["stage"] = "Tempo-matching track B..."
        ratio = 1.0
        if grid_a["bpm"] and grid_b["bpm"]:
            ratio = _fold_ratio(grid_a["bpm"] / grid_b["bpm"])
        af = (f"atempo={ratio:.4f}," if abs(ratio - 1) > 0.005 else "") + f"volume={gain_b:.3f}"
        b_matched = work / "b_matched.wav"
        _run(["ffmpeg", "-y", "-i", str(b_path), "-af", af, "-ac", "2", "-ar", "44100", str(b_matched)])
        job["stretch"] = round(ratio, 4)

        # tempo sanity: too far apart -> automix falls back to a clean fade
        if style == "automix" and not (0.9 <= ratio <= 1.12):
            job["fallback"] = f"crossfade (tempos too far apart, x{ratio:.2f})"
            style = "crossfade"
            T = min(T, 4 * bar_a)

        # B entry point: first strong bar (skip a quiet intro)
        grid_bm = analysis.beat_grid(b_matched)
        r_b, win_b = analysis.rms_profile(b_matched)
        med_bm = _active_median(r_b)
        bar_b = grid_bm["bar_len"]
        b_start = 0.0
        t = grid_bm["bar"]
        while t < min(60, _duration(b_matched) - 8):
            if _mean_rms(r_b, win_b, t, t + 2 * bar_b) >= 0.6 * med_bm:
                b_start = t
                break
            t += bar_b
        job["b_skip"] = round(b_start, 2)

        # A handover point: a bar boundary before the outro fade
        dur_a = _duration(a_path)
        target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
        if target < 10:
            raise ValueError("track A is too short for this transition length")
        k = int((target - grid_a["bar"]) // bar_a)
        cut = grid_a["bar"] + k * bar_a
        look = min(T, 8.0)
        while cut > 20 and _mean_rms(r_a, win_a, cut, cut + look) < 0.45 * med_a:
            cut -= bar_a

        sa = None
        win_start = 0.0
        if style in STEM_STYLES:
            # one separation of A's tail window: used both to find a vocal
            # phrase boundary for the cut AND for the transition stems
            win_start = max(0.0, min(cut - 6 * bar_a, dur_a - T - 2) - 2 * bar_a)
            win_len = dur_a - win_start
            job["stage"] = "Extracting stems from track A (Demucs)..."
            sa = _separate_window(a_path, win_start, win_len, work, "a")

            # refine the cut: prefer a bar boundary inside a vocal gap
            # (except acapella, which WANTS the vocal running at the cut)
            r_v, win_v = analysis.rms_profile(sa["vocals"])
            med_v = _active_median(r_v)
            if med_v > 1e-4 and style != "acapella":
                cand = cut
                best = None
                for _ in range(8):  # try up to 8 bars back
                    local = cand - win_start
                    if local < 0:
                        break
                    voc = _mean_rms(r_v, win_v, max(0, local - 0.3), local + 0.75 * bar_a)
                    energy_ok = _mean_rms(r_a, win_a, cand, cand + look) >= 0.45 * med_a
                    if voc < 0.35 * med_v and energy_ok:
                        best = cand
                        break
                    cand -= bar_a
                if best is not None:
                    cut = best
        job["transition_at"] = round(cut, 2)

        out_name = f"{a_path.stem}_to_{b_path.stem}_{style}.mp3"
        out_path = DJ_DIR / out_name
        X = 0.05  # seam crossfade

        delay_ms = int(cut * 1000)
        if style in STEM_STYLES:
            job["stage"] = "Extracting stems from track B (Demucs)..."
            sb = _separate_window(b_matched, b_start, T, work, "b")

            # micro-align B's beats onto A's inside the overlap
            delta, conf = analysis.align_beats(a_path, cut, b_matched, b_start,
                                               min(T, 4 * bar_a), bpm)
            job["nudge_ms"] = int(delta * 1000)
            job["beat_confidence"] = round(conf, 2)
            delay_ms = max(0, int((cut + delta) * 1000))
            if style == "automix" and conf < 0.25:
                job["fallback"] = "crossfade (low beat-match confidence)"
                style = "crossfade"
                T = min(T, 4 * bar_a)

        if style in STEM_STYLES:
            # slice A's transition stems out of the analyzed window
            sa_cut = {}
            for k2, p in sa.items():
                out = work / f"a_{k2}_cut.wav"
                _slice(p, cut - win_start, T, out)
                sa_cut[k2] = out

            job["stage"] = "Rendering the transition..."
            env_a, env_b = _stem_envelopes(style, T, bar_a)
            cmd = ["ffmpeg", "-y", "-i", str(a_path)]
            order = ["vocals", "drums", "bass", "other"]
            for k2 in order:
                cmd += ["-i", str(sa_cut[k2])]
            for k2 in order:
                cmd += ["-i", str(sb[k2])]
            cmd += ["-i", str(b_matched)]
            fa = [f"[{i + 1}:a]{env_a[k2]}[sa{i}]" for i, k2 in enumerate(order)]
            fb = [f"[{i + 5}:a]{env_b[k2]}[sb{i}]" for i, k2 in enumerate(order)]
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
            cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]
            _run(cmd)

        elif style == "crossfade":
            job["stage"] = "Rendering the transition..."
            fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
                  f";[0:a]atrim=0:{cut + T:.3f}[a]"
                  f";[a][b1]acrossfade=d={T:.3f}:c1=qsin:c2=qsin[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style == "filter":
            job["stage"] = "Rendering the transition..."
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
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style == "echo":
            job["stage"] = "Rendering the transition..."
            beat_ms = int(60000 / bpm)
            tail = max(2.0, T)
            pre = 2 * 60 / bpm
            fc = (f"[0:a]atrim=0:{cut - pre:.3f}[ahead]"
                  f";[0:a]atrim={cut - pre:.3f}:{cut + 0.05:.3f},asetpts=PTS-STARTPTS,"
                  f"apad=pad_dur={tail:.3f},aecho=0.8:0.7:{beat_ms}|{beat_ms * 2}:0.5|0.3[atail]"
                  f";[ahead][atail]concat=n=2:v=0:a=1[aall]"
                  f";[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
                  f"adelay={delay_ms}|{delay_ms}[b]"
                  f";[aall][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style in ("tapestop", "backspin", "looproll"):
            job["stage"] = "Rendering the transition..."
            import djfx
            if style == "tapestop":
                fx_dur = min(1.2, max(0.5, bar_a / 2))
                chunk = _fx_chunk(a_path, cut - fx_dur, fx_dur, work,
                                  lambda y, sr: djfx.tape_stop(y, sr, fx_dur))
            elif style == "backspin":
                fx_dur = min(1.0, max(0.45, bar_a / 2))
                src_len = min(cut - fx_dur, 3.5 * fx_dur)
                chunk = _fx_chunk(a_path, cut - fx_dur - src_len, src_len, work,
                                  lambda y, sr: djfx.backspin(y, sr, fx_dur))
            else:  # looproll
                fx_dur = bar_a
                chunk = _fx_chunk(a_path, cut - bar_a, bar_a, work,
                                  lambda y, sr: djfx.loop_roll(y, sr, bar_a))
            head_end = cut - fx_dur
            fc = (f"[0:a]atrim=0:{head_end:.3f},{AFMT}[h]"
                  f";[1:a]{AFMT}[fx]"
                  f";[h][fx]concat=n=2:v=0:a=1[aall]"
                  f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
                  f"adelay={int(cut * 1000)}|{int(cut * 1000)}[bd]"
                  f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(chunk), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style == "riser":
            job["stage"] = "Rendering the transition..."
            import djfx
            rise_dur = 4 * bar_a
            rise = work / "riser.wav"
            djfx.save(rise, djfx.riser(44100, rise_dur, gain=0.4 * float(med_a + 1e-3) * 3), 44100)
            rise_at = max(0, int((cut - rise_dur) * 1000))
            fc = (f"[0:a]atrim=0:{cut:.3f},afade=t=out:st={cut - 0.05:.3f}:d=0.05[a]"
                  f";[1:a]{AFMT},adelay={rise_at}|{rise_at}[r]"
                  f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
                  f"adelay={int(cut * 1000)}|{int(cut * 1000)}[bd]"
                  f";[a][r][bd]amix=inputs=3:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(rise), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        else:  # cut
            job["stage"] = "Rendering the transition..."
            fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
                  f";[0:a]atrim=0:{cut:.3f}[a]"
                  f";[a][b1]acrossfade=d=0.03[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        job["file"] = f"/djmixes/{out_name}"
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


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
