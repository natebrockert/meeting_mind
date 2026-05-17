"""Segment-split proposals — v0.2.10 Pass D.

Diarizer-boundary lag: the diarizer cuts on energy/embedding shifts, but
real conversational turns often have a sharp speaker change a beat
before that. faster-whisper transcribes the whole window to one
segment, so the *next* speaker's first few words leak onto the *current*
segment. Concrete example from the v0.2.10 panel testing:

  seg 15  [16:05-16:32]  Speaker 2 (Jan)
    "...are these companies wise or silly to be rushing to spend
    10s, even hundreds of billions of dollars on AI and might some
    of them really regret it? Okay, so I am of"

  seg 16  [16:32-17:07]  Speaker 4 (Paul)
    "the opinion that we are witnessing that, that they're..."

The "Okay, so I am of" tail belongs to Paul, but it's stuck onto Jan's
segment because the diarizer's boundary lagged by ~2s. Both segments
have low speaker confidence (0.25 and 0.45) — the system *knew* it was
uncertain but couldn't propose a fix because v0.2.4's reattributer only
reassigns *whole* segments, not split points.

This pass:

  1. Scans every segment whose speaker_confidence is below
     `repair.segment_split_min_confidence` (default 0.55).
  2. Looks for a discourse-opener pattern at the tail of the segment
     text (e.g. "Okay, so ...", "Yeah, well ...", "Right, I think...").
  3. If found, uses `transcript_words` to locate the word-level
     timestamp where the opener starts.
  4. Persists a `review_items` row with kind='segment_split_proposal'
     so the user can accept or reject in the UI.

Like the other Pass-X repairs, this is conservative and never
auto-applies. Accepting splits the segment in two and reassigns the
tail to the speaker of the *following* segment (which is the diarizer's
own best guess at who started speaking next).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect
from app.services.repair import tier_for_confidence

_LOG = logging.getLogger(__name__)

# Discourse-opener patterns: phrasings that strongly suggest the start
# of a NEW speaker's turn. The patterns require the opener to appear
# late in the segment — first half of a segment is almost always the
# segment's own speaker. Matched against the tail half of the text.
#
# Anchored with `\b` so we don't grab "okay, so I'll continue" mid-thought.
# Each match captures the START position of the opener so we know where
# to split the words.
_DISCOURSE_OPENER_PATTERNS = [
    re.compile(
        r"\b(?P<opener>(?:Okay|OK)[,.!?]?\s+(?:so|well|let|I))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<opener>(?:Yeah|Yes|Yep)[,.!?]?\s+(?:well|so|I|right|but))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<opener>(?:Right|Sure|Absolutely)[,.!?]\s+(?:so|well|I|let|but))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<opener>(?:Well|So)[,.!?]\s+(?:I think|I'd say|the|let|to))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<opener>I (?:think|believe|would say|am of)\s+(?:the|that|we|it))\b",
        re.IGNORECASE,
    ),
]


@dataclass(frozen=True)
class SplitProposal:
    """A proposed segment-split. Persisted into review_items as
    kind='segment_split_proposal'.

    Attributes:
        segment_id: The segment to split.
        split_at_ms: The timestamp where the tail starts. The accept
            handler will assign all words at/after this time to the new
            tail segment.
        tail_text: The portion of the segment text that's proposed to
            move to the new tail segment.
        head_text: The portion that stays with the original segment.
        tail_speaker_id: The diarization_speaker_id we'd assign to the
            new tail — typically the speaker of the immediately-following
            segment (the diarizer's own best guess at who spoke next).
        confidence: Heuristic confidence in [0, 1].
        evidence: Short string describing why this fired (for the UI tooltip).
    """

    segment_id: int
    split_at_ms: int
    tail_text: str
    head_text: str
    tail_speaker_id: str
    confidence: float
    evidence: str


def propose_segment_splits(
    config: AppConfig, meeting_id: int
) -> list[SplitProposal]:
    """Scan every low-confidence segment for a discourse-opener tail
    that suggests a speaker boundary leak. Returns proposals; persists
    nothing.
    """
    min_conf = float(getattr(config.repair, "segment_split_min_confidence", 0.55))
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text, diarization_speaker_id,
                   COALESCE(speaker_confidence, confidence) AS conf
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        words_by_segment: dict[int, list[dict]] = {}
        for row in conn.execute(
            """
            SELECT segment_id, start_ms, end_ms, text
            FROM transcript_words
            WHERE meeting_id = ?
            ORDER BY segment_id, start_ms
            """,
            (meeting_id,),
        ).fetchall():
            words_by_segment.setdefault(int(row["segment_id"]), []).append(dict(row))

    proposals: list[SplitProposal] = []
    for index, row in enumerate(rows):
        confidence = row["conf"]
        if confidence is None or float(confidence) >= min_conf:
            continue
        # Need a NEXT segment to attribute the tail to — if this is the
        # last segment, we can't usefully propose a split.
        if index + 1 >= len(rows):
            continue
        next_speaker_id = str(rows[index + 1]["diarization_speaker_id"])
        # Skip if the next segment is the same speaker — nothing to fix.
        if next_speaker_id == str(row["diarization_speaker_id"]):
            continue

        match = _find_tail_opener(str(row["text"]))
        if not match:
            continue
        opener_char_start = match.start()
        head_text = str(row["text"])[:opener_char_start].rstrip()
        tail_text = str(row["text"])[opener_char_start:].strip()
        if not tail_text or not head_text:
            continue

        split_at_ms = _locate_split_ms(
            words=words_by_segment.get(int(row["id"]), []),
            head_text=head_text,
            segment_start_ms=int(row["start_ms"]),
            segment_end_ms=int(row["end_ms"]),
        )

        # Confidence: base 0.6 for low-conf segment with a clear opener.
        # Boost if the gap is genuinely below 0.4 (very low conf), or if
        # words were available for a real timestamp (vs heuristic estimate).
        score = 0.6
        if confidence is not None and float(confidence) < 0.4:
            score += 0.1
        if words_by_segment.get(int(row["id"])):
            score += 0.05

        proposals.append(
            SplitProposal(
                segment_id=int(row["id"]),
                split_at_ms=split_at_ms,
                tail_text=tail_text,
                head_text=head_text,
                tail_speaker_id=next_speaker_id,
                confidence=round(min(0.85, score), 3),
                evidence=(
                    f"low spk-conf {confidence:.2f}; "
                    f"tail opens with '{match.group('opener')}'"
                ),
            )
        )
    return proposals


def persist_segment_split_proposals(
    config: AppConfig, meeting_id: int
) -> dict:
    """Run the detector and persist proposals as review_items.

    v0.2.11: three-tier auto-accept. Each proposal is classified by
    its heuristic confidence:

      - silent (>= auto_apply_silent_threshold, default 0.90):
        applied immediately, stored with status='auto_applied' and
        payload tier='silent' for the audit log.
      - toast (>= auto_apply_toast_threshold, default 0.70):
        applied immediately, status='auto_applied' tier='toast'. The
        frontend shows a small "Applied N corrections" notice with an
        expand affordance.
      - manual (below toast threshold): status='open' as before.

    `auto_apply_enabled=False` reverts to v0.2.10 behavior (every
    proposal manual). Idempotent: deletes any prior status='open' rows
    for this meeting first (auto_applied rows persist as audit trail).

    Returns dict with `total`, `auto_applied`, `manual` counts.
    """
    if not getattr(config.repair, "segment_split_enabled", True):
        return {"total": 0, "auto_applied": 0, "manual": 0}
    auto_enabled = bool(getattr(config.repair, "auto_apply_enabled", True))
    silent_thr = float(getattr(config.repair, "auto_apply_silent_threshold", 0.90))
    toast_thr = float(getattr(config.repair, "auto_apply_toast_threshold", 0.70))
    proposals = propose_segment_splits(config, meeting_id)

    auto_applied = 0
    manual = 0
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            DELETE FROM review_items
            WHERE meeting_id = ? AND kind = 'segment_split_proposal'
              AND status = 'open'
            """,
            (meeting_id,),
        )
        for proposal in proposals:
            tier = tier_for_confidence(
                proposal.confidence, auto_enabled, silent_thr, toast_thr
            )
            payload = {
                "segment_id": proposal.segment_id,
                "split_at_ms": proposal.split_at_ms,
                "head_text": proposal.head_text,
                "tail_text": proposal.tail_text,
                "tail_speaker_id": proposal.tail_speaker_id,
                "evidence": proposal.evidence,
                "tier": tier,
            }
            status = "auto_applied" if tier in ("silent", "toast") else "open"
            cursor = conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence,
                   source_segment_ids, status)
                VALUES (?, 'segment_split_proposal', ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    f"Split segment {proposal.segment_id} at {proposal.split_at_ms}ms",
                    json.dumps(payload),
                    proposal.confidence,
                    json.dumps([proposal.segment_id]),
                    status,
                ),
            )
            review_item_id = int(cursor.lastrowid)
            if status == "auto_applied":
                # Apply the split immediately. Use the same connection
                # so the whole batch is one atomic write — if the
                # apply fails we demote the row to manual review.
                try:
                    _apply_split_inline(conn, payload, meeting_id)
                    conn.execute(
                        "UPDATE review_items SET resolved_at = "
                        "CURRENT_TIMESTAMP WHERE id = ?",
                        (review_item_id,),
                    )
                    auto_applied += 1
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _LOG.warning(
                        "auto-apply of split proposal %s failed: %s",
                        review_item_id,
                        exc,
                    )
                    # Demote to manual: clear status so the user can
                    # try by hand.
                    conn.execute(
                        "UPDATE review_items SET status = 'open', "
                        "resolved_at = NULL WHERE id = ?",
                        (review_item_id,),
                    )
                    manual += 1
            else:
                manual += 1
    return {"total": len(proposals), "auto_applied": auto_applied, "manual": manual}


def _apply_split_inline(conn, payload: dict, meeting_id: int) -> None:
    """Apply a segment-split using the same writes as
    `accept_split_proposal`, but operating on an already-open
    connection (the persist call's txn) so the whole batch can be one
    atomic write. Does NOT modify the review_item — the caller owns
    that side-effect.
    """
    segment_id = int(payload["segment_id"])
    split_at_ms = int(payload["split_at_ms"])
    head_text = str(payload["head_text"])
    tail_text = str(payload["tail_text"])
    tail_speaker_id = str(payload["tail_speaker_id"])

    segment = conn.execute(
        "SELECT * FROM transcript_segments WHERE id = ? AND meeting_id = ?",
        (segment_id, meeting_id),
    ).fetchone()
    if not segment:
        raise ValueError("segment_not_found")

    # Same drift check as the manual accept path — if the user edited
    # the segment text between proposal time and now (rare in
    # auto-apply since it runs immediately at persist time, but cheap
    # to verify), bail.
    def _normalize(text: str) -> str:
        return " ".join(str(text or "").split())

    snapshot = _normalize(f"{head_text} {tail_text}")
    live = _normalize(str(segment["text"] or ""))
    if snapshot != live:
        raise ValueError("segment_changed")

    tail_cursor = conn.execute(
        """
        INSERT INTO transcript_segments
          (meeting_id, start_ms, end_ms, text, diarization_speaker_id,
           assigned_person_id, confidence, text_confidence, speaker_confidence)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            meeting_id,
            split_at_ms,
            int(segment["end_ms"]),
            tail_text,
            tail_speaker_id,
            segment["confidence"],
            segment["text_confidence"],
            None,
        ),
    )
    new_tail_id = int(tail_cursor.lastrowid)

    conn.execute(
        "UPDATE transcript_segments SET end_ms = ?, text = ? WHERE id = ?",
        (split_at_ms, head_text, segment_id),
    )

    conn.execute(
        """
        UPDATE transcript_words
        SET segment_id = ?
        WHERE meeting_id = ? AND segment_id = ? AND start_ms >= ?
        """,
        (new_tail_id, meeting_id, segment_id, split_at_ms),
    )


def accept_split_proposal(
    config: AppConfig, meeting_id: int, review_item_id: int
) -> dict:
    """Apply a segment-split proposal: shrink the original segment's
    end_ms / text to the head portion, insert a new segment for the
    tail with the proposed speaker_id, and reassign any transcript_words
    rows that fall at/after the split point.

    Atomic: all three writes happen in a single `connect()` block. Marks
    the review item as 'resolved' on success.

    Raises ValueError("review_item_not_found" | "not_a_split_proposal" |
    "already_resolved" | "segment_not_found").
    """
    with connect(config.paths.database_path) as conn:
        item = conn.execute(
            "SELECT id, kind, status, payload_json FROM review_items "
            "WHERE id = ? AND meeting_id = ?",
            (review_item_id, meeting_id),
        ).fetchone()
        if not item:
            raise ValueError("review_item_not_found")
        if item["kind"] != "segment_split_proposal":
            raise ValueError("not_a_split_proposal")
        if item["status"] == "resolved":
            raise ValueError("already_resolved")
        payload = json.loads(item["payload_json"] or "{}")
        segment_id = int(payload["segment_id"])
        split_at_ms = int(payload["split_at_ms"])
        head_text = str(payload["head_text"])
        tail_text = str(payload["tail_text"])
        tail_speaker_id = str(payload["tail_speaker_id"])

        segment = conn.execute(
            "SELECT * FROM transcript_segments WHERE id = ? AND meeting_id = ?",
            (segment_id, meeting_id),
        ).fetchone()
        if not segment:
            raise ValueError("segment_not_found")

        # v0.2.10 audit H2: refuse to accept if the user edited the
        # segment after the proposal was persisted. The proposal carries
        # a snapshot of head_text + tail_text; if the live text no
        # longer matches that snapshot (modulo whitespace), accepting
        # would silently overwrite the user's edits with stale content.
        # Normalize by collapsing internal whitespace so we don't trip
        # on cosmetic differences between the snapshot and the stored
        # row.
        def _normalize(text: str) -> str:
            return " ".join(str(text or "").split())

        snapshot = _normalize(f"{head_text} {tail_text}")
        live = _normalize(str(segment["text"] or ""))
        if snapshot != live:
            raise ValueError("segment_changed")

        # v0.2.10 audit M3: race on concurrent accept. Resolve the
        # review item FIRST via a conditional UPDATE; if rowcount is 0
        # someone already accepted/rejected, so we bail before
        # duplicating the segment. SQLite serializes writes so only one
        # caller wins the update.
        cursor = conn.execute(
            "UPDATE review_items SET status = 'resolved', "
            "resolved_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'open'",
            (review_item_id,),
        )
        if cursor.rowcount == 0:
            raise ValueError("already_resolved")

        # Insert the tail segment first so we have an id to repoint
        # words at. Keep the head's diarization speaker_id on the head;
        # use the proposed `tail_speaker_id` on the tail.
        tail_cursor = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id, confidence, text_confidence, speaker_confidence)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                meeting_id,
                split_at_ms,
                int(segment["end_ms"]),
                tail_text,
                tail_speaker_id,
                segment["confidence"],
                segment["text_confidence"],
                # Reset speaker_confidence on the tail — the diarizer
                # didn't actually decide this boundary, the user did.
                None,
            ),
        )
        new_tail_id = int(tail_cursor.lastrowid)

        # Shrink the original head segment.
        conn.execute(
            "UPDATE transcript_segments SET end_ms = ?, text = ? WHERE id = ?",
            (split_at_ms, head_text, segment_id),
        )

        # Repoint words that fall in the tail half to the new segment.
        conn.execute(
            """
            UPDATE transcript_words
            SET segment_id = ?
            WHERE meeting_id = ? AND segment_id = ? AND start_ms >= ?
            """,
            (new_tail_id, meeting_id, segment_id, split_at_ms),
        )

    return {
        "status": "ok",
        "head_segment_id": segment_id,
        "tail_segment_id": new_tail_id,
        "split_at_ms": split_at_ms,
        "tail_speaker_id": tail_speaker_id,
    }


def reject_split_proposal(
    config: AppConfig, meeting_id: int, review_item_id: int
) -> dict:
    """Mark a split proposal rejected. Idempotent on already-rejected;
    raises ValueError on already-resolved (the user can't 'unresolve'
    an applied split through reject).
    """
    with connect(config.paths.database_path) as conn:
        item = conn.execute(
            "SELECT kind, status FROM review_items WHERE id = ? AND meeting_id = ?",
            (review_item_id, meeting_id),
        ).fetchone()
        if not item:
            raise ValueError("review_item_not_found")
        if item["kind"] != "segment_split_proposal":
            raise ValueError("not_a_split_proposal")
        if item["status"] == "rejected":
            return {"status": "ok", "result": "already_rejected"}
        if item["status"] == "resolved":
            raise ValueError("already_resolved")
        conn.execute(
            "UPDATE review_items SET status = 'rejected', resolved_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (review_item_id,),
        )
    return {"status": "ok", "result": "rejected"}


def _find_tail_opener(text: str) -> re.Match[str] | None:
    """Return the first discourse-opener match that starts in the back
    half of the text. None if no opener is found there. We deliberately
    skip openers in the first half — those are almost always the
    segment's own speaker's normal sentence transitions.
    """
    if not text or len(text) < 30:
        return None
    half = len(text) // 2
    best: re.Match[str] | None = None
    for pattern in _DISCOURSE_OPENER_PATTERNS:
        for match in pattern.finditer(text):
            if match.start() < half:
                continue
            if best is None or match.start() < best.start():
                best = match
    return best


def _locate_split_ms(
    words: list[dict],
    head_text: str,
    segment_start_ms: int,
    segment_end_ms: int,
) -> int:
    """Find the word-level start_ms of the first word *after* `head_text`.

    Approach: count whitespace-separated tokens in head_text, then index
    into `words` by that count. If words are missing (some ASR paths
    don't persist per-word timestamps), fall back to a proportional
    estimate based on character count.
    """
    head_word_count = len(head_text.split())
    if words and head_word_count < len(words):
        return int(words[head_word_count]["start_ms"])
    # Proportional fallback: estimate by character ratio of the full text.
    # head_text is roughly head_word_count tokens — use char-based ratio
    # against the segment duration.
    total_chars = max(1, head_word_count * 5)  # ~5 chars per word avg
    duration = max(1, segment_end_ms - segment_start_ms)
    ratio = min(0.95, max(0.05, total_chars / (total_chars + 20)))
    return int(segment_start_ms + duration * ratio)
