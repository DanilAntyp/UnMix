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


def loop_roll(seg: np.ndarray, sr: int, beat_dur: float,
              total_dur: float = None) -> np.ndarray:
    """Roll into the drop: looped subdivisions that halve into a stutter.
    beat_dur must be the LOCAL beat length (measured from the track's real
    downbeats, not the global average) so every repeat stays on the groove;
    the output is trimmed/padded to exactly total_dur (default 4 beats) so
    the drop after it lands flush on the grid. Full-beat loops fill whatever
    the accelerating 2-beat tail doesn't cover, and a gentle volume build
    makes it read as tension instead of a skipping CD."""
    if total_dur is None:
        total_dur = 4 * beat_dur
    fade = int(0.004 * sr)
    tail = [(0.5, 2), (0.25, 2), (0.125, 4)]  # the accelerating final 2 beats
    lead = max(0, int(round(total_dur / beat_dur)) - 2)
    pattern = ([(1.0, lead)] if lead else []) + tail
    total_reps = sum(r for _, r in pattern)
    pieces, rep_i = [], 0
    for frac, reps in pattern:
        n = max(fade * 2 + 1, int(round(beat_dur * frac * sr)))
        p = seg[:min(n, len(seg))].copy()
        p[:fade] *= np.linspace(0, 1, fade)[:, None]
        p[-fade:] *= np.linspace(1, 0, fade)[:, None]
        for _ in range(reps):
            g = 0.88 + 0.12 * (rep_i / max(1, total_reps - 1))
            pieces.append(p * g)
            rep_i += 1
    out = np.concatenate(pieces)
    n_total = int(round(total_dur * sr))
    if len(out) < n_total:  # sub-beat rounding shortfall only: a breath of air
        out = np.concatenate(
            [out, np.zeros((n_total - len(out), out.shape[1]), np.float32)])
    return out[:n_total]


def riser(sr: int, dur: float, gain: float = 0.35, beat_dur: float = None) -> np.ndarray:
    """Filtered-noise riser: dark filtered sweep that stays quiet until the
    very end, optionally amplitude-gated to half-beats so it breathes with
    the groove instead of reading as static noise."""
    n = int(dur * sr)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n).astype(np.float32)
    # one-pole lowpass whose cutoff rises from ~250 Hz to ~5 kHz — bright
    # enough to cut through a full mix at the top, below the piercing range
    u = np.linspace(0, 1, n)
    cutoff = 250 * (5000 / 250) ** u
    alpha = np.exp(-2 * np.pi * cutoff / sr).astype(np.float32)
    out = np.empty(n, dtype=np.float32)
    prev = 0.0
    for i in range(n):
        prev = alpha[i] * prev + (1 - alpha[i]) * noise[i]
        out[i] = prev  # lowpassed: starts dark, brightens as the cutoff rises
    # second pass -> 12 dB/oct: actually dark, no residual hiss on top
    prev = 0.0
    for i in range(n):
        prev = alpha[i] * prev + (1 - alpha[i]) * out[i]
        out[i] = prev
    # normalize so the swell peaks near `gain` regardless of filter loss
    peak_rms = np.sqrt(np.mean(out[-max(1, n // 8):] ** 2)) + 1e-9
    out *= 1.0 / peak_rms
    env = (u ** 2.0) * gain  # a build you can hear coming, not a jump scare
    if beat_dur:
        # "shh-shh-shh": each half-beat decays, building tension in rhythm
        ph = ((np.arange(n) / sr) % (beat_dur / 2)) / (beat_dur / 2)
        env = env * (0.45 + 0.55 * (1.0 - ph) ** 1.5)
    out *= env
    n_end = min(n, int(0.02 * sr))
    out[-n_end:] *= np.linspace(1, 0, n_end)
    return np.stack([out, out], axis=1)
