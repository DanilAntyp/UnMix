"""Export blind listening clips against a listener-supplied local reference.

No professional recordings are downloaded or fabricated. This is a listening
experiment, not an automatic score of musical quality or a training dataset.
"""
import argparse
import json
import random
import shutil
from pathlib import Path

import dj
import mixengine


def export_pack(a, b, tokens, reference, out, seed=None):
    a, b, reference, out = map(Path, (a, b, reference, out))
    if not reference.is_file():
        raise ValueError('Supply a local reference transition clip you want to compare against')
    entries = []
    for token in dict.fromkeys(tokens):
        plan, _ = mixengine.load_candidate(token, a, b)
        preview = dj.DJ_DIR / f'preview_{token}_automix.mp3'
        if not preview.is_file():
            raise ValueError('Candidate preview is missing; generate it again')
        entries.append(dict(path=preview, kind='candidate', candidate=token, plan=plan))
    if not entries:
        raise ValueError('Choose at least one saved candidate')
    entries.append(dict(path=reference, kind='listener-supplied reference'))
    random.Random(seed).shuffle(entries)
    # Refuse to overwrite any previous experiment or user files.
    out.mkdir(parents=True, exist_ok=False)
    answers = []
    for index, entry in enumerate(entries):
        label = f'version_{index + 1:02d}{entry["path"].suffix.lower()}'
        shutil.copyfile(entry['path'], out / label)
        answers.append({**entry, 'path': str(entry['path'].resolve()), 'file': label})
    manifest = dict(a=mixengine.fingerprint(a), b=mixengine.fingerprint(b),
                    mixer_version=mixengine.VERSION, versions=answers)
    (out / 'answers.json').write_text(json.dumps(manifest, indent=2))
    (out / 'LISTEN.txt').write_text(
        'Listen before opening answers.json. Use the same playback volume.\n'
        'Rate each version for vocal continuity, groove, energy and overall preference.\n'
        'Mark ties and bad versions; do not force a winner. Repeat on unseen song pairs.\n'
        'The supplied reference should cover a comparable transition and listening duration.\n'
        'Files are unchanged: different loudness/length can bias this comparison.\n'
        'These verdicts are not automatically imported as candidate likes/dislikes.\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('a', type=Path)
    parser.add_argument('b', type=Path)
    parser.add_argument('--candidate', action='append', required=True)
    parser.add_argument('--reference', type=Path, required=True,
                        help='Local listener-approved or human-DJ transition clip')
    parser.add_argument('--out', type=Path, required=True, help='New experiment directory')
    args = parser.parse_args()
    try:
        result = export_pack(args.a, args.b, args.candidate, args.reference, args.out)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f'{len(result["versions"])} blind versions saved to {args.out}')


if __name__ == '__main__':
    main()
