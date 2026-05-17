from __future__ import annotations

from pathlib import Path

import pytest
from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.obsidian_writer import (
    _merge_generated_note,
    _prune_broken_meeting_links,
    build_meeting_overview,
    delete_generated_workstream,
    delete_meeting,
    managed_section,
    promote_meeting,
    write_staged_meeting,
)
from app.services.pdf_export import write_meeting_pdf
from app.services.review import approve_speaker_label
from app.services.vault_lint import lint_vault


def test_managed_section_markers() -> None:
    content = managed_section("A useful summary.")
    assert content == "A useful summary."
    assert "meetingmind:section" not in content


def test_promotion_requires_speaker_approval(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, 0, 1000, "We need a launch plan.", "Speaker 1"),
        )

    with pytest.raises(ValueError, match="Speaker approval required"):
        promote_meeting(cfg, meeting_id)

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    output = promote_meeting(cfg, meeting_id)

    assert output.exists()
    assert (cfg.paths.vault_dir / "People" / "Owner.md").exists()
    assert "[[People/Owner|Owner]]" in output.read_text()


def test_promotion_preserves_human_notes_and_sanitizes_names(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Planning | Unsafe]\nTitle",
                "planning",
                "source.m4a",
                "processed/source.m4a",
                60,
                "transcribed",
            ),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, 0, 1000, "We need a launch plan.", "Speaker 1"),
        )
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, "workstream", "../Unsafe/Workstream", "{}", 0.9),
        )
        conn.execute(
            """
            INSERT INTO action_items (meeting_id, text, priority, source_segment_ids)
            VALUES (?, 'Draft the launch checklist', 'medium', '[]')
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "../Owner/Lead")
    output = promote_meeting(cfg, meeting_id)
    original = output.read_text()
    output.write_text(original + "\n\n## Manual Follow-up\n\nKeep this section.\n")
    promote_meeting(cfg, meeting_id)

    updated = output.read_text()
    assert "## Manual Follow-up\n\nKeep this section." in updated
    assert "|../Owner/Lead" not in updated
    assert "../Unsafe/Workstream" not in updated
    assert (cfg.paths.vault_dir / "People" / "Owner-Lead.md").exists()
    assert (cfg.paths.vault_dir / "Workstreams" / "Unsafe-Workstream.md").exists()
    person_page = (cfg.paths.vault_dir / "People" / "Owner-Lead.md").read_text()
    workstream_page = (cfg.paths.vault_dir / "Workstreams" / "Unsafe-Workstream.md").read_text()
    action_rollup = (cfg.paths.vault_dir / "Actions" / "Open Actions.md").read_text()
    assert "Add manual notes here" not in updated
    assert "Add manual notes here" not in person_page
    assert "Add manual notes here" not in workstream_page
    assert "|Planning Unsafe Title]]" in person_page
    assert "|Planning Unsafe Title]]" in workstream_page
    assert "|Planning Unsafe Title]]" in action_rollup
    assert "Planning | Unsafe" not in person_page
    assert "Planning | Unsafe" not in workstream_page
    assert "Planning | Unsafe" not in action_rollup


def test_meeting_note_renders_source_linked_claim_sections(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, ?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO action_items
              (meeting_id, text, priority, source_segment_ids)
            VALUES (?, 'Draft launch plan', 'high', '[10]')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, 'decision', 'Launch plan approved',
                    '{"decision": "Launch plan approved"}', 0.9, '[10]')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, 'open_question', 'Who owns launch?',
                    '{"text": "Who owns launch?"}', 0.7, '[10]')
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    output = promote_meeting(cfg, meeting_id)
    text = output.read_text()

    assert "## Key Takeaways" in text
    assert "## Decisions" in text
    assert "## Open Questions" in text
    assert "**Owner** [00:00]: We need a launch plan." in text
    assert "^seg-10" not in text
    assert "seg 10" not in text
    assert "- Launch plan approved" in text
    # Action carries source_segment_ids → vault note prepends its timestamp.
    assert "- [ ] [00:00] Draft launch plan (priority high)" in text
    assert "- Who owns launch?" in text
    assert text.index("## Key Takeaways") < text.index("## Transcript")
    takeaways = text.split("## Key Takeaways", 1)[1].split("## Decisions", 1)[0]
    assert "Who owns launch?" not in takeaways


def test_empty_claim_sections_are_suppressed(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    output = promote_meeting(cfg, meeting_id)
    text = output.read_text()

    assert "## Decisions" not in text
    assert "## Action Items" in text
    assert "- No action items captured." in text
    assert "## Open Questions" not in text
    assert "- None" not in text


def test_promotion_removes_staging_note_and_generated_orphans(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )

    orphan = cfg.paths.vault_dir / "People" / "Speaker 1.md"
    orphan.write_text(
        "---\ntype: person\ngenerated_by: meetingmind\n---\n\n# Speaker 1\n\n"
        "## Meetings\n\n"
        "- [[Meetings/2026/missing|Missing]]\n"
    )
    write_staged_meeting(cfg, meeting_id)
    assert (cfg.paths.vault_dir / "Staging" / "planning.md").exists()

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    promote_meeting(cfg, meeting_id)

    assert not (cfg.paths.vault_dir / "Staging" / "planning.md").exists()
    assert not orphan.exists()


def test_vault_lint_reports_broken_wiki_links(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    note = cfg.paths.vault_dir / "People" / "Owner.md"
    note.write_text(
        "---\ntype: person\ngenerated_by: meetingmind\n---\n\n"
        "# Owner\n\n"
        "[[Meetings/2026/missing|Missing]]\n"
    )

    result = lint_vault(cfg)

    assert not result.ok
    assert any("broken wiki link -> Meetings/2026/missing" in issue for issue in result.issues)


def test_delete_generated_workstream_removes_review_item_and_vault_page(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json, confidence)
            VALUES (?, 'workstream', 'Garbage Topic', '{}', 0.7)
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    output = promote_meeting(cfg, meeting_id)
    assert "Garbage Topic" in output.read_text()
    assert (cfg.paths.vault_dir / "Workstreams" / "Garbage Topic.md").exists()

    result = delete_generated_workstream(cfg, "Garbage Topic")

    assert result["removed"] == 1
    assert result["promoted"] == 1
    assert "Garbage Topic" not in output.read_text()
    assert not (cfg.paths.vault_dir / "Workstreams" / "Garbage Topic.md").exists()


def test_meeting_overview_matches_managed_note_sections(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 1800, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, ?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, 'summary', 'Planning', '{"summary": "Launch planning summary."}', 0.9, '[]')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, 'decision', 'Launch approved',
                    '{"decision": "Launch approved"}', 0.9, '[10]')
            """,
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO action_items
              (meeting_id, text, priority, source_segment_ids)
            VALUES (?, 'Draft launch plan', 'high', '[10]')
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")

    overview = build_meeting_overview(cfg, meeting_id)

    assert overview["summary"] == "Launch planning summary."
    assert overview["participants"] == ["Owner"]
    assert overview["decisions"] == ["Launch approved"]
    # The action carries source_segment_ids that resolve to segment 10
    # (start_ms=0), so the rendered text now leads with [00:00].
    assert overview["actions"] == ["[00:00] Draft launch plan (priority high)"]
    assert "Launch approved" in overview["obsidian_sections"]["decisions"]


def test_meetingmind_pdf_export_writes_valid_pdf(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Planning", "planning", "source.m4a", "processed/source.m4a", 60, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 0, 1000, 'We need a launch plan.', 'Speaker 1')
            """,
            (meeting_id,),
        )

    approve_speaker_label(cfg, meeting_id, "Speaker 1", "Owner")
    output = write_meeting_pdf(cfg, meeting_id)

    assert output.name == "planning.meetingmind.pdf"
    assert output.read_bytes().startswith(b"%PDF-1.4")


def test_note_merge_inserts_new_sections_in_template_order() -> None:
    existing = """# Planning

## Summary

Old summary.

## Action Items

- [ ] Existing action.

## People

- [[People/Owner|Owner]]

## Workstreams

- [[Workstreams/Launch|Launch]]

## Transcript

Transcript.

## Manual Follow-up

Keep this.
"""
    generated = """# Planning

## Summary

New summary.

## Key Takeaways

- Takeaway.

## Decisions

- Decision.

## Action Items

- [ ] Action.

## Open Questions

- Question?

## People

- [[People/Owner|Owner]]

## Workstreams

- [[Workstreams/Launch|Launch]]

## Transcript

Transcript.
"""
    merged = _merge_generated_note(existing, generated)

    headings = [
        "## Summary",
        "## Key Takeaways",
        "## Decisions",
        "## Action Items",
        "## Open Questions",
        "## People",
        "## Workstreams",
        "## Transcript",
        "## Manual Follow-up",
    ]
    positions = [merged.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "meetingmind:section" not in merged
    assert "Keep this." in merged


def test_prune_broken_meeting_links_does_not_match_slug_prefixes() -> None:
    text = (
        "- [[Meetings/2026/planning|Planning]]\n"
        "- [[Meetings/2026/planning-followup|Planning Follow-up]]\n"
    )

    pruned = _prune_broken_meeting_links(text, {"Meetings/2026/planning"})

    assert "[[Meetings/2026/planning|Planning]]" not in pruned
    assert "[[Meetings/2026/planning-followup|Planning Follow-up]]" in pruned


def test_delete_meeting_cascades_through_segment_overlap_hints(tmp_path: Path) -> None:
    """Regression for v0.2.10 hotfix: deleting a meeting that has rows in
    `segment_overlap_hints`, `segment_comments`, or `meeting_key_terms`
    previously raised a 500 because those tables (added after v0.1.x)
    were missing from `_MEETING_LINKED_TABLES`. With
    `PRAGMA foreign_keys=ON`, deleting `transcript_segments` before the
    dependent rows triggers `FOREIGN KEY constraint failed` and rolls
    back the whole txn. Test seeds one row in each missing table and
    confirms delete now succeeds.
    """
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path,
                                  duration_seconds, status, created_at)
            VALUES ('Demo', 'demo', '/dev/null', '/dev/null', 60, 'complete',
                    '2026-05-14T00:00:00Z')
            """
        )
        meeting_id = int(cursor.lastrowid)
        seg_cursor = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 0, 5000, 'hi', 'Speaker 1')
            """,
            (meeting_id,),
        )
        segment_id = int(seg_cursor.lastrowid)
        # Seed each of the previously-missing tables.
        conn.execute(
            """
            INSERT INTO segment_overlap_hints
              (meeting_id, segment_id, kind, evidence, confidence)
            VALUES (?, ?, 'yield_marker', 'go ahead', 0.8)
            """,
            (meeting_id, segment_id),
        )
        conn.execute(
            """
            INSERT INTO segment_comments
              (meeting_id, segment_id, body, author)
            VALUES (?, ?, 'note', 'you')
            """,
            (meeting_id, segment_id),
        )
        conn.execute(
            "INSERT INTO meeting_key_terms (meeting_id, terms_json) VALUES (?, '[]')",
            (meeting_id,),
        )

    # The buggy version raised sqlite3.IntegrityError here.
    result = delete_meeting(cfg, meeting_id)
    assert result["meeting_id"] == meeting_id

    # Meeting row + all linked rows actually gone.
    with connect(cfg.paths.database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()[0] == 0
        for table in (
            "segment_overlap_hints",
            "segment_comments",
            "meeting_key_terms",
            "transcript_segments",
        ):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?",  # nosec B608
                (meeting_id,),
            ).fetchone()[0]
            assert count == 0, f"{table} still has rows for deleted meeting"


def _test_config(tmp_path: Path) -> AppConfig:
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    return AppConfig(config_path=tmp_path / "config" / "local.toml", paths=paths)
