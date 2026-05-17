"""Tests for the Reflections backend.

Covers: experimental-flag gating, owner-required behaviour, quality
refusals, evidence-required validation, hallucinated-segment filtering,
cache hit/miss, per-meeting opt-out, and — critically — the export
boundary that prevents Reflections content from leaking into HTML /
PDF / Obsidian / overview surfaces.

ModelBus is stubbed throughout so these tests are offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import AppConfig, ExperimentalConfig, OwnerConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.reflections import (
    Reflections,
    compute_reflections,
    invalidate_reflections_cache,
    set_meeting_skip_reflections,
)


def _test_config(tmp_path: Path, *, reflections_enabled: bool = True) -> AppConfig:
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
        experimental=ExperimentalConfig(reflections_enabled=reflections_enabled),
    )


_slug_counter = {"n": 0}


def _seed_owner(cfg: AppConfig, display_name: str = "Alex") -> int:
    """Insert a person row and configure them as the owner."""
    with connect(cfg.paths.database_path) as conn:
        person_id = int(
            conn.execute(
                "INSERT INTO people (display_name) VALUES (?)", (display_name,)
            ).lastrowid
        )
    cfg.owner = OwnerConfig(person_id=person_id, display_name=display_name, aliases=[])
    return person_id


def _seed_meeting(
    cfg: AppConfig,
    *,
    duration_seconds: float = 900.0,
    owner_label: str = "Alex",
    owner_seconds: float = 180.0,
    other_seconds: float = 480.0,
    avg_confidence: float = 0.9,
) -> tuple[int, list[int]]:
    """Build a meeting where the owner spoke for ~owner_seconds and
    one other speaker spoke for ~other_seconds. Returns (id, seg_ids).
    """
    _slug_counter["n"] += 1
    slug = f"refl-{_slug_counter['n']}"
    seg_ids: list[int] = []
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Test", slug, "s.m4a", f"p/{slug}.m4a", duration_seconds, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)
        person_id = int(
            conn.execute(
                "SELECT id FROM people WHERE display_name = ?", (owner_label,)
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, approved_label, person_id, confirmed_by_user)
            VALUES (?, ?, ?, ?, 1)
            """,
            (meeting_id, "Speaker_001", owner_label, person_id),
        )
        # Owner contributions — split into a question + a normal turn so
        # stats compute non-trivially.
        owner_ms = int(owner_seconds * 1000)
        question_ms = max(4_000, owner_ms // 4)
        s1 = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id, text_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                0,
                question_ms,
                "What's actually blocking the rollout?",
                "Speaker_001",
                avg_confidence,
            ),
        )
        seg_ids.append(int(s1.lastrowid))
        s2 = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id, text_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                question_ms,
                owner_ms,
                "I'm not sure on the QA capacity question — good question, "
                "let's dig in. What do you think Avery?",
                "Speaker_001",
                avg_confidence,
            ),
        )
        seg_ids.append(int(s2.lastrowid))
        s3 = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id, text_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                owner_ms,
                owner_ms + int(other_seconds * 1000),
                "From my side, the constraint is QA capacity, not engineering. "
                "We're short two people and that's been the bottleneck since June.",
                "Speaker_002",
                avg_confidence,
            ),
        )
        seg_ids.append(int(s3.lastrowid))
    return meeting_id, seg_ids


class _StubModelBus:
    """Returns a canned payload mirroring the real ModelBus interface."""

    instances: list[_StubModelBus] = []
    canned_payload: dict | Exception = {"observations": []}

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        type(self).instances.append(self)

    def chat_json(self, messages, _schema, model=None, timeout=None, cache_prefix=None):
        if isinstance(type(self).canned_payload, Exception):
            raise type(self).canned_payload
        return type(self).canned_payload


@pytest.fixture(autouse=True)
def _reset_stub():
    _StubModelBus.instances = []
    _StubModelBus.canned_payload = {"observations": []}
    yield


# ── flag gating + skipped reasons ────────────────────────────────────────


def test_flag_off_returns_none(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path, reflections_enabled=False)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, _ = _seed_meeting(cfg)
    # Flag off → None means "hide the surface entirely". The API
    # endpoint maps this to 404 so the frontend hides the tab.
    assert compute_reflections(cfg, meeting_id) is None


def test_no_owner_returns_skipped(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    # No owner configured. Don't call _seed_owner.
    _slug_counter["n"] += 1
    slug = f"no-owner-{_slug_counter['n']}"
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Test", slug, "s.m4a", f"p/{slug}.m4a", 600.0, "transcribed"),
        )
        meeting_id = int(cursor.lastrowid)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert result.skipped_reason == "no_owner_configured"
    assert result.observations == []


def test_short_transcript_skipped_no_llm_call(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    # Only 60s total speech — under the 5-min threshold.
    meeting_id, _ = _seed_meeting(
        cfg, duration_seconds=60, owner_seconds=20, other_seconds=20
    )
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert result.skipped_reason == "transcript_too_short"
    # Quality refusal must short-circuit BEFORE any LLM call so we
    # never spend frontier-model budget on a meeting that wouldn't
    # produce usable Reflections.
    assert _StubModelBus.instances == []


def test_low_asr_confidence_skipped(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, _ = _seed_meeting(cfg, avg_confidence=0.5)
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert result.skipped_reason == "asr_confidence_too_low"
    assert _StubModelBus.instances == []


def test_owner_spoke_too_little_skipped(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    # Total 6 min but owner only ~30s.
    meeting_id, _ = _seed_meeting(
        cfg, duration_seconds=360, owner_seconds=30, other_seconds=300
    )
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert result.skipped_reason == "owner_spoke_too_little"
    assert _StubModelBus.instances == []


def test_per_meeting_opt_out_sticky_and_clears_cache(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)

    # Populate the cache.
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "uncertainty_admission",
                "observation": f"You said 'I'm not sure' at [{seg_ids[1]}] — a strong "
                "psychological-safety signal for the team.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "medium",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    first = compute_reflections(cfg, meeting_id)
    assert isinstance(first, Reflections)
    assert len(first.observations) == 1

    # Skip the meeting. Cache should be cleared AND subsequent reads
    # should return the skipped reason.
    set_meeting_skip_reflections(cfg, meeting_id, skip=True)
    second = compute_reflections(cfg, meeting_id)
    assert second is not None
    assert second.skipped_reason == "skipped_per_meeting"
    assert second.observations == []

    # Verify the cache row was actually dropped.
    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM reflection_observations WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
    assert row is None


# ── happy path + cache ───────────────────────────────────────────────────


def test_happy_path_observation_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "invited_input",
                "observation": f"You invited Avery specifically at [{seg_ids[1]}] — "
                "named invitations get responses ~2x more often than "
                "broadcast 'thoughts?'",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "high",
                "why_this_matters": "Specific invitations land harder than "
                "broadcast ones.",
                "suggested_next_time": "One option is to keep naming the "
                "person whose perspective you most need.",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert result.owner_display_name == "Alex"
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "invited_input"
    assert obs.evidence_segment_ids == [seg_ids[1]]
    assert obs.confidence == "high"
    assert obs.suggested_next_time is not None
    # Deterministic stats populated from segments + atoms.
    assert result.stats.talk_time_seconds > 0
    assert result.stats.questions_asked >= 1
    assert result.stats.uncertainty_admissions >= 1  # "I'm not sure" / "good question"
    assert result.stats.inputs_invited >= 1  # "What do you think Avery?"


def test_cache_hit_skips_model(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "uncertainty_admission",
                "observation": "test",
                "evidence_segment_ids": [seg_ids[0]],
                "confidence": "medium",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    first = compute_reflections(cfg, meeting_id)
    second = compute_reflections(cfg, meeting_id)
    assert isinstance(first, Reflections) and isinstance(second, Reflections)
    assert first.model_dump() == second.model_dump()
    # Exactly one ModelBus invocation across both calls. Reflections
    # are expensive; this is the perf contract.
    assert len(_StubModelBus.instances) == 1


def test_invalidate_forces_recompute(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {"observations": []}
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    compute_reflections(cfg, meeting_id)
    assert len(_StubModelBus.instances) == 1

    invalidate_reflections_cache(cfg, meeting_id)
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "loop_closure",
                "observation": "Recompute trigger.",
                "evidence_segment_ids": [seg_ids[0]],
                "confidence": "medium",
            }
        ]
    }
    again = compute_reflections(cfg, meeting_id)
    assert len(_StubModelBus.instances) == 2
    assert again.observations[0].kind == "loop_closure"


# ── trust anchors: cite-or-skip + hallucinated segment ids ───────────────


def test_observation_without_evidence_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {
        "observations": [
            # No evidence — MUST be dropped. The UI refuses to render
            # observations without segment_ids and the backend enforces
            # the same rule before the data ever leaves the model layer.
            {
                "kind": "framing_quality",
                "observation": "Generic claim with no anchor.",
                "evidence_segment_ids": [],
                "confidence": "high",
            },
            {
                "kind": "uncertainty_admission",
                "observation": "Valid one with evidence.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "medium",
            },
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert len(result.observations) == 1
    assert result.observations[0].kind == "uncertainty_admission"


def test_hallucinated_segment_id_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "decision_driven",
                "observation": "Hallucinated.",
                "evidence_segment_ids": [99_999],
                "confidence": "high",
            },
            {
                "kind": "invited_input",
                "observation": "Real evidence.",
                "evidence_segment_ids": [seg_ids[1], 99_999],
                "confidence": "medium",
            },
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    # First observation drops entirely (no valid evidence after
    # filtering); second survives because at least one segment_id is real.
    assert len(result.observations) == 1
    assert result.observations[0].kind == "invited_input"
    assert result.observations[0].evidence_segment_ids == [seg_ids[1]]


def test_invalid_kind_dropped(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "MADE_UP_KIND",
                "observation": "Should be dropped.",
                "evidence_segment_ids": [seg_ids[0]],
                "confidence": "high",
            },
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert result.observations == []


# ── EXPORT BOUNDARY ──────────────────────────────────────────────────────
# These tests pin the §6.5a rule: Reflections content NEVER appears in
# HTML / PDF / Obsidian / build_meeting_overview output. The boundary
# is easy to break unintentionally when adding a new export path; the
# regression tests below catch it.


def test_reflections_not_in_build_meeting_overview(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    # Populate Reflections in the cache so we know they exist.
    distinctive = "REFLECTIONS_LEAK_CANARY_xyz"
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "uncertainty_admission",
                "observation": f"{distinctive} — should never escape.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "medium",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    compute_reflections(cfg, meeting_id)

    # Now build the overview that powers the meeting-detail endpoint
    # AND the HTML / PDF export paths. The canary string must NOT
    # appear anywhere in the serialized output.
    from app.services.obsidian_writer import build_meeting_overview

    overview = build_meeting_overview(cfg, meeting_id)
    serialized = json.dumps(overview)
    assert distinctive not in serialized, (
        "Reflections content leaked into build_meeting_overview output — "
        "export boundary violation. See "
        "docs/design/meeting-output-improvements.md §6.5a."
    )
    # Belt-and-braces: ensure there's no key suggesting Reflections
    # data is being threaded through. (`reflections`, `observations`
    # etc. — none of these should be top-level overview keys.)
    forbidden_keys = {"reflections", "observations", "owner_observations", "skipped_reason"}
    assert forbidden_keys.isdisjoint(overview.keys()), (
        f"Overview leaks Reflections-shaped keys: "
        f"{forbidden_keys.intersection(overview.keys())}"
    )


def test_llm_failure_returns_compute_error_and_does_not_persist(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, _ = _seed_meeting(cfg)

    _StubModelBus.canned_payload = RuntimeError("model unreachable")
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    # Catch-all: a transient LLM failure must NEVER raise out of the
    # compute path — that would break the dashboard. Instead the user
    # sees a Reflections with compute_error so the UI can render a
    # "couldn't generate Reflections — try again" state honestly.
    assert isinstance(result, Reflections)
    assert result.skipped_reason == "compute_error"
    assert result.observations == []

    # And the failure must NOT poison the cache: a subsequent retry
    # should be free to try again, not get stuck serving an empty
    # cached entry.
    with connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT 1 FROM reflection_observations WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()
    assert rows == []


def test_commitments_made_only_counts_owner_actions(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    owner_pid = _seed_owner(cfg)
    meeting_id, _ = _seed_meeting(cfg)

    # Add two actions: one to the owner, one to a different person.
    # Pre-fix, the truthy check on owner_person_id credited BOTH to the
    # owner. After the fix, only the actual owner-attributed action
    # counts.
    with connect(cfg.paths.database_path) as conn:
        other_pid = int(
            conn.execute(
                "INSERT INTO people (display_name) VALUES (?)", ("Avery",)
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO action_items (meeting_id, owner_person_id, text, priority)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, owner_pid, "Alex sends the deck", "normal"),
        )
        conn.execute(
            """
            INSERT INTO action_items (meeting_id, owner_person_id, text, priority)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, other_pid, "Avery reviews the doc", "normal"),
        )
        # Plus an unattributed action that happens to start with "I will" —
        # under the old text-prefix fallback this would have wrongly
        # credited the owner. The fix drops the fallback entirely.
        conn.execute(
            """
            INSERT INTO action_items (meeting_id, owner_person_id, text, priority)
            VALUES (?, NULL, ?, ?)
            """,
            (meeting_id, "I will follow up with finance", "normal"),
        )

    _StubModelBus.canned_payload = {"observations": []}
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    # Exactly one commitment: the action explicitly owned by the owner.
    assert result.stats.commitments_made == 1


def test_evidence_accepts_string_typed_integers(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    # Local models under loose structured-output enforcement frequently
    # serialize JSON integers as strings. The observation must survive
    # the coercion step, not get dropped for "empty evidence".
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "uncertainty_admission",
                "observation": "Test segment-id-as-string coercion.",
                "evidence_segment_ids": [str(seg_ids[1])],
                "confidence": "medium",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    assert isinstance(result, Reflections)
    assert len(result.observations) == 1
    assert result.observations[0].evidence_segment_ids == [seg_ids[1]]


def test_optional_prose_fields_capped_at_280_chars(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    long_string = "x" * 1000
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "invited_input",
                "observation": "Test prose-field caps.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "medium",
                "why_this_matters": long_string,
                "suggested_next_time": long_string,
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)

    result = compute_reflections(cfg, meeting_id)
    obs = result.observations[0]
    # A runaway model on loose schema enforcement could otherwise persist
    # multi-kilobyte strings — bad for UI rendering and cache I/O.
    assert obs.why_this_matters is not None and len(obs.why_this_matters) <= 280
    assert obs.suggested_next_time is not None and len(obs.suggested_next_time) <= 280


def test_reflections_not_in_obsidian_render(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    distinctive = "REFLECTIONS_LEAK_CANARY_obsidian"
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "specific_invitation",
                "observation": f"{distinctive} must not appear in vault notes.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "high",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    compute_reflections(cfg, meeting_id)

    from app.services.obsidian_writer import (
        load_meeting_export_data,
        render_meeting_note,
    )

    data = load_meeting_export_data(cfg, meeting_id)
    rendered = render_meeting_note(data, status="promoted")
    assert distinctive not in rendered, (
        "Reflections content leaked into Obsidian Markdown output — "
        "export boundary violation."
    )


def test_reflections_not_in_html_export(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    distinctive = "REFLECTIONS_LEAK_CANARY_html"
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "decision_driven",
                "observation": f"{distinctive} must never appear in HTML export.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "high",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    compute_reflections(cfg, meeting_id)

    from app.services.html_export import render_meeting_html_string

    rendered = render_meeting_html_string(cfg, meeting_id)
    assert distinctive not in rendered, (
        "Reflections content leaked into HTML export — export boundary "
        "violation. See docs/design/meeting-output-improvements.md §6.5a."
    )


def test_reflections_not_in_pdf_export(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    _seed_owner(cfg)
    meeting_id, seg_ids = _seed_meeting(cfg)
    distinctive = "REFLECTIONS_LEAK_CANARY_pdf"
    _StubModelBus.canned_payload = {
        "observations": [
            {
                "kind": "paraphrase_check",
                "observation": f"{distinctive} must never appear in PDF export.",
                "evidence_segment_ids": [seg_ids[1]],
                "confidence": "high",
            }
        ]
    }
    monkeypatch.setattr("app.services.reflections.ModelBus", _StubModelBus)
    compute_reflections(cfg, meeting_id)

    # PDF export goes through the HTML pipeline before binary
    # rasterisation. Verifying the upstream HTML doesn't carry the
    # canary covers the PDF surface without depending on weasyprint
    # in test deps. If a future PDF backend skips the HTML step we'd
    # need to add a binary-content check here too.
    from app.services.html_export import render_meeting_html_string

    html_for_pdf = render_meeting_html_string(cfg, meeting_id)
    assert distinctive not in html_for_pdf, (
        "Reflections content leaked into PDF-bound HTML — export "
        "boundary violation."
    )
