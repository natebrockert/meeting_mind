"""Tests for v0.2.6 quality_hints — the synthesis-prompt hedging helper.

Verifies the hint reader reaches into both `segment_overlap_hints`
(v0.2.2) and `review_items` with `kind='speaker_reattribution'`
(v0.2.4), filters to segments inside a given chunk, and renders a
hedging prompt fragment.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.services.repair.quality_hints import (
    augmented_chunk_text,
    gather_hints_for_chunk,
)


def _make_db(tmp_path: Path) -> Path:
    """Build a minimal SQLite DB with the two tables we read from."""
    db_path = tmp_path / "test.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE segment_overlap_hints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              meeting_id INTEGER NOT NULL,
              segment_id INTEGER NOT NULL,
              partner_segment_id INTEGER,
              kind TEXT NOT NULL,
              evidence TEXT NOT NULL DEFAULT '',
              confidence REAL NOT NULL DEFAULT 0.5
            );
            CREATE TABLE review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              meeting_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              confidence REAL,
              source_segment_ids TEXT NOT NULL DEFAULT '[]'
            );
            """
        )
    return db_path


def _cfg(tmp_path: Path):
    return SimpleNamespace(paths=SimpleNamespace(database_path=_make_db(tmp_path)))


def test_no_hints_returns_empty_fragment(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[1, 2, 3])
    assert result.prompt_fragment == ""
    assert result.overlap_segment_ids == []
    assert result.reattribution_segment_ids == []


def test_empty_segment_ids_short_circuits(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[])
    assert result.prompt_fragment == ""


def test_overlap_hint_appears_in_fragment(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO segment_overlap_hints
              (meeting_id, segment_id, kind, evidence, confidence)
            VALUES (1, 5, 'yield_marker', 'sorry, go ahead', 0.85)
            """
        )
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[3, 4, 5, 6])
    assert result.overlap_segment_ids == [5]
    assert "#5" in result.prompt_fragment
    assert "overlap" in result.prompt_fragment.lower()


def test_overlap_hint_filters_to_chunk_segments(tmp_path: Path) -> None:
    """A hint for a segment OUTSIDE the chunk must not appear."""
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO segment_overlap_hints
              (meeting_id, segment_id, kind, evidence, confidence)
            VALUES (1, 99, 'yield_marker', 'sorry, go ahead', 0.85)
            """
        )
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[1, 2, 3])
    assert result.overlap_segment_ids == []
    assert result.prompt_fragment == ""


def test_reattribution_hint_appears(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'demo', ?, 'open', '[42]')
            """,
            (
                json.dumps(
                    {
                        "segment_id": 42,
                        "current_speaker": "Speaker 1",
                        "proposed_speaker": "Alice",
                    }
                ),
            ),
        )
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[40, 41, 42, 43])
    assert result.reattribution_segment_ids == [42]
    assert "Speaker 1" in result.prompt_fragment
    assert "Alice" in result.prompt_fragment
    assert "hedge" in result.prompt_fragment.lower() or "impersonal" in result.prompt_fragment.lower()


def test_reattribution_filters_to_chunk_segments(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'demo', ?, 'open', '[99]')
            """,
            (
                json.dumps(
                    {
                        "segment_id": 99,
                        "current_speaker": "S1",
                        "proposed_speaker": "S2",
                    }
                ),
            ),
        )
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[1, 2, 3])
    assert result.reattribution_segment_ids == []


def test_reattribution_skips_non_open_status(tmp_path: Path) -> None:
    """A resolved reattribution proposal must NOT influence synthesis."""
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'demo', ?, 'rejected', '[5]')
            """,
            (
                json.dumps(
                    {
                        "segment_id": 5,
                        "current_speaker": "A",
                        "proposed_speaker": "B",
                    }
                ),
            ),
        )
    result = gather_hints_for_chunk(cfg, meeting_id=1, segment_ids=[3, 4, 5, 6])
    assert result.reattribution_segment_ids == []


def test_augmented_chunk_text_appends_fragment(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO segment_overlap_hints
              (meeting_id, segment_id, kind, evidence, confidence)
            VALUES (1, 7, 'yield_marker', 'sorry, go ahead', 0.85)
            """
        )
    augmented = augmented_chunk_text(
        cfg,
        meeting_id=1,
        chunk_text="transcript content here",
        segment_ids=[6, 7, 8],
    )
    assert augmented.startswith("transcript content here")
    assert "QUALITY HINTS" in augmented
    assert "#7" in augmented


def test_augmented_chunk_text_unchanged_when_no_hints(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    augmented = augmented_chunk_text(
        cfg,
        meeting_id=1,
        chunk_text="transcript content here",
        segment_ids=[1, 2, 3],
    )
    assert augmented == "transcript content here"
