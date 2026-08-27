#!/usr/bin/env python3
"""Music toolbox web app.

The API lives here; the UI is a React app in web/ (see webapp.py).

Panel 1: download video or audio from a YouTube link (yt-dlp), with a
         quality/size table like classic downloader sites.
Panel 2: extract vocals / instrumental from a song (Demucs).
Plus: Karaoke Mode in a separate window (see karaoke.py).

Run:  python app.py   ->  http://localhost:5555
"""
import re
import subprocess
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, request, send_from_directory, jsonify

import separator
from separator import get_separator, sep_lock
import karaoke
import webapp

APP_DIR = Path(__file__).parent
OUT_DIR = APP_DIR / "separated"
DL_DIR = APP_DIR / "downloads"
CONV_DIR = APP_DIR / "converted"
OUT_DIR.mkdir(exist_ok=True)
DL_DIR.mkdir(exist_ok=True)
CONV_DIR.mkdir(exist_ok=True)

PROGRESS = {}  # yt-dlp download progress, keyed by client-chosen id

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.register_blueprint(karaoke.bp)
app.register_blueprint(webapp.bp)


MODEL_STEMS = {
    "htdemucs": ("vocals", "drums", "bass", "other"),
    "htdemucs_6s": ("vocals", "drums", "bass", "guitar", "piano", "other"),
}


def run_separation(audio_path: Path, stem_name: str, remove: str = "vocals",
                   model: str = "htdemucs") -> dict:
    from demucs.api import save_audio
    with sep_lock:
        separator.progress["pct"] = 0.0
        sep = get_separator(model)
        _, stems = sep.separate_audio_file(audio_path)
    if remove == "all":
        urls = {}
        for k in MODEL_STEMS[model]:
            name = f"{stem_name}_{k}.wav"
            save_audio(stems[k], str(OUT_DIR / name), samplerate=sep.samplerate)
            urls[k] = f"/separated/{name}"
        return {"all": urls, "remove": "all"}
    target = stems[remove]
    rest = sum(v for k, v in stems.items() if k != remove)
    t_name = f"{stem_name}_{remove}.wav"
    r_name = f"{stem_name}_no_{remove}.wav"
    save_audio(target, str(OUT_DIR / t_name), samplerate=sep.samplerate)
    save_audio(rest, str(OUT_DIR / r_name), samplerate=sep.samplerate)
    return {"target": f"/separated/{t_name}", "rest": f"/separated/{r_name}", "remove": remove}


@app.get("/")
def index():
    return webapp.page()


@app.post("/yt/info")
def yt_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="no url given"), 400

    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=str(e)), 500

    duration = info.get("duration") or 0

    audio = [
        {"label": "320k (.mp3)", "abr": 320, "size": duration * 320_000 / 8},
        {"label": "128k (.mp3)", "abr": 128, "size": duration * 128_000 / 8},
    ]

    # Best mp4/avc1 stream per common resolution, plus ~129k audio on top.
    audio_extra = duration * 129_000 / 8
    by_height = {}
    for f in info.get("formats", []):
        h = f.get("height")
        if h not in (1080, 720, 480, 360) or f.get("vcodec", "none") == "none":
            continue
        is_avc_mp4 = f.get("ext") == "mp4" and (f.get("vcodec") or "").startswith("avc1")
        size = (f.get("filesize") or f.get("filesize_approx")
                or (f.get("tbr") or 0) * 1000 / 8 * duration)
        rank = (is_avc_mp4, f.get("tbr") or 0)
        if h not in by_height or rank > by_height[h][0]:
            by_height[h] = (rank, size)
    video = [
        {"label": f"{h}p", "height": h, "size": (s + audio_extra) if s else 0}
        for h, (_rank, s) in sorted(by_height.items(), reverse=True)
    ]

    return jsonify(title=info.get("title", "video"), audio=audio, video=video)


@app.post("/yt")
def yt_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    kind = data.get("kind", "audio")
    if not url:
        return jsonify(error="no url given"), 400

    import yt_dlp

    if kind == "audio":
        abr = int(data.get("abr", 192))
        tag = f"{abr}k"
        opts = {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": str(abr)}],
        }
    else:
        h = int(data.get("height", 720))
        tag = f"{h}p"
        opts = {
            "format": (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                       f"/best[height<={h}][ext=mp4]/best[height<={h}]/best"),
            "merge_output_format": "mp4",
        }
    pid = data.get("pid")

    def hook(d):
        if not pid:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                PROGRESS[pid] = {"pct": round(d.get("downloaded_bytes", 0) * 100 / total, 1)}
        elif d.get("status") == "finished":
            PROGRESS[pid] = {"pct": 100.0}

    opts.update({
        "outtmpl": str(DL_DIR / f"%(title)s [{tag}].%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "progress_hooks": [hook],
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if kind == "audio":
            path = path.with_suffix(".mp3")
        if not path.exists():
            candidates = sorted(DL_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise FileNotFoundError("downloaded file not found")
            path = candidates[-1]
        return jsonify(title=info.get("title", path.stem), file=f"/downloads/{path.name}")
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if pid:
            PROGRESS.pop(pid, None)


@app.post("/separate")
def separate():
    # Either a browser upload, or a file already on the server (from the YT panel).
    if request.files.get("audio"):
        f = request.files["audio"]
        remove = request.form.get("remove", "vocals")
        model = request.form.get("model", "htdemucs")
    else:
        data = request.get_json(silent=True) or {}
        remove = data.get("remove", "vocals")
        model = data.get("model", "htdemucs")

    if model not in MODEL_STEMS:
        return jsonify(error=f"unknown model: {model}"), 400
    if remove not in MODEL_STEMS[model] + ("all",):
        return jsonify(error=f"unknown stem: {remove}"), 400

    if request.files.get("audio"):
        f = request.files["audio"]
        stem_name = Path(f.filename).stem or "song"
        suffix = Path(f.filename).suffix or ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = Path(tmp.name)
        try:
            return jsonify(run_separation(tmp_path, stem_name, remove, model))
        except Exception as e:
            return jsonify(error=str(e)), 500
        finally:
            tmp_path.unlink(missing_ok=True)

    server_file = data.get("server_file", "")
    if server_file.startswith("/downloads/"):
        # Resolve strictly inside DL_DIR to keep path traversal out.
        path = (DL_DIR / Path(server_file).name).resolve()
        if path.parent == DL_DIR.resolve() and path.is_file():
            try:
                return jsonify(run_separation(path, path.stem, remove, model))
            except Exception as e:
                return jsonify(error=str(e)), 500
    return jsonify(error="no file uploaded"), 400


@app.get("/progress/<pid>")
def get_progress(pid):
    if pid == "sep":
        return jsonify(separator.progress)
    return jsonify(PROGRESS.get(pid, {}))


TIME_RE = re.compile(r"^\d+(?::[0-5]?\d){0,2}(?:\.\d+)?$")  # ss / mm:ss / hh:mm:ss

CONV_FORMATS = {
    "mp3-320": ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    "mp3-192": ("mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "mp3-128": ("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
    "wav": ("wav", ["-c:a", "pcm_s16le"]),
    "flac": ("flac", ["-c:a", "flac"]),
    "m4a": ("m4a", ["-c:a", "aac", "-b:a", "192k"]),
}


@app.post("/convert")
def convert():
    f = request.files.get("audio")
    if f is None or f.filename == "":
        return jsonify(error="no file uploaded"), 400
    fmt = request.form.get("format", "mp3-192")
    if fmt not in CONV_FORMATS:
        return jsonify(error=f"unknown format: {fmt}"), 400
    start = (request.form.get("start") or "").strip()
    end = (request.form.get("end") or "").strip()
    for t in (start, end):
        if t and not TIME_RE.match(t):
            return jsonify(error=f"bad time '{t}' - use mm:ss"), 400

    ext, codec_args = CONV_FORMATS[fmt]
    stem = Path(f.filename).stem or "audio"
    suffix = Path(f.filename).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = Path(tmp.name)
    try:
        out_name = f"{stem}{'_cut' if (start or end) else ''}.{ext}"
        out_path = CONV_DIR / out_name
        cmd = ["ffmpeg", "-y", "-i", str(tmp_path), "-vn"]
        if start:
            cmd += ["-ss", start]
        if end:
            cmd += ["-to", end]
        cmd += codec_args + [str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return jsonify(error="ffmpeg failed: " + r.stderr[-300:]), 500
        return jsonify(file=f"/converted/{out_name}", name=out_name)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/converted/<path:name>")
def get_converted(name):
    return send_from_directory(CONV_DIR, name)


@app.get("/separated/<path:name>")
def get_separated(name):
    return send_from_directory(OUT_DIR, name)


@app.get("/downloads/<path:name>")
def get_download(name):
    return send_from_directory(DL_DIR, name)


if __name__ == "__main__":
    if not (webapp.DIST / "index.html").is_file():
        print("warning: web/dist is missing - build the frontend with "
              "`cd web && npm install && npm run build`")
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5555")).start()
    print("UnMix running at http://localhost:5555")
    app.run(port=5555, threaded=True)
