"""Stage C — LLM-based speaker identity resolver.

The deductive resolver (`identity_resolver.py`) catches direct-address
and vocative bindings via regex, but misses story-context references
like "Alice is graciously helping us" or "Bob was saying" where the
binding isn't carried by a vocative anchor. A single LLM call against
per-speaker transcript samples is dramatically better at these
context inferences than regex.

Wired as one MORE evidence source in the resolver — never replaces
the regex pass, never decreases scores, only adds weight to
high-confidence LLM matches. Stage A's evidence_count >= 2 silent
gate means LLM alone never silent-auto-applies; it needs corroboration
from another signal (voice match, direct-address, or vocative_thank).

Cost: one OpenRouter call per meeting (~$0.01 with grok-4.1-fast).
Cache: persists as `review_items.kind='llm_speaker_identities'` so
re-running `persist_identity_assignments` is free.

Failure mode: best-effort. On any error the function returns None,
the resolver skips this evidence source, and the regex / voice paths
still produce assignments.
"""

from __future__ import annotations

import json
import logging

from app.config import AppConfig
from app.db.database import connect
from app.services.model_bus import ChatMessage, ModelBus

_LOG = logging.getLogger(__name__)


_LLM_IDENTITY_PROMPT = """\
You are solving a who-said-what attribution puzzle. The transcript
labels speakers as "Speaker 1", "Speaker 2", etc. — these are
diarization labels, not real names. Several real human names appear
inside the dialogue (in direct address, story-context references,
self-introductions, etc.). Your task is to deduce which diarization
label corresponds to which real name, by reasoning across the WHOLE
conversation like a logic puzzle.

REASONING PROCESS (do this internally — your output is structured):

1. Scan for direct-address anchors ("Hey John, what do you think?").
   The NEXT speaker who responds is usually John. Two consecutive
   direct-address hits to the same name across different chunks
   greatly increase confidence.

2. Scan for self-reference exclusions. If Speaker X says a name in
   their own utterance ("Alice is helping us" / "Alice just told
   me"), Speaker X is NOT Alice — people rarely talk about
   themselves in third person.

3. Scan for self-introductions ("I'm Brent" / "this is Brent
   speaking"). Strong direct binding.

4. Scan for welcome / join events ("welcome Brent" / "thanks for
   joining, Brent"). The NEXT NEW speaker (one who hadn't spoken
   yet) is Brent.

5. Cross-check: an assignment is only valid if it survives ALL of
   1–4. If Speaker 3 was addressed as "Brent" but ALSO referred to
   "Brent" in third person, drop the assignment — internal
   contradiction.

6. Use the owner name as a high prior — the dashboard owner is
   almost always one of the speakers, often the one driving the
   conversation or asking the most questions.

OUTPUT FORMAT: return JSON matching the schema. For each speaker
you're confident about, emit { speaker_id, name, confidence, brief
justification quoting the transcript evidence }. Skip any speaker
you can't confidently identify — do NOT guess.

HARD RULES:

1. **Names MUST come from the candidate pool or the owner name.**
   You are not allowed to invent a name that isn't in the inputs.
   If a real name appears in the dialogue but ISN'T in `candidates`,
   it means the regex resolver couldn't validate it — do NOT use
   it.

2. **A name can only be assigned to one speaker.** If two speakers
   plausibly fit a name, pick the better-supported one and skip the
   other.

3. **A speaker can only be assigned one name.** Pick the strongest
   match per speaker.

4. **Self-reference exclusion is hard.** See reasoning step 2.

5. **Return fewer rows when uncertain.** confidence < 0.5 means
   skip the assignment — emit fewer rows rather than guessing.

INPUTS:

- `owner` — the dashboard owner's display name. High prior — they
  uploaded the recording and are almost always present.

- `candidates` — names already detected by the regex resolver. The
  only valid name choices (plus the owner).

- `dialogue` — the chronological transcript with diarization labels
  intact, in "Speaker N: utterance" form. May be truncated for
  length on very long meetings — reason about the visible portion.
"""


_LLM_IDENTITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "speaker_id": {"type": "string"},
                    "name": {"type": "string"},
                    "confidence": {"type": "number"},
                    "justification": {"type": "string"},
                },
                "required": ["speaker_id", "name", "confidence"],
            },
        },
    },
    "required": ["assignments"],
}


def synthesize_speaker_identities(
    config: AppConfig, meeting_id: int
) -> list[dict] | None:
    """Run the LLM identity resolver and persist its output.

    Returns the assignments list on success, None on any failure.
    The output is cached as `review_items.kind='llm_speaker_identities'`
    so the deductive resolver can read it without re-calling the model.

    Best-effort: failures are logged and swallowed so the rest of the
    speaker-naming pipeline still runs on regex + voice evidence alone.
    """
    inputs = _build_inputs(config, meeting_id)
    if inputs is None:
        return None
    try:
        bus = ModelBus(config)
        payload = bus.chat_json(
            [
                ChatMessage("system", _LLM_IDENTITY_PROMPT),
                ChatMessage("user", json.dumps(inputs)),
            ],
            {"name": "SpeakerIdentities", "schema": _LLM_IDENTITY_SCHEMA},
            # Identity reasoning needs context. The quality model is
            # the right choice here — same model used for the recap
            # synthesizer, which has similar reasoning requirements.
            model=bus.config.models.quality_model,
            timeout=45,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        _LOG.warning("llm_identity_synthesis_failed err=%s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    raw = payload.get("assignments")
    if not isinstance(raw, list):
        return None
    # Validate + dedup. The schema enforces required fields but we
    # need to filter for valid candidate names (don't trust the LLM
    # to honor "names from candidate pool only") and clamp confidence.
    valid_names = {n.casefold() for n in inputs["candidates"]}
    owner_first = (inputs.get("owner") or "").strip().split()
    # Length guard: a 1-char owner token (e.g. display_name="A") would
    # match any single letter and let the LLM make an unvalidated
    # assignment. Require 2+ chars to qualify as a real name token.
    if owner_first and len(owner_first[0]) >= 2:
        valid_names.add(owner_first[0].casefold())
    seen_speakers: set[str] = set()
    seen_names: set[str] = set()
    assignments: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not speaker or not name:
            continue
        if name.casefold() not in valid_names:
            # LLM invented a name not in the candidate pool. Honor
            # rule #1 by dropping it.
            _LOG.info(
                "llm_identity: dropped invented name %r for %s", name, speaker
            )
            continue
        if speaker in seen_speakers or name.casefold() in seen_names:
            continue
        confidence_raw = item.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.5:
            continue
        seen_speakers.add(speaker)
        seen_names.add(name.casefold())
        assignments.append(
            {
                "speaker_id": speaker,
                "name": name,
                "confidence": round(confidence, 3),
                "justification": str(item.get("justification") or "")[:400],
            }
        )

    _persist_llm_identities(config, meeting_id, assignments)
    return assignments


def load_llm_speaker_identities(
    config: AppConfig, meeting_id: int
) -> list[dict]:
    """Read the cached LLM identity assignments from review_items.
    Returns [] if no row exists (e.g. Stage C disabled / synthesizer
    failed). Safe to call before `synthesize_speaker_identities`.
    """
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'llm_speaker_identities'
            ORDER BY id DESC LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        return []
    out: list[dict] = []
    for item in assignments:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not speaker or not name:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            {
                "speaker_id": speaker,
                "name": name,
                "confidence": confidence,
            }
        )
    return out


# ── internals ────────────────────────────────────────────────────────


_MAX_DIALOGUE_CHARS = 15000


def _build_inputs(config: AppConfig, meeting_id: int) -> dict | None:
    """Assemble the inputs the LLM prompt expects: owner, the
    candidate-name pool, and the chronological dialogue with
    diarization labels intact. Returns None when the meeting has no
    transcript or no candidates — both mean the LLM has nothing to
    work with.

    The dialogue is kept in temporal order (essential for cross-
    attribution reasoning — direct address followed by response,
    welcome events, etc.) and capped at ~15k chars. For longer
    meetings we keep the first 10k chars (intros + early direct
    addresses live here) and the last 5k chars (the close usually
    contains landing references that disambiguate stragglers),
    eliding the middle.
    """
    with connect(config.paths.database_path) as conn:
        seg_rows = conn.execute(
            """
            SELECT id, diarization_speaker_id, start_ms, end_ms, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        cand_rows = conn.execute(
            """
            SELECT payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'speaker_name_candidate'
            """,
            (meeting_id,),
        ).fetchall()
    if not seg_rows:
        return None

    candidates: set[str] = set()
    for row in cand_rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        name = (payload.get("candidate_name") or "").strip()
        if name:
            candidates.add(name)
    if not candidates:
        # No regex candidates → nothing to validate the LLM against.
        # Skip rather than letting the LLM invent freely.
        return None

    dialogue = _build_dialogue(seg_rows)
    if not dialogue:
        return None

    owner_display = (config.owner.display_name or "").strip()
    return {
        "owner": owner_display,
        "candidates": sorted(candidates),
        "dialogue": dialogue,
    }


def _build_dialogue(seg_rows) -> str:
    """Render segments as 'Speaker N: text' lines in temporal order.

    Caps total length at `_MAX_DIALOGUE_CHARS`. For oversize meetings,
    keeps the first 10k chars and the last 5k chars with an explicit
    ellipsis between — preserves the highest-signal portions for
    cross-attribution reasoning while bounding token cost.
    """
    lines: list[str] = []
    for r in seg_rows:
        sid = str(r["diarization_speaker_id"])
        text = str(r["text"] or "").strip()
        if not text:
            continue
        lines.append(f"{sid}: {text}")
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) <= _MAX_DIALOGUE_CHARS:
        return joined
    # Oversize: head + ellipsis + tail. Cut on a line boundary so we
    # don't slice a sentence mid-word.
    head_budget = 10_000
    tail_budget = 5_000
    head_end = joined.rfind("\n", 0, head_budget)
    if head_end <= 0:
        head_end = head_budget
    tail_start = joined.rfind("\n", 0, len(joined) - tail_budget)
    if tail_start <= 0:
        tail_start = len(joined) - tail_budget
    # Boundary guard: if head and tail meet or overlap (e.g. a meeting
    # just slightly over the cap with few line breaks), inserting an
    # ellipsis would either be a lie ("truncated" but no content was
    # actually elided) or duplicate content. Fall back to a flat slice
    # in those cases.
    if tail_start <= head_end:
        return joined[:_MAX_DIALOGUE_CHARS]
    head = joined[:head_end]
    tail = joined[tail_start:].lstrip("\n")
    return f"{head}\n\n[... transcript truncated for length ...]\n\n{tail}"


def _persist_llm_identities(
    config: AppConfig, meeting_id: int, assignments: list[dict]
) -> None:
    """Cache the LLM output as a review_items row. Idempotent: a
    second run for the same meeting replaces the prior cache."""
    payload = json.dumps({"assignments": assignments})
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            DELETE FROM review_items
            WHERE meeting_id = ? AND kind = 'llm_speaker_identities'
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (?, 'llm_speaker_identities', ?, ?, '[]')
            """,
            (
                meeting_id,
                f"LLM identity resolver ({len(assignments)} assignments)",
                payload,
            ),
        )
