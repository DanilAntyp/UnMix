"""Mashup maker: vocals from song A over the instrumental of song B.

Pipeline (background job):
  1. Analyze both songs (BPM + key, analysis.py).
  2. Demucs-separate A -> acapella; separate B -> instrumental.
  3. Time-stretch the acapella to B's tempo (ffmpeg atempo, pitch-preserving),
     optionally pitch-shift it by n semitones, optionally delay its entry.
  4. Mix acapella + instrumental with the chosen gains -> mp3.
"""
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory

from separator import get_separator, sep_lock

APP_DIR = Path(__file__).parent
MASHUP_DIR = APP_DIR / "mashups"
DL_DIR = APP_DIR / "downloads"
MASHUP_DIR.mkdir(exist_ok=True)

bp = Blueprint("mashup", __name__)
jobs = {}


def _resolve(url_path: str):
    if url_path.startswith("/downloads/"):
        p = (DL_DIR / Path(url_path).name).resolve()
        if p.parent == DL_DIR.resolve() and p.is_file():
            return p
    return None


def _separate(path: Path, keep_vocals: bool, work: Path, name: str) -> Path:
    from demucs.api import save_audio
    import separator as sep_mod
    with sep_lock:
        sep_mod.progress["pct"] = 0.0
        sep = get_separator()
        _, stems = sep.separate_audio_file(path)
    part = stems["vocals"] if keep_vocals else sum(v for k, v in stems.items() if k != "vocals")
    out = work / f"{name}.wav"
    save_audio(part, str(out), samplerate=sep.samplerate)
    return out


def _fold_ratio(ratio: float) -> float:
    """Keep the stretch musical: prefer half/double tempo over extreme stretches."""
    while ratio > 1.5:
        ratio /= 2
    while ratio < 0.667:
        ratio *= 2
    return ratio


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        import analysis
        job["stage"] = "Analyzing both songs..."
        a_facts = analysis.analyze(a_path)
        b_facts = analysis.analyze(b_path)
        job["a_facts"], job["b_facts"] = a_facts, b_facts

        work = Path(tempfile.mkdtemp(prefix="mashup_"))
        job["stage"] = "Extracting the acapella from song A (Demucs)..."
        acap = _separate(a_path, True, work, "acapella")
        job["stage"] = "Extracting the instrumental from song B (Demucs)..."
        instr = _separate(b_path, False, work, "instrumental")

        job["stage"] = "Tempo-matching and mixing..."
        ratio = 1.0
        if a_facts.get("bpm") and b_facts.get("bpm"):
            ratio = _fold_ratio(b_facts["bpm"] / a_facts["bpm"])
        job["stretch"] = round(ratio, 4)

        af = []
        if abs(ratio - 1.0) > 0.005:
            af.append(f"atempo={ratio:.4f}")
        semis = int(opts.get("semitones") or 0)
        if semis:
            factor = 2 ** (semis / 12)
            af.append(f"asetrate=44100*{factor:.6f},aresample=44100,atempo={1 / factor:.6f}")
        offset = float(opts.get("offset") or 0)
        acap_in = ["-i", str(acap)]
        if offset > 0:
            ms = int(offset * 1000)
            af.append(f"adelay={ms}|{ms}")
        elif offset < 0:
            acap_in = ["-ss", f"{-offset:.3f}", "-i", str(acap)]

        va = max(0.0, min(3.0, float(opts.get("acapella_gain") or 1.0)))
        vi = max(0.0, min(3.0, float(opts.get("instrumental_gain") or 1.0)))
        acap_chain = f"[1:a]{','.join(af) + ',' if af else ''}volume={va:.3f}[v]"
        fc = f"[0:a]volume={vi:.3f}[i];{acap_chain};[i][v]amix=inputs=2:duration=first:normalize=0[out]"

        out_name = f"{Path(a_path).stem}_x_{Path(b_path).stem}_mashup.mp3"
        out_path = MASHUP_DIR / out_name
        cmd = (["ffmpeg", "-y", "-i", str(instr)] + acap_in +
               ["-filter_complex", fc, "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + r.stderr[-300:])

        job["file"] = f"/mashups/{out_name}"
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


@bp.post("/mashup/start")
def mashup_start():
    data = request.get_json(silent=True) or {}
    a = _resolve(data.get("a_file", ""))
    b = _resolve(data.get("b_file", ""))
    if a is None or b is None:
        return jsonify(error="pick both songs first"), 400
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"stage": "Starting...", "done": False, "error": None, "file": None}
    threading.Thread(target=process_job, args=(job_id, a, b, data), daemon=True).start()
    return jsonify(job=job_id)


@bp.get("/mashup/status/<job_id>")
def mashup_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="unknown job"), 404
    out = dict(job)
    if "Demucs" in (out.get("stage") or ""):
        import separator as sep_mod
        out["pct"] = sep_mod.progress.get("pct")
    return jsonify(out)


@bp.get("/mashups/<path:name>")
def mashup_file(name):
    return send_from_directory(MASHUP_DIR, name)
