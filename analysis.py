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


def _detect_bpm(S: np.ndarray) -> float:
    flux = np.diff(S, axis=0)
    np.maximum(flux, 0, out=flux)
    env = flux.sum(axis=1)
    env -= env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    fps = SR / HOP
    min_lag = max(1, int(fps * 60 / 200))   # 200 BPM
    max_lag = min(len(ac) - 2, int(fps * 60 / 60))  # 60 BPM
    if max_lag <= min_lag:
        return 0.0
    seg = ac[min_lag:max_lag + 1]
    best = int(np.argmax(seg)) + min_lag
    # parabolic interpolation for sub-lag precision
    a, b, c = ac[best - 1], ac[best], ac[best + 1]
    denom = a - 2 * b + c
    shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
    lag = best + float(np.clip(shift, -0.5, 0.5))
    bpm = 60.0 * fps / lag
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return round(bpm, 1)


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
