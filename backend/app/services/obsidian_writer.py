from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from app.config import AppConfig
from app.db.database import PER_MEETING_LLM_CACHE_TABLES, connect
from app.services.review import get_unapproved_speaker_ids
from app.services.transcript_quality import safe_transcript_text


@dataclass(frozen=True)
class MeetingPreview:
    """Rendered meeting note preview before promotion into the vault."""

    slug: str
    created_at: str
    content: str


def managed_section(content: str) -> str:
    """Return generated section content without hidden Obsidian marker artifacts."""
    return content.rstrip()


def _contained_audio_path(config: AppConfig, stored: str | None) -> Path | None:
    """Resolve a DB-stored audio path; return None unless it's in a data dir.

    Mirrors `routes._safe_repo_path` but returns None instead of raising
    so callers (delete_meeting, etc.) can fall back cleanly. Audit H-C.
    """
    if not stored:
        return None
    candidate = (config.paths.repo_root / stored).resolve()
    allowed_roots = [
        config.paths.processed_dir.resolve(),
        config.paths.delete_review_dir.resolve(),
    ]
    archive_dir = getattr(config.paths, "archive_dir", None)
    if archive_dir is not None:
        allowed_roots.append(Path(archive_dir).resolve())
    for root in allowed_roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return candidate
    return None


# These headings are the merge contract for generated content. Unknown H2 sections
# are treated as user-owned notes and preserved across MeetingMind rewrites.
_MANAGED_SECTION_HEADINGS = {
    "summary": "Summary",
    "key-takeaways": "Key Takeaways",
    "decisions": "Decisions",
    "action-items": "Action Items",
    "open-questions": "Open Questions",
    "people": "People",
    "workstreams": "Workstreams",
    "transcript": "Transcript",
    "meetingmind-meetings": "Meetings",
    "meetingmind-profile": "Profile",
    "meetingmind-related-people": "Related People",
    "open-actions": "Open Actions",
}
_MANAGED_HEADING_NAMES = {heading: name for name, heading in _MANAGED_SECTION_HEADINGS.items()}


def write_staged_meeting(config: AppConfig, meeting_id: int) -> Path:
    data = load_meeting_export_data(config, meeting_id)
    output = config.paths.vault_dir / "Staging" / f"{data['slug']}.md"
    content = render_meeting_note(data, status="staged")
    content = _write_generated_note(output, content)
    _record_export(config, meeting_id, output, content)
    return output


def promote_meeting(config: AppConfig, meeting_id: int) -> Path:
    unapproved = get_unapproved_speaker_ids(config, meeting_id)
    if config.review.speaker_assignment_required and unapproved:
        raise ValueError(f"Speaker approval required before promotion: {', '.join(unapproved)}")
    data = load_meeting_export_data(config, meeting_id)
    year = data["created_at"][:4]
    output_dir = config.paths.vault_dir / "Meetings" / year
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{data['slug']}.md"
    content = render_meeting_note(data, status="promoted")
    content = _write_generated_note(output, content)
    _record_export(config, meeting_id, output, content)
    with connect(config.paths.database_path) as conn:
        conn.execute("UPDATE meetings SET status = ? WHERE id = ?", ("promoted", meeting_id))
    write_person_pages(config, data)
    write_workstream_pages(config, data)
    write_action_rollup(config)
    _remove_staged_note(config, data["slug"])
    cleanup_generated_orphans(config)
    return output


def render_promoted_meeting_preview(config: AppConfig, meeting_id: int) -> MeetingPreview:
    data = load_meeting_export_data(config, meeting_id)
    return MeetingPreview(
        slug=data["slug"],
        created_at=data["created_at"],
        content=render_meeting_note(data, status="promoted"),
    )


def delete_generated_workstream(config: AppConfig, title: str) -> dict:
    clean_title = title.strip()
    if not clean_title:
        return {"removed": 0, "promoted": 0}
    with connect(config.paths.database_path) as conn:
        meeting_rows = conn.execute(
            """
            SELECT DISTINCT meeting_id
            FROM review_items
            WHERE kind = 'workstream' AND lower(title) = lower(?)
            """,
            (clean_title,),
        ).fetchall()
        removed = conn.execute(
            """
            DELETE FROM review_items
            WHERE kind = 'workstream' AND lower(title) = lower(?)
            """,
            (clean_title,),
        ).rowcount
    promoted = 0
    for row in meeting_rows:
        try:
            promote_meeting(config, int(row["meeting_id"]))
            promoted += 1
        except ValueError:
            continue
    _remove_generated_workstream_page(config, clean_title)
    cleanup_generated_orphans(config)
    return {"removed": int(removed or 0), "promoted": promoted}


def rename_generated_workstream(config: AppConfig, old_title: str, new_title: str) -> dict:
    """Rename every workstream review item from old_title → new_title (case-insensitive)
    and re-promote affected meetings so vault notes carry the new label.
    """
    old = old_title.strip()
    new = new_title.strip()
    if not old or not new:
        raise ValueError("rename_requires_old_and_new_titles")
    if old.lower() == new.lower():
        return {"renamed": 0, "promoted": 0}
    with connect(config.paths.database_path) as conn:
        meeting_rows = conn.execute(
            """
            SELECT DISTINCT meeting_id
            FROM review_items
            WHERE kind = 'workstream' AND lower(title) = lower(?)
            """,
            (old,),
        ).fetchall()
        renamed = conn.execute(
            """
            UPDATE review_items
            SET title = ?
            WHERE kind = 'workstream' AND lower(title) = lower(?)
            """,
            (new, old),
        ).rowcount
    promoted = 0
    failures: list[Exception] = []
    for row in meeting_rows:
        try:
            promote_meeting(config, int(row["meeting_id"]))
            promoted += 1
        except ValueError:
            # Promotion gated on speaker review — skip but don't roll back.
            continue
        except Exception as exc:  # noqa: BLE001 — caller wants partial-failure visibility
            failures.append(exc)
    # Only drop the old generated workstream page once every meeting whose
    # vault note still pointed at it has been re-promoted successfully. If any
    # promotion failed unexpectedly we keep the page so the user can retry
    # without orphan-link cleanup throwing away references.
    if not failures:
        _remove_generated_workstream_page(config, old)
        cleanup_generated_orphans(config)
    return {
        "renamed": int(renamed or 0),
        "promoted": promoted,
        "failures": [str(exc) for exc in failures],
    }


# All meeting_id-keyed tables that need a cascading delete. Listed explicitly
# because the schema does not declare ON DELETE CASCADE.
#
# LLM cache tables (`PER_MEETING_LLM_CACHE_TABLES`) are folded in at module
# load via tuple concat — the source of truth lives in `app.db.database`
# so a new cache table added there gets cleaned up by `delete_meeting`
# automatically. Without this, a hard-delete would leave orphan rows in
# `meeting_llm_drivers`, `meeting_driver_enrichment`, and
# `reflection_observations` — none of which are FK-linked to `meetings`
# (so SQLite doesn't catch it) but which still hold per-meeting payloads
# that should die with the meeting.
#
# Note: `reflection_observations` has a composite PK
# `(meeting_id, owner_person_id)` — multiple rows can exist per meeting
# when a meeting has been viewed under different owner identities.
# `DELETE … WHERE meeting_id = ?` correctly removes every row regardless
# of `owner_person_id` (SQLite doesn't require a full-PK match for DELETE).
_MEETING_LINKED_TABLES = (
    "transcript_corrections",
    "transcript_candidates",
    "transcript_words",
    "speaker_assignment_evidence",
    "speaker_match_suggestions",
    "speaker_profile_observations",
    "speaker_assignments",
    # v0.2.10 fix: tables that have FOREIGN KEY to transcript_segments
    # need to drop BEFORE transcript_segments itself. With
    # PRAGMA foreign_keys=ON (set on every connection), deleting
    # transcript_segments while these still reference rows triggers
    # FOREIGN KEY constraint failed and the whole delete_meeting txn
    # rolls back as a 500 to the API. Three tables added after
    # v0.1.x were missing from this list and silently breaking deletes
    # for any meeting that had overlap hints, segment comments, or
    # cached key terms: segment_overlap_hints (v0.2.2),
    # segment_comments (segment comment feature), and meeting_key_terms
    # (synthesis cache).
    "segment_overlap_hints",
    "segment_comments",
    "transcript_segments",
    "action_items",
    "review_items",
    "meeting_workstreams",
    "processing_jobs",
    "obsidian_exports",
) + PER_MEETING_LLM_CACHE_TABLES


def delete_meeting(config: AppConfig, meeting_id: int) -> dict:
    """Hard-delete a meeting: vault note, audio source, embedding/clip artifacts,
    every row in any meeting_id-keyed table, then sweep orphaned vault pages.
    """
    removed_paths: list[str] = []
    with connect(config.paths.database_path) as conn:
        meeting_row = conn.execute(
            "SELECT id, slug, created_at FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if not meeting_row:
            raise ValueError("meeting_not_found")
        slug = meeting_row["slug"]
        year = (meeting_row["created_at"] or "")[:4]

        # Remove every meeting_id-keyed row. _MEETING_LINKED_TABLES is a
        # module-level tuple of hardcoded literals, not user input.
        for table in _MEETING_LINKED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))  # nosec B608

        # Remove the on-disk audio source plus its db row
        source_rows = conn.execute(
            "SELECT storage_path FROM source_files WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()
        for src in source_rows:
            stored = src["storage_path"]
            if stored:
                # Audit finding H-C: a tampered DB row pointing at e.g.
                # /etc/hosts would let delete_meeting unlink arbitrary
                # files. Enforce the same data-dir containment used by
                # routes._safe_repo_path before any unlink.
                audio_path = _contained_audio_path(config, stored)
                if audio_path is not None and audio_path.exists():
                    try:
                        audio_path.unlink()
                        removed_paths.append(str(audio_path))
                    except OSError:
                        pass
        conn.execute("DELETE FROM source_files WHERE meeting_id = ?", (meeting_id,))

        # Drop the meeting row itself
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

    # Remove the promoted vault note + any staged note + per-meeting artifacts
    if year and slug:
        vault_note = config.paths.vault_dir / "Meetings" / year / f"{slug}.md"
        if vault_note.exists():
            vault_note.unlink()
            removed_paths.append(str(vault_note))
    _remove_staged_note(config, slug)
    for sub in ("speaker-embedding-clips", "asr-candidates"):
        directory = config.paths.runtime_dir / sub
        if not directory.exists():
            continue
        for path in directory.glob(f"*meeting-{meeting_id}-*"):
            try:
                path.unlink()
                removed_paths.append(str(path))
            except OSError:
                continue

    cleanup_generated_orphans(config)
    return {"meeting_id": meeting_id, "removed_paths": removed_paths}


def _workstream_confidences(config: AppConfig, meeting_id: int) -> dict[str, float]:
    """Per-workstream confidence for a single meeting from review_items.
    Returns a dict mapping the workstream title (as stored) to its confidence
    in [0, 1]. Missing rows simply aren't included.
    """
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT title, confidence
            FROM review_items
            WHERE kind = 'workstream' AND meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()
    return {
        row["title"]: float(row["confidence"])
        for row in rows
        if row["confidence"] is not None
    }


def _workstream_descriptions(config: AppConfig, meeting_id: int) -> dict[str, str]:
    """Per-workstream one-line description from review_items.payloadjson.

    New extractions store a `description` field on ExtractedWorkstream that the
    Topics tile reads. For meetings extracted before the field existed the
    payload won't have it; those entries are simply omitted and the frontend
    falls back to deriving a snippet from source segments.
    """
    descriptions: dict[str, str] = {}
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT title, payload_json
            FROM review_items
            WHERE kind = 'workstream' AND meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        description = str(payload.get("description") or "").strip()
        if description:
            descriptions[row["title"]] = description
    return descriptions


def build_meeting_overview(config: AppConfig, meeting_id: int) -> dict:
    data = load_meeting_export_data(config, meeting_id)
    decisions = [
        _strip_markdown_task(item)
        for item in _lines_without_markers(_render_claims(data["decisions"]))
    ]
    actions = [
        _strip_markdown_task(item)
        for item in _lines_without_markers(_render_actions(data["actions"]))
    ]
    open_questions = [
        _strip_markdown_task(item)
        for item in _lines_without_markers(_render_claims(data["open_questions"]))
    ]
    # Structured detail variants surface the new optional fields (rationale,
    # status, raised_by, due_date_source) to the dashboard while the flat
    # string arrays above stay stable for HTML/PDF exports and existing
    # callers that just want labels.
    decision_details = [
        {
            "decision": item.get("text", ""),
            "rationale": item.get("rationale"),
            "source_segment_ids": _source_segment_ids(item.get("source_segment_ids")),
        }
        for item in data["decisions"]
    ]
    open_question_details = [
        {
            "question": item.get("text", ""),
            "status": item.get("status", "unanswered"),
            "raised_by": item.get("raised_by"),
            "addressed_to": item.get("addressed_to"),
            "source_segment_ids": _source_segment_ids(item.get("source_segment_ids")),
        }
        for item in data["open_questions"]
    ]
    action_details = [
        {
            "text": action.get("text", ""),
            "due_date": action.get("due_date"),
            "due_date_source": action.get("due_date_source"),
            "priority": action.get("priority", "normal"),
            "start_ms": action.get("start_ms"),
            "source_segment_ids": _source_segment_ids(action.get("source_segment_ids")),
            # Owner attribution flows through from action_items so the
            # frontend's For-You filter and any per-owner exports can
            # match by stable id rather than scraping the prose.
            "owner_person_id": action.get("owner_person_id"),
            "owner_display_name": action.get("owner_display_name"),
            # Clustering fields: members of a near-duplicate cluster are
            # absent from this list (filtered in load_meeting_export_data);
            # the canonical surfaces them under `cluster_members` for the
            # "N related mentions" disclosure, plus `due_date_history`
            # when an earlier date was superseded.
            "cluster_members": [
                {
                    "text": m.get("text", ""),
                    "owner_person_id": m.get("owner_person_id"),
                    "due_date": m.get("due_date"),
                    "source_segment_ids": _source_segment_ids(
                        m.get("source_segment_ids")
                    ),
                }
                for m in action.get("cluster_members", [])
            ],
            "due_date_history": action.get("due_date_history", []),
        }
        for action in data["actions"]
    ]
    confidences = _workstream_confidences(config, meeting_id)
    descriptions = _workstream_descriptions(config, meeting_id)
    from app.services.conversation_drivers import compute_drivers_and_cog
    from app.services.meeting_health import compute_meeting_health
    from app.services.owner import annotate_overview_for_owner, load_owner

    meeting_health = compute_meeting_health(config, meeting_id).model_dump()
    drivers, cog = compute_drivers_and_cog(config, meeting_id)
    conversation_drivers = [d.model_dump() for d in drivers]
    center_of_gravity = cog.model_dump()

    overview_dict: dict = {
        "id": data["id"],
        "title": data["title"],
        "slug": data["slug"],
        "status": data["status"],
        "created_at": data["created_at"],
        "duration_seconds": data["duration_seconds"],
        "speaker_status": data["speaker_status"],
        "source_file": Path(data["imported_path"]).name,
        "summary": data["summary"] or "Review pending.",
        "tldr": data.get("tldr", ""),
        "briefing": data.get("briefing", []),
        "executive_recap": data.get("executive_recap"),
        "participant_contributions": data.get("participant_contributions", []),
        "chapter_markers": data.get("chapter_markers", []),
        "themes": data.get("themes", []),
        "stat_callouts": data.get("stat_callouts", []),
        "tension_points": data.get("tension_points", []),
        "key_takeaways": _lines_without_markers(_key_takeaways(data)),
        "participants": data["speakers"],
        "workstreams": data["workstreams"],
        "workstream_confidences": confidences,
        "workstream_descriptions": descriptions,
        "decisions": decisions,
        "decision_details": decision_details,
        "actions": actions,
        "action_details": action_details,
        "open_questions": open_questions,
        "open_question_details": open_question_details,
        "meeting_health": meeting_health,
        "conversation_drivers": conversation_drivers,
        "center_of_gravity": center_of_gravity,
        "obsidian_sections": {
            "summary": data["summary"] or "Review pending.",
            "key_takeaways": _key_takeaways(data),
            "decisions": _render_claims_md(data["decisions"]) or "No explicit decisions captured.",
            "action_items": _render_actions(data["actions"]) or "No action items captured.",
            "open_questions": (
                _render_claims_md(data["open_questions"]) or "No open questions captured."
            ),
            "people": "\n".join(f"- {speaker}" for speaker in data["speakers"])
            or "- Pending speaker review",
            "workstreams": "\n".join(f"- {stream}" for stream in data["workstreams"])
            or "- Pending workstream review",
        },
    }
    annotate_overview_for_owner(overview_dict, load_owner(config))
    return overview_dict


def render_meeting_note(data: dict, status: str) -> str:
    transcript = "\n\n".join(
        f"**{segment['speaker']}** [{segment['timestamp']}]: {segment['text']}"
        for segment in data["segments"]
    )
    actions = _render_actions(data["actions"])
    decisions = _render_claims_md(data["decisions"])
    open_questions = _render_claims_md(data["open_questions"])
    key_takeaways = _key_takeaways(data)
    people = "\n".join(
        f"- [[People/{_vault_note_stem(speaker)}|{_vault_alias(speaker)}]]"
        for speaker in data["speakers"]
    )
    workstreams = (
        "\n".join(
            f"- [[Workstreams/{_vault_note_stem(stream)}|{_vault_alias(stream)}]]"
            for stream in data["workstreams"]
        )
        or "- Pending workstream review"
    )
    summary = data.get("summary") or "Review pending."
    sections = [
        ("Summary", managed_section(summary)),
        ("Key Takeaways", managed_section(key_takeaways)),
    ]
    if decisions:
        sections.append(("Decisions", managed_section(decisions)))
    sections.append(
        (
            "Action Items",
            managed_section(actions or "- No action items captured."),
        )
    )
    if open_questions:
        sections.append(("Open Questions", managed_section(open_questions)))
    sections.extend(
        [
            ("People", managed_section(people or "- Pending speaker review")),
            ("Workstreams", managed_section(workstreams)),
            ("Transcript", managed_section(transcript or "Transcript pending.")),
        ]
    )
    section_markdown = "\n\n".join(f"## {heading}\n\n{content}" for heading, content in sections)
    # Front-matter exposed via Obsidian Publish / git sync. Keep
    # internal IDs out, name the export-time fields honestly. Source
    # filename can carry sensitive content (client names, dates) — use
    # the slug-derived stem instead so the vault file is self-contained.
    speaker_status_label = {
        "pending": "in-progress",
        "partial": "in-progress",
        "complete": "verified",
    }.get(data.get("speaker_status") or "", "in-progress")
    # JSON-dump every string scalar in the frontmatter so YAML's special
    # characters (`:`, `#`, leading `-`, etc.) can never break out of the
    # value or inject sibling keys. Numeric and enum-controlled fields are
    # still bare scalars — those don't accept user input.
    return f"""---
type: meeting
slug: {json.dumps(data['slug'])}
status: {json.dumps(status)}
title: {json.dumps(data['title'])}
date: {json.dumps(data['created_at'][:10])}
duration_minutes: {round(float(data['duration_seconds']) / 60, 1)}
speaker_identification: {json.dumps(speaker_status_label)}
exported_at: {json.dumps(datetime.now(UTC).isoformat())}
---

# {data['title']}

{section_markdown}
"""


def write_person_pages(config: AppConfig, data: dict) -> list[Path]:
    outputs: list[Path] = []
    people_dir = config.paths.vault_dir / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    for speaker in data["speakers"]:
        output = people_dir / f"{_vault_note_stem(speaker)}.md"
        existing = (
            _strip_generated_artifacts(output.read_text())
            if output.exists()
            else _new_person_page(speaker)
        )
        content = _replace_or_append_section(
            existing,
            "meetingmind-meetings",
            _person_meetings_section(config, speaker, data),
        )
        content = _replace_or_append_section(
            content,
            "meetingmind-profile",
            _person_profile_section(config, speaker, data),
            heading="Profile",
        )
        output.write_text(content)
        outputs.append(output)
    return outputs


def write_workstream_pages(config: AppConfig, data: dict) -> list[Path]:
    outputs: list[Path] = []
    workstream_dir = config.paths.vault_dir / "Workstreams"
    workstream_dir.mkdir(parents=True, exist_ok=True)
    for workstream in data["workstreams"]:
        output = workstream_dir / f"{_vault_note_stem(workstream)}.md"
        existing = (
            _strip_generated_artifacts(output.read_text())
            if output.exists()
            else _new_workstream_page(workstream)
        )
        content = _replace_or_append_section(
            existing,
            "meetingmind-meetings",
            _workstream_meetings_section(config, workstream, data),
        )
        content = _replace_or_append_section(
            content,
            "meetingmind-related-people",
            _workstream_related_people_section(config, workstream, data),
            heading="Related People",
        )
        output.write_text(content)
        outputs.append(output)
    return outputs


def write_action_rollup(config: AppConfig) -> Path:
    output = config.paths.vault_dir / "Actions" / "Open Actions.md"
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT ai.text, m.slug, m.title, m.created_at
            FROM action_items ai
            JOIN meetings m ON m.id = ai.meeting_id
            WHERE ai.status = 'open'
            ORDER BY ai.id
            """
        ).fetchall()
    actions = "\n".join(
        f"- [ ] {row['text']} From [[Meetings/{row['created_at'][:4]}/{row['slug']}|"
        f"{_vault_alias(row['title'])}]]."
        for row in rows
    )
    content = f"""---
type: action_rollup
generated_by: meetingmind
---

# Open Actions

{managed_section(actions or "- No open actions.")}
"""
    output.write_text(content)
    return output


def load_meeting_export_data(config: AppConfig, meeting_id: int) -> dict:
    with connect(config.paths.database_path) as conn:
        meeting = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if not meeting:
            raise ValueError(f"Meeting {meeting_id} not found")
        segments = conn.execute(
            """
            SELECT id, start_ms, text, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        # Pull owner_person_id + the matching display_name so callers
        # (annotate_overview_for_owner) can filter "yours" by stable id
        # instead of brittle prose-prefix scanning. LEFT JOIN keeps
        # unattributed actions in the list with owner_display_name=NULL.
        # Pull every row (canonicals + members + standalones) so we can
        # build the "N related mentions" disclosure on canonicals without
        # a second query. The default action list excludes members; the
        # reader attaches them under their canonical via cluster_id.
        actions = conn.execute(
            """
            SELECT ai.id, ai.text, ai.due_date, ai.due_date_source, ai.priority,
                   ai.source_segment_ids, ai.owner_person_id,
                   ai.cluster_id, ai.cluster_role,
                   p.display_name AS owner_display_name
            FROM action_items ai
            LEFT JOIN people p ON p.id = ai.owner_person_id
            WHERE ai.meeting_id = ?
            ORDER BY ai.id
            """,
            (meeting_id,),
        ).fetchall()
        cluster_meta_rows = conn.execute(
            """
            SELECT title, payload_json FROM review_items
            WHERE meeting_id = ? AND kind = 'action_cluster_meta'
            """,
            (meeting_id,),
        ).fetchall()
        assignments = conn.execute(
            """
            SELECT diarization_speaker_id, approved_label
            FROM speaker_assignments
            WHERE meeting_id = ? AND confirmed_by_user = 1
            """,
            (meeting_id,),
        ).fetchall()
        workstreams = conn.execute(
            """
            SELECT title
            FROM review_items
            WHERE meeting_id = ? AND kind = 'workstream'
            ORDER BY confidence DESC, id
            """,
            (meeting_id,),
        ).fetchall()
        decisions = conn.execute(
            """
            SELECT title, payload_json, source_segment_ids
            FROM review_items
            WHERE meeting_id = ? AND kind = 'decision'
            ORDER BY confidence DESC, id
            """,
            (meeting_id,),
        ).fetchall()
        open_questions = conn.execute(
            """
            SELECT title, payload_json, source_segment_ids
            FROM review_items
            WHERE meeting_id = ? AND kind = 'open_question'
            ORDER BY id
            """,
            (meeting_id,),
        ).fetchall()
        executive_recap_row = conn.execute(
            """
            SELECT payload_json
            FROM review_items
            WHERE meeting_id = ? AND kind = 'executive_recap'
            ORDER BY id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
        summary = conn.execute(
            """
            SELECT payload_json
            FROM review_items
            WHERE meeting_id = ? AND kind = 'summary'
            ORDER BY id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()

    speaker_map = {row["diarization_speaker_id"]: row["approved_label"] for row in assignments}
    export_segments = []
    speakers = []
    segment_start_by_id: dict[int, int] = {}
    for row in segments:
        speaker = speaker_map.get(row["diarization_speaker_id"], row["diarization_speaker_id"])
        if speaker not in speakers:
            speakers.append(speaker)
        segment_start_by_id[int(row["id"])] = int(row["start_ms"])
        export_segments.append(
            {
                "id": row["id"],
                "speaker": speaker,
                "timestamp": _format_timestamp(row["start_ms"]),
                "text": safe_transcript_text(int(row["id"]), str(row["text"])),
            }
        )

    # Attach the action's `start_ms` from the earliest source segment so vault
    # notes and exports can render "[mm:ss] task" rather than opaque strings.
    by_id: dict[int, dict] = {}
    for row in actions:
        action_dict = dict(row)
        try:
            segment_ids = json.loads(action_dict.get("source_segment_ids") or "[]")
        except json.JSONDecodeError:
            segment_ids = []
        starts: list[int] = []
        for sid in segment_ids:
            try:
                if isinstance(sid, bool):
                    continue
                if isinstance(sid, str) and not sid.strip().lstrip("-").isdigit():
                    continue
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int in segment_start_by_id:
                starts.append(segment_start_by_id[sid_int])
        if starts:
            action_dict["start_ms"] = min(starts)
        by_id[int(action_dict["id"])] = action_dict

    # Cluster members get folded under their canonical and dropped from
    # the top-level list. Canonicals pick up `cluster_members` (member
    # dicts) and `due_date_history` from the sidecar review_items row.
    history_by_canonical: dict[int, list] = {}
    for meta in cluster_meta_rows:
        title = (meta["title"] or "")
        if not title.startswith("action:"):
            continue
        try:
            canon_id = int(title.split(":", 1)[1])
            payload = json.loads(meta["payload_json"] or "{}")
        except (ValueError, json.JSONDecodeError):
            continue
        hist = payload.get("due_date_history")
        if isinstance(hist, list):
            history_by_canonical[canon_id] = hist

    enriched_actions: list[dict] = []
    members_by_canonical: dict[int, list[dict]] = {}
    for action_dict in by_id.values():
        role = action_dict.get("cluster_role")
        cid = action_dict.get("cluster_id")
        if role == "member" and cid is not None:
            members_by_canonical.setdefault(int(cid), []).append(action_dict)
    for action_dict in by_id.values():
        if action_dict.get("cluster_role") == "member":
            continue
        aid = int(action_dict["id"])
        if action_dict.get("cluster_role") == "canonical":
            action_dict["cluster_members"] = members_by_canonical.get(aid, [])
            if aid in history_by_canonical:
                action_dict["due_date_history"] = history_by_canonical[aid]
        enriched_actions.append(action_dict)

    return {
        **dict(meeting),
        "segments": export_segments,
        "actions": enriched_actions,
        "decisions": [_claim_from_review_item(row, "decision") for row in decisions],
        "open_questions": [_claim_from_review_item(row, "text") for row in open_questions],
        "speakers": speakers,
        "workstreams": sorted({row["title"] for row in workstreams}),
        "speaker_status": _speaker_status(export_segments, speaker_map),
        "summary": _summary_text(summary["payload_json"]) if summary else "",
        "tldr": _summary_tldr(summary["payload_json"]) if summary else "",
        "briefing": _summary_briefing(summary["payload_json"]) if summary else [],
        "participant_contributions": (
            _summary_participant_contributions(summary["payload_json"]) if summary else []
        ),
        "chapter_markers": (
            _summary_chapter_markers(summary["payload_json"]) if summary else []
        ),
        "themes": _summary_themes(summary["payload_json"]) if summary else [],
        "stat_callouts": _summary_stat_callouts(summary["payload_json"]) if summary else [],
        "tension_points": _summary_tension_points(summary["payload_json"]) if summary else [],
        "model_key_takeaways": (
            _summary_key_takeaways(summary["payload_json"]) if summary else []
        ),
        "executive_recap": (
            _parse_executive_recap(executive_recap_row["payload_json"])
            if executive_recap_row
            else None
        ),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _record_export(config: AppConfig, meeting_id: int, output: Path, content: str) -> None:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO obsidian_exports (meeting_id, output_path, content_hash)
            VALUES (?, ?, ?)
            """,
            (meeting_id, str(output), digest),
        )


def _format_timestamp(ms: int) -> str:
    total_seconds = int(ms / 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"


def _render_action(action: dict) -> str:
    detail_parts = []
    if action.get("due_date"):
        detail_parts.append(f"due {action['due_date']}")
    if action.get("priority") and action["priority"] != "normal":
        detail_parts.append(f"priority {action['priority']}")
    detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
    timestamp = ""
    start_ms = action.get("start_ms")
    if isinstance(start_ms, int) and start_ms >= 0:
        timestamp = f"[{_format_timestamp(start_ms)}] "
    return f"- [ ] {timestamp}{action['text']}{detail}".rstrip()


def _render_claim(item: dict) -> str:
    """Render a decision or open_question as a flat bullet line.

    Stays plain so the same output is safe for the React dashboard's flat
    `decisions` / `open_questions` arrays (literal underscores would render
    as text, not italics). Rationale / status surface through the
    structured `*_details` fields in the overview, and through Markdown-
    specific renderers below for Obsidian/HTML/PDF exports.
    """
    return f"- {item['text']}".rstrip()


def _render_claim_md(item: dict) -> str:
    """Markdown-rich variant for Obsidian / HTML / PDF exports.

    Adds an indented rationale line below decisions and a status suffix on
    deferred / partially-answered open questions. Used only by the export
    renderers, never by the flat overview strings.
    """
    base = f"- {item['text']}".rstrip()
    rationale = item.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        base = f"{base}\n    *why:* {rationale.strip()}"
    status = item.get("status")
    if isinstance(status, str) and status in {"partially_answered", "deferred"}:
        label = "partially answered" if status == "partially_answered" else "deferred"
        base = f"{base} *(status: {label})*"
    return base


def _render_actions(actions: list[dict]) -> str:
    return "\n".join(_render_action(action) for action in actions)


def _render_claims(items: list[dict]) -> str:
    return "\n".join(_render_claim(item) for item in items)


def _render_claims_md(items: list[dict]) -> str:
    return "\n".join(_render_claim_md(item) for item in items)


def _lines_without_markers(markdown: str) -> list[str]:
    # v0.2.10: filter out the empty-state placeholders that the
    # markdown-generating functions emit ("- No key takeaways extracted
    # yet.", "- No explicit decisions captured.", etc.). The dashboard
    # treats those placeholders as real content and renders them as a
    # numbered HIGHLIGHTS row — that hides the actual "pending vs
    # nothing" distinction. Returning an empty list here lets the
    # frontend's `length > 0` check kick in and show the right empty
    # state based on the meeting's processing status.
    placeholder_pattern = re.compile(
        r"^-?\s*No\s+(?:key takeaways extracted yet"
        r"|explicit decisions captured"
        r"|action items captured"
        r"|open questions captured)\.?\s*$"
    )
    lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line == "- None" or placeholder_pattern.match(line):
            continue
        line = re.sub(r"^- \[ \]\s*", "", line)
        line = re.sub(r"^-\s*", "", line)
        line = re.sub(r"\s*\(evidence:.*?\)\s*$", "", line)
        if line:
            lines.append(line)
    return lines


def _strip_markdown_task(value: str) -> str:
    return re.sub(r"^- \[ \]\s*", "", value.strip()).strip()


def _key_takeaways(data: dict, limit: int = 5) -> str:
    """Render the "Key Takeaways" section.

    Preference order:
      1. Model-supplied `model_key_takeaways` from the summary payload —
         these are the 3-5 bullets the LLM curated explicitly for "what
         someone who wasn't here needs to know" (the new key_takeaways
         field on MeetingAtoms).
      2. Decisions (fallback for meetings extracted before the field
         existed — preserves backwards-compat for any old payloads).
      3. First-sentence-from-summary (legacy last-resort).
    """
    model_takeaways = data.get("model_key_takeaways") or []
    if isinstance(model_takeaways, list) and model_takeaways:
        cleaned = [f"- {str(t).strip()}" for t in model_takeaways if str(t).strip()]
        if cleaned:
            return "\n".join(cleaned[:limit])
    takeaways = []
    for item in data["decisions"]:
        takeaways.append(_render_claim(item))
        if len(takeaways) >= limit:
            break
    if not takeaways and data["summary"]:
        sentences = re.split(r"(?<=[.!?])\s+", data["summary"])
        takeaways = [f"- {sentence.strip()}" for sentence in sentences if sentence.strip()][:limit]
    return "\n".join(takeaways) or "- No key takeaways extracted yet."


def _claim_from_review_item(row, payload_key: str) -> dict:
    """Read a decision or open_question review_item back into a dict.

    Handles both legacy flat and new structured payloads:
    - Decisions: legacy `{decision: str}`, new `{decision, rationale, ...}`
    - Open questions: legacy `{text: str}`, new
      `{question, raised_by, addressed_to, status, ...}`

    `payload_key` is the legacy-shape field name ("decision" or "text"); the
    newer "question" field name is also tried for open_question rows.
    Structured optional fields (rationale, status, raised_by, addressed_to)
    surface in the returned dict only when present, so callers can detect
    and render them conditionally.
    """
    text = row["title"]
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        for key in (payload_key, "question", "decision", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    claim: dict = {
        "text": text,
        "source_segment_ids": row["source_segment_ids"],
    }
    if isinstance(payload, dict):
        rationale = payload.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            claim["rationale"] = rationale.strip()
        status = payload.get("status")
        if isinstance(status, str) and status in {
            "unanswered",
            "partially_answered",
            "deferred",
        }:
            claim["status"] = status
        raised_by = payload.get("raised_by")
        if isinstance(raised_by, str) and raised_by.strip():
            claim["raised_by"] = raised_by.strip()
        addressed_to = payload.get("addressed_to")
        if isinstance(addressed_to, str) and addressed_to.strip():
            claim["addressed_to"] = addressed_to.strip()
    return claim


def _source_segment_ids(value: str) -> list[int]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [int(item) for item in parsed if isinstance(item, int)]


def _new_person_page(display_name: str) -> str:
    return f"""---
type: person
display_name: {json.dumps(display_name)}
generated_by: meetingmind
---

# {display_name}
"""


def _new_workstream_page(display_name: str) -> str:
    return f"""---
type: workstream
display_name: {json.dumps(display_name)}
status: active
generated_by: meetingmind
---

# {display_name}
"""


def _replace_or_append_section(
    existing: str,
    section_name: str,
    content: str,
    *,
    heading: str = "Meetings",
) -> str:
    rendered = managed_section(content)
    block = f"## {heading}\n\n{rendered}"
    if _has_managed_section(existing, section_name):
        return _replace_managed_section(existing, section_name, block)
    return existing.rstrip() + "\n\n" + block + "\n"


def _write_generated_note(output: Path, generated: str) -> str:
    if not output.exists():
        output.write_text(generated)
        return generated
    merged = _merge_generated_note(_strip_generated_artifacts(output.read_text()), generated)
    output.write_text(merged)
    return merged


def _merge_generated_note(existing: str, generated: str) -> str:
    """Merge generated H2 sections while preserving manual notes and unknown sections."""
    merged = existing
    blocks = _generated_managed_blocks(generated)
    for index, block in enumerate(blocks):
        if _has_managed_section(merged, block["name"]):
            merged = _replace_managed_section(merged, block["name"], block["section"])
        else:
            later_sections = [candidate["name"] for candidate in blocks[index + 1 :]]
            merged = _insert_generated_block(merged, block, later_sections)
    generated_names = {block["name"] for block in blocks}
    for stale_section in ("decisions", "action-items", "open-questions"):
        if stale_section not in generated_names and _has_managed_section(merged, stale_section):
            merged = _remove_managed_block(merged, stale_section)
    return merged


def _generated_managed_blocks(generated: str) -> list[dict[str, str]]:
    """Extract generated sections that MeetingMind owns by stable H2 heading."""
    blocks: list[dict[str, str]] = []
    for heading, start, end in _h2_blocks(generated):
        name = _MANAGED_HEADING_NAMES.get(heading)
        if not name:
            continue
        block = generated[start:end]
        blocks.append(
            {
                "name": name,
                "block": block.strip(),
                "section": block.strip(),
            }
        )
    return blocks


def _insert_generated_block(existing: str, block: dict[str, str], later_sections: list[str]) -> str:
    for anchor in later_sections:
        anchor_index = _managed_section_heading_start(existing, anchor)
        if anchor_index >= 0:
            return (
                existing[:anchor_index].rstrip()
                + "\n\n"
                + block["block"]
                + "\n\n"
                + existing[anchor_index:].lstrip()
            )
    return existing.rstrip() + "\n\n" + block["block"] + "\n"


def _managed_sections(text: str) -> dict[str, str]:
    return {
        _MANAGED_HEADING_NAMES[heading]: text[start:end]
        for heading, start, end in _h2_blocks(text)
        if heading in _MANAGED_HEADING_NAMES
    }


def _h2_blocks(text: str) -> list[tuple[str, int, int]]:
    """Return top-level note blocks keyed by second-level Markdown headings."""
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n?", text))
    blocks: list[tuple[str, int, int]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1).strip(), match.start(), end))
    return blocks


def _has_managed_section(existing: str, section_name: str) -> bool:
    return _managed_section_heading_start(existing, section_name) >= 0


def _managed_section_heading_start(existing: str, section_name: str) -> int:
    heading = _MANAGED_SECTION_HEADINGS.get(section_name)
    if not heading:
        return -1
    for candidate_heading, start, _end in _h2_blocks(existing):
        if candidate_heading == heading:
            return start
    return -1


def _replace_managed_section(existing: str, section_name: str, rendered: str) -> str:
    heading = _MANAGED_SECTION_HEADINGS.get(section_name)
    if heading:
        for candidate_heading, start, end in _h2_blocks(existing):
            if candidate_heading == heading:
                prefix = existing[:start].rstrip()
                suffix = existing[end:].lstrip()
                if prefix and suffix:
                    return f"{prefix}\n\n{rendered.strip()}\n\n{suffix}"
                if prefix:
                    return f"{prefix}\n\n{rendered.strip()}\n"
                if suffix:
                    return f"{rendered.strip()}\n\n{suffix}"
                return rendered.strip() + "\n"
    return existing.rstrip() + "\n\n" + rendered + "\n"


def _remove_managed_block(existing: str, section_name: str) -> str:
    heading = _MANAGED_SECTION_HEADINGS.get(section_name)
    if not heading:
        return existing
    for candidate_heading, start, end in _h2_blocks(existing):
        if candidate_heading == heading:
            return (existing[:start].rstrip() + "\n\n" + existing[end:].lstrip()).rstrip() + "\n"
    return existing


def _remove_staged_note(config: AppConfig, slug: str) -> None:
    staged = config.paths.vault_dir / "Staging" / f"{slug}.md"
    if staged.exists():
        staged.unlink()


def _remove_generated_workstream_page(config: AppConfig, title: str) -> None:
    path = config.paths.vault_dir / "Workstreams" / f"{_vault_note_stem(title)}.md"
    if not path.exists():
        return
    text = _strip_generated_artifacts(path.read_text())
    if "generated_by: meetingmind" not in text:
        return
    if not _has_manual_content(text):
        path.unlink()
        return
    text = _remove_managed_block(text, "meetingmind-meetings")
    text = _remove_managed_block(text, "meetingmind-related-people")
    path.write_text(text)


def cleanup_generated_orphans(config: AppConfig) -> list[Path]:
    removed: list[Path] = []
    existing_notes = {
        path.relative_to(config.paths.vault_dir).with_suffix("").as_posix()
        for path in config.paths.vault_dir.rglob("*.md")
    }
    for directory_name in ("People", "Workstreams"):
        directory = config.paths.vault_dir / directory_name
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            original = path.read_text()
            text = _strip_generated_artifacts(original)
            if "generated_by: meetingmind" not in text:
                continue
            targets = re.findall(r"\[\[([^]|#]+)", text)
            missing_meeting_links = {
                target.removesuffix(".md")
                for target in targets
                if target.startswith("Meetings/")
                and target.removesuffix(".md") not in existing_notes
            }
            if missing_meeting_links:
                pruned = _prune_broken_meeting_links(text, missing_meeting_links)
                if _generated_page_has_no_meeting_links(pruned) and not _has_manual_content(pruned):
                    path.unlink()
                    removed.append(path)
                else:
                    path.write_text(pruned)
            elif _generated_page_has_no_meeting_links(text) and not _has_manual_content(text):
                path.unlink()
                removed.append(path)
            elif text != original:
                path.write_text(text)
    return removed


def _person_meetings_section(config: AppConfig, display_name: str, data: dict) -> str:
    lines = _person_meeting_lines(config, display_name)
    if not lines:
        lines = [f"- {_meeting_link(data)}"]
    return "\n".join(dict.fromkeys(lines))


def _person_profile_section(config: AppConfig, display_name: str, data: dict) -> str:
    stats = _person_stats(config, display_name)
    if not stats:
        segment_count = sum(1 for segment in data["segments"] if segment["speaker"] == display_name)
        stats = {
            "meeting_count": 1,
            "turn_count": segment_count,
            "last_link": _meeting_link(data),
        }
    return "\n".join(
        [
            f"- Last seen: {stats['last_link']}",
            f"- Meetings captured: {stats['meeting_count']}",
            f"- Transcript turns captured: {stats['turn_count']}",
        ]
    )


def _workstream_meetings_section(config: AppConfig, title: str, data: dict) -> str:
    lines = _workstream_meeting_lines(config, title)
    if not lines:
        lines = [f"- {_meeting_link(data)}"]
    return "\n".join(dict.fromkeys(lines))


def _workstream_related_people_section(config: AppConfig, title: str, data: dict) -> str:
    people = _workstream_people(config, title)
    if not people:
        people = data["speakers"]
    return (
        "\n".join(
            f"- [[People/{_vault_note_stem(speaker)}|{_vault_alias(speaker)}]]"
            for speaker in people
        )
        or "- Pending speaker review"
    )


def _person_meeting_lines(config: AppConfig, display_name: str) -> list[str]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT m.title, m.slug, m.created_at
            FROM speaker_assignments sa
            JOIN meetings m ON m.id = sa.meeting_id
            WHERE sa.confirmed_by_user = 1
              AND sa.approved_label = ?
              AND m.status = 'promoted'
            ORDER BY m.created_at DESC, m.id DESC
            """,
            (display_name,),
        ).fetchall()
    return [f"- {_meeting_link(row)}" for row in rows]


def _person_stats(config: AppConfig, display_name: str) -> dict | None:
    with connect(config.paths.database_path) as conn:
        meeting_rows = conn.execute(
            """
            SELECT DISTINCT m.id, m.title, m.slug, m.created_at
            FROM speaker_assignments sa
            JOIN meetings m ON m.id = sa.meeting_id
            WHERE sa.confirmed_by_user = 1
              AND sa.approved_label = ?
              AND m.status = 'promoted'
            ORDER BY m.created_at DESC, m.id DESC
            """,
            (display_name,),
        ).fetchall()
        turn_row = conn.execute(
            """
            SELECT COUNT(*) AS turn_count
            FROM transcript_segments ts
            JOIN speaker_assignments sa
              ON sa.meeting_id = ts.meeting_id
             AND sa.diarization_speaker_id = ts.diarization_speaker_id
            JOIN meetings m ON m.id = ts.meeting_id
            WHERE sa.confirmed_by_user = 1
              AND sa.approved_label = ?
              AND m.status = 'promoted'
            """,
            (display_name,),
        ).fetchone()
    if not meeting_rows:
        return None
    return {
        "meeting_count": len(meeting_rows),
        "turn_count": int(turn_row["turn_count"] or 0) if turn_row else 0,
        "last_link": _meeting_link(meeting_rows[0]),
    }


def _workstream_meeting_lines(config: AppConfig, title: str) -> list[str]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT m.title, m.slug, m.created_at
            FROM review_items ri
            JOIN meetings m ON m.id = ri.meeting_id
            WHERE ri.kind = 'workstream'
              AND ri.title = ?
              AND m.status = 'promoted'
            ORDER BY m.created_at DESC, m.id DESC
            """,
            (title,),
        ).fetchall()
    return [f"- {_meeting_link(row)}" for row in rows]


def _workstream_people(config: AppConfig, title: str) -> list[str]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT sa.approved_label
            FROM review_items ri
            JOIN meetings m ON m.id = ri.meeting_id
            JOIN speaker_assignments sa ON sa.meeting_id = m.id
            WHERE ri.kind = 'workstream'
              AND ri.title = ?
              AND m.status = 'promoted'
              AND sa.confirmed_by_user = 1
            ORDER BY sa.approved_label
            """,
            (title,),
        ).fetchall()
    return [str(row["approved_label"]) for row in rows if row["approved_label"]]


def _meeting_link(row: dict) -> str:
    return f"[[Meetings/{str(row['created_at'])[:4]}/{row['slug']}|{_vault_alias(row['title'])}]]"


def _prune_broken_meeting_links(text: str, missing_meeting_links: set[str]) -> str:
    lines = []
    for line in text.splitlines():
        line_targets = {
            target.strip().removesuffix(".md")
            for target in re.findall(r"\[\[([^]|#]+)", line)
        }
        if line_targets & missing_meeting_links:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _generated_page_has_no_meeting_links(text: str) -> bool:
    return not re.search(r"\[\[Meetings/", text)


def _has_manual_content(text: str) -> bool:
    without_frontmatter = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    without_title = re.sub(r"^# .*$", "", without_frontmatter, count=1, flags=re.MULTILINE)
    without_managed = without_title
    for heading, start, end in reversed(_h2_blocks(without_managed)):
        if heading in _MANAGED_HEADING_NAMES:
            without_managed = without_managed[:start] + without_managed[end:]
    without_headings = re.sub(r"^## .*$", "", without_managed, flags=re.MULTILINE)
    return bool(without_headings.strip())


def _strip_generated_artifacts(text: str) -> str:
    """Remove legacy hidden markers, segment anchors, and evidence links from old exports."""
    cleaned = re.sub(
        r"\n## Human Notes\n\nAdd manual notes here\. MeetingMind will not edit this section\.\n?",
        "\n",
        text,
    )
    cleaned = re.sub(r"<!-- meetingmind:section:start [^>]+ -->\n?", "", cleaned)
    cleaned = re.sub(r"<!-- meetingmind:section:end [^>]+ -->\n?", "", cleaned)
    cleaned = re.sub(r"(?m)^\^seg-\d+\n?", "", cleaned)
    evidence_pattern = (
        r"\s*\(evidence: \[\[#\^seg-\d+\|seg \d+\]\]"
        r"(?:, \[\[#\^seg-\d+\|seg \d+\]\])*\)"
    )
    cleaned = re.sub(evidence_pattern, "", cleaned)
    return cleaned.rstrip() + "\n"


def _vault_note_stem(display_name: str) -> str:
    cleaned = re.sub(r"[/\\:#|\[\]\n\r\t]+", "-", display_name).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:96].strip() or "Untitled").rstrip(".")


def _vault_alias(display_name: str) -> str:
    cleaned = re.sub(r"[/\\:#|\[\]\n\r\t]+", " ", display_name)
    cleaned = cleaned.replace("..", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Untitled"


def _speaker_status(segments: list[dict], speaker_map: dict[str, str]) -> str:
    raw_speakers = {segment["speaker"] for segment in segments}
    if not raw_speakers:
        return "pending"
    if all(speaker in speaker_map.values() for speaker in raw_speakers):
        return "complete"
    return "partial" if speaker_map else "pending"


@lru_cache(maxsize=32)
def _parse_summary_payload(payload_json: str) -> dict:
    """Parse `payload_json` once and memoize.

    Audit (perf MED): `load_meeting_export_data` previously called eight
    `_summary_*` helpers on the same string, each of which re-ran
    `json.loads`. With an lru_cache around the parse step we now do it
    once per export. The cache is keyed by the exact string, so a new
    extraction (different payload) misses naturally and the dict for
    the previous extraction ages out.
    """
    try:
        return json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _summary_text(payload_json: str) -> str:
    return str(_parse_summary_payload(payload_json).get("summary", "")).strip()


def _summary_tldr(payload_json: str) -> str:
    """Extract the wire-thin headline persisted alongside the long summary.

    Older meetings (pre-tldr) won't have the field; callers should fall
    back to a smart truncation of `summary`.
    """
    return str(_parse_summary_payload(payload_json).get("tldr", "")).strip()


def _summary_briefing(payload_json: str) -> list[str]:
    """Extract the 3-sentence Mind Map briefing.

    Returns the list as-is from the payload, filtered to non-empty
    trimmed strings. Older meetings (pre-briefing) return []. Renderers
    should fall back to tldr + summary when this is empty.
    """
    raw = _parse_summary_payload(payload_json).get("briefing", [])
    if not isinstance(raw, list):
        return []
    return [str(s).strip() for s in raw if isinstance(s, str) and s.strip()]


def _coerce_segment_ids(raw) -> list[int]:
    """Best-effort int coercion for source_segment_ids in legacy payloads.

    The model has historically returned ints, but JSON-encoded payloads
    sometimes come back as strings (e.g. "42") or floats (e.g. 42.0).
    Plain `int(sid)` would crash on non-numeric strings; we wrap in
    try/except so a single bad id doesn't drop the whole list.
    """
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for sid in raw:
        try:
            out.append(int(sid))
        except (TypeError, ValueError):
            continue
    return out


def _summary_themes(payload_json: str) -> list[str]:
    raw = _parse_summary_payload(payload_json).get("themes") or []
    return [str(t).strip() for t in raw if isinstance(t, str) and t.strip()][:3]


def _summary_key_takeaways(payload_json: str) -> list[str]:
    """Read the model-supplied 3-5 key takeaways from the summary
    payload. Returns [] for meetings extracted before the field
    existed; the overview renderer falls back to deriving from
    decisions when this is empty.
    """
    raw = _parse_summary_payload(payload_json).get("key_takeaways") or []
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if isinstance(t, str) and t.strip()][:5]


def _parse_executive_recap(payload_json: str) -> dict | None:
    """Coerce a stored ExecutiveRecap payload into a plain dict for the
    overview consumer. Returns None on parse failure so the frontend
    falls back to the existing tldr/summary surface.
    """
    try:
        payload = json.loads(payload_json or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    # Drop empty sections so the renderer can skip them without a
    # secondary "both header and body null?" check. The risk section is
    # the common skipped section per the prompt's fallback rules.
    def _clean_section(section: object) -> dict | None:
        if not isinstance(section, dict):
            return None
        header = (section.get("header") or "") if isinstance(section.get("header"), str) else ""
        body = (section.get("body") or "") if isinstance(section.get("body"), str) else ""
        if not header.strip() and not body.strip():
            return None
        return {"header": header.strip() or None, "body": body.strip() or None}

    strategy_raw = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else None
    strategy: dict | None = None
    if strategy_raw is not None:
        header_raw = strategy_raw.get("header")
        header = header_raw if isinstance(header_raw, str) else None
        body_raw = strategy_raw.get("body")
        body = body_raw if isinstance(body_raw, str) else None
        bullets_field = strategy_raw.get("bullets")
        bullets_raw = bullets_field if isinstance(bullets_field, list) else []
        bullets = [
            {
                "owner": str(b.get("owner") or "").strip(),
                "commitment": str(b.get("commitment") or "").strip(),
                "purpose": (
                    str(b["purpose"]).strip()
                    if isinstance(b.get("purpose"), str) and b["purpose"].strip()
                    else None
                ),
            }
            for b in bullets_raw
            if isinstance(b, dict) and b.get("commitment")
        ]
        trailer_raw = strategy_raw.get("trailer")
        trailer = trailer_raw if isinstance(trailer_raw, str) else None
        if header or body or bullets:
            strategy = {
                "header": (header.strip() if isinstance(header, str) and header.strip() else None),
                "body": (body.strip() if isinstance(body, str) and body.strip() else None),
                "bullets": bullets,
                "trailer": (
                    trailer.strip()
                    if isinstance(trailer, str) and trailer.strip()
                    else None
                ),
            }

    cleaned = {
        "reframe": _clean_section(payload.get("reframe")),
        "strategy": strategy,
        "risk": _clean_section(payload.get("risk")),
    }
    # If every section cleaned to None the recap carries no content;
    # return None so callers (and logs) see "no recap" rather than a
    # truthy-empty dict that surprises later readers.
    if not any(cleaned.values()):
        return None
    return cleaned


def _summary_stat_callouts(payload_json: str) -> list[dict]:
    raw = _parse_summary_payload(payload_json).get("stat_callouts") or []
    cleaned: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip()
        label = str(entry.get("label") or "").strip()
        if not value or not label:
            continue
        cleaned.append(
            {
                "value": value,
                "label": label,
                "source_segment_ids": _coerce_segment_ids(
                    entry.get("source_segment_ids")
                ),
            }
        )
    return cleaned[:3]


def _summary_tension_points(payload_json: str) -> list[dict]:
    raw = _parse_summary_payload(payload_json).get("tension_points") or []
    cleaned: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        positive = str(entry.get("positive_side") or "").strip()
        negative = str(entry.get("negative_side") or "").strip()
        if not title or not (positive or negative):
            continue
        cleaned.append(
            {
                "title": title,
                "positive_side": positive,
                "negative_side": negative,
                "source_segment_ids": _coerce_segment_ids(
                    entry.get("source_segment_ids")
                ),
            }
        )
    return cleaned[:2]


def _summary_chapter_markers(payload_json: str) -> list[dict]:
    """Extract model-generated chapter labels from the persisted summary
    payload. Empty for meetings extracted before this field existed —
    the frontend falls back to deriving chapters from workstreams.
    """
    raw = _parse_summary_payload(payload_json).get("chapter_markers") or []
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        anchor = entry.get("start_segment_id")
        if not label or not isinstance(anchor, (int, float)):
            continue
        item: dict = {"label": label, "start_segment_id": int(anchor)}
        chapter_summary = entry.get("summary")
        if isinstance(chapter_summary, str) and chapter_summary.strip():
            item["summary"] = chapter_summary.strip()
        cleaned.append(item)
    return cleaned


def _summary_participant_contributions(payload_json: str) -> list[dict]:
    """Extract per-attendee contributions persisted with the summary.

    Used by the Minutes view. Returns a list of {speaker, contribution,
    source_segment_ids} dicts. Empty when the meeting was extracted
    before the field existed (the frontend renders a fallback then).
    """
    raw = _parse_summary_payload(payload_json).get("participant_contributions") or []
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        speaker = str(entry.get("speaker") or "").strip()
        contribution = str(entry.get("contribution") or "").strip()
        if not speaker or not contribution:
            continue
        # Drop placeholder-speaker contributions. The "Speaker N" scrub
        # in extraction rewrites prose like "Speaker 2 thanked Alex"
        # → "a team member thanked Alex", which is fine inline but
        # produces a confusing "a team member" participant card in the
        # Minutes view. The placeholder + its abbreviations ("a team
        # member", "team member", "unknown speaker") should never appear
        # as a card label — their contribution is already folded into
        # other speakers' narratives via the scrub. Normalize whitespace
        # before the set check: strip every Unicode "format" / "control"
        # codepoint (categories Cf and Cc — includes ZWSP, ZWNJ, ZWJ, BOM,
        # word joiner, soft hyphen, variation selectors, bidi controls,
        # etc.), then collapse runs of ASCII/Unicode whitespace to a
        # single space so variants like "a  team  member" or
        # "​a team member" still match. Category-based instead of an
        # enumerated codepoint table so future invisible-char additions
        # to Unicode don't quietly open a bypass.
        invisible_stripped = "".join(
            ch for ch in speaker if unicodedata.category(ch) not in {"Cf", "Cc"}
        )
        normalized = re.sub(r"\s+", " ", invisible_stripped.casefold().lstrip("@").strip())
        if normalized in {"a team member", "team member", "unknown speaker"}:
            continue
        cleaned.append(
            {
                "speaker": speaker,
                "contribution": contribution,
                "source_segment_ids": [
                    int(sid)
                    for sid in (entry.get("source_segment_ids") or [])
                    if isinstance(sid, (int, float))
                ],
            }
        )
    return cleaned
