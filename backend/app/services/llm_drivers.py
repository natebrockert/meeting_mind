"""LLM-judged Conversation Drivers (reframing, challenge, unstick).

Complements the deterministic driver kinds in conversation_drivers.py
with three signals that require interpretation:

- reframing: a participant introduced a new framing of the existing
  topic (e.g. shifted from "what blocked us" to "what we'd do
  differently"). Pure pattern-matching can't see this; the model can.
- challenge: a counterpoint that meaningfully shifted direction —
  distinct from a polite question or restatement.
- unstick: a moment that broke a circular discussion and let the group
  move forward.

Uses the cache-table pattern from PR #25:

  1. Lookup by meeting_id in `meeting_llm_drivers`.
  2. Hit → deserialize and return (~3ms).
  3. Miss → call the model, persist, return (~3-15s on frontier, longer
     on local).
  4. `extract_meeting_atoms` invalidates this cache at the top of every
     extraction so a re-extracted meeting regenerates drivers.

Empty result is a valid cache hit. We don't re-call the model just
because the previous run found zero LLM-judged drivers — that would
double-charge for low-signal meetings.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Literal

from app.config import AppConfig
from app.db.database import connect
from app.services.conversation_drivers import ConversationDriver
from app.services.model_bus import ChatMessage, ModelBus

LOGGER = logging.getLogger(__name__)

# Per-meeting compute deduplication. Mirrors the lock pattern in
# reflections.py — see the comment there for the full reasoning. Without
# this, a UI mount that fires the meeting-detail fetch twice in quick
# succession can cause both calls to miss the empty cache and run the
# model in parallel, wasting a frontier-model call.
_LLM_DRIVERS_LOCKS: dict[int, threading.Lock] = {}
_LLM_DRIVERS_LOCKS_MUTEX = threading.Lock()


def _llm_drivers_lock(meeting_id: int) -> threading.Lock:
    with _LLM_DRIVERS_LOCKS_MUTEX:
        lock = _LLM_DRIVERS_LOCKS.get(meeting_id)
        if lock is None:
            lock = threading.Lock()
            _LLM_DRIVERS_LOCKS[meeting_id] = lock
        return lock


# Secondary call — fail fast if the model is unreachable rather than
# hang the overview load. Matches the 45s timeout used for key-terms
# enrichment in synthesis.py.
_LLM_DRIVERS_TIMEOUT_SECONDS = 45

# Hard cap so a runaway model can't flood the panel. The deterministic
# panel already returns up to 6 drivers; LLM-judged kinds typically
# overlap or sit alongside those, so 4 LLM drivers is a sensible ceiling.
_MAX_LLM_DRIVERS = 4


_LLM_DRIVER_KIND = Literal["reframing", "challenge", "unstick"]


_SYSTEM_PROMPT = (
    "You identify pivot moments in a recorded business meeting that "
    "require interpretation, not pattern-matching. You return JSON "
    "matching the schema. Apply these rules strictly:\n\n"
    "1. Output zero to four entries. **Empty is the correct output for "
    "most meetings.** Most conversations don't have a reframing, "
    "challenge, or unstick — surfacing one when there isn't one "
    "damages user trust more than missing a real one.\n\n"
    "2. Each entry must cite a single `segment_id` from the supplied "
    "transcript — the moment itself. Never invent a segment_id.\n\n"
    "3. The `kind` field is one of:\n"
    "   - `reframing`: a participant introduced a NEW framing of the "
    "existing topic. Not a restatement; not a clarifying question. The "
    "framing has to redirect what the group discusses next. Example: "
    "'instead of asking why the launch slipped, let's ask what we'd "
    "do differently' — that's a reframing if the group then pivots.\n"
    "   - `challenge`: a counterpoint that shifted direction. Not "
    "polite disagreement; the group's next 90s must reflect the shift. "
    "Example: 'I don't think the constraint is engineering — it's "
    "QA capacity' followed by the group exploring QA.\n"
    "   - `unstick`: a moment that broke a circular discussion. The "
    "preceding 60s must show repetition or stuck back-and-forth, AND "
    "the moment must let the group move on. The hardest kind to "
    "identify; lean toward emitting zero unless the pattern is "
    "unmistakable.\n\n"
    "4. The `description` is one sentence (max 25 words) explaining "
    "WHAT shifted, not WHY it was important. Concrete and specific.\n\n"
    "5. The `confidence` is `high` only when the shift is unmistakable "
    "in the transcript. `medium` when the pattern is there but you "
    "had to interpret. `low` when you're uncertain — these will be "
    "hidden in the UI by default, so use `low` rather than dropping "
    "a borderline observation.\n\n"
    "6. Do not credit a speaker for a moment they didn't speak at. "
    "Do not infer speakers' traits or intent. Stick to what the "
    "transcript shows in the moment.\n\n"
    "Return JSON with shape {\"drivers\": [...]}. Empty list is "
    "valid and expected when nothing notable surfaces."
)


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "drivers": {
            "type": "array",
            "maxItems": _MAX_LLM_DRIVERS,
            "items": {
                "type": "object",
                "required": ["kind", "segment_id", "description", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["reframing", "challenge", "unstick"],
                    },
                    "segment_id": {"type": "integer"},
                    "description": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
        }
    },
    "required": ["drivers"],
}


def compute_llm_drivers(
    config: AppConfig, meeting_id: int
) -> list[ConversationDriver]:
    """Return LLM-judged drivers, cache-first.

    Always returns a list (possibly empty). Failures during the LLM call
    surface as an empty list with a warning log — drivers are an
    enhancement, not a must-have, and the overview must keep loading.
    """
    cached = _load_cached_llm_drivers(config, meeting_id)
    if cached is not None:
        return cached
    # In-flight dedup. Re-check the cache after acquiring the lock so a
    # second request that was racing the first sees the freshly persisted
    # row instead of running its own LLM call.
    with _llm_drivers_lock(meeting_id):
        cached = _load_cached_llm_drivers(config, meeting_id)
        if cached is not None:
            return cached
        try:
            drivers = _compute_llm_drivers_uncached(config, meeting_id)
        except Exception as exc:  # noqa: BLE001 — cache-first call must not break overview
            LOGGER.warning("llm_drivers_compute_failed meeting_id=%s err=%s", meeting_id, exc)
            return []
        _persist_llm_drivers_cache(config, meeting_id, drivers)
        return drivers


def invalidate_llm_drivers_cache(config: AppConfig, meeting_id: int) -> None:
    """Drop the cached LLM-driver row. Called at the top of
    `extract_meeting_atoms` so a re-extracted meeting regenerates.
    """
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM meeting_llm_drivers WHERE meeting_id = ?",
            (meeting_id,),
        )


def _load_cached_llm_drivers(
    config: AppConfig, meeting_id: int
) -> list[ConversationDriver] | None:
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            "SELECT drivers_json FROM meeting_llm_drivers WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["drivers_json"]) if row["drivers_json"] else []
    except (TypeError, json.JSONDecodeError):
        # Corrupt cache row — treat as a miss so we recompute and
        # overwrite. Better than serving garbage from a stale shape.
        return None
    if not isinstance(raw, list):
        return None
    out: list[ConversationDriver] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(ConversationDriver.model_validate(entry))
        except Exception:  # noqa: BLE001 — drop malformed entries, keep others
            continue
    return out


def _persist_llm_drivers_cache(
    config: AppConfig, meeting_id: int, drivers: list[ConversationDriver]
) -> None:
    payload = json.dumps([d.model_dump() for d in drivers])
    with connect(config.paths.database_path) as conn:
        # UPSERT pattern: replace any existing row so re-computation
        # always writes the freshest result.
        conn.execute(
            """
            INSERT INTO meeting_llm_drivers (meeting_id, drivers_json)
            VALUES (?, ?)
            ON CONFLICT(meeting_id) DO UPDATE SET
              drivers_json = excluded.drivers_json,
              computed_at = CURRENT_TIMESTAMP
            """,
            (meeting_id, payload),
        )


def _compute_llm_drivers_uncached(
    config: AppConfig, meeting_id: int
) -> list[ConversationDriver]:
    # Coercion still needs the per-segment + per-speaker lookup tables
    # from the local builder. The transcript STRING itself comes from
    # the shared canonical renderer so the cache_prefix below
    # byte-matches whatever else uses the same helper (sharing a warm
    # prompt cache across services within a meeting).
    from app.services.transcript_render import render_canonical_transcript_for_llm

    transcript = render_canonical_transcript_for_llm(config, meeting_id)
    if not transcript.strip():
        return []
    _, segment_lookup, speaker_meta = _build_llm_context(config, meeting_id)
    model_bus = ModelBus(config)
    payload = model_bus.chat_json(
        [
            ChatMessage("system", _SYSTEM_PROMPT),
            ChatMessage(
                "user",
                # No directional ("above"/"below") reference — the
                # transcript may sit before OR within this same user
                # turn depending on the model bus's cache-prefix path
                # (OpenRouter prepends it as a separate message;
                # LM Studio / Ollama fold it into this user turn via
                # `_fold_cache_prefix`). Either ordering parses cleanly
                # when the prompt just says "the transcript".
                "Find LLM-judged drivers in the transcript.",
            ),
        ],
        {"name": "LLMDrivers", "schema": _RESPONSE_SCHEMA},
        # Use the quality model when configured — interpretation is the
        # job, and small local models miss reframings or hallucinate them.
        model=config.models.quality_model or None,
        timeout=_LLM_DRIVERS_TIMEOUT_SECONDS,
        # Transcript is the long stable content — flag as cacheable so
        # later calls (enrichment, key terms, reflections) within the
        # same meeting hit the warm prompt-cache on supporting routes.
        cache_prefix=transcript,
    )
    raw = payload.get("drivers") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return _coerce_llm_drivers(raw, segment_lookup, speaker_meta)


def _build_llm_context(
    config: AppConfig, meeting_id: int
) -> tuple[str, dict[int, dict], dict[str, tuple[str, bool]]]:
    """Render the transcript as the model sees it, plus return lookup
    tables the coercion step needs (segment by id, speaker label by
    diarization id).
    """
    with connect(config.paths.database_path) as conn:
        segment_rows = conn.execute(
            """
            SELECT id, diarization_speaker_id, start_ms, end_ms, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        assignment_rows = conn.execute(
            """
            SELECT diarization_speaker_id, approved_label, confirmed_by_user
            FROM speaker_assignments
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()

    speaker_meta: dict[str, tuple[str, bool]] = {}
    for row in assignment_rows:
        sid = str(row["diarization_speaker_id"])
        label = row["approved_label"] or sid
        confirmed = bool(row["confirmed_by_user"]) and bool(row["approved_label"])
        speaker_meta[sid] = (str(label), confirmed)

    lines: list[str] = []
    segment_lookup: dict[int, dict] = {}
    for row in segment_rows:
        segment_lookup[int(row["id"])] = dict(row)
        speaker_id = str(row["diarization_speaker_id"])
        speaker_label = speaker_meta.get(speaker_id, (speaker_id, False))[0]
        lines.append(
            f"[{row['id']}] {speaker_label}: {str(row['text'] or '').strip()}"
        )
    return "\n".join(lines), segment_lookup, speaker_meta


def _coerce_llm_drivers(
    raw: list,
    segment_lookup: dict[int, dict],
    speaker_meta: dict[str, tuple[str, bool]],
) -> list[ConversationDriver]:
    """Validate each LLM-emitted entry against the schema, derive the
    speaker label/confirmation from the cited segment, and skip entries
    that reference unknown segment_ids (rather than fail the whole pass).
    """
    out: list[ConversationDriver] = []
    for entry in raw[:_MAX_LLM_DRIVERS]:
        if not isinstance(entry, dict):
            continue
        try:
            segment_id = int(entry.get("segment_id"))
        except (TypeError, ValueError):
            continue
        seg = segment_lookup.get(segment_id)
        if seg is None:
            # Hallucinated segment id — drop the entry. Catches the
            # known failure mode where the model invents an integer
            # outside the supplied range.
            LOGGER.debug(
                "llm_drivers_unknown_segment_id segment_id=%s", segment_id
            )
            continue
        kind = entry.get("kind")
        if kind not in ("reframing", "challenge", "unstick"):
            continue
        confidence = entry.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        description = str(entry.get("description") or "").strip()
        if not description:
            continue
        speaker_id = str(seg["diarization_speaker_id"])
        speaker_label, confirmed = speaker_meta.get(speaker_id, (speaker_id, False))
        impact = _impact_seconds_for(segment_id, segment_lookup)
        out.append(
            ConversationDriver(
                kind=kind,
                segment_id=segment_id,
                speaker_label=speaker_label,
                speaker_confirmed=confirmed,
                description=description[:200],
                impact_seconds=round(impact, 1),
                confidence=confidence,
                source="llm",
            )
        )
    return out


def _impact_seconds_for(
    trigger_segment_id: int, segment_lookup: dict[int, dict]
) -> float:
    """Compute the same other-speaker follow-on metric used for the
    deterministic kinds so all drivers sort on one comparable scale.
    Implementation parallels conversation_drivers._follow_on_metrics
    but operates on the in-memory lookup we already have here.
    """
    trigger = segment_lookup.get(trigger_segment_id)
    if not trigger:
        return 0.0
    trigger_speaker = str(trigger["diarization_speaker_id"])
    window_start = int(trigger["end_ms"] or 0)
    window_end = window_start + 90_000

    ordered = sorted(segment_lookup.values(), key=lambda r: int(r["start_ms"] or 0))
    seconds = 0.0
    for seg in ordered:
        start = int(seg["start_ms"] or 0)
        if start < window_start:
            continue
        if start >= window_end:
            break
        if str(seg["diarization_speaker_id"]) == trigger_speaker:
            continue
        end = int(seg["end_ms"] or start)
        overlap_end = min(end, window_end)
        if overlap_end > start:
            seconds += (overlap_end - start) / 1000.0
    return seconds
