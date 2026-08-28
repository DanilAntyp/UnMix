"""Audio -> MIDI transcription (monophonic, pure numpy).

YIN pitch tracking over the audio (best on separated stems - bass and lead
vocals are monophonic), segmented into notes, written as a standard MIDI file
by a small built-in encoder. No ML dependencies.
"""
import struct
from pathlib import Path

import numpy as np

from analysis import _load_mono

SR = 22050
FRAME = 2048
HOP = 256
FMIN, FMAX = 55.0, 1200.0   # A1 .. ~D6
YIN_THRESHOLD = 0.15
MIN_NOTE_SEC = 0.09


def _yin_track(y: np.ndarray):
    """Frame-wise YIN f0 estimate; returns (f0_hz, voiced, rms) per frame."""
    tau_min = int(SR / FMAX)
    tau_max = int(SR / FMIN)
    n_frames = max(0, 1 + (len(y) - FRAME) // HOP)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx]
    rms = np.sqrt((frames ** 2).mean(axis=1))

    # difference function via autocorrelation (FFT, all frames at once)
    n_fft = 2 * FRAME
    f = np.fft.rfft(frames, n_fft, axis=1)
    ac = np.fft.irfft(f * np.conj(f), n_fft, axis=1)[:, :tau_max + 1]
    cum = np.cumsum(frames ** 2, axis=1)
    energy0 = cum[:, -1]
    # energy of the shifted window: sum(y[tau : tau+FRAME]^2) approx by full-frame energy
    diff = energy0[:, None] + energy0[:, None] - 2 * ac  # d(tau), rough but effective
    diff[:, 0] = 0
    # cumulative mean normalized difference
    csum = np.cumsum(diff[:, 1:], axis=1)
    taus = np.arange(1, tau_max + 1)
    cmnd = diff[:, 1:] * taus[None, :] / np.maximum(csum, 1e-9)

    f0 = np.zeros(n_frames)
    voiced = np.zeros(n_frames, dtype=bool)
    for i in range(n_frames):
        row = cmnd[i]
        below = np.where(row[tau_min - 1:] < YIN_THRESHOLD)[0]
        if len(below) == 0:
            continue
        tau = below[0] + tau_min
        # walk down to the local minimum
        while tau + 1 <= tau_max and row[tau] < row[tau - 1]:
            tau += 1
        # parabolic interpolation
        t = tau - 1  # index into row
        if 0 < t < len(row) - 1:
            a, b, c = row[t - 1], row[t], row[t + 1]
            denom = a - 2 * b + c
            shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
            tau = tau + float(np.clip(shift, -0.5, 0.5))
        f0[i] = SR / tau
        voiced[i] = True
    return f0, voiced, rms


def _median3(x):
    if len(x) < 3:
        return x
    out = x.copy()
    out[1:-1] = np.median(np.stack([x[:-2], x[1:-1], x[2:]]), axis=0)
    return out


def transcribe(path: Path):
    """Returns a chronological list of notes: {start, end, pitch} (seconds, MIDI number)."""
    y = _load_mono(path, max_seconds=240)
    f0, voiced, rms = _yin_track(y)
    if not voiced.any():
        return []
    gate = rms > max(0.01, np.percentile(rms[voiced], 20) * 0.5)
    voiced &= gate
    midi = np.zeros_like(f0)
    midi[voiced] = 69 + 12 * np.log2(f0[voiced] / 440.0)
    midi = _median3(midi)
    pitch = np.round(midi).astype(int)

    fps = SR / HOP
    notes = []
    cur = None  # [pitch, start_frame, last_frame]
    for i in range(len(pitch)):
        if voiced[i] and 24 <= pitch[i] <= 96:
            if cur is not None and pitch[i] == cur[0]:
                cur[2] = i
            else:
                if cur is not None:
                    notes.append(cur)
                cur = [int(pitch[i]), i, i]
        else:
            if cur is not None:
                notes.append(cur)
                cur = None
    if cur is not None:
        notes.append(cur)

    out = []
    for p, s, e in notes:
        start, end = s / fps, (e + 1) / fps
        if end - start >= MIN_NOTE_SEC:
            out.append({"start": round(start, 3), "end": round(end, 3), "pitch": p})
    return out


# ---------------------------------------------------------------- MIDI writer

def _varlen(n: int) -> bytes:
    buf = [n & 0x7F]
    n >>= 7
    while n:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(buf))


def write_midi(notes, path: Path, tpq: int = 480):
    """Minimal single-track MIDI file. Tempo fixed at 120 BPM => 1s = 2*tpq ticks."""
    events = []  # (tick, priority, bytes)
    for n in notes:
        on = int(n["start"] * 2 * tpq)
        off = int(n["end"] * 2 * tpq)
        events.append((on, 1, bytes([0x90, n["pitch"], 96])))
        events.append((max(off, on + 1), 0, bytes([0x80, n["pitch"], 0])))
    events.sort(key=lambda e: (e[0], e[1]))

    track = bytearray()
    track += b"\x00\xff\x51\x03" + (500000).to_bytes(3, "big")  # tempo 120 BPM
    last = 0
    for tick, _, msg in events:
        track += _varlen(tick - last) + msg
        last = tick
    track += b"\x00\xff\x2f\x00"  # end of track

    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpq))
        f.write(b"MTrk" + struct.pack(">I", len(track)) + bytes(track))
