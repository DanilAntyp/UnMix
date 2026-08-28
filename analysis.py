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
            return 0.0
        ea, eb = ea[:n], eb[:n]
        max_lag = int(0.6 * beat * fps)
        best, best_lag = -1e18, 0
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                s = float((ea[lag:] * eb[:n - lag]).sum())
            else:
                s = float((ea[:n + lag] * eb[-lag:]).sum())
            if s > best:
                best, best_lag = s, lag
        return float(np.clip(best_lag / fps, -0.5 * beat, 0.5 * beat))
    except Exception:
        return 0.0


def rms_profile(path: Path, max_seconds: int = 600, win: float = 0.4):
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
    y = _load_mono(path)
    S = _stft_mag(y)
    result = {"bpm": _detect_bpm(S)}
    key = _detect_key(S)
    if key:
        result.update(key)
    return result
