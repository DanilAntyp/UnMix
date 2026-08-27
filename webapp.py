"""Serving of the React frontend that lives in web/.

`npm run build` (in web/) writes web/dist; that build is what Flask hands out
on / and /karaoke. During frontend work `npm run dev` serves the same app on
:5173 and proxies every API route back here.
"""
from pathlib import Path

from flask import Blueprint, send_from_directory

APP_DIR = Path(__file__).parent
DIST = APP_DIR / "web" / "dist"

bp = Blueprint("web", __name__)

NOT_BUILT = """<!doctype html>
<html><head><meta charset="utf-8"><title>UnMix</title></head>
<body style="background:#07070c;color:#a8a4c4;font-family:system-ui;
             display:flex;align-items:center;justify-content:center;height:95vh">
<div style="text-align:center;max-width:34rem">
  <h1 style="color:#f4f2ff">The UnMix frontend has not been built yet</h1>
  <p>Run this once, then reload:</p>
  <pre style="background:#12121c;padding:14px 18px;border-radius:12px;color:#ffc233;
              display:inline-block;text-align:left">cd web
npm install
npm run build</pre>
</div>
</body></html>"""


def page():
    """The single-page app's index.html (both screens are served from it)."""
    if (DIST / "index.html").is_file():
        return send_from_directory(DIST, "index.html")
    return NOT_BUILT, 503


@bp.get("/assets/<path:name>")
def assets(name):
    return send_from_directory(DIST / "assets", name)


@bp.get("/favicon.svg")
def favicon():
    return send_from_directory(DIST, "favicon.svg")
