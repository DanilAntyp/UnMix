#!/usr/bin/env python3
"""Music toolbox web app.

Panel 1: download video or audio from a YouTube link (yt-dlp), with a
         quality/size table like classic downloader sites.
Panel 2: extract vocals / instrumental from a song (Demucs).
Plus: Karaoke Mode in a separate window (see karaoke.py).

Run:  python app.py   ->  http://localhost:5555
"""
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, request, send_from_directory, jsonify

import separator
from separator import get_separator, sep_lock
import karaoke
import dj

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
app.register_blueprint(dj.bp)
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
        # never cache the shell: it names the hashed bundle, so a cached copy
        # keeps serving the previous build's JS after a rebuild
        r = send_from_directory(DIST, "index.html")
        r.headers["Cache-Control"] = "no-store, must-revalidate"
        return r
    return PAGE  # fallback: the old inline frontend


@app.get("/assets/<path:name>")
def dist_assets(name):

    r = send_from_directory(DIST / "assets", name)
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return r


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
        if info.get("entries"):  # ytsearch1: queries wrap the hit in a playlist
            info = info["entries"][0]
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
        path = resolve_served(server_file)
        if path is not None:
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


SERVED_DIRS = {"/downloads/": DL_DIR, "/separated/": OUT_DIR, "/converted/": CONV_DIR,
               "/djmixes/": dj.DJ_DIR}


def resolve_served(url_path: str):
    """Map a /downloads|separated|converted/ URL back to a safe local path.

    Library folders mean these URLs can be several segments deep, so the file
    only has to sit somewhere under its root — not directly in it.
    """
    for prefix, base in SERVED_DIRS.items():
        if url_path.startswith(prefix):
            root = base.resolve()
            p = (root / url_path[len(prefix):].strip("/")).resolve()
            if p.is_file() and root in p.parents:
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
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
    files = sorted((p for p in DL_DIR.rglob("*")
                    if p.suffix.lower() in exts and not p.name.startswith(".")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify(files=[{"name": p.relative_to(DL_DIR).as_posix(),
                           "file": "/downloads/" + p.relative_to(DL_DIR).as_posix()}
                          for p in files[:200]])


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
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        return jsonify(error="ffmpeg failed: " + r.stderr[-300:]), 500
    return jsonify(file=f"/converted/{out_name}", name=out_name)


LIBRARY_DIRS = {
    "downloads": (DL_DIR, "/downloads/"),
    "stems": (OUT_DIR, "/separated/"),
    "converted": (CONV_DIR, "/converted/"),
    "mixes": (dj.DJ_DIR, "/djmixes/"),
    "karaoke": (APP_DIR / "karaoke", "/karaoke/video/"),
    "midi": (MIDI_DIR, "/midi/"),
}
LIBRARY_LABELS = {
    "downloads": "Downloads", "stems": "Stems", "converted": "Converted",
    "mixes": "DJ mixes", "karaoke": "Karaoke", "midi": "MIDI",
}
MEDIA_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".mp4", ".mid"}
LIB_MAX_ENTRIES = 4000  # a whole-tree listing the browser can still hold happily
LIB_MAX_DEPTH = 10      # deep enough for real filing, shallow enough to stay quick


def _lib_owner(url_path: str):
    """(kind, base, prefix) of the root directory a library URL belongs to."""
    for kind, (base, prefix) in LIBRARY_DIRS.items():
        if url_path == prefix.rstrip("/") or url_path.startswith(prefix):
            return kind, base, prefix
    return None


def _lib_path(url_path: str):
    """Map a library URL to a real path, refusing anything outside its root.

    Folders make these URLs multi-segment (/downloads/House/track.mp3), so the
    old "parent must be the root" test is replaced by a containment check that
    survives ../ and symlinks pointing elsewhere.
    """
    owner = _lib_owner(url_path or "")
    if owner is None:
        return None
    _, base, prefix = owner
    root = base.resolve()
    rel = url_path[len(prefix):].strip("/") if url_path.startswith(prefix) else ""
    p = (root / rel).resolve() if rel else root
    if p != root and root not in p.parents:
        return None
    return p


def _lib_file(url_path: str):
    """_lib_path, but only for something that is actually a file."""
    p = _lib_path(url_path)
    return p if p is not None and p.is_file() else None


def _lib_url(p: Path):
    """The library URL for a real path, or None if it sits outside every root."""
    p = p.resolve()
    for base, prefix in LIBRARY_DIRS.values():
        root = base.resolve()
        if p == root:
            return prefix.rstrip("/")
        if root in p.parents:
            return prefix + p.relative_to(root).as_posix()
    return None


def _lib_is_root(p: Path):
    """Top-level folders are the app's own output dirs — never user-editable."""
    return any(p == base.resolve() for base, _ in LIBRARY_DIRS.values())


def _lib_name(raw):
    """A folder or file name the OS and the rest of the app can live with."""
    name = re.sub(r"[/\\\x00]", "", raw or "").strip().strip(".")
    return name[:120] or None


def _lib_unique(target: Path):
    """A free path near `target`, so an import never overwrites what is there."""
    if not target.exists():
        return target
    for n in range(2, 1000):
        alt = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not alt.exists():
            return alt
    return target.with_name(f"{target.stem} ({int(time.time())}){target.suffix}")


def _lib_entry(p: Path, kind: str, url: str):
    st = p.stat()
    ext = p.suffix.lower()
    return {"name": p.name, "kind": kind, "file": url, "dir": url.rsplit("/", 1)[0],
            "size": st.st_size, "mtime": int(st.st_mtime),
            "video": ext == ".mp4", "midi": ext == ".mid"}


@app.get("/library")
def library():
    """The whole media tree in one response: roots, folders, files.

    A few thousand entries is small enough to send at once, and having the tree
    client-side is what lets the browser open folders, search and re-sort
    without another round trip.
    """
    roots, folders, items = [], [], []
    truncated = False
    for kind, (base, prefix) in LIBRARY_DIRS.items():
        root_url = prefix.rstrip("/")
        roots.append({"kind": kind, "label": LIBRARY_LABELS.get(kind, kind),
                      "path": root_url})
        if not base.exists():
            continue
        stack = [(base, root_url, 0)]
        while stack and not truncated:
            d, d_url, depth = stack.pop()
            try:
                children = sorted(d.iterdir(), key=lambda c: c.name.lower())
            except OSError:
                continue
            for c in children:
                if c.name.startswith("."):
                    continue
                if len(items) + len(folders) >= LIB_MAX_ENTRIES:
                    truncated = True
                    break
                c_url = f"{d_url}/{c.name}"
                try:
                    if c.is_dir():
                        if c.is_symlink() or depth >= LIB_MAX_DEPTH:
                            continue
                        folders.append({"name": c.name, "kind": kind, "path": c_url,
                                        "dir": d_url, "mtime": int(c.stat().st_mtime)})
                        stack.append((c, c_url, depth + 1))
                    elif c.suffix.lower() in MEDIA_EXTS:
                        items.append(_lib_entry(c, kind, c_url))
                except OSError:
                    continue
    items.sort(key=lambda x: -x["mtime"])
    return jsonify(roots=roots, folders=folders, items=items, truncated=truncated)


@app.post("/library/folder")
def library_folder():
    """Make a new folder inside an existing library folder."""
    data = request.get_json(silent=True) or {}
    parent = _lib_path(data.get("dir", ""))
    name = _lib_name(data.get("name"))
    if parent is None or not parent.is_dir():
        return jsonify(error="that folder is gone"), 404
    if not name:
        return jsonify(error="give the folder a name"), 400
    dest = parent / name
    if dest.exists():
        return jsonify(error=f"'{name}' already exists here"), 400
    dest.mkdir()
    return jsonify(ok=True, path=_lib_url(dest), name=name)


@app.post("/library/move")
def library_move():
    """Where a drag lands: move files and folders into another folder."""
    data = request.get_json(silent=True) or {}
    dest = _lib_path(data.get("dir", ""))
    if dest is None or not dest.is_dir():
        return jsonify(error="that folder is gone"), 404
    moved, errors = [], []
    for url in data.get("files") or []:
        src = _lib_path(url)
        if src is None or not src.exists():
            errors.append(f"{Path(url).name or url}: not found")
        elif _lib_is_root(src):
            errors.append(f"{src.name}: top-level folders stay put")
        elif src.parent == dest:
            continue  # dropped back where it already lives
        elif src.is_dir() and (src == dest or src in dest.parents):
            errors.append(f"{src.name}: a folder cannot go inside itself")
        elif (dest / src.name).exists():
            errors.append(f"{src.name}: already in that folder")
        else:
            try:
                shutil.move(str(src), str(dest / src.name))
                moved.append(_lib_url(dest / src.name))
            except OSError as e:
                errors.append(f"{src.name}: {e.strerror or e}")
    if errors and not moved:
        return jsonify(error="; ".join(errors[:3])), 400
    return jsonify(ok=True, moved=moved, errors=errors)


@app.post("/library/upload")
def library_upload():
    """Add music from the desktop straight into the folder that is open."""
    dest = _lib_path(request.form.get("dir") or "/downloads")
    if dest is None or not dest.is_dir():
        return jsonify(error="that folder is gone"), 404
    added, errors = [], []
    for f in request.files.getlist("files"):
        name = _lib_name(Path(f.filename or "").name)
        if not name:
            continue
        if Path(name).suffix.lower() not in MEDIA_EXTS:
            errors.append(f"{name}: not an audio or video file this app reads")
            continue
        target = _lib_unique(dest / name)
        f.save(target)
        added.append(_lib_url(target))
    if errors and not added:
        return jsonify(error="; ".join(errors[:3])), 400
    return jsonify(ok=True, added=added, errors=errors)


@app.post("/library/save")
def library_save():
    """Keep a rendered mix, stem or karaoke video as a library source.

    The original stays in the output directory it was rendered into; this puts a
    copy where the decks and playlists look for material, so a finished mix can
    be mixed again.
    """
    data = request.get_json(silent=True) or {}
    src = _lib_file(data.get("file", ""))
    dest = _lib_path(data.get("dir") or "/downloads")
    if src is None:
        return jsonify(error="that file is gone"), 404
    if dest is None or not dest.is_dir():
        return jsonify(error="that folder is gone"), 404
    name = _lib_name(data.get("name")) or src.name
    if Path(name).suffix.lower() != src.suffix.lower():
        name += src.suffix
    if (dest / name).resolve() == src:
        return jsonify(error="it is already saved there"), 400
    target = _lib_unique(dest / name)
    try:
        shutil.copy2(src, target)
    except OSError as e:
        return jsonify(error=f"could not save it: {e.strerror or e}"), 500
    return jsonify(ok=True, file=_lib_url(target), name=target.name)


@app.post("/library/delete")
def library_delete():
    """Remove files and folders. A folder takes everything inside it."""
    data = request.get_json(silent=True) or {}
    targets = data.get("files") or ([data["file"]] if data.get("file") else [])
    removed, errors = 0, []
    for url in targets:
        p = _lib_path(url)
        if p is None or not p.exists():
            errors.append(f"{Path(url).name or url}: not found")
            continue
        if _lib_is_root(p):
            errors.append(f"{p.name}: top-level folders stay put")
            continue
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            removed += 1
        except OSError as e:
            errors.append(f"{p.name}: {e.strerror or e}")
    if errors and not removed:
        return jsonify(error="; ".join(errors[:3])), 404
    return jsonify(ok=True, removed=removed, errors=errors)


@app.post("/library/rename")
def library_rename():
    data = request.get_json(silent=True) or {}
    p = _lib_path(data.get("file") or data.get("path") or "")
    name = _lib_name(data.get("name"))
    if p is None or not p.exists():
        return jsonify(error="file not found"), 404
    if not name:
        return jsonify(error="bad request"), 400
    if p.is_dir():
        dest = p.with_name(name)
    else:
        # callers pass either a bare title (the recognizer does) or the whole
        # filename shown in the browser; the extension is kept either way.
        stem = Path(name).stem if Path(name).suffix.lower() == p.suffix.lower() else name
        dest = p.with_name(stem + p.suffix)
    if dest == p:
        return jsonify(ok=True, file=_lib_url(p), path=_lib_url(p), name=p.name)
    if dest.exists():
        return jsonify(error="a file with that name already exists"), 400
    p.rename(dest)
    return jsonify(ok=True, file=_lib_url(dest), path=_lib_url(dest), name=dest.name)


ACOUSTID_KEYS = ("cSpUJKpD", "v8pQ6oyB")  # tried in order; overridable via acoustid_key.txt


def _acoustid_key():
    cfg = APP_DIR / "acoustid_key.txt"
    if cfg.exists():
        return [cfg.read_text().strip()]
    return list(ACOUSTID_KEYS)


def _recognize_file(p):
    """chromaprint fingerprint -> AcoustID -> {artist, title, score} or None.
    Raises RuntimeError when fingerprinting or every AcoustID key fails."""
    import json as _json
    import urllib.parse
    import urllib.request
    r = subprocess.run(["fpcalc", "-json", str(p)], capture_output=True,
                       text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("fingerprinting failed (is chromaprint installed?)")
    fp = _json.loads(r.stdout)
    last_err = "no AcoustID key worked"
    for key in _acoustid_key():
        body = urllib.parse.urlencode({
            "client": key, "duration": int(fp["duration"]),
            "fingerprint": fp["fingerprint"], "meta": "recordings",
        }).encode()
        try:
            req = urllib.request.Request("https://api.acoustid.org/v2/lookup", data=body)
            resp = _json.load(urllib.request.urlopen(req, timeout=15))
        except Exception as e:
            last_err = str(e)
            continue
        if resp.get("status") != "ok":
            last_err = resp.get("error", {}).get("message", "lookup failed")
            continue
        best = None
        for res in resp.get("results", []):
            for rec in res.get("recordings", []) or []:
                if rec.get("title") and rec.get("artists"):
                    cand = {"score": res.get("score", 0), "title": rec["title"],
                            "artist": rec["artists"][0]["name"]}
                    if best is None or cand["score"] > best["score"]:
                        best = cand
        return best
    raise RuntimeError(f"AcoustID unavailable: {last_err}. Put a free API key "
                       f"from acoustid.org/new-application into acoustid_key.txt")


@app.post("/recognize")
def recognize():
    """Identify a track: chromaprint fingerprint -> AcoustID -> artist/title."""
    data = request.get_json(silent=True) or {}
    p = _lib_file(data.get("file", ""))
    if p is None:
        return jsonify(error="file not found"), 404
    try:
        best = _recognize_file(p)
    except RuntimeError as e:
        msg = str(e)
        return jsonify(error=msg), 500 if "fingerprinting" in msg else 502
    if best is None:
        return jsonify(found=False)
    return jsonify(found=True, artist=best["artist"], title=best["title"],
                   score=round(best["score"], 2),
                   suggested=f"{best['artist']} - {best['title']}")


from webmeta import dz_get as _dz_get, slug as _slug


def _web_suggest_job(job, seed_path):
    """Song picker, web edition: identify the seed track online, then rank
    Deezer's artist-radio tracks (their own 'sounds like this' engine — stays
    inside the genre) as candidates, then rank them on features measured from
    each candidate's own 30s preview audio."""
    import math
    import urllib.parse
    import numpy as np
    import analysis
    import webmeta
    import dj as dj_mod
    try:
        job["stage"] = "Analyzing your track..."
        fa = dict(analysis.analyze(seed_path))
        fa["bpm"] = analysis.accurate_bpm(seed_path)
        job["stage"] = "Identifying the track..."
        meta = webmeta.track_meta(seed_path)
        if meta is None:
            # filename didn't match anything — identify by audio fingerprint
            job["stage"] = "Name lookup failed — listening to the track (AcoustID)..."
            try:
                rec = _recognize_file(seed_path)
            except RuntimeError:
                rec = None
            if rec:
                q = f"{rec['artist']} {rec['title']}"
                res = _dz_get("https://api.deezer.com/search?q="
                              + urllib.parse.quote(q) + "&limit=1").get("data") or []
                if res:
                    meta = {"artist": res[0]["artist"]["name"], "title": res[0]["title"],
                            "artist_id": res[0]["artist"]["id"], "track_id": res[0]["id"]}
        if meta is None:
            raise ValueError("couldn't identify this track online — "
                             "try renaming it to 'Artist - Title'")
        job["seed"] = {"artist": meta["artist"], "title": meta["title"],
                       "bpm": fa.get("bpm"), "camelot": fa.get("camelot")}

        # Deezer's radio for this artist = their similarity engine's picks,
        # genre-consistent by construction (related-artist top hits are not)
        job["stage"] = f"Tuning into {meta['artist']}'s station..."
        pool = _dz_get(f"https://api.deezer.com/artist/{meta['artist_id']}/radio"
                       ).get("data") or []
        if not pool:  # some artists have no radio — fall back to related tops
            job["stage"] = "No station — browsing related artists..."
            related = (_dz_get(f"https://api.deezer.com/artist/{meta['artist_id']}/related?limit=6")
                       .get("data") or [])
            for ra in related:
                pool += (_dz_get(f"https://api.deezer.com/artist/{ra['id']}/top?limit=5")
                         .get("data") or [])

        have = [_slug(p.name) for p in DL_DIR.rglob("*")
                if p.is_file() and not p.name.startswith(".")]

        def in_library(t):
            a, ti = _slug(t["artist"]["name"]), _slug(t["title"])
            return any(a in h and ti in h for h in have)

        seen, cands = {meta.get("track_id")}, []
        for t in pool:
            if t["id"] in seen or in_library(t):
                continue
            seen.add(t["id"])
            cands.append(t)
        cands = cands[:20]

        # Rank web picks on measured audio, not metadata: each candidate's 30s
        # preview is analyzed for real BPM, key and timbre, then scored through
        # the same ranker the library uses.
        siga = analysis.style_signals(seed_path)
        va = analysis.timbre_vec(seed_path)
        seed_fam = webmeta.genre_family((meta or {}).get("genre"))
        feats = {}
        for i, t in enumerate(cands):
            job["stage"] = f"Listening to previews... ({i + 1}/{len(cands)})"
            job["pct"] = round(i / max(1, len(cands)) * 100, 1)
            f = webmeta.preview_features(t["id"], t.get("preview"))
            if f:
                feats[t["id"]] = f

        stats = analysis.timbre_stats(
            [va] + [np.asarray(f["timbre"], dtype=np.float32) for f in feats.values()])
        meta_a = {"family": seed_fam, "genre": (meta or {}).get("genre")}
        out = []
        for i, t in enumerate(cands):
            f = feats.get(t["id"])
            mine = t["artist"]["id"] == meta["artist_id"]
            origin = "same artist" if mine else f"{meta['artist']} radio pick #{i + 1}"
            if f:
                fb = {"bpm": f["bpm"], "camelot": f["camelot"]}
                sigb = {k: f[k] for k in ("onset_density", "low_ratio", "mid_ratio")}
                sim = analysis.timbre_sim_z(
                    va, np.asarray(f["timbre"], dtype=np.float32), stats)
                # radio candidates carry no genre tag of their own; the station
                # is already genre-coherent, so treat them as the seed's family
                scored = dj_mod._pair_score(fa, siga, fb, sigb, sim, meta_a,
                                            {"family": seed_fam, "genre": None})
                if scored is None:
                    continue
                match, reasons, style, _ratio = scored
                reasons = [origin] + [r for r in reasons if "genre" not in r]
            else:  # preview unavailable — rank on radio position alone
                match = max(1, int(round(100 * math.exp(-0.03 * i - 0.6))))
                reasons = [origin, "no preview to analyze"]
                style, fb = None, {}
            out.append({"artist": t["artist"]["name"], "title": t["title"],
                        "match": match, "bpm": (f or {}).get("bpm"),
                        "camelot": (f or {}).get("camelot"),
                        "style": style, "reasons": reasons,
                        "id": t["id"],
                        "preview": t.get("preview") or None,
                        "cover": (t.get("album") or {}).get("cover_small"),
                        "query": f"{t['artist']['name']} {t['title']} official audio"})
        out.sort(key=lambda m: -m["match"])
        job["web_matches"] = out[:12]
        job["analyzed"] = len(feats)
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


@app.post("/dj/suggest/web")
def dj_suggest_web():
    import uuid as _uuid
    data = request.get_json(silent=True) or {}
    p = _lib_file(data.get("file", ""))
    if p is None:
        return jsonify(error="pick a track first"), 400
    job_id = _uuid.uuid4().hex[:12]
    dj.jobs[job_id] = {"stage": "Starting...", "done": False, "error": None}
    threading.Thread(target=_web_suggest_job, args=(dj.jobs[job_id], p),
                     daemon=True).start()
    return jsonify(job=job_id)


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
    # scratch dirs from jobs that were killed or crashed before cleanup ran
    freed = dj.sweep_temp()
    if freed:
        print(f"cleaned up {freed / 1e9:.1f} GB of leftover scratch files")
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5555")).start()
    app.run(port=5555, threaded=True)
