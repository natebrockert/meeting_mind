"""Tests for the LLM-judged conversation drivers module.

ModelBus is mocked end-to-end so these tests stay fast and offline. The
real LLM behaviour is covered by the eval harness against fixtures —
this suite covers the plumbing: schema coercion, hallucinated-segment
filtering, cache hit/miss, and cache invalidation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.llm_drivers import (
    compute_llm_drivers,
    invalidate_llm_drivers_cache,
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


_slug_counter = {"n": 0}


def _seed_meeting_with_segments(cfg: AppConfig) -> tuple[int, list[int]]:
    """Insert a 3-segment meeting and return (meeting_id, segment_ids)."""
    _slug_counter["n"] += 1
    slug = f"llm-{_slug_counter['n']}"
    seg_ids: list[int] = []
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Test", slug, "s.m4a", f"p/{slug}.m4a", 600.0, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        for i, (speaker, text) in enumerate(
            [
                ("A", "Let's discuss the rollout."),
                ("B", "I disagree — the real blocker is QA capacity, not eng."),
                ("A", "Good point. Let's pivot to QA."),
            ]
        ):
            c = conn.execute(
                """
                INSERT INTO transcript_segments
                  (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (meeting_id, i * 30_000, (i + 1) * 30_000, text, speaker),
            )
            seg_ids.append(int(c.lastrowid))
    return meeting_id, seg_ids


class _StubModelBus:
    """Captures arguments and returns a canned payload. Mirrors the
    ModelBus.chat_json signature so the real call path is exercised.
    """

    instances: list[_StubModelBus] = []
    canned_payload: dict | Exception = {"drivers": []}

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_messages: list = []
        type(self).instances.append(self)

    def chat_json(self, messages, _schema, model=None, timeout=None, cache_prefix=None):
        self.last_messages = messages
        self.last_cache_prefix = cache_prefix
        if isinstance(type(self).canned_payload, Exception):
            raise type(self).canned_payload
        return type(self).canned_payload


@pytest.fixture(autouse=True)
def _reset_stub():
    _StubModelBus.instances = []
    _StubModelBus.canned_payload = {"drivers": []}
    yield


def test_empty_meeting_skips_call(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _slug_counter["n"] += 1
    slug = f"empty-{_slug_counter['n']}"
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Empty", slug, "s.m4a", f"p/{slug}.m4a", 0.0, "transcribed"),
        )

    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)
    out = compute_llm_drivers(cfg, 1)
    assert out == []
    # An empty transcript shouldn't waste an LLM call.
    assert _StubModelBus.instances == []


def test_valid_driver_round_trip(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, seg_ids = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {
        "drivers": [
            {
                "kind": "challenge",
                "segment_id": seg_ids[1],
                "description": "B reframed the bottleneck from eng to QA capacity.",
                "confidence": "high",
            }
        ]
    }
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    drivers = compute_llm_drivers(cfg, meeting_id)
    assert len(drivers) == 1
    assert drivers[0].kind == "challenge"
    assert drivers[0].segment_id == seg_ids[1]
    assert drivers[0].source == "llm"
    # Speaker B is unconfirmed → speaker_confirmed=False, label falls
    # back to the raw diarization id ("B").
    assert drivers[0].speaker_confirmed is False
    assert drivers[0].speaker_label == "B"


def test_hallucinated_segment_id_dropped(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, seg_ids = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {
        "drivers": [
            # One real, one fabricated. The fabricated one must be
            # silently dropped (it's a known failure mode for small
            # local models that invent integer ids).
            {
                "kind": "reframing",
                "segment_id": seg_ids[0],
                "description": "Opened with a clear question framing.",
                "confidence": "medium",
            },
            {
                "kind": "challenge",
                "segment_id": 99_999,
                "description": "Invented segment that doesn't exist.",
                "confidence": "high",
            },
        ]
    }
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    drivers = compute_llm_drivers(cfg, meeting_id)
    assert len(drivers) == 1
    assert drivers[0].kind == "reframing"


def test_cache_hit_skips_model(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, seg_ids = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {
        "drivers": [
            {
                "kind": "unstick",
                "segment_id": seg_ids[2],
                "description": "A acknowledged the new framing and moved the group on.",
                "confidence": "medium",
            }
        ]
    }
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    first = compute_llm_drivers(cfg, meeting_id)
    second = compute_llm_drivers(cfg, meeting_id)
    assert [d.model_dump() for d in first] == [d.model_dump() for d in second]
    # Exactly one ModelBus invocation across both calls — the second hit
    # the cache. Regression-checks the perf characteristic from PR #25.
    assert len(_StubModelBus.instances) == 1


def test_invalidate_forces_recompute(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, seg_ids = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {
        "drivers": [
            {
                "kind": "challenge",
                "segment_id": seg_ids[1],
                "description": "first compute.",
                "confidence": "high",
            }
        ]
    }
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)
    compute_llm_drivers(cfg, meeting_id)
    assert len(_StubModelBus.instances) == 1

    invalidate_llm_drivers_cache(cfg, meeting_id)

    # A different canned payload to prove the second call actually re-ran.
    _StubModelBus.canned_payload = {
        "drivers": [
            {
                "kind": "reframing",
                "segment_id": seg_ids[0],
                "description": "second compute.",
                "confidence": "medium",
            }
        ]
    }
    again = compute_llm_drivers(cfg, meeting_id)
    assert len(_StubModelBus.instances) == 2
    assert again[0].kind == "reframing"


def test_empty_llm_result_is_valid_cache_hit(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, _ = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {"drivers": []}
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    assert compute_llm_drivers(cfg, meeting_id) == []
    assert compute_llm_drivers(cfg, meeting_id) == []
    # Empty result is a valid cache hit. We don't re-call the model just
    # because no LLM-judged drivers were found last time — that would
    # double-charge for low-signal meetings.
    assert len(_StubModelBus.instances) == 1


def test_model_failure_returns_empty_does_not_persist(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, _ = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = RuntimeError("model unreachable")
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    out = compute_llm_drivers(cfg, meeting_id)
    assert out == []
    # No row persisted on failure — a subsequent retry should be free
    # to try again, not get stuck serving an empty cached entry.
    with connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT 1 FROM meeting_llm_drivers WHERE meeting_id = ?", (meeting_id,)
        ).fetchall()
    assert rows == []


def test_malformed_kind_and_confidence_handled(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id, seg_ids = _seed_meeting_with_segments(cfg)

    _StubModelBus.canned_payload = {
        "drivers": [
            # Bad kind — must be dropped entirely.
            {
                "kind": "INVENTED",
                "segment_id": seg_ids[0],
                "description": "should be dropped.",
                "confidence": "high",
            },
            # Bad confidence — coerces to "medium" rather than dropping.
            {
                "kind": "reframing",
                "segment_id": seg_ids[1],
                "description": "should survive at medium confidence.",
                "confidence": "off-the-charts",
            },
        ]
    }
    monkeypatch.setattr("app.services.llm_drivers.ModelBus", _StubModelBus)

    drivers = compute_llm_drivers(cfg, meeting_id)
    assert len(drivers) == 1
    assert drivers[0].kind == "reframing"
    assert drivers[0].confidence == "medium"
