"""Speaker re-attribution — Pass C of the v0.2.x LLM repair plan.

Reads windows of transcript with current diarization labels and asks
the LLM to flag segments where conversational context suggests the
diarizer got the speaker wrong. This is the biggest single lever for
closing the AMI DER gap on the lite-stack diarizer — pyannote does
~9% AMI, FoxNoseTech ~14.96%; the gap is mostly multi-speaker
confusion that text-side context catches trivially:

  - Introductions: "Welcome, Alice." → next speaker is Alice
  - Direct address: "Bob, what do you think?" → next speaker is Bob
  - Self-reference continuation: "As I was saying about X..." → same
    speaker as the earlier reference to X
  - Q→A flow: question → answer pattern almost always means a speaker
    switch in real meetings

Design constraints (parallel to vocab_corrector):
  - The LLM PROPOSES; it does NOT auto-apply. Every proposal is
    persisted as a `review_items` row with kind='speaker_reattribution',
    surfaced in the review UI for accept/reject.
  - Conservative confidence threshold (default 0.6) so the review queue
    doesn't drown in low-signal proposals.
  - Hard cap on segments per meeting so long meetings don't fan out into
    huge LLM token costs.

Added in v0.2.4. Opt-in flag default-on:
  `repair.speaker_reattribution_enabled = true`
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect
from app.services.repair import tier_for_confidence

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReattributionProposal:
    """A single proposed speaker label correction."""

    segment_id: int
    current_speaker: str
    proposed_speaker: str
    confidence: float  # 0..1 from the LLM
    basis: str  # one-sentence rationale


def propose_speaker_reattributions(
    config: AppConfig,
    meeting_id: int,
) -> list[ReattributionProposal]:
    """Walk windows of a meeting's transcript, ask the LLM to flag
    segments whose diarization label looks wrong given conversational
    context. Returns proposals (NOT yet persisted)."""
    if not config.repair.speaker_reattribution_enabled:
        return []

    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            LIMIT ?
            """,
            (meeting_id, config.repair.speaker_reattribution_max_segments),
        ).fetchall()
    if not rows:
        return []

    segments = [
        {
            "id": row["id"],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "text": row["text"] or "",
            "speaker": row["diarization_speaker_id"] or "Unknown",
        }
        for row in rows
    ]

    window_size = max(2, config.repair.speaker_reattribution_window_size)
    min_confidence = config.repair.speaker_reattribution_min_confidence

    proposals: list[ReattributionProposal] = []
    # Overlapping windows so segments near the boundary still get
    # surrounding context. We step by window_size // 2.
    step = max(1, window_size // 2)
    for start in range(0, len(segments), step):
        window = segments[start : start + window_size]
        if len(window) < 2:
            continue
        # Audit M-A (v0.2.6): defensively constrain proposed labels to
        # speakers that actually appear in this window. The system
        # prompt instructs the LLM not to invent names, but a hallucinated
        # name would otherwise reach review_items unchallenged.
        in_window_labels = {str(seg.get("speaker") or "") for seg in window}
        in_window_labels.discard("")
        try:
            decisions = _llm_score_window(config, window)
        except Exception as exc:  # noqa: BLE001 — never crash pipeline on repair
            _LOG.warning("speaker reattributer LLM call failed: %s", exc)
            continue
        for decision in decisions:
            try:
                segment_id = int(decision["segment_id"])
                proposed = str(decision["proposed_speaker"]).strip()
                current = str(decision.get("current_speaker", "")).strip()
                confidence = float(decision.get("confidence", 0.0))
                basis = str(decision.get("basis", ""))[:240]
            except (KeyError, TypeError, ValueError):
                continue
            if not proposed or proposed == current:
                continue
            if confidence < min_confidence:
                continue
            # Skip hallucinated labels — only accept proposed speakers
            # that already exist in this window.
            if proposed not in in_window_labels:
                _LOG.debug(
                    "rejecting proposed speaker '%s' for segment %d — "
                    "not in window labels %s",
                    proposed,
                    segment_id,
                    in_window_labels,
                )
                continue
            proposals.append(
                ReattributionProposal(
                    segment_id=segment_id,
                    current_speaker=current,
                    proposed_speaker=proposed,
                    confidence=round(confidence, 3),
                    basis=basis,
                )
            )

    return _dedupe_proposals(proposals)


def _dedupe_proposals(proposals: list[ReattributionProposal]) -> list[ReattributionProposal]:
    """When overlapping windows propose the same correction, keep the
    higher-confidence one. Keyed on segment_id."""
    best: dict[int, ReattributionProposal] = {}
    for p in proposals:
        prev = best.get(p.segment_id)
        if prev is None or p.confidence > prev.confidence:
            best[p.segment_id] = p
    return sorted(best.values(), key=lambda p: p.segment_id)


def _llm_score_window(
    config: AppConfig,
    window: list[dict],
) -> list[dict]:
    """Send a window of segments to the small model for re-attribution
    scoring. Returns a list of decisions; entries with `proposed_speaker`
    == current label or `confidence` < threshold are filtered upstream.
    """
    from app.services.model_bus import ChatMessage, ModelBus

    prompt = _build_prompt(window)
    schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_id": {"type": "integer"},
                        "current_speaker": {"type": "string"},
                        "proposed_speaker": {"type": "string"},
                        "confidence": {"type": "number"},
                        "basis": {"type": "string"},
                    },
                    "required": ["segment_id", "proposed_speaker", "confidence"],
                },
            }
        },
        "required": ["decisions"],
    }
    bus = ModelBus(config=config)
    payload = bus.chat_json(
        [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=prompt),
        ],
        schema=schema,
        timeout=30,
    )
    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    return decisions if isinstance(decisions, list) else []


_SYSTEM_PROMPT = (
    "You are a careful transcript reviewer. The transcript below has "
    "speaker labels from automatic diarization, which sometimes gets "
    "the speaker wrong on short segments or after a speaker change. "
    "Your job: for each segment, decide whether the conversational "
    "context (introductions like 'Welcome, X', direct address like "
    "'X, what do you think', question→answer flow, continuation cues "
    "like 'as I was saying') suggests the diarizer's label is wrong. "
    "If you have HIGH confidence the label is wrong, propose a "
    "correction. If you're unsure, do not propose — say nothing for "
    "that segment. False corrections are worse than missed corrections."
)


def _build_prompt(window: list[dict]) -> str:
    lines = [
        "TRANSCRIPT WINDOW (current speaker labels in brackets):",
        "",
    ]
    for segment in window:
        speaker = segment.get("speaker") or "Unknown"
        text = (segment.get("text") or "").strip()
        seg_id = segment.get("id")
        # Truncate very long segments so the prompt stays bounded
        if len(text) > 240:
            text = text[:240].rstrip() + "..."
        lines.append(f"  [#{seg_id}] [{speaker}]: {text}")
    lines.append("")
    lines.append(
        "For each segment whose label you believe is wrong, return: "
        '{"segment_id": ..., "current_speaker": "...", '
        '"proposed_speaker": "...", "confidence": 0.0-1.0, '
        '"basis": "one sentence"}. '
        "Use ONLY speaker labels that appear in the window above as the "
        '"proposed_speaker" — do not invent new speakers. Omit segments '
        "you are not highly confident about. Format: "
        '{"decisions": [...]}.'
    )
    return "\n".join(lines)


def accept_reattribution_proposal(
    config: AppConfig,
    meeting_id: int,
    review_item_id: int,
) -> dict:
    """Apply an accepted speaker-reattribution proposal: update the
    transcript segment's speaker label, mark the review item resolved.

    This is the missing accept flow — proposals from
    `persist_speaker_reattribution_proposals` land in `review_items`,
    but until this endpoint runs the speaker labels in
    `transcript_segments` are unchanged.

    Returns a dict with `segment_id`, `previous_speaker`, `new_speaker`
    for the caller (HTTP route) to relay to the UI.

    Raises:
      - ValueError("review_item_not_found") if the row doesn't exist
      - ValueError("not_a_reattribution") if the kind isn't
        speaker_reattribution
      - ValueError("already_resolved") if the row was already marked
        resolved/rejected
    """
    from app.services.transcript_editor import reassign_segment_speaker

    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT id, kind, status, payload_json
            FROM review_items
            WHERE id = ? AND meeting_id = ?
            """,
            (review_item_id, meeting_id),
        ).fetchone()
    if not row:
        raise ValueError("review_item_not_found")
    if row["kind"] != "speaker_reattribution":
        raise ValueError("not_a_reattribution")
    if row["status"] not in ("open", None):
        raise ValueError("already_resolved")

    try:
        payload = json.loads(row["payload_json"] or "{}")
        segment_id = int(payload["segment_id"])
        proposed_speaker = str(payload["proposed_speaker"]).strip()
        previous_speaker = str(payload.get("current_speaker", "")).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_payload: {exc}") from exc
    if not proposed_speaker:
        raise ValueError("invalid_payload: empty proposed_speaker")

    # Audit M3 (v0.2.8): the transcript update and the review-item
    # resolution must commit together. Previously each ran in its own
    # `connect()` block — a crash between them left the transcript
    # updated but the review item still 'open', so the next
    # reattributer run could re-propose the now-applied label.
    #
    # `reassign_segment_speaker` opens its own internal `connect()`,
    # commits, and returns. We can't share its transaction. So instead
    # we mark the review item resolved FIRST in a transaction we control,
    # then call reassign. If reassign fails, we revert the status flip
    # to keep state consistent.
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            UPDATE review_items
            SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (review_item_id,),
        )
    try:
        # Apply the speaker label update — reuses the existing segment-edit
        # path so the same speaker_assignment_evidence + confidence side
        # effects fire as if a user did this through the rename UI.
        reassign_segment_speaker(config, meeting_id, segment_id, proposed_speaker)
    except Exception:
        # Roll back the status flip so the proposal stays in the queue
        # for the user to retry. Better than a ghost transcript edit.
        with connect(config.paths.database_path) as conn:
            conn.execute(
                """
                UPDATE review_items
                SET status = 'open', resolved_at = NULL
                WHERE id = ?
                """,
                (review_item_id,),
            )
        raise
    return {
        "segment_id": segment_id,
        "previous_speaker": previous_speaker,
        "new_speaker": proposed_speaker,
    }


def reject_reattribution_proposal(
    config: AppConfig,
    meeting_id: int,
    review_item_id: int,
) -> None:
    """Mark a speaker-reattribution proposal rejected. The transcript
    speaker labels are left as the diarizer set them.

    Idempotent on already-rejected rows (no-op). REJECTS to flip an
    accepted (resolved) row — audit M2 (v0.2.8): the previous version
    would silently flip status='resolved' → 'rejected', ghost-applying
    the transcript edit. Now mirrors the accept-flow guard.
    """
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            "SELECT kind, status FROM review_items WHERE id = ? AND meeting_id = ?",
            (review_item_id, meeting_id),
        ).fetchone()
        if not row:
            raise ValueError("review_item_not_found")
        if row["kind"] != "speaker_reattribution":
            raise ValueError("not_a_reattribution")
        # Already rejected → no-op (idempotent, as the docstring promised).
        if row["status"] == "rejected":
            return
        # Already accepted → refuse to flip; the transcript was already
        # updated and reversing it via reject would create a stale state.
        if row["status"] == "resolved":
            raise ValueError("already_resolved")
        conn.execute(
            """
            UPDATE review_items
            SET status = 'rejected', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (review_item_id,),
        )


def persist_speaker_reattribution_proposals(
    config: AppConfig,
    meeting_id: int,
) -> dict:
    """Run the re-attributer and persist proposals as review_items.

    v0.2.11: three-tier auto-accept. Each proposal is classified by
    its LLM-reported confidence:

      - silent (>= auto_apply_silent_threshold, default 0.90):
        reassign immediately, store with status='auto_applied' and
        payload tier='silent'.
      - toast (>= auto_apply_toast_threshold, default 0.70):
        same apply behavior, tier='toast' so frontend surfaces a
        small inline notice.
      - manual (below toast threshold): status='open' as before;
        user clicks Apply in the review banner.

    `auto_apply_enabled=False` reverts to v0.2.10 behavior. Idempotent:
    clears prior open + auto_applied proposals so a re-run doesn't
    accumulate duplicates.

    Returns dict with `total`, `auto_applied`, `manual` counts.
    """
    from app.services.transcript_editor import reassign_segment_speaker

    auto_enabled = bool(getattr(config.repair, "auto_apply_enabled", True))
    silent_thr = float(getattr(config.repair, "auto_apply_silent_threshold", 0.90))
    toast_thr = float(getattr(config.repair, "auto_apply_toast_threshold", 0.70))

    proposals = propose_speaker_reattributions(config, meeting_id)
    auto_applied = 0
    manual = 0
    # Audit-fix H1: track which inserted rows are auto-apply
    # candidates so we can flip them from 'open' → 'auto_applied' ONLY
    # after `reassign_segment_speaker` actually succeeds. The earlier
    # design inserted as 'auto_applied' first and then ran reassign;
    # if reassign crashed (or the process died between the two), the
    # audit row would lie about an apply that never happened.
    #
    # New flow:
    #   1. INSERT every row with status='open' (manual-review fallback
    #      is the safe default if anything goes wrong).
    #   2. For each silent/toast proposal, call reassign_segment_speaker.
    #   3. If it succeeds, UPDATE status='auto_applied' + payload.tier.
    #   4. If it fails, leave the row as 'open' so the user can apply
    #      by hand from the manual review banner.
    #
    # `pending_applies` carries the row metadata we need for step 2-4.
    # (review_item_id, segment_id, proposed_speaker, tier)
    pending_applies: list[tuple[int, int, str, str]] = []
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            DELETE FROM review_items
            WHERE meeting_id = ? AND kind = ?
              AND status = 'open'
            """,
            (meeting_id, "speaker_reattribution"),
        )
        for proposal in proposals:
            tier = tier_for_confidence(
                proposal.confidence, auto_enabled, silent_thr, toast_thr
            )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO review_items (
                        meeting_id, kind, title, payload_json,
                        status, confidence, source_segment_ids
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        "speaker_reattribution",
                        f"Speaker label: {proposal.current_speaker} → {proposal.proposed_speaker}",
                        json.dumps(
                            {
                                "segment_id": proposal.segment_id,
                                "current_speaker": proposal.current_speaker,
                                "proposed_speaker": proposal.proposed_speaker,
                                "basis": proposal.basis,
                                "tier": tier,
                            }
                        ),
                        # Always 'open' at INSERT time — flip after apply.
                        "open",
                        proposal.confidence,
                        json.dumps([proposal.segment_id]),
                    ),
                )
                if tier in ("silent", "toast"):
                    pending_applies.append(
                        (
                            int(cursor.lastrowid),
                            proposal.segment_id,
                            proposal.proposed_speaker,
                            tier,
                        )
                    )
                else:
                    manual += 1
            except Exception as exc:  # noqa: BLE001 — best-effort
                _LOG.debug("speaker_reattribution insert skipped: %s", exc)

    # Apply the auto-accepted reassignments. Each one is idempotent on
    # the segment's diarization_speaker_id (reassign_segment_speaker
    # short-circuits when target == current), so retries are safe.
    # On apply failure, the row stays 'open' and gets counted as manual.
    for review_item_id, segment_id, proposed_speaker, _tier in pending_applies:
        try:
            reassign_segment_speaker(config, meeting_id, segment_id, proposed_speaker)
            with connect(config.paths.database_path) as conn:
                conn.execute(
                    "UPDATE review_items SET status = 'auto_applied', "
                    "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (review_item_id,),
                )
            auto_applied += 1
        except Exception as exc:  # noqa: BLE001 — best-effort
            _LOG.warning(
                "auto-apply of reattribution %s failed: %s", review_item_id, exc
            )
            manual += 1

    return {"total": len(proposals), "auto_applied": auto_applied, "manual": manual}


