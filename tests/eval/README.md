# Eval corpus

This folder holds reference meetings for `meetingmind eval` — the
regression-gate harness that runs the lite-stack pipeline against known-
good audio and reports quality metrics (WER, speaker count, keyword
recall, wall-clock time).

The corpus is intentionally **not committed** to the repo — meeting audio
is private. Fixtures live here locally, are git-ignored, and each user
maintains their own set.

## Why this exists

Phase 4 of the v0.2.x plan: dogfooding discipline. If a PR touches the
pipeline and the lite-stack regresses on these fixtures, you find out
before merging. Without this, "dev on high path → low path becomes
garbage" is structurally invited; with it, every PR is exercised against
the lite stack.

## Layout

```
tests/eval/fixtures/
    panel-meeting/
        audio.wav         # mono 16 kHz; ≤ ~10 min recommended for speed
        reference.json    # optional — schema below
    standup-2026-05/
        audio.wav
        reference.json
```

Each fixture subdirectory must contain `audio.wav` (mono 16 kHz WAV).
`reference.json` is optional but enables WER + keyword-recall metrics:

```json
{
  "transcript": "Full reference transcript as a single string.",
  "speaker_count": 3,
  "expected_keywords": ["RevOps", "Q3 OKR", "Aranza"],
  "min_speakers": 2,
  "max_speakers": 5
}
```

All fields are optional. Without `transcript` you get speaker-count and
wall-clock only.

## Running it

```bash
uv run meetingmind eval
# or against a different corpus location:
uv run meetingmind eval path/to/other/corpus
# or write a JSON report:
uv run meetingmind eval --json eval-report.json
```

Exit code is 0 if every fixture completed without error, 1 otherwise.
Wire that into CI once your corpus is stable.

## Building your first fixture

1. Pick a recorded meeting you've already processed (or generate
   synthetic audio with a TTS tool — easier to control reference text).
2. Convert to mono 16 kHz WAV:
   ```bash
   ffmpeg -i input.m4a -ac 1 -ar 16000 audio.wav
   ```
3. Transcribe by hand for the reference (or use a known-good model and
   accept the transcript as ground truth — "pyannote + mlx-whisper-large"
   is a reasonable baseline if you don't have human-labeled data).
4. Drop both files in `tests/eval/fixtures/your-slug/` and run
   `meetingmind eval`.

## Empty corpus is fine

The harness handles "no fixtures" gracefully — runs cleanly, reports
"No fixtures found," exits 0. You can check in the harness code without
committing audio, then grow your fixture set over time.
