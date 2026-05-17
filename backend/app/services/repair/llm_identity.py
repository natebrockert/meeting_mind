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
from collections import defaultdict

from app.config import AppConfig
from app.db.database import connect
from app.services.model_bus import ChatMessage, ModelBus

_LOG = logging.getLogger(__name__)


_LLM_IDENTITY_PROMPT = """\
You are matching speaker labels in a meeting transcript to real human
names mentioned in the conversation. Each speaker appears as
"Speaker 1", "Speaker 2", etc. — these are diarization labels, not
real names. Your job: identify which speaker corresponds to each
name.

OUTPUT FORMAT: return JSON matching the schema you've been given.
For each speaker you're confident about, emit an assignment with
the speaker's diarization label, the matching name, a confidence
score in [0, 1], and a brief justification quoting the transcript
evidence. Skip any speaker you can't confidently identify — do NOT
guess.

HARD RULES:

1. **Names MUST come from the candidate pool or the owner name.**
   You are not allowed to invent a name that isn't in the inputs.

2. **A name can only be assigned to one speaker.** If two speakers
   plausibly fit a name, pick the better-supported one and skip the
   other.

3. **A speaker can only be assigned one name.** Pick the strongest
   match per speaker.

4. **Self-reference exclusion.** If Speaker X uses a name in their
   own utterance ("Alice is helping us" said by Speaker 1), Speaker
   X is NOT that name. People rarely refer to themselves in the
   third person.

5. **Return null when uncertain.** confidence < 0.5 means skip the
   assignment entirely — emit fewer rows rather than guessing.

INPUTS:

- `owner` — the dashboard owner's name. They almost always appear
  in their own meeting; if a speaker's samples sound like the
  owner's voice or are addressed-as the owner, weight that match.

- `candidates` — names already detected by the regex resolver. These
  are the only valid name choices. If a real name appears in
  transcript samples but ISN'T in this list, do NOT use it — it
  means the regex resolver couldn't validate it.

- `speakers` — for each diarization speaker, two or three of their
  longest transcript segments. These show speaking style + content
  and let you triangulate identity from address patterns ("Hey,
  John" → John is the NEXT speaker who responds) and self-reference
  exclusions.
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


def _build_inputs(config: AppConfig, meeting_id: int) -> dict | None:
    """Assemble the inputs dict the LLM prompt expects: owner, the
    candidate-name pool, and per-speaker transcript samples. Returns
    None when the meeting has no transcript or no candidates — both
    cases mean the LLM has nothing to work with.
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

    # Pick up to 3 of each speaker's longest segments. The LLM needs
    # enough material to triangulate identity from style + content
    # but not so much that the prompt balloons. Cap each sample at
    # 300 chars so even a speaker with a 30-segment monologue
    # contributes a bounded amount.
    by_speaker: dict[str, list[dict]] = defaultdict(list)
    for r in seg_rows:
        sid = str(r["diarization_speaker_id"])
        text = str(r["text"] or "").strip()
        if not text:
            continue
        duration = max(1, int(r["end_ms"]) - int(r["start_ms"]))
        by_speaker[sid].append(
            {"id": int(r["id"]), "duration_ms": duration, "text": text}
        )

    speakers_payload: dict[str, list[str]] = {}
    for sid, segs in by_speaker.items():
        # Longest by duration, then truncate text. Up to 3 per speaker.
        top = sorted(segs, key=lambda s: -s["duration_ms"])[:3]
        speakers_payload[sid] = [s["text"][:300] for s in top]

    owner_display = (config.owner.display_name or "").strip()
    return {
        "owner": owner_display,
        "candidates": sorted(candidates),
        "speakers": speakers_payload,
    }


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
