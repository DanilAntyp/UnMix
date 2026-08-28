"""DSP effects for DJ transitions that ffmpeg can't express: variable-speed
playback (tape stop, backspin), loop rolls and noise risers. Pure numpy."""
import numpy as np
import soundfile as sf


def load(path):
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    return y, sr


def save(path, y, sr):
    sf.write(str(path), y.astype(np.float32), sr)


def _var_speed(seg: np.ndarray, sr: int, out_dur: float, speed_fn, reverse=False):
    """Resample seg with a time-varying speed. speed_fn(u) for u in [0,1]."""
    n_out = int(out_dur * sr)
    u = np.linspace(0, 1, n_out)
    speed = speed_fn(u)
    pos = np.cumsum(speed) / sr
    if reverse:
        pos = (len(seg) - 1) / sr - pos
    idx = pos * sr
    idx = np.clip(idx, 0, len(seg) - 2)
    i0 = idx.astype(int)
    frac = (idx - i0)[:, None]
    out = seg[i0] * (1 - frac) + seg[i0 + 1] * frac
    return out


def tape_stop(seg: np.ndarray, sr: int, dur: float) -> np.ndarray:
    """Turntable power-off: speed (and pitch) slide from 1 to 0."""
    out = _var_speed(seg, sr, dur, lambda u: (1 - u) ** 1.6)
    # short fade at the very end so it dies cleanly
    n = min(len(out), int(0.03 * sr))
    if n:
        out[-n:] *= np.linspace(1, 0, n)[:, None]
    return out


def backspin(seg: np.ndarray, sr: int, dur: float) -> np.ndarray:
    """Rewind: playback runs backwards, accelerating like a spun platter."""
    out = _var_speed(seg, sr, dur, lambda u: 1 + 7 * u ** 2, reverse=True)
    n = min(len(out), int(0.05 * sr))
    if n:
        out[-n:] *= np.linspace(1, 0, n)[:, None]
        out[:n] *= np.linspace(0, 1, n)[:, None]
    return out


def loop_roll(seg: np.ndarray, sr: int, beat_dur: float) -> np.ndarray:
    """Classic 2-bar roll into the drop: a 1-beat loop repeated, then halving
    subdivisions each repeated to fill their slot — 1 beat x4, 1/2 x4,
    1/4 x4, 1/8 x8 (accelerating stutter, total 8 beats)."""
    pieces = []
    fade = int(0.004 * sr)
    pattern = [(1.0, 4), (0.5, 4), (0.25, 4), (0.125, 8)]
    for frac, reps in pattern:
        n = max(fade * 2 + 1, int(beat_dur * frac * sr))
        p = seg[:min(n, len(seg))].copy()
        p[:fade] *= np.linspace(0, 1, fade)[:, None]
        p[-fade:] *= np.linspace(1, 0, fade)[:, None]
        for _ in range(reps):
            pieces.append(p)
    return np.concatenate(pieces)


def riser(sr: int, dur: float, gain: float = 0.35) -> np.ndarray:
    """White-noise riser: amplitude swells, brightness opens up over time."""
    n = int(dur * sr)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n).astype(np.float32)
    # one-pole lowpass whose cutoff rises from ~400 Hz to ~9 kHz
    u = np.linspace(0, 1, n)
    cutoff = 400 * (9000 / 400) ** u
    alpha = np.exp(-2 * np.pi * cutoff / sr).astype(np.float32)
    out = np.empty(n, dtype=np.float32)
    prev = 0.0
    for i in range(n):
        prev = alpha[i] * prev + (1 - alpha[i]) * noise[i]
        out[i] = noise[i] - prev  # highpassed part sounds airier
    env = (u ** 2.2) * gain
    out *= env
    n_end = min(n, int(0.02 * sr))
    out[-n_end:] *= np.linspace(1, 0, n_end)
    return np.stack([out, out], axis=1)
