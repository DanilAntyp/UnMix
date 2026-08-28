"""DJ transitions: join track A into track B with a proper DJ-style transition.

Styles:
  neural    - stem-based layered blend: A's vocals leave first, the bass swaps
              on the midpoint, drums hand over last (djay "Neural Mix" style)
  bassswap  - everything crossfades over the transition, but the basslines hard-swap
  crossfade - classic equal-power crossfade
  filter    - A drains through a rising highpass sweep while B fades in
  echo      - A chops off into echo tails while B drops in
  cut       - clean switch on the beat

Fast because stems are only extracted from the transition WINDOW (seconds, not
whole songs). B is tempo-stretched to A's BPM so the overlap stays on the grid.
"""
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory

from separator import get_separator, sep_lock

APP_DIR = Path(__file__).parent
DJ_DIR = APP_DIR / "djmixes"
DL_DIR = APP_DIR / "downloads"
DJ_DIR.mkdir(exist_ok=True)

bp = Blueprint("dj", __name__)
jobs = {}

STYLES = ("neural", "bassswap", "crossfade", "filter", "echo", "cut")


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


def _separate_segment(src: Path, start: float, dur: float, work: Path, tag: str) -> dict:
    """Cut [start, start+dur] out of src and split that segment into 4 stems."""
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


# Per-style stem envelopes over the transition window of length T.
# Each entry: filter chain applied to that stem (local clock 0..T).
def _stem_envelopes(style: str, T: float):
    if style == "neural":
        a = {
            "vocals": f"afade=t=out:st=0:d={0.3 * T:.3f}",
            "other": f"afade=t=out:st={0.25 * T:.3f}:d={0.5 * T:.3f}",
            "bass": f"volume=0:enable='gte(t,{0.5 * T:.3f})'",
            "drums": f"afade=t=out:st={0.5 * T:.3f}:d={0.5 * T:.3f}",
        }
        b = {
            "drums": f"afade=t=in:st=0:d={0.4 * T:.3f}",
            "bass": f"volume=0:enable='lt(t,{0.5 * T:.3f})'",
            "other": f"afade=t=in:st={0.15 * T:.3f}:d={0.45 * T:.3f}",
            "vocals": f"afade=t=in:st={0.3 * T:.3f}:d={0.5 * T:.3f}",
        }
    else:  # bassswap
        a = {
            "vocals": f"afade=t=out:st=0:d={T:.3f}",
            "other": f"afade=t=out:st=0:d={T:.3f}",
            "drums": f"afade=t=out:st=0:d={T:.3f}",
            "bass": f"volume=0:enable='gte(t,{0.5 * T:.3f})'",
        }
        b = {
            "vocals": f"afade=t=in:st=0:d={T:.3f}",
            "other": f"afade=t=in:st=0:d={T:.3f}",
            "drums": f"afade=t=in:st=0:d={T:.3f}",
            "bass": f"volume=0:enable='lt(t,{0.5 * T:.3f})'",
        }
    return a, b


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        import analysis
        style = opts.get("style", "neural")
        if style not in STYLES:
            raise ValueError(f"unknown style: {style}")
        beats = int(opts.get("beats") or 16)

        job["stage"] = "Analyzing both tracks..."
        a_facts = analysis.analyze(a_path)
        b_facts = analysis.analyze(b_path)
        job["a_facts"], job["b_facts"] = a_facts, b_facts
        bpm = a_facts.get("bpm") or 120
        T = max(2.0, beats * 60.0 / bpm)          # transition length in seconds

        work = Path(tempfile.mkdtemp(prefix="dj_"))

        # tempo-match B to A over the whole track
        job["stage"] = "Tempo-matching track B..."
        ratio = 1.0
        if a_facts.get("bpm") and b_facts.get("bpm"):
            ratio = _fold_ratio(a_facts["bpm"] / b_facts["bpm"])
        b_matched = work / "b_matched.wav"
        af = f"atempo={ratio:.4f}" if abs(ratio - 1) > 0.005 else "anull"
        _run(["ffmpeg", "-y", "-i", str(b_path), "-af", af, "-ac", "2", "-ar", "44100", str(b_matched)])
        job["stretch"] = round(ratio, 4)

        dur_a = _duration(a_path)
        if style in ("cut", "echo"):
            cut = dur_a - 1.0
        else:
            cut = dur_a - T - 1.5
        if cut < 5:
            raise ValueError("track A is too short for this transition length")
        cut = analysis.snap_to_beat(a_path, cut)
        job["transition_at"] = round(cut, 2)

        out_name = f"{a_path.stem}_to_{b_path.stem}_{style}.mp3"
        out_path = DJ_DIR / out_name
        X = 0.05  # seam crossfade

        if style in ("neural", "bassswap"):
            job["stage"] = "Extracting stems from track A (Demucs)..."
            sa = _separate_segment(a_path, cut, T, work, "a")
            job["stage"] = "Extracting stems from track B (Demucs)..."
            sb = _separate_segment(b_matched, 0, T, work, "b")

            job["stage"] = "Rendering the transition..."
            env_a, env_b = _stem_envelopes(style, T)
            # inputs: 0=A full, 1..4 = A stems, 5..8 = B stems, 9 = B matched
            cmd = ["ffmpeg", "-y", "-i", str(a_path)]
            order = ["vocals", "drums", "bass", "other"]
            for k in order:
                cmd += ["-i", str(sa[k])]
            for k in order:
                cmd += ["-i", str(sb[k])]
            cmd += ["-i", str(b_matched)]
            fa = [f"[{i + 1}:a]{env_a[k]}[sa{i}]" for i, k in enumerate(order)]
            fb = [f"[{i + 5}:a]{env_b[k]}[sb{i}]" for i, k in enumerate(order)]
            fc = ";".join(fa + fb) + (
                f";[sa0][sa1][sa2][sa3]amix=inputs=4:normalize=0[atrans]"
                f";[sb0][sb1][sb2][sb3]amix=inputs=4:normalize=0[btrans]"
                f";[0:a]atrim=0:{cut + X:.3f}[ahead]"
                f";[ahead][atrans]acrossfade=d={X}[aall]"
                f";[9:a]atrim={T - X:.3f},asetpts=PTS-STARTPTS[brest]"
                f";[btrans][brest]acrossfade=d={X}[ball]"
                f";[ball]adelay={int(cut * 1000)}|{int(cut * 1000)}[bd]"
                f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]"
            )
            cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]
            _run(cmd)

        elif style == "crossfade":
            job["stage"] = "Rendering the transition..."
            fc = (f"[0:a]atrim=0:{cut + T:.3f}[a]"
                  f";[a][1:a]acrossfade=d={T:.3f}:c1=qsin:c2=qsin[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style == "filter":
            job["stage"] = "Rendering the transition..."
            # rising highpass sweep on A across the window, approximated in steps
            steps = 10
            cmds = []
            for i in range(steps):
                t = cut + T * i / steps
                f = 30 * (3000 / 30) ** (i / (steps - 1))
                cmds.append(f"{t:.3f} highpass@sw f {f:.0f}")
            send = ";".join(cmds)
            fc = (f"[0:a]atrim=0:{cut + T:.3f},asendcmd=c='{send}',highpass@sw=f=20,"
                  f"afade=t=out:st={cut + 0.6 * T:.3f}:d={0.4 * T:.3f}[a]"
                  f";[1:a]afade=t=in:st=0:d={0.6 * T:.3f},adelay={int(cut * 1000)}|{int(cut * 1000)}[b]"
                  f";[a][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        elif style == "echo":
            job["stage"] = "Rendering the transition..."
            beat_ms = int(60000 / bpm)
            tail = max(2.0, T)
            fc = (f"[0:a]atrim=0:{cut - 2 * 60 / bpm:.3f}[ahead]"
                  f";[0:a]atrim={cut - 2 * 60 / bpm:.3f}:{cut + 0.05:.3f},asetpts=PTS-STARTPTS,"
                  f"apad=pad_dur={tail:.3f},aecho=0.8:0.7:{beat_ms}|{beat_ms * 2}:0.5|0.3[atail]"
                  f";[ahead][atail]concat=n=2:v=0:a=1[aall]"
                  f";[1:a]afade=t=in:st=0:d=0.3,adelay={int(cut * 1000)}|{int(cut * 1000)}[b]"
                  f";[aall][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97[out]")
            _run(["ffmpeg", "-y", "-i", str(a_path), "-i", str(b_matched),
                  "-filter_complex", fc, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        else:  # cut
            job["stage"] = "Rendering the transition..."
            fc = (f"[0:a]atrim=0:{cut:.3f}[a]"
                  f";[a][1:a]acrossfade=d=0.03[out]")
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
