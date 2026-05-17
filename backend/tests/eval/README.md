# Eval harness

Calibration scaffolding for meeting-output features. Lives outside the
normal unit-test directory because eval tests are slower, focused on
**quality** rather than correctness, and built around realistic
fixtures rather than minimal edge cases.

## What's here

- `harness.py` — fixture loader and runner. Seeds a fixture's segments
  / decisions / chapter_markers into a fresh SQLite DB, then runs a
  named pipeline step against it (currently: deterministic drivers,
  meeting health). LLM-judged steps can be plugged in but are gated by
  `MEETINGMIND_EVAL_REAL_LLM=1` so the default run is offline.
- `scoring.py` — match algorithms (set overlap, IoU on segment_ids,
  bucket-equality for categorical fields).
- `fixtures/*.py` — realistic meeting fixtures with **expected** values
  captured alongside the segment data. Each fixture is a Python module
  exporting `FIXTURE: Fixture` so the data, expectations, and provenance
  notes live in one place.
- `test_eval_drivers.py`, `test_eval_meeting_health.py` — pytest entry
  points that iterate fixtures and assert quality thresholds.

## Why fixtures instead of mocks

The deterministic compute functions (`compute_drivers_and_cog`,
`compute_meeting_health`) have plenty of unit-test coverage already —
edge cases, threshold boundaries, schema variants. What's missing is
**realistic calibration**: do the thresholds we picked actually feel
right when run against a 10-minute meeting with mixed speech patterns?
Are pivot-question detections precise on real conversational rhythm?
Does the standout rule fire only when it should?

Fixtures are the unit of truth for that question. Each captures a
hand-labeled "this is what a reasonable human reviewer would call the
driver moments" — and the harness scores the pipeline's output against
those labels. Regressions in threshold tuning or compute logic fail
loudly.

## Adding a fixture

1. Add `backend/tests/eval/fixtures/<name>.py` exporting `FIXTURE`.
2. The fixture lists segments (speaker, text, start_ms, end_ms),
   optional chapter_markers, optional decisions, optional confirmed
   speaker assignments, and **expected** driver kinds + standout
   speaker (or `None` when the boring "top talker == top driver"
   case applies).
3. Add the import + iteration to `test_eval_drivers.py` /
   `test_eval_meeting_health.py`.

Keep fixtures small (10–50 segments). The point is variety, not size.

## Running with a real LLM

Default: harness mocks LLM calls so `pytest backend/tests/eval/` runs
offline in <1s.

To exercise real LLM passes (slow + costs budget):

```bash
MEETINGMIND_EVAL_REAL_LLM=1 uv run pytest backend/tests/eval/
```

That flag is checked at fixture load time; the eval harness will spin
up a real ModelBus and call the configured `quality_model` against
each fixture's transcript. Use sparingly; gate on prompt iterations.
