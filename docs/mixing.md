# Mixing in UnMix

[← Back to the README](../README.md)

Start with **Auto direction**, **Natural**, and **Auto length**. Two-deck
Auto/AutoMix offers three distinct modes:

- **Natural** (default): original recordings, phrase-based 16/32-beat blends,
  complete detected vocal lines, and coordinated low-shelf bass transfer.
  Separated stems are used for analysis only, never to rebuild the recording.
- **Club**: the same original-audio renderer with longer 32/64-beat candidates
  and more freedom to choose an instrumental entry.
- **Creative**: the stem-based candidate engine, with original-audio,
  selective-vocal, and separated-instrument methods. Separation can introduce
  audible artifacts; this mode is an explicit choice.

Natural/Club search up to ten placements. They use measured local bar positions,
not rounded BPM metadata, and reject unresolved bar-phase disagreement or
excessive drift. Phrase boundaries, local bass/percussion/intensity and vocal
activity guide placement. The outgoing recording can leave after a complete
line before its next line begins. The incoming recording keeps its first main
section in Natural mode. Whole-track loudness is not a substitute for matching
the local arrangement: a breakdown and a drop are different musical roles.

Local harmony and kick-attack checks, measured level surges/dips and spectral
jumps reject or rank rendered candidates. These are diagnostic heuristics,
not a guarantee of taste, perfect vocal detection, or human-DJ quality.
If no confident blend survives, Natural/Club preserve native-tempo playback
with an 80 ms de-click handover at the end of A instead of forcing an overlap.

**Audition transitions** offers up to three auditions with hidden method names,
plus a native-playback control when a blend is available.
Use **Show mixing methods** to reveal them, or **Export this transition** to
reuse the chosen transition's saved PCM audio without rerunning the search.
Saved auditions live in `djmixes/.candidates/`; changing either source file or
the rendering version requires new previews. Feedback records the specific
candidate and method. Oversampled limiting leaves headroom for MP3 encoding.
Use **Set listening reference** to keep an approved audition for that song pair;
it is included in later comparisons. The playback control is not presented as
a professional DJ reference.

Explicit likes/dislikes can train a small local preference ranker. It stays off
until there are at least 24 distinct candidate ratings across 4 song pairs,
including 6 likes and 6 dislikes, and it beats the majority baseline by at
least 5 percentage points (and reaches 65% accuracy) on held-out pairs. Repeated
votes for one candidate count once; changed feedback invalidates an old model.
Preferences cannot override timing, vocal or level guards. This is not a
pretrained professional-DJ model: no labelled professional mixing dataset is
bundled. Feedback, references and learned weights remain local.

For comparison with a human-made transition you already have locally, export
a blind listening pack (candidate IDs are in the saved audition filenames):

```bash
.venv/bin/python dj_benchmark.py path/to/a.mp3 path/to/b.mp3 \
  --candidate SAVED_CANDIDATE_ID --reference path/to/reference-transition.mp3 \
  --out djmixes/new-listening-experiment
```

Repeat `--candidate` for alternatives. Use comparable clip durations and
loudness; the tool leaves recordings unchanged, shuffles anonymous filenames,
and keeps their identities in `answers.json`. It does not fabricate a pro
reference, automatically judge taste, or train from unlabelled audio.

Musical-section planning uses cached allin1 neural phrases when available,
otherwise local spectral self-similarity boundaries. The optional allin1 model
is not downloaded automatically. Missing weights or failed analysis are reported
and never prevent mixing; the waveform/EQ and listening comparisons still work.
With model weights installed, the first phrase analysis can take a few minutes
per track; subsequent mixes reuse its cache. Playlist stem handovers also use
gradual complementary rhythm fades, while the multi-candidate A/B interface
and the new modes/reference workflow are specific to two-deck Auto/AutoMix.

Blends keep the original musical key and allow at most a 6% tempo change.
The result reports the style actually used, analysis sources and any fallback.
Manual effects remain available; separation and phrase detection are estimates,
so audition unfamiliar pairs before exporting a set.

Suggested markers remain automatic until moved. **Reset to auto** lets the
renderer refine them again. Saved auditions export their exact transition PCM,
cue positions, local tempo ratio and gain without running the planner again.
Manual markers remain subject to safety checks and may be snapped to downbeats.

Audio regression checks (FFmpeg required):

```bash
.venv/bin/python -m unittest test_dj_transitions test_mixengine test_naturalmix -v
```
