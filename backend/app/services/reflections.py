"""Owner-only Reflections (experimental).

A post-extraction LLM pass that surfaces "one or two non-obvious things
about how the owner showed up" in each meeting. Source-anchored,
selective by design, wrapped in wise-feedback framing.

This module ships behind `config.experimental.reflections_enabled` and
the contents NEVER leave the in-app Reflections surface. See the export
boundary rules in docs/design/meeting-output-improvements.md §6.5a —
build_meeting_overview, render_meeting_note, html_export, pdf_export
do NOT and MUST NOT read this data.

Architecture:

  1. Deterministic owner stats (talk-time %, question count, etc.) are
     computed in Python from segments + atoms and *injected into the
     prompt as known facts*. The model only does qualitative
     interpretation. This dramatically reduces hallucination risk on
     numeric claims and lets the prompt say "the user spoke 38% of
     words" without asking the model to count.
  2. Quality refusal gates short-circuit before any LLM call:
       - No configured owner
       - Owner spoke under 60s total
       - Transcript shorter than 5 min of speech
       - Average ASR confidence under 0.6
       - Meeting marked skip_reflections=1
     In each case, return a `Reflections` with `skipped_reason` set and
     no observations. The UI renders the empty state honestly rather
     than fabricating signal from noise.
  3. The LLM call follows the wise-feedback framing prescribed by §4.3:
     observational not judgmental, forward-framed, every observation
     cites evidence_segment_ids (the UI refuses to render observations
     without them).
  4. Cache pattern from PR #25: keyed by (meeting_id, owner_person_id),
     empty result is a valid cache hit, invalidated at the top of
     extract_meeting_atoms.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Literal

from app.config import AppConfig
from app.db.database import connect
from app.services.model_bus import ChatMessage, ModelBus
from app.services.owner import OwnerView, load_owner
from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

# In-flight compute deduplication. Two near-simultaneous requests for
# the same (meeting_id, owner_person_id) — common when a UI mount fires
# the fetch twice (React StrictMode in dev, browser prefetch, multi-tab,
# dashboard reload mid-compute) — would both miss the empty cache, both
# call the model, and both upsert the result. Whichever finished last
# would win, wasting one frontier-model call per duplicate request.
#
# We hold a per-key lock through the cache-check/compute/persist sequence.
# A second arrival waits for the first to finish, then re-checks the
# cache and returns the freshly persisted value. The lock is process-
# local (single-worker uvicorn); multi-worker deployments would need a
# distributed lock, but that's out of scope for the local-first product.
_REFLECTIONS_LOCKS: dict[tuple[int, int], threading.Lock] = {}
_REFLECTIONS_LOCKS_MUTEX = threading.Lock()


def _reflections_lock(meeting_id: int, owner_person_id: int) -> threading.Lock:
    key = (meeting_id, owner_person_id)
    with _REFLECTIONS_LOCKS_MUTEX:
        lock = _REFLECTIONS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REFLECTIONS_LOCKS[key] = lock
        return lock


# Quality refusal thresholds — all in seconds / pct.
_MIN_TOTAL_SPEECH_SECONDS = 5 * 60.0
_MIN_OWNER_SPEECH_SECONDS = 60.0
_MIN_AVG_ASR_CONFIDENCE = 0.6

# 90s window for "did your question get a response" detection. Matches
# the conversation_drivers follow-on window for consistency.
_FOLLOW_ON_WINDOW_MS = 90_000

# Secondary call — fail fast rather than hang the Reflections panel.
_REFLECTIONS_TIMEOUT_SECONDS = 60

# Hard cap on observations per meeting. Design doc calls for 0-3
# typical, 5 hard cap. We enforce the cap; selectivity to 0-3 is
# the prompt's job.
_MAX_OBSERVATIONS = 5


# 17 observation kinds per design doc §4.2. Listed in three groups
# (speaking, questions, engagement, leadership, communication, commitment).
# The kind enum doubles as the per-kind mute key for §6.7 (deferred to
# Phase E for the UX surface).
_OBSERVATION_KINDS = (
    # Speaking patterns
    "talk_time",
    "interruption_pattern",
    # Question quality
    "question_quality",
    "unanswered_question",
    "clarifying_question",
    # Engagement / psych-safety
    "uncertainty_admission",
    "invited_input",
    "specific_invitation",
    "paraphrase_check",
    "build_on_other",
    # Leadership / facilitation
    "framing_quality",
    "loop_closure",
    "delegation_balance",
    # Communication clarity
    "bluf_response",
    "decision_rationale",
    # Commitment tracking
    "commitment",
    "decision_driven",
)


class Observation(BaseModel):
    kind: Literal[
        "talk_time",
        "interruption_pattern",
        "question_quality",
        "unanswered_question",
        "clarifying_question",
        "uncertainty_admission",
        "invited_input",
        "specific_invitation",
        "paraphrase_check",
        "build_on_other",
        "framing_quality",
        "loop_closure",
        "delegation_balance",
        "bluf_response",
        "decision_rationale",
        "commitment",
        "decision_driven",
    ]
    # 1-2 sentences. OBSERVATIONAL not judgmental, FORWARD-FRAMED. The
    # prompt enforces this; readers should never see "you should..."
    observation: str
    # MUST be non-empty. The UI refuses to render observations without
    # evidence; this is the source-anchoring trust guarantee.
    evidence_segment_ids: list[int] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    # Optional one-sentence rationale tying the observation to a
    # defensible principle (Edmondson, wise-feedback, etc.) so the
    # reader can evaluate whether they agree with the framing.
    why_this_matters: str | None = None
    # Optional forward-looking option, phrased as "one option is..."
    # never "you should." Agency-preserving by design.
    suggested_next_time: str | None = None


class OwnerStats(BaseModel):
    """Deterministic numerics injected into the prompt as known facts.

    Always populated even when no observations are emitted, so the UI
    can render "this meeting's stats" even on a skipped meeting. The
    model is told these are FACTS not to be re-counted or contradicted.
    """

    talk_time_seconds: float = 0.0
    talk_time_pct: float = 0.0
    questions_asked: int = 0
    questions_open_ended: int = 0
    questions_unanswered: int = 0
    commitments_made: int = 0
    uncertainty_admissions: int = 0
    inputs_invited: int = 0


class Reflections(BaseModel):
    owner_display_name: str
    stats: OwnerStats = Field(default_factory=OwnerStats)
    # 0-5 observations. EMPTY IS A VALID RESULT — the design doc is
    # explicit about this. "Nothing notable surfaced this meeting"
    # beats a fabricated observation every time.
    observations: list[Observation] = Field(default_factory=list)
    # When observations is empty AND there was a deterministic reason
    # we didn't even attempt the LLM call (no owner, short transcript,
    # low confidence, opted out), this carries the reason so the UI can
    # render an honest empty state ("Reflections off for this meeting"
    # / "Not enough signal for Reflections") instead of "no Reflections
    # — this meeting looked well-balanced".
    skipped_reason: str | None = None


def invalidate_reflections_cache(config: AppConfig, meeting_id: int) -> None:
    """Drop ALL cached Reflections rows for this meeting (across owners).

    Called at the top of `extract_meeting_atoms`. Owner-agnostic by
    design: if the user changes the configured owner between extracts,
    stale rows from the previous owner would still be cleared by an
    extraction event, which is the right behaviour.
    """
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM reflection_observations WHERE meeting_id = ?",
            (meeting_id,),
        )


def compute_reflections(
    config: AppConfig, meeting_id: int
) -> Reflections | None:
    """Return Reflections for the configured owner, cache-first.

    Returns None when the feature flag is off — callers (the API
    endpoint) interpret that as "the surface is hidden entirely." When
    the flag is on but the meeting can't produce Reflections (no owner,
    short transcript, etc.), returns a Reflections with skipped_reason
    set so the UI can render an honest empty state.
    """
    if not config.experimental.reflections_enabled:
        return None

    owner = load_owner(config)
    if not owner.configured or owner.person_id is None:
        return _skipped(owner, "no_owner_configured")

    if _meeting_skipped(config, meeting_id):
        return _skipped(owner, "skipped_per_meeting")

    cached = _load_cached_reflections(config, meeting_id, owner.person_id)
    if cached is not None:
        return cached

    # In-flight dedup: hold the per-key lock through cache-check / compute /
    # persist so a second request that arrived while we were computing
    # waits, then sees our freshly persisted row instead of running its
    # own LLM call. We re-check the cache after acquiring the lock for
    # exactly that case.
    with _reflections_lock(meeting_id, owner.person_id):
        cached = _load_cached_reflections(config, meeting_id, owner.person_id)
        if cached is not None:
            return cached
        try:
            result = _compute_reflections_uncached(config, meeting_id, owner)
        except Exception as exc:  # noqa: BLE001 — Reflections must not break the dashboard
            LOGGER.warning(
                "reflections_compute_failed meeting_id=%s err=%s", meeting_id, exc
            )
            # Don't persist on transient failure — let a subsequent load retry.
            return _skipped(owner, "compute_error")

        _persist_reflections_cache(config, meeting_id, owner.person_id, result)
        return result


def _meeting_skipped(config: AppConfig, meeting_id: int) -> bool:
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            "SELECT skip_reflections FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    if row is None:
        return False
    return bool(row["skip_reflections"])


def _skipped(owner: OwnerView, reason: str) -> Reflections:
    return Reflections(
        owner_display_name=owner.display_name or "",
        skipped_reason=reason,
    )


def _load_cached_reflections(
    config: AppConfig, meeting_id: int, owner_person_id: int
) -> Reflections | None:
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT reflections_json FROM reflection_observations
            WHERE meeting_id = ? AND owner_person_id = ?
            """,
            (meeting_id, owner_person_id),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["reflections_json"]) if row["reflections_json"] else {}
    except (TypeError, json.JSONDecodeError):
        # Corrupt row — treat as miss and recompute.
        return None
    try:
        return Reflections.model_validate(payload)
    except Exception:  # noqa: BLE001 — schema drift → recompute
        return None


def _persist_reflections_cache(
    config: AppConfig,
    meeting_id: int,
    owner_person_id: int,
    reflections: Reflections,
) -> None:
    payload = reflections.model_dump_json()
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO reflection_observations
              (meeting_id, owner_person_id, reflections_json)
            VALUES (?, ?, ?)
            ON CONFLICT(meeting_id, owner_person_id) DO UPDATE SET
              reflections_json = excluded.reflections_json,
              computed_at = CURRENT_TIMESTAMP
            """,
            (meeting_id, owner_person_id, payload),
        )


def _compute_reflections_uncached(
    config: AppConfig, meeting_id: int, owner: OwnerView
) -> Reflections:
    segments, atoms_ctx, total_speech_seconds, avg_confidence = _load_context(
        config, meeting_id
    )

    if total_speech_seconds < _MIN_TOTAL_SPEECH_SECONDS:
        return _skipped(owner, "transcript_too_short")
    if avg_confidence is not None and avg_confidence < _MIN_AVG_ASR_CONFIDENCE:
        return _skipped(owner, "asr_confidence_too_low")

    owner_segment_ids = _owner_segment_ids(segments, owner)
    owner_speech_seconds = sum(
        _segment_seconds(seg) for seg in segments
        if int(seg["id"]) in owner_segment_ids
    )
    if owner_speech_seconds < _MIN_OWNER_SPEECH_SECONDS:
        return _skipped(owner, "owner_spoke_too_little")

    stats = _compute_owner_stats(
        segments=segments,
        owner_segment_ids=owner_segment_ids,
        owner_speech_seconds=owner_speech_seconds,
        atoms_ctx=atoms_ctx,
        owner_person_id=owner.person_id,
    )

    observations = _llm_observations(
        config=config,
        owner=owner,
        segments=segments,
        owner_segment_ids=owner_segment_ids,
        stats=stats,
    )

    return Reflections(
        owner_display_name=owner.display_name or "",
        stats=stats,
        observations=observations,
    )


# ─── deterministic stat computation ─────────────────────────────────────────


def _load_context(
    config: AppConfig, meeting_id: int
) -> tuple[list[dict], dict, float, float | None]:
    """Pull everything the prompt needs in one DB pass.

    Returns: segments (sorted), atoms_context (open_questions, decisions,
    actions for owner-aware questions/commitments stats), total speech
    seconds, average text_confidence (None if no segments).
    """
    with connect(config.paths.database_path) as conn:
        segment_rows = conn.execute(
            """
            SELECT id, diarization_speaker_id, start_ms, end_ms, text,
                   text_confidence, speaker_confidence, confidence
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
        open_question_rows = conn.execute(
            """
            SELECT payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'open_question'
            """,
            (meeting_id,),
        ).fetchall()
        action_rows = conn.execute(
            """
            SELECT owner_person_id, text FROM action_items
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()

    speaker_map = {row["diarization_speaker_id"]: row["approved_label"] for row in assignment_rows}
    segments: list[dict] = []
    total_seconds = 0.0
    confidences: list[float] = []
    for row in segment_rows:
        seg = dict(row)
        seg["speaker_label"] = speaker_map.get(
            seg["diarization_speaker_id"], seg["diarization_speaker_id"]
        )
        segments.append(seg)
        total_seconds += _segment_seconds(seg)
        conf = seg.get("text_confidence")
        if conf is None:
            conf = seg.get("confidence")
        if isinstance(conf, (int, float)):
            confidences.append(float(conf))
    avg_conf = sum(confidences) / len(confidences) if confidences else None

    atoms_ctx = {
        "open_questions": [
            _safe_json_loads(row["payload_json"]) for row in open_question_rows
        ],
        "actions": [dict(row) for row in action_rows],
    }
    return segments, atoms_ctx, total_seconds, avg_conf


def _safe_json_loads(value: str | None) -> dict:
    if not value:
        return {}
    try:
        out = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


def _segment_seconds(seg: dict) -> float:
    start = float(seg.get("start_ms") or 0)
    end = float(seg.get("end_ms") or 0)
    return max(0.0, (end - start) / 1000.0)


def _owner_segment_ids(segments: list[dict], owner: OwnerView) -> set[int]:
    out: set[int] = set()
    for seg in segments:
        label = seg.get("speaker_label") or ""
        if owner.matches(label):
            out.add(int(seg["id"]))
    return out


_QUESTION_PATTERN = re.compile(r"\?\s*$|\?\s+\S")
# Heuristic for "open-ended" questions — start with how/what/why/tell me.
# Misses some open-ended questions but catches the common cases without
# an LLM call. Deliberately conservative.
_OPEN_ENDED_OPENERS = re.compile(
    r"\b(?:how|what|why|when|where|tell me|describe|walk (?:me|us) through)\b",
    re.IGNORECASE,
)
# Owner says "I'm not sure" / "I don't know" / "good question" — admissions
# of uncertainty. Edmondson lists these as one of the strongest psych-
# safety signals leaders can model.
_UNCERTAINTY_PATTERN = re.compile(
    r"\b(?:i'?m not (?:sure|certain)|i don'?t know|good question|"
    r"i'?d have to check|let me think)\b",
    re.IGNORECASE,
)
# Owner invites input — "what do you think?", "any concerns?", "thoughts?"
_INVITE_PATTERN = re.compile(
    r"\b(?:what do you think|any (?:concerns|thoughts|reactions)|"
    r"(?:anyone|anybody) have|thoughts\?|reactions\?)\b",
    re.IGNORECASE,
)


def _compute_owner_stats(
    *,
    segments: list[dict],
    owner_segment_ids: set[int],
    owner_speech_seconds: float,
    atoms_ctx: dict,
    owner_person_id: int | None,
) -> OwnerStats:
    total_seconds = sum(_segment_seconds(s) for s in segments) or 1.0
    talk_time_pct = round(owner_speech_seconds / total_seconds, 3)

    # Questions: count owner segments with a question mark.
    questions_asked = 0
    questions_open_ended = 0
    questions_unanswered = 0
    by_id = {int(s["id"]): s for s in segments}
    ordered = sorted(segments, key=lambda s: int(s.get("start_ms") or 0))
    for seg_id in owner_segment_ids:
        seg = by_id.get(seg_id)
        if seg is None:
            continue
        text = str(seg.get("text") or "")
        if not _QUESTION_PATTERN.search(text):
            continue
        questions_asked += 1
        if _OPEN_ENDED_OPENERS.search(text):
            questions_open_ended += 1
        # Unanswered = no other-speaker speech in next 90s. Mirrors the
        # conversation_drivers follow-on metric so the two surfaces agree.
        if _is_unanswered(seg, ordered, owner_segment_ids):
            questions_unanswered += 1

    # Commitments: action_items assigned to *the configured owner specifically*,
    # not any action with any owner. Counting all attributed actions would
    # wildly inflate `commitments_made` in any multi-participant meeting.
    owner_action_count = sum(
        1
        for action in atoms_ctx.get("actions", [])
        if _action_is_owner(action, owner_person_id)
    )

    # Uncertainty admissions + input invitations.
    uncertainty = 0
    invited = 0
    for seg_id in owner_segment_ids:
        seg = by_id.get(seg_id)
        if seg is None:
            continue
        text = str(seg.get("text") or "")
        if _UNCERTAINTY_PATTERN.search(text):
            uncertainty += 1
        if _INVITE_PATTERN.search(text):
            invited += 1

    return OwnerStats(
        talk_time_seconds=round(owner_speech_seconds, 1),
        talk_time_pct=talk_time_pct,
        questions_asked=questions_asked,
        questions_open_ended=questions_open_ended,
        questions_unanswered=questions_unanswered,
        commitments_made=owner_action_count,
        uncertainty_admissions=uncertainty,
        inputs_invited=invited,
    )


# Treat a question as cross-talk (not a real failed question worth
# coaching) when the SAME speaker resumes within this window. Pattern:
# the owner asks "what are you thinking?", then keeps talking 2s later
# because another participant is dealing with audio / dialing / phone.
# Real questions get a beat for response.
_CROSS_TALK_RESUME_WINDOW_MS = 5_000

# Tech-issue / mic / audio / phone-dial cues that mark a question as
# happening during a side-channel activity. When a question segment OR
# the segment immediately before contains one of these phrases, the
# question is dropped from the "unanswered" count and from Reflections
# candidate consideration. Conservative — we want to miss real
# unanswered questions less than we want to surface fake ones.
_SIDE_CHANNEL_PATTERN = re.compile(
    r"\b(?:dial(?:ing)? (?:him|her|them|in)|"
    r"mic(?:rophone)? (?:on|off|check|issue)|"
    r"can(?:'t)? hear|hear me (?:now|ok)|"
    r"screenshare|screen share|"
    r"are you (?:on|there|muted)|"
    r"frozen|breaking up|cutting out|"
    r"text(?:ing)? (?:him|her|them))\b",
    re.IGNORECASE,
)


def _is_cross_talk(
    question_seg: dict,
    ordered_segments: list[dict],
    owner_segment_ids: set[int],
) -> bool:
    """True iff this question_seg should NOT be treated as a real
    unanswered question. Two patterns mark cross-talk:

      1. The owner resumes speaking themselves within ~5s of the
         question — i.e., they didn't actually wait for a response;
         they kept going. That's a thinking-aloud or self-redirect,
         not a question the team failed to answer.
      2. The question segment or the segment immediately before it
         contains a side-channel cue (mic/dial/screenshare/etc.) that
         indicates the moment was about technical logistics, not
         substantive content.
    """
    text = str(question_seg.get("text") or "")
    if _SIDE_CHANNEL_PATTERN.search(text):
        return True
    end_ms = int(question_seg.get("end_ms") or 0)
    # Owner-resume check: scan forward; if the FIRST segment after this
    # question is from the same owner, that's a self-redirect.
    qid = int(question_seg.get("id") or -1)
    if qid < 0:
        return False
    for seg in ordered_segments:
        start = int(seg.get("start_ms") or 0)
        if start < end_ms:
            continue
        if int(seg.get("id") or -1) == qid:
            continue
        # First non-question segment we hit. Within the resume window
        # AND the same owner speaking → cross-talk self-redirect.
        if (
            start - end_ms <= _CROSS_TALK_RESUME_WINDOW_MS
            and int(seg["id"]) in owner_segment_ids
        ):
            return True
        break
    # Look back one segment for side-channel cues too — phrases like
    # "I'm trying to dial him in" often sit one segment before the
    # owner's cross-talk question.
    prev = None
    for seg in ordered_segments:
        if int(seg.get("id") or -1) == qid:
            break
        prev = seg
    return bool(
        prev is not None
        and _SIDE_CHANNEL_PATTERN.search(str(prev.get("text") or ""))
    )


def _is_unanswered(
    question_seg: dict,
    ordered_segments: list[dict],
    owner_segment_ids: set[int],
) -> bool:
    # Cross-talk filter first — a question that's really side-channel
    # noise (mic / dial / self-redirect) is not "unanswered" in any
    # coaching sense.
    if _is_cross_talk(question_seg, ordered_segments, owner_segment_ids):
        return False
    window_start = int(question_seg.get("end_ms") or 0)
    window_end = window_start + _FOLLOW_ON_WINDOW_MS
    for seg in ordered_segments:
        start = int(seg.get("start_ms") or 0)
        if start < window_start:
            continue
        if start >= window_end:
            break
        if int(seg["id"]) in owner_segment_ids:
            continue
        # Any other-speaker segment with substantive content (≥3 words)
        # counts as an answer. Filters "Mhm" and "Yeah" backchannels.
        text = str(seg.get("text") or "")
        if len(text.split()) >= 3:
            return False
    return True


def _action_is_owner(action: dict, owner_person_id: int | None) -> bool:
    """True iff this action is attributed to the configured owner.

    Strict comparison against `owner_person_id`. The earlier truthy-check
    pattern (`if action.get("owner_person_id"): return True`) incorrectly
    counted every attributed action as owner-owned and inflated
    `commitments_made` in any multi-participant meeting.

    No text-prefix fallback for unattributed actions: action_items rows
    don't carry the source speaker, and "I will send the deck" said by
    anyone else would falsely credit the owner. When persistence couldn't
    map owner_person_id, the action stays uncounted — better to undercount
    than to credit the wrong person.
    """
    if owner_person_id is None:
        return False
    raw = action.get("owner_person_id")
    if raw is None:
        return False
    try:
        return int(raw) == int(owner_person_id)
    except (TypeError, ValueError):
        return False


# ─── LLM call ───────────────────────────────────────────────────────────────


# Wise-feedback header. Non-negotiable — present on every call regardless
# of model. See docs/design/meeting-output-improvements.md §4.3.
_SYSTEM_HEADER = (
    "You are writing reflections for a meeting attendee — the OWNER — "
    "about how they showed up in a meeting they chose to look back on. "
    "You are a mirror, not a judge. The owner has explicitly opted in; "
    "you are giving these observations because the owner holds a high "
    "standard for themselves and you believe they can act on what you "
    "show them.\n\n"
    "VOICE: write in the third person, using the owner's NAME (supplied "
    "in the user message), NEVER the second person 'you'. Use the form "
    "'Alex asked a clarifying question at [142]...', NOT 'You asked...'. "
    "This frame helps the owner read their own behaviour from the "
    "outside instead of as an accusation.\n\n"
    "Surface only the ONE or TWO non-obvious things about THIS SPECIFIC "
    "meeting that an observant peer would mention to a friend. Many "
    "meetings will yield ZERO observations and that is the correct "
    "output. Empty is fine.\n\n"
    "Rules (non-negotiable):\n"
    "1. Each observation MUST cite at least one segment_id from the "
    "supplied transcript as `evidence_segment_ids`. If you cannot "
    "anchor an observation to a specific moment, do not surface it.\n"
    "2. Observations are OBSERVATIONAL, not judgmental. Say "
    "'Alex's question at [142] received no direct response in the next "
    "90s', not 'Alex asked unclear questions'. Behaviour + evidence, "
    "never trait inference.\n"
    "3. Phrase any `suggested_next_time` as 'One option next time is "
    "to...' or 'One thing to try is...', NEVER as a directive. "
    "Agency-preserving — the owner decides what to take.\n"
    "4. No personality inference, no comparison to other speakers, no "
    "aggregate scores or grades. Hard prohibition.\n"
    "5. Skip the obvious: if a stat is already in the supplied Owner "
    "Stats (talk-time, question count, etc.), do NOT emit an "
    "observation that just restates it. The observation must ADD signal "
    "by linking the stat to a specific moment, or by explaining why "
    "the pattern mattered in this meeting.\n"
    "6. BALANCE — at least one observation must be a STRENGTH when "
    "the data supports it. If the Owner Stats show non-trivial "
    "`uncertainty_admissions` (≥2), `inputs_invited` (≥2), or "
    "`questions_open_ended` (≥3), AT LEAST ONE observation MUST "
    "name the strength — e.g. `uncertainty_admission`, "
    "`invited_input`, `specific_invitation`, `paraphrase_check`, "
    "`build_on_other`, `framing_quality`. Cite a specific moment "
    "where the behaviour shows up. People learn faster when "
    "growth-oriented feedback is anchored to recognized strength; "
    "all-corrective Reflections damage trust and adoption. The "
    "single exception: if the data genuinely shows no strength "
    "(quiet, brief, or muted meeting), do not fabricate one.\n"
    "7. UNANSWERED QUESTIONS are the highest-leverage growth signal. "
    "If `questions_unanswered` is ≥1, you SHOULD surface one as an "
    "observation unless every candidate is clearly cross-talk "
    "(mic-trouble, side-conversation, owner thinking aloud "
    "without waiting for a response). Side-channel questions are "
    "filtered upstream; treat the remaining count as substantive "
    "and pick the highest-stakes one to surface.\n"
    "8. Selectivity over coverage. Better one excellent observation "
    "than five mediocre ones. Hard cap is 5; aim for 2-3 with "
    "diversity of kind — no two observations of the same kind "
    "unless they add independent signal.\n"
    "9. The Owner Stats below are FACTS — do not contradict or "
    "re-count them. Build on them; cite them in `why_this_matters` "
    "when relevant.\n\n"
    "Valid `kind` values: talk_time, interruption_pattern, "
    "question_quality, unanswered_question, clarifying_question, "
    "uncertainty_admission, invited_input, specific_invitation, "
    "paraphrase_check, build_on_other, framing_quality, loop_closure, "
    "delegation_balance, bluf_response, decision_rationale, commitment, "
    "decision_driven.\n\n"
    "Return JSON of shape {\"observations\": [...]}. Empty list is "
    "valid and expected when nothing notable surfaces."
)

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "maxItems": _MAX_OBSERVATIONS,
            "items": {
                "type": "object",
                "required": ["kind", "observation", "evidence_segment_ids", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(_OBSERVATION_KINDS),
                    },
                    "observation": {"type": "string"},
                    "evidence_segment_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "why_this_matters": {"type": ["string", "null"]},
                    "suggested_next_time": {"type": ["string", "null"]},
                },
            },
        }
    },
    "required": ["observations"],
}


def _llm_observations(
    *,
    config: AppConfig,
    owner: OwnerView,
    segments: list[dict],
    owner_segment_ids: set[int],
    stats: OwnerStats,
) -> list[Observation]:
    transcript = _render_transcript_for_prompt(segments, owner, owner_segment_ids)
    if not transcript.strip():
        return []
    user_prompt = (
        f"Owner display name: {owner.display_name}\n\n"
        f"Owner Stats (FACTS, do not re-count):\n"
        f"  talk_time_pct: {int(round(stats.talk_time_pct * 100))}% "
        f"({int(round(stats.talk_time_seconds))}s)\n"
        f"  questions_asked: {stats.questions_asked}\n"
        f"  questions_open_ended: {stats.questions_open_ended}\n"
        f"  questions_unanswered: {stats.questions_unanswered}\n"
        f"  commitments_made: {stats.commitments_made}\n"
        f"  uncertainty_admissions: {stats.uncertainty_admissions}\n"
        f"  inputs_invited: {stats.inputs_invited}"
    )
    model_bus = ModelBus(config)
    payload = model_bus.chat_json(
        [
            ChatMessage("system", _SYSTEM_HEADER),
            ChatMessage("user", user_prompt),
        ],
        {"name": "Reflections", "schema": _RESPONSE_SCHEMA},
        model=config.models.quality_model or None,
        timeout=_REFLECTIONS_TIMEOUT_SECONDS,
        # Cache the transcript across pipeline LLM calls in the same
        # meeting. On Anthropic / Qwen routes this fires explicit
        # cache_control breakpoints; OpenAI / Grok / DeepSeek auto-cache
        # the matching prefix; other routes pay the standard cost.
        cache_prefix=f"Transcript (segment_id in brackets):\n{transcript}",
    )
    raw = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return _coerce_observations(raw, segments)


def _render_transcript_for_prompt(
    segments: list[dict],
    owner: OwnerView,
    owner_segment_ids: set[int],
) -> str:
    """One line per segment: `[id] (you)? Speaker: text`.

    Tagging the owner's lines with `(you)` keeps the model anchored on
    the right speaker even when ASR mishears the name or diarization
    miscuts a turn.
    """
    lines: list[str] = []
    for seg in segments:
        sid = int(seg["id"])
        label = seg.get("speaker_label") or seg.get("diarization_speaker_id") or "Speaker"
        tag = " (you)" if sid in owner_segment_ids else ""
        text = str(seg.get("text") or "").strip()
        lines.append(f"[{sid}]{tag} {label}: {text}")
    return "\n".join(lines)


def _optional_short_string(value, *, max_chars: int = 280) -> str | None:
    """Coerce an LLM-supplied optional prose field. Returns None for
    missing / non-string / empty values; caps survivors at max_chars.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed[:max_chars] if trimmed else None


def _coerce_observations(
    raw: list, segments: list[dict]
) -> list[Observation]:
    """Validate each LLM-emitted observation. Drop entries with empty
    evidence_segment_ids (the trust anchor) or segment_ids that don't
    exist in the supplied transcript (hallucinated).
    """
    valid_segment_ids = {int(s["id"]) for s in segments}
    out: list[Observation] = []
    for entry in raw[:_MAX_OBSERVATIONS]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in _OBSERVATION_KINDS:
            continue
        evidence_raw = entry.get("evidence_segment_ids") or []
        if not isinstance(evidence_raw, list):
            continue
        # Loose JSON from local models often serializes integers as
        # strings ("142") under permissive structured-output enforcement.
        # `int(...)` accepts both ints and well-formed numeric strings;
        # we filter out bools (subclass of int but never a segment id),
        # explicit None, and anything else that can't coerce.
        evidence: list[int] = []
        for x in evidence_raw:
            if isinstance(x, bool) or x is None:
                continue
            try:
                sid = int(x)
            except (TypeError, ValueError):
                continue
            if sid in valid_segment_ids:
                evidence.append(sid)
        if not evidence:
            # Cite-or-skip: no anchor → drop. Non-negotiable per design.
            LOGGER.debug(
                "reflections_obs_dropped_no_evidence kind=%s raw=%s", kind, evidence_raw
            )
            continue
        observation_text = str(entry.get("observation") or "").strip()
        if not observation_text:
            continue
        confidence = entry.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        # Optional prose fields. Capped at 280 chars (Twitter-style)
        # because they're meant for one short sentence each — a runaway
        # model on loose schema enforcement could otherwise persist
        # multi-kilobyte strings that render badly and slow cache I/O.
        why = _optional_short_string(entry.get("why_this_matters"))
        suggestion = _optional_short_string(entry.get("suggested_next_time"))
        out.append(
            Observation(
                kind=kind,
                observation=observation_text[:400],
                evidence_segment_ids=evidence,
                confidence=confidence,
                why_this_matters=why,
                suggested_next_time=suggestion,
            )
        )
    return out


def set_meeting_skip_reflections(
    config: AppConfig, meeting_id: int, *, skip: bool
) -> None:
    """Persist the per-meeting opt-out toggle.

    Used by the API layer when the user clicks "skip Reflections for
    this meeting" in Phase E. Also drops any cached rows for this
    meeting when skip=True so the user sees the empty state immediately
    rather than the previous Reflections.
    """
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "UPDATE meetings SET skip_reflections = ? WHERE id = ?",
            (1 if skip else 0, meeting_id),
        )
        if skip:
            conn.execute(
                "DELETE FROM reflection_observations WHERE meeting_id = ?",
                (meeting_id,),
            )


__all__ = [
    "Observation",
    "OwnerStats",
    "Reflections",
    "compute_reflections",
    "invalidate_reflections_cache",
    "set_meeting_skip_reflections",
]
