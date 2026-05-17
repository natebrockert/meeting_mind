"""Sweep orphan processing_jobs on system restart.

Regression gate for the bug where the "Restart system" button left
`processing_jobs` rows in `running` state, which the dashboard then
read forever as `TRANSCRIPTION RUNNING 5%` — gating the user behind
a fake in-progress job. The dying process can't finish those rows
and the new process has no resume hook; the only honest answer is
to mark them failed at restart time and let the user re-trigger.

These tests verify the sweep logic without invoking subprocess.Popen
(which would actually try to restart the backend). They drive the
DB-mutation portion directly to isolate the regression.
"""

from __future__ import annotations

from pathlib import Path

from app.api.routes import _SWEEP_ORPHAN_JOBS_SQL
from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database


def _sandbox_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=PathConfig(
            repo_root=tmp_path,
            data_dir=tmp_path / "data",
            inbox_dir=tmp_path / "data" / "inbox",
            processed_dir=tmp_path / "data" / "processed",
            archive_dir=tmp_path / "data" / "archive",
            delete_review_dir=tmp_path / "data" / "delete-review",
            runtime_dir=tmp_path / "runtime",
            database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
            vault_dir=tmp_path / "vault" / "meeting_mind",
        ),
    )


def _sweep(cfg: AppConfig) -> int:
    """Run the exact UPDATE the restart endpoint runs. Imports the SQL
    constant from the route module so this test can't silently drift
    if the production SQL changes.
    """
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(_SWEEP_ORPHAN_JOBS_SQL)
        return cursor.rowcount


def test_sweep_marks_running_and_queued_as_failed(tmp_path: Path) -> None:
    """Both `running` and `queued` jobs get marked failed with the
    standard error message. Terminal states (`complete`, `failed`)
    are left untouched so the audit trail of past runs is preserved.
    """
    cfg = _sandbox_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO meetings (id, title, slug, status, source_path, "
            "imported_path, created_at) "
            "VALUES (1, 'Test', 'test-meeting', 'ingested', '', '', "
            "CURRENT_TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO processing_jobs (meeting_id, stage, status, "
            "progress) VALUES (?, ?, ?, ?)",
            [
                (1, "transcription", "running", 0.05),
                (1, "extraction", "queued", 0.0),
                (1, "ingestion", "complete", 1.0),
                (1, "old_run", "failed", 0.5),
            ],
        )

    swept = _sweep(cfg)
    assert swept == 2

    with connect(cfg.paths.database_path) as conn:
        rows = {
            row["stage"]: (row["status"], row["error"])
            for row in conn.execute(
                "SELECT stage, status, error FROM processing_jobs "
                "ORDER BY id"
            )
        }
    assert rows["transcription"] == ("failed", "orphaned at restart")
    assert rows["extraction"] == ("failed", "orphaned at restart")
    # Terminal-state rows are untouched.
    assert rows["ingestion"][0] == "complete"
    assert rows["old_run"][0] == "failed"
    assert rows["old_run"][1] is None  # not stomped with our message


def test_sweep_leaves_partial_status_alone(tmp_path: Path) -> None:
    """The `partial` status is written by asr_candidates.py:135 when
    SOME but not all ASR candidates succeed — and is written
    alongside `completed_at`, so it's terminal-by-construction.
    The sweep must not stomp it. This test pins that decision so
    a future hand at the WHERE clause doesn't accidentally widen it.
    """
    cfg = _sandbox_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO meetings (id, title, slug, status, source_path, "
            "imported_path, created_at) "
            "VALUES (1, 'Test', 'test-meeting', 'transcribed', '', '', "
            "CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO processing_jobs "
            "(meeting_id, stage, status, progress, completed_at) "
            "VALUES (1, 'asr_candidates', 'partial', 1.0, "
            "CURRENT_TIMESTAMP)"
        )

    swept = _sweep(cfg)
    assert swept == 0

    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT status FROM processing_jobs WHERE meeting_id = 1"
        ).fetchone()
    assert row["status"] == "partial"


def test_sweep_no_op_when_nothing_running(tmp_path: Path) -> None:
    """Idempotency: calling sweep against a clean DB returns 0 and
    doesn't touch any rows.
    """
    cfg = _sandbox_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO meetings (id, title, slug, status, source_path, "
            "imported_path, created_at) "
            "VALUES (1, 'Test', 'test-meeting', 'extracted', '', '', "
            "CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO processing_jobs (meeting_id, stage, status) "
            "VALUES (1, 'ingestion', 'complete')"
        )

    swept = _sweep(cfg)
    assert swept == 0
