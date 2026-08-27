#!/usr/bin/env python3
"""Simple web UI for vocal extraction.

Run:  python ui.py
Then open http://localhost:5555 - drop a song in, get vocals + instrumental back.
"""
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, request, send_from_directory, jsonify

APP_DIR = Path(__file__).parent
OUT_DIR = APP_DIR / "separated"
OUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads

_separator = None
_lock = threading.Lock()


def get_separator():
    global _separator
    if _separator is None:
        import torch
        from demucs.api import Separator
        device = "mps" if torch.backends.mps.is_available() else (
            "cuda" if torch.cuda.is_available() else "cpu")
        _separator = Separator(model="htdemucs", device=device)
    return _separator


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vocal Extractor</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: -apple-system, system-ui, sans-serif;
    background: #111318; color: #e8eaf0;
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .card { width: 100%; max-width: 520px; text-align: center; }
  h1 { font-size: 1.5rem; margin-bottom: 6px; }
  .sub { color: #9aa1b0; margin-bottom: 28px; }
  #drop {
    border: 2px dashed #3a4152; border-radius: 16px; padding: 56px 24px;
    cursor: pointer; transition: border-color .15s, background .15s;
  }
  #drop.hover { border-color: #7c9cff; background: #1a1f2b; }
  #drop .icon { font-size: 2.4rem; margin-bottom: 12px; }
  #drop p { color: #9aa1b0; }
  #drop strong { color: #e8eaf0; }
  input[type=file] { display: none; }
  #status { margin-top: 24px; color: #9aa1b0; min-height: 1.4em; }
  .spinner {
    display: inline-block; width: 16px; height: 16px; vertical-align: -3px;
    border: 2px solid #3a4152; border-top-color: #7c9cff; border-radius: 50%;
    animation: spin .8s linear infinite; margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #results { margin-top: 24px; display: none; text-align: left; }
  .stem {
    background: #1a1f2b; border-radius: 12px; padding: 16px; margin-bottom: 12px;
  }
  .stem .row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .stem .name { font-weight: 600; }
  .stem a {
    color: #7c9cff; text-decoration: none; font-size: .9rem;
    border: 1px solid #3a4152; padding: 5px 12px; border-radius: 8px;
  }
  .stem a:hover { background: #232a3a; }
  audio { width: 100%; }
  #again { margin-top: 8px; background: none; border: none; color: #7c9cff; cursor: pointer; font-size: .95rem; }
</style>
</head>
<body>
<div class="card">
  <h1>&#127908; Vocal Extractor</h1>
  <p class="sub">Drop a song &mdash; get the vocals and the instrumental.</p>

  <div id="drop">
    <div class="icon">&#127925;</div>
    <p><strong>Drop a song here</strong> or click to choose<br>
    <span style="font-size:.85rem">mp3, wav, flac, m4a &hellip;</span></p>
  </div>
  <input type="file" id="file" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac">

  <div id="status"></div>

  <div id="results">
    <div class="stem">
      <div class="row"><span class="name">&#127911; Vocals</span><a id="dl-v" download>Download</a></div>
      <audio id="au-v" controls></audio>
    </div>
    <div class="stem">
      <div class="row"><span class="name">&#127928; Instrumental</span><a id="dl-i" download>Download</a></div>
      <audio id="au-i" controls></audio>
    </div>
    <center><button id="again">Separate another song</button></center>
  </div>
</div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const status = document.getElementById('status');
const results = document.getElementById('results');

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { if (fileInput.files[0]) upload(fileInput.files[0]); });
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hover'); }));
drop.addEventListener('drop', ev => { const f = ev.dataTransfer.files[0]; if (f) upload(f); });
document.getElementById('again').addEventListener('click', () => {
  results.style.display = 'none'; drop.style.display = 'block'; status.textContent = ''; fileInput.value = '';
});

async function upload(file) {
  drop.style.display = 'none';
  results.style.display = 'none';
  status.innerHTML = '<span class="spinner"></span>Separating &ldquo;' + file.name +
    '&rdquo; &mdash; this takes about the length of the song&hellip;';
  const fd = new FormData();
  fd.append('audio', file);
  try {
    const res = await fetch('/separate', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'separation failed');
    status.textContent = 'Done!';
    for (const [k, url] of [['v', data.vocals], ['i', data.instrumental]]) {
      document.getElementById('au-' + k).src = url;
      const a = document.getElementById('dl-' + k);
      a.href = url; a.setAttribute('download', url.split('/').pop());
    }
    results.style.display = 'block';
  } catch (err) {
    status.textContent = 'Error: ' + err.message;
    drop.style.display = 'block';
  }
}
</script>
</body>
</html>"""


@app.get("/")
def index():
    return PAGE


@app.post("/separate")
def separate():
    f = request.files.get("audio")
    if f is None or f.filename == "":
        return jsonify(error="no file uploaded"), 400

    stem_name = Path(f.filename).stem or "song"
    suffix = Path(f.filename).suffix or ".mp3"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        from demucs.api import save_audio
        with _lock:  # one separation at a time
            sep = get_separator()
            _, stems = sep.separate_audio_file(tmp_path)
        vocals = stems["vocals"]
        instrumental = sum(v for k, v in stems.items() if k != "vocals")

        v_name = f"{stem_name}_vocals.wav"
        i_name = f"{stem_name}_instrumental.wav"
        save_audio(vocals, str(OUT_DIR / v_name), samplerate=sep.samplerate)
        save_audio(instrumental, str(OUT_DIR / i_name), samplerate=sep.samplerate)
        return jsonify(vocals=f"/separated/{v_name}", instrumental=f"/separated/{i_name}")
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/separated/<path:name>")
def download(name):
    return send_from_directory(OUT_DIR, name)


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5555")).start()
    print("Vocal Extractor running at http://localhost:5555")
    app.run(port=5555, threaded=True)
