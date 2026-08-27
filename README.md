# UnMix

A local web app for taking music apart: download from YouTube, split songs into
stems with AI, make real karaoke videos with synced lyrics, convert and trim.
Everything runs on your machine — no accounts, no uploads to third parties.

## Features

1. **YouTube Downloader** — paste a link, pick a quality from the format table
   (mp3 320k/128k, mp4 1080p/720p/480p/360p with file sizes). Downloaded audio
   gets one-click "Extract sound →" and "Karaoke" buttons.
2. **Sound Extraction** — drop a song, pick a sound (vocals / drums / bass /
   other; the Extended 6-stem model adds guitar & piano, or "All instruments"
   for every stem as its own file), get it isolated plus the track without it.
   Powered by [Demucs](https://github.com/adefossez/demucs).
3. **Convert & Trim** — drop any audio/video file, choose mp3 (320/192/128k),
   wav, flac, or m4a, optionally set start/end times to cut a piece out.
4. **Karaoke Mode** (its own window) — drop a song or send one from the
   YouTube panel, tick what to mute, and it renders an mp4: black background,
   lyrics in sync, each word lighting up yellow as it is sung, next line
   previewed in gray. Real lyrics are fetched from LRCLIB when available and
   word timing is measured from the vocal track by Whisper; unknown songs fall
   back to full AI transcription.

Long operations (downloads, separation, transcription) show live progress
percentages. Downloads are saved to `downloads/`, stems to `separated/`,
karaoke videos to `karaoke/`, conversions to `converted/`.

## Setup (macOS)

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py     # opens http://localhost:5555
```

The built frontend ships with the repo, so nothing needs installing for Node.

The first separation downloads the Demucs weights (~80-300 MB) and the first
karaoke run downloads the Whisper large-v3-turbo weights (~1.6 GB); after that
everything runs offline. Separation uses the Apple GPU (MPS) when available.

## Frontend

The interface is a React app (Vite) in `web/`; Flask serves the production
build out of `web/dist` on `/` and `/karaoke`, and owns every API route
(`/yt`, `/separate`, `/convert`, `/karaoke/*`). That build is committed, so a
plain `python app.py` needs no Node toolchain.

To change the UI:

```bash
cd web
npm install
npm run dev      # http://localhost:5173, API calls proxied to :5555
npm run build    # refresh web/dist, which is what app.py serves
```

Run `python app.py` alongside `npm run dev` — the dev server proxies the API
to it, so separation, downloads and karaoke all work while hot-reloading.
Commit the rebuilt `web/dist` along with your source changes.

## Command line

There is also a standalone CLI for vocal extraction:

```bash
.venv/bin/python extract_vocals.py song.mp3
```

Options: `--out results/`, `--model htdemucs_ft` (higher quality, ~4x slower),
`--mp3`. Works with mp3, wav, flac, m4a and most other audio formats; multiple
files can be passed at once.
