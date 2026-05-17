# Changelog

All notable changes to MeetingMind go here. The format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning is [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.14] — 2026-05-14

User-observed bug after v0.2.13 went live on a real business meeting:
"Brent, thanks for joining" at 24:05 incorrectly bound Brent to
Speaker 1 (the host who just said "There he is"). The
`vocative_thank` rule assumed the addressee was the previous speaker
— but for a JOIN event, the addressee hadn't spoken yet.

### New rule: join-event → new speaker

- `_JOIN_EVENT_PATTERN` matches "thanks for joining", "glad you
  joined", "welcome to/aboard", "you just joined" — distinct from a
  generic "thank you for that" vocative.
- New mention kind `join_event` (weight 5.0 — stronger than plain
  `vocative_thank`).
- Scoring rule: bind to the FIRST NEW SPEAKER after the welcome
  event, defined as a speaker whose first appearance is at or after
  the welcome segment.
- `_detect_out_of_meeting` treats `join_event` as strong in-meeting
  evidence alongside `welcome` / `vocative_thank`.

Verified on the v0.2.13 reference meeting:
- BEFORE: Speaker 1 → Brent (wrong — Speaker 1 was host throughout)
- AFTER: Speaker 5 → Brent (correct — Brent's first words at 24:44)

### PII sanitization sweep

Pre-merge audit removed real-looking proper names from source
docstrings, test fixtures, and comments. All replaced with
fictional placeholders (Aranza → Alice, Yvette → Carol, etc.).
Verified: zero hardcoded secrets, no SQL injection surface, no
path-traversal-from-request, CORS still rejects public-internet
origins, CSRF still blocks mismatched-origin POSTs. Only
`.env.example` tracked.

## [0.2.13] — 2026-05-14

**Pass E — deductive speaker-identity resolver.** Implements
NLI-style constraint propagation over the transcript to auto-bind
`speaker_id` → name. Combines v0.2.12's regex candidates as
pre-classified evidence with a fresh scan that catches 3rd-person
and future-tense markers for exclusion.

### Rules

1. **Self-reference exclusion** (hard, -100): people rarely say
   their own name; if Speaker X says Y, Speaker X ≠ Y.
2. **Vocative → next speaker** (+3, +4 panelist boost): "Y, I
   think..."
3. **Vocative-thank → previous speaker** (+4): "Y, thanks for that"
4. **Welcome → next speaker** (+4): "Welcome Y"
5. **3rd-person reference → exclude name from meeting**: "Y is
   charged", "she'll lead"
6. **Future-tense reference → exclude**: "I'll talk to Y tomorrow"
7. **Past in-meeting → diffuse +1**: "Y said earlier"
8. **Self-introduction** (replay from v0.2.12, +5): "I am Y"

Greedy assignment picks the highest-score (speaker, name) pair,
locks both, repeats. Uses v0.2.11 three-tier auto-apply model so
confident bindings apply silently via `approve_speaker_label`.

Positive-filter name lexicon: ~500 common English first names
(curated list) + v0.2.12 candidates. Prevents geo names like
"Oklahoma" and contractions like "Isn't" from being treated as
candidates.

Live test on real business meeting:
- Speaker 4 → Alex   (score 9, conf 0.95) — multi-vocative
- Speaker 1 → Brent  (score 4, conf 0.82) — vocative_thank
- Speaker 3 → Becky  (score 3, conf 0.74) — vocative_address

Ambiguous cases (Scott, conflicting evidence) correctly punt to
manual review rather than guess wrong.

### Auto-apply path

Each identity assignment classified into silent / toast / manual
tiers; silent + toast call `approve_speaker_label` inline to
create the `people` row and update `speaker_assignments`. Insert
+ apply split across separate `connect()` blocks to avoid SQLite
write-lock deadlock.

## [0.2.12] — 2026-05-14

**Business-meeting vocative patterns.** v0.2.10 patterns were tuned
for moderated panels ("Question for you, Paul" / "Janet, thank you
for..."). On a real business meeting they caught 0 of 28 name
mentions. Pattern expansion:

1. Vocative-then-question — "Alex, I don't know if you've done
   this", "Scott, you also have", "Pam, tell us what". Binds to
   NEXT speaker.
2. Embedded vocative after conversational filler — "you know scott
   i was telling him", "hey brett, like that".
3. `vocative_thank` relaxed to allow hey/oh/well/listen prefix —
   "Oh, hey, Brent, thanks for joining."
4. `host_intro` relaxed to allow no-period — "Pat Smith is charged
   with..." — but only with identity-style verbs ("is charged",
   "is the lead", "serves as") so geo names like "Clark County is"
   don't false-match.

Plus new `_NAME_PATTERN_ANY_CASE` (case-insensitive first letter)
to handle ASR's unreliable name capitalization in casual speech.
Safety provided by an expanded `STOP_NAMES` set covering common
discourse markers and filler words.

Live test on real business meeting: 4 clean candidates (Becky,
Scott, Brent, Shanda), zero false positives. Previously 0.

## [0.2.11] — 2026-05-14

The "upload-and-walk-away" release. The v0.2.10 manual-test surfaced a
fundamental UX problem: with 7 segment-split proposals + reattribution
proposals on a single 38-min meeting, the user had to triage 10+ items
before seeing a useful summary. The product target is *click upload →
occasionally rename a speaker* — anything else is plumbing the user
shouldn't see.

### Three-tier auto-accept for repair proposals

Each Pass C (speaker reattribution) and Pass D (segment split)
proposal is now classified by confidence:

| Tier | Confidence | Behavior |
|---|---|---|
| **silent** | ≥ `auto_apply_silent_threshold` (default 0.90) | Applied immediately. Logged in the audit panel; user never sees a banner. |
| **toast** | ≥ `auto_apply_toast_threshold` (default 0.70) | Applied immediately. Inline "✓ N corrections applied automatically" notice starts expanded so the user can spot-check. |
| **manual** | below toast threshold | Lands in the existing review banner as in v0.2.10. |

`auto_apply_enabled = False` in `config/local.toml` reverts to the
old behavior (every proposal manual). Default is on.

**New `AutoAppliedRepairsNotice` component** renders above the
manual-review banners with a `▾` chevron to expand the list. Each row
shows the confidence percentage and a one-line description of what
was changed (e.g. "seg 24 split → tail to Speaker 2").

**Auto-apply failure path:** if the inline apply raises (e.g. segment
drift between persist and apply), the proposal is demoted to manual
review automatically — same as if it had been a low-confidence
proposal to begin with.

**Logging:** the pipeline now emits `INFO segment-split: total=N
auto_applied=N manual=N` and the equivalent for reattribution, so the
operator can see what was done without opening the dashboard.

### Repo hygiene: PII removed from test fixtures

Hardcoded owner-name references in `test_obsidian_writer.py` and
`test_speaker_learning.py` renamed to generic "Owner". The repo
already gitignores all user data (vault, DB, audio, vocab files,
local config), but the test code shouldn't bake in the project
author's name. Verified by sweep:

- `git ls-files` returns zero `.wav` / `.m4a` / `.toml` / `.sqlite`
  user-data files
- `config/local.*`, `data/`, `runtime/`, `vault/`, `.env.local` all
  excluded via `.gitignore`
- Only remaining owner-name mentions are in README badges, project URLs,
  and `.github/CODEOWNERS` — appropriate for a public project's
  ownership metadata.

### Tests

**269 total passing** (+4 new):
- `test_auto_apply_silent_tier_writes_resolved_review_item`
- `test_auto_apply_toast_tier_tags_payload`
- `test_auto_apply_below_thresholds_stays_manual`
- `test_auto_apply_disabled_keeps_v0210_behavior`

## [0.2.10] — 2026-05-14

Largest release of the v0.2.x repair series. Closes the gaps surfaced
by the v0.2.9 post-merge audit (route tests, type cleanup) AND the
real-world findings from the panel-discussion test session
(handoff-naming regex misses, no rename UI, diarizer boundary leak).
Six independent improvements; all are conservative — proposes, never
auto-applies.

### Phase 1: panel-discussion handoff naming v2

The original `speaker_identity` regexes only caught self-introductions
and `"question for {Name}"` with the name immediately after the
trigger. They missed three patterns that show up constantly in
moderated panels:

| Pattern                                            | Now caught | Evidence type |
|----------------------------------------------------|------------|---------------|
| `"Question for you, Paul"` (filler between trigger and name) | ✓ | response_after_direct_address |
| `"Janet, thank you for that introduction"` (vocative-first) | ✓ | vocative_thank |
| `"Paul Krugman. He serves as research professor at..."` (host introduces panelist; pool boosts later direct-address) | ✓ | response_after_direct_address_panelist |
| `"I'm sitting on the panel..."` → fake "Sitting" | ✗ false-positive killed | (STOP_NAMES) |

`_NAME_PATTERN_CI_SAFE` uses `(?-i:[A-Z])` to keep the name's first
letter case-sensitive even when the surrounding pattern is compiled
with `re.IGNORECASE`. Otherwise lowercase trigger words would catch
common nouns as "names" (`"question for everyone"` → `everyone`).

Verified against the real v0.2.10 panel-discussion test transcript: 3
correct detections (Janet / Durang / Paul), 0 false positives. New
`naming-report` CLI command prints the extractor's output on any
meeting for before/after diff.

### Phase 2: People-page rename UI

The People-detail page had Delete and Set-as-owner buttons but no way
to rename a person — the only rename surface was inside the
segment-level `SpeakerEditModal`, hidden behind clicking a speaker
chip on a transcript row. Even when discoverable, that path only
renamed the speaker_assignment for one meeting; the orphan Person
record stayed in the directory.

New `POST /api/people/{id}/rename?new_name=...` cascades the new label
across every meeting that references the person. Two outcomes:

  - **renamed** — no other person already has the target name; updates
    `people.display_name` plus every `speaker_assignments.approved_label`
    in one transaction.
  - **merged** — the target name belongs to a different person row;
    repoints every FK (speaker assignments, transcript segments,
    action items, profile observations) to the target, then deletes
    the source row. Owner config migrates automatically if the
    renamed person was the configured "you".

Frontend: ✎ Rename button on the People-detail page. Inline edit
input (Enter to save, Esc to cancel). Surfaces the
renamed-vs-merged outcome in the status toast.

### Phase 3: Pass D — segment-split repair

The diarizer's boundary detection lags real conversational
turn-taking by ~1–2 seconds. faster-whisper transcribes the whole
window as a single segment, so the next speaker's first few words get
stitched onto the *previous* segment. Concrete example from the
v0.2.10 panel:

  > seg 15 [16:05] Speaker 2 (Jan): "...might some of them regret it?
  > **Okay, so I am of**"
  > seg 16 [16:32] Speaker 4 (Paul): "the opinion that we are
  > witnessing..."

"Okay, so I am of" is Paul, not Jan. Both segments have very low
speaker confidence (0.25 and 0.45) but v0.2.4's reattributer only
proposed *whole-segment* relabels — it had no way to surface a
boundary fix.

New `repair.segment_splitter` (Pass D) scans every segment with
`speaker_confidence < repair.segment_split_min_confidence` (default
0.55) for a discourse-opener pattern late in the text
(`"Okay, so ..."`, `"Yeah, well ..."`, `"I think the ..."`). When
found, uses `transcript_words` for the word-level split timestamp and
proposes a `review_item` of kind `segment_split_proposal`.

Backend endpoints `POST /api/meetings/{id}/review-items/{rid}/{accept,reject}-split`
apply the proposal atomically: shrink the head segment's end_ms +
text, insert a new tail segment with the proposed `tail_speaker_id`
(the diarizer's own next-segment speaker), repoint any
`transcript_words` that fall after the split.

Frontend `SplitProposalsBanner` surfaces proposals in the Review view
alongside the v0.2.8 reattribution banner with consistent UX.

### Phase 4: `naming-report` CLI

`meetingmind naming-report <meeting_id>` prints which name candidates
the extractor finds on a given meeting and why each fired. Pure
read-only, doesn't touch the DB. Designed for before/after
verification when adjusting the regex set or `_STOP_NAMES`.

### Audit fixes (pre-merge Sonnet pass)

- **H1**: `POST /api/people/{id}/rename?new_name=...` now caps the name
  at 1–200 chars via `Query(..., min_length=1, max_length=200)`.
  Without it, a multi-MB request would cascade into every
  `speaker_assignments.approved_label` row. Paired with a 422-returning
  route test and a `maxLength={200}` on the frontend rename input (L4).
- **H2**: `accept_split_proposal` now refuses with `segment_changed`
  (409) when the live segment text has drifted from the proposal's
  captured `head_text + tail_text` (normalized for whitespace).
  Otherwise accepting a stale proposal after a manual edit would
  silently overwrite the user's edits.
- **M1**: `rename_person` was already migrating owner config; tests
  now exercise both rename-in-place and merge owner paths.
- **M3**: race on concurrent accept-split. The status flip is now a
  conditional `UPDATE ... WHERE id=? AND status='open'`; if rowcount
  is 0 we raise `already_resolved` before doing any segment writes —
  so two simultaneous accepts can't double-apply.
- **M5**: pipeline smoke test for `persist_segment_split_proposals`
  on a meeting with no segments (the empty-input shape the pipeline's
  try/except would otherwise silence).
- **M6**: accept-split test path on a segment with zero
  `transcript_words` rows — covers the proportional-fallback branch
  of `_locate_split_ms`.

### Phase 5: route integration tests + OverlapHint cleanup (from earlier in v0.2.10)

Closes the gaps surfaced by the v0.2.9 post-merge audit: a missing
FastAPI integration-test harness, a too-loose union on `OverlapHint.kind`,
and a `partner_segment_id` field that was on the wire but unused.

### New: FastAPI TestClient harness + `GET /api/meetings/{id}` coverage

`backend/tests/test_routes_meeting_detail.py` boots a `TestClient`
against a tmp-path SQLite DB, monkeypatches `routes.load_config`, and
stubs the synthesis / overview / transcript-markdown builders so the
test stays hermetic (no LLM, no Obsidian vault). Three tests:

1. `test_meeting_detail_returns_overlap_hints` — seeds two segments
   with three hints (two on the same segment) and asserts the v0.2.9
   `ORDER BY confidence DESC, kind` contract holds end-to-end. Catches
   regressions where the route either drops the key or reverts to
   non-deterministic ordering.
2. `test_meeting_detail_full_payload_shape` — asserts every key the
   frontend `MeetingDetail` type expects (`meeting`, `segments`,
   `review_items`, `assignments`, `source_file`, `candidates`,
   `speaker_evidence`, `overlap_hints`, `synthesis`, `overview`,
   `transcript_markdown`) is present. This was the v0.2.9 audit's HIGH
   — a refactor dropping any key would have shipped silently.
3. `test_meeting_detail_unknown_meeting_returns_404` — guard on the
   error path.

This is the first FastAPI integration test in the suite; it's
deliberately small and self-contained so the pattern can be copied for
other routes later.

### `OverlapHint.kind` is now a closed union

Changed from `"yield_marker" | ... | string` to `OverlapKind | (string & {})`.
The intersection trick keeps autocomplete and lets exhaustive
ternaries / switches surface a TS error when a new kind is added
backend-side, while still letting unknown values from older clients
flow through at runtime.

### `OverlapBadge` now uses `partner_segment_id`

If the persisted hint has a partner segment, the badge becomes a button
that scrolls the partner row into view (`scrollIntoView({ behavior:
"smooth", block: "center" })`). This makes the badge a navigation
affordance rather than a dead annotation. Badges without a partner
remain inert spans.

## [0.2.9] — 2026-05-14

Surfaces v0.2.2's linguistic overlap-detection results in the transcript UI so reviewers can see at a glance which segments the detector flagged as crosstalk / interruptions / yield handoffs.

### New: overlap hint badges on transcript rows

`GET /api/meetings/{id}` now returns `overlap_hints: OverlapHint[]` alongside `segments` — one row per flagged segment from the `segment_overlap_hints` table. Each entry is `{ segment_id, partner_segment_id, kind, evidence, confidence }`.

In the Review view, `TranscriptView` indexes the hints by `segment_id` (single `useMemo` over `detail.overlap_hints`) and passes the matching hint down to each `TranscriptRow`. Rows whose segment has a hint render a small pill next to the speaker chip:

- `yield_marker` → "⤬ yield" (the "I'm sorry, go ahead" pattern)
- `stutter_interrupt` → "⤬ interrupt"
- `rapid_alternation` → "⤬ crosstalk"

The full evidence string and confidence appear on hover, e.g. `yield_marker · "no, go ahead" · 87%`.

### Why

v0.2.2 shipped the detector and persisted hints to the database, and v0.2.6 wired them into the synthesis prompt's QUALITY HINTS sidebar so the summary LLM would hedge appropriately. But until now the reviewer in the UI had no visibility into which segments the detector had flagged — making it hard to spot-check whether the detector was firing on genuine handoffs or false positives. The badge closes that loop.

### Implementation notes

- `OverlapBadge` is a tiny inline component in `TranscriptRow.tsx`; it only renders when `overlapHint` is defined, so unflagged rows are unchanged.
- The `overlapHint` prop is `OverlapHint | undefined` rather than nullable — `Map.get` returning `undefined` is the natural shape and `React.memo` treats `undefined` as a stable identity across renders.
- The hint map is built in the parent so all rows share the same `Map` instance; a row only re-renders if `detail.overlap_hints` itself changes (typically once per detail fetch).

## [0.2.8] — 2026-05-14

UI surfacing for speaker re-attribution + three MEDIUM audit fixes from the v0.2.6/v0.2.7 audit pass.

### New: `RepairProposalsBanner` in the Review view

When a meeting has open `kind='speaker_reattribution'` review_items, a banner appears at the top of the Review view listing each proposal:

> #12 re-label **Speaker 1** → **Aranza**  85%
> *self-introduction*
> [Apply] [Dismiss]

Apply hits `POST /api/meetings/{id}/review-items/{rid}/accept-reattribution` from v0.2.7 — which updates the transcript via `reassign_segment_speaker` and marks the review item resolved. Dismiss marks it rejected. Up to 12 proposals shown at a time; more are paged in as the user clears the first batch.

### Audit M1: HTTP status codes for review-item routes

`review_item_not_found` now returns **404** (was 400). `already_resolved` and `not_a_reattribution` now return **409** (was 400). Frontend can switch on status to distinguish "your row is gone" from "your row was already handled."

### Audit M2: `reject_reattribution_proposal` is now genuinely idempotent

Previously, calling reject on a row whose status was already `resolved` (i.e., the proposal was already accepted) would silently flip it to `rejected` — the transcript edit was "ghost-applied" but the review item now claimed it was rejected. Now:
- Already `rejected` → no-op (matches docstring).
- Already `resolved` → raises `already_resolved`. The user has to choose: keep the applied label, or manually re-rename via the speaker UI.

### Audit M3: accept flow now atomic

`accept_reattribution_proposal` updated the transcript and then updated the review_item status in two separate transactions. A crash between them left the transcript changed but the review item still `open`, so the next reattributer run could re-propose the already-applied label.

Now: flip status to `resolved` first; then call `reassign_segment_speaker`. If reassign raises, roll the status back to `open` so the proposal remains available for retry. Better failure mode — proposal stays in queue, transcript untouched.

### Test coverage

198 → **201 tests** (+3 audit regressions):
- `test_reject_is_idempotent_on_already_rejected`
- `test_reject_refuses_to_flip_accepted_proposal`
- `test_accept_rolls_back_status_if_reassign_fails`

### Not in this PR

- Overlap-hint badge on transcript segments (v0.2.2 hints persist to `segment_overlap_hints` but the UI doesn't yet show them per-row). Separate frontend pass.
- L1 / L2 / N1-N3 from the audit — cosmetic, deferred.

## [0.2.7] — 2026-05-14

Closes the last open audit MEDIUM (M-C: WeSpeaker ONNX integrity) and ships the missing accept-flow for v0.2.4 speaker re-attribution proposals.

### Audit M-C: WeSpeaker ONNX SHA256 verification

The v0.2.5 fix raised the size floor from 1 MB to 20 MB, but a fully-downloaded-but-corrupted file still passed. Now every download AND every first-load cache check verifies SHA256 against a pinned constant. A corrupt cache file is detected on next process start, removed, and re-downloaded automatically. A bad download is rejected before the file moves into the cache.

Pinned: `7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068` (matches the canonical 26.5 MB file from the HF mirror). If upstream bumps the model, this constant gets updated in the same PR as the URL bump.

### New: speaker-reattribution accept flow

The v0.2.4 reattributer was persisting proposals as `review_items` rows with `kind='speaker_reattribution'`, but **nothing accepted them**. The transcript's speaker labels stayed as the diarizer set them regardless of how many proposals piled up in the review queue. Now:

- `accept_reattribution_proposal(config, meeting_id, review_item_id)` — applies the proposal: updates `transcript_segments.diarization_speaker_id` via the existing `reassign_segment_speaker` path, marks the review item resolved.
- `reject_reattribution_proposal` — marks rejected, transcript untouched.
- Two HTTP endpoints:
  - `POST /api/meetings/{id}/review-items/{rid}/accept-reattribution`
  - `POST /api/meetings/{id}/review-items/{rid}/reject-reattribution`

Frontend wiring (the actual "Accept" button on the review queue card) is the next follow-up — backend contract is now there for it to use.

### Test coverage

191 → **198 tests** (+7):
- `test_wespeaker_integrity.py` (5): hash regression, pinned-vs-real, corrupt cache detection, truncated download rejection, hash-mismatch rejection
- `test_speaker_reattributer.py` (+2): accept-flow updates transcript + marks resolved, already-resolved raises

## [0.2.6] — 2026-05-14

Synthesis-prompt hedging + two follow-up audit fixes (M-A, M-B) from the v0.2.4/v0.2.5 audit pass.

### New: synthesis-prompt hedging (`backend/app/services/repair/quality_hints.py`)

The v0.2.2 overlap detector and v0.2.4 speaker reattributer were persisting hints to the DB, but the synthesis LLM (which writes the actual summary users see) was ignoring them. Now `extraction.py`'s chunk-loop calls `augmented_chunk_text` which appends a short "QUALITY HINTS" sidebar to each chunk's user message:

- **Overlap segments** → "hedge attribution (e.g. 'speakers talked over each other')"
- **Reattribution-flagged segments** → "prefer impersonal phrasing ('one speaker said'); don't invent the alternative name"

Hints are filtered per-chunk: only segments inside the chunk's `segment_ids` range produce hints, so a long meeting's chunks each get only their local context. No-op when no hints apply — zero token overhead for clean meetings.

### Audit M-A (v0.2.4): hallucinated speaker labels filtered

Pre-fix, the system prompt told the LLM "use only speakers from this window" but nothing in code enforced it. A hallucinated name (e.g., proposing "Aranza" when only "Speaker 1" and "Speaker 2" exist) would persist to the review queue. Now `propose_speaker_reattributions` programmatically filters out proposals whose `proposed_speaker` isn't in the window's actual speaker set. Regression test added.

### Audit M-B (v0.2.5 race): vocab corrector DELETE + INSERT atomic

The L3 fix in v0.2.5 split DELETE and INSERT across two `connect()` blocks (two transactions). A crash mid-INSERT would leave the vocab-corrector candidates table empty for the meeting because the DELETE had already committed. Both operations now share one transaction.

### Test coverage

181 → **191 tests** (+10):
- `test_quality_hints.py` (9 new): empty cases, filter to chunk, status filtering, augmented text shape
- `test_speaker_reattributer.py` (+1): `test_propose_filters_out_hallucinated_speakers`

### Deferred from this PR

- **M-C** (WeSpeaker SHA256 verification) — needs a pinned hash, separate PR.
- Three NIT items from the post-fix audit — cosmetic.

## [0.2.5] — 2026-05-14

Audit fixes from the Sonnet pass on v0.2.0–v0.2.3 that ran post-merge (audit was skipped pre-merge — that lapse motivated this cleanup PR).

### H1 — doctor + upgrade tier-aware

`meetingmind doctor` was still telling lite-stack users to install `mlx-whisper` and `pyannote.audio` and offering `uv sync --extra ml` as the `--fix` action — the exact opposite of the v0.2.0 promise. Same for `meetingmind upgrade --deps`. Now both branch on the configured `diarization.provider` + `asr.engine`:

- Lite stack (`foxnose` + `faster_whisper`): checks `faster-whisper` + `diarize` are installed, suggests `--extra ml-lite`, treats the HF token as optional (only required for the pyannote opt-in)
- Pyannote opt-in: same flow as before

### H2 — lite-stack pipeline tests (`test_pipeline_lite.py`)

`test_pipeline.py`'s `_test_config` was force-pinned to `engine=mlx_whisper, provider=pyannote` so the legacy monkeypatch points kept working — which left the default user-facing stack with **zero end-to-end pipeline coverage**. New module exercises the lite stack:

- `test_lite_pipeline_dispatches_to_faster_whisper` — verifies factory dispatch goes through `FasterWhisperProvider` + `FoxnoseDiarizationProvider`, not the legacy paths
- `test_lite_pipeline_persists_segments_and_runs_repair_passes` — end-to-end with mocked providers and mocked LLM calls
- `test_lite_pipeline_repair_pass_failure_does_not_crash_pipeline` — sabotage the speaker-reattribution LLM call, verify the transcript still persists

### M1 — overlap detection no longer false-positives on None speaker

`_detect_rapid_alternation` treated `speakers = ["A", None, "A"]` as `["A", "", "A"]` and fired `rapid_alternation` because empty-string read as a "third speaker." Now skips the window when any speaker label is empty. Regression test added.

### M2 — vocab corrector no longer silently caps vocabulary at 50 terms

The prompt builder enumerated `sorted(set(vocabulary))[:50]` as a "VOCABULARY" block. Users with longer vocab lists got silent degradation because the LLM saw a proposed replacement that wasn't in the block it was shown (and the conservative system prompt biased toward NO in that case). Each candidate already pairs `(original → replacement)`; the LLM only needs to evaluate that specific pair. Dropped the redundant vocab block entirely.

### L1 — WeSpeaker ONNX size floor 1 MB → 20 MB

A partial download between 1 MB and 25 MB would be treated as valid and fail downstream with an opaque ONNX-load error. Floor raised to 20 MB (file is ~25 MB).

### L2 — stutter regex now matches `--` (ASCII double-hyphen)

Whisper outputs `--` more often than `—`. Now matches both. Regression test.

### L3 — stale vocab-correction candidates cleared on re-run

`persist_vocab_correction_candidates` relied on `UNIQUE(meeting_id, segment_id, profile_name)` to avoid duplicates, but never DELETE'd existing `vocab_corrector` rows for the meeting. Removing a vocab term from config and re-ingesting left stale suggestions hanging in the review queue. Now delete-then-insert keyed on `provider = 'vocab_corrector'`. Other providers' rows (e.g. `asr_candidates`) are untouched.

### Test coverage

176 → **181 tests** (+5: lite pipeline ×3, overlap regression ×2).

## [0.2.4] — 2026-05-14

Pass C — speaker re-attribution. The biggest single lever for closing the AMI DER gap on the lite-stack diarizer: text-side reasoning over conversational context can catch speaker-label errors the diarizer makes from acoustics alone.

### New: `backend/app/services/repair/speaker_reattributer.py`

LLM-driven pass that scans transcript windows with current diarization labels and flags segments where context suggests the label is wrong. Signals it leans on:

- **Introductions** — "Welcome, Aranza" → next speaker is Aranza
- **Direct address** — "Yvette, what do you think?" → next speaker is Yvette
- **Q→A flow** — questions almost always followed by a different speaker
- **Continuation cues** — "as I was saying" → same speaker as earlier reference

### Persistence + UI

Proposals land as `review_items` rows with `kind='speaker_reattribution'`. Title is human-readable ("Speaker label: Speaker 1 → Aranza"); `payload_json` carries `{segment_id, current_speaker, proposed_speaker, basis}`. The existing review queue UI surfaces them automatically. **Never auto-applies** — every proposal is accept/reject in the UI.

Idempotent — re-running on a meeting clears prior `speaker_reattribution` items first so old corrections don't accumulate across pipeline re-runs. Other `kind=` items are untouched.

### Config

The four `RepairConfig.speaker_reattribution_*` fields declared (but unused) in v0.2.1 are now wired up:
- `speaker_reattribution_enabled` (default `true`)
- `speaker_reattribution_window_size` (default `12` segments per LLM call)
- `speaker_reattribution_min_confidence` (default `0.6`)
- `speaker_reattribution_max_segments` (default `240` — hard cap so a 2hr meeting doesn't fan out into hundreds of LLM calls)

### Pipeline integration

Runs after `persist_overlap_hints` in `pipeline.process_meeting_audio`, same try/except wrap as other repair passes — model failures never break the pipeline.

### Tests

10 new unit tests in `test_speaker_reattributer.py`:
- Prompt construction (window inclusion, long-segment truncation)
- Dedupe (overlapping windows → highest-confidence wins)
- Disabled / no-segments short-circuits
- LLM mocking: low-confidence filtered, same-as-current filtered, valid proposal kept
- End-to-end persistence to `review_items`
- Clearing stale proposals on re-run (other kinds preserved)

176/176 backend tests passing.

## [0.2.3] — 2026-05-14

Eval harness — `meetingmind eval` runs the lite-stack pipeline against a corpus of fixture meetings with reference transcripts, reports WER + speaker-count match + keyword recall + wall-clock time. Closes the Phase 4 dogfooding discipline gap: every PR can now run the corpus, and CI fails when the lite tier silently regresses.

### New: `backend/app/services/eval/runner.py` + `meetingmind eval` CLI

Corpus format (intentionally minimal):
```
tests/eval/fixtures/
    <slug>/
        audio.wav         # required, mono 16 kHz
        reference.json    # optional — schema in tests/eval/README.md
```

Metrics per fixture:
- **WER** (word error rate via Levenshtein on tokenized words; lowercased, punctuation-stripped) — only if `reference.json` supplies a transcript
- **Detected speaker count vs expected** — surfaces over/under-detection
- **Keyword recall** — for each expected keyword, did it appear in the transcript at all?
- **Wall-clock time** — regression signal for performance changes

Empty corpus → empty report, exit 0. Fixtures are gitignored (private audio); each user maintains their own. README in `tests/eval/` covers the schema and how to build a first fixture.

### CLI

```
meetingmind eval                                  # default: tests/eval/fixtures
meetingmind eval path/to/other/corpus
meetingmind eval --json eval-report.json          # write structured output
```

Exits 1 if any fixture errored (audio missing, provider crashed, etc.), 0 otherwise.

### Tests

9 new unit tests in `test_eval_runner.py`:
- Empty corpus → clean report
- Corpus with subdirs but no audio → error per fixture, not a crash
- End-to-end with mocked providers (verifies WER + speaker count + keyword recall plumbing)
- Standalone `_compute_wer` cases (perfect match, single substitution, punctuation, empty-reference edge)
- `EvalReport.to_json` shape (regression check against CI tooling that reads it)

166/166 backend tests passing.

### What this enables

- Each future PR that touches the pipeline can run `meetingmind eval` before merging
- Wiring into CI as a regression gate is a one-line workflow change — held off in this PR because the corpus is empty by default; once you've added 1-2 fixtures the gate becomes meaningful
- Closes the dev-on-high-path concern from the v0.2.0 plan: the harness is what makes "lite is the default we develop against" structurally enforceable

## [0.2.2] — 2026-05-14

Linguistic overlap detection — meetings where speakers talked over each other are now flagged in the transcript even though the lite-stack diarizer doesn't model acoustic overlap. Pure text-based heuristic, no LLM call required.

### Why

The lite-stack default (FoxNoseTech) assigns each segment to exactly one speaker — overlapping speech is invisible at the acoustic stage. But meetings have strong linguistic markers when overlap happens ("sorry, go ahead" / "no, you first" / stuttering self-interrupt), and those survive into the transcript regardless of which diarizer was used.

### New: `backend/app/services/repair/overlap_inference.py`

Three detection patterns, all heuristic-only in v0.2.2:

1. **Yield markers** — "sorry, go ahead" / "no, you first" / "after you" / "what were you saying" / similar phrases. High precision (0.85 confidence by default).
2. **Stutter / self-interrupt** — "um, I— I was" / "wait, let me". Suggests the speaker was cut off (0.6 confidence).
3. **Rapid alternation** — 3+ adjacent sub-1.5s segments alternating between two speakers (A→B→A). Classic cross-talk fingerprint after diarization. (0.55 confidence.)

For yield/stutter hints, the `partner_segment_id` is computed as the adjacent segment from the *other* speaker — that's the segment the speaker was overlapping with.

### Storage

New `segment_overlap_hints` table:
```sql
(meeting_id, segment_id, partner_segment_id, kind, evidence, confidence, created_at)
```
Idempotent — re-running detection on a meeting clears prior hints first so we don't accumulate duplicates.

### Pipeline integration

`pipeline.process_meeting_audio` runs `persist_overlap_hints` after the vocab corrector pass. Same try/except wrap — a detection failure logs a warning and continues.

### Tests

9 new unit tests in `test_overlap_inference.py`:
- All three yield-marker phrasings detected
- Stutter pattern detected
- Rapid alternation detected (3 short turns A→B→A)
- "Sorry" without yield phrasing NOT flagged (false-positive guard)
- Long alternating turns NOT flagged as cross-talk
- Partner segment correctly resolves to the other speaker

### What's not in this release

- UI indicator on flagged segments — backend hints are now stored, frontend wiring is a separate PR
- Synthesis-prompt hedging on overlap moments — coming in v0.2.x
- Optional LLM gate to filter false positives — scoped if production noise warrants

## [0.2.1] — 2026-05-14

First of three planned LLM repair passes that close the quality gap from v0.2.0's lite-stack default. The vocab corrector catches the most user-visible class of ASR errors: misheard named entities and domain terms.

### New: vocab corrector (`backend/app/services/repair/vocab_corrector.py`)

Two-stage post-ASR pass that proposes corrections for low-confidence words that look like misheard vocabulary terms:

1. **Deterministic phonetic-distance filter.** Scans word-level confidence; for each word below `repair.vocab_correction_min_confidence` (default `0.6`), finds vocabulary terms within `repair.vocab_correction_max_distance` Levenshtein steps (default `3`). Handles multi-word terms via window matching ("Sample Treat" → "Sample Street").
2. **LLM yes/no gate.** Batches candidate substitutions to the configured small model, asks for an `accept`/`reject` decision per candidate with a one-line basis. The LLM never generates new text — it only picks from the deterministic candidates. Conservative system prompt: "when in doubt, say NO."

Accepted corrections are persisted as `transcript_candidates` rows, so the existing review UI surfaces them as accept/reject suggestions. **Never auto-applied** — the user always sees and approves each edit.

### Config

New `RepairConfig` block in `config.py`. All three fields default-on:
- `repair.vocab_correction_enabled` (default `true`)
- `repair.vocab_correction_min_confidence` (default `0.6`)
- `repair.vocab_correction_max_distance` (default `3`)
- `repair.vocab_correction_batch_size` (default `24` — LLM token-budget cap)

### Pipeline integration

`pipeline.process_meeting_audio` runs `persist_vocab_correction_candidates` after speaker/voice-profile candidate persistence and before the optional auto-repair pass. Wrapped in a try/except — a repair-pass failure never breaks the main pipeline (`_LOG.warning`, continue).

### Tests

8 new unit tests in `test_vocab_corrector.py` cover the deterministic stage (phonetic matching, confidence filtering, multi-word vocab) plus the disabled-by-config and empty-vocabulary short-circuit paths. The LLM gate is intentionally not unit-tested — its behavior is validated by integration smoke tests on real meetings.

### What's next

- Pass B (beam-search reranker) — extends existing `asr_candidates` infrastructure
- Pass C (speaker re-attribution) — closes the diarization gap, not just the transcript one
- Linguistic overlap detection ("sorry, go ahead" patterns) — v0.2.2

## [0.2.0] — 2026-05-14

**Breaking default change.** The lite stack (FoxNoseTech diarization + WeSpeaker embeddings + faster-whisper ASR) is now the default. No Hugging Face account required for the standard install path, runs on any platform (Linux / Intel macOS / Windows / Apple Silicon).

The pyannote + mlx-whisper stack stays available as a config opt-in for users who specifically want the higher accuracy (and are willing to set up the HF token + accept the model TOS).

### Default switch

- `diarization.provider`: `pyannote` → `foxnose`
- `diarization.embedding_provider`: `pyannote` → `wespeaker`
- `asr.engine`: `mlx_whisper` → `faster_whisper`

### Why

A live A/B comparison on a real 17-minute, 6-speaker meeting (PR #33) showed:
- Both stacks detect the same 6 speakers
- 99.1% per-second agreement on who-is-talking-when
- Total speech detection within 4%
- Different boundary policy (foxnose merges into fewer/longer turns)

The remaining quality gap (foxnose ~14.96% AMI DER vs pyannote ~9% on multi-speaker benchmarks) is small and will be closed in v0.2.1+ with LLM repair passes (vocab corrector, beam reranker, speaker re-attribution) that benefit both tiers.

### Install + onboarding

- `uv sync --extra ml-lite` is the new default install command (was `--extra ml`)
- `meetingmind install` no longer requires a Hugging Face token
- `meetingmind doctor --fix` pre-fetches the WeSpeaker ONNX (~25 MB) from a public HF mirror so the first ingest doesn't stall
- README rewritten — pyannote-specific steps demoted to a sidebar, install reduced from 5 numbered steps to 4

### Migration

Existing users on pyannote: your `config/local.toml` either explicitly sets `diarization.provider = "pyannote"` or relies on the prior default. If you relied on the default, set the field explicitly OR run `uv sync --extra ml-lite` and accept the new lite stack. Your already-extracted meetings keep their old transcripts — only new ingests use the new pipeline. Re-run `meetingmind reprocess <meeting_id>` if you want a meeting re-transcribed on the new stack.

### Dep fixes (from the lite-stack rollout)

- `torchcodec` pinned to `>=0.7,<0.8` in `ml` and `ml-cpu` extras for PyTorch 2.8 ABI compat. Newer torchcodec requires PyTorch 2.11.
- For the pyannote opt-in path on macOS, `ffmpeg@7` may need to be brew-installed alongside ffmpeg 8 (torchcodec 0.7 supports ffmpeg 4-7 only); document in install wizard.

### What's not in this release

- LLM repair passes (planned v0.2.1)
- Linguistic overlap detection (planned v0.2.2)
- Tier-aware install wizard (deferred — current user base is small enough to not need it)

## [0.1.6] — 2026-05-14

A third Sonnet audit on the v0.1.5 commit found that the perf branch of that release was effectively a no-op for default users — `React.memo` was being defeated by an inline `[]` allocation in the parent. This release fixes that, plus four minor follow-ups; a fourth audit on the fix commit returned "ship it" with two NIT-level polish items, both addressed.

### Perf — the real fix

- **`keyTerms` no longer churns identity every render** (`main.tsx`). Wrapping it in `useMemo` keyed on `[showKeyTermHighlights, detail?.synthesis.key_terms]` is what makes the v0.1.5 `React.memo(TranscriptRow)` + the row's inner `useMemo(highlightImportantText, ...)` actually skip work.
- **`setMessage` wrapped in `useCallback`** with a `backendBouncingRef` carrying the live flag. The v0.1.5 `useCallback`-wrapped `correctSegment` / `refreshAfterSegmentRevert` were closing over a stale `setMessage` (and through it a stale `backendBouncing`). Stable identity now flows correctly through the whole chain.

### Quality

- **`SegmentEditModal` keydown listener** no longer re-registers every keystroke. `draft` / `onSave` / `onClose` are stashed in refs (assigned inline at render top — Dan Abramov's "latest ref" pattern, no `useEffect` lag).
- **`playExactAudioSpan` callbacks** (`onStop` / `onReady` / `onError`) guard with a `mountedRef` so they no-op cleanly if the row unmounts mid-playback.
- **Dropped unused `export` on `escapeRegExp`** in `components/highlight.tsx`.

## [0.1.5] — 2026-05-13

Big-bang frontend refactor: continues splitting `main.tsx` until the transcript-view cluster lives in its own files. Browser-verified end-to-end + two parallel Sonnet audits pre-merge.

### Refactor

`main.tsx` 8015 → 7158 lines via four new modules and one move:

- `src/audio.ts` — module-level `activeAudio` singleton + `playExactAudioSpan` + the three accessor helpers used by both the meeting scrubber (main.tsx) and per-row clip playback (TranscriptRow.tsx). ES module guarantees a single instance, so cross-component pause coordination is preserved.
- `src/components/highlight.tsx` — `highlightImportantText` + `escapeRegExp`. Moved into `components/` (audit M1) to match `ConfirmModal.tsx`'s placement; both render JSX.
- `src/components/ConfirmModal.tsx` — themed replacement for `window.confirm`. Used in 8 places.
- `src/components/TranscriptRow.tsx` — the row itself + the locally-scoped `SegmentEditModal`. 319 lines.
- `src/components/SegmentExtras.tsx` — the per-row comments + edit-history tray, with its four internal helpers (`EditDiffRow`, `CommentBubble`, `CommentInput`, `renderWordDiff`). 433 lines. Split out of TranscriptRow.tsx per audit H1 so neither file ends up a grab-bag.

### Performance

- **`highlightImportantText` is now `useMemo`'d per row** keyed on `(text, highlightTerms, ownerTerms)`. Audit M2 flagged that the regex was being rebuilt on every render of every visible row — pre-existing but worth fixing while in the area.
- **`correctSegment` + `refreshAfterSegmentRevert` wrapped in `useCallback`** at the App level so the `React.memo` around `TranscriptRow` actually skips work. Previously the inline arrow defeated the memo every render (audit correctness — flagged as pre-existing but unblocked by this PR's prop refactor).

### Cleanup

- Removed dead imports from main.tsx (`memo`, `ConfidenceBar`, `SegmentComment`, `SegmentEdit`, `highlightImportantText`, `formatPctShort`) — all were consumed only by code that moved into the new modules.
- Cleaned a misleading `eslint-disable @typescript-eslint/no-explicit-any` directive in `audio.ts` (no `any` was actually used).
- Added a proper top-of-file purpose comment to `TranscriptRow.tsx`.

### Verification

- Two parallel Sonnet audits ran pre-merge (correctness + architecture/security). All HIGH and MEDIUM findings addressed.
- Browser-verified in Brave: TranscriptRow renders, SegmentEditModal opens via Edit button, Escape dismisses, SegmentExtras Comments + Edit History panels expand correctly, `highlightImportantText` renders, zero console errors.
- 140/140 backend tests pass; frontend builds clean; ruff + bandit + CodeQL clean.

## [0.1.4] — 2026-05-13

Sweeps up the remaining audit-deferred items: the upload + upgrade race conditions, the real Waveform memoization fix, and a meaningful chunk of the `frontend/src/main.tsx` split.

### Concurrency

- **`/api/upload` race fixed** (audit M-B). Previously each upload triggered a full inbox scan, so two concurrent uploads could ingest each other's files. `ingest_pending_files` now accepts `only_filename=` and `/api/upload` scopes ingest to the file it just wrote. Module-level lock guards the inbox loop. Regression test in `test_ingest_race.py`.
- **`/api/system/upgrade` concurrent-click guard**. Two clicks on the Upgrade pill no longer spawn two `uv run meetingmind upgrade` processes racing on the same git checkout. Returns `409 upgrade_already_in_progress` for a duplicate while one is running. Stale-flag timeout (15 min) so a crashed upgrade can't permanently disable the endpoint.

### Performance

- **Waveform peaks no longer rebuild per playhead tick.** Split into two static `<g>` groups (played + unplayed) clipped by a moving SVG `<clipPath>`, so the bars are diff'd once when the meeting loads instead of O(N) per animation frame. Wrapped in `React.memo` keyed on peaks + geometry.

### Refactor: split `frontend/src/main.tsx`

The 8666-line `main.tsx` is now down to ~8015 lines after extracting four new modules:

- `src/types.ts` — all API DTOs + shared UI types (~400 lines)
- `src/api.ts` — fetch wrapper + network-failure detection (~100 lines)
- `src/format.ts` — plain string/date formatters (~60 lines)
- `src/components/Waveform.tsx` — the audio waveform + its memoized peak renderer (~200 lines)

Future work: the remaining big self-contained components (TranscriptRow + SegmentEditModal + SegmentExtras) have a wider transitive footprint (`playExactAudioSpan`, `highlightImportantText`, shared module state) and were left in place for a future pass with browser-side verification.

### Test coverage

- New `test_ingest_race.py` (2 tests) pins the `only_filename` ingest scoping.

## [0.1.3] — 2026-05-13

Follow-up audit pass found a Unicode bypass of the v0.1.2 env-injection fix plus two missed applications of the path-containment helper. All fixed. Bundles with quality + perf wins from the deferred list.

### Security (follow-up audit)

- **H-A (regression of C-1)**: `_sanitize_env_value` only stripped `\n` / `\r` / `\x00`, but Python's `str.splitlines()` *also* splits on U+2028, U+2029, NEL (`\x85`), VT (`\x0b`), FF (`\x0c`), FS/GS/RS. An attacker could embed U+2028 in a key value and re-inject a `KEY=value` line via the same C-1 vector. Sanitizer now strips the full set.
- **H-B**: `_safe_repo_path` previously asserted containment against `repo_root` — too generous. A tampered DB row could point at `.env.local`, the SQLite file itself, or source code. Now restricted to the actual audio data dirs (`processed_dir`, `delete_review_dir`, `archive_dir`).
- **H-C**: `delete_meeting` and `build_waveform` joined `repo_root / stored_path` directly, skipping the new containment helper. Both now validate containment before unlinking or piping to ffmpeg.
- **M-A**: `ModelBus.unload()` lacked the regex + `--` terminator that `ensure_lm_studio_model_loaded` had. `DashboardSettingsUpdate.default_model` / `.quality_model` now reject malformed model names at the API boundary.
- **M-D**: `_write_env_var` now validates `var_name` against `^[A-Z][A-Z0-9_]+$` so a misconfigured `openrouter_api_key_env` (operator-editable in `local.toml`) can't clobber `PATH` / `HOME` / etc.
- **Magic-byte upload validation**: `/api/upload` now bounces obvious extension/content mismatches (a renamed `.exe` as `.mp3`, etc.) before the file ever reaches ffmpeg.
- **YAML frontmatter hardening**: every string scalar in meeting-note frontmatter is now `json.dumps`'d, so a user-controllable value with `:`, `#`, or a leading `-` can't break out of its YAML field.

### Performance

- **N+1 fix in `workstream_intelligence`**: collapsed the per-workstream `SELECT` loop into a single windowed query.
- **`obsidian_writer` `payload_json` parse**: the 8 `_summary_*` helpers now share an `lru_cache`-memoized parse instead of re-running `json.loads` on the same blob.
- **`initialize_database` once at startup**: 30 redundant per-request calls removed. Schema is initialized in `create_app()` only.
- **Frontend `TranscriptRow` wrapped in `React.memo`** with stable per-row callbacks via `useCallback`. Playhead ticks no longer re-render every transcript row.
- **`filteredSegments` useMemo deps narrowed** from `[detail, ...]` to `[detail?.segments, ...]` so the filter doesn't recompute on unrelated detail changes.
- **`extractActionOwner` regex hoisted** to module scope (was recompiling on every call).

### Quality

- Batched delete in `people_prune_orphans` (single `IN(...)` instead of per-row).
- Removed dead `void` suppressions and the unused `pullQuote` in `SummaryMindmap`.
- Dropped the `load_meeting_export_data` underscore-wrapper.
- `synthesis.py` 45 s timeout comment corrected to reflect intent + caching.

### Test coverage

- `test_env_injection.py`: regression test pins the Unicode line-separator scrub (8 tests, +1).
- `test_security_helpers.py`: new regression that the v0.1.2 path-containment helper would have passed but is properly tightened now (8 tests, +1).

## [0.1.2] — 2026-05-13

Security hardening + perf wins from a three-pass Sonnet audit before public-tester rollout. No data exposure occurred; these are defense-in-depth fixes for issues that would have mattered as the user pool grew.

### Security

- **`.env.local` injection blocked** (C-1). `POST /api/settings/openrouter-key` and `/api/settings/huggingface-token` previously wrote values into `.env.local` without stripping embedded newlines — a malicious payload could inject arbitrary env vars (e.g. a stolen HF token) that would load on next backend start. New `_sanitize_env_value` strips `\n`, `\r`, NUL, and surrounding whitespace; `_write_env_var` re-sanitizes defensively. (PR #26)
- **CORS regex tightened** (H-1). Previously matched any host with dots — even with no cookies, that meant any site could read `/api/meetings` cross-origin. Now restricted to loopback + RFC1918 + Tailscale CGNAT (`100.64.0.0/10`). `allow_credentials=False` since we use no cookies.
- **Audio path containment** (H-2). The four endpoints serving audio (`/audio`, `/process`, `/asr-candidates`, `/source/delete-review`) joined a DB-stored path against repo_root using Python's `/` operator — which silently drops the left operand when the right is absolute. New `_safe_repo_path` resolves + asserts containment.
- **`lms load` arg injection** (H-3). Model names are now validated against `^[A-Za-z0-9][A-Za-z0-9_./@:-]{0,200}$`, and the `lms load` invocation uses `--` to terminate flag parsing — so a configured model name can't smuggle CLI flags into the lms binary.
- **Username no longer leaked** (M-4). `/api/health` substitutes `~` for the user's home directory in `inbox` / `vault` paths so the dashboard can still show users where their files live without leaking `/Users/<name>/...` over Tailscale.
- **GitHub release `tag_name` validated** (M-3). The release URL is now constructed only if the upstream tag matches a strict semver pattern.

### Performance

- **Model-list subprocess calls now cached** with a 15-second TTL. `GET /api/settings` and `/api/setup-status` no longer spawn `lms ls` + `ollama list` (up to 10 s each, blocking) on every poll.
- **`ensure_lm_studio_model_loaded` cached** in-process. Previously shelled out `lms ps` on every LLM request — for a typical synthesis pass that meant 5-10 shell-outs per meeting. Now keyed by model name with a TTL slightly under the lms idle timeout.
- **Four missing meeting_id indexes added** on `transcript_segments`, `review_items`, `action_items`, `speaker_assignments`. Every meeting-scoped query previously did a full table scan.
- **Frontend startup parallelized**. The nine independent fetches at app-load now run via `Promise.all` instead of nine sequential awaits.

### Bug fixes

- **Segment revert now refreshes the row.** A successful revert previously left stale pre-edit text on screen until the user reloaded the page.

### Test coverage

- New `test_env_injection.py` (7 tests) pins the sanitizer behavior.
- New `test_security_helpers.py` (7 tests) covers `_safe_repo_path`, `_validate_model_name`, `_tildefy_path`.

## [0.1.1] — 2026-05-13

Tester-onboarding polish, the in-dashboard upgrade loop, and a wave of bug fixes surfaced by the first live ingest.

### Tester onboarding

- **`meetingmind start` opens the dashboard automatically** once the frontend is healthy. No more "what was the URL again?"
- **Global `mm` launcher.** `meetingmind install` drops `~/.local/bin/mm` so `mm upgrade`, `mm status`, `mm logs` work from any directory — no more `cd` + `uv run`.
- **Setup checklist on the Inbox screen.** If anything's missing (HF token, model loaded, identity set, pyannote weights cached) you see a clear checklist with one-click jumps to the right Settings panel. Disappears once everything's green.
- **`meetingmind upgrade` smarter defaults.** `--include-ml` is now on by default (most users need it), `--check` auto-chains `doctor --fix`, `--preview` shows the pending commits without pulling, and a `pre-upgrade-<stamp>` git tag is laid down for instant rollback.
- **`meetingmind doctor --fix`.** Interactive remediation for the safe, account-free issues: install ML deps, create missing folders, install frontend `node_modules`, pre-download pyannote model weights, and switch to `whisper-medium-mlx` on <12 GB hosts.

### In-dashboard upgrade flow

- New floating **"new version available" pill** (top-right) appears when a GitHub Release is ahead of the installed version.
- Click → modal with current version, new version, full release notes, and **Upgrade now** / **Remind me later** (24-hour snooze, per version).
- One-click upgrade spawns `mm upgrade` in a detached subprocess, the dashboard polls `/api/health` until the new backend answers, then auto-reloads.
- Backed by new endpoints `/api/system/version` (cached, GH-rate-limit-friendly) and `/api/system/upgrade`.

### Bug fixes

- **`DELETE /api/people/{id}` no longer 500s** with `FOREIGN KEY constraint failed` — also purges `speaker_profile_observations` for the person before dropping the row.
- **Speaker rename now ~50 ms** instead of 5 s. The pyannote embedding `Model` + `Inference` are cached at module scope; before, every approve reloaded the checkpoint from disk + revalidated through Lightning.
- **`approve_speaker_label` is idempotent now.** Double-clicks (or React re-fires) on the Confirm button no-op when the speaker is already approved with the same name.
- **OnboardingModal stops eating clicks.** Backdrop click always dismisses (was held open if a name was typed, which made the dashboard look frozen for first-time testers).
- **Review-page meeting click no longer silently fails.** Detail fetch now `.catch()`es and surfaces 404s instead of swallowing them.
- **PostIngestSpeakerModal drafts start blank.** Suggestion pills are one-click to accept — AI is an assistant, not an opinion you have to correct.

### Performance & UX

- **Hero meta strip** redesigned: now 4 cells (Conducted · Duration · Actions · Topics) with even spacing, clay accent ticks, brand-tinted wash.
- **HTML export drops the Google Fonts CDN** — fully offline-safe now.
- **HTML export footer** no longer leaks the source recording filename.
- **PDF export status** routed through user-facing labels ("Ready to review" / "Promoted to vault") instead of raw schema strings.
- **Obsidian front-matter cleaned up** — internal IDs and vague source paths removed; honest `date` + `exported_at` fields added.
- **Mobile palette FAB** so the command palette is reachable on phones.
- **Global `:focus-visible` ring** so keyboard users actually see where focus is.
- **iOS input zoom** suppressed by bumping inputs to 16 px font on mobile.

### Cross-platform

- `pyannote/embedding` now loads cleanly on fresh installs — added the missing `omegaconf` to the `ml` extra.
- `mlx-whisper` carries a `darwin/arm64` marker so `uv sync --extra ml` no longer half-installs on Linux/Intel macOS.

### Repo & docs

- Branch protection on `main`, secret scanning + push protection on, private vulnerability reporting on, Dependabot security + version updates, tag-protection ruleset, CODEOWNERS.
- README rewritten as a numbered 5-step path: prereqs → HF licenses → clone+install → start → first ingest.
- New `CONTRIBUTING.md`, issue templates, Discussions enabled, Welcome discussion pinned.
- 6 Dependabot upgrades: vite 7→8, @vitejs/plugin-react 5→6, typescript 5→6, actions/checkout v4→v6, actions/setup-node v4→v6, astral-sh/setup-uv v5→v7, unused `lucide-react` removed.

## [0.1.0] — 2026-05-13

Initial public tester preview.

### Pipeline

- Local-first three-pass pipeline: Whisper ASR + pyannote diarization + LLM synthesis.
- LLM provider options: LM Studio, Ollama, or opt-in OpenRouter (BYO API key).
- Aggressive action-item extraction prompt — scans transcripts for "I will / we should / let me follow up" phrasing so narrative-strong models stop absorbing commitments into prose.

### Dashboard

- Three-tab Review surface (Mind map, Minutes, Transcript) with shared 4-cell meta strip (Conducted, Duration, Actions, Topics).
- Themed `ConfirmModal` everywhere instead of `window.confirm`.
- People directory with delete + orphan-tidy.
- Workstreams index with cross-meeting search, rename, and themed-confirm delete.
- Command palette (`⌘K`) for meetings, people, workstreams, and view jumps.
- Mobile shape: bottom-tab navigation, sheet modals, 44pt touch targets, floating palette button.

### Exports

- Hand-rolled zero-dependency PDF exporter.
- Standalone HTML export (light/dark toggle, no external CDN).
- Obsidian-friendly Markdown export with stable slug-anchored front-matter.

### Security & privacy

- Loopback-only backend (`127.0.0.1`).
- `Origin` middleware blocks cross-origin mutating requests as a CSRF defense.
- Streaming upload size enforcement.
- `.env.local` for HF token + OpenRouter key — both gitignored, pastable from Settings.

### Licensing

- PolyForm Noncommercial 1.0.0 — fork, modify, use for personal/charity/education/government. Commercial use requires a separate license.
