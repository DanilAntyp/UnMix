#!/usr/bin/env python3
"""Extract vocals from music using the Demucs neural network.

For each input song, writes two files next to it (or into --out):
  <name>_vocals.wav        - the isolated voice
  <name>_instrumental.wav  - the song minus the voice (karaoke track)

Usage:
  python extract_vocals.py song.mp3 [more songs ...]
  python extract_vocals.py song.mp3 --out results/
  python extract_vocals.py song.mp3 --model htdemucs_ft   # slower, higher quality
"""
import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract vocals from music files.")
    parser.add_argument("inputs", nargs="+", type=Path, help="audio files (mp3, wav, flac, m4a, ...)")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default: next to each input)")
    parser.add_argument("--model", default="htdemucs",
                        help="Demucs model: htdemucs (default, fast), htdemucs_ft (best quality, ~4x slower)")
    parser.add_argument("--mp3", action="store_true", help="save outputs as mp3 instead of wav")
    args = parser.parse_args()

    for f in args.inputs:
        if not f.is_file():
            print(f"error: file not found: {f}", file=sys.stderr)
            return 1

    # Imported here so --help stays instant.
    import torch
    from demucs.api import Separator, save_audio

    device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Loading model '{args.model}' on {device} (first run downloads the weights)...")
    separator = Separator(model=args.model, device=device)

    ext = "mp3" if args.mp3 else "wav"
    for f in args.inputs:
        out_dir = args.out if args.out else f.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nSeparating {f.name} ...")
        origin, stems = separator.separate_audio_file(f)

        vocals = stems["vocals"]
        instrumental = sum(v for k, v in stems.items() if k != "vocals")

        sr = separator.samplerate
        vocals_path = out_dir / f"{f.stem}_vocals.{ext}"
        inst_path = out_dir / f"{f.stem}_instrumental.{ext}"
        save_audio(vocals, str(vocals_path), samplerate=sr)
        save_audio(instrumental, str(inst_path), samplerate=sr)
        print(f"  wrote {vocals_path}")
        print(f"  wrote {inst_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
