"""Labeled song structure via the allin1 neural analyzer (verse/chorus/...).

Heavy (demucs + transformer per track), so results are cached on disk keyed by
file mtime. Callers that must stay fast (e.g. the editor's inspect endpoint)
use get_cached() and fall back to heuristics until a render has warmed the
cache. Requires natten_compat, which restores the legacy NATTEN API."""
import hashlib
import json
from pathlib import Path

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "allin1_cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cache_file(path: Path) -> Path:
    p = Path(path)
    key = hashlib.md5(f"{p.resolve()}::{p.stat().st_mtime}".encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def get_cached(path: Path):
    cf = _cache_file(path)
    if cf.exists():
        return json.loads(cf.read_text())
    return None


def compute(path: Path, device: str = "cpu"):
    cached = get_cached(path)
    if cached is not None:
        return cached
    import natten_compat
    natten_compat.install()
    import allin1
    r = allin1.analyze(str(path), device=device,
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
