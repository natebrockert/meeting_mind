from __future__ import annotations

from pathlib import Path

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.search import search_meeting_index, workstream_intelligence


def test_cross_meeting_search_returns_transcript_and_review_hits(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_search_fixture(cfg)

    results = search_meeting_index(cfg, "launch")

    result_types = {result["result_type"] for result in results}
    workstream_hit = next(result for result in results if result["result_type"] == "workstream")
    assert "transcript" in result_types
    assert "workstream" in result_types
    assert any(result["meeting_title"] == "Revenue Sync" for result in results)
    assert workstream_hit["review_item_id"] is not None
    assert workstream_hit["segment_id"] == 10
    assert workstream_hit["start_ms"] == 0
    assert workstream_hit["speaker"] == "Speaker 1"
    assert workstream_hit["source_segment_ids"] == [10]


def test_workstream_intelligence_groups_across_meetings(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_search_fixture(cfg)

    workstreams = workstream_intelligence(cfg)

    launch = next(item for item in workstreams if item["display_name"] == "Launch")
    assert launch["meeting_count"] == 2
    assert launch["mention_count"] == 2
    assert launch["avg_confidence"] == 0.8
    assert {meeting["meeting_title"] for meeting in launch["meetings"]} == {
        "Launch Review",
        "Revenue Sync",
    }


def test_cross_meeting_search_does_not_starve_review_hits(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_search_fixture(cfg)
    with connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 1, ?, ?, 'launch transcript filler', 'Speaker 1')
            """,
            [(100 + index, 2000 + index, 2100 + index) for index in range(10)],
        )

    results = search_meeting_index(cfg, "launch", limit=5)

    assert len(results) == 5
    assert any(result["result_type"] != "transcript" for result in results)


def _insert_search_fixture(cfg: AppConfig) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, 'inbox/source.m4a', 'processed/source.m4a', 60, 'transcribed')
            """,
            [
                (1, "Launch Review", "launch-review"),
                (2, "Revenue Sync", "revenue-sync"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 1, 0, 1000, "Launch owners need a weekly update.", "Speaker 1"),
                (20, 2, 0, 1000, "Revenue forecast is ready.", "Speaker 2"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "workstream", "Launch", "{}", 0.9, "[10]"),
                (2, "workstream", "Launch", "{}", 0.7, "[20]"),
                (2, "decision", "Launch budget approved", "{}", 0.8, "[20]"),
            ],
        )


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
