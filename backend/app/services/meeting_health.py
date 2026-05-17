"""Deterministic team-level meeting-health chips.

Computed from segments + action_items + review_items with no LLM call,
which keeps the compute cheap enough to run on every overview load and
safe on every model tier (no hallucination risk because there's no model).

The chips surface participation balance, active vs silent speaker counts,
decision density, unresolved-question count, and action clarity. They are
*facts about the meeting*, not judgments about any individual — so they
ride alongside the Mind Map / Minutes content rather than living in the
owner-only Reflections surface (which is a separate, flagged feature in
phase D).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Literal

from app.config import AppConfig
from app.db.database import connect
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)


# Threshold tuning — kept explicit at module top so it's easy to revisit
# once we have real-world usage data. Each threshold reflects a defensible
# bucket boundary, not an arbitrary cutoff.

# Word-share thresholds for participation balance. Above the top of the
# range is "this meeting had one dominant voice"; below is well-distributed.
_PARTICIPATION_BALANCED_MAX = 0.40
_PARTICIPATION_SKEWED_MAX = 0.60

# Speech-time floor for "active" speaker. Under this they were diarized but
# said almost nothing. 60s is roughly two substantive turns and feels like
# the line between "present" and "participating".
_ACTIVE_SPEAKER_MIN_SECONDS = 60.0

# Decisions normalized per 30 min of meeting. <1 per half hour is
# conversational rather than decisive; >3 is unusually decisive.
_DECISION_DENSITY_LOW_PER_30M = 1.0
_DECISION_DENSITY_HIGH_PER_30M = 3.0

# Action clarity = fraction of actions with BOTH explicit owner AND due
# date. <30% means most actions are floating; >70% is well-specified.
_ACTION_CLARITY_LOW_MAX = 0.30
_ACTION_CLARITY_HIGH_MIN = 0.70


class MeetingHealth(BaseModel):
    """Deterministic team-level meeting signals.

    Every field is computed without calling an LLM. Frontend renders these
    as small chips on the meeting overview; each carries a tooltip
    explaining what its label means. Optional fields skip the chip in the
    UI rather than rendering a misleading zero.
    """

    # "balanced": top speaker ≤ 40% of words.
    # "skewed": top speaker 40–60%.
    # "dominated": top speaker > 60%.
    # None when the meeting has no transcript yet.
    participation_balance: Literal["balanced", "skewed", "dominated"] | None = None
    # Word share of the most-talkative speaker (0..1). Carries the exact
    # number behind the categorical label so the UI can render "Avery 47%".
    top_speaker_share: float | None = None
    # Display name (approved label, or diarization id when unconfirmed) of
    # the top speaker. Null when no transcript.
    top_speaker_label: str | None = None
    # Speakers with ≥ 60s of speech total.
    speaker_count_active: int = 0
    # Speakers diarized but with < 60s of speech total — present-but-quiet.
    speaker_count_silent: int = 0
    # Decisions per 30 minutes:
    # "low" < 1, "moderate" 1–3, "high" > 3. None when duration is 0.
    decision_density: Literal["low", "moderate", "high"] | None = None
    decision_count: int = 0
    # Open questions still tagged "unanswered" after extraction. Legacy
    # OQ payloads (pre-status-field) default to unanswered, so the count
    # is conservative on old meetings — leans toward surfacing rather
    # than hiding work.
    unresolved_question_count: int = 0
    # Fraction of action items with BOTH explicit owner and due_date,
    # bucketed into "low" / "moderate" / "high". None when the meeting
    # captured zero actions (no signal to score against — chip is hidden).
    action_clarity: Literal["low", "moderate", "high"] | None = None
    action_count: int = 0


def compute_meeting_health(
    config: AppConfig, meeting_id: int
) -> MeetingHealth:
    with connect(config.paths.database_path) as conn:
        meeting_row = conn.execute(
            "SELECT duration_seconds FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if not meeting_row:
            return MeetingHealth()
        duration_seconds = float(meeting_row["duration_seconds"] or 0.0)

        segment_rows = conn.execute(
            """
            SELECT diarization_speaker_id, start_ms, end_ms, text
            FROM transcript_segments
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()
        speaker_map = {
            row["diarization_speaker_id"]: row["approved_label"]
            for row in conn.execute(
                """
                SELECT diarization_speaker_id, approved_label
                FROM speaker_assignments
                WHERE meeting_id = ? AND approved_label IS NOT NULL
                """,
                (meeting_id,),
            ).fetchall()
            if row["approved_label"]
        }
        action_rows = conn.execute(
            "SELECT owner_person_id, due_date FROM action_items WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()
        decision_count = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE meeting_id = ? AND kind = 'decision'
            """,
            (meeting_id,),
        ).fetchone()[0]
        open_question_rows = conn.execute(
            """
            SELECT payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'open_question'
            """,
            (meeting_id,),
        ).fetchall()

    return MeetingHealth(
        **_compute_participation(segment_rows, speaker_map),
        **_compute_decision_density(int(decision_count), duration_seconds),
        unresolved_question_count=_count_unresolved_questions(open_question_rows),
        **_compute_action_clarity(action_rows),
    )


def _compute_participation(
    segment_rows, speaker_map: dict[str, str]
) -> dict:
    """Top-speaker word share + active/silent speaker counts.

    Word counts (not time) drive the balance bucket because they better
    reflect substantive contribution: someone who talked for 5 minutes
    with 30 words isn't engaged the way someone with 800 words is. Time
    drives the active/silent split (a person can say something concise
    but important in a short window).
    """
    if not segment_rows:
        return {
            "participation_balance": None,
            "top_speaker_share": None,
            "top_speaker_label": None,
            "speaker_count_active": 0,
            "speaker_count_silent": 0,
        }
    words_by_speaker: dict[str, int] = defaultdict(int)
    seconds_by_speaker: dict[str, float] = defaultdict(float)
    for row in segment_rows:
        speaker_id = str(row["diarization_speaker_id"])
        text = str(row["text"] or "")
        words_by_speaker[speaker_id] += len(text.split())
        start = float(row["start_ms"] or 0)
        end = float(row["end_ms"] or 0)
        if end > start:
            seconds_by_speaker[speaker_id] += (end - start) / 1000.0

    total_words = sum(words_by_speaker.values())
    if total_words <= 0:
        return {
            "participation_balance": None,
            "top_speaker_share": None,
            "top_speaker_label": None,
            "speaker_count_active": 0,
            "speaker_count_silent": 0,
        }
    top_speaker_id, top_words = max(words_by_speaker.items(), key=lambda kv: kv[1])
    share = top_words / total_words
    if share <= _PARTICIPATION_BALANCED_MAX:
        balance: Literal["balanced", "skewed", "dominated"] = "balanced"
    elif share <= _PARTICIPATION_SKEWED_MAX:
        balance = "skewed"
    else:
        balance = "dominated"
    label = speaker_map.get(top_speaker_id, top_speaker_id)

    active = sum(
        1 for seconds in seconds_by_speaker.values() if seconds >= _ACTIVE_SPEAKER_MIN_SECONDS
    )
    silent = sum(
        1 for seconds in seconds_by_speaker.values() if seconds < _ACTIVE_SPEAKER_MIN_SECONDS
    )
    return {
        "participation_balance": balance,
        "top_speaker_share": round(share, 3),
        "top_speaker_label": label,
        "speaker_count_active": active,
        "speaker_count_silent": silent,
    }


def _compute_decision_density(
    decision_count: int, duration_seconds: float
) -> dict:
    if duration_seconds <= 0:
        return {"decision_density": None, "decision_count": decision_count}
    per_30m = decision_count / (duration_seconds / 1800.0)
    if per_30m < _DECISION_DENSITY_LOW_PER_30M:
        density: Literal["low", "moderate", "high"] = "low"
    elif per_30m <= _DECISION_DENSITY_HIGH_PER_30M:
        density = "moderate"
    else:
        density = "high"
    return {"decision_density": density, "decision_count": decision_count}


def _count_unresolved_questions(rows) -> int:
    """Count open_question review_items with status='unanswered'.

    Legacy payloads (pre-structured-OQ) lack a status field and default
    to unanswered — count them as unresolved. Anything explicitly tagged
    `partially_answered` or `deferred` no longer counts as "unresolved"
    for the chip.
    """
    unresolved = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        status = payload.get("status") if isinstance(payload, dict) else None
        if status in {"partially_answered", "deferred"}:
            continue
        unresolved += 1
    return unresolved


def _compute_action_clarity(action_rows) -> dict:
    count = len(action_rows)
    if count == 0:
        return {"action_clarity": None, "action_count": 0}
    well_specified = sum(
        1
        for row in action_rows
        if row["owner_person_id"] is not None
        and isinstance(row["due_date"], str)
        and row["due_date"].strip()
    )
    fraction = well_specified / count
    if fraction < _ACTION_CLARITY_LOW_MAX:
        clarity: Literal["low", "moderate", "high"] = "low"
    elif fraction <= _ACTION_CLARITY_HIGH_MIN:
        clarity = "moderate"
    else:
        clarity = "high"
    return {"action_clarity": clarity, "action_count": count}
