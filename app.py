#!/usr/bin/env python3
"""Music toolbox web app.

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
MIDI_DIR = APP_DIR / "midi"
MIDI_DIR.mkdir(exist_ok=True)


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


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UnMix</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: -apple-system, system-ui, sans-serif;
    background: #111318; color: #e8eaf0; min-height: 100vh; padding: 32px 24px;
  }
  header { text-align: center; margin-bottom: 10px; }
  header h1 { font-size: 1.7rem; }
  header p { color: #9aa1b0; margin-top: 4px; }
  .karaoke-link { text-align: center; margin-bottom: 28px; }
  .karaoke-link a {
    display: inline-block; margin-top: 10px; color: #ffd166; text-decoration: none;
    border: 1px solid #4a4152; background: #221d26; padding: 8px 20px; border-radius: 10px;
  }
  .karaoke-link a:hover { background: #2d2532; }
  .panels {
    display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
    max-width: 1060px; margin: 0 auto;
  }
  @media (max-width: 860px) { .panels { grid-template-columns: 1fr; } }
  .panel {
    background: #191d26; border: 1px solid #262c3a; border-radius: 16px; padding: 24px;
  }
  .panel h2 { font-size: 1.15rem; margin-bottom: 4px; }
  .panel .desc { color: #9aa1b0; font-size: .9rem; margin-bottom: 18px; }
  input[type=url] {
    width: 100%; padding: 12px 14px; border-radius: 10px; border: 1px solid #3a4152;
    background: #111318; color: #e8eaf0; font-size: .95rem; outline: none;
  }
  input[type=url]:focus { border-color: #7c9cff; }
  button.go {
    width: 100%; margin-top: 12px; padding: 12px; border: none; border-radius: 10px; cursor: pointer;
    background: #7c9cff; color: #10131a; font-weight: 600; font-size: 1rem;
  }
  button.go:hover { background: #92adff; }
  button.go:disabled { background: #3a4152; color: #9aa1b0; cursor: default; }
  .status { margin-top: 14px; color: #9aa1b0; font-size: .92rem; min-height: 1.3em; word-break: break-word; }
  .spinner {
    display: inline-block; width: 14px; height: 14px; vertical-align: -2px;
    border: 2px solid #3a4152; border-top-color: #7c9cff; border-radius: 50%;
    animation: spin .8s linear infinite; margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* format table (like classic downloader sites) */
  #fmt { margin-top: 18px; display: none; border: 1px solid #262c3a; border-radius: 12px; overflow: hidden; }
  #fmt .title { padding: 12px 14px; font-weight: 600; font-size: .95rem; border-bottom: 1px solid #262c3a; }
  #fmt table { width: 100%; border-collapse: collapse; font-size: .93rem; }
  #fmt th {
    text-align: left; padding: 10px 14px; background: #202634; color: #c9cfdb;
    font-size: .9rem; font-weight: 600;
  }
  #fmt td { padding: 10px 14px; border-top: 1px solid #262c3a; }
  #fmt td.size { color: #9aa1b0; white-space: nowrap; }
  .badge {
    display: inline-block; background: #1e3a2b; color: #6fcf97; border-radius: 6px;
    padding: 1px 8px; font-size: .78rem; margin-left: 6px;
  }
  button.dl {
    background: #2f9e63; color: #fff; border: none; border-radius: 8px;
    padding: 8px 16px; cursor: pointer; font-size: .9rem; font-weight: 600; white-space: nowrap;
    font-family: inherit;
  }
  button.dl:hover { background: #37b571; }
  button.dl:disabled { background: #3a4152; color: #9aa1b0; cursor: default; }
  button.dl.karaoke { background: #b98a2e; margin-left: 6px; }
  button.dl.karaoke:hover { background: #d19f39; }
  button.dl.karaoke:disabled { background: #3a4152; color: #9aa1b0; }

  .result { margin-top: 16px; display: none; }
  .stem { background: #111318; border-radius: 12px; padding: 14px; margin-bottom: 10px; }
  .stem .row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; gap: 8px; }
  .stem .name { font-weight: 600; font-size: .95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn {
    color: #7c9cff; text-decoration: none; font-size: .85rem; background: none;
    border: 1px solid #3a4152; padding: 5px 12px; border-radius: 8px; cursor: pointer;
    white-space: nowrap; font-family: inherit;
  }
  .btn:hover { background: #232a3a; }
  audio, video { width: 100%; border-radius: 8px; }
  .drop {
    border: 2px dashed #3a4152; border-radius: 12px; padding: 36px 18px;
    cursor: pointer; text-align: center; transition: border-color .15s, background .15s;
  }
  .drop.hover { border-color: #7c9cff; background: #1a1f2b; }
  .drop p { color: #9aa1b0; font-size: .92rem; }
  .drop strong { color: #e8eaf0; }
  .drop .icon { font-size: 1.8rem; margin-bottom: 8px; }
  input[type=file] { display: none; }
  .picked { margin-top: 10px; color: #c9cfdb; font-size: .9rem; min-height: 1.2em; }
  .setting input.time {
    flex: 1; min-width: 0; padding: 9px 12px; border-radius: 10px;
    border: 1px solid #3a4152; background: #111318; color: #e8eaf0; font-size: .9rem;
  }
  .setting input.time:focus { border-color: #7c9cff; outline: none; }
  .setting {
    margin-top: 14px; display: flex; align-items: center; gap: 10px;
    font-size: .93rem; color: #c9cfdb;
  }
  .setting select {
    flex: 1; padding: 9px 12px; border-radius: 10px; border: 1px solid #3a4152;
    background: #111318; color: #e8eaf0; font-size: .93rem; font-family: inherit;
    outline: none; cursor: pointer;
  }
  .setting select:focus { border-color: #7c9cff; }
</style>
</head>
<body>
<header>
  <h1>&#127900; UnMix</h1>
  <p>Grab music from YouTube &middot; split any song into vocals and instrumental</p>
</header>
<div class="karaoke-link">
  <a href="/karaoke" onclick="window.open('/karaoke','karaoke','width=760,height=950'); return false;">
    &#127909; Karaoke Mode &#8599;
  </a>
</div>

<div class="panels">

  <!-- Panel 1: YouTube downloader -->
  <section class="panel">
    <h2>&#11015;&#65039; YouTube Downloader</h2>
    <p class="desc">Paste a link, pick a quality.</p>
    <input type="url" id="yt-url" placeholder="https://www.youtube.com/watch?v=...">
    <button class="go" id="yt-go">Get formats</button>
    <div class="status" id="yt-status"></div>

    <div id="fmt">
      <div class="title" id="fmt-title"></div>
      <table>
        <tbody id="fmt-rows"></tbody>
      </table>
    </div>

    <div class="result" id="yt-result">
      <div class="stem">
        <div class="row">
          <span class="name" id="yt-name"></span>
          <span>
            <button class="btn" id="yt-karaoke" style="display:none">Make karaoke &rarr;</button>
            <button class="btn" id="yt-extract" style="display:none">Extract sound &rarr;</button>
            <a class="btn" id="yt-dl" download>Download</a>
          </span>
        </div>
        <div id="yt-player"></div>
      </div>
    </div>
  </section>

  <!-- Panel 2: sound extraction -->
  <section class="panel">
    <h2>&#127898;&#65039; Sound Extraction</h2>
    <p class="desc">Drop a song &mdash; get the chosen sound isolated, plus the track without it.</p>
    <div id="drop" class="drop">
      <div class="icon">&#127925;</div>
      <p><strong>Drop a song here</strong> or click to choose<br>
      <span style="font-size:.82rem">mp3, wav, flac, m4a &hellip;</span></p>
    </div>
    <input type="file" id="file" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac">
    <div class="setting">
      <label for="model-sel">Model:</label>
      <select id="model-sel">
        <option value="htdemucs" selected>Standard &mdash; 4 stems, best quality</option>
        <option value="htdemucs_6s">Extended &mdash; 6 stems, adds guitar &amp; piano</option>
      </select>
    </div>
    <div class="setting">
      <label for="stem-sel">Sound to extract:</label>
      <select id="stem-sel"></select>
    </div>
    <div class="status" id="sep-status"></div>
    <div class="result" id="sep-result"></div>
  </section>

  <!-- Panel 3: convert & trim -->
  <section class="panel">
    <h2>&#128295; Convert &amp; Trim</h2>
    <p class="desc">Change format or cut a piece out of any audio or video file.</p>
    <div id="cdrop" class="drop">
      <div class="icon">&#9986;&#65039;</div>
      <p><strong>Drop a file here</strong> or click to choose</p>
    </div>
    <input type="file" id="cfile" accept="audio/*,video/*">
    <div class="picked" id="conv-picked"></div>
    <div class="setting">
      <label for="conv-fmt">Format:</label>
      <select id="conv-fmt">
        <option value="mp3-320">mp3 &mdash; 320k</option>
        <option value="mp3-192" selected>mp3 &mdash; 192k</option>
        <option value="mp3-128">mp3 &mdash; 128k</option>
        <option value="wav">wav</option>
        <option value="flac">flac</option>
        <option value="m4a">m4a</option>
      </select>
    </div>
    <div class="setting">
      <label>Trim:</label>
      <input type="text" class="time" id="conv-start" placeholder="start mm:ss (optional)">
      <input type="text" class="time" id="conv-end" placeholder="end mm:ss (optional)">
    </div>
    <button class="go" id="conv-go" style="display:none">Convert</button>
    <div class="status" id="conv-status"></div>
    <div class="result" id="conv-result"></div>
  </section>

</div>

<script>
const $ = id => document.getElementById(id);

/* ---------- Panel 1: YouTube ---------- */
$('yt-go').addEventListener('click', getFormats);
$('yt-url').addEventListener('keydown', e => { if (e.key === 'Enter') getFormats(); });

function fmtMB(bytes) {
  if (!bytes) return '~';
  return (bytes / 1048576).toFixed(2) + 'MB';
}

// Poll a /progress url and feed pct into render() until stopped.
function pollProgress(url, render) {
  const iv = setInterval(async () => {
    try {
      const p = await (await fetch(url)).json();
      if (p.pct != null) render(p.pct);
    } catch (e) {}
  }, 600);
  return () => clearInterval(iv);
}

async function getFormats() {
  const url = $('yt-url').value.trim();
  if (!url) { $('yt-status').textContent = 'Paste a YouTube link first.'; return; }
  $('yt-go').disabled = true;
  $('fmt').style.display = 'none';
  $('yt-result').style.display = 'none';
  $('yt-status').innerHTML = '<span class="spinner"></span>Reading video info&hellip;';
  try {
    const res = await fetch('/yt/info', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'could not read video');
    $('yt-status').textContent = '';
    $('fmt-title').textContent = data.title;
    const rows = $('fmt-rows');
    rows.innerHTML = '';
    const section = label => {
      const tr = document.createElement('tr');
      tr.innerHTML = '<th colspan="3">' + label + '</th>';
      rows.appendChild(tr);
    };
    const row = (labelHtml, size, spec, withKaraoke) => {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + labelHtml + '</td><td class="size">' + fmtMB(size) + '</td><td></td>';
      const b = document.createElement('button');
      b.className = 'dl';
      b.innerHTML = '&#11015; Download';
      b.onclick = () => downloadYt(url, spec, b);
      tr.lastElementChild.appendChild(b);
      if (withKaraoke) {
        const kb = document.createElement('button');
        kb.className = 'dl karaoke';
        kb.innerHTML = '&#127908; Karaoke';
        kb.onclick = () => downloadYt(url, spec, kb, true);
        tr.lastElementChild.appendChild(kb);
      }
      rows.appendChild(tr);
    };
    section('&#127925; Audio');
    for (const a of data.audio)
      row(a.label, a.size, {kind: 'audio', abr: a.abr}, true);
    if (data.video.length) {
      section('&#127909; Video');
      for (const v of data.video)
        row(v.label + ' <span class="badge">mp4 | avc1</span>', v.size, {kind: 'video', height: v.height});
    }
    $('fmt').style.display = 'block';
  } catch (err) {
    $('yt-status').textContent = 'Error: ' + err.message;
  } finally {
    $('yt-go').disabled = false;
  }
}

async function downloadYt(url, spec, btn, toKaraoke) {
  // Open the karaoke window right away (inside the click) so the popup
  // blocker allows it; it gets pointed at the file once the download ends.
  const kw = toKaraoke ? window.open('', 'karaoke', 'width=760,height=950') : null;
  if (kw) kw.document.write('<body style="background:#111318;color:#9aa1b0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:95vh">Downloading the song&hellip;</body>');
  document.querySelectorAll('button.dl').forEach(b => b.disabled = true);
  const old = btn.innerHTML;
  btn.innerHTML = '&hellip;';
  $('yt-result').style.display = 'none';
  $('yt-status').innerHTML = '<span class="spinner"></span>Downloading&hellip;';
  const pid = 'yt' + Date.now();
  const stopPoll = pollProgress('/progress/' + pid, pct =>
    $('yt-status').innerHTML = '<span class="spinner"></span>Downloading&hellip; ' + Math.round(pct) + '%');
  try {
    const res = await fetch('/yt', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({url, pid}, spec))
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'download failed');
    if (kw) kw.location = '/karaoke?file=' + encodeURIComponent(data.file);
    $('yt-status').textContent = 'Done!';
    $('yt-name').textContent = data.title;
    $('yt-dl').href = data.file;
    $('yt-dl').setAttribute('download', data.file.split('/').pop());
    $('yt-player').innerHTML = spec.kind === 'video'
      ? '<video controls src="' + encodeURI(data.file) + '"></video>'
      : '<audio controls src="' + encodeURI(data.file) + '"></audio>';
    const ex = $('yt-extract');
    ex.style.display = spec.kind === 'audio' ? 'inline-block' : 'none';
    ex.onclick = () => separateServerFile(data.file, data.title);
    const ka = $('yt-karaoke');
    ka.style.display = spec.kind === 'audio' ? 'inline-block' : 'none';
    ka.onclick = () => window.open('/karaoke?file=' + encodeURIComponent(data.file),
                                   'karaoke', 'width=760,height=950');
    $('yt-result').style.display = 'block';
  } catch (err) {
    if (kw) kw.close();
    $('yt-status').textContent = 'Error: ' + err.message;
  } finally {
    stopPoll();
    btn.innerHTML = old;
    document.querySelectorAll('button.dl').forEach(b => b.disabled = false);
  }
}

/* ---------- Panel 2: separator ---------- */
const drop = $('drop'), fileInput = $('file');
drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { if (fileInput.files[0]) uploadSong(fileInput.files[0]); });
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hover'); }));
drop.addEventListener('drop', ev => { const f = ev.dataTransfer.files[0]; if (f) uploadSong(f); });

const STEM_NAMES = {
  vocals: '\\u{1F3A4} Vocals', drums: '\\u{1F941} Drums', bass: '\\u{1F3B8} Bass',
  guitar: '\\u{1F3B6} Guitar', piano: '\\u{1F3B9} Piano', other: '\\u{1F3BC} Other'
};
const MODEL_STEMS = {
  htdemucs: ['vocals', 'drums', 'bass', 'other'],
  htdemucs_6s: ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other']
};

function rebuildStemSel() {
  const stems = MODEL_STEMS[$('model-sel').value];
  const sel = $('stem-sel');
  const cur = sel.value;
  sel.innerHTML = '';
  for (const k of stems) {
    const o = document.createElement('option');
    o.value = k;
    o.textContent = STEM_NAMES[k] + (k === 'other' ? ' (synths, strings, ...)' : '');
    sel.appendChild(o);
  }
  const all = document.createElement('option');
  all.value = 'all';
  all.textContent = '\\u2728 All instruments (' + stems.length + ' separate files)';
  sel.appendChild(all);
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : 'vocals';
}
document.addEventListener('DOMContentLoaded', () => {
  $('model-sel').addEventListener('change', rebuildStemSel);
  rebuildStemSel();
});

function sepBusy(name) {
  $('sep-result').style.display = 'none';
  $('sep-status').innerHTML = '<span class="spinner"></span>Separating &ldquo;' + name +
    '&rdquo;&hellip;';
  return pollProgress('/progress/sep', pct =>
    $('sep-status').innerHTML = '<span class="spinner"></span>Separating &ldquo;' + name +
      '&rdquo;&hellip; ' + Math.round(pct) + '%');
}
function sepDone(data) {
  $('sep-status').textContent = 'Done!';
  const items = data.remove === 'all'
    ? ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other']
        .filter(k => data.all[k]).map(k => [STEM_NAMES[k], data.all[k]])
    : [[STEM_NAMES[data.remove] || data.remove, data.target],
       ['\\u{1F3B5} Everything else', data.rest]];
  const box = $('sep-result');
  box.innerHTML = '';
  for (const [label, url] of items) {
    const d = document.createElement('div');
    d.className = 'stem';
    d.innerHTML = '<div class="row"><span class="name"></span>' +
                  '<a class="btn" download>Download</a></div><audio controls></audio>';
    d.querySelector('.name').textContent = label;
    const a = d.querySelector('a');
    a.href = encodeURI(url);
    a.setAttribute('download', url.split('/').pop());
    d.querySelector('audio').src = encodeURI(url);
    box.appendChild(d);
  }
  box.style.display = 'block';
}

async function uploadSong(file) {
  const stopPoll = sepBusy(file.name);
  const fd = new FormData();
  fd.append('audio', file);
  fd.append('remove', $('stem-sel').value);
  fd.append('model', $('model-sel').value);
  try {
    const res = await fetch('/separate', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'separation failed');
    stopPoll();
    sepDone(data);
  } catch (err) {
    stopPoll();
    $('sep-status').textContent = 'Error: ' + err.message;
  } finally {
    fileInput.value = '';
  }
}

async function separateServerFile(fileUrl, title) {
  const stopPoll = sepBusy(title);
  try {
    const res = await fetch('/separate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({server_file: fileUrl, remove: $('stem-sel').value,
                            model: $('model-sel').value})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'separation failed');
    stopPoll();
    sepDone(data);
  } catch (err) {
    stopPoll();
    $('sep-status').textContent = 'Error: ' + err.message;
  }
}

/* ---------- Panel 3: convert & trim ---------- */
const cdrop = $('cdrop'), cfile = $('cfile');
let convFile = null;
cdrop.addEventListener('click', () => cfile.click());
cfile.addEventListener('change', () => { if (cfile.files[0]) cpick(cfile.files[0]); });
['dragenter','dragover'].forEach(e => cdrop.addEventListener(e, ev => { ev.preventDefault(); cdrop.classList.add('hover'); }));
['dragleave','drop'].forEach(e => cdrop.addEventListener(e, ev => { ev.preventDefault(); cdrop.classList.remove('hover'); }));
cdrop.addEventListener('drop', ev => { const f = ev.dataTransfer.files[0]; if (f) cpick(f); });

function cpick(f) {
  convFile = f;
  $('conv-picked').textContent = '\\u266a ' + f.name;
  $('conv-go').style.display = 'block';
  $('conv-result').style.display = 'none';
  $('conv-status').textContent = '';
}

$('conv-go').addEventListener('click', async () => {
  if (!convFile) return;
  $('conv-go').disabled = true;
  $('conv-result').style.display = 'none';
  $('conv-status').innerHTML = '<span class="spinner"></span>Converting&hellip;';
  const fd = new FormData();
  fd.append('audio', convFile);
  fd.append('format', $('conv-fmt').value);
  fd.append('start', $('conv-start').value.trim());
  fd.append('end', $('conv-end').value.trim());
  try {
    const res = await fetch('/convert', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'conversion failed');
    $('conv-status').textContent = 'Done!';
    const box = $('conv-result');
    box.innerHTML = '<div class="stem"><div class="row"><span class="name"></span>' +
      '<a class="btn" download>Download</a></div><audio controls></audio></div>';
    box.querySelector('.name').textContent = data.name;
    const a = box.querySelector('a');
    a.href = encodeURI(data.file);
    a.setAttribute('download', data.name);
    box.querySelector('audio').src = encodeURI(data.file);
    box.style.display = 'block';
  } catch (err) {
    $('conv-status').textContent = 'Error: ' + err.message;
  } finally {
    $('conv-go').disabled = false;
    cfile.value = '';
  }
});
</script>
</body>
</html>"""


DIST = APP_DIR / "frontend" / "dist"


@app.get("/")
def index():
    if (DIST / "index.html").exists():
        return send_from_directory(DIST, "index.html")
    return PAGE  # fallback: the old inline frontend


@app.get("/assets/<path:name>")
def dist_assets(name):
    return send_from_directory(DIST / "assets", name)


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

    # Best stream per available resolution (prefer mp4/avc1), plus ~129k audio.
    audio_extra = duration * 129_000 / 8
    by_height = {}
    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec", "none") == "none":
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

    return jsonify(title=info.get("title", "video"), audio=audio, video=video,
                   thumbnail=info.get("thumbnail"),
                   duration=duration, channel=info.get("channel") or info.get("uploader"))


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


SERVED_DIRS = {"/downloads/": DL_DIR, "/separated/": OUT_DIR, "/converted/": CONV_DIR}


def resolve_served(url_path: str):
    """Map a /downloads|separated|converted/ URL back to a safe local path."""
    for prefix, base in SERVED_DIRS.items():
        if url_path.startswith(prefix):
            p = (base / Path(url_path).name).resolve()
            if p.parent == base.resolve() and p.is_file():
                return p
    return None


@app.post("/convert")
def convert():
    f = request.files.get("audio")
    if f is not None and f.filename:
        form = request.form
        stem = Path(f.filename).stem or "audio"
        suffix = Path(f.filename).suffix or ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            src, cleanup = Path(tmp.name), True
    else:
        form = request.get_json(silent=True) or {}
        src = resolve_served(form.get("server_file", ""))
        if src is None:
            return jsonify(error="no file uploaded"), 400
        stem, cleanup = src.stem, False

    fmt = form.get("format", "mp3-192")
    if fmt not in CONV_FORMATS:
        return jsonify(error=f"unknown format: {fmt}"), 400
    start = (form.get("start") or "").strip()
    end = (form.get("end") or "").strip()
    for t in (start, end):
        if t and not TIME_RE.match(t):
            return jsonify(error=f"bad time '{t}' - use mm:ss"), 400

    ext, codec_args = CONV_FORMATS[fmt]
    try:
        out_name = f"{stem}{'_cut' if (start or end) else ''}.{ext}"
        out_path = CONV_DIR / out_name
        cmd = ["ffmpeg", "-y", "-i", str(src), "-vn"]
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
        if cleanup:
            src.unlink(missing_ok=True)


@app.get("/converted/<path:name>")
def get_converted(name):
    return send_from_directory(CONV_DIR, name)


@app.post("/upload")
def upload():
    """Store an uploaded song server-side so other tools can reference it."""
    f = request.files.get("audio")
    if f is None or not f.filename:
        return jsonify(error="no file uploaded"), 400
    name = Path(f.filename).name
    f.save(DL_DIR / name)
    return jsonify(file=f"/downloads/{name}", name=name)


@app.get("/files")
def list_files():
    exts = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
    files = sorted((p for p in DL_DIR.iterdir() if p.suffix.lower() in exts),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify(files=[{"name": p.name, "file": f"/downloads/{p.name}"} for p in files[:60]])


@app.post("/studio/export")
def studio_export():
    """Mix stem files with per-track gains into one file."""
    data = request.get_json(silent=True) or {}
    tracks = data.get("tracks") or []
    if not tracks:
        return jsonify(error="no tracks"), 400
    cmd, filters, labels = ["ffmpeg", "-y"], [], []
    for i, t in enumerate(tracks):
        p = resolve_served(t.get("file", ""))
        if p is None:
            return jsonify(error=f"file not found: {t.get('file')}"), 404
        cmd += ["-i", str(p)]
        gain = max(0.0, min(3.0, float(t.get("gain", 1.0))))
        filters.append(f"[{i}:a]volume={gain:.3f}[a{i}]")
        labels.append(f"[a{i}]")
    name = re.sub(r"[^\w\s.-]", "", data.get("name") or "mix").strip() or "mix"
    try:
        start = float(data.get("start")) if data.get("start") is not None else None
        end = float(data.get("end")) if data.get("end") is not None else None
    except (TypeError, ValueError):
        return jsonify(error="bad trim range"), 400
    cut = start is not None and end is not None and end > (start or 0)
    out_name = f"{name}_custom_mix{'_cut' if cut else ''}.mp3"
    out_path = CONV_DIR / out_name
    fc = ";".join(filters) + f";{''.join(labels)}amix=inputs={len(tracks)}:normalize=0[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]"]
    if cut:
        cmd += ["-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify(error="ffmpeg failed: " + r.stderr[-300:]), 500
    return jsonify(file=f"/converted/{out_name}", name=out_name)


@app.post("/midi")
def audio_to_midi():
    data = request.get_json(silent=True) or {}
    path = resolve_served(data.get("server_file", ""))
    if path is None:
        return jsonify(error="file not found"), 404
    try:
        import midi_tool
        notes = midi_tool.transcribe(path)
        out_name = f"{path.stem}.mid"
        midi_tool.write_midi(notes, MIDI_DIR / out_name)
        return jsonify(file=f"/midi/{out_name}", notes=notes[:4000], count=len(notes))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/midi/<path:name>")
def get_midi(name):
    return send_from_directory(MIDI_DIR, name, as_attachment=True)


ANALYSIS_CACHE = {}


@app.post("/analyze")
def analyze_track():
    data = request.get_json(silent=True) or {}
    path = resolve_served(data.get("server_file", ""))
    if path is None:
        return jsonify(error="file not found"), 404
    cache_key = (str(path), path.stat().st_mtime)
    if cache_key not in ANALYSIS_CACHE:
        try:
            import analysis
            ANALYSIS_CACHE[cache_key] = analysis.analyze(path)
        except Exception as e:
            return jsonify(error=str(e)), 500
    return jsonify(ANALYSIS_CACHE[cache_key])


@app.get("/separated/<path:name>")
def get_separated(name):
    return send_from_directory(OUT_DIR, name)


@app.get("/downloads/<path:name>")
def get_download(name):
    return send_from_directory(DL_DIR, name)


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5555")).start()
    print("UnMix running at http://localhost:5555")
    app.run(port=5555, threaded=True)
