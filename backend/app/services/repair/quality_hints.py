"""Inject quality hints into synthesis prompts.

The lite-stack diarizer's gaps are partially compensated by the v0.2.2
overlap detector and v0.2.4 speaker re-attributer — but those hints
only help if the downstream synthesis pass knows about them. Otherwise
the summary LLM confidently writes "Alice said X" even when the
diarizer's "Alice" label for that segment was already flagged as
likely-wrong.

This module assembles a short sidebar of hints for each chunk that the
extraction pass appends to the user message before sending to the LLM.
The hints tell the LLM:
  - Which segments had detected overlap → hedge attribution
  - Which segments have low-confidence speaker labels → prefer
    impersonal phrasing ("one speaker said...") over confident
    name attribution

Added in v0.2.6. No new config field — uses the data the v0.2.2 and
v0.2.4 passes already persisted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkQualityHints:
    """Per-chunk quality hint summary for the synthesis prompt."""

    overlap_segment_ids: list[int]
    reattribution_segment_ids: list[int]
    # Pre-rendered short string suitable for appending to the user prompt.
    # Empty if there are no hints for this chunk.
    prompt_fragment: str


def gather_hints_for_chunk(
    config: AppConfig,
    meeting_id: int,
    segment_ids: list[int],
) -> ChunkQualityHints:
    """Read v0.2.2 overlap hints + v0.2.4 reattribution proposals
    whose segments fall inside this chunk. Return a structured hint
    block + a pre-rendered prompt fragment.

    Returns an empty fragment if no relevant hints exist for the chunk
    (the caller should then skip the append).
    """
    if not segment_ids:
        return ChunkQualityHints([], [], "")

    placeholders = ",".join("?" for _ in segment_ids)
    overlap_ids: list[int] = []
    reattr: list[tuple[int, str, str]] = []  # (segment_id, current, proposed)
    try:
        with connect(config.paths.database_path) as conn:
            overlap_rows = conn.execute(
                f"""
                SELECT segment_id
                FROM segment_overlap_hints
                WHERE meeting_id = ? AND segment_id IN ({placeholders})
                """,  # nosec B608 — placeholders only
                (meeting_id, *segment_ids),
            ).fetchall()
            overlap_ids = sorted({int(row["segment_id"]) for row in overlap_rows})

            reattr_rows = conn.execute(
                """
                SELECT payload_json
                FROM review_items
                WHERE meeting_id = ?
                  AND kind = 'speaker_reattribution'
                  AND status = 'open'
                """,
                (meeting_id,),
            ).fetchall()
            for row in reattr_rows:
                try:
                    payload = json.loads(row["payload_json"])
                    seg_id = int(payload.get("segment_id"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if seg_id in segment_ids:
                    reattr.append(
                        (
                            seg_id,
                            str(payload.get("current_speaker", "")),
                            str(payload.get("proposed_speaker", "")),
                        )
                    )
    except Exception as exc:  # noqa: BLE001 — repair info is best-effort
        _LOG.debug("quality hint lookup failed: %s", exc)
        return ChunkQualityHints([], [], "")

    fragment = _render_fragment(overlap_ids, reattr)
    return ChunkQualityHints(
        overlap_segment_ids=overlap_ids,
        reattribution_segment_ids=[r[0] for r in reattr],
        prompt_fragment=fragment,
    )


def _render_fragment(
    overlap_ids: list[int],
    reattribution: list[tuple[int, str, str]],
) -> str:
    """Compose a one-block hint string the LLM can act on. Empty if
    nothing to report."""
    if not overlap_ids and not reattribution:
        return ""
    lines = ["", "QUALITY HINTS (apply these when writing summaries):"]
    if overlap_ids:
        ids_str = ", ".join(f"#{sid}" for sid in overlap_ids[:24])
        lines.append(
            f"- Likely overlapping speech at segments: {ids_str}. When "
            "summarizing these moments, hedge attribution (e.g. 'speakers "
            "talked over each other,' 'one speaker interjected') rather "
            "than confidently quoting one speaker."
        )
    if reattribution:
        # Compact: only show segment ids + the alternative label; the LLM
        # can read "uncertain — could be Alice" and adjust phrasing.
        items = ", ".join(
            f"#{sid} (currently labeled '{cur}', maybe '{prop}')"
            for sid, cur, prop in reattribution[:24]
        )
        lines.append(
            f"- Speaker labels uncertain at: {items}. For these "
            "segments, prefer impersonal phrasing ('one speaker said,' "
            "'a participant raised') instead of confidently asserting "
            "the labeled name. Do NOT invent the alternative name in "
            "the summary; just hedge."
        )
    return "\n".join(lines)


def augmented_chunk_text(
    config: AppConfig,
    meeting_id: int,
    chunk_text: str,
    segment_ids: list[int],
) -> str:
    """Convenience: take a chunk's transcript text + its segment ids,
    return the text with any quality hints appended as a sidebar block.

    No-op if no hints apply.
    """
    hints = gather_hints_for_chunk(config, meeting_id, segment_ids)
    if not hints.prompt_fragment:
        return chunk_text
    return chunk_text + "\n" + hints.prompt_fragment
