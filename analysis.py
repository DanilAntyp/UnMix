"""Lightweight local audio analysis: BPM and musical key (Camelot notation).

Pure numpy — no extra ML dependencies:
  BPM: spectral-flux onset envelope -> autocorrelation peak in 60-200 BPM,
       folded into the 70-180 range DJs expect.
  Key: log-magnitude chroma profile correlated against the
       Krumhansl-Schmuckler major/minor key profiles.
"""
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 22050
N_FFT = 2048
HOP = 512

# in-memory cache for expensive per-file analysis, keyed by (fn, path, mtime)
_CACHE = {}


def _cache_key(name, path, *extra):
    p = Path(path)
    return (name, str(p), p.stat().st_mtime) + extra


def _cached(name, path, fn, *extra):
    try:
        key = _cache_key(name, path, *extra)
    except OSError:
        return fn()
    if key not in _CACHE:
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
            capture_output=True, text=True)
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


def madmom_grid(path: Path) -> dict:
    """Neural beat/downbeat tracking (madmom RNN + DBN decoder). Returns real,
    per-beat times instead of a rigid phase+period grid — this is what makes
    transitions land on the actual downbeat, not an estimated one."""
    return _cached("madmom", path, lambda: _madmom_grid_impl(path))


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
    return sections


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
        max_lag = int(0.6 * beat * fps)
        scores, lags = [], []
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                s = float((ea[lag:] * eb[:n - lag]).sum())
            else:
                s = float((ea[:n + lag] * eb[-lag:]).sum())
            scores.append(s)
            lags.append(lag)
        arr = np.array(scores)
        best_i = int(np.argmax(arr))
        spread = arr.max() - arr.min()
        conf = float((arr.max() - np.median(arr)) / spread) if spread > 0 else 0.0
        delta = float(np.clip(lags[best_i] / fps, -0.5 * beat, 0.5 * beat))
        return delta, conf
    except Exception:
        return 0.0, 0.0


def rms_profile(path: Path, max_seconds: int = 600, win: float = 0.4):
    return _cached("rms", path, lambda: _rms_profile_impl(path, max_seconds, win), max_seconds, win)


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
