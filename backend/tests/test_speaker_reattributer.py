"""Tests for v0.2.4 speaker re-attribution (Pass C).

The deterministic parts (windowing, dedupe, prompt construction) are
unit-tested here with a mocked ModelBus. The actual LLM-quality dimension
is validated by the eval harness against fixture meetings.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.services.repair.speaker_reattributer import (
    ReattributionProposal,
    _build_prompt,
    _dedupe_proposals,
    persist_speaker_reattribution_proposals,
    propose_speaker_reattributions,
)


def _make_window(segments: list[tuple[int, str, str]]) -> list[dict]:
    """Helper: build a window of {id, text, speaker, start_ms, end_ms}."""
    return [
        {
            "id": sid,
            "speaker": speaker,
            "text": text,
            "start_ms": i * 1000,
            "end_ms": (i + 1) * 1000,
        }
        for i, (sid, speaker, text) in enumerate(segments)
    ]


def test_prompt_includes_all_window_segments() -> None:
    """The LLM prompt must list every segment in the window so the model
    has full context for its decisions."""
    window = _make_window(
        [
            (1, "Speaker 1", "Welcome everyone."),
            (2, "Speaker 1", "Hi, I'm Alice."),
            (3, "Speaker 2", "Thanks for joining."),
        ]
    )
    prompt = _build_prompt(window)
    assert "#1" in prompt
    assert "#2" in prompt
    assert "#3" in prompt
    assert "Speaker 1" in prompt
    assert "Alice" in prompt


def test_prompt_truncates_long_segments() -> None:
    """Very long segments should be truncated so the prompt stays bounded."""
    long_text = "blah " * 200  # 1000+ chars
    window = _make_window([(1, "A", long_text)])
    prompt = _build_prompt(window)
    # The full thing should NOT appear verbatim
    assert long_text not in prompt
    # But some prefix should
    assert "blah blah" in prompt


def test_dedupe_keeps_highest_confidence() -> None:
    """Overlapping windows can produce two proposals for the same segment.
    Dedupe keeps the higher-confidence one."""
    proposals = [
        ReattributionProposal(
            segment_id=5,
            current_speaker="A",
            proposed_speaker="B",
            confidence=0.7,
            basis="introduced",
        ),
        ReattributionProposal(
            segment_id=5,
            current_speaker="A",
            proposed_speaker="B",
            confidence=0.85,
            basis="direct address",
        ),
        ReattributionProposal(
            segment_id=7,
            current_speaker="A",
            proposed_speaker="C",
            confidence=0.65,
            basis="Q→A",
        ),
    ]
    deduped = _dedupe_proposals(proposals)
    assert len(deduped) == 2
    seg5 = next(p for p in deduped if p.segment_id == 5)
    assert seg5.confidence == 0.85  # higher one wins


def test_dedupe_handles_empty_list() -> None:
    assert _dedupe_proposals([]) == []


def test_propose_returns_empty_when_disabled(tmp_path: Path) -> None:
    """Feature flag off → no LLM call, no DB read, return immediately."""
    cfg = _build_test_config(tmp_path, enabled=False)
    # No fixture in DB at all — function should short-circuit before reading
    assert propose_speaker_reattributions(cfg, meeting_id=42) == []


def test_propose_returns_empty_when_no_segments(tmp_path: Path) -> None:
    """Empty meeting → empty proposals, no LLM call."""
    cfg = _build_test_config(tmp_path, enabled=True)
    # DB exists but the meeting has no segments
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT
            );
            """
        )
    result = propose_speaker_reattributions(cfg, meeting_id=1)
    assert result == []


def test_propose_filters_out_hallucinated_speakers(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit M-A regression (v0.2.6): if the LLM hallucinates a name
    not in the window's labels (e.g. proposes 'Alice' when only
    'Speaker 1' and 'Speaker 2' exist), the proposal must be filtered
    out rather than persisted."""
    cfg = _build_test_config(tmp_path, enabled=True)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT
            );
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (1, 1, 0, 2000, 'Hello.', 'Speaker 1');
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (2, 1, 2000, 4000, 'Hi.', 'Speaker 2');
            """
        )

    def fake_llm_score(_config, _window):
        return [
            # In-window label — should be kept
            {
                "segment_id": 1,
                "current_speaker": "Speaker 1",
                "proposed_speaker": "Speaker 2",
                "confidence": 0.85,
                "basis": "context",
            },
            # Hallucinated label — should be filtered
            {
                "segment_id": 2,
                "current_speaker": "Speaker 2",
                "proposed_speaker": "Alice",
                "confidence": 0.95,
                "basis": "hallucinated",
            },
        ]

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window", fake_llm_score
    )
    proposals = propose_speaker_reattributions(cfg, meeting_id=1)
    assert len(proposals) == 1
    assert proposals[0].segment_id == 1
    assert proposals[0].proposed_speaker == "Speaker 2"
    # Hallucinated 'Alice' must NOT be present
    assert all(p.proposed_speaker != "Alice" for p in proposals)


def test_propose_with_mocked_llm_filters_low_confidence(
    tmp_path: Path, monkeypatch
) -> None:
    """Window LLM returns 3 decisions: one low-confidence (filtered),
    one same-as-current (filtered), one valid (kept)."""
    cfg = _build_test_config(tmp_path, enabled=True)
    # Seed two segments with current labels
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT
            );
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (10, 1, 0, 2000, 'Hi, this is Alice.', 'Speaker 1');
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (11, 1, 2000, 4000, 'Yes, welcome.', 'Speaker 2');
            """
        )

    def fake_llm_score(_config, _window):
        return [
            # Below default threshold (0.6) — should be dropped
            {
                "segment_id": 10,
                "current_speaker": "Speaker 1",
                "proposed_speaker": "Speaker 2",
                "confidence": 0.4,
                "basis": "weak",
            },
            # Same as current — should be dropped
            {
                "segment_id": 11,
                "current_speaker": "Speaker 2",
                "proposed_speaker": "Speaker 2",
                "confidence": 0.9,
                "basis": "no change",
            },
            # Valid — high-confidence, in-window label change, should be kept
            {
                "segment_id": 10,
                "current_speaker": "Speaker 1",
                "proposed_speaker": "Speaker 2",
                "confidence": 0.85,
                "basis": "context",
            },
        ]

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window", fake_llm_score
    )
    proposals = propose_speaker_reattributions(cfg, meeting_id=1)
    # After dedupe + filters, only the high-confidence Speaker 2 proposal remains
    assert len(proposals) == 1
    assert proposals[0].segment_id == 10
    assert proposals[0].proposed_speaker == "Speaker 2"
    assert proposals[0].confidence == 0.85


def test_persist_writes_review_items(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: proposals get inserted as review_items rows with
    kind='speaker_reattribution' and the correct payload."""
    cfg = _build_test_config(tmp_path, enabled=True)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT
            );
            CREATE TABLE IF NOT EXISTS review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              meeting_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              confidence REAL,
              source_segment_ids TEXT NOT NULL DEFAULT '[]'
            );
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (1, 1, 0, 2000, 'Hello, I am Alice.', 'Speaker 1');
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (2, 1, 2000, 4000, 'Great, hi everyone.', 'Alice');
            """
        )

    def fake_llm_score(_config, _window):
        return [
            {
                "segment_id": 1,
                "current_speaker": "Speaker 1",
                "proposed_speaker": "Alice",  # in-window: yes (segment 2 has that label)
                "confidence": 0.9,
                "basis": "self-introduction",
            }
        ]

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window", fake_llm_score
    )
    summary = persist_speaker_reattribution_proposals(cfg, meeting_id=1)
    assert summary["total"] == 1
    assert summary["manual"] == 1
    assert summary["auto_applied"] == 0

    with sqlite3.connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT kind, title, payload_json, confidence FROM review_items WHERE meeting_id = 1"
        ).fetchone()
    assert row is not None
    kind, title, payload, confidence = row
    assert kind == "speaker_reattribution"
    assert "Speaker 1" in title and "Alice" in title
    assert confidence == 0.9
    import json as _json

    parsed = _json.loads(payload)
    assert parsed["proposed_speaker"] == "Alice"


def test_persist_clears_prior_proposals(tmp_path: Path, monkeypatch) -> None:
    """Re-running on the same meeting must clear stale proposals first
    so old corrections don't accumulate."""
    cfg = _build_test_config(tmp_path, enabled=True)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT
            );
            CREATE TABLE IF NOT EXISTS review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              meeting_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              confidence REAL,
              source_segment_ids TEXT NOT NULL DEFAULT '[]'
            );
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (1, 1, 0, 2000, 'Test', 'Speaker 1');
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (2, 1, 2000, 4000, 'More text', 'Speaker 2');
            -- Pre-existing stale proposal from a previous run
            INSERT INTO review_items (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
              VALUES (1, 'speaker_reattribution', 'STALE', '{}', 0.9, '[1]');
            -- A different kind — must NOT be deleted
            INSERT INTO review_items (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
              VALUES (1, 'workstream', 'keepme', '{}', 0.8, '[]');
            """
        )

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window",
        lambda _c, _w: [],  # no new proposals — but should still clear stale
    )
    persist_speaker_reattribution_proposals(cfg, meeting_id=1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT kind, title FROM review_items WHERE meeting_id = 1"
        ).fetchall()
    assert len(rows) == 1
    kind, title = rows[0]
    assert kind == "workstream"  # the other kind survived
    assert title == "keepme"


def test_accept_proposal_updates_transcript_segment(
    tmp_path: Path, monkeypatch
) -> None:
    """Accepting a reattribution proposal must update the speaker label
    in `transcript_segments` AND mark the review_item resolved."""
    from app.config import AppConfig, DiarizationConfig, PathConfig

    db_path = tmp_path / "real.sqlite3"
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=db_path,
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        diarization=DiarizationConfig(),
    )
    from app.db.database import initialize_database

    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'T', 't', '/tmp/a', '/tmp/a', 2.0, 'transcribed')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, 1, 0, 2000, 'Hello', 'Speaker 1')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'T1->T2', ?, 'open', '[10]')
            """,
            (
                json.dumps(
                    {
                        "segment_id": 10,
                        "current_speaker": "Speaker 1",
                        "proposed_speaker": "Speaker 2",
                    }
                ),
            ),
        )
        review_id = cursor.lastrowid

    from app.services.repair.speaker_reattributer import accept_reattribution_proposal

    result = accept_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)
    assert result["segment_id"] == 10
    assert result["previous_speaker"] == "Speaker 1"
    assert result["new_speaker"] == "Speaker 2"

    with sqlite3.connect(db_path) as conn:
        speaker = conn.execute(
            "SELECT diarization_speaker_id FROM transcript_segments WHERE id = 10"
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM review_items WHERE id = ?", (review_id,)
        ).fetchone()[0]
    assert speaker == "Speaker 2"
    assert status == "resolved"


def test_accept_proposal_already_resolved_raises(tmp_path: Path) -> None:
    """Re-accepting an already-resolved proposal is an error — protects
    against double-application from a stale UI click."""
    from app.config import AppConfig, DiarizationConfig, PathConfig
    from app.db.database import initialize_database
    from app.services.repair.speaker_reattributer import accept_reattribution_proposal

    db_path = tmp_path / "x.sqlite3"
    paths = PathConfig(
        repo_root=tmp_path,
        database_path=db_path,
    )
    cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        paths=paths,
        diarization=DiarizationConfig(),
    )
    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'T', 't', '/x', '/x', 1.0, 'transcribed')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'T', ?, 'resolved', '[1]')
            """,
            (json.dumps({"segment_id": 1, "proposed_speaker": "X"}),),
        )
        review_id = cursor.lastrowid

    try:
        accept_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)
        raise AssertionError("should have raised")
    except ValueError as exc:
        assert "already_resolved" in str(exc)


def test_reject_is_idempotent_on_already_rejected(tmp_path: Path) -> None:
    """Audit M2 regression (v0.2.8): rejecting twice is a no-op,
    matching the docstring contract."""
    from app.config import AppConfig, DiarizationConfig, PathConfig
    from app.db.database import initialize_database
    from app.services.repair.speaker_reattributer import reject_reattribution_proposal

    db_path = tmp_path / "x.sqlite3"
    paths = PathConfig(repo_root=tmp_path, database_path=db_path)
    cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        paths=paths,
        diarization=DiarizationConfig(),
    )
    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'T', 't', '/x', '/x', 1.0, 'transcribed')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'T', ?, 'rejected', '[1]')
            """,
            (json.dumps({"segment_id": 1, "proposed_speaker": "X"}),),
        )
        review_id = cursor.lastrowid

    # No raise, no DB change
    reject_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)
    reject_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)


def test_reject_refuses_to_flip_accepted_proposal(tmp_path: Path) -> None:
    """Audit M2 regression: previously, calling reject on a status=resolved
    row would silently overwrite it to 'rejected', ghost-applying the
    transcript edit. Now it raises already_resolved."""
    from app.config import AppConfig, DiarizationConfig, PathConfig
    from app.db.database import initialize_database
    from app.services.repair.speaker_reattributer import reject_reattribution_proposal

    db_path = tmp_path / "y.sqlite3"
    paths = PathConfig(repo_root=tmp_path, database_path=db_path)
    cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        paths=paths,
        diarization=DiarizationConfig(),
    )
    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'T', 't', '/x', '/x', 1.0, 'transcribed')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'T', ?, 'resolved', '[1]')
            """,
            (json.dumps({"segment_id": 1, "proposed_speaker": "X"}),),
        )
        review_id = cursor.lastrowid

    try:
        reject_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)
        raise AssertionError("expected ValueError on rejecting already-accepted")
    except ValueError as exc:
        assert "already_resolved" in str(exc)


def test_accept_rolls_back_status_if_reassign_fails(tmp_path: Path, monkeypatch) -> None:
    """Audit M3 regression: if `reassign_segment_speaker` raises after
    we've flipped the review item to resolved, we must roll back so the
    proposal stays available in the queue. Otherwise the user sees the
    proposal vanish without the transcript actually changing."""
    from app.config import AppConfig, DiarizationConfig, PathConfig
    from app.db.database import initialize_database
    from app.services.repair.speaker_reattributer import accept_reattribution_proposal

    db_path = tmp_path / "z.sqlite3"
    paths = PathConfig(repo_root=tmp_path, database_path=db_path)
    cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        paths=paths,
        diarization=DiarizationConfig(),
    )
    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'T', 't', '/x', '/x', 1.0, 'transcribed')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (1, 1, 0, 1000, 'hi', 'Speaker 1')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, status, source_segment_ids)
            VALUES (1, 'speaker_reattribution', 'T', ?, 'open', '[1]')
            """,
            (
                json.dumps(
                    {
                        "segment_id": 1,
                        "current_speaker": "Speaker 1",
                        "proposed_speaker": "Speaker 2",
                    }
                ),
            ),
        )
        review_id = cursor.lastrowid

    def boom(*_a, **_k):
        raise RuntimeError("simulated reassign failure")

    # `reassign_segment_speaker` is imported lazily inside the function;
    # monkeypatch the source module so the lazy import picks up the boom.
    monkeypatch.setattr(
        "app.services.transcript_editor.reassign_segment_speaker", boom
    )

    try:
        accept_reattribution_proposal(cfg, meeting_id=1, review_item_id=review_id)
        raise AssertionError("expected the simulated failure to propagate")
    except RuntimeError as exc:
        assert "simulated" in str(exc)

    # The status flip must have been rolled back
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id = ?", (review_id,)
        ).fetchone()[0]
        # And the speaker label stayed unchanged
        speaker = conn.execute(
            "SELECT diarization_speaker_id FROM transcript_segments WHERE id = 1"
        ).fetchone()[0]
    assert status == "open", f"expected status rolled back to 'open', got {status!r}"
    assert speaker == "Speaker 1"


def _seed_simple_proposal(cfg, monkeypatch) -> None:
    """Seed two segments + a fake LLM that proposes seg 1's Speaker 1
    should be re-labeled as Alice, with confidence 0.9.
    """
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              id INTEGER PRIMARY KEY,
              meeting_id INTEGER,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              diarization_speaker_id TEXT,
              assigned_person_id INTEGER,
              text_confidence REAL,
              speaker_confidence REAL
            );
            CREATE TABLE IF NOT EXISTS review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              meeting_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              confidence REAL,
              source_segment_ids TEXT NOT NULL DEFAULT '[]',
              resolved_at TEXT
            );
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (1, 1, 0, 2000, 'Hello, I am Alice.', 'Speaker 1');
            INSERT INTO transcript_segments (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
              VALUES (2, 1, 2000, 4000, 'Great, hi everyone.', 'Alice');
            """
        )

    def fake_llm_score(_config, _window):
        return [
            {
                "segment_id": 1,
                "current_speaker": "Speaker 1",
                "proposed_speaker": "Alice",
                "confidence": 0.9,
                "basis": "self-introduction",
            }
        ]

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window", fake_llm_score
    )


def test_auto_apply_silent_tier_writes_resolved_review_item(tmp_path, monkeypatch) -> None:
    """v0.2.11: a Pass C proposal at silent threshold applies in-place
    and flips the row to status='auto_applied'. The transcript
    segment's speaker_id is actually updated.
    """
    cfg = _build_test_config(tmp_path, enabled=True)
    cfg.repair.auto_apply_enabled = True
    cfg.repair.auto_apply_silent_threshold = 0.5
    cfg.repair.auto_apply_toast_threshold = 0.4
    _seed_simple_proposal(cfg, monkeypatch)

    # reassign_segment_speaker uses Pydantic config paths that the
    # SimpleNamespace fake doesn't provide. Stub it.
    applied: list[tuple] = []

    def fake_reassign(_cfg, mid, sid, target):
        applied.append((mid, sid, target))
        # Simulate the real side-effect: update segment speaker
        with sqlite3.connect(cfg.paths.database_path) as conn:
            conn.execute(
                "UPDATE transcript_segments SET diarization_speaker_id = ? "
                "WHERE id = ? AND meeting_id = ?",
                (target, sid, mid),
            )

    monkeypatch.setattr(
        "app.services.transcript_editor.reassign_segment_speaker", fake_reassign
    )

    summary = persist_speaker_reattribution_proposals(cfg, meeting_id=1)
    assert summary == {"total": 1, "auto_applied": 1, "manual": 0}
    assert applied == [(1, 1, "Alice")]

    with sqlite3.connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT status, payload_json FROM review_items WHERE meeting_id = 1"
        ).fetchone()
        assert row[0] == "auto_applied"
        import json as _json

        assert _json.loads(row[1])["tier"] == "silent"
        seg = conn.execute(
            "SELECT diarization_speaker_id FROM transcript_segments WHERE id = 1"
        ).fetchone()
        assert seg[0] == "Alice"


def test_auto_apply_failure_demotes_to_manual(tmp_path, monkeypatch) -> None:
    """If reassign_segment_speaker raises, the proposal stays as
    'open' (manual review) and `manual` is incremented, not
    `auto_applied`. The audit row never lies about an apply that
    didn't happen.
    """
    cfg = _build_test_config(tmp_path, enabled=True)
    cfg.repair.auto_apply_enabled = True
    cfg.repair.auto_apply_silent_threshold = 0.5
    cfg.repair.auto_apply_toast_threshold = 0.4
    _seed_simple_proposal(cfg, monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("reassign blew up")

    monkeypatch.setattr(
        "app.services.transcript_editor.reassign_segment_speaker", explode
    )

    summary = persist_speaker_reattribution_proposals(cfg, meeting_id=1)
    # The proposal counts as manual because apply failed.
    assert summary["total"] == 1
    assert summary["auto_applied"] == 0
    assert summary["manual"] == 1

    with sqlite3.connect(cfg.paths.database_path) as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]
        # Row exists and is 'open' so the user can apply by hand.
        assert status == "open"
        # Segment was NOT mutated.
        seg = conn.execute(
            "SELECT diarization_speaker_id FROM transcript_segments WHERE id = 1"
        ).fetchone()
        assert seg[0] == "Speaker 1"


def test_auto_apply_disabled_keeps_v0210_behavior(tmp_path, monkeypatch) -> None:
    """`auto_apply_enabled=False` → every proposal manual regardless of
    confidence."""
    cfg = _build_test_config(tmp_path, enabled=True)
    cfg.repair.auto_apply_enabled = False
    _seed_simple_proposal(cfg, monkeypatch)
    summary = persist_speaker_reattribution_proposals(cfg, meeting_id=1)
    assert summary["auto_applied"] == 0
    assert summary["manual"] == 1


def test_dataclass_shape() -> None:
    """Regression check — persistence depends on every field."""
    proposal = ReattributionProposal(
        segment_id=1,
        current_speaker="Speaker 1",
        proposed_speaker="Alice",
        confidence=0.85,
        basis="self-introduction",
    )
    assert proposal.proposed_speaker == "Alice"
    assert proposal.confidence == 0.85


# Helpers ----------------------------------------------------------------


def _build_test_config(tmp_path: Path, *, enabled: bool):
    """Minimal config stub for testing — only the fields the
    re-attributer reads are populated."""
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Touch the file so connect() succeeds
    sqlite3.connect(db_path).close()

    return SimpleNamespace(
        repair=SimpleNamespace(
            speaker_reattribution_enabled=enabled,
            speaker_reattribution_window_size=12,
            speaker_reattribution_min_confidence=0.6,
            speaker_reattribution_max_segments=240,
            # v0.2.11: keep tests on the manual-review path unless
            # they explicitly opt in to auto-apply.
            auto_apply_enabled=False,
            auto_apply_silent_threshold=0.90,
            auto_apply_toast_threshold=0.70,
        ),
        paths=SimpleNamespace(database_path=db_path),
    )
