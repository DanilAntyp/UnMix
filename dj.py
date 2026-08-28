"""DJ transitions: join track A into track B with a proper DJ-style transition.

What makes it sound right (AutoMix-style):
  - bar-accurate grid: both tracks get a beat/downbeat grid; the transition
    starts on a bar boundary of A and B enters on one of its own bar boundaries
  - smart points: A hands over before its outro fade (not into silence),
    B skips its quiet intro and enters at its first strong bar
  - loudness match: B is gain-matched to A before mixing
  - tempo match: B is stretched to A's BPM (comb-scored tempo detection)

Styles: automix (subtle stem blend), neural (theatrical stem swap), bassswap,
crossfade, filter, echo, cut. Stems are separated only from the transition
window, so jobs stay fast.
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

STYLES = ("automix", "neural", "bassswap", "crossfade", "filter", "echo", "cut")


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


def _separate_segment(src: Path, start: float, dur: float, work: Path, tag: str) -> dict:
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


def _stem_envelopes(style: str, T: float):
    def fade(kind, st, d):
        return f"afade=t={kind}:st={st * T:.3f}:d={d * T:.3f}"

    if style == "automix":
        # gentle, Apple-AutoMix-like: long overlapping fades, bass never doubled
        a = {
            "vocals": fade("out", 0, 0.4),
            "other": fade("out", 0.2, 0.55),
            "bass": fade("out", 0.45, 0.2),
            "drums": fade("out", 0.55, 0.45),
        }
        b = {
            "drums": fade("in", 0, 0.3),
            "other": fade("in", 0.2, 0.45),
            "bass": fade("in", 0.55, 0.15),
            "vocals": fade("in", 0.5, 0.4),
        }
    elif style == "neural":
        a = {
            "vocals": fade("out", 0, 0.3),
            "other": fade("out", 0.25, 0.5),
            "bass": f"volume=0:enable='gte(t,{0.5 * T:.3f})'",
            "drums": fade("out", 0.5, 0.5),
        }
        b = {
            "drums": fade("in", 0, 0.4),
            "bass": f"volume=0:enable='lt(t,{0.5 * T:.3f})'",
            "other": fade("in", 0.15, 0.45),
            "vocals": fade("in", 0.3, 0.5),
        }
    else:  # bassswap
        a = {
            "vocals": fade("out", 0, 1.0),
            "other": fade("out", 0, 1.0),
            "drums": fade("out", 0, 1.0),
            "bass": f"volume=0:enable='gte(t,{0.5 * T:.3f})'",
        }
        b = {
            "vocals": fade("in", 0, 1.0),
            "other": fade("in", 0, 1.0),
            "drums": fade("in", 0, 1.0),
            "bass": f"volume=0:enable='lt(t,{0.5 * T:.3f})'",
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
        r_b0, win_b0 = analysis.rms_profile(b_path)
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
        target = dur_a - (1.0 if style in ("cut", "echo") else T + 1.0)
        if target < 10:
            raise ValueError("track A is too short for this transition length")
        k = int((target - grid_a["bar"]) // bar_a)
        cut = grid_a["bar"] + k * bar_a
        look = min(T, 8.0)
        while cut > 20 and _mean_rms(r_a, win_a, cut, cut + look) < 0.45 * med_a:
            cut -= bar_a
        job["transition_at"] = round(cut, 2)

        out_name = f"{a_path.stem}_to_{b_path.stem}_{style}.mp3"
        out_path = DJ_DIR / out_name
        X = 0.05  # seam crossfade

        if style in ("automix", "neural", "bassswap"):
            job["stage"] = "Extracting stems from track A (Demucs)..."
            sa = _separate_segment(a_path, cut, T, work, "a")
            job["stage"] = "Extracting stems from track B (Demucs)..."
            sb = _separate_segment(b_matched, b_start, T, work, "b")

            job["stage"] = "Rendering the transition..."
            env_a, env_b = _stem_envelopes(style, T)
            cmd = ["ffmpeg", "-y", "-i", str(a_path)]
            order = ["vocals", "drums", "bass", "other"]
            for k2 in order:
                cmd += ["-i", str(sa[k2])]
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
                f";[ball]adelay={int(cut * 1000)}|{int(cut * 1000)}[bd]"
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
                  f"adelay={int(cut * 1000)}|{int(cut * 1000)}[b]"
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
                  f"adelay={int(cut * 1000)}|{int(cut * 1000)}[b]"
                  f";[aall][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
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
