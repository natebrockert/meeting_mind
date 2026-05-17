"""Conversation Drivers and Center of Gravity.

Two complementary views on "who and what drove this meeting" computed
deterministically from segments + atoms with no LLM call:

- ConversationDriver: specific moments (chapter intros, pivot questions,
  decision seedings) that reshaped what followed. Surfaced inline in the
  Mind Map "What drove this meeting" panel and used as the data source
  for review-page prioritization (segments with unconfirmed speakers
  that anchor driver moments).
- CenterOfGravity: per-speaker ranking that complements talk-time.
  Highlights the standout case where someone's gravitational rank is
  meaningfully higher than their talk-time rank — the "low-talk,
  high-impact" person who introduces the right question and stays
  mostly quiet while others discuss it.

Diarization quality is weak today; design accommodates that. Driver
detection itself doesn't depend on diarization accuracy — segments are
segments. Speaker attribution gates on `confirmed_by_user`: when the
speaker at a driver moment is unconfirmed, the moment surfaces without
a name, with a flag that the UI uses to render a "needs speaker review"
treatment. The same gate naturally drives review-page prioritization:
unconfirmed driver segments are the ones the reviewer should look at
first because attribution mistakes there have the highest cost.

LLM-judged signals (reference detection, idea adoption, reframing vs
polite redirect) are deferred to a later iteration — they add precision
but require frontier-model cost. See docs/design/meeting-output-improvements.md
§4b for the full design.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Literal

from app.config import AppConfig
from app.db.database import connect
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)


# Window of transcript-time after a "trigger" segment that we count as
# "discussion that followed." 90s is the design-doc threshold: short
# enough that a tangential remark 5 minutes later doesn't get credited
# back, long enough that a real Q→A→discussion chain fits inside.
_FOLLOW_ON_WINDOW_MS = 90_000

# Minimum follow-on speech from speakers *other than* the trigger for
# the moment to count as a pivot. Filters out questions with one-line
# answers and topic intros that died on the vine.
_PIVOT_MIN_OTHER_SPEAKER_SECONDS = 30.0
# Minimum distinct other-speaker turns for a question to count as a
# pivot. Two separate non-trigger speakers in the window is the "real
# discussion vs. one person dominating the reply" threshold.
_PIVOT_MIN_DISTINCT_OTHER_SPEAKERS = 2

# How many driver moments to surface per meeting. The design doc calls
# for 3-6; 6 is the hard cap so the panel stays scannable.
_MAX_DRIVERS = 6

# Per-kind quotas applied BEFORE the global cap. Ensures the panel is
# diverse — a decision-heavy meeting still shows topic_introductions
# and pivot_questions, not 6 decision_moments; a discussion-heavy
# meeting can't push every decision_moment off the panel with extra
# pivot_questions. Calibrated against the decision_heavy_meeting eval
# fixture where 3 decisions need to surface alongside the pivot
# questions they triggered.
_DRIVER_KIND_QUOTAS: dict[str, int] = {
    "decision_moment": 3,
    "pivot_question": 3,
    "topic_introduction": 2,
    "reframing": 1,
    "challenge": 1,
    "unstick": 1,
}

# Priority for resolving "same segment, multiple kinds." Earlier kinds
# claim the segment first; later kinds get deduped if their segment is
# already taken. Decision pronouncements outrank pivot questions which
# outrank topic introductions — most specific kind wins. LLM-judged
# kinds come last because the deterministic kinds are higher-precision.
_DRIVER_KIND_PRIORITY: list[str] = [
    "decision_moment",
    "pivot_question",
    "topic_introduction",
    "challenge",
    "reframing",
    "unstick",
]

# Composite weights for gravity_score, mirrored from §4b.2.
_WEIGHT_CHAPTERS = 0.30
_WEIGHT_FOLLOW_ON = 0.35
_WEIGHT_PIVOT_QUESTIONS = 0.20
_WEIGHT_DECISIONS = 0.15

# Standout rule for the conditional CoG chip: surface only when a
# speaker's gravity rank is meaningfully better than their talk-time
# rank. Two tiers:
#   - Big-flip case: delta ≥ 2 ranks AND talk-time < 30%. Classic
#     "low-talk-but-driving" leader.
#   - Very-quiet case: delta ≥ 1 rank AND talk-time < 15%. A speaker
#     who took up less than 1/8 of the meeting but climbed at least
#     one gravity rank is worth surfacing even if the rank flip is
#     modest — at that talk share they almost never get credit by the
#     metrics most tools use.
# Calibrated against the pivot_question_meeting eval fixture where
# Briar speaks ~8% of words and asks the question that reframes the
# meeting; under the strict-only rule she would have been hidden.
_STANDOUT_BIG_FLIP_MIN_DELTA = 2
_STANDOUT_BIG_FLIP_MAX_TALK_PCT = 0.30
_STANDOUT_QUIET_MIN_DELTA = 1
_STANDOUT_QUIET_MAX_TALK_PCT = 0.15


class ConversationDriver(BaseModel):
    """A specific moment that meaningfully reshaped what followed.

    Deterministic kinds (chapter intros, pivot questions, decision
    moments) come from segments + atoms with no LLM call. LLM-judged
    kinds (reframing, challenge, unstick) require frontier-model
    interpretation and are produced by llm_drivers.py with the cache
    pattern from #25 so cost is bounded to one call per meeting.
    """

    kind: Literal[
        # Deterministic kinds — see conversation_drivers.py for compute.
        "topic_introduction",
        "pivot_question",
        "decision_moment",
        # LLM-judged kinds — see llm_drivers.py. They overlap segments
        # the deterministic kinds catch; the merge step dedupes by
        # (segment_id, kind) so we don't double-render the same moment.
        "reframing",      # New framing of the existing topic.
        "challenge",      # Counterpoint that shifted direction.
        "unstick",        # Moment that broke a circular discussion.
    ]
    segment_id: int
    # Display name when the speaker is confirmed; else the raw
    # diarization id. UI uses `speaker_confirmed` to decide whether to
    # render the name or a "needs speaker review" placeholder.
    speaker_label: str
    speaker_confirmed: bool
    # One-sentence reason this moment was a driver. Kept short so the
    # panel can render it as a single line.
    description: str
    # Multi-speaker speech seconds in the 90s following the moment.
    # Higher = more impact. Used for sorting drivers and as the primary
    # "why this matters" stat shown in the panel. For LLM-judged kinds
    # we still compute this deterministically so all drivers sort on
    # the same scale.
    impact_seconds: float
    # high = strong signal + confirmed speaker; medium = signal there
    # but speaker unconfirmed OR mid-strength; low = weak signal that
    # we still surface for the inclusive case (rare).
    confidence: Literal["high", "medium", "low"]
    # "deterministic" or "llm". Lets the UI badge LLM-judged moments
    # (they're less precise) and lets the eval harness score them
    # separately. Defaults to "deterministic" for backward compat.
    source: Literal["deterministic", "llm"] = "deterministic"


class SpeakerGravity(BaseModel):
    """Per-speaker CoG snapshot. Always populated for active speakers."""

    speaker_id: str  # raw diarization id (stable key)
    speaker_label: str  # display name when confirmed, else id
    speaker_confirmed: bool
    talk_time_pct: float  # 0..1, word share
    gravity_score: float  # 0..1, composite of the four signals below
    chapters_introduced: int
    pivot_questions: int
    decisions_seeded: int
    other_seconds_after_turns: float


class CenterOfGravity(BaseModel):
    """Per-meeting CoG snapshot."""

    rankings: list[SpeakerGravity]
    # Surfaced only when gravity rank diverges meaningfully from
    # talk-time rank — the non-obvious "low-talk, high-impact" case.
    # When unset, the dashboard suppresses the CoG chip entirely
    # (the boring case is "top talker also most impactful").
    standout_speaker_id: str | None = None
    standout_label: str | None = None
    standout_reason: str | None = None


def compute_drivers_and_cog(
    config: AppConfig,
    meeting_id: int,
    *,
    include_llm_drivers: bool = True,
    enrich_descriptions: bool = True,
) -> tuple[list[ConversationDriver], CenterOfGravity]:
    """Compute both deterministic + (optionally) LLM-judged drivers.

    `include_llm_drivers` defaults to True for the production path; tests
    pass False to keep them LLM-free and deterministic. Failures inside
    the LLM step are swallowed by `compute_llm_drivers` itself, so this
    function never raises on LLM problems — the deterministic drivers
    still come through.

    `enrich_descriptions` controls the narrative-rewrite pass that
    replaces the mechanical "Opened a new chapter; 75s of follow-on…"
    descriptions with a 2-3 sentence narrative covering who/what/why/
    takeaway. Defaults True for production; tests pass False to keep
    them mechanical and offline. Failure inside the enrichment falls
    back to the mechanical descriptions, never raises.
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
        assignments = conn.execute(
            """
            SELECT diarization_speaker_id, approved_label, confirmed_by_user
            FROM speaker_assignments
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()
        summary_row = conn.execute(
            """
            SELECT payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'summary'
            ORDER BY id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
        decision_rows = conn.execute(
            """
            SELECT id, title, source_segment_ids
            FROM review_items
            WHERE meeting_id = ? AND kind = 'decision'
            ORDER BY id
            """,
            (meeting_id,),
        ).fetchall()

    if not segment_rows:
        return [], CenterOfGravity(rankings=[])

    speaker_meta = _build_speaker_meta(assignments)
    segments = [dict(row) for row in segment_rows]
    seg_by_id = {seg["id"]: seg for seg in segments}

    chapter_intros = _chapter_intro_segment_ids(summary_row, seg_by_id)
    decision_seed_segments = _decision_seed_segment_ids(decision_rows, seg_by_id)

    candidates: list[ConversationDriver] = []
    chapters_by_speaker: dict[str, int] = defaultdict(int)
    pivots_by_speaker: dict[str, int] = defaultdict(int)
    decisions_by_speaker: dict[str, int] = defaultdict(int)

    # 1. Topic introductions — one per chapter marker.
    for seg_id in chapter_intros:
        seg = seg_by_id.get(seg_id)
        if not seg:
            continue
        impact, distinct_others = _follow_on_metrics(seg, segments)
        speaker_id = str(seg["diarization_speaker_id"])
        chapters_by_speaker[speaker_id] += 1
        label, confirmed = _label_for(speaker_id, speaker_meta)
        candidates.append(
            ConversationDriver(
                kind="topic_introduction",
                segment_id=int(seg["id"]),
                speaker_label=label,
                speaker_confirmed=confirmed,
                description=_describe_topic_intro(seg, impact, distinct_others),
                impact_seconds=round(impact, 1),
                confidence=_confidence(confirmed, impact, distinct_others),
            )
        )

    # 2. Pivot questions — any segment containing a sentence-ending '?'
    # whose follow-on satisfies the multi-speaker threshold. Filters out
    # rhetorical questions, questions with one-line answers, and
    # interrogative openers without a real question (e.g. "How about
    # that.").
    for seg in segments:
        if not _segment_contains_question(str(seg.get("text") or "")):
            continue
        impact, distinct_others = _follow_on_metrics(seg, segments)
        if impact < _PIVOT_MIN_OTHER_SPEAKER_SECONDS:
            continue
        if distinct_others < _PIVOT_MIN_DISTINCT_OTHER_SPEAKERS:
            continue
        speaker_id = str(seg["diarization_speaker_id"])
        pivots_by_speaker[speaker_id] += 1
        label, confirmed = _label_for(speaker_id, speaker_meta)
        candidates.append(
            ConversationDriver(
                kind="pivot_question",
                segment_id=int(seg["id"]),
                speaker_label=label,
                speaker_confirmed=confirmed,
                description=_describe_pivot_question(seg, impact, distinct_others),
                impact_seconds=round(impact, 1),
                confidence=_confidence(confirmed, impact, distinct_others),
            )
        )

    # 3. Decision moments — display anchored to the pronouncement
    # (latest source segment), gravity credit to the seeder (earliest).
    # See _decision_seed_segment_ids for why these differ.
    for display_seg_id, seed_seg_id, decision_title in decision_seed_segments:
        display_seg = seg_by_id.get(display_seg_id)
        if not display_seg:
            continue
        impact, distinct_others = _follow_on_metrics(display_seg, segments)
        # Gravity credit to the seeder, not the pronouncer.
        seed_seg = seg_by_id.get(seed_seg_id, display_seg)
        seed_speaker_id = str(seed_seg["diarization_speaker_id"])
        decisions_by_speaker[seed_speaker_id] += 1
        # Display attribution uses the pronouncer (the moment we point at).
        display_speaker_id = str(display_seg["diarization_speaker_id"])
        label, confirmed = _label_for(display_speaker_id, speaker_meta)
        candidates.append(
            ConversationDriver(
                kind="decision_moment",
                segment_id=int(display_seg["id"]),
                speaker_label=label,
                speaker_confirmed=confirmed,
                description=_describe_decision_moment(decision_title),
                impact_seconds=round(impact, 1),
                confidence=_confidence(confirmed, impact, distinct_others),
            )
        )

    # 4. LLM-judged drivers (reframing / challenge / unstick). Cached so
    # cost is bounded to one call per meeting. Failures degrade silently
    # — the deterministic panel still works without them.
    if include_llm_drivers:
        from app.services.llm_drivers import compute_llm_drivers

        for driver in compute_llm_drivers(config, meeting_id):
            # Dedupe: if a deterministic kind already claimed this
            # segment, don't double-render. The deterministic kinds
            # have higher precision so we keep them on conflict.
            if any(c.segment_id == driver.segment_id for c in candidates):
                continue
            candidates.append(driver)

    # Apply per-kind quotas so the panel stays diverse. Within each
    # kind we keep the highest-impact entries; ties broken by segment
    # order for stable output. Then we merge, enforce the global cap
    # (by impact), and finally re-sort chronologically so the panel
    # reads in transcript order rather than impact order.
    # Filter out drivers whose speaker hasn't been confirmed in the
    # transcript review. Surfacing "needs speaker review" placeholders
    # in the panel pushes the work of resolving attribution onto the
    # user — friction we explicitly don't want. The driver is still
    # available downstream once the speaker is confirmed (extraction
    # re-runs invalidate the cache); until then we just hide it.
    candidates = [c for c in candidates if c.speaker_confirmed]
    by_kind: dict[str, list[ConversationDriver]] = defaultdict(list)
    for c in candidates:
        by_kind[c.kind].append(c)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda d: (-d.impact_seconds, d.segment_id))
    selected: list[ConversationDriver] = []
    seen_segment_ids: set[int] = set()
    # Walk kinds in priority order so a segment claimed by a more
    # specific kind (decision_moment) isn't displaced by a less
    # specific one (topic_introduction). Any kind not in the priority
    # list is appended to the end so we still process it.
    ordered_kinds: list[str] = [k for k in _DRIVER_KIND_PRIORITY if k in by_kind]
    ordered_kinds += [k for k in by_kind if k not in _DRIVER_KIND_PRIORITY]
    for kind in ordered_kinds:
        items = by_kind.get(kind, [])
        quota = _DRIVER_KIND_QUOTAS.get(kind, 1)
        for d in items[:quota]:
            if d.segment_id in seen_segment_ids:
                continue
            selected.append(d)
            seen_segment_ids.add(d.segment_id)
    selected.sort(key=lambda d: (-d.impact_seconds, d.segment_id))
    top = selected[:_MAX_DRIVERS]
    top.sort(key=lambda d: d.segment_id)

    # Narrative-enrich the surviving driver descriptions via a single
    # LLM call. Cache-first so this is ~3ms after the first load.
    # Failure paths inside enrich_drivers degrade to the mechanical
    # descriptions, never raises.
    if enrich_descriptions and top:
        from app.services.driver_enrichment import enrich_drivers

        top = enrich_drivers(config, meeting_id, top)

    cog = _compute_center_of_gravity(
        segments=segments,
        speaker_meta=speaker_meta,
        chapters_by_speaker=chapters_by_speaker,
        pivots_by_speaker=pivots_by_speaker,
        decisions_by_speaker=decisions_by_speaker,
    )
    return top, cog


# ─── helpers ────────────────────────────────────────────────────────────────


def _build_speaker_meta(rows) -> dict[str, tuple[str, bool]]:
    """diarization_speaker_id → (display_label, confirmed_by_user)."""
    out: dict[str, tuple[str, bool]] = {}
    for row in rows:
        sid = str(row["diarization_speaker_id"])
        label = row["approved_label"] if row["approved_label"] else sid
        confirmed = bool(row["confirmed_by_user"]) and bool(row["approved_label"])
        out[sid] = (str(label), confirmed)
    return out


def _label_for(
    speaker_id: str, meta: dict[str, tuple[str, bool]]
) -> tuple[str, bool]:
    label, confirmed = meta.get(speaker_id, (speaker_id, False))
    return label, confirmed


def _chapter_intro_segment_ids(summary_row, seg_by_id) -> list[int]:
    if not summary_row:
        return []
    try:
        payload = json.loads(summary_row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return []
    raw = payload.get("chapter_markers") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("start_segment_id")
        if not isinstance(anchor, (int, float)):
            continue
        seg_id = int(anchor)
        if seg_id in seg_by_id:
            out.append(seg_id)
    return out


def _decision_seed_segment_ids(
    rows, seg_by_id
) -> list[tuple[int, int, str]]:
    """Return (display_seg_id, seed_seg_id, title) per decision.

    display_seg_id = latest source segment, where the decision was
      *pronounced*. That's the moment we show in the panel — what the
      user can find by clicking through.
    seed_seg_id = earliest source segment, where the idea was first
      raised. CoG attributes the `decisions_seeded` gravity credit to
      the speaker at the seed segment, not the pronouncer — the
      seeder did the harder work of starting the thread.

    Splitting these prevents a pivot_question and a decision_moment
    from colliding on the same segment when the pivot question itself
    is what seeded the decision (common in real meetings).
    """
    out: list[tuple[int, int, str]] = []
    for row in rows:
        try:
            ids = json.loads(row["source_segment_ids"] or "[]")
        except (TypeError, json.JSONDecodeError):
            ids = []
        if not isinstance(ids, list):
            continue
        coerced = [int(x) for x in ids if isinstance(x, int) and x in seg_by_id]
        if not coerced:
            continue
        out.append((max(coerced), min(coerced), str(row["title"])))
    return out


_QUESTION_PATTERN = re.compile(r"\?\s*$|\?\s+\S")


def _segment_contains_question(text: str) -> bool:
    """Cheap heuristic: a segment has a question if its text contains a
    '?' followed by sentence end or whitespace+content. Falls back to a
    trailing '?' check. Deliberately conservative — high precision over
    high recall — because false positives drag the gravity score for
    speakers who happen to use rising-intonation declaratives.
    """
    return bool(_QUESTION_PATTERN.search(text))


def _follow_on_metrics(
    trigger_seg, segments: list[dict]
) -> tuple[float, int]:
    """Other-speaker speech seconds + distinct other-speakers count in
    the 90s window after `trigger_seg.end_ms`. Returns (seconds, count).
    """
    trigger_speaker = str(trigger_seg["diarization_speaker_id"])
    window_start = int(trigger_seg["end_ms"] or 0)
    window_end = window_start + _FOLLOW_ON_WINDOW_MS

    other_seconds = 0.0
    distinct_others: set[str] = set()
    for seg in segments:
        start = int(seg["start_ms"] or 0)
        if start < window_start:
            continue
        if start >= window_end:
            break
        speaker = str(seg["diarization_speaker_id"])
        if speaker == trigger_speaker:
            continue
        end = int(seg["end_ms"] or start)
        overlap_end = min(end, window_end)
        if overlap_end > start:
            other_seconds += (overlap_end - start) / 1000.0
            distinct_others.add(speaker)
    return other_seconds, len(distinct_others)


def _confidence(
    confirmed: bool, impact_seconds: float, distinct_others: int
) -> Literal["high", "medium", "low"]:
    """Map signal strength + speaker-confirmation to a confidence tier.

    High: confirmed speaker AND strong signal (≥60s of follow-on AND ≥2
    distinct others — i.e., real multi-speaker discussion, not just a
    monologue reply).
    Medium: confirmed speaker but weaker signal, OR unconfirmed speaker
    with strong signal (we trust the *moment* even when we don't yet
    know the speaker — the design doc gates speaker attribution on
    confirmation but doesn't require it to surface the moment).
    Low: unconfirmed speaker AND weaker signal.
    """
    strong = impact_seconds >= 60.0 and distinct_others >= 2
    if confirmed and strong:
        return "high"
    if confirmed or strong:
        return "medium"
    return "low"


def _describe_topic_intro(seg, impact: float, distinct_others: int) -> str:
    sec = int(round(impact))
    if sec <= 0:
        return "Opened a new chapter of the discussion."
    others = "speakers" if distinct_others != 1 else "speaker"
    return f"Opened a new chapter; {sec}s of follow-on across {distinct_others} other {others}."


def _describe_pivot_question(seg, impact: float, distinct_others: int) -> str:
    sec = int(round(impact))
    others = "speakers" if distinct_others != 1 else "speaker"
    return (
        f"Asked a question that triggered {sec}s of discussion "
        f"across {distinct_others} other {others}."
    )


def _describe_decision_moment(title: str) -> str:
    clean = (title or "").strip()
    if not clean:
        return "Seeded the discussion that led to a decision."
    snippet = clean if len(clean) <= 80 else clean[:77].rstrip() + "..."
    return f"Seeded the decision: “{snippet}”"


def _compute_center_of_gravity(
    *,
    segments: list[dict],
    speaker_meta: dict[str, tuple[str, bool]],
    chapters_by_speaker: dict[str, int],
    pivots_by_speaker: dict[str, int],
    decisions_by_speaker: dict[str, int],
) -> CenterOfGravity:
    words_by_speaker: dict[str, int] = defaultdict(int)
    follow_on_by_speaker: dict[str, float] = defaultdict(float)

    # Per-turn follow-on aggregation: for each turn by speaker X, count
    # other-speaker speech in the next 90s and add it to X's bucket.
    # Coarse-grained but faithful to the "did your turn cause discussion"
    # signal in the design doc.
    for seg in segments:
        speaker_id = str(seg["diarization_speaker_id"])
        text = str(seg.get("text") or "")
        words_by_speaker[speaker_id] += len(text.split())
        other_seconds, _distinct = _follow_on_metrics(seg, segments)
        follow_on_by_speaker[speaker_id] += other_seconds

    total_words = sum(words_by_speaker.values()) or 1
    max_chapters = max(chapters_by_speaker.values(), default=0) or 1
    max_follow_on = max(follow_on_by_speaker.values(), default=0.0) or 1.0
    max_pivots = max(pivots_by_speaker.values(), default=0) or 1
    max_decisions = max(decisions_by_speaker.values(), default=0) or 1

    rankings: list[SpeakerGravity] = []
    all_speakers = (
        set(words_by_speaker.keys())
        | set(chapters_by_speaker.keys())
        | set(pivots_by_speaker.keys())
        | set(decisions_by_speaker.keys())
    )
    for speaker_id in all_speakers:
        label, confirmed = _label_for(speaker_id, speaker_meta)
        chapters = chapters_by_speaker.get(speaker_id, 0)
        pivots = pivots_by_speaker.get(speaker_id, 0)
        decisions = decisions_by_speaker.get(speaker_id, 0)
        follow_on = follow_on_by_speaker.get(speaker_id, 0.0)
        words = words_by_speaker.get(speaker_id, 0)
        score = (
            _WEIGHT_CHAPTERS * (chapters / max_chapters)
            + _WEIGHT_FOLLOW_ON * (follow_on / max_follow_on)
            + _WEIGHT_PIVOT_QUESTIONS * (pivots / max_pivots)
            + _WEIGHT_DECISIONS * (decisions / max_decisions)
        )
        rankings.append(
            SpeakerGravity(
                speaker_id=speaker_id,
                speaker_label=label,
                speaker_confirmed=confirmed,
                talk_time_pct=round(words / total_words, 3),
                gravity_score=round(score, 3),
                chapters_introduced=chapters,
                pivot_questions=pivots,
                decisions_seeded=decisions,
                other_seconds_after_turns=round(follow_on, 1),
            )
        )

    standout_id, standout_label, standout_reason = _find_standout(rankings)
    return CenterOfGravity(
        rankings=sorted(rankings, key=lambda r: -r.gravity_score),
        standout_speaker_id=standout_id,
        standout_label=standout_label,
        standout_reason=standout_reason,
    )


def _find_standout(
    rankings: list[SpeakerGravity],
) -> tuple[str | None, str | None, str | None]:
    """The chip-worthy case: gravity rank ≥2 better than talk-time rank
    AND talk-time pct < 30%. Returns (id, label, reason) or (None,)*3
    when the boring case applies (top talker is also top driver, etc.).
    """
    if len(rankings) < 2:
        return None, None, None
    by_gravity = sorted(rankings, key=lambda r: -r.gravity_score)
    by_talk = sorted(rankings, key=lambda r: -r.talk_time_pct)
    talk_rank: dict[str, int] = {r.speaker_id: i for i, r in enumerate(by_talk)}
    for grav_rank, r in enumerate(by_gravity):
        # Require at least one *discrete* driver — chapter introduction,
        # pivot question, or decision seeded. Follow-on alone is noisy:
        # a speaker who happens to say "Mhm" between two segments of a
        # monologue would otherwise inherit the speech that follows as
        # though they triggered it. Calibrated against the dominated_
        # strategy_meeting fixture; without this gate Briar would surface
        # as standout for a 2-word interjection.
        discrete_drivers = (
            r.chapters_introduced + r.pivot_questions + r.decisions_seeded
        )
        if discrete_drivers == 0:
            continue
        delta = talk_rank[r.speaker_id] - grav_rank
        big_flip = (
            delta >= _STANDOUT_BIG_FLIP_MIN_DELTA
            and r.talk_time_pct < _STANDOUT_BIG_FLIP_MAX_TALK_PCT
        )
        very_quiet = (
            delta >= _STANDOUT_QUIET_MIN_DELTA
            and r.talk_time_pct < _STANDOUT_QUIET_MAX_TALK_PCT
        )
        if big_flip or very_quiet:
            pct = int(round(r.talk_time_pct * 100))
            parts: list[str] = []
            if r.chapters_introduced:
                parts.append(
                    f"introduced {r.chapters_introduced} "
                    f"{'chapter' if r.chapters_introduced == 1 else 'chapters'}"
                )
            if r.pivot_questions:
                parts.append(
                    f"asked {r.pivot_questions} pivot "
                    f"{'question' if r.pivot_questions == 1 else 'questions'}"
                )
            if r.decisions_seeded:
                parts.append(
                    f"seeded {r.decisions_seeded} "
                    f"{'decision' if r.decisions_seeded == 1 else 'decisions'}"
                )
            if not parts:
                # No discrete drivers but high follow-on alone — describe
                # generally rather than fabricate a count.
                parts.append("triggered the most follow-on discussion")
            reason = f"{pct}% talk time; {', '.join(parts)}."
            return r.speaker_id, r.speaker_label, reason
    return None, None, None
