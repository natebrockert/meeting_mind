"""Canonical transcript rendering for LLM prompts.

A single source of truth for "what does this meeting's transcript look
like when we hand it to a model." The point isn't compactness — it's
byte-identical output across services so the same `cache_prefix=...`
matches across `llm_drivers`, future synthesis passes, and any other
caller that wants to share a warm prompt cache for the same meeting.

The pre-existing per-service renderers (`llm_drivers._build_llm_context`,
`reflections._render_transcript_for_prompt`) diverged in subtle ways —
trailing whitespace, inclusion of owner `(you)` tags, label resolution
order — so a cache_prefix produced by one would never byte-match
another. With this helper, any caller that wants cross-service cache
hits passes `render_canonical_transcript_for_llm(config, meeting_id)`
as their `cache_prefix`.

When a service has prompt-shape needs that the canonical form can't
serve (e.g. Reflections wants `(you)` tags inline to anchor owner-
specific judgements), it should keep its own renderer for the user-
message body and still pass the canonical version as `cache_prefix`.
The cache match runs over the FIRST message; later messages can vary.
"""

from __future__ import annotations

from app.config import AppConfig
from app.db.database import connect


def render_canonical_transcript_for_llm(
    config: AppConfig, meeting_id: int
) -> str:
    """Render a meeting's transcript in the canonical `[id] Speaker: text`
    one-line-per-segment form.

    Speaker labels resolve to the confirmed `approved_label` from
    `speaker_assignments` when one exists, otherwise fall back to the
    raw `diarization_speaker_id`. Text is stripped of surrounding
    whitespace; no other normalization is applied so the output stays
    deterministic across calls.

    Returns an empty string when the meeting has no segments.
    """
    with connect(config.paths.database_path) as conn:
        # `ORDER BY start_ms, id` — the id tiebreaker is load-bearing.
        # Without it, two segments that share a start_ms can sort in
        # either order across SQLite versions / collations, which would
        # change the rendered prefix bytes and miss every prompt cache.
        # `llm_drivers._build_llm_context` currently only orders by
        # `start_ms`; downstream coercion is id-keyed so it doesn't
        # care, but it means the legacy renderer would diverge here on
        # shared-start_ms inputs. New cache-aware callers MUST use this
        # helper, not the legacy builder.
        segment_rows = conn.execute(
            """
            SELECT id, diarization_speaker_id, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms, id
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

    labels: dict[str, str] = {}
    for row in assignment_rows:
        sid = str(row["diarization_speaker_id"])
        approved = row["approved_label"]
        # Only the confirmed-and-named pairs get to override the raw
        # diarization id. Unconfirmed suggestions can be wrong and would
        # poison the cache prefix if they later flip.
        if approved and bool(row["confirmed_by_user"]):
            labels[sid] = str(approved)

    lines: list[str] = []
    for row in segment_rows:
        sid = str(row["diarization_speaker_id"])
        speaker_label = labels.get(sid, sid)
        text = str(row["text"] or "").strip()
        lines.append(f"[{row['id']}] {speaker_label}: {text}")
    return "\n".join(lines)
