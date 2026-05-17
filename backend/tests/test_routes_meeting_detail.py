"""Integration test for GET /api/meetings/{id}.

v0.2.10: until now there was no FastAPI TestClient coverage for any route
— the post-merge audit of v0.2.9 flagged this as HIGH because a future
refactor could silently drop fields (overlap_hints, candidates, etc.)
from the response payload without any test failing.

This module sets up the minimum bootstrap to drive the route through
TestClient against a tmp-path SQLite DB:

  1. Monkeypatch `app.api.routes.load_config` so the route reads our
     tmp-path config instead of disk.
  2. Initialize the schema with `initialize_database`.
  3. Insert a meeting + two segments + three overlap hints (two on
     the same segment to exercise the v0.2.9 deterministic-ordering
     contract).
  4. Stub the synthesis / overview / transcript-markdown builders so
     the test doesn't try to talk to a real LLM.
  5. GET the route through TestClient and assert the response shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.api import routes as routes_module
from app.api.routes import router as api_router
from app.config import AppConfig, AsrConfig, DiarizationConfig, PathConfig, ReviewConfig
from app.db.database import connect, initialize_database
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        review=ReviewConfig(),
    )


@pytest.fixture
def client_with_seed(tmp_path: Path, monkeypatch):
    """Yield (TestClient, cfg) with the routes' load_config stubbed and
    a meeting + two segments + three overlap hints already inserted.
    """
    cfg = _test_config(tmp_path)
    paths = cfg.paths
    for p in [paths.data_dir, paths.inbox_dir, paths.processed_dir, paths.runtime_dir]:
        p.mkdir(parents=True, exist_ok=True)
    initialize_database(paths.database_path)

    # Stub config + heavy builders so the route is exercised end-to-end
    # without talking to a real LLM / Obsidian vault.
    monkeypatch.setattr(routes_module, "load_config", lambda: cfg)
    monkeypatch.setattr(
        routes_module,
        "build_synthesis_snapshot",
        lambda _cfg, _mid: {
            "summary": "",
            "key_terms": [],
            "workstreams": [],
            "decisions": [],
            "action_count": 0,
            "quality_count": 0,
            "speaker_confidence_count": 0,
            "words_available": 0,
            "next_steps": [],
        },
    )
    monkeypatch.setattr(
        routes_module,
        "build_meeting_overview",
        lambda _cfg, _mid: {
            "id": _mid,
            "title": "Test",
            "slug": "test",
            "status": "complete",
            "created_at": "",
            "duration_seconds": 0,
            "speaker_status": "ok",
            "source_file": "",
            "summary": "",
            "key_takeaways": [],
            "participants": [],
            "workstreams": [],
            "decisions": [],
            "actions": [],
            "open_questions": [],
            "obsidian_sections": {},
        },
    )
    monkeypatch.setattr(
        routes_module,
        "build_transcript_markdown",
        lambda _mid, _db: "",
    )

    with connect(paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'Demo', 'demo', '/dev/null', '/dev/null', 60, 'complete')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms,
                                             text, diarization_speaker_id, confidence)
            VALUES (10, 1, 0, 5000, 'no go ahead', 'speaker_1', 0.9),
                   (11, 1, 5000, 9000, 'i was saying', 'speaker_2', 0.85)
            """
        )
        # Segment 10 has TWO hints; segment 11 has one. After v0.2.9's
        # ORDER BY confidence DESC, kind, the higher-confidence hint
        # (rapid_alternation, 0.9) must come first for segment 10.
        conn.executemany(
            """
            INSERT INTO segment_overlap_hints
              (meeting_id, segment_id, partner_segment_id, kind, evidence, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 10, 11, "yield_marker", "no, go ahead", 0.75),
                (1, 10, 11, "rapid_alternation", "rapid switch", 0.90),
                (1, 11, 10, "stutter_interrupt", "i—i was", 0.80),
            ],
        )

    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    client = TestClient(app)
    return client, cfg


def test_meeting_detail_returns_overlap_hints(client_with_seed) -> None:
    """v0.2.9 contract: response includes overlap_hints with deterministic
    ordering. Guards against a future refactor silently dropping the key.
    """
    client, _cfg = client_with_seed

    response = client.get("/api/meetings/1")
    assert response.status_code == 200, response.text
    body = response.json()

    # The key must be present even if there were no hints — frontend
    # depends on `detail.overlap_hints ?? []`.
    assert "overlap_hints" in body
    hints = body["overlap_hints"]
    assert isinstance(hints, list)
    assert len(hints) == 3

    # v0.2.9 H1: ORDER BY segment_id, confidence DESC, kind.
    # Segment 10 has two hints; rapid_alternation (0.9) must come before
    # yield_marker (0.75).
    seg_10 = [h for h in hints if h["segment_id"] == 10]
    assert len(seg_10) == 2
    assert seg_10[0]["kind"] == "rapid_alternation"
    assert seg_10[0]["confidence"] == pytest.approx(0.9)
    assert seg_10[1]["kind"] == "yield_marker"

    # Shape check on every entry — these are the keys the frontend
    # OverlapHint type expects.
    for hint in hints:
        assert set(hint.keys()) == {
            "segment_id",
            "partner_segment_id",
            "kind",
            "evidence",
            "confidence",
        }


def test_meeting_detail_full_payload_shape(client_with_seed) -> None:
    """Asserts every top-level key the frontend MeetingDetail type expects
    is present in the response. If a route refactor drops one of these,
    this test catches it without needing a real meeting to fixture.
    """
    client, _cfg = client_with_seed

    response = client.get("/api/meetings/1")
    assert response.status_code == 200
    body = response.json()

    expected_keys = {
        "meeting",
        "segments",
        "review_items",
        "assignments",
        "source_file",
        "candidates",
        "speaker_evidence",
        "overlap_hints",
        "synthesis",
        "overview",
        "transcript_markdown",
    }
    assert expected_keys.issubset(body.keys()), (
        f"missing keys: {expected_keys - body.keys()}"
    )


def test_meeting_detail_unknown_meeting_returns_404(client_with_seed) -> None:
    client, _cfg = client_with_seed
    response = client.get("/api/meetings/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "meeting_not_found"
