"""Shared, lazily-loaded Demucs separators (one per model name)."""
import threading

sep_lock = threading.Lock()  # demucs: one separation at a time
_separators = {}

# Progress of the separation currently running (separations are serialized by
# sep_lock, so a single slot is enough). Read by the /progress endpoints.
progress = {"pct": 0.0}


def _callback(info):
    try:
        length = info.get("audio_length") or 0
        if length and info.get("state") == "end":
            pct = 100.0 * info.get("segment_offset", 0) / length
            progress["pct"] = round(min(99.0, pct), 1)
    except Exception:
        pass


def get_separator(model: str = "htdemucs"):
    """htdemucs: 4 stems (vocals/drums/bass/other), best quality.
    htdemucs_6s: 6 stems (adds guitar and piano), guitar/piano are weaker."""
    if model not in _separators:
        import torch
        from demucs.api import Separator
        device = "mps" if torch.backends.mps.is_available() else (
            "cuda" if torch.cuda.is_available() else "cpu")
        _separators[model] = Separator(model=model, device=device, callback=_callback)
    return _separators[model]
