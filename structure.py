"""Labeled song structure via the allin1 neural analyzer (verse/chorus/...).

Heavy (demucs + transformer per track), so results are cached on disk keyed by
file mtime. Callers that must stay fast (e.g. the editor's inspect endpoint)
use get_cached() and fall back to heuristics until a render has warmed the
cache. Requires natten_compat, which restores the legacy NATTEN API."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "allin1_cache"
CACHE_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()
_attempted = set()


def _compute_isolated(path, timeout=180):
    """Bound the whole analysis process tree, including Demucs children."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(Path(path).resolve())],
        env={**os.environ, "HF_HUB_OFFLINE": "1", "MPLCONFIGDIR": str(CACHE_DIR / "matplotlib")},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=os.name == "posix")
    try:
        proc.communicate(timeout=timeout)
        return proc.returncode == 0
    except BaseException:
        # This process group was created exclusively for this analysis.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.communicate()
        raise


def _cache_file(path: Path) -> Path:
    p = Path(path)
    key = hashlib.md5(f"{p.resolve()}::{p.stat().st_mtime}".encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def get_cached(path: Path):
    cf = _cache_file(path)
    if cf.exists():
        try:
            return json.loads(cf.read_text())
        except (OSError, ValueError):
            return None
    return None


def compute(path: Path, device=None):
    cached = get_cached(path)
    if cached is not None:
        return cached
    import natten_compat
    natten_compat.install()
    import torch
    if device is None:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    import allin1
    r = allin1.analyze(str(path), device=device, model="harmonix-fold0", multiprocess=False,
                       demix_dir=str(CACHE_DIR / "demix"),
                       spec_dir=str(CACHE_DIR / "spec"),
                       keep_byproducts=False)
    data = {
        "bpm": float(r.bpm),
        "downbeats": [round(float(t), 3) for t in r.downbeats],
        "segments": [{"start": round(float(s.start), 2), "end": round(float(s.end), 2),
                      "label": s.label} for s in r.segments],
    }
    _cache_file(path).write_text(json.dumps(data))
    return data


def for_mix(path, compute_missing=False):
    """Use neural phrases when available, otherwise spectral section boundaries.

    Neural analysis runs with a time limit. Missing optional weights never block
    mixing or silently start a download; the caller reports the analysis source.
    """
    import analysis
    p = Path(path)
    key = (str(p.resolve()), p.stat().st_mtime)
    cached = get_cached(p)
    reason = None
    if not cached and compute_missing:
        with _lock:
            if key not in _attempted:
                _attempted.add(key)
                try:
                    from huggingface_hub import try_to_load_from_cache
                    weight = try_to_load_from_cache("taejunkim/allinone", "harmonix-fold0-0vra4ys2.pth")
                    if not isinstance(weight, str):
                        reason = "optional neural phrase model is not installed"
                    else:
                        cached = get_cached(p) if _compute_isolated(p) else None
                        if not cached:
                            reason = "neural phrase analysis unavailable"
                except (ImportError, OSError, subprocess.TimeoutExpired):
                    reason = "neural phrase analysis unavailable"
    energy, win = analysis.energy_profile(p)
    if cached and cached.get("segments"):
        sections = []
        for s in cached["segments"]:
            seg = energy[int(s["start"] / win):max(1, int(s["end"] / win))]
            sections.append({**s, "energy": float(seg.mean()) if len(seg) else 0.0})
        return {"sections": sections, "source": "neural", "downbeats": cached.get("downbeats")}
    return {"sections": analysis.detect_sections(p), "source": "spectral",
            "note": reason or "spectral phrase boundaries", "downbeats": None}


if __name__ == "__main__":
    compute(Path(sys.argv[1]))
