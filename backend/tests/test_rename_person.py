"""v0.2.10: rename-person service + route tests.

Two modes:

  1. Plain rename — no other person has the target name; we update the
     `people` row and every speaker_assignment label in one txn.
  2. Merge — the target name already exists on a different person row;
     all FK references repoint to the target, the source row drops.

Plus the error cases and a route-level smoke test through TestClient.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from app.api import routes as routes_module
from app.api.routes import router as api_router
from app.config import AppConfig, AsrConfig, DiarizationConfig, PathConfig, ReviewConfig
from app.db.database import initialize_database
from app.services.aux_features import rename_person
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _cfg(tmp_path: Path) -> AppConfig:
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
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        review=ReviewConfig(),
    )
    initialize_database(paths.database_path)
    with sqlite3.connect(paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'Demo', 'demo', '/dev/null', '/dev/null', 60, 'complete')
            """
        )
    return cfg


def _seed_carl(cfg: AppConfig) -> tuple[int, int]:
    """Insert a 'Carl' person + Speaker 4 assignment + a segment +
    an action item attributed to Carl. Returns (carl_id, segment_id).
    """
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO people (id, display_name) VALUES (5, 'Carl')"
        )
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, person_id, approved_label,
               confirmed_by_user, confidence)
            VALUES (1, 'Speaker 4', 5, 'Carl', 1, 1.0)
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id)
            VALUES (10, 1, 0, 5000, 'hello', 'Speaker 4', 5)
            """
        )
        conn.execute(
            """
            INSERT INTO action_items
              (id, meeting_id, text, owner_person_id, priority, status)
            VALUES (100, 1, 'Follow up', 5, 'normal', 'open')
            """
        )
    return 5, 10


def test_rename_in_place_updates_people_and_labels(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)

    result = rename_person(cfg, carl_id, "Paul")

    assert result == {
        "status": "ok",
        "result": "renamed",
        "from": "Carl",
        "to": "Paul",
        "person_id": carl_id,
    }
    with sqlite3.connect(cfg.paths.database_path) as conn:
        person = conn.execute(
            "SELECT display_name FROM people WHERE id = ?", (carl_id,)
        ).fetchone()
        assert person[0] == "Paul"
        label = conn.execute(
            "SELECT approved_label FROM speaker_assignments WHERE person_id = ?",
            (carl_id,),
        ).fetchone()
        assert label[0] == "Paul"


def test_rename_to_existing_name_merges_records(tmp_path: Path) -> None:
    """If 'Paul' already exists as a separate person, renaming Carl to
    Paul should MERGE — repoint Carl's references to Paul's id and
    delete the Carl row.
    """
    cfg = _cfg(tmp_path)
    carl_id, segment_id = _seed_carl(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute("INSERT INTO people (id, display_name) VALUES (7, 'Paul')")

    result = rename_person(cfg, carl_id, "Paul")

    assert result["result"] == "merged"
    assert result["person_id"] == 7
    with sqlite3.connect(cfg.paths.database_path) as conn:
        # Carl row is gone.
        carl = conn.execute(
            "SELECT id FROM people WHERE id = ?", (carl_id,)
        ).fetchone()
        assert carl is None
        # Speaker assignment now points at Paul (id=7) with label "Paul".
        sa = conn.execute(
            "SELECT person_id, approved_label FROM speaker_assignments "
            "WHERE meeting_id = 1 AND diarization_speaker_id = 'Speaker 4'"
        ).fetchone()
        assert sa[0] == 7
        assert sa[1] == "Paul"
        # Segment + action item also repointed.
        seg = conn.execute(
            "SELECT assigned_person_id FROM transcript_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
        assert seg[0] == 7
        action = conn.execute(
            "SELECT owner_person_id FROM action_items WHERE id = 100"
        ).fetchone()
        assert action[0] == 7


def test_rename_unknown_person_raises_not_found(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="person_not_found"):
        rename_person(cfg, 999, "Whoever")


def test_rename_blank_name_raises(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    with pytest.raises(ValueError, match="name_required"):
        rename_person(cfg, carl_id, "   ")


def test_rename_same_name_raises(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    with pytest.raises(ValueError, match="same_name"):
        rename_person(cfg, carl_id, "Carl")


def test_route_rename_returns_200(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    monkeypatch.setattr(routes_module, "load_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    client = TestClient(app)

    response = client.post(f"/api/people/{carl_id}/rename?new_name=Paul")
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "renamed"


def test_route_rename_missing_returns_404(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(routes_module, "load_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/people/999/rename?new_name=Whoever")
    assert response.status_code == 404
    assert response.json()["detail"] == "person_not_found"


def test_rename_owner_migrates_config(tmp_path: Path) -> None:
    """v0.2.10 audit M1: if the renamed person was the configured
    'you', the owner config should follow the rename so /api/owner
    keeps pointing at the right person.
    """
    from app.services.owner import load_owner, set_owner

    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    set_owner(cfg, carl_id, "Carl", aliases=["cb"])
    assert load_owner(cfg).person_id == carl_id
    assert load_owner(cfg).display_name == "Carl"

    rename_person(cfg, carl_id, "Paul")

    owner = load_owner(cfg)
    assert owner.person_id == carl_id  # same row, new label
    assert owner.display_name == "Paul"
    assert "cb" in owner.aliases  # aliases preserved


def test_rename_merge_owner_repoints_to_target(tmp_path: Path) -> None:
    """v0.2.10 audit M1: on merge, owner should point at the surviving
    target person id with the new name."""
    from app.services.owner import load_owner, set_owner

    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute("INSERT INTO people (id, display_name) VALUES (7, 'Paul')")
    set_owner(cfg, carl_id, "Carl", aliases=[])

    result = rename_person(cfg, carl_id, "Paul")
    assert result["result"] == "merged"

    owner = load_owner(cfg)
    assert owner.person_id == 7  # repointed to the target
    assert owner.display_name == "Paul"


def test_route_rename_too_long_returns_422(tmp_path: Path, monkeypatch) -> None:
    """v0.2.10 audit H1: the rename query param is capped at 200 chars.
    Anything longer must be rejected by FastAPI before hitting the
    service layer (422 Unprocessable Entity is FastAPI's default for
    Query validation failures).
    """
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    monkeypatch.setattr(routes_module, "load_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        f"/api/people/{carl_id}/rename?new_name={'A' * 250}"
    )
    assert response.status_code == 422


def test_route_rename_same_name_returns_409(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    carl_id, _ = _seed_carl(cfg)
    monkeypatch.setattr(routes_module, "load_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    client = TestClient(app)

    response = client.post(f"/api/people/{carl_id}/rename?new_name=Carl")
    assert response.status_code == 409
    assert response.json()["detail"] == "same_name"
