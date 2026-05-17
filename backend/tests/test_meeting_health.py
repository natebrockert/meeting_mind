from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.meeting_health import compute_meeting_health


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


# Module-level counter so each _seed_meeting() call within a test gets a
# unique slug. The slug column is UNIQUE; reusing it across seeds raised
# IntegrityError in test_decision_density_thresholds.
_slug_counter = {"n": 0}


def _seed_meeting(cfg: AppConfig, *, duration_seconds: float = 1800.0) -> int:
    _slug_counter["n"] += 1
    slug = f"test-{_slug_counter['n']}"
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Test", slug, "src.m4a", f"p/{slug}.m4a", duration_seconds, "transcribed"),
        )
        return int(cursor.lastrowid)


def _add_segment(
    cfg: AppConfig,
    meeting_id: int,
    *,
    speaker: str,
    text: str,
    start_ms: int,
    end_ms: int,
) -> None:
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, start_ms, end_ms, text, speaker),
        )


def _add_decision(cfg: AppConfig, meeting_id: int, decision: str) -> None:
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json)
            VALUES (?, 'decision', ?, ?)
            """,
            (meeting_id, decision[:80], json.dumps({"decision": decision})),
        )


def _add_open_question(
    cfg: AppConfig, meeting_id: int, question: str, status: str = "unanswered"
) -> None:
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json)
            VALUES (?, 'open_question', ?, ?)
            """,
            (meeting_id, question[:80], json.dumps({"question": question, "status": status})),
        )


def _add_action(
    cfg: AppConfig,
    meeting_id: int,
    *,
    text: str,
    owner_person_id: int | None,
    due_date: str | None,
) -> None:
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO action_items
              (meeting_id, owner_person_id, text, due_date, priority)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, owner_person_id, text, due_date, "normal"),
        )


def test_compute_meeting_health_empty(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg, duration_seconds=0)

    health = compute_meeting_health(cfg, meeting_id)
    # No transcript, no decisions, actions, or questions, no duration —
    # every signal resolves to a "skip rendering" None or 0 so the UI can
    # hide each chip cleanly rather than show misleading zeroes.
    assert health.participation_balance is None
    assert health.decision_density is None
    assert health.action_clarity is None
    assert health.speaker_count_active == 0
    assert health.unresolved_question_count == 0


def test_participation_balance_balanced(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Three speakers, each contributing roughly a third of the words →
    # top speaker share ≤ 0.40 → "balanced".
    _add_segment(cfg, meeting_id, speaker="A", text="one two three four five",
                 start_ms=0, end_ms=70_000)
    _add_segment(cfg, meeting_id, speaker="B", text="six seven eight nine ten",
                 start_ms=70_000, end_ms=140_000)
    _add_segment(cfg, meeting_id, speaker="C", text="alpha beta gamma delta epsilon",
                 start_ms=140_000, end_ms=210_000)

    health = compute_meeting_health(cfg, meeting_id)
    assert health.participation_balance == "balanced"
    assert health.speaker_count_active == 3
    assert health.speaker_count_silent == 0


def test_participation_balance_dominated(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Speaker A monopolizes — ~95% of words. Confirms the "dominated"
    # bucket fires and that B and C land in speaker_count_silent (each
    # under 60s of speech).
    _add_segment(cfg, meeting_id, speaker="A",
                 text=" ".join(["word"] * 80), start_ms=0, end_ms=300_000)
    _add_segment(cfg, meeting_id, speaker="B", text="yep", start_ms=300_000, end_ms=302_000)
    _add_segment(cfg, meeting_id, speaker="C", text="agreed", start_ms=302_000, end_ms=303_000)

    health = compute_meeting_health(cfg, meeting_id)
    assert health.participation_balance == "dominated"
    assert health.top_speaker_share is not None and health.top_speaker_share > 0.60
    assert health.speaker_count_active == 1
    assert health.speaker_count_silent == 2


def test_decision_density_thresholds(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    # 30-min meeting, 0 decisions → "low".
    low_id = _seed_meeting(cfg, duration_seconds=1800)
    assert compute_meeting_health(cfg, low_id).decision_density == "low"

    # 30-min meeting, 2 decisions → "moderate".
    mod_id = _seed_meeting(cfg, duration_seconds=1800)
    _add_decision(cfg, mod_id, "Decision one")
    _add_decision(cfg, mod_id, "Decision two")
    assert compute_meeting_health(cfg, mod_id).decision_density == "moderate"

    # 30-min meeting, 5 decisions → "high".
    high_id = _seed_meeting(cfg, duration_seconds=1800)
    for n in range(5):
        _add_decision(cfg, high_id, f"Decision {n}")
    assert compute_meeting_health(cfg, high_id).decision_density == "high"


def test_unresolved_questions_respects_status(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    _add_open_question(cfg, meeting_id, "Q1", status="unanswered")
    _add_open_question(cfg, meeting_id, "Q2", status="partially_answered")
    _add_open_question(cfg, meeting_id, "Q3", status="deferred")
    _add_open_question(cfg, meeting_id, "Q4", status="unanswered")

    health = compute_meeting_health(cfg, meeting_id)
    # Two unanswered; the partial and deferred should not count.
    assert health.unresolved_question_count == 2


def test_unresolved_questions_legacy_payload_defaults_to_unanswered(
    tmp_path: Path,
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Legacy OQ payload shape: {"text": "..."} with no status field. The
    # missing status defaults to unanswered per the chip rules — this
    # leans toward surfacing work rather than hiding it.
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json)
            VALUES (?, 'open_question', ?, ?)
            """,
            (meeting_id, "Legacy Q", json.dumps({"text": "Legacy Q"})),
        )

    assert compute_meeting_health(cfg, meeting_id).unresolved_question_count == 1


def test_action_clarity(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    with connect(cfg.paths.database_path) as conn:
        person_id = int(
            conn.execute(
                "INSERT INTO people (display_name) VALUES (?)", ("Avery",)
            ).lastrowid
        )
    # 4 actions: 1 has both owner + date (well-specified), 3 do not.
    _add_action(cfg, meeting_id, text="Owned + dated",
                owner_person_id=person_id, due_date="2026-06-01")
    _add_action(cfg, meeting_id, text="Owned only",
                owner_person_id=person_id, due_date=None)
    _add_action(cfg, meeting_id, text="Date only",
                owner_person_id=None, due_date="2026-06-15")
    _add_action(cfg, meeting_id, text="Neither", owner_person_id=None, due_date=None)

    health = compute_meeting_health(cfg, meeting_id)
    assert health.action_count == 4
    # 1/4 = 25% well-specified → below 30% threshold → "low".
    assert health.action_clarity == "low"


def test_action_clarity_none_when_no_actions(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    health = compute_meeting_health(cfg, meeting_id)
    # Zero actions → action_clarity should be None so the UI skips the
    # chip rather than showing a misleading "low" on an empty set.
    assert health.action_clarity is None
    assert health.action_count == 0


def test_top_speaker_label_uses_approved_name(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    _add_segment(cfg, meeting_id, speaker="Speaker 1",
                 text=" ".join(["word"] * 50), start_ms=0, end_ms=200_000)
    _add_segment(cfg, meeting_id, speaker="Speaker 2",
                 text="brief", start_ms=200_000, end_ms=201_000)
    # Confirm Speaker 1 → Avery via speaker_assignments so the chip can
    # render the human-readable name instead of "Speaker 1".
    with connect(cfg.paths.database_path) as conn:
        person_id = int(
            conn.execute(
                "INSERT INTO people (display_name) VALUES (?)", ("Avery",)
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, approved_label, person_id, confirmed_by_user)
            VALUES (?, ?, ?, ?, 1)
            """,
            (meeting_id, "Speaker 1", "Avery", person_id),
        )

    health = compute_meeting_health(cfg, meeting_id)
    assert health.top_speaker_label == "Avery"
