"""Deezer track metadata (canonical artist/title, genre) with a disk cache.

Keyless public API. Every local file is looked up at most once — results
(including 'not found') persist in webmeta.json across restarts. A couple of
consecutive network failures trip a latch so an offline session doesn't hang
on timeouts for every track.
"""
import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).parent
CACHE_FILE = APP_DIR / "webmeta.json"

_cache = None
_fails = 0          # consecutive network failures; >=2 = assume offline


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            _cache = {}
    return _cache


def _save():
    try:
        CACHE_FILE.write_text(json.dumps(_cache))
    except OSError:
        pass


def dz_get(url, timeout=6):
    """GET a Deezer public-API URL -> parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "UnMix/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def clean_query(name: str) -> str:
    """Filename -> search query: drop extension, bracket junk, deck prefixes
    ('A - '), and downloader boilerplate words."""
    q = Path(name).stem
    q = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", q)
    q = re.sub(r"^[A-Za-z]\s*-\s*", "", q)
    q = re.sub(r"(?i)\b(official|audio|video|lyrics?|explicit|version|"
               r"visualizer|remastered|hd|4k)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


GENRE_FAMILIES = (
    ("electronic", ("dance", "electro", "house", "techno", "edm", "trance",
                    "dubstep", "drum & bass", "drum and bass")),
    ("rap", ("rap", "hip hop", "hip-hop", "trap", "grime")),
    ("rock", ("rock", "metal", "punk", "alternative", "indie", "grunge")),
    ("rnb", ("r&b", "soul", "funk", "disco")),
    ("latin", ("latin", "reggaeton", "salsa")),
    ("reggae", ("reggae", "dancehall", "ska")),
    ("jazz", ("jazz", "blues")),
    ("classical", ("classical", "soundtrack", "films/games")),
    ("country", ("country", "folk", "singer & songwriter")),
    ("pop", ("pop",)),
)


def genre_family(genre):
    g = (genre or "").lower()
    for fam, keys in GENRE_FAMILIES:
        if any(k in g for k in keys):
            return fam
    return None


PREVIEW_FILE = APP_DIR / "preview_cache.json"
_prev = None


def _prev_load():
    global _prev
    if _prev is None:
        try:
            _prev = json.loads(PREVIEW_FILE.read_text())
        except Exception:
            _prev = {}
    return _prev


def preview_features(track_id, url):
    """Measure BPM / key / timbre from a candidate's 30s preview clip.

    Deezer reports bpm=0 for most tracks and never reports key, so ranking web
    suggestions on its metadata alone is guesswork. The clip is ~470KB and
    takes under a second to fetch and analyze, and results are cached by track
    id so each candidate is only ever downloaded once."""
    import tempfile
    import analysis
    key = str(track_id)
    c = _prev_load()
    if key in c:
        return c[key] or None
    if not url:
        return None
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="dzprev_")) / f"{key}.mp3"
        urllib.request.urlretrieve(url, tmp)
        a = analysis.analyze(tmp)
        sig = analysis.style_signals(tmp)
        feat = {"bpm": a.get("bpm"), "camelot": a.get("camelot"),
                "onset_density": sig["onset_density"],
                "low_ratio": sig["low_ratio"], "mid_ratio": sig["mid_ratio"],
                "timbre": [round(float(x), 6) for x in analysis.timbre_vec(tmp)]}
    except Exception:
        return None
    finally:
        # the clip is scratch: never leave it behind
        if tmp is not None:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    c[key] = feat
    try:
        PREVIEW_FILE.write_text(json.dumps(c))
    except OSError:
        pass
    return feat


def track_meta(path: Path):
    """Identify a local file on Deezer: {'artist','title','genre','family',
    'artist_id','track_id'} or None. Disk-cached by name+size; network errors
    return None without caching so a later online run can fill them in."""
    global _fails
    try:
        key = f"{path.name}:{path.stat().st_size}"
    except OSError:
        return None
    c = _load()
    if key in c:
        return c[key] or None
    if _fails >= 2:
        return None
    q = clean_query(path.name)
    if not q:
        c[key] = None
        _save()
        return None
    try:
        data = (dz_get("https://api.deezer.com/search?q="
                       + urllib.parse.quote(q) + "&limit=1").get("data") or [])
        meta = None
        if data:
            t = data[0]
            # sanity: the found artist or title must appear in the filename,
            # otherwise Deezer matched something unrelated
            fslug = slug(path.name)
            if slug(t["artist"]["name"]) in fslug or slug(t["title"]) in fslug:
                genre = None
                try:
                    alb = dz_get(f"https://api.deezer.com/album/{t['album']['id']}")
                    gs = (alb.get("genres") or {}).get("data") or []
                    genre = gs[0]["name"] if gs else None
                except Exception:
                    pass
                meta = {"artist": t["artist"]["name"], "title": t["title"],
                        "genre": genre, "family": genre_family(genre),
                        "artist_id": t["artist"]["id"], "track_id": t["id"]}
        _fails = 0
        c[key] = meta
        _save()
        return meta
    except Exception:
        _fails += 1
        return None
