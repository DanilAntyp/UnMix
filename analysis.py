"""Lightweight local audio analysis: BPM and musical key (Camelot notation).

Pure numpy — no extra ML dependencies:
  BPM: spectral-flux onset envelope -> autocorrelation peak in 60-200 BPM,
       folded into the 70-180 range DJs expect.
  Key: log-magnitude chroma profile correlated against the
       Krumhansl-Schmuckler major/minor key profiles.
"""
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 22050
N_FFT = 2048
HOP = 512

# in-memory cache for expensive per-file analysis, keyed by (fn, path, mtime)
_CACHE = {}

# ...and a disk mirror of the small, expensive results, so a server restart
# doesn't re-analyze the whole library (that used to cost ~2s per track).
# Big arrays (rms profiles) stay memory-only; they are cheap to recompute.
_DISK_FILE = Path(__file__).parent / "analysis_cache.json"
_PERSIST = {"analyze", "signals", "sections", "madmom", "timbre", "bpm", "mix"}
_CODECS = {"timbre": (lambda v: [round(float(x), 6) for x in v],
                      lambda v: np.asarray(v, dtype=np.float32))}
_disk = None
_disk_lock = threading.Lock()
_disk_dirty = False
_disk_saved = 0.0


def _disk_load():
    global _disk
    if _disk is None:
        try:
            _disk = json.loads(_DISK_FILE.read_text())
        except Exception:
            _disk = {}
    return _disk


def flush_cache(force=False):
    """Write the disk cache out; called after batch work and periodically."""
    global _disk_dirty, _disk_saved
    with _disk_lock:
        if _disk is None or not _disk_dirty:
            return
        if not force and time.time() - _disk_saved < 2.0:
            return
        try:
            tmp = _DISK_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_disk))
            tmp.replace(_DISK_FILE)          # atomic: never a half-written cache
            _disk_dirty, _disk_saved = False, time.time()
        except OSError:
            pass


import atexit
# a debounced save can otherwise discard minutes of analysis if the process
# exits right after computing something
atexit.register(lambda: flush_cache(force=True))


def _cache_key(name, path, *extra):
    p = Path(path)
    return (name, str(p), p.stat().st_mtime) + extra


def _cached(name, path, fn, *extra):
    global _disk_dirty
    try:
        key = _cache_key(name, path, *extra)
    except OSError:
        return fn()
    if key in _CACHE:
        return _CACHE[key]
    if name in _PERSIST:
        dkey = "|".join(str(x) for x in key)
        d = _disk_load()
        if dkey in d:
            dec = _CODECS.get(name, (None, None))[1]
            val = d[dkey]
            val = dec(val) if dec else val
            _CACHE[key] = val
            return val
        val = fn()
        _CACHE[key] = val
        enc = _CODECS.get(name, (None, None))[0]
        with _disk_lock:
            d[dkey] = enc(val) if enc else val
            _disk_dirty = True
        flush_cache()
        return val
    _CACHE[key] = fn()
    return _CACHE[key]


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Camelot wheel: 1B=B major ... counting up a fifth each step; xA is the relative minor.
_CAMELOT_MAJOR = ["B", "F#", "C#", "G#", "D#", "A#", "F", "C", "G", "D", "A", "E"]
_CAMELOT_MINOR = ["G#", "D#", "A#", "F", "C", "G", "D", "A", "E", "B", "F#", "C#"]
CAMELOT = {(n, "major"): f"{i + 1}B" for i, n in enumerate(_CAMELOT_MAJOR)}
CAMELOT.update({(n, "minor"): f"{i + 1}A" for i, n in enumerate(_CAMELOT_MINOR)})


def _load_mono(path: Path, max_seconds: int = 120):
    """Decode any audio via ffmpeg to mono 22.05k, analyzing at most max_seconds."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-t", str(max_seconds),
             "-ac", "1", "-ar", str(SR), str(tmp_path)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("could not decode audio")
        y, _ = sf.read(tmp_path, dtype="float32")
        return np.asarray(y, dtype=np.float32)
    finally:
        tmp_path.unlink(missing_ok=True)


def _stft_mag(y: np.ndarray) -> np.ndarray:
    if len(y) < N_FFT + HOP:
        y = np.pad(y, (0, N_FFT + HOP - len(y)))
    n_frames = 1 + (len(y) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hanning(N_FFT)[None, :].astype(np.float32)
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)


def _onset_env(S: np.ndarray) -> np.ndarray:
    flux = np.diff(S, axis=0)
    np.maximum(flux, 0, out=flux)
    return flux.sum(axis=1)


def _detect_bpm(S: np.ndarray) -> float:
    """Comb-scored tempo estimate: each BPM candidate is supported by its own
    lag AND its multiples, which resists the classic half/double-tempo error."""
    env = _onset_env(S)
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    if len(ac) < 4 or ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    fps = SR / HOP

    def ac_at(lag):
        i = int(round(lag))
        return float(ac[i]) if 1 <= i < len(ac) else 0.0

    best_bpm, best_score = 0.0, -1.0
    for bpm10 in range(600, 2001, 5):          # 60.0 .. 200.0 step 0.5
        bpm = bpm10 / 10
        lag = fps * 60 / bpm
        score = ac_at(lag) + 0.5 * ac_at(2 * lag) + 0.3 * ac_at(4 * lag)
        if bpm < 85:
            score *= 0.82
        elif bpm > 155:
            score *= 0.88
        if score > best_score:
            best_score, best_bpm = score, bpm
    return round(best_bpm, 1)


def beat_grid(path: Path, max_seconds: int = 180) -> dict:
    return _cached("grid", path, lambda: _beat_grid_impl(path, max_seconds), max_seconds)


def _beat_grid_impl(path: Path, max_seconds: int) -> dict:
    """Estimate tempo, beat phase and bar (downbeat) phase of a track.
    Beat phase maximizes onset energy on the beat comb; the downbeat is the
    one of the 4 beat offsets with the most low-frequency (kick/bass) energy."""
    y = _load_mono(path, max_seconds)
    S = _stft_mag(y)
    bpm = _detect_bpm(S)
    fps = SR / HOP
    period = fps * 60 / max(bpm, 1)
    env = _onset_env(S)
    n = len(env)

    def comb(offset, step, e):
        idx = np.arange(offset, n - 1, step).astype(int)
        return float(e[idx].mean()) if len(idx) else 0.0

    offsets = np.linspace(0, period, 48, endpoint=False)
    beat = max(offsets, key=lambda o: comb(o, period, env))

    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    low = np.diff(S[:, freqs < 150], axis=0)
    np.maximum(low, 0, out=low)
    low_env = low.sum(axis=1)
    bar = max((beat + k * period for k in range(4)),
              key=lambda o: comb(o, 4 * period, low_env))

    return {"bpm": bpm, "beat": beat / fps, "bar": bar / fps,
            "beat_len": 60 / bpm, "bar_len": 4 * 60 / bpm}


def _camelot_parse(c: str):
    return int(c[:-1]), c[-1]


def camelot_compatible(a: str, b: str) -> bool:
    """Harmonic mixing rule: same slot, relative major/minor (same number),
    or an adjacent number with the same letter."""
    if not a or not b:
        return True
    na, la = _camelot_parse(a)
    nb, lb = _camelot_parse(b)
    if na == nb:
        return True
    if la == lb and ((na - nb) % 12 == 1 or (nb - na) % 12 == 1):
        return True
    return False


def harmony_plan(cam_a: str, cam_b: str, max_shift: int = 2):
    """(semitone_shift_for_B, clash). Shifting pitch by +1 semitone moves a
    Camelot slot by +7 positions; try small shifts before declaring a clash."""
    if not cam_a or not cam_b or camelot_compatible(cam_a, cam_b):
        return 0, False
    nb, lb = _camelot_parse(cam_b)
    for s in (1, -1, 2, -2):
        if abs(s) > max_shift:
            continue
        nb2 = ((nb - 1 + 7 * s) % 12) + 1
        if camelot_compatible(cam_a, f"{nb2}{lb}"):
            return s, False
    return 0, True


def style_signals(path: Path) -> dict:
    """Cheap per-track character signals for auto style selection:
    onset_density (hits/sec), low_ratio (kick/808 weight), mid_ratio
    (vocal/lead presence proxy)."""
    return _cached("signals", path, lambda: _style_signals_impl(path))


def _style_signals_impl(path: Path) -> dict:
    y = _load_mono(path, max_seconds=120)
    S = _stft_mag(y)
    env = _onset_env(S)
    fps = SR / HOP
    thr = env.mean() + 1.2 * env.std()
    min_dist = int(0.1 * fps)
    peaks, last = 0, -min_dist
    for i in range(1, len(env) - 1):
        if env[i] >= thr and env[i] >= env[i - 1] and env[i] >= env[i + 1] and i - last >= min_dist:
            peaks += 1
            last = i
    seconds = max(1e-6, len(env) / fps)
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    total = float(S.sum()) + 1e-9
    return {
        "onset_density": round(peaks / seconds, 2),
        "low_ratio": round(float(S[:, freqs < 150].sum()) / total, 3),
        "mid_ratio": round(float(S[:, (freqs >= 2000) & (freqs < 6000)].sum()) / total, 3),
    }


def _act_med(r: np.ndarray) -> float:
    a = r[r > r.max() * 0.06] if len(r) and r.max() > 0 else r
    return float(np.median(a)) if len(a) else 0.0


def vocal_activity(path: Path, max_seconds: int = 600, win: float = 0.4):
    """Per-window lead-vocal presence score in [0,1]: (scores, win_seconds).

    When a Demucs vocal stem for this file already exists (the vocal-extract
    feature writes separated/<name>_vocals.wav), the score is that stem's real
    loudness. Otherwise a cheap spectral proxy: the 300-3000 Hz energy share,
    smoothed and z-scored against the track's own loud windows. Calibrated
    against Demucs stems on 12 library tracks: ~0.72 balanced accuracy, i.e.
    good enough to RANK candidate mix placements, never to hard-gate them.
    """
    return _cached("vocal", path,
                   lambda: _vocal_activity_impl(path, max_seconds, win),
                   max_seconds, win)


def _vocal_activity_impl(path: Path, max_seconds: int, win: float):
    w = int(SR * win)
    stem = _DISK_FILE.parent / "separated" / f"{Path(path).stem}_vocals.wav"
    if stem.is_file():
        try:
            yv = _load_mono(stem, max_seconds)
            n = len(yv) // w
            if n:
                rv = np.sqrt((yv[:n * w].reshape(n, w) ** 2).mean(axis=1))
                med = _act_med(rv)
                if med > 1e-6:
                    return np.clip(rv / (0.7 * med), 0.0, 1.0).astype(np.float32), win
        except Exception:
            pass
    y = _load_mono(path, max_seconds)
    n = len(y) // w
    if n < 4:
        return np.zeros(max(n, 1), dtype=np.float32), win
    frames = y[:n * w].reshape(n, w)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    mag = np.abs(np.fft.rfft(frames * np.hanning(w).astype(np.float32), axis=1))
    freqs = np.fft.rfftfreq(w, 1 / SR)
    ratio = (mag[:, (freqs >= 300) & (freqs < 3000)].sum(axis=1)
             / (mag.sum(axis=1) + 1e-9))
    k = max(1, int(round(1.2 / win)))  # vocals sustain; cymbal spikes don't
    ratio = np.convolve(ratio, np.ones(k) / k, mode="same")
    med = _act_med(rms)
    loud = rms >= 0.3 * med
    base = ratio[loud] if loud.sum() >= 8 else ratio
    mu, sd = float(base.mean()), float(base.std()) + 1e-9
    score = 1.0 / (1.0 + np.exp(-((ratio - mu) / sd + 0.6) / 0.5))
    score[rms < 0.25 * med] = 0.0  # nothing audible -> nothing to clash with
    return score.astype(np.float32), win


def accurate_bpm(path: Path) -> float:
    """BPM we can actually rank on.

    The cheap autocorrelation estimator agrees with madmom's neural tracker on
    ~11 of 12 tracks, so paying 12s/track for madmom everywhere is waste. But
    when the cheap estimator and librosa's beat tracker disagree, that IS the
    unreliable case (measured: it flagged exactly the track the cheap estimator
    got wrong), and only then is madmom worth its cost.
    """
    return _cached("bpm", path, lambda: _accurate_bpm_impl(path))


def _fold(x, lo=78.0, hi=155.0):
    if x <= 0:
        return 0.0
    while x > hi:
        x /= 2
    while x < lo:
        x *= 2
    return x


def _accurate_bpm_impl(path: Path) -> float:
    fast = analyze(path).get("bpm") or 0.0
    try:
        import librosa
        y, sr = librosa.load(str(path), sr=SR, duration=120, mono=True)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        lib = float(np.atleast_1d(tempo)[0])
    except Exception:
        return fast
    if fast and abs(_fold(fast) - _fold(lib)) < 2.5:
        return fast                       # two independent methods agree
    try:                                  # they disagree -> ask the neural one
        return madmom_grid(path).get("bpm") or fast
    except Exception:
        return fast


def timbre_vec(path: Path) -> np.ndarray:
    """Timbre fingerprint: MFCCs (spectral envelope — the 'colour' of the
    sound), their frame-to-frame deltas (how much it moves), and spectral
    contrast (peak-vs-valley per band, which separates tonal music from dense
    walls of sound). Mean+std of each, L2-normalized.

    Replaces a plain 26-band energy profile that scored rock at 0.950 against
    an EDM seed while real EDM scored 0.945-0.972 — i.e. it was measuring
    mastering EQ, not music, and could not tell the genres apart."""
    return _cached("timbre", path, lambda: _timbre_vec_impl(path))


def _timbre_vec_impl(path: Path) -> np.ndarray:
    try:
        import librosa
        y, sr = librosa.load(str(path), sr=SR, duration=120, mono=True)
        if not len(y):
            raise ValueError("empty audio")
        S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
        mf = librosa.feature.mfcc(S=librosa.power_to_db(S ** 2), sr=sr, n_mfcc=20)
        mf = mf[1:]                        # drop MFCC0: that is loudness
        d = np.diff(mf, axis=1) if mf.shape[1] > 1 else np.zeros_like(mf)
        sc = librosa.feature.spectral_contrast(S=S, sr=sr)
        parts = [mf.mean(1), mf.std(1), np.abs(d).mean(1), sc.mean(1), sc.std(1)]
    except Exception:
        return _timbre_bands_fallback(path)
    v = np.concatenate(parts).astype(np.float32)
    v = np.nan_to_num(v)
    # standardize blocks so MFCCs (large range) don't drown spectral contrast
    out = []
    for p in np.split(v, np.cumsum([len(x) for x in parts])[:-1]):
        s = p.std()
        out.append((p - p.mean()) / s if s > 1e-6 else p * 0)
    v = np.concatenate(out)
    n = float(np.linalg.norm(v))
    return (v / n if n > 0 else v).astype(np.float32)


def _timbre_bands_fallback(path: Path) -> np.ndarray:
    """Log-band energy profile, used only if librosa is unavailable."""
    y = _load_mono(path, max_seconds=120)
    S = _stft_mag(y)
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    edges = np.geomspace(40, 10000, 27)
    bands = []
    for e0, e1 in zip(edges[:-1], edges[1:]):
        m = (freqs >= e0) & (freqs < e1)
        bands.append(np.log1p(S[:, m].mean(axis=1)) if m.any()
                     else np.zeros(S.shape[0], dtype=np.float32))
    B = np.stack(bands, axis=1)
    mu = B.mean(axis=0)
    mu -= mu.mean()
    v = np.concatenate([mu, B.std(axis=0)])
    n = float(np.linalg.norm(v))
    return (v / n if n > 0 else v).astype(np.float32)


def timbre_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def timbre_stats(vecs):
    """Per-dimension mean/std over a set of tracks.

    Raw timbre vectors share a large common component, so plain cosine
    similarity saturates (everything scored 0.89-0.97 and rap outranked EDM
    for an EDM seed). Removing the corpus mean leaves only what makes a track
    distinctive, which quadrupled genre separation in testing."""
    if not len(vecs):
        return None
    M = np.stack(list(vecs))
    return M.mean(0), M.std(0) + 1e-6


def timbre_sim_z(a: np.ndarray, b: np.ndarray, stats) -> float:
    """Cosine similarity in corpus-standardized space; ~0.3+ is a close match,
    negative means actively unalike (plain timbre_sim has no useful zero)."""
    if stats is None:
        return timbre_sim(a, b)
    mu, sd = stats
    a, b = (a - mu) / sd, (b - mu) / sd
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / n) if n > 0 else 0.0


def mix_profile(path: Path) -> dict:
    """How easy this track is to mix into or out of — the thing a DJ actually
    cares about and that BPM/key cannot express.

    intro_len: seconds before it first hits full energy (a long, quiet intro
    is room to blend under). cold_open: it starts at full energy, so there is
    nothing to mix into. outro_len: trailing low-energy run to mix out of."""
    return _cached("mix", path, lambda: _mix_profile_impl(path))


def _mix_profile_impl(path: Path) -> dict:
    try:
        secs = detect_sections(path)
    except Exception:
        secs = []
    if not secs:
        return {"intro_len": 0.0, "outro_len": 0.0, "cold_open": False, "known": False}
    peak = max(s["energy"] for s in secs) or 1.0
    hot = 0.75 * peak
    intro = 0.0
    for s in secs:
        if s["energy"] >= hot:
            intro = s["start"]
            break
    else:
        intro = secs[-1]["start"]
    outro = 0.0
    for s in reversed(secs):
        if s["energy"] >= hot:
            break
        outro = secs[-1]["end"] - s["start"]
    return {"intro_len": round(float(intro), 1),
            "outro_len": round(float(outro), 1),
            # no runway at all: the track is at full tilt from the first bar
            "cold_open": bool(intro < 4.0),
            "known": True}


def madmom_grid(path: Path) -> dict:
    """Neural beat/downbeat tracking (madmom RNN + DBN decoder). Returns real,
    per-beat times instead of a rigid phase+period grid — this is what makes
    transitions land on the actual downbeat, not an estimated one."""
    from mix_timing import normalize
    return normalize(_cached("madmom", path, lambda: _madmom_grid_impl(path)))


def _madmom_grid_impl(path: Path) -> dict:
    from madmom.features.downbeats import RNNDownBeatProcessor, DBNDownBeatTrackingProcessor
    act = RNNDownBeatProcessor()(str(path))
    beats = DBNDownBeatTrackingProcessor(beats_per_bar=[4], fps=100)(act)
    times = beats[:, 0]
    downbeats = times[beats[:, 1] == 1]
    ibi = np.diff(times)
    bpm = round(60.0 / float(np.median(ibi)), 1) if len(ibi) else 0.0
    if len(downbeats) > 2:
        bar_len = float(np.median(np.diff(downbeats)))
    else:
        bar_len = 4 * 60 / max(bpm, 1)
    return {"bpm": bpm, "beat_len": 60 / max(bpm, 1), "bar_len": bar_len,
            "downbeats": [round(float(t), 3) for t in downbeats],
            "bar": float(downbeats[0]) if len(downbeats) else 0.0}


def detect_sections(path: Path, max_seconds: int = 600):
    return _cached("sections", path, lambda: _detect_sections_impl(path, max_seconds), max_seconds)


def _detect_sections_impl(path: Path, max_seconds: int):
    """Structural segmentation (Foote novelty on a self-similarity matrix).

    Returns chronological sections [{start, end, energy}] where energy is
    relative loudness (~1.0 = the loudest parts of the song, e.g. chorus/drop).
    """
    y = _load_mono(path, max_seconds)
    S = _stft_mag(y)
    fps = SR / HOP
    step = max(1, int(0.5 * fps))            # ~0.5s feature frames
    n_steps = S.shape[0] // step
    if n_steps < 24:
        return []

    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    edges = np.geomspace(60, 8000, 25)
    band_masks = [(freqs >= e0) & (freqs < e1) for e0, e1 in zip(edges[:-1], edges[1:])]
    feats = np.zeros((n_steps, len(band_masks)))
    rms_steps = np.zeros(n_steps)
    for i in range(n_steps):
        chunk = S[i * step:(i + 1) * step]
        spec = chunk.mean(axis=0)
        for j, m in enumerate(band_masks):
            feats[i, j] = np.log1p(spec[m].mean()) if m.any() else 0.0
        rms_steps[i] = np.sqrt((chunk ** 2).mean())

    feats -= feats.mean(axis=0)
    feats /= np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-9)
    ssm = feats @ feats.T

    L = 16  # 8s checkerboard half-window
    g = np.exp(-0.5 * (np.linspace(-1, 1, 2 * L) ** 2) / 0.25)
    kern = np.outer(g, g)
    sign = np.ones((2 * L, 2 * L))
    sign[:L, L:] = -1
    sign[L:, :L] = -1
    kern *= sign
    nov = np.zeros(n_steps)
    for t in range(L, n_steps - L):
        nov[t] = (ssm[t - L:t + L, t - L:t + L] * kern).sum()

    thr = nov.mean() + 0.4 * nov.std()
    bounds, last = [0.0], -999
    for t in range(L, n_steps - L):
        if nov[t] >= thr and nov[t] == nov[max(0, t - 8):t + 9].max() and t - last >= 8:
            bounds.append(t * step / fps)
            last = t
    bounds.append(n_steps * step / fps)

    peak = np.percentile(rms_steps, 95) + 1e-9
    sections = []
    for s0, s1 in zip(bounds[:-1], bounds[1:]):
        i0 = int(s0 * fps / step)
        i1 = max(i0 + 1, int(s1 * fps / step))
        energy = float(rms_steps[i0:i1].mean() / peak)
        sections.append({"start": round(s0, 2), "end": round(s1, 2),
                         "energy": round(min(energy, 1.0), 3)})
    _label_sections(sections)
    return sections


def _label_sections(secs):
    """Heuristic functional labels from energy + position: the top-energy
    cluster is 'chorus', quiet edges are intro/outro, quiet middles 'break'."""
    if not secs:
        return
    n = len(secs)
    e_hi = max(s["energy"] for s in secs)
    for i, s in enumerate(secs):
        if i == 0 and s["energy"] < 0.55:
            s["label"] = "intro"
        elif i == n - 1 and s["energy"] < 0.55:
            s["label"] = "outro"
        elif 0 < i < n - 1 and s["energy"] <= 0.5:
            s["label"] = "break"
        elif s["energy"] >= e_hi - 0.08:
            s["label"] = "chorus"
        else:
            s["label"] = "verse"


def align_beats(a_path: Path, a_start: float, b_path: Path, b_start: float,
                dur: float, bpm: float) -> float:
    """Micro-align B's beats to A's inside the overlap: cross-correlate the two
    onset envelopes and return the extra delay (seconds, within ±half a beat)
    to add to B so its transients land on A's."""
    try:
        beat = 60.0 / max(bpm, 1)
        fps = SR / HOP

        def seg_env(path, start):
            y = _load_mono(path, max_seconds=int(start + dur + 2))
            y = y[int(start * SR):int((start + dur) * SR)]
            e = _onset_env(_stft_mag(y))
            return e - e.mean()

        ea, eb = seg_env(a_path, a_start), seg_env(b_path, b_start)
        n = min(len(ea), len(eb))
        if n < 16:
            return 0.0, 0.0
        ea, eb = ea[:n], eb[:n]
        # A nudge cannot repair a wrong beat or sustained tempo drift. Compare
        # normalized correlation at the beginning AND end of the overlap.
        max_lag = max(1, int(0.20 * beat * fps))

        def match(a, b):
            scores = []
            lags = range(-max_lag, max_lag + 1)
            for lag in lags:
                x, y = (a[lag:], b[:len(b) - lag]) if lag >= 0 else (a[:len(a) + lag], b[-lag:])
                denom = float(np.linalg.norm(x) * np.linalg.norm(y))
                scores.append(float(np.dot(x, y)) / denom if denom > 1e-9 else 0.0)
            best = int(np.argmax(scores))
            return (best - max_lag) / fps, max(0.0, scores[best])

        delta, corr = match(ea, eb)
        half = n // 2
        early, ce = match(ea[:half], eb[:half])
        late, cl = match(ea[half:], eb[half:])
        drift = abs(early - late)
        stability = max(0.0, 1.0 - drift / max(0.08, 0.2 * beat))
        return float(delta), float(min(corr, ce, cl) * stability)
    except Exception:
        return 0.0, 0.0


def rms_profile(path: Path, max_seconds: int = 600, win: float = 0.4):
    return _cached("rms", path, lambda: _rms_profile_impl(path, max_seconds, win), max_seconds, win)


def energy_profile(path: Path, max_seconds=600, win=0.4):
    """Local musical intensity: percussion, bass and level, independent of gain.

    A mastered breakdown can be nearly as loud as a chorus. Onset activity
    and low-frequency power distinguish that change in arrangement.
    """
    return _cached("energy", path, lambda: _energy_profile_impl(path, max_seconds, win), max_seconds, win)


def _energy_profile_impl(path, max_seconds, win):
    y = _load_mono(path, max_seconds)
    S = _stft_mag(y)
    flux = np.r_[0.0, _onset_env(S)]
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    bass = np.sqrt(np.mean(S[:, (freqs >= 40) & (freqs < 200)] ** 2, axis=1))
    level = np.sqrt(np.mean(S ** 2, axis=1))
    n = max(1, int(len(y) / SR / win))
    features = np.zeros((n, 3))
    for i in range(n):
        lo, hi = int(i * win * SR / HOP), max(1, int((i + 1) * win * SR / HOP))
        for j, curve in enumerate((flux, bass, level)):
            seg = curve[lo:hi]
            features[i, j] = seg.mean() if len(seg) else 0
    scale = np.maximum(np.percentile(features, 85, axis=0), 1e-6)
    normalized = np.clip(features / scale, 0, 1.25)
    return (normalized @ np.array([0.45, 0.35, 0.20])).astype(np.float32), win


def _rms_profile_impl(path: Path, max_seconds: int, win: float):
    """Coarse loudness envelope: (rms_per_window, window_seconds)."""
    y = _load_mono(path, max_seconds)
    w = int(SR * win)
    n = len(y) // w
    if n == 0:
        return np.zeros(1), win
    r = np.sqrt((y[:n * w].reshape(n, w) ** 2).mean(axis=1))
    return r, win


def _detect_key(S: np.ndarray):
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    mask = (freqs >= 65) & (freqs <= 1200)
    midi = 69 + 12 * np.log2(freqs[mask] / 440.0)
    pitch_class = np.round(midi).astype(int) % 12
    mag = np.log1p(S[:, mask]).mean(axis=0)
    # mean per class, not sum: linear FFT bins give unequal bin counts per
    # pitch class, which otherwise biases the tonic estimate
    chroma = np.array([mag[pitch_class == k].mean() if (pitch_class == k).any() else 0.0
                       for k in range(12)])
    if chroma.std() == 0:
        return None
    best = None
    for tonic in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            score = np.corrcoef(np.roll(profile, tonic), chroma)[0, 1]
            if best is None or score > best[0]:
                best = (score, tonic, mode)
    _, tonic, mode = best
    name = NOTE_NAMES[tonic]
    return {"key": f"{name} {mode}", "camelot": CAMELOT[(name, mode)]}


def snap_to_beat(path: Path, target_sec: float, window: float = 1.5) -> float:
    """Snap a time to the strongest onset near it (approximate beat alignment)."""
    try:
        y = _load_mono(path, max_seconds=int(target_sec + window + 2))
        S = _stft_mag(y)
        flux = np.diff(S, axis=0)
        np.maximum(flux, 0, out=flux)
        env = flux.sum(axis=1)
        fps = SR / HOP
        lo = max(0, int((target_sec - window) * fps))
        hi = min(len(env) - 1, int((target_sec + window) * fps))
        if hi <= lo:
            return target_sec
        return (lo + int(np.argmax(env[lo:hi]))) / fps
    except Exception:
        return target_sec


def analyze(path: Path) -> dict:
    return _cached("analyze", path, lambda: _analyze_impl(path))


def _analyze_impl(path: Path) -> dict:
    y = _load_mono(path)
    S = _stft_mag(y)
    result = {"bpm": _detect_bpm(S)}
    key = _detect_key(S)
    if key:
        result.update(key)
    return result
