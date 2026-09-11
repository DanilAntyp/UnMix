"""Karaoke mode: song -> stem removal -> synced-lyrics video on black background.

Pipeline per job (runs in a background thread):
  1. Demucs splits the song into vocals / drums / bass / other.
  2. The stems the user chose to remove are dropped; the rest are mixed.
  3. Whisper transcribes the vocals stem with word-level timestamps.
  4. Pillow renders one frame per lyric state (a word lights up when sung).
  5. ffmpeg stitches the frames (concat demuxer) with the mixed audio into mp4.

(The frames are rendered in Python because Homebrew's ffmpeg 9 ships without
libass/drawtext, so there is no in-ffmpeg text rendering to lean on.)
"""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory

from separator import get_separator, sep_lock

APP_DIR = Path(__file__).parent
KARAOKE_DIR = APP_DIR / "karaoke"
DL_DIR = APP_DIR / "downloads"
KARAOKE_DIR.mkdir(exist_ok=True)

bp = Blueprint("karaoke", __name__)

jobs = {}  # id -> {"stage": str, "done": bool, "error": str|None, "video": str|None}

_whisper = None
_whisper_lock = threading.Lock()

W, H = 1280, 720
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"  # covers most languages
SUNG = (255, 215, 60)      # yellow - already sung
UNSUNG = (255, 255, 255)   # white - not yet sung
NEXT = (150, 155, 168)     # gray - preview of the next line
LEAD = 1.0                 # show a line this long before its first word


def get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            local = APP_DIR / "models" / "large-v3-turbo"
            model = str(local) if (local / "model.bin").exists() else "large-v3-turbo"
            _whisper = WhisperModel(model, device="cpu", compute_type="int8")
        return _whisper


def transcribe_words(vocals_wav: Path, on_progress=None):
    """Whisper -> flat list of (word, start, end) with measured timestamps."""
    total = probe_duration(vocals_wav)
    model = get_whisper()
    # No VAD: it swallows distorted/screamed singing (tested on Nirvana - whole
    # choruses vanished). Hallucinations over silence are handled instead by
    # hallucination_silence_threshold plus a blacklist of Whisper's stock fillers.
    segments, _ = model.transcribe(
        str(vocals_wav), word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False, beam_size=5,
        hallucination_silence_threshold=2.0)
    HALLUCINATIONS = {
        "thank you", "thanks for watching", "thank you for watching", "you",
        "bye", "subscribe", "see you next time", "see you in the next video",
    }
    words = []
    for seg in segments:
        if on_progress and total:
            on_progress(round(min(99.0, seg.end * 100 / total), 1))
        if seg.text.strip().strip(".!?,").lower() in HALLUCINATIONS:
            continue
        for w in seg.words or []:
            if w.word.strip():
                words.append((w.word.strip(), w.start, w.end))
    return words


def transcribe_lines(vocals_wav: Path, on_progress=None):
    """Whisper -> list of lines, each a list of (word, start, end)."""
    lines = []
    MAX_WORDS = 7
    GAP = 1.0  # start a new line after a pause this long
    current = []
    for word, start, end in transcribe_words(vocals_wav, on_progress):
        if current and (len(current) >= MAX_WORDS or start - current[-1][2] > GAP):
            lines.append(current)
            current = []
        current.append((word, start, end))
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------- real lyrics

def clean_title(stem_name: str) -> str:
    """'Nirvana - Smells Like Teen Spirit (Lyrics)_320k [320k]' -> 'Nirvana - Smells Like Teen Spirit'."""
    name = re.sub(r"[\s_]*\[?\d{2,3}k\]?$", "", stem_name)
    name = re.sub(r"[(\[][^)\]]*(lyric|official|video|audio|visuali[sz]er|remaster|hd|4k|mv)[^)\]]*[)\]]",
                  "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -_")


def parse_lrc(lrc: str):
    """LRC text -> chronological [(line_start_time, line_text)]."""
    stamped = []
    for raw in lrc.splitlines():
        times = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        text = re.sub(r"\[[^\]]*\]", "", raw).strip()
        if text:
            for m, s in times:
                stamped.append((int(m) * 60 + float(s), text))
    stamped.sort(key=lambda x: x[0])
    return stamped


PACE = 0.075  # estimated seconds of singing per character


def estimate_line(words, t0, t1):
    """Spread a line's words over [t0, ~] by word length (no measured timing)."""
    est = min(max(0.5, t1 - t0 - 0.1), max(1.2, PACE * sum(len(w) + 1 for w in words)))
    total = sum(len(w) for w in words)
    cur, line = t0, []
    for w in words:
        d = est * len(w) / total
        line.append((w, cur, cur + d))
        cur += d
    return line


def estimate_lines(stamped, duration: float):
    return [estimate_line(text.split(), t, stamped[i + 1][0] if i + 1 < len(stamped) else duration)
            for i, (t, text) in enumerate(stamped)]


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def align_words(stamped, whisper_words, duration: float):
    """Marry LRC text (exact words, exact line times) with Whisper word timestamps.

    Whisper's transcript is matched word-by-word against the LRC words
    (difflib); matched words take Whisper's measured start time, the rest are
    interpolated between matches. Line boundaries stay clamped to LRC times.
    """
    import difflib
    lrc_flat = [w for _, text in stamped for w in text.split()]
    a = [_norm(w) for w in lrc_flat]
    b = [_norm(w) for w, _, _ in whisper_words]
    times = [None] * len(lrc_flat)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            times[blk.a + k] = whisper_words[blk.b + k][1:3]

    lines, pos = [], 0
    for i, (t0, text) in enumerate(stamped):
        words = text.split()
        n = len(words)
        t1 = stamped[i + 1][0] if i + 1 < len(stamped) else duration
        wt = times[pos:pos + n]
        pos += n
        # drop matches that landed far outside this line's window (bad match,
        # e.g. the same chorus words from a different repetition)
        wt = [x if x and t0 - 2.0 <= x[0] <= t1 + 2.0 else None for x in wt]
        anchors = [j for j in range(n) if wt[j]]
        if not anchors:
            lines.append(estimate_line(words, t0, t1))
            continue
        starts = [None] * n
        for j in anchors:
            starts[j] = wt[j][0]
        for j in range(anchors[0] - 1, -1, -1):  # estimate backwards to line start
            starts[j] = max(t0, starts[j + 1] - max(0.15, PACE * len(words[j])))
        for ai in range(len(anchors) - 1):       # interpolate between anchors
            j0, j1 = anchors[ai], anchors[ai + 1]
            span = starts[j1] - starts[j0]
            chars = sum(len(words[k]) for k in range(j0, j1)) or 1
            cur = starts[j0]
            for k in range(j0 + 1, j1):
                cur += span * len(words[k - 1]) / chars
                starts[k] = cur
        for j in range(anchors[-1] + 1, n):      # estimate forwards to line end
            starts[j] = min(t1, starts[j - 1] + max(0.15, PACE * len(words[j - 1])))
        for j in range(1, n):                    # keep strictly increasing
            starts[j] = max(starts[j], starts[j - 1] + 0.01)
        last_end = (wt[n - 1][1] if wt[n - 1] else
                    min(t1, starts[-1] + max(0.3, PACE * len(words[-1]))))
        line = [(w, starts[j], starts[j + 1] if j + 1 < n else max(last_end, starts[j] + 0.1))
                for j, w in enumerate(words)]
        lines.append(line)
    return lines


def fetch_synced_lyrics(stem_name: str, duration: float):
    """Try LRCLIB for real time-synced lyrics. Returns [(time, text)] or None."""
    query = clean_title(stem_name)
    if not query:
        return None
    url = "https://lrclib.net/api/search?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "UnMix/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        results = json.load(r)
    best = None
    for t in results:
        if not t.get("syncedLyrics"):
            continue
        diff = abs((t.get("duration") or 0) - duration)
        if best is None or diff < best[0]:
            best = (diff, t)
    if best is None or best[0] > 10:  # nothing matches this song's length
        return None
    return parse_lrc(best[1]["syncedLyrics"]) or None


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                       timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- frame drawing

def _fit_font(draw, ImageFont, text, size, max_width):
    while size > 20:
        font = ImageFont.truetype(FONT, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return ImageFont.truetype(FONT, 20)


def draw_state(words, sung, next_text):
    """One karaoke frame: `sung` of the line's words highlighted, next line below."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    if words:
        line_text = " ".join(w for w, _, _ in words)
        font = _fit_font(d, ImageFont, line_text, 60, W - 120)
        x = (W - d.textlength(line_text, font=font)) / 2
        y = H / 2 - font.size / 2
        space = d.textlength(" ", font=font)
        for i, (w, _, _) in enumerate(words):
            d.text((x, y), w, font=font, fill=SUNG if i < sung else UNSUNG)
            x += d.textlength(w, font=font) + space
    if next_text:
        font2 = _fit_font(d, ImageFont, next_text, 38, W - 160)
        x2 = (W - d.textlength(next_text, font=font2)) / 2
        d.text((x2, H - 110), next_text, font=font2, fill=NEXT)
    return img


def render_frames(lines, duration, work: Path):
    """Render unique frames + ffconcat playlist with exact durations."""
    frames_dir = work / "frames"
    frames_dir.mkdir()

    # Timeline of (time, line_idx or None, sung_count). None = black screen.
    # Built strictly monotonically: a line can never appear before the previous
    # one is finished, so the screen never jumps back and forth between lines.
    events = [(0.0, None, 0)]
    now = 0.0
    for i, words in enumerate(lines):
        show = max(now, max(0.0, words[0][1] - LEAD))
        events.append((show, i, 0))
        now = show
        for j, (_, ws, _) in enumerate(words):
            now = max(now, ws)
            events.append((now, i, j + 1))
        end = max(now, words[-1][2] + 0.3)
        nxt_show = max(0.0, lines[i + 1][0][1] - LEAD) if i + 1 < len(lines) else duration
        if end < nxt_show:
            events.append((end, None, 0))
            now = end

    rendered = {}  # state -> filename
    playlist = ["ffconcat version 1.0"]
    for k, (t, line_idx, sung) in enumerate(events):
        t_next = events[k + 1][0] if k + 1 < len(events) else duration
        dur = t_next - t
        if dur <= 0.001:
            continue
        state = (line_idx, sung)
        if state not in rendered:
            if line_idx is None:
                img = draw_state([], 0, "")
            else:
                nxt = " ".join(w for w, _, _ in lines[line_idx + 1]) if line_idx + 1 < len(lines) else ""
                img = draw_state(lines[line_idx], sung, nxt)
            name = f"f{len(rendered):04d}.png"
            img.save(frames_dir / name)
            rendered[state] = name
        playlist += [f"file 'frames/{rendered[state]}'", f"duration {dur:.3f}"]
    # concat demuxer wants the last file repeated with no duration
    playlist.append(playlist[-2])
    (work / "list.txt").write_text("\n".join(playlist) + "\n")
    return work / "list.txt"


# ---------------------------------------------------------------- job pipeline

def process_job(job_id: str, audio_path: Path, stem_name: str, remove: set,
                keep_source: bool = False):
    job = jobs[job_id]
    work = None
    try:
        job["stage"] = "Looking up lyrics..."
        lines, lyric_source = None, None
        try:
            stamped = fetch_synced_lyrics(stem_name, probe_duration(audio_path))
        except Exception:
            stamped = None  # no internet / API down -> fall back to transcription

        job["stage"] = "Separating instruments (Demucs)..."
        job["pct"] = None
        from demucs.api import save_audio
        import separator as sep_mod
        with sep_lock:
            sep_mod.progress["pct"] = 0.0
            sep = get_separator()
            _, stems = sep.separate_audio_file(audio_path)

        keep = [v for k, v in stems.items() if k not in remove]
        if not keep:
            raise ValueError("you removed every stem - nothing left to play")
        mix = sum(keep)
        duration = mix.shape[-1] / sep.samplerate

        work = Path(tempfile.mkdtemp(prefix="karaoke_"))
        mix_wav = work / "mix.wav"
        vocals_wav = work / "vocals.wav"
        save_audio(mix, str(mix_wav), samplerate=sep.samplerate)
        save_audio(stems["vocals"], str(vocals_wav), samplerate=sep.samplerate)

        on_pct = lambda p: job.__setitem__("pct", p)
        if stamped:
            # Real lyrics: Whisper still listens to the vocals, and its measured
            # word timestamps are aligned onto the correct LRC text.
            job["stage"] = "Measuring word timing (Whisper)..."
            job["pct"] = None
            try:
                lines = align_words(stamped, transcribe_words(vocals_wav, on_pct), duration)
                lyric_source = "real lyrics + measured word timing"
            except Exception:
                lines = estimate_lines(stamped, duration)
                lyric_source = "real lyrics found online"
        else:
            job["stage"] = "Transcribing lyrics (Whisper)..."
            job["pct"] = None
            lines = transcribe_lines(vocals_wav, on_pct)
            lyric_source = "lyrics transcribed by AI"

        job["stage"] = "Rendering video..."
        job["pct"] = None
        out_name = f"{stem_name}_karaoke.mp4"
        out_path = KARAOKE_DIR / out_name
        if lines:
            playlist = render_frames(lines, duration, work)
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist),
                   "-i", str(mix_wav), "-r", "30"]
        else:
            cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30",
                   "-i", str(mix_wav), "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", str(out_path)]
        # video render is the long pole; still bounded so it cannot hang forever
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + r.stderr[-400:])

        job["video"] = f"/karaoke/video/{out_name}"
        job["stage"] = (f"Done! ({lyric_source})" if lines
                        else "Done (no lyrics detected - video has audio only).")
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True
        if not keep_source:
            audio_path.unlink(missing_ok=True)
        # the finished video lives in KARAOKE_DIR; the scratch dir (stems,
        # frames, wavs) is pure intermediate and would otherwise pile up
        if work:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- routes

@bp.post("/karaoke/start")
def karaoke_start():
    f = request.files.get("audio")
    if f is not None and f.filename:
        remove = set((request.form.get("remove") or "vocals").split(","))
        stem_name = Path(f.filename).stem or "song"
        suffix = Path(f.filename).suffix or ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = Path(tmp.name)
        src, keep = tmp_path, False
    else:
        # A file already on the server, e.g. straight from the YouTube panel.
        data = request.get_json(silent=True) or {}
        server_file = data.get("server_file", "")
        if not server_file.startswith("/downloads/"):
            return jsonify(error="no file uploaded"), 400
        # songs filed into library folders sit deeper than DL_DIR's top level
        root = DL_DIR.resolve()
        path = (root / server_file[len("/downloads/"):].strip("/")).resolve()
        if not path.is_file() or root not in path.parents:
            return jsonify(error="file not found"), 404
        remove = set((data.get("remove") or "vocals").split(","))
        stem_name = path.stem
        src, keep = path, True

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"stage": "Starting...", "done": False, "error": None,
                    "video": None, "pct": None}
    threading.Thread(target=process_job, args=(job_id, src, stem_name, remove, keep),
                     daemon=True).start()
    return jsonify(job=job_id)


@bp.get("/karaoke/status/<job_id>")
def karaoke_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="unknown job"), 404
    out = dict(job)
    if "Separating" in (out.get("stage") or ""):
        import separator as sep_mod
        out["pct"] = sep_mod.progress.get("pct")
    return jsonify(out)


@bp.get("/karaoke/video/<path:name>")
def karaoke_video(name):
    return send_from_directory(KARAOKE_DIR, name)


@bp.get("/karaoke")
def karaoke_page():
    dist_index = APP_DIR / "frontend" / "dist" / "index.html"
    if dist_index.exists():
        # The React app handles /karaoke?file=... itself.
        return send_from_directory(dist_index.parent, "index.html")
    return PAGE  # fallback: the old standalone page


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Karaoke Mode</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: -apple-system, system-ui, sans-serif;
    background: #111318; color: #e8eaf0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .card { width: 100%; max-width: 640px; text-align: center; }
  h1 { font-size: 1.6rem; margin-bottom: 6px; }
  .sub { color: #9aa1b0; margin-bottom: 26px; }
  #drop {
    border: 2px dashed #3a4152; border-radius: 16px; padding: 44px 24px;
    cursor: pointer; transition: border-color .15s, background .15s;
  }
  #drop.hover { border-color: #7c9cff; background: #1a1f2b; }
  #drop .icon { font-size: 2.2rem; margin-bottom: 10px; }
  #drop p { color: #9aa1b0; }
  #drop strong { color: #e8eaf0; }
  input[type=file] { display: none; }
  #picked { margin-top: 14px; color: #c9cfdb; font-size: .95rem; min-height: 1.3em; }
  .opts {
    margin: 20px 0; display: none; text-align: left;
    background: #191d26; border: 1px solid #262c3a; border-radius: 14px; padding: 18px 20px;
  }
  .opts h3 { font-size: .95rem; color: #9aa1b0; font-weight: 500; margin-bottom: 12px; }
  .opts label {
    display: flex; align-items: center; gap: 10px; padding: 7px 0; cursor: pointer; font-size: 1rem;
  }
  .opts input { width: 17px; height: 17px; accent-color: #7c9cff; }
  button.go {
    display: none; width: 100%; padding: 14px; border: none; border-radius: 12px; cursor: pointer;
    background: #7c9cff; color: #10131a; font-weight: 700; font-size: 1.05rem;
  }
  button.go:hover { background: #92adff; }
  button.go:disabled { background: #3a4152; color: #9aa1b0; cursor: default; }
  #status { margin-top: 20px; color: #9aa1b0; min-height: 1.4em; }
  .spinner {
    display: inline-block; width: 15px; height: 15px; vertical-align: -2px;
    border: 2px solid #3a4152; border-top-color: #7c9cff; border-radius: 50%;
    animation: spin .8s linear infinite; margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #result { margin-top: 20px; display: none; }
  video { width: 100%; border-radius: 12px; background: #000; }
  #result a {
    display: inline-block; margin-top: 12px; color: #7c9cff; text-decoration: none;
    border: 1px solid #3a4152; padding: 8px 18px; border-radius: 10px;
  }
  #result a:hover { background: #232a3a; }
  .back { position: fixed; top: 18px; left: 22px; color: #9aa1b0; text-decoration: none; font-size: .9rem; }
  .back:hover { color: #e8eaf0; }
</style>
</head>
<body>
<a class="back" href="/">&larr; UnMix</a>
<div class="card">
  <h1>&#127909; Karaoke Mode</h1>
  <p class="sub">Make a real karaoke video: pick what to mute, lyrics appear in sync.</p>

  <div id="drop">
    <div class="icon">&#127925;</div>
    <p><strong>Drop a song here</strong> or click to choose</p>
  </div>
  <input type="file" id="file" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac">
  <div id="picked"></div>

  <div class="opts" id="opts">
    <h3>Remove from the audio:</h3>
    <label><input type="checkbox" value="vocals" checked> &#127908; Vocals</label>
    <label><input type="checkbox" value="drums"> &#129345; Drums</label>
    <label><input type="checkbox" value="bass"> &#127930; Bass</label>
    <label><input type="checkbox" value="other"> &#127929; Everything else (guitars, synths, ...)</label>
  </div>

  <button class="go" id="go">Create karaoke video</button>
  <div id="status"></div>

  <div id="result">
    <video id="player" controls></video><br>
    <a id="dl" download>Download video</a>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const drop = $('drop'), fileInput = $('file');
let song = null;
let serverFile = null;

// Opened from the YouTube panel with ?file=/downloads/... - no upload needed.
const preload = new URLSearchParams(location.search).get('file');
if (preload && preload.startsWith('/downloads/')) {
  serverFile = preload;
  window.addEventListener('DOMContentLoaded', () => {
    $('picked').textContent = '\\u266a ' + decodeURIComponent(preload.split('/').pop());
    $('opts').style.display = 'block';
    $('go').style.display = 'block';
  });
}

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { if (fileInput.files[0]) pick(fileInput.files[0]); });
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hover'); }));
drop.addEventListener('drop', ev => { const f = ev.dataTransfer.files[0]; if (f) pick(f); });

function pick(f) {
  song = f;
  serverFile = null;
  $('picked').textContent = '\\u266a ' + f.name;
  $('opts').style.display = 'block';
  $('go').style.display = 'block';
  $('result').style.display = 'none';
  $('status').textContent = '';
}

$('go').addEventListener('click', async () => {
  if (!song && !serverFile) return;
  const remove = [...document.querySelectorAll('.opts input:checked')].map(c => c.value);
  if (!remove.length) { $('status').textContent = 'Pick at least one thing to remove.'; return; }
  $('go').disabled = true;
  $('result').style.display = 'none';
  $('status').innerHTML = '<span class="spinner"></span>Starting\\u2026';
  let req;
  if (serverFile) {
    req = { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({server_file: serverFile, remove: remove.join(',')}) };
  } else {
    const fd = new FormData();
    fd.append('audio', song);
    fd.append('remove', remove.join(','));
    req = { method: 'POST', body: fd };
  }
  try {
    const res = await fetch('/karaoke/start', req);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'failed to start');
    poll(data.job);
  } catch (err) {
    $('status').textContent = 'Error: ' + err.message;
    $('go').disabled = false;
  }
});

async function poll(id) {
  try {
    const res = await fetch('/karaoke/status/' + id);
    const j = await res.json();
    if (j.error) throw new Error(j.error);
    if (!j.done) {
      const pct = (j.pct != null) ? ' \\u2014 ' + Math.round(j.pct) + '%' : '';
      $('status').innerHTML = '<span class="spinner"></span>' + j.stage + pct +
        ' <span style="font-size:.85rem">(a few minutes for a full song)</span>';
      setTimeout(() => poll(id), 1500);
      return;
    }
    $('status').textContent = j.stage;
    $('player').src = encodeURI(j.video);
    $('dl').href = encodeURI(j.video);
    $('dl').setAttribute('download', j.video.split('/').pop());
    $('result').style.display = 'block';
    $('go').disabled = false;
  } catch (err) {
    $('status').textContent = 'Error: ' + err.message;
    $('go').disabled = false;
  }
}
</script>
</body>
</html>"""
