"""LLM rewrite of Conversation Driver descriptions.

The deterministic detection in conversation_drivers.py knows WHEN a
moment happened (chapter intro, pivot question, decision moment) and
HOW MUCH discussion followed. It does not know WHAT the speaker
actually said, WHY the moment mattered, or what came of it.

This module fills that gap with a single LLM call per meeting that
rewrites the `description` field of every selected driver as a 2-3
sentence narrative:

  - Who spoke (by name, color-coded later by the frontend)
  - What they said or asked (specific, with phrases quoted when useful)
  - Why this moment mattered (what shifted, or what was raised)
  - What came of it (decisions, follow-on takeaways, who weighed in)

Cache pattern from PR #25: keyed by meeting_id, drops the enrichment
when extract_meeting_atoms re-runs, in-flight deduplication via lock
so two concurrent overview loads don't both call the model.

Failure mode: the model is unreachable / output is malformed → the
function returns the input drivers unchanged. The deterministic
descriptions are a graceful fallback, never crashing the overview.
"""

from __future__ import annotations

import json
import logging
import threading

from app.config import AppConfig
from app.db.database import connect
from app.services.conversation_drivers import ConversationDriver
from app.services.model_bus import ChatMessage, ModelBus

LOGGER = logging.getLogger(__name__)


_ENRICHMENT_TIMEOUT_SECONDS = 60

# How many segments around each driver we send to the model for
# context. 4 before + 4 after is enough for the model to see why the
# moment mattered without bloating the prompt.
_CONTEXT_WINDOW = 4

# In-flight dedup mirrors the pattern in reflections.py / llm_drivers.py.
_ENRICHMENT_LOCKS: dict[int, threading.Lock] = {}
_ENRICHMENT_LOCKS_MUTEX = threading.Lock()


def _enrichment_lock(meeting_id: int) -> threading.Lock:
    with _ENRICHMENT_LOCKS_MUTEX:
        lock = _ENRICHMENT_LOCKS.get(meeting_id)
        if lock is None:
            lock = threading.Lock()
            _ENRICHMENT_LOCKS[meeting_id] = lock
        return lock


_SYSTEM_PROMPT = (
    "You write narrative summaries of pivot moments in business meetings. "
    "You will be given a list of driver moments — each identified by "
    "segment_id and kind — and the surrounding transcript context for "
    "each. Your job is to rewrite each moment's `description` field as "
    "a 2-3 sentence narrative that answers, IN ORDER:\n\n"
    "  1. WHO spoke (use the speaker's name exactly as it appears in "
    "the transcript — never write 'the speaker' or 'Speaker N').\n"
    "  2. WHAT they said or asked — be specific. Quote a phrase of "
    "5-12 words when it captures the substance.\n"
    "  3. WHY this moment mattered — what shifted in the conversation, "
    "what was raised, or what risk/idea surfaced.\n"
    "  4. WHAT CAME OF IT — name the people who responded and what "
    "they brought (a decision made, a counterpoint raised, a "
    "follow-on question, a takeaway). Cite the responders by name.\n\n"
    "OFF-TOPIC DETECTION (load-bearing): some driver candidates the "
    "deterministic detector picks up are not actually substantive — "
    "they're side chatter, tech-issue troubleshooting, off-topic "
    "questions about movies / sports / food / weather, or "
    "cross-talk during a phone-dial / mic-fix. Examples to drop: "
    "'is your mic on?', 'can you hear me?', 'isn't there another "
    "Dune coming out?', 'where did the screenshare go?'. For each "
    "moment you'd be rewriting, look at the context. If the moment is "
    "off-topic relative to the meeting's substance, return "
    "`{\"segment_id\": <id>, \"description\": null}` instead of a "
    "description, and the caller will drop the driver. Better to drop "
    "a borderline driver than surface a noisy one.\n\n"
    "Rules:\n"
    "  - Stay observational, not editorialising. \"Briar asked whether "
    "QA capacity was the real constraint\" beats \"Briar wisely "
    "challenged the team\".\n"
    "  - No filler phrases. \"This was an important moment\" / \"a "
    "good question\" / \"this was significant\" add nothing. Cut them.\n"
    "  - Speaker NAMES, not pronouns. \"Avery agreed and committed to "
    "the pilot scope\" beats \"He agreed\".\n"
    "  - Stay within 60 words per moment. The panel is scannable, not "
    "a doc.\n"
    "  - If a moment has no clear takeaway in its follow-on (rare), "
    "describe what was raised and stop. Don't fabricate consequences.\n\n"
    "Return JSON of shape {\"enrichments\": [{\"segment_id\": int, "
    "\"description\": str | null}, ...]} with one entry per driver. "
    "`null` description marks the moment for drop. Never invent "
    "segment_ids."
)


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "enrichments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["segment_id", "description"],
                "properties": {
                    "segment_id": {"type": "integer"},
                    # `null` description signals "drop this driver — off
                    # topic." Caller filters those out before rendering.
                    "description": {"type": ["string", "null"]},
                },
            },
        }
    },
    "required": ["enrichments"],
}

# A sentinel string the cached enrichment map uses to mark drivers
# the model explicitly flagged as off-topic. We can't store `None`
# directly in the cache JSON because the splicer only treats string
# values as replacements; a sentinel lets us round-trip the "drop"
# decision through the cache without re-calling the model.
_DROP_SENTINEL = "__MM_DROP_DRIVER__"


def invalidate_driver_enrichment_cache(
    config: AppConfig, meeting_id: int
) -> None:
    """Drop the cached enrichment row. Called at the top of
    extract_meeting_atoms so re-extracted meetings regenerate.
    """
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM meeting_driver_enrichment WHERE meeting_id = ?",
            (meeting_id,),
        )


def enrich_drivers(
    config: AppConfig,
    meeting_id: int,
    drivers: list[ConversationDriver],
) -> list[ConversationDriver]:
    """Return drivers with their descriptions rewritten as narrative.

    Cache-first: if we've already enriched this meeting, splice the
    cached descriptions into the input list. On miss, call the model
    once for the full driver list, persist, splice. Failures degrade
    to the input drivers (with their mechanical descriptions intact).
    """
    if not drivers:
        return drivers

    cached = _load_cached_enrichment(config, meeting_id)
    if cached is not None:
        return _splice(drivers, cached)

    with _enrichment_lock(meeting_id):
        # Re-check cache after acquiring lock — a sibling request may
        # have populated it while we were waiting.
        cached = _load_cached_enrichment(config, meeting_id)
        if cached is not None:
            return _splice(drivers, cached)
        try:
            enriched_map = _compute_enrichment_uncached(config, meeting_id, drivers)
        except Exception as exc:  # noqa: BLE001 — enrichment is a nice-to-have
            LOGGER.warning(
                "driver_enrichment_failed meeting_id=%s err=%s", meeting_id, exc
            )
            return drivers
        _persist_enrichment(config, meeting_id, enriched_map)
        return _splice(drivers, enriched_map)


def _splice(
    drivers: list[ConversationDriver], enrichment: dict[int, str]
) -> list[ConversationDriver]:
    """Return new drivers with `description` replaced where enrichment
    has an entry for that segment_id. Drivers the model flagged as
    off-topic (via the drop sentinel) are filtered out entirely.
    Unmatched drivers pass through unchanged so the panel never goes
    blank on a cache miss for a single moment.
    """
    out: list[ConversationDriver] = []
    for d in drivers:
        replacement = enrichment.get(int(d.segment_id))
        if replacement == _DROP_SENTINEL:
            # Off-topic — the enrichment pass flagged this driver as
            # side-chatter / tech-issue / unrelated. Skip rendering.
            continue
        if isinstance(replacement, str) and replacement.strip():
            out.append(d.model_copy(update={"description": replacement.strip()}))
        else:
            out.append(d)
    return out


def _load_cached_enrichment(
    config: AppConfig, meeting_id: int
) -> dict[int, str] | None:
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            "SELECT enrichment_json FROM meeting_driver_enrichment WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["enrichment_json"]) if row["enrichment_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            sid = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, str):
            out[sid] = v
    return out


def _persist_enrichment(
    config: AppConfig, meeting_id: int, enrichment: dict[int, str]
) -> None:
    # JSON object keys are strings; we serialize with str keys and
    # parse back to ints on load. Empty-dict is a valid cache hit
    # (means "we tried, got nothing" — don't re-call).
    payload = json.dumps({str(k): v for k, v in enrichment.items()})
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meeting_driver_enrichment (meeting_id, enrichment_json)
            VALUES (?, ?)
            ON CONFLICT(meeting_id) DO UPDATE SET
              enrichment_json = excluded.enrichment_json,
              computed_at = CURRENT_TIMESTAMP
            """,
            (meeting_id, payload),
        )


def _compute_enrichment_uncached(
    config: AppConfig,
    meeting_id: int,
    drivers: list[ConversationDriver],
) -> dict[int, str]:
    segments_by_id, ordered_segments = _load_segments(config, meeting_id)
    if not segments_by_id:
        return {}
    contexts = [
        _format_driver_context(d, segments_by_id, ordered_segments)
        for d in drivers
    ]
    user_prompt = (
        "Driver moments to rewrite (one description per segment_id):\n\n"
        + "\n\n---\n\n".join(contexts)
    )
    model_bus = ModelBus(config)
    payload = model_bus.chat_json(
        [
            ChatMessage("system", _SYSTEM_PROMPT),
            ChatMessage("user", user_prompt),
        ],
        {"name": "DriverEnrichment", "schema": _RESPONSE_SCHEMA},
        model=config.models.quality_model or None,
        timeout=_ENRICHMENT_TIMEOUT_SECONDS,
    )
    raw = payload.get("enrichments") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return {}
    valid_segment_ids = {int(d.segment_id) for d in drivers}
    out: dict[int, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            sid = int(entry.get("segment_id"))
        except (TypeError, ValueError):
            continue
        if sid not in valid_segment_ids:
            # Hallucinated segment id — drop it. The driver keeps its
            # original mechanical description as a fallback.
            continue
        desc = entry.get("description")
        if desc is None:
            # Model flagged the moment as off-topic. Persist the drop
            # signal so cache hits also filter it.
            out[sid] = _DROP_SENTINEL
            continue
        if isinstance(desc, str) and desc.strip():
            # Scrub placeholder leakage — the enrichment prompt forbids
            # "Speaker N" in output but models still regress. Same
            # belt-and-braces pass used in extraction.persist_atoms;
            # imported lazily to avoid circular dep at module load time.
            from app.services.extraction import _scrub_speaker_placeholders

            out[sid] = _scrub_speaker_placeholders(desc.strip())
    return out


def _format_driver_context(
    driver: ConversationDriver,
    segments_by_id: dict[int, dict],
    ordered_segments: list[dict],
) -> str:
    """Format a single driver as: kind/speaker header + a window of
    surrounding transcript so the model can see why it mattered."""
    target = segments_by_id.get(int(driver.segment_id))
    if target is None:
        return f"[{driver.segment_id}] {driver.kind} — segment not found"
    target_index = next(
        (i for i, s in enumerate(ordered_segments) if int(s["id"]) == int(driver.segment_id)),
        -1,
    )
    if target_index < 0:
        return f"[{driver.segment_id}] {driver.kind} — segment not in order"
    start = max(0, target_index - _CONTEXT_WINDOW)
    end = min(len(ordered_segments), target_index + _CONTEXT_WINDOW + 1)
    lines = [
        f"DRIVER kind={driver.kind} segment={driver.segment_id} "
        f"speaker={driver.speaker_label} impact={int(driver.impact_seconds)}s",
        "Context:",
    ]
    for s in ordered_segments[start:end]:
        marker = "→" if int(s["id"]) == int(driver.segment_id) else " "
        label = s.get("speaker_label") or s.get("diarization_speaker_id") or "Speaker"
        text = str(s.get("text") or "").strip()
        lines.append(f"  {marker} [{s['id']}] {label}: {text}")
    return "\n".join(lines)


def _load_segments(
    config: AppConfig, meeting_id: int
) -> tuple[dict[int, dict], list[dict]]:
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
            SELECT diarization_speaker_id, approved_label
            FROM speaker_assignments
            WHERE meeting_id = ? AND confirmed_by_user = 1
            """,
            (meeting_id,),
        ).fetchall()
    speaker_map = {row["diarization_speaker_id"]: row["approved_label"] for row in assignment_rows}
    ordered: list[dict] = []
    by_id: dict[int, dict] = {}
    for row in segment_rows:
        seg = dict(row)
        seg["speaker_label"] = speaker_map.get(
            seg["diarization_speaker_id"], seg["diarization_speaker_id"]
        )
        ordered.append(seg)
        by_id[int(seg["id"])] = seg
    return by_id, ordered
