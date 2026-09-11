"""DJ transitions: join track A into track B with a proper DJ-style transition.

Core ideas:
  - comb-scored BPM + bar grid; transitions land on bar lines, loops snap to
    real onsets (transients), not just the estimated grid
  - A hands over before its outro fade at a vocal-phrase boundary; B skips its
    quiet intro; B is loudness-matched to A
  - B is tempo-stretched ONLY for styles where the tracks overlap; hard styles
    (tapestop/backspin/looproll/riser/cut/echo) keep B at its native tempo
  - preview mode renders short clips of the SAME transition in many styles at
    once, so you can audition with your ears before the full render
"""
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from flask import Blueprint, request, jsonify, send_from_directory

from separator import get_separator, sep_lock

APP_DIR = Path(__file__).parent
DJ_DIR = APP_DIR / "djmixes"
DL_DIR = APP_DIR / "downloads"
DJ_DIR.mkdir(exist_ok=True)

bp = Blueprint("dj", __name__)
jobs = {}

STYLES = ("automix", "acapella", "tapestop", "looproll", "backspin", "riser",
          "neural", "bassswap", "crossfade", "filter", "echo", "cut")
STEM_STYLES = ("automix", "neural", "bassswap", "acapella")
HARD_STYLES = ("cut", "echo", "tapestop", "looproll", "backspin", "riser")

AFMT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
LIMITER = "alimiter=limit=0.97:level=false:latency=true"
MAX_TEMPO_CHANGE = 0.06


def _dl_url(path: Path):
    """The /downloads/ URL for a library track, folders and all.

    Rebuilding from path.name alone would drop the folder a song is filed
    into and hand back a URL that no longer resolves.
    """
    try:
        return "/downloads/" + Path(path).resolve().relative_to(DL_DIR.resolve()).as_posix()
    except ValueError:
        return f"/downloads/{Path(path).name}"


def _resolve(url_path: str):
    """A /downloads/ URL to a local path. Library folders make these several
    segments deep, so the file only has to sit somewhere under DL_DIR."""
    if url_path.startswith("/downloads/"):
        root = DL_DIR.resolve()
        p = (root / url_path[len("/downloads/"):].strip("/")).resolve()
        if p.is_file() and root in p.parents:
            return p
    return None


# No render should outlive the user's patience; a helper that stops making
# progress is killed rather than left running forever in the background.
FFMPEG_TIMEOUT = 900        # 15 min: enough for a full set render
PROBE_TIMEOUT = 60


def _run(cmd, timeout=FFMPEG_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(cmd[0]).name} timed out after {timeout}s and was stopped")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-400:])


def _duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timed out and was stopped")
    return float(r.stdout.strip())


def _cleanup(work):
    """Delete a job's scratch directory. Every job writes its real output to
    DJ_DIR, so nothing worth keeping lives in here."""
    if work:
        shutil.rmtree(work, ignore_errors=True)


def sweep_temp(max_age_h=6):
    """Remove scratch dirs abandoned by crashed or killed jobs. Runs at
    startup: without it every interrupted render leaks its stems forever."""
    freed = 0
    cutoff = time.time() - max_age_h * 3600
    for parent in {Path(tempfile.gettempdir())}:
        for pat in ("dj_*", "djset_*", "karaoke_*"):
            for d in parent.glob(pat):
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    continue
    return freed


MAX_JOBS = 40


def _prune_jobs():
    """Keep the job table bounded — finished jobs are dropped oldest-first once
    the cap is passed, so a long session can't grow memory without limit."""
    if len(jobs) <= MAX_JOBS:
        return
    done = sorted((j.get("ts", 0), i) for i, j in jobs.items() if j.get("done"))
    for _, jid in done[:len(jobs) - MAX_JOBS]:
        jobs.pop(jid, None)


def _new_job(**extra):
    job = {"stage": "Starting...", "done": False, "error": None,
           "ts": time.time(), **extra}
    _prune_jobs()
    return job


def _extract(src: Path, start: float, dur: float, out: Path):
    """Accurate-seek extraction to 44.1k stereo wav."""
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}",
          "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(out)])
    return out


def _fold_ratio(ratio: float) -> float:
    while ratio > 1.5:
        ratio /= 2
    while ratio < 0.667:
        ratio *= 2
    return ratio


def _active_median(r: np.ndarray) -> float:
    active = r[r > r.max() * 0.06] if r.max() > 0 else r
    return float(np.median(active)) if len(active) else 0.0


def _mean_rms(r: np.ndarray, win: float, t0: float, t1: float) -> float:
    i0, i1 = int(t0 / win), max(int(t0 / win) + 1, int(t1 / win))
    seg = r[i0:i1]
    return float(seg.mean()) if len(seg) else 0.0


def _separate_window(src: Path, start: float, dur: float, work: Path, tag: str) -> dict:
    from demucs.api import save_audio
    import separator as sep_mod
    seg = work / f"{tag}_seg.wav"
    _extract(src, start, dur, seg)
    with sep_lock:
        sep_mod.progress["pct"] = 0.0
        sep = get_separator()
        # Random shift augmentation otherwise makes previews differ from exports.
        shifts = sep._shifts
        try:
            sep.update_parameter(shifts=0)
            _, stems = sep.separate_audio_file(seg)
        finally:
            sep.update_parameter(shifts=shifts)
    out = {}
    for k, v in stems.items():
        p = work / f"{tag}_{k}.wav"
        save_audio(v, str(p), samplerate=sep.samplerate, clip="none", as_float=True)
        out[k] = p
    return out


def _slice(src: Path, start: float, dur: float, out: Path):
    _run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
          "-c:a", "pcm_f32le", str(out)])


# where the rhythm-section swap sits inside the transition window, per style
SWAP_FRAC = {"automix": 0.5, "neural": 0.5, "bassswap": 0.5, "acapella": 0.4}

FEEDBACK_FILE = APP_DIR / "feedback.jsonl"


def _feedback_bias() -> dict:
    """Aggregate 👍/👎 verdicts per style from past listening sessions."""
    bias = {}
    try:
        import json as _json
        for line in FEEDBACK_FILE.read_text().splitlines():
            try:
                d = _json.loads(line)
                bias[d["style"]] = bias.get(d["style"], 0) + int(d["verdict"])
            except Exception:
                continue
    except OSError:
        pass
    return bias


def _style_for_pair(ratio, shift, clash, sa, sb):
    """(style, why) for a pair from tempo feasibility, key compatibility and
    how hard the tracks hit. Shared by auto style and the match suggester."""
    if abs(ratio - 1) > MAX_TEMPO_CHANGE:
        return "cut", "different tempos — clean handover at the original tempo"
    if clash:
        return "automix", "keys clash — rhythm-only blend"
    return "automix", "compatible pair — smooth blend"


def _auto_style(ctx, job, analysis, a_path, b_path) -> str:
    """Pick a transition style from the pair's character: tempo feasibility,
    key compatibility, aggressiveness — nudged by accumulated 👍/👎 feedback."""
    sa = analysis.style_signals(a_path)
    sb = analysis.style_signals(b_path)
    cand, why = _style_for_pair(ctx["ratio"], ctx["shift"], ctx["clash"], sa, sb)

    bias = _feedback_bias()
    if bias.get(cand, 0) <= -2:  # you kept disliking this one — try the next best
        for alt in (("cut",) if abs(ctx["ratio"] - 1) > MAX_TEMPO_CHANGE
                    else ("automix", "cut")):
            if alt != cand and bias.get(alt, 0) > -2:
                why += f" (switched from {cand}: your 👎 history)"
                cand = alt
                break
    job["auto_reason"] = why
    return cand


def _load_grid(analysis, path: Path, job=None, label="") -> dict:
    """Real downbeats from madmom when possible, comb-grid fallback otherwise."""
    try:
        if job is not None:
            job["stage"] = f"Tracking beats{label} (neural)..."
        import mix_timing
        import structure
        grid = analysis.madmom_grid(path)
        cached = structure.get_cached(path)
        if cached:
            grid = mix_timing.resolve(grid, {'downbeats': cached.get('downbeats'),
                                             'sections': cached.get('segments', [])})
        return mix_timing.normalize(grid)
    except Exception:
        g = dict(analysis.beat_grid(path))
        g["downbeats"] = None
        return g


def _grid_scaled(grid: dict, ratio: float) -> dict:
    """Grid of the tempo-stretched file: atempo=ratio maps t -> t/ratio."""
    if abs(ratio - 1) < 0.005:
        return grid
    g = {"bpm": round(grid["bpm"] * ratio, 2), "beat_len": grid["beat_len"] / ratio,
         "bar_len": grid["bar_len"] / ratio, "bar": grid["bar"] / ratio}
    g["downbeats"] = ([round(t / ratio, 3) for t in grid["downbeats"]]
                      if grid.get("downbeats") else None)
    return g


def _snap_grid(t, grid):
    """Nearest downbeat (real, per-beat) or uniform-grid fallback."""
    db = grid.get("downbeats")
    if db:
        arr = np.asarray(db)
        return float(arr[np.argmin(np.abs(arr - t))])
    k = round((t - grid["bar"]) / grid["bar_len"])
    return max(0.0, grid["bar"] + k * grid["bar_len"])


def _phrase_candidates(t, sections, grid, phrase_bars=4):
    """Phrase boundaries near t: every `phrase_bars`-th downbeat, anchored at
    the downbeat nearest to the start of the section containing t."""
    db = grid.get("downbeats")
    if db:
        arr = np.asarray(db)
        anchor_i = 0
        for s in sections or []:
            if s["start"] <= t + 1e-6:
                anchor_i = int(np.argmin(np.abs(arr - s["start"])))
            else:
                break
        return arr[anchor_i % phrase_bars::phrase_bars]
    # uniform fallback
    anchor = grid["bar"]
    for s in sections or []:
        if s["start"] <= t + 1e-6:
            k = round((s["start"] - grid["bar"]) / grid["bar_len"])
            anchor = grid["bar"] + k * grid["bar_len"]
        else:
            break
    phrase = phrase_bars * grid["bar_len"]
    lo = anchor - phrase * 40
    return np.array([lo + i * phrase for i in range(90)])


def _phrase_snap(t, sections, grid, phrase_bars=4):
    cands = _phrase_candidates(t, sections, grid, phrase_bars)
    return float(cands[np.argmin(np.abs(cands - t))])


def _phrase_prev(t, sections, grid, phrase_bars=4):
    cands = _phrase_candidates(t, sections, grid, phrase_bars)
    below = cands[cands < t - 1e-3]
    return float(below[-1]) if len(below) else t - phrase_bars * grid["bar_len"]


def _find_drop(sections, limit=120.0):
    """B's 'drop': the first CHORUS-labeled section, else the first strong
    section arriving after a quieter one, else the first full-energy one."""
    for s in sections or []:
        if s["start"] > limit:
            break
        if s.get("label") == "chorus":
            return s["start"]
    prev_e = None
    for s in sections or []:
        if s["start"] > limit:
            break
        if prev_e is not None and s["energy"] >= 0.75 and s["energy"] - prev_e >= 0.12:
            return s["start"]
        prev_e = s["energy"]
    for s in sections or []:
        if s["start"] > 90:
            break
        if s["energy"] >= 0.7:
            return s["start"]
    return None


def _stem_envelopes(style: str, T: float, bar: float, clash: bool = False):
    def fade(kind, st, d):
        st = max(0.0, min(st, T - 0.1))
        d = max(0.1, min(d, T - st))
        return f"afade=t={kind}:st={st:.3f}:d={d:.3f}"

    if style == "automix":
        swap = T / 2
        rhythm_fade = min(2 * bar, T / 2)
        melodic_fade = min(bar, T / 3)
        a = {
            "vocals": fade("out", swap - melodic_fade, melodic_fade),
            "other": fade("out", swap - melodic_fade if clash else swap - melodic_fade / 2, melodic_fade),
            "bass": fade("out", swap - rhythm_fade / 2, rhythm_fade),
            "drums": fade("out", swap - rhythm_fade / 2, rhythm_fade),
        }
        b = {
            # keys clash -> B's melodic layer waits for the swap so the two
            # harmonies never sound together; only rhythm crosses over
            "other": fade("in", swap if clash else swap - melodic_fade / 2, melodic_fade),
            "drums": fade("in", swap - rhythm_fade / 2, rhythm_fade),
            "bass": fade("in", swap - rhythm_fade / 2, rhythm_fade),
            "vocals": fade("in", swap, melodic_fade),
        }
    elif style == "acapella":
        swap = 0.4 * T
        quick = min(bar, 0.15 * T)
        a = {
            "vocals": fade("out", 0.72 * T, 0.2 * T),
            "drums": fade("out", 0, quick),
            "bass": fade("out", 0, quick),
            "other": fade("out", 0, quick),
        }
        b = {
            "drums": f"volume=0:enable='lt(t,{swap:.3f})'",
            "bass": fade("in", swap - 0.05, 0.25),
            "other": fade("in", swap, bar),
            "vocals": fade("in", 0.78 * T, 0.18 * T),
        }
    elif style == "neural":
        a = {
            "vocals": fade("out", 0, 0.3 * T),
            "other": fade("out", 0.25 * T, 0.5 * T),
            "bass": fade("out", 0.5 * T - 0.15, 0.3),
            "drums": fade("out", 0.5 * T, 0.5 * T),
        }
        b = {
            "drums": fade("in", 0, 0.4 * T),
            "bass": fade("in", 0.5 * T - 0.05, 0.25),
            "other": fade("in", 0.5 * T if clash else 0.15 * T, 0.45 * T),
            "vocals": fade("in", 0.5 * T if clash else 0.3 * T, 0.5 * T),
        }
    else:  # bassswap
        a = {
            "vocals": fade("out", 0, T),
            "other": fade("out", 0, T),
            "drums": fade("out", 0, T),
            "bass": fade("out", 0.5 * T - 0.15, 0.3),
        }
        b = {
            "vocals": fade("in", 0.5 * T if clash else 0, T),
            "other": fade("in", 0.5 * T if clash else 0, T),
            "drums": fade("in", 0, T),
            "bass": fade("in", 0.5 * T - 0.05, 0.25),
        }
    if style in ("neural", "bassswap"):
        # Complementary rhythm envelopes avoid doubling the kick/drum level.
        a["drums"], b["drums"] = fade("out", 0, T), fade("in", 0, T)
        a["bass"] = fade("out", T / 2 - 0.05, 0.1)
        b["bass"] = fade("in", T / 2 - 0.05, 0.1)
        span = min(bar, T / 3)
        a["other"] = fade("out", T / 2 - span if clash else 0, span if clash else T)
        b["other"] = fade("in", T / 2 if clash else 0, span if clash else T)
    return a, b


def _stem_activity(path, win=0.02):
    """Fine vocal timing from a separated stem; bridge gaps between syllables."""
    import soundfile as sf
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    hop = max(1, round(sr * win))
    n = len(y) // hop
    if not n:
        return np.zeros(1, dtype=bool), win
    rms = np.sqrt(np.mean(y[:n * hop].reshape(n, hop, -1) ** 2, axis=(1, 2)))
    peak = float(np.percentile(rms, 95))
    active = rms > max(0.0015, peak * 0.16)
    # Breaths/gaps under 180 ms belong to the same phrase.
    hits = np.flatnonzero(active)
    for left, right in zip(hits[:-1], hits[1:]):
        if (right - left) * win < 0.18:
            active[left:right] = True
    return active, win


def _vocal_handoff(sa, sb, T):
    """Coordinate both singers. None means this overlap cannot preserve phrases.

    A leaves in a real gap; B opens just BEFORE a fresh phrase. B must already
    be open (or silent) when its original full mix resumes at the end.
    """
    return _vocal_handoff_curves(_stem_activity(sa["vocals"]),
                                  _stem_activity(sb["vocals"]), T)


def _vocal_handoff_curves(curve_a, curve_b, T):
    a, wa = curve_a
    b, wb = curve_b
    gap = max(1, round(0.18 / wa))
    end = None
    for i in range(max(0, min(len(a) - gap + 1, int((T - 0.12) / wa)))):
        if not a[i:i + gap].any():
            end = i * wa
            break
    if end is None:
        return None
    a_off = end + 0.06
    start = None
    for i in range(min(len(b), int(T / wb))):
        if b[i] and (i == 0 or not b[i - 1]):
            onset = i * wb
            # A may already be silent when B starts on the first sample.
            if onset >= a_off + 0.04 or (i == 0 and not a[:gap].any()):
                start = max(0.0, onset - 0.04)
                break
    if start is None:
        # A silent B boundary can safely return to the original mix.
        tail = b[max(0, int((T - 0.12) / wb)):int(T / wb) + 1]
        if len(tail) and not tail.any():
            start = T - 0.08
        else:
            return None
    return (f"afade=t=out:st={end:.3f}:d=0.06",
            f"afade=t=in:st={start:.3f}:d=0.03")


def _curve_segment(curve, start, duration):
    values, win = curve
    return values[max(0, round(start / win)):round((start + duration) / win)], win


def _absolute_curve(curve, start):
    values, win = curve
    return np.pad(values, (round(start / win), 0), constant_values=True), win


def _boundary_cost(curve, t):
    """A boundary through sustained singing costs more than a gap or pickup."""
    if curve is None:
        return 0.0
    values, win = curve
    def level(lo, hi):
        seg = values[max(0, int(lo / win)):max(1, int(hi / win) + 1)]
        return float(np.mean(seg)) if len(seg) else 1.0
    return level(t - 0.25, t) * level(t, t + 0.25)


def _clean_cue(curve, target, grid, lo, hi, placement_cost=None):
    """Prefer a quiet downbeat, or a nearby phrase gap over chopping a word."""
    bar = grid["bar_len"]
    points = {_snap_grid(target + i * bar, grid) for i in range(-4, 5)}
    if curve is not None:
        values, win = curve
        left = max(0, int(max(lo, target - 4 * bar) / win))
        right = min(len(values), int(min(hi, target + 4 * bar) / win))
        quiet = np.asarray(values[left:right]) < 0.2
        bounds = np.diff(np.r_[False, quiet, False].astype(int))
        for start, end in zip(np.flatnonzero(bounds == 1), np.flatnonzero(bounds == -1)):
            if (end - start) * win >= 0.18:
                points.add((left + start) * win + 0.06)
                points.add((left + end) * win - 0.06)
    points = [t for t in points if lo <= t <= hi]
    if not points:
        return float(np.clip(target, lo, hi))
    return min(points, key=lambda t: 2 * _boundary_cost(curve, t)
               + 0.035 * abs(t - target) / bar
               + 0.08 * abs(t - _snap_grid(t, grid)) / bar
               + (placement_cost(t) if placement_cost else 0.0))


def _align_transition(A, cut, B, b_start, T, bpm, ga, gb):
    """Validate actual downbeat drift; different drum patterns need not correlate."""
    import analysis
    matched = _grid_alignment(cut, b_start, T, bpm, ga, gb)
    if matched is not None:
        return matched
    return analysis.align_beats(A, cut, B, b_start, T, bpm)


def _grid_alignment(cut, b_start, T, bpm, ga, gb):
    """Cheap grid check usable while ranking every candidate placement."""
    if min(ga.get('phase_confidence', 1), gb.get('phase_confidence', 1)) < .5:
        return 0.0, 0.0
    if not ga.get("downbeats") or not gb.get("downbeats"):
        return None
    def local(grid, start):
        times = np.asarray(grid["downbeats"])
        anchor = int(np.argmin(abs(times - start)))
        count = max(3, int(np.ceil(T / grid["bar_len"])) + 1)
        return times[anchor:anchor + count] - start
    a, b = local(ga, cut), local(gb, b_start)
    n = min(len(a), len(b))
    if n < 3:
        return None
    errors = a[:n] - b[:n]
    offset = float(np.median(errors))
    drift = float(np.max(abs(errors - offset)))
    beat = 60 / bpm
    # Reject wrong bar phase and drift; a small, steady offset can be nudged.
    if abs(offset) > 0.2 * beat:
        return 0.0, 0.0
    grid_conf = max(0.0, 1.0 - drift / (0.2 * beat))
    if grid_conf < 0.35:
        return 0.0, grid_conf
    return offset, grid_conf


def _loudness(path, start=0.0, duration=None):
    """Measure perceived loudness without changing the source audio."""
    import json
    cmd = ["ffmpeg", "-hide_banner", "-ss", f"{max(0, start):.3f}", "-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    r = subprocess.run(cmd + ["-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
                              "-f", "null", "-"], capture_output=True, text=True,
                       timeout=PROBE_TIMEOUT)
    if r.returncode:
        raise RuntimeError("could not measure transition loudness")
    data = json.loads(r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1])
    value = float(data["input_i"])
    return value if np.isfinite(value) else None


def _roll_geometry(cut, bar, grid):
    """Loop-roll timing from the REAL grid: the bar the roll replaces is the
    span back to the actual previous downbeat, and the loop slice is a quarter
    of it — the global-average beat drifts against the true grid and makes
    every repeat land slightly off. Slow songs roll half a bar so the fill
    doesn't drag. Returns (local_bar, local_beat, roll_dur, roll_start)."""
    db = (grid or {}).get("downbeats")
    span_bar = bar
    if db:
        earlier = [t for t in db if t < cut - 0.25 * bar]
        if earlier:
            prev = max(earlier)
            if 0.5 * bar < cut - prev < 1.5 * bar:
                span_bar = cut - prev
    beat = span_bar / 4
    fx_dur = span_bar if span_bar <= 2.6 else span_bar / 2
    return span_bar, beat, fx_dur, cut - fx_dur


def _roll_clean_source(analysis, A, target, span_bar, beat, floor=0.0):
    """Loop the less vocal of {this beat, the same beat one bar earlier}:
    stuttering a sung word is what makes rolls sound broken."""
    try:
        vs, vw = analysis.vocal_activity(A)

        def voc(t):
            i0, i1 = int(t / vw), int((t + beat) / vw) + 1
            return float(vs[i0:i1].mean()) if 0 <= i0 and i1 <= len(vs) else 1.0

        alt = target - span_bar
        if alt > floor and voc(alt) + 0.08 < voc(target):
            return alt
    except Exception:
        pass
    return target


def _riser_plan(r_a, win_a, med_a, cut, bar):
    """Riser length and level from the song around it: ~4s of build in whole
    bars (tempo-aware), shrunk while A's tail under it is already fading (a
    riser over silence is naked noise), and scaled to the LOCAL loudness of
    the bars it actually sits on — not the whole track's median, which made
    it scream over quiet outros and drown under loud choruses."""
    rise_bars = int(np.clip(round(4.0 / bar), 1, 4))
    rise_bars = max(1, min(rise_bars, int(cut / bar)))
    while rise_bars > 1 and _mean_rms(r_a, win_a, cut - rise_bars * bar, cut) < 0.5 * med_a:
        rise_bars -= 1
    rise_dur = rise_bars * bar
    local = _mean_rms(r_a, win_a, cut - rise_dur, cut)
    # the climax announces B's drop: it should ride just under the mix it
    # sits on, whether that's a loud chorus or a fading outro
    gain = float(min(0.9 * max(local, 0.6 * med_a), 0.55))
    return rise_dur, gain


def _render_style(style, A, cut, B, b_start, delay_s, T, bar, bpm, med_a,
                  sa, sb, work, out_path, tmax=None, clash=False, grid=None,
                  output_start=None, vocal_env=None):
    """Render one transition. A/B are audio files; cut/b_start/delay_s are in
    those files' local clocks. sa/sb: stem dicts already sliced to T (or None).
    grid: A's beat grid IN THE SAME CLOCK (None when A is an excerpt whose
    clock differs from the analyzed file, e.g. preview clips)."""
    import analysis
    import djfx
    X = 0.05
    delay_ms = max(0, int(delay_s * 1000))
    tail = ["-t", f"{tmax:.3f}"] if tmax else []
    if output_start is not None:
        tail = ["-ss", f"{output_start:.3f}"] + tail
    enc = ["-c:a", "libmp3lame", "-b:a", "320k", str(out_path)]

    if style in STEM_STYLES:
        env_a, env_b = _stem_envelopes(style, T, bar, clash)
        vocal_env = vocal_env or _vocal_handoff(sa, sb, T)
        if vocal_env is None:
            raise ValueError("no clean vocal handover in this overlap")
        env_a["vocals"], env_b["vocals"] = vocal_env
        order = ["vocals", "drums", "bass", "other"]
        cmd = ["ffmpeg", "-y", "-i", str(A)]
        for k in order:
            cmd += ["-i", str(sa[k])]
        for k in order:
            cmd += ["-i", str(sb[k])]
        cmd += ["-i", str(B)]
        fa = [f"[{i + 1}:a]{env_a[k]}[sa{i}]" for i, k in enumerate(order)]
        fb = [f"[{i + 5}:a]{env_b[k]}[sb{i}]" for i, k in enumerate(order)]
        fc = ";".join(fa + fb) + (
            f";[sa0][sa1][sa2][sa3]amix=inputs=4:normalize=0[atrans]"
            f";[sb0][sb1][sb2][sb3]amix=inputs=4:normalize=0[btrans]"
            f";[0:a]atrim=0:{cut + X:.3f}[ahead]"
            f";[ahead][atrans]acrossfade=d={X}[aall]"
            f";[9:a]atrim={b_start + T - X:.3f},asetpts=PTS-STARTPTS[brest]"
            f";[btrans][brest]acrossfade=d={X}[ball]"
            f";[ball]adelay={delay_ms}|{delay_ms}[bd]"
            f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[out]"
        )
        _run(cmd + ["-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "crossfade":
        fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
              f";[0:a]atrim=0:{cut + T:.3f}[a]"
              f";[a][b1]acrossfade=d={T:.3f}:c1=qsin:c2=qsin,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "filter":
        steps = 10
        cmds = []
        for i in range(steps):
            t2 = cut + T * i / steps
            f = 30 * (3000 / 30) ** (i / (steps - 1))
            cmds.append(f"{t2:.3f} highpass@sw f {f:.0f}")
        send = ";".join(cmds)
        fc = (f"[0:a]atrim=0:{cut + T:.3f},asendcmd=c='{send}',highpass@sw=f=20,"
              f"afade=t=out:st={cut + 0.6 * T:.3f}:d={0.4 * T:.3f}[a]"
              f";[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={0.6 * T:.3f},"
              f"adelay={delay_ms}|{delay_ms}[b]"
              f";[a][b]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "echo":
        beat_ms = int(60000 / bpm)
        tail_d = max(2.0, T)
        pre = 2 * 60 / bpm
        fc = (f"[0:a]atrim=0:{cut - pre:.3f}[ahead]"
              f";[0:a]atrim={cut - pre:.3f}:{cut + 0.05:.3f},asetpts=PTS-STARTPTS,"
              f"apad=pad_dur={tail_d:.3f},aecho=0.8:0.7:{beat_ms}|{beat_ms * 2}:0.5|0.3[atail]"
              f";[ahead][atail]concat=n=2:v=0:a=1[aall]"
              f";[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
              f"adelay={delay_ms}|{delay_ms}[b]"
              f";[aall][b]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style in ("tapestop", "backspin", "looproll"):
        if style == "tapestop":
            fx_dur = min(1.2, max(0.5, bar / 2))
            seg = work / "fx_src.wav"
            _extract(A, cut - fx_dur, fx_dur, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.tape_stop(y, sr, fx_dur), sr)
        elif style == "backspin":
            fx_dur = min(1.0, max(0.45, bar / 2))
            src_len = min(max(0.5, cut - fx_dur), 3.5 * fx_dur)
            seg = work / "fx_src.wav"
            _extract(A, cut - fx_dur - src_len, src_len, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.backspin(y, sr, fx_dur), sr)
        else:  # looproll: roll the last real bar (half bar on slow songs)
            span_bar, beat, fx_dur, src = _roll_geometry(cut, bar, grid)
            src = _roll_clean_source(analysis, A, src, span_bar, beat)
            # snap the loop source to a real transient so every repeat hits
            snapped = analysis.snap_to_beat(A, src, window=0.25 * beat)
            seg = work / "fx_src.wav"
            _extract(A, snapped, 1.5 * beat, seg)
            y, sr = djfx.load(seg)
            chunk = work / "fx_out.wav"
            djfx.save(chunk, djfx.loop_roll(y, sr, beat, total_dur=fx_dur), sr)
        head_end = cut - fx_dur
        fc = (f"[0:a]atrim=0:{head_end:.3f},{AFMT}[h]"
              f";[1:a]{AFMT}[fx]"
              f";[h][fx]concat=n=2:v=0:a=1[aall]"
              f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
              f"adelay={delay_ms}|{delay_ms}[bd]"
              f";[aall][bd]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(chunk), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    elif style == "riser":
        r_a, win_a = analysis.rms_profile(A)
        rise_dur, gain = _riser_plan(r_a, win_a, med_a, cut, bar)
        rise = work / "riser.wav"
        djfx.save(rise, djfx.riser(44100, rise_dur, gain=gain,
                                   beat_dur=bar / 4), 44100)
        rise_at = max(0, int((cut - rise_dur) * 1000))
        fc = (f"[0:a]atrim=0:{cut:.3f},afade=t=out:st={cut - 0.05:.3f}:d=0.05[a]"
              f";[1:a]{AFMT},adelay={rise_at}|{rise_at}[r]"
              f";[2:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.01,"
              f"adelay={delay_ms}|{delay_ms}[bd]"
              f";[a][r][bd]amix=inputs=3:duration=longest:normalize=0,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(rise), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)

    else:  # cut
        fc = (f"[1:a]atrim={b_start:.3f},asetpts=PTS-STARTPTS[b1]"
              f";[0:a]atrim=0:{cut:.3f}[a]"
              f";[a][b1]acrossfade=d=0.03,{LIMITER}[out]")
        _run(["ffmpeg", "-y", "-i", str(A), "-i", str(B),
              "-filter_complex", fc, "-map", "[out]"] + tail + enc)


def _prep(job, a_path, b_path, beats):
    """Shared analysis: grids, loudness, matched/plain B, entry points."""
    import analysis
    ctx = {}
    grid_a = _load_grid(analysis, a_path, job, " of track A")
    grid_b = _load_grid(analysis, b_path, job, " of track B")
    ctx["grid_b_orig"] = grid_b
    job["a_facts"] = analysis.analyze(a_path)
    job["b_facts"] = analysis.analyze(b_path)
    shift, clash = analysis.harmony_plan(
        job["a_facts"].get("camelot"), job["b_facts"].get("camelot"))
    # Keep the singer's original pitch. Incompatible keys use separate melodic
    # handovers, even when transposing B could theoretically match the key.
    ctx["shift"], ctx["clash"] = 0, bool(shift or clash)
    ctx["bpm"] = grid_a["bpm"] or 120
    ctx["T"] = max(2.0, beats * 60.0 / ctx["bpm"])
    ctx["bar"] = grid_a["bar_len"]
    ctx["grid_a"] = grid_a
    ctx["energy_a"] = analysis.energy_profile(a_path)
    ctx["energy_b"] = analysis.energy_profile(b_path)

    work = Path(tempfile.mkdtemp(prefix="dj_"))
    ctx["work"] = work

    r_a, win_a = analysis.rms_profile(a_path)
    r_b0, win_b0 = analysis.rms_profile(b_path)
    ctx["r_a"], ctx["win_a"] = r_a, win_a
    ctx["med_a"] = _active_median(r_a)
    med_b = _active_median(r_b0)
    ctx["r_b0"], ctx["win_b0"], ctx["med_b0"] = r_b0, win_b0, med_b
    gain_b = float(np.clip(ctx["med_a"] / med_b, 0.5, 2.0)) if med_b > 0 else 1.0

    job["stage"] = "Preparing track B..."
    ratio = 1.0
    if grid_a["bpm"] and grid_b["bpm"]:
        ratio = _fold_ratio(grid_a["bpm"] / grid_b["bpm"])
    ctx["ratio"] = ratio
    job["stretch"] = round(ratio, 4)

    def make_b(stretched, override_orig=None):
        import shutil as _sh
        out = work / ("b_matched.wav" if stretched else "b_plain.wav")
        stretching = stretched and abs(ratio - 1) > 0.005
        shift = ctx.get("shift", 0) if stretched else 0  # only blends overlap harmonically
        # rubberband earns its runtime only for audible stretches or pitch
        # shifts; tiny tempo nudges sound identical through atempo
        need_rb = (stretching and abs(ratio - 1) > 0.04) or shift
        rb = _sh.which("rubberband")
        if rb and need_rb:
            # rubberband (R2 engine — good transients, reasonable speed);
            # also does clean pitch shifting for key matching
            raw = work / "b_raw.wav"
            _run(["ffmpeg", "-y", "-i", str(b_path), "-ac", "2", "-ar", "44100", str(raw)])
            cmd = [rb, "-t", f"{1 / ratio:.6f}" if stretching else "1.0"]
            if shift:
                cmd += ["-p", str(shift)]
            rbo = work / "b_rb.wav"
            r = subprocess.run(cmd + [str(raw), str(rbo)], capture_output=True,
                               text=True, timeout=FFMPEG_TIMEOUT)
            if r.returncode == 0:
                _run(["ffmpeg", "-y", "-i", str(rbo), "-af", f"volume={gain_b:.3f}", "-c:a", "pcm_f32le", str(out)])
                job["stretch_tool"] = "rubberband"
            else:
                rb = None
        if not (rb and need_rb):
            af = (f"atempo={ratio:.4f}," if stretching else "")
            if shift:
                f = 2 ** (shift / 12)
                af += f"asetrate=44100*{f:.6f},aresample=44100,atempo={1 / f:.6f},"
            af += f"volume={gain_b:.3f}"
            _run(["ffmpeg", "-y", "-i", str(b_path), "-af", af, "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(out)])
        if shift:
            job["key_action"] = f"B pitch-shifted {shift:+d} st for key match"
        elif ctx.get("clash") and stretched:
            job["key_action"] = "keys clash — B melodic layers held until the swap"
        k = ratio if stretching else 1.0
        grid = _grid_scaled(ctx["grid_b_orig"], k)

        def snap(t):
            return _snap_grid(t, grid)

        if override_orig is not None:
            # user-picked entry point, given on B's ORIGINAL timeline
            t_local = float(override_orig) / ratio if stretching else float(override_orig)
            return {"file": out, "b_start": snap(t_local), "manual": True, "k": k}

        try:
            # sections of the ORIGINAL B (cached across jobs), scaled in time
            secs0 = ctx.get("b_sections")
            if secs0 is None:
                secs0 = analysis.detect_sections(b_path)
            k = ratio if stretching else 1.0
            secs = [{**s, "start": round(s["start"] / k, 2), "end": round(s["end"] / k, 2)} for s in secs0]
        except Exception:
            secs = []
        drop = _find_drop(secs)
        loud = None
        if drop is None:  # fallback: first loud bar
            r_b, win_b = analysis.rms_profile(out)
            med = _active_median(r_b)
            loud = 0.0
            t = grid["bar"]
            dur = _duration(out)
            while t < min(60, dur - 8):
                if _mean_rms(r_b, win_b, t, t + 2 * grid["bar_len"]) >= 0.6 * med:
                    loud = t
                    break
                t += grid["bar_len"]
        return {"file": out, "snap": snap, "sections": secs, "k": k,
                "drop": snap(drop) if drop is not None else None, "loud": loud}
    ctx["make_b"] = make_b
    return ctx


def _entry_for(bvar, style, T):
    c = _entry_candidates(bvar, style, T)[0]
    return c[0], c[2]


def _manual_cut(ctx, a_path, style, T, wanted):
    """Snap a user-picked exit point to the nearest real downbeat."""
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    wanted = min(max(wanted, 10.0), target)
    ctx["cut_reason"] = "manual"
    return max(10.0, min(_snap_grid(wanted, ctx["grid_a"]), target))


def _exit_candidates(ctx, a_path, style, T, limit=6):
    """Up to `limit` phrase-snapped exit points of A (latest first), each with
    the energy of the section it leaves — used for energy-continuity pairing."""
    import analysis
    dur_a = _duration(a_path)
    target = dur_a - (1.0 if style in HARD_STYLES else T + 1.0)
    if target < 10:
        raise ValueError("track A is too short for this transition length")
    grid_a, bar = ctx["grid_a"], ctx["bar"]
    look = min(T, 8.0)

    def alive(t):
        return _mean_rms(ctx["r_a"], ctx["win_a"], t, t + look) >= 0.45 * ctx["med_a"]

    try:
        secs = ctx.get("a_sections")
        if secs is None:
            secs = analysis.detect_sections(a_path)
    except Exception:
        secs = []
    ctx["a_sections"] = secs
    cands = []

    def consider(s, reason):
        # The overlap must FINISH before the section ends. Starting at the
        # chorus end instead mixes its following breakdown into B's drop.
        end_limit = min(target, s["end"] - (T if style not in HARD_STYLES else 0))
        cut = _phrase_snap(end_limit, secs, grid_a)
        while cut > end_limit:
            cut = _phrase_prev(cut, secs, grid_a)
        while cut > s["start"] + bar and not alive(cut):
            cut = _phrase_prev(cut, secs, grid_a)
        if cut >= s["start"] and cut > 20 and alive(cut) and all(abs(cut - c) > 1 for c, _, _ in cands):
            energy = _local_energy(ctx.get("energy_a"), cut, cut + look)
            cands.append((cut, energy if energy is not None else s["energy"], reason))

    # labeled choruses first — "hand over right after the chorus"
    for s in reversed(secs):
        if len(cands) >= limit:
            break
        if s.get("label") == "chorus" and s["end"] >= 20:
            consider(s, "before the last chorus ends" if not cands else "before an earlier chorus ends")
    for s in reversed(secs):
        if len(cands) >= limit:
            break
        if s["energy"] < 0.55 or s["end"] < 20 or s.get("label") == "chorus":
            continue
        consider(s, "end of a high-energy phrase")
    if not cands:
        cut = _phrase_snap(target, secs, grid_a)
        while cut > target:
            cut = _phrase_prev(cut, secs, grid_a)
        while cut > 20 and not alive(cut):
            cut = _phrase_prev(cut, secs, grid_a)
        cands = [(cut, None, "loudness fallback")]
    return cands


def _pick_cut(ctx, a_path, style, T):
    cut, _, reason = _exit_candidates(ctx, a_path, style, T)[0]
    ctx["cut_reason"] = reason
    return cut


def _entry_candidates(bvar, style, T, limit=6):
    """Up to `limit` entry points of B (earliest first) with section energies."""
    if bvar.get("manual"):
        return [(bvar["b_start"], None, "manual")]
    frac = SWAP_FRAC.get(style)

    def entry(t):
        off = frac * T if frac else 0.0
        return bvar["snap"](max(0.0, t - off))

    cands = []
    prev_e = None
    for s in bvar.get("sections") or []:
        if s["start"] > 120 or len(cands) >= limit:
            break
        jump = prev_e is not None and s["energy"] - prev_e >= 0.10
        if s["energy"] >= 0.65 and (jump or s["energy"] >= 0.75):
            cands.append((entry(s["start"]), s["energy"],
                          "drop-to-drop" if frac else "on the drop"))
        prev_e = s["energy"]
    if not cands:
        base = bvar["drop"] if bvar.get("drop") is not None else (bvar.get("loud") or 0.0)
        cands = [(entry(base), None, "first loud section")]
    return cands


def _choose_pair(a_cands, b_cands, pen=None):
    """Energy continuity: pick the exit/entry pair whose section energies match,
    with a mild preference for the latest exit and earliest entry. `pen` adds
    a placement penalty (vocal clash, dead air) for a candidate (cut, entry)."""
    best = None
    for ia, (ca, ea, ra) in enumerate(a_cands):
        for ib, (cb, eb, rb) in enumerate(b_cands):
            if ea is None or eb is None:
                score = 0.5 + 0.04 * ia + 0.04 * ib
            else:
                score = abs(ea - eb) + 0.04 * ia + 0.04 * ib
            if pen is not None:
                score += pen(ca, cb)
            if best is None or score < best[0]:
                best = (score, ca, cb, ra, rb, ea, eb)
    if best is None:
        raise ValueError("not enough audio remains for this transition")
    return best


def _vocal_curve(path):
    """Lead-vocal presence curve of the ORIGINAL file, or None if unanalyzable."""
    import analysis
    try:
        return analysis.vocal_activity(path)
    except Exception:
        return None


def _vocal_clash(va, vb, cut, b_start, k, T, style):
    """[0,1]-ish: both tracks' lead vocals audible at once during the blend.
    cut runs on A's playing timeline (same as its curve), b_start on B's
    PLAYING (tempo-matched) timeline; k maps that back to the original file
    the curve was computed on (matched t -> original t*k)."""
    if va is None or vb is None or T <= 0:
        return 0.0
    sa, wa = va
    sb, wb = vb
    frac = SWAP_FRAC.get(style)
    tot = wsum = 0.0
    for t in np.arange(0.2, T, 0.4):
        if frac:
            # stem blends mute B's vocal until the swap, so the clash risk is
            # A's vocal still hanging on after it while A fades out
            w = (1.0 - t / T) if t >= frac * T else 0.0
        else:
            w = 4.0 * (t / T) * (1.0 - t / T)  # plain fades: mid-blend counts
        if w <= 0:
            continue
        ia = int((cut + t) / wa)
        ib = int((b_start + t) * k / wb)
        a = float(sa[ia]) if 0 <= ia < len(sa) else 0.0
        b = float(sb[ib]) if 0 <= ib < len(sb) else 0.0
        tot += w * a * b
        wsum += w
    return tot / wsum if wsum else 0.0


def _dead_air(ctx, cut, b_start, k, T):
    """Penalty for a blend outliving its material: A's tail already faded out,
    or B still near-silent for much of the overlap."""
    span, p = 0.7 * T, 0.0
    r_a, win_a, med_a = ctx.get("r_a"), ctx.get("win_a"), ctx.get("med_a")
    if r_a is not None and med_a:
        seg = r_a[int(cut / win_a):int((cut + span) / win_a) + 1]
        if len(seg):
            p += 0.5 * float((seg < 0.35 * med_a).mean())
    r_b, win_b, med_b = ctx.get("r_b0"), ctx.get("win_b0"), ctx.get("med_b0")
    if r_b is not None and med_b:
        seg = r_b[int(b_start * k / win_b):int((b_start + span) * k / win_b) + 1]
        if len(seg):
            p += 0.3 * float((seg < 0.3 * med_b).mean())
    return p


def _local_energy(profile, start, end):
    if profile is None:
        return None
    values, win = profile
    seg = values[max(0, int(start / win)):max(1, int(np.ceil(end / win)))]
    return float(np.mean(seg)) if len(seg) else 0.0


def _energy_handover(ctx, cut, b_start, k, T, style):
    """Compare the energy listeners actually hear before and across the swap."""
    pa, pb = ctx.get("energy_a"), ctx.get("energy_b")
    if pa is None or pb is None:
        return 0.0
    bar = ctx.get("bar", 2.0)
    swap = 0 if style in HARD_STYLES else SWAP_FRAC.get(style, 0.5) * T
    before = _local_energy(pa, max(0, cut - 2 * bar), cut)
    outgoing = _local_energy(pa, max(0, cut + swap - bar), cut + swap + (0 if style in HARD_STYLES else bar))
    incoming = _local_energy(pb, (b_start + swap) * k, (b_start + swap + 2 * bar) * k)
    # A quiet passage into a drop is an arrangement mismatch, even if volume
    # normalization makes the two sections equally loud.
    mismatch = abs(incoming - outgoing)
    collapse = max(0.0, before - outgoing - 0.12)
    surge = max(0.0, incoming - outgoing - 0.18)
    return 4.0 * mismatch + 5.0 * collapse + 5.0 * surge


def _auto_beat_options(style):
    """Candidate blend lengths when the user leaves length on Auto."""
    if style in HARD_STYLES:
        return (16,)                 # near-instant switches; T only sizes the fx tail
    if style in ("crossfade", "filter"):
        return (8, 16, 32)
    return (8, 16, 32)               # long blends require an explicit choice


def _plan_join(get_a, get_b, beat_opts, bpm, pen_factory, risky):
    """Score each candidate length jointly with its best exit/entry pair.
    Prefer shorter blends unless a longer one finds a better placement.
    Returns (beats, T, choice)
    where choice is _choose_pair's tuple."""
    best = err = None
    for tier, nb in enumerate(beat_opts):
        T = max(2.0, nb * 60.0 / bpm)
        try:
            a_cands, b_cands = get_a(T), get_b(T)
        except ValueError as e:
            err = e
            continue
        try:
            choice = _choose_pair(a_cands, b_cands, pen_factory(T))
        except ValueError as e:
            err = e
            continue
        total = choice[0] + (0.18 * tier if risky else 0.06 * tier)
        if best is None or total < best[0]:
            best = (total, nb, T, choice)
    if best is None:
        raise err or ValueError("no viable transition point")
    return best[1], best[2], best[3]


def _length_reason(beats, beat_opts, risky):
    if len(beat_opts) < 2:
        return None
    if beats == beat_opts[-1]:
        return "longer blend found a cleaner handover"
    if risky:
        return "kept short — key/tempo risk"
    return "short blend preserves vocals and momentum"


def process_job(job_id, a_path, b_path, opts):
    job = jobs[job_id]
    try:
        if opts.get("preview"):
            _preview_job(job, a_path, b_path, opts)
        else:
            _mix_pair(job, a_path, b_path, opts)
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


def _mix_pair(job, a_path, b_path, opts, preview=False):
    if opts.get("candidate") or opts.get("style", "auto") in ("auto", "automix"):
        import mixengine
        return mixengine.run(job, a_path, b_path, opts, preview)
    return _mix_pair_legacy(job, a_path, b_path, opts, preview)


def _mix_pair_legacy(job, a_path, b_path, opts, preview=False, prepared=None):
    """One planning and rendering path for both auditions and full exports."""
    import analysis
    ctx = None
    try:
        style = opts.get("style", "auto")
        if style != "auto" and style not in STYLES:
            raise ValueError(f"unknown style: {style}")
        raw = opts.get("beats")
        auto_len = raw in (None, "", 0, "auto")
        beats = 16 if auto_len else int(raw)
        if beats not in (4, 8, 16, 32, 64):
            raise ValueError("choose 4, 8, 16, 32 or 64 beats")
        ctx = prepared if prepared is not None else _prep(job, a_path, b_path, beats)
        bar, bpm, work = ctx["bar"], ctx["bpm"], ctx["work"]
        if style == "auto":
            style = _auto_style(ctx, job, analysis, a_path, b_path)
            job["style_chosen"] = style
        if style not in HARD_STYLES and abs(ctx["ratio"] - 1) > MAX_TEMPO_CHANGE:
            job["fallback"] = "cut — tempo difference is too large for a natural blend"
            style = "cut"
        # A harmonic full-mix crossfade has no way to remove clashing melodies.
        if style in ("crossfade", "filter") and ctx["clash"]:
            job["fallback"] = "automix — separate melodic handovers for different keys"
            style = "automix"

        va, vb = _vocal_curve(a_path), _vocal_curve(b_path)
        for attempt in range(2):
            bvar = ctx["make_b"](style not in HARD_STYLES, opts.get("b_start"))
            B, k_b = bvar["file"], bvar["k"]
            gb = _grid_scaled(ctx["grid_b_orig"], k_b)
            risky = ctx["clash"] or abs(k_b - 1) > 0.04
            beat_opts = _auto_beat_options(style) if auto_len else (beats,)

            def penalty(T_):
                def score(ca, cb):
                    p = (0.8 * _boundary_cost(vb, cb * k_b)
                         + _energy_handover(ctx, ca, cb, k_b, T_, style))
                    if style in HARD_STYLES:
                        return p + 1.5 * _boundary_cost(va, ca)
                    alignment = _grid_alignment(ca, cb, T_, bpm, ctx["grid_a"], gb)
                    if alignment is not None:
                        p += 6.0 if alignment[1] < 0.35 else 0.15 * (1 - alignment[1])
                    return (p + 1.4 * _vocal_clash(va, vb, ca, cb, k_b, T_, style)
                            + 1.5 * _dead_air(ctx, ca, cb, k_b, T_))
                return score

            def exits(T_):
                if opts.get("cut") is not None:
                    return [(_manual_cut(ctx, a_path, style, T_, float(opts["cut"])), None, "manual")]
                return _exit_candidates(ctx, a_path, style, T_)

            def entries(T_):
                return [c for c in _entry_candidates(bvar, style, T_)
                        if 0 <= c[0] <= _duration(B) - (1 if style in HARD_STYLES else T_ + 0.1)]

            beats_used, T, choice = _plan_join(exits, entries, beat_opts, bpm, penalty, risky)
            _, cut, b_start, reason_a, plan_b, _, _ = choice
            sa_cut = sb_cut = vocal_env = None
            if style in HARD_STYLES:
                if opts.get("cut") is None:
                    cut = _clean_cue(va, cut, ctx["grid_a"], 10, _duration(a_path) - 0.5,
                                     lambda ca: _energy_handover(ctx, ca, b_start, k_b, T, style))
                if opts.get("b_start") is None:
                    b_start = _clean_cue(vb, b_start, gb, 0, _duration(B) - 1,
                                         lambda cb: _energy_handover(ctx, cut, cb, k_b, T, style))
            else:
                delta, conf = _align_transition(a_path, cut, B, b_start, T, bpm, ctx["grid_a"], gb)
                job["beat_confidence"] = round(conf, 2)
                if conf < 0.35:
                    job["fallback"] = "cut — beats do not stay aligned through the blend"
                    style = "cut"
                    continue
                # Correct the source clock, so every envelope swaps on the same
                # downbeat. Delaying only B moves its bass/vocal swap off A's.
                b_start = max(0.0, b_start - delta)
                job["nudge_ms"] = round(delta * 1000)

            # Match the sections actually meeting, using perceived loudness.
            job["stage"] = "Matching transition loudness..."
            la = _loudness(a_path, max(0, cut - 4), 8)
            lb = _loudness(B, b_start + (T / 2 if style not in HARD_STYLES else 0), 8)
            gain_db = float(np.clip(la - lb, -3, 3)) if la is not None and lb is not None else 0.0
            leveled = work / f"b_leveled_{attempt}.wav"
            _run(["ffmpeg", "-y", "-i", str(B), "-af", f"volume={gain_db:.3f}dB",
                  "-c:a", "pcm_f32le", str(leveled)])
            B = leveled
            job["local_gain_db"] = round(gain_db, 2)

            if style in STEM_STYLES:
                a0, b0 = max(0, cut - 2 * bar), max(0, b_start - 2 * bar)
                a_len = min(_duration(a_path) - a0, cut - a0 + T + 2 * bar)
                b_len = min(_duration(B) - b0, b_start - b0 + T + 2 * bar)
                job["stage"] = "Checking vocal phrases in track A (Demucs)..."
                sa = _separate_window(a_path, a0, a_len, work, "a")
                job["stage"] = "Checking vocal phrases in track B (Demucs)..."
                sb = _separate_window(B, b0, b_len, work, "b")
                av, bv = _stem_activity(sa["vocals"]), _stem_activity(sb["vocals"])
                va = _absolute_curve(av, a0)
                vb_playing = _absolute_curve(bv, b0)
                vb = (vb_playing[0], vb_playing[1] * k_b)
                # Refine both cue points together, using actual stems. Keep the
                # original beat nudge when moving B by whole bars.
                ac = [cut] if opts.get("cut") is not None else sorted({
                    _snap_grid(cut + i * bar, ctx["grid_a"]) for i in range(-2, 3)})
                bc = [b_start] if opts.get("b_start") is not None else sorted({
                    max(0, _snap_grid(b_start + delta + i * bar, gb) - delta) for i in range(-2, 3)})
                best = None
                for ca in ac:
                    for cb in bc:
                        if ca < a0 or ca + T > a0 + a_len - 0.05 or cb < b0 or cb + T > b0 + b_len - 0.05:
                            continue
                        # Vocal refinement must not move an energetic placement
                        # over the edge into a breakdown.
                        if (_energy_handover(ctx, ca, cb, k_b, T, style)
                                > _energy_handover(ctx, cut, b_start, k_b, T, style) + 0.35):
                            continue
                        env = _vocal_handoff_curves(_curve_segment(av, ca - a0, T),
                                                    _curve_segment(bv, cb - b0, T), T)
                        if env is None:
                            continue
                        score = penalty(T)(ca, cb) + 0.04 * (abs(ca - cut) + abs(cb - b_start)) / bar
                        if best is None or score < best[0]:
                            best = (score, ca, cb, env)
                if best is None:
                    job["fallback"] = "cut — no overlap lets both vocal phrases stay intact"
                    style = "cut"
                    continue
                _, cut, b_start, vocal_env = best
                # Recheck at the refined points; a different section can drift.
                _, conf = _align_transition(a_path, cut, B, b_start, T, bpm, ctx["grid_a"], gb)
                if conf < 0.35:
                    job["fallback"] = "cut — rhythm is unstable at the vocal-safe cues"
                    style = "cut"
                    continue
                sa_cut, sb_cut = {}, {}
                for name in ("vocals", "drums", "bass", "other"):
                    sa_cut[name], sb_cut[name] = work / f"a_{name}_cut.wav", work / f"b_{name}_cut.wav"
                    _slice(sa[name], cut - a0, T, sa_cut[name])
                    _slice(sb[name], b_start - b0, T, sb_cut[name])
                if opts.get("cut") is None:
                    reason_a = "vocal phrase handover"
            elif style in ("crossfade", "filter") and _vocal_clash(va, vb, cut, b_start, k_b, T, style) > 0.25:
                job["fallback"] = "cut — overlapping lead vocals"
                style = "cut"
                continue

            job.update(style_used=style, stretch=round(k_b, 4), transition_at=round(cut, 3),
                       source_transition_at=round(cut, 3),
                       energy_mismatch=round(_energy_handover(ctx, cut, b_start, k_b, T, style), 3),
                       transition_duration=round(T, 3), beats_used=beats_used,
                       cut_reason=reason_a, entry_plan=plan_b, b_skip=round(b_start, 3),
                       length_reason=_length_reason(beats_used, beat_opts, risky))
            if style in HARD_STYLES:
                job.pop("key_action", None)
                job.pop("nudge_ms", None)
            uid = uuid.uuid4().hex[:8]
            out_name = (f"preview_{uid}_{style}.mp3" if preview else
                        f"{a_path.stem[:80]}_to_{b_path.stem[:80]}_{style}_{uid}.mp3")
            start = max(0, cut - 12) if preview else None
            job["stage"] = "Rendering the transition..."
            _render_style(style, a_path, cut, B, b_start, cut, T, bar, bpm,
                          ctx["med_a"], sa_cut, sb_cut, work, DJ_DIR / out_name,
                          tmax=cut - start + T + 15 if preview else None,
                          output_start=start, vocal_env=vocal_env,
                          clash=ctx["clash"], grid=ctx["grid_a"])
            job["file"] = f"/djmixes/{out_name}"
            if preview:
                job["transition_at"] = round(cut - start, 3)
            job["stage"] = "Done!"
            return
        raise ValueError("could not find a clean transition")
    finally:
        if ctx:
            _cleanup(ctx["work"])


PREVIEW_STYLES = ("automix", "acapella", "tapestop", "looproll", "backspin",
                  "riser", "echo", "cut")


def _preview_job(job, a_path, b_path, opts):
    """Audition each style through the exact full-export planner and renderer."""
    if not opts.get("styles") and opts.get("style", "auto") in ("auto", "automix"):
        _mix_pair(job, a_path, b_path, opts, preview=True)
        if not job.get("previews"):
            job["previews"] = [{k: job[k] for k in ("file", "style_used", "transition_at", "transition_duration", "fallback") if k in job}]
            job["previews"][0]["style"] = job["style_used"]
        return
    styles = list(dict.fromkeys(opts.get("styles") or (opts.get("style", "auto"), *PREVIEW_STYLES)))
    previews = []
    reusable = {}
    for i, style in enumerate(styles):
        if style not in (*STYLES, "auto"):
            continue
        job["stage"] = f"Preview {i + 1}/{len(styles)}: {style}"
        result = {}
        try:
            if style in reusable:
                result = dict(reusable[style])
            else:
                _mix_pair(result, a_path, b_path, {**opts, "style": style}, preview=True)
                if not result.get("fallback"):
                    reusable[result["style_used"]] = dict(result)
            previews.append({**result, "style": style})
        except Exception as e:
            previews.append({"style": style, "error": str(e)})
        job["pct"] = round(100 * (i + 1) / len(styles), 1)
    job["previews"] = previews
    job["stage"] = "Done! Previews use the same transition planning and sound processing as exports."


@bp.post("/dj/feedback")
def dj_feedback():
    """👍/👎 on a preview/result; stored with pair context to tune defaults."""
    import json as _json
    import time as _time
    data = request.get_json(silent=True) or {}
    style = data.get("style")
    verdict = data.get("verdict")
    if style not in STYLES or verdict not in (1, -1):
        return jsonify(error="bad feedback"), 400
    entry = {"ts": int(_time.time()), "style": style, "verdict": verdict,
             "a": Path(data.get("a_file", "")).name, "b": Path(data.get("b_file", "")).name,
             "beats": data.get("beats")}
    if data.get("candidate"):
        import mixengine
        a, b = _resolve(data.get("a_file", "")), _resolve(data.get("b_file", ""))
        if a is None or b is None:
            return jsonify(error="pick both tracks first"), 400
        try:
            plan, _ = mixengine.load_candidate(data["candidate"], a, b)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        entry.update(candidate=data["candidate"], method=plan["method"],
                     shape=plan["shape"], metrics=plan["metrics"],
                     cut=plan["cut"], b_start=plan["b_start"], beats=plan["beats"],
                     mode=plan.get("mode", "creative"), ratio=plan.get("ratio", 1))
    try:
        import analysis
        for k, f in (("a_facts", data.get("a_file")), ("b_facts", data.get("b_file"))):
            p = _resolve(f or "")
            if p is not None:
                entry[k] = analysis.analyze(p)
    except Exception:
        pass
    with open(FEEDBACK_FILE, "a") as fh:
        fh.write(_json.dumps(entry) + "\n")
    import mix_preferences
    return jsonify(ok=True, bias=_feedback_bias(), learning=mix_preferences.train_from_feedback())


@bp.post("/dj/reference")
def dj_reference():
    import mix_preferences
    data = request.get_json(silent=True) or {}
    a, b = _resolve(data.get('a_file', '')), _resolve(data.get('b_file', ''))
    if a is None or b is None:
        return jsonify(error='pick both tracks first'), 400
    try:
        mix_preferences.set_reference(a, b, data.get('candidate'))
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(error=str(e)), 400


AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}


def _pair_score(fa, siga, fb, sigb, sim=None, meta_a=None, meta_b=None,
                mix_b=None):
    """Score one candidate against the seed. Returns
    (match 0-100, reasons, style, ratio) or None when the candidate IS the seed.

    Shared by library matches and web suggestions so both are ranked by exactly
    the same rules — a web pick's BPM/key come from analyzing its 30s preview,
    not from Deezer metadata, which is missing a BPM for most tracks and never
    carries a key at all."""
    import math
    import analysis
    ma, mb = meta_a or {}, meta_b or {}
    if ma.get("track_id") and ma.get("track_id") == mb.get("track_id"):
        return None
    reasons = []

    # genre / vibe — the dominant term. `sim` is corpus-standardized cosine:
    # ~0.25+ is genuinely alike, 0 is average, negative is actively unalike.
    vibe_cost = 0.0
    if ma.get("family") and mb.get("family"):
        if ma["family"] == mb["family"]:
            reasons.append(f"same genre ({mb.get('genre') or mb['family']})"
                           if ma.get("genre") == mb.get("genre")
                           else f"close genres ({ma.get('genre')} / {mb.get('genre')})")
            if sim is not None:  # refine within the genre
                vibe_cost = min(max(0.0, 0.20 - sim) * 1.2, 0.5)
        else:
            reasons.append(f"different genres ({ma.get('genre')} / {mb.get('genre')})")
            vibe_cost = 1.4
    elif sim is not None:
        # Genre unknown for one side. Timbre alone is not trustworthy enough to
        # promote a candidate over a confirmed same-genre one (it happily rated
        # grunge as a close match for house), so it nudges within an
        # uncertainty penalty rather than driving the rank.
        vibe_cost = float(np.clip((0.15 - sim) * 2.0, 0.0, 1.4))
        if ma.get("family") or mb.get("family"):
            vibe_cost += 0.35
            reasons.append("genre unconfirmed")
        if sim >= 0.30:
            reasons.append("sounds similar")
        elif sim < -0.10:
            reasons.append("different sound")

    bpm_a, bpm_b = fa.get("bpm") or 0, fb.get("bpm") or 0
    raw = bpm_a / bpm_b if bpm_a and bpm_b else 1.0
    ratio = _fold_ratio(raw)
    stretch = abs(math.log(ratio))
    folded = abs(math.log(max(raw, 1e-6) / ratio)) > 0.1
    if abs(ratio - 1) <= 0.02:
        reasons.append("half/double tempo" if folded else "same tempo")
    elif 0.9 <= ratio <= 1.12:
        reasons.append(f"{(ratio - 1) * 100:+.0f}% stretch"
                       + (" (half/double)" if folded else ""))
    else:
        reasons.append("tempos far apart — hard styles only")

    shift, clash = analysis.harmony_plan(fa.get("camelot"), fb.get("camelot"))
    if clash:
        reasons.append("keys clash — rhythm-only blend")
    elif shift:
        reasons.append(f"keys ok with {shift:+d} st shift")
    elif fa.get("camelot") and fb.get("camelot"):
        reasons.append(f"keys match ({fa['camelot']}→{fb['camelot']})")

    dd = abs(siga["onset_density"] - sigb["onset_density"])

    # mixability: can you actually get into this track? A long quiet intro is
    # runway to blend under; a track at full tilt from bar one gives you none.
    mix_cost = 0.0
    if mix_b and mix_b.get("known"):
        il = mix_b.get("intro_len", 0.0)
        if mix_b.get("cold_open"):
            mix_cost = 0.30
            reasons.append("cold open — no room to mix in")
        elif il >= 8:
            mix_cost = -0.15
            reasons.append(f"{il:.0f}s intro to blend under")

    # anchors: same-genre same-tempo ~100, 5% stretch ~80, different genre ~25.
    # our key estimate is noisy and the mixer copes with clashes (rhythm-only
    # blend / pitch shift), so key only nudges the rank instead of sinking it
    # a half/double match is real but riskier than a true tempo match (a
    # half-time track does not carry the same drive), and short preview clips
    # bias tempo estimates toward half-time — so it is discounted, not free
    cost = (4.5 * stretch + (0.45 if folded else 0.0)
            + (0.35 if clash else 0.05 * abs(shift))
            + vibe_cost + mix_cost
            + min(dd / 3.0, 1.0) * 0.3)
    style, _ = _style_for_pair(ratio, shift, clash, siga, sigb)
    return (int(np.clip(round(100 * math.exp(-cost)), 1, 100)),
            reasons, style, ratio)


def _match_entry(fa, siga, fb, sigb, path, sim=None, meta_a=None, meta_b=None,
                 mix_b=None):
    """A scored library candidate, packaged for the UI."""
    scored = _pair_score(fa, siga, fb, sigb, sim, meta_a, meta_b, mix_b)
    if scored is None:
        return None
    match, reasons, style, ratio = scored
    return {"file": _dl_url(path), "name": path.name,
            "match": match,
            "bpm": fb.get("bpm") or None, "camelot": fb.get("camelot"),
            "genre": (meta_b or {}).get("genre"),
            "stretch": round((ratio - 1) * 100, 1), "style": style,
            "reasons": reasons}


RATINGS_FILE = APP_DIR / "ratings.jsonl"


def _is_render(name: str) -> bool:
    """A mix this app rendered, not a song. Two naming schemes have been used:
    'A -> B [style]' and process_job's current 'A_to_B_style.mp3'."""
    import re as _re
    if "->" in name:
        return True
    styles = "|".join(STYLES)
    return bool(_re.search(rf"_to_.+_({styles})\.[a-z0-9]+$", name, _re.I))


def _library_tracks():
    # rglob, not iterdir: songs filed into library folders are still library.
    return [p for p in sorted(DL_DIR.rglob("*"))
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS
            and not p.name.startswith(".") and not _is_render(p.name)]


def _timbre_space(extra=()):
    """Corpus statistics for timbre similarity, over the whole library.

    Deduplicated by real path: the seed is normally already a library track,
    and counting it twice shifts the stats enough to change a score by a few
    points — which would make a recorded rating disagree with what was shown."""
    import analysis
    seen, vecs = set(), []
    for p in list(_library_tracks()) + list(extra):
        try:
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            vecs.append(analysis.timbre_vec(p))
        except Exception:
            continue
    return analysis.timbre_stats(vecs)


def _score_pair(seed: Path, cand: Path, stats=None):
    """Rank one pair through the exact code path the suggester uses, so a
    stored rating always reflects the score the user actually saw."""
    import analysis
    import webmeta
    if stats is None:
        stats = _timbre_space()
    fa = dict(analysis.analyze(seed))
    fa["bpm"] = analysis.accurate_bpm(seed)
    fb = dict(analysis.analyze(cand))
    fb["bpm"] = analysis.accurate_bpm(cand)
    sim = analysis.timbre_sim_z(analysis.timbre_vec(seed),
                                analysis.timbre_vec(cand), stats)
    return _match_entry(fa, analysis.style_signals(seed), fb,
                        analysis.style_signals(cand), cand, sim,
                        webmeta.track_meta(seed), webmeta.track_meta(cand),
                        analysis.mix_profile(cand))


def _pair_signals(seed: Path, cand: Path):
    """The raw inputs behind a score — stored with each rating so weights can
    later be fitted against real judgments instead of guessed."""
    import math
    import analysis
    import webmeta
    fa, fb = analysis.analyze(seed), analysis.analyze(cand)
    ba, bb = analysis.accurate_bpm(seed), analysis.accurate_bpm(cand)
    ratio = _fold_ratio(ba / bb) if ba and bb else 1.0
    shift, clash = analysis.harmony_plan(fa.get("camelot"), fb.get("camelot"))
    ma, mb = webmeta.track_meta(seed), webmeta.track_meta(cand)
    mix = analysis.mix_profile(cand)
    return {
        "stretch": round(abs(math.log(ratio)), 4),
        "clash": bool(clash), "shift": int(shift),
        "same_family": (bool(ma and mb and ma.get("family")
                             and ma.get("family") == mb.get("family"))
                        if ma and mb else None),
        "timbre": round(analysis.timbre_sim_z(analysis.timbre_vec(seed),
                                              analysis.timbre_vec(cand),
                                              _timbre_space()), 4),
        "intro_len": mix.get("intro_len"), "cold_open": mix.get("cold_open"),
        "onset_delta": round(abs(analysis.style_signals(seed)["onset_density"]
                                 - analysis.style_signals(cand)["onset_density"]), 2),
    }


@bp.post("/dj/rate")
def dj_rate():
    """Record 'would I actually mix these?' for one suggested pair.

    Works for library matches (`file`) and for web suggestions (`web_id` plus
    the score the UI showed). Web picks matter most for evaluation: a small
    library offers few candidates, while the radio pool is effectively
    unlimited and every pick has a preview you can listen to before judging."""
    import json as _json
    import time as _time
    data = request.get_json(silent=True) or {}
    seed = _resolve(data.get("seed_file", ""))
    verdict = data.get("verdict")
    if seed is None or verdict not in (1, -1):
        return jsonify(error="bad rating"), 400

    web_id = data.get("web_id")
    try:
        if web_id is not None:
            import webmeta
            feat = webmeta.preview_features(web_id, data.get("preview"))
            row = {"ts": int(_time.time()), "seed": seed.name,
                   "cand": f"web:{web_id}",
                   "cand_name": data.get("name") or str(web_id),
                   "source": "web", "verdict": int(verdict),
                   "score": data.get("score"),
                   "signals": _web_pair_signals(seed, feat) if feat else None}
        else:
            cand = _resolve(data.get("file", ""))
            if cand is None:
                return jsonify(error="bad rating"), 400
            entry = _score_pair(seed, cand)
            row = {"ts": int(_time.time()), "seed": seed.name, "cand": cand.name,
                   "cand_name": cand.name, "source": "library",
                   "verdict": int(verdict),
                   "score": entry["match"] if entry else None,
                   "signals": _pair_signals(seed, cand)}
    except Exception as e:
        return jsonify(error=str(e)), 500
    with open(RATINGS_FILE, "a") as fh:
        fh.write(_json.dumps(row) + "\n")
    return jsonify(ok=True, n=_rating_count())


def _web_pair_signals(seed: Path, feat: dict):
    """Same signal snapshot as a library pair, from the candidate's preview."""
    import math
    import analysis
    import numpy as _np
    fa = analysis.analyze(seed)
    ba, bb = analysis.accurate_bpm(seed), feat.get("bpm") or 0
    ratio = _fold_ratio(ba / bb) if ba and bb else 1.0
    shift, clash = analysis.harmony_plan(fa.get("camelot"), feat.get("camelot"))
    return {
        "stretch": round(abs(math.log(ratio)), 4),
        "clash": bool(clash), "shift": int(shift),
        "same_family": None,          # radio picks carry no genre tag
        "timbre": round(analysis.timbre_sim_z(
            analysis.timbre_vec(seed),
            _np.asarray(feat["timbre"], dtype=_np.float32), _timbre_space()), 4),
        "intro_len": None, "cold_open": None,   # unknowable from a 30s excerpt
        "onset_delta": round(abs(analysis.style_signals(seed)["onset_density"]
                                 - feat.get("onset_density", 0)), 2),
    }


def _load_ratings():
    import json as _json
    rows = []
    try:
        for line in RATINGS_FILE.read_text().splitlines():
            try:
                rows.append(_json.loads(line))
            except Exception:
                continue
    except OSError:
        pass
    # one judgment per pair: a later rating supersedes an earlier one
    latest = {}
    for r in rows:
        latest[(r.get("seed"), r.get("cand"))] = r
    return list(latest.values())


def _rating_count():
    return len(_load_ratings())


def _auc(pos, neg):
    """P(a liked pair outranks a disliked one). 0.5 = no better than chance."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


@bp.get("/dj/suggest/eval")
def dj_suggest_eval():
    """How well does the current ranking agree with your own judgments?"""
    rows = _load_ratings()

    def split(rs):
        return ([r["score"] for r in rs if r["verdict"] == 1 and r.get("score") is not None],
                [r["score"] for r in rs if r["verdict"] == -1 and r.get("score") is not None])

    pos, neg = split(rows)
    auc = _auc(pos, neg)
    out = {"n": len(rows), "liked": len(pos), "disliked": len(neg), "auc": auc,
           "mean_score_liked": round(sum(pos) / len(pos), 1) if pos else None,
           "mean_score_disliked": round(sum(neg) / len(neg), 1) if neg else None}
    # library and web picks are ranked from different evidence (full-track
    # analysis vs a 30s preview), so their quality is worth reading apart
    for src in ("library", "web"):
        rs = [r for r in rows if r.get("source") == src]
        p, n = split(rs)
        out[src] = {"n": len(rs), "auc": _auc(p, n)}
    if auc is None:
        out["verdict"] = ("Rate some pairs first — you need both 👍 and 👎 "
                          "before the ranking can be scored.")
    elif len(pos) + len(neg) < 20:
        out["verdict"] = (f"AUC {auc:.2f}, but only {len(pos) + len(neg)} ratings — "
                          f"too few to trust. Aim for 30+.")
    else:
        quality = ("strong" if auc >= 0.8 else "decent" if auc >= 0.7
                   else "weak" if auc >= 0.6 else "no better than chance")
        out["verdict"] = (f"AUC {auc:.2f} ({quality}) over {len(pos) + len(neg)} "
                          f"ratings — the ranking puts a pair you liked above one "
                          f"you disliked {auc * 100:.0f}% of the time.")
    return jsonify(out)


def _suggest_job(job, seed: Path):
    """Song picker: rank every other track in Downloads as a mix partner for
    the seed, by tempo distance, key compatibility and energy character."""
    import analysis
    import webmeta
    try:
        cands = [p for p in _library_tracks() if p.resolve() != seed.resolve()]
        if not cands:
            raise ValueError("no other tracks in Downloads to match against")
        job["stage"] = "Analyzing your track..."
        fa = dict(analysis.analyze(seed))
        fa["bpm"] = analysis.accurate_bpm(seed)
        siga = analysis.style_signals(seed)
        va = analysis.timbre_vec(seed)
        meta_a = webmeta.track_meta(seed)
        seed_slug = webmeta.slug(webmeta.clean_query(seed.name))

        # pass 1: features for everything (cheap once the disk cache is warm).
        # Timbre similarity is only meaningful against the library's own
        # statistics, so every vector has to exist before anything is scored.
        feats = {}
        for i, p in enumerate(cands):
            job["stage"] = f"Analyzing the library... ({i + 1}/{len(cands)})"
            job["pct"] = round(i / len(cands) * 50, 1)
            cslug = webmeta.slug(webmeta.clean_query(p.name))
            if seed_slug and cslug and (cslug in seed_slug or seed_slug in cslug):
                continue  # the same song under another filename
            try:
                fb = dict(analysis.analyze(p))
                fb["bpm"] = analysis.accurate_bpm(p)
                feats[p] = (fb, analysis.style_signals(p), analysis.timbre_vec(p),
                            analysis.mix_profile(p), cslug)
            except Exception:
                continue  # unreadable file — skip, don't sink the whole scan
        # library-wide stats (not just this scan's candidates) so the score
        # shown here is reproducible later when a rating is recorded
        stats = _timbre_space(extra=[seed])

        best = {}  # duplicate files of one song keep only their best entry
        for i, (p, (fb, sigb, vb, mixb, cslug)) in enumerate(feats.items()):
            job["stage"] = f"Ranking matches... ({i + 1}/{len(feats)})"
            job["pct"] = round(50 + i / max(1, len(feats)) * 50, 1)
            meta_b = webmeta.track_meta(p)
            try:
                entry = _match_entry(fa, siga, fb, sigb, p,
                                     analysis.timbre_sim_z(va, vb, stats),
                                     meta_a, meta_b, mixb)
            except Exception:
                continue
            if entry is None:
                continue
            dkey = (str(meta_b["track_id"]) if meta_b and meta_b.get("track_id")
                    else cslug or p.name)
            if dkey not in best or entry["match"] > best[dkey]["match"]:
                best[dkey] = entry
        matches = sorted(best.values(), key=lambda m: -m["match"])
        job["seed"] = {"name": seed.name, "file": _dl_url(seed),
                       "bpm": fa.get("bpm"), "camelot": fa.get("camelot")}
        job["matches"] = matches[:10]
        job["rated"] = _rating_count()
        job["stage"] = "Done!"
    except Exception as e:
        job["error"] = str(e)
    finally:
        analysis.flush_cache(force=True)
        job["done"] = True


@bp.post("/dj/suggest")
def dj_suggest():
    data = request.get_json(silent=True) or {}
    p = _resolve(data.get("file", ""))
    if p is None:
        return jsonify(error="pick a track first"), 400
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = _new_job()
    threading.Thread(target=_suggest_job, args=(jobs[job_id], p), daemon=True).start()
    return jsonify(job=job_id)


def _order_tracks(paths, grids, facts):
    """Greedy chain minimizing tempo distance + key penalty."""
    import math
    import analysis
    n = len(paths)
    bpms = [g["bpm"] or 120 for g in grids]
    start = min(range(n), key=lambda i: abs(bpms[i] - float(np.median(bpms))))
    order, used = [start], {start}
    while len(order) < n:
        cur = order[-1]

        def cost(j):
            r = _fold_ratio(bpms[cur] / bpms[j])
            shift, clash = analysis.harmony_plan(facts[cur].get("camelot"),
                                                 facts[j].get("camelot"))
            return abs(math.log(r)) * 3 + (1.0 if clash else 0.25 * abs(shift))
        nxt = min((j for j in range(n) if j not in used), key=cost)
        order.append(nxt)
        used.add(nxt)
    return order, bpms


SET_JOIN_STYLES = ("automix", "tapestop", "backspin", "looproll", "riser", "cut")


def _set_plan_job(job, paths):
    """Analyze + order the tracks and suggest a join style per pair, without
    rendering anything - the UI lets the user reorder and adjust first."""
    import analysis
    try:
        n = len(paths)
        facts, grids, sigs = [], [], []
        for i, p in enumerate(paths):
            job["stage"] = f"Analyzing tracks... ({i + 1}/{n})"
            job["pct"] = round(i / max(1, n) * 100, 1)
            grids.append(_load_grid(analysis, p))
            facts.append(analysis.analyze(p))
            sigs.append(analysis.style_signals(p))
        order, bpms = _order_tracks(paths, grids, facts)
        tracks = [{"file": _dl_url(paths[i]), "name": Path(paths[i]).name,
                   "bpm": bpms[i], "camelot": facts[i].get("camelot")}
                  for i in order]
        joins = []
        for k in range(1, n):
            a, b = order[k - 1], order[k]
            clash = analysis.harmony_plan(facts[a].get("camelot"), facts[b].get("camelot"))[1]
            ratio = _fold_ratio(bpms[a] / bpms[b])
            style, note = _style_for_pair(ratio, 0, clash, sigs[a], sigs[b])
            joins.append({"style": style, "note": note})
        job["plan"] = {"tracks": tracks, "joins": joins}
        job["stage"] = "Plan ready - reorder or adjust join styles, then render."
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True


def _set_job(job, paths, beats, join_styles=None, ordered=False,
             cuts=None, b_starts=None):
    """Playlist AutoMix: chain the tracks with per-join transitions into one
    continuous set. paths may be pre-ordered by the user (ordered=True).
    cuts/b_starts are optional per-join marker overrides from the join editor,
    given on the ORIGINAL files' timelines (None = engine decides)."""
    import analysis
    import djfx
    work = None
    try:
        auto_len = beats in (None, "", 0, "auto")
        base_beats = 32 if auto_len else int(beats)
        n = len(paths)
        total_steps = n * 2 + (n - 1) * 3 + 2
        step = {"i": 0}

        def prog(stage):
            step["i"] += 1
            job["stage"] = stage
            job["pct"] = round(min(99.0, step["i"] / total_steps * 100), 1)

        facts, grids = [], []
        for i, p in enumerate(paths):
            prog(f"Analyzing tracks... ({i + 1}/{n})")
            grids.append(_load_grid(analysis, p))
            facts.append(analysis.analyze(p))

        if ordered:
            order = list(range(n))
            bpms = [g["bpm"] or 120 for g in grids]
        else:
            order, bpms = _order_tracks(paths, grids, facts)
        job["order"] = [Path(paths[i]).stem for i in order]

        work = Path(tempfile.mkdtemp(prefix="djset_"))
        X = 0.05
        r0, _ = analysis.rms_profile(paths[order[0]])
        med0 = _active_median(r0)

        matched, mgrids, ratios, effective_styles = [], [], [], []
        target_bpm = bpms[order[0]]
        for k, i in enumerate(order):
            prog(f"Tempo-chaining... ({k + 1}/{n})")
            ratio = 1.0 if k == 0 else _fold_ratio(target_bpm / bpms[i])
            if k:
                st = (join_styles[k - 1] if join_styles and k - 1 < len(join_styles)
                      and join_styles[k - 1] in SET_JOIN_STYLES else "automix")
                if st in HARD_STYLES or abs(ratio - 1) > MAX_TEMPO_CHANGE:
                    ratio = 1.0
                    if st == "automix":
                        st = "cut"
                effective_styles.append(st)
            ri, _ = analysis.rms_profile(paths[i])
            gain = float(np.clip(med0 / max(_active_median(ri), 1e-6), 0.5, 2.0))
            out = work / f"m{k}.wav"
            af = (f"atempo={ratio:.4f}," if abs(ratio - 1) > 0.005 else "") + f"volume={gain:.3f}"
            _run(["ffmpeg", "-y", "-i", str(paths[i]), "-af", af, "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(out)])
            matched.append(out)
            mgrids.append(_grid_scaled(grids[i], ratio if abs(ratio - 1) > 0.005 else 1.0))
            ratios.append(ratio if abs(ratio - 1) > 0.005 else 1.0)
            target_bpm = bpms[i] * ratio

        pieces = []
        join_beats = []
        tracklist = [{"title": Path(paths[order[0]]).stem, "at": 0.0}]
        pos = 0.0
        prev_start = 0.0
        for k in range(1, n):
            st = effective_styles[k - 1]
            A, B = matched[k - 1], matched[k]
            ga, gb = mgrids[k - 1], mgrids[k]
            bpm = ga["bpm"] or 120
            prog(f"Join {k}/{n - 1} ({st}): picking points...")
            r_a, win_a = analysis.rms_profile(A)
            ctx = {"grid_a": ga, "bar": ga["bar_len"], "r_a": r_a, "win_a": win_a,
                   "med_a": _active_median(r_a)}
            ctx["energy_a"] = analysis.energy_profile(A)
            ctx["energy_b"] = analysis.energy_profile(paths[order[k]])
            r_b0, win_b0 = analysis.rms_profile(paths[order[k]])
            ctx["r_b0"], ctx["win_b0"] = r_b0, win_b0
            ctx["med_b0"] = _active_median(r_b0)
            secs_b = analysis.detect_sections(B)
            bvar = {"snap": lambda t, g=gb: _snap_grid(t, g), "sections": secs_b,
                    "drop": _find_drop(secs_b), "loud": gb["bar"]}
            shift, clash = analysis.harmony_plan(facts[order[k - 1]].get("camelot"),
                                                 facts[order[k]].get("camelot"))
            clash = bool(shift or clash)
            # vocal curves live on the ORIGINAL files; the matched decks play
            # sped up by ratios[i], so scale A's curve window and map B by kb
            ka_map, kb_map = ratios[k - 1], ratios[k]
            va = _vocal_curve(paths[order[k - 1]])
            if va is not None:
                va = (va[0], va[1] / ka_map)
            vb = _vocal_curve(paths[order[k]])
            risky = clash or abs(kb_map - 1) > 0.06

            def pen_factory(T_, _st=st, _va=va, _vb=vb, _kb=kb_map, _ctx=ctx):
                def pen(ca, cb):
                    p = 1.4 * _vocal_clash(_va, _vb, ca, cb, _kb, T_, _st)
                    p += _energy_handover(_ctx, ca, cb, _kb, T_, _st)
                    if _st not in HARD_STYLES:
                        aligned = _grid_alignment(ca, cb, T_, bpm, ga, gb)
                        if aligned is not None:
                            p += 6.0 if aligned[1] < 0.35 else 0.15 * (1 - aligned[1])
                    p += 0.8 * _boundary_cost(_vb, cb * _kb)
                    if _st in HARD_STYLES:
                        p += 1.5 * _boundary_cost(_va, ca)
                    if _st == "automix":
                        p += _dead_air(_ctx, ca, cb, _kb, T_)
                    return p
                return pen

            def get_a(T_, _ctx=ctx, _A=A, _st=st, _ps=prev_start, _bar=ga["bar_len"]):
                cands = [c for c in _exit_candidates(_ctx, _A, _st, T_)
                         if c[0] > _ps + _bar]
                if not cands:
                    raise ValueError("no exit left after the previous join")
                return cands

            def get_b(T_, _bvar=bvar, _st=st):
                return _entry_candidates(_bvar, _st, T_)

            opts_k = _auto_beat_options(st) if auto_len else (base_beats,)
            try:
                nb, T, choice = _plan_join(get_a, get_b, opts_k, bpm, pen_factory, risky)
            except ValueError:
                # every exit conflicted with the previous join's end; retry
                # unguarded (the old behavior) rather than failing the set
                nb, T, choice = _plan_join(
                    lambda T_, _ctx=ctx, _A=A, _st=st: _exit_candidates(_ctx, _A, _st, T_),
                    get_b, opts_k, bpm, pen_factory, risky)
            _, cut, b_start, _, _, _, _ = choice
            join_beats.append(nb)
            job["join_beats"] = list(join_beats)
            # join-editor overrides arrive on the original timelines; the
            # matched files are tempo-stretched, so rescale before snapping
            if cuts and k - 1 < len(cuts) and cuts[k - 1] is not None:
                cut = _snap_grid(float(cuts[k - 1]) / ratios[k - 1], ga)
                lo = prev_start + ga["bar_len"]
                hi = _duration(A) - (T if st == "automix" else 0.5)
                cut = float(min(max(cut, lo), max(lo, hi)))
            if b_starts and k - 1 < len(b_starts) and b_starts[k - 1] is not None:
                b_start = _snap_grid(float(b_starts[k - 1]) / ratios[k], gb)
                hi = _duration(B) - (T if st == "automix" else 1.0)
                b_start = float(min(max(b_start, 0.0), max(0.0, hi)))

            if st == "automix":
                delta, confidence = _align_transition(A, cut, B, b_start, T, bpm, ga, gb)
                b_start = max(0, b_start - delta)
                delta = 0.0
                vocal_env = None
                if confidence >= 0.35:
                    prog(f"Join {k}/{n - 1}: stems (Demucs)...")
                    sa = _separate_window(A, cut, T, work, f"a{k}")
                    sb = _separate_window(B, b_start, T, work, f"b{k}")
                    vocal_env = _vocal_handoff(sa, sb, T)
                if vocal_env is None:
                    st = "cut"
                    effective_styles[k - 1] = st
                    if not cuts or k - 1 >= len(cuts) or cuts[k - 1] is None:
                        cut = _clean_cue(va, cut, ga, prev_start + ga["bar_len"], _duration(A) - 0.5,
                                         lambda ca: _energy_handover(ctx, ca, b_start, kb_map, T, st))
                    if not b_starts or k - 1 >= len(b_starts) or b_starts[k - 1] is None:
                        b_start = _clean_cue((vb[0], vb[1] / kb_map) if vb else None,
                                             b_start, gb, 0, _duration(B) - 1,
                                             lambda cb: _energy_handover(ctx, cut, cb, kb_map, T, st))

            if st == "automix":

                prog(f"Join {k}/{n - 1}: rendering...")
                env_a, env_b = _stem_envelopes("automix", T, ga["bar_len"], clash)
                env_a["vocals"], env_b["vocals"] = vocal_env
                join = work / f"join{k}.wav"
                order_s = ["vocals", "drums", "bass", "other"]
                cmd = ["ffmpeg", "-y"]
                for s in order_s:
                    cmd += ["-i", str(sa[s])]
                for s in order_s:
                    cmd += ["-i", str(sb[s])]
                dms = max(0, int(delta * 1000))
                fa = [f"[{i2}:a]{env_a[s]}[sa{i2}]" for i2, s in enumerate(order_s)]
                fb = [f"[{i2 + 4}:a]{env_b[s]},adelay={dms}|{dms}[sb{i2}]"
                      for i2, s in enumerate(order_s)]
                fc = ";".join(fa + fb) + (
                    ";[sa0][sa1][sa2][sa3]amix=inputs=4:normalize=0[at]"
                    ";[sb0][sb1][sb2][sb3]amix=inputs=4:normalize=0[bt]"
                    f";[at][bt]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[out]")
                _run(cmd + ["-filter_complex", fc, "-map", "[out]", str(join)])

                head = work / f"head{k}.wav"
                _extract(A, prev_start, cut - prev_start, head)
                pieces += [head, join]
                pos += (cut - prev_start) + T / 2
                tracklist.append({"title": Path(paths[order[k]]).stem, "at": round(pos, 1)})
                pos += T / 2 - X * 2
                prev_start = b_start + T - X
            elif st in ("tapestop", "backspin", "looproll", "riser"):
                prog(f"Join {k}/{n - 1}: {st}...")
                bar = ga["bar_len"]
                beat = bar / 4
                if st == "looproll":
                    span_bar, beat, replace, _ = _roll_geometry(cut, bar, ga)
                elif st == "riser":
                    replace, rise_gain = _riser_plan(r_a, win_a, ctx["med_a"], cut, bar)
                else:
                    replace = min(1.4 if st == "backspin" else 1.2, max(0.5, bar / 2))
                replace = min(replace, max(0.3, cut - prev_start - 0.1))
                seg = work / f"fxsrc{k}.wav"
                # looproll loops the beat right before the drop (transient-
                # snapped, avoiding sung words); the others transform (or
                # overlay) the whole span
                if st == "looproll":
                    src = _roll_clean_source(analysis, A, cut - replace, span_bar,
                                             beat, floor=prev_start)
                    src_at = analysis.snap_to_beat(A, src, window=0.25 * beat)
                    _extract(A, src_at, 1.5 * beat, seg)
                else:
                    _extract(A, cut - replace, replace, seg)
                y, sr = djfx.load(seg)
                if st == "tapestop":
                    out_y = djfx.tape_stop(y, sr, replace)
                elif st == "backspin":
                    out_y = djfx.backspin(y, sr, replace)
                elif st == "looproll":
                    out_y = djfx.loop_roll(y, sr, beat, total_dur=replace)
                else:  # riser: noise sweep over A's tail, B slams on the peak
                    r = djfx.riser(sr, replace, gain=rise_gain, beat_dur=beat)
                    L = min(len(y), len(r))
                    out_y = np.clip(y[:L] + r[:L], -1.0, 1.0)
                chunk = work / f"fx{k}.wav"
                djfx.save(chunk, out_y, sr)
                ch_dur = len(out_y) / sr
                head = work / f"head{k}.wav"
                _extract(A, prev_start, max(0.1, cut - replace - prev_start), head)
                prog(f"Join {k}/{n - 1}: assembling...")
                pieces += [head, chunk]
                pos += max(0.1, cut - replace - prev_start) + ch_dur - X * 2
                tracklist.append({"title": Path(paths[order[k]]).stem, "at": round(pos, 1)})
                prev_start = b_start
                prog(f"Join {k}/{n - 1}: done")
            else:  # cut
                prog(f"Join {k}/{n - 1}: cut...")
                head = work / f"head{k}.wav"
                _extract(A, prev_start, cut - prev_start, head)
                pieces.append(head)
                pos += (cut - prev_start) - X
                tracklist.append({"title": Path(paths[order[k]]).stem, "at": round(pos, 1)})
                prev_start = b_start
                prog(f"Join {k}/{n - 1}: assembling...")
                prog(f"Join {k}/{n - 1}: done")

        tail = work / "tail.wav"
        _extract(matched[-1], prev_start, _duration(matched[-1]) - prev_start, tail)
        pieces.append(tail)

        prog("Stitching the set...")
        acc = pieces[0]
        for i, p in enumerate(pieces[1:]):
            nxt = work / f"acc{i}.wav"
            _run(["ffmpeg", "-y", "-i", str(acc), "-i", str(p),
                  "-filter_complex", f"[0:a][1:a]acrossfade=d={X}[out]",
                  "-map", "[out]", str(nxt)])
            acc = nxt
        out_name = f"set_{uuid.uuid4().hex[:6]}.mp3"
        out_path = DJ_DIR / out_name
        prog("Encoding...")
        _run(["ffmpeg", "-y", "-i", str(acc), "-af", LIMITER, "-c:a", "libmp3lame", "-b:a", "320k", str(out_path)])

        job["tracklist"] = tracklist
        job["join_styles_used"] = effective_styles
        job["file"] = f"/djmixes/{out_name}"
        job["stage"] = "Done!"
        job["pct"] = 100.0
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True
        _cleanup(work)


@bp.post("/dj/set/plan")
def dj_set_plan():
    data = request.get_json(silent=True) or {}
    paths = [_resolve(f) for f in (data.get("files") or [])]
    if any(p is None for p in paths) or len(paths) < 2:
        return jsonify(error="pick at least two tracks"), 400
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = _new_job()
    threading.Thread(target=_set_plan_job,
                     args=(jobs[job_id], paths),
                     daemon=True).start()
    return jsonify(job=job_id)


@bp.post("/dj/set/start")
def dj_set_start():
    data = request.get_json(silent=True) or {}
    paths = [_resolve(f) for f in (data.get("files") or [])]
    if any(p is None for p in paths) or len(paths) < 2:
        return jsonify(error="pick at least two tracks"), 400
    raw_beats = data.get("beats")
    beats = "auto" if raw_beats in (None, "", 0, "auto") else int(raw_beats)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = _new_job(file=None)
    threading.Thread(target=_set_job,
                     args=(jobs[job_id], paths, beats, data.get("join_styles"),
                           bool(data.get("ordered")),
                           data.get("cuts"), data.get("b_starts")),
                     daemon=True).start()
    return jsonify(job=job_id)


@bp.post("/dj/inspect")
def dj_inspect():
    """Analysis for the transition editor: grids, sections and proposed points
    for both tracks, all on the ORIGINAL files' timelines."""
    import analysis
    data = request.get_json(silent=True) or {}
    a = _resolve(data.get("a_file", ""))
    b = _resolve(data.get("b_file", ""))
    if a is None or b is None:
        return jsonify(error="pick both tracks first"), 400
    if data.get('style', 'auto') in ('auto', 'automix') and data.get('mode', 'natural') != 'creative':
        import naturalmix
        try:
            return jsonify(naturalmix.inspect(a, b, data))
        except ValueError as e:
            return jsonify(error=str(e)), 400
    raw_beats = data.get("beats")
    auto_len = raw_beats in (None, "", 0, "auto")
    beats = 32 if auto_len else int(raw_beats)
    try:
        grid_a = _load_grid(analysis, a)
        grid_b = _load_grid(analysis, b)
        bpm = grid_a["bpm"] or 120
        r_a, win_a = analysis.rms_profile(a)
        ctx = {"grid_a": grid_a, "bar": grid_a["bar_len"],
               "r_a": r_a, "win_a": win_a, "med_a": _active_median(r_a)}
        ctx["energy_a"] = analysis.energy_profile(a)
        ctx["energy_b"] = analysis.energy_profile(b)
        r_b0, win_b0 = analysis.rms_profile(b)
        ctx["r_b0"], ctx["win_b0"] = r_b0, win_b0
        ctx["med_b0"] = _active_median(r_b0)

        import structure
        phr_a, phr_b = structure.for_mix(a), structure.for_mix(b)
        ctx["a_sections"] = phr_a["sections"]
        secs_b = phr_b["sections"]
        drop = _find_drop(secs_b)
        ratio = 1.0
        if grid_a["bpm"] and grid_b["bpm"]:
            ratio = _fold_ratio(grid_a["bpm"] / grid_b["bpm"])
        # everything here runs on the ORIGINAL timelines; during a blend B's
        # original clock advances ratio x faster than A's, so entry offsets and
        # the clash window get scaled by ratio
        bvar = {"snap": lambda t: _snap_grid(t, grid_b), "sections": secs_b,
                "drop": _snap_grid(drop, grid_b) if drop is not None else None,
                "loud": grid_b["bar"]}
        va, vb = _vocal_curve(a), _vocal_curve(b)
        shift, clash = analysis.harmony_plan(analysis.analyze(a).get("camelot"),
                                             analysis.analyze(b).get("camelot"))
        clash = bool(shift or clash)
        style = data.get("style", "auto")
        if style == "auto":
            style = _auto_style({"ratio": ratio, "shift": 0, "clash": clash}, {}, analysis, a, b)
        if style not in STYLES:
            raise ValueError("unknown transition style")
        if style not in HARD_STYLES and abs(ratio - 1) > MAX_TEMPO_CHANGE:
            style = "cut"
        if style in ("crossfade", "filter") and clash:
            style = "automix"
        if style in HARD_STYLES:
            ratio = 1.0
        risky = clash or abs(ratio - 1) > 0.04

        def pen_factory(T_):
            def pen(ca, cb):
                p = (0.8 * _boundary_cost(vb, cb)
                     + _energy_handover(ctx, ca, cb / ratio, ratio, T_, style))
                if style in HARD_STYLES:
                    return p + 1.5 * _boundary_cost(va, ca)
                aligned = _grid_alignment(ca, cb / ratio, T_, bpm, grid_a, _grid_scaled(grid_b, ratio))
                if aligned is not None:
                    p += 6.0 if aligned[1] < 0.35 else 0.15 * (1 - aligned[1])
                return (p + 1.4 * _vocal_clash(va, vb, ca, cb / ratio, ratio, T_, style)
                        + 1.5 * _dead_air(ctx, ca, cb / ratio, ratio, T_))
            return pen

        beat_opts = _auto_beat_options(style) if auto_len else (beats,)
        nb, T, choice = _plan_join(
            lambda T_: _exit_candidates(ctx, a, style, T_),
            lambda T_: _entry_candidates(bvar, style, T_ * ratio),
            beat_opts, bpm, pen_factory, risky)
        _, cut, b_start, reason_a, plan_b, _, _ = choice
        ctx["cut_reason"] = reason_a

        return jsonify(
            style_used=style,
            T=round(T, 2),
            beats=nb,
            length_reason=_length_reason(nb, beat_opts, risky) if auto_len else None,
            entry_plan=plan_b,
            a={"duration": round(_duration(a), 2), "bpm": grid_a["bpm"],
               "bar_phase": round(grid_a["bar"], 3), "bar_len": round(grid_a["bar_len"], 4),
               "downbeats": grid_a.get("downbeats"),
               "sections": ctx.get("a_sections", analysis.detect_sections(a)),
               "cut": round(cut, 2), "cut_reason": ctx.get("cut_reason")},
            b={"duration": round(_duration(b), 2), "bpm": grid_b["bpm"],
               "bar_phase": round(grid_b["bar"], 3), "bar_len": round(grid_b["bar_len"], 4),
               "downbeats": grid_b.get("downbeats"),
               "sections": secs_b, "b_start": round(b_start, 2)},
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


@bp.post("/dj/start")
def dj_start():
    data = request.get_json(silent=True) or {}
    a = _resolve(data.get("a_file", ""))
    b = _resolve(data.get("b_file", ""))
    if a is None or b is None:
        return jsonify(error="pick both tracks first"), 400
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = _new_job(file=None)
    threading.Thread(target=process_job, args=(job_id, a, b, data), daemon=True).start()
    return jsonify(job=job_id)


@bp.get("/dj/status/<job_id>")
def dj_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="unknown job"), 404
    out = dict(job)
    if "Demucs" in (out.get("stage") or ""):
        import separator as sep_mod
        out["pct"] = sep_mod.progress.get("pct")
    return jsonify(out)


@bp.get("/djmixes/<path:name>")
def dj_file(name):
    return send_from_directory(DJ_DIR, name)
