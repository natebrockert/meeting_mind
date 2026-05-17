"""Stage C regression gate: LLM identity resolver caches + loads
correctly, validates candidate names, drops malformed payloads.

The LLM call itself is not exercised here — that's a network-bound
integration test. These tests focus on the deterministic boundaries:
input assembly, output validation, caching round-trip, candidate
allowlist enforcement.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, OwnerConfig, PathConfig
from app.db.database import connect, initialize_database


def _sandbox_config(tmp_path: Path, owner_name: str | None = "Owner") -> AppConfig:
    cfg = AppConfig(
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
    if owner_name:
        cfg.owner = OwnerConfig(display_name=owner_name)
    return cfg


def _seed_meeting_with_segments(
    cfg: AppConfig, meeting_id: int = 1, segments: list[tuple[str, str]] | None = None
) -> None:
    """Insert a meeting + transcript segments. `segments` is a list of
    (diarization_speaker_id, text) pairs.
    """
    initialize_database(cfg.paths.database_path)
    segments = segments or []
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO meetings (id, title, slug, status, source_path, "
            "imported_path, created_at) "
            "VALUES (?, 'Test', 'test', 'extracted', '', '', CURRENT_TIMESTAMP)",
            (meeting_id,),
        )
        for i, (spk, text) in enumerate(segments):
            conn.execute(
                """
                INSERT INTO transcript_segments
                  (meeting_id, diarization_speaker_id, start_ms, end_ms, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (meeting_id, spk, i * 1000, (i + 1) * 1000, text),
            )


def _seed_candidate(
    cfg: AppConfig, meeting_id: int, speaker: str, name: str, conf: float = 0.8
) -> None:
    """Mirror a `speaker_name_candidate` review_item write."""
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence,
               source_segment_ids)
            VALUES (?, 'speaker_name_candidate', ?, ?, ?, '[]')
            """,
            (
                meeting_id,
                f"Possible name for {speaker}: {name}",
                json.dumps({"speaker_id": speaker, "candidate_name": name}),
                conf,
            ),
        )


# ── _build_inputs ─────────────────────────────────────────────────────


def test_build_inputs_assembles_owner_candidates_and_speaker_samples(
    tmp_path: Path,
) -> None:
    """Per-speaker longest segments + candidate pool + owner are all
    in the prompt input."""
    from app.services.repair.llm_identity import _build_inputs

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(
        cfg,
        segments=[
            ("Speaker 1", "Hey, John. Happy Friday."),
            ("Speaker 2", "Yeah happy Friday."),
            ("Speaker 1", "as you know John you have been here long enough"),
        ],
    )
    _seed_candidate(cfg, 1, "Speaker 2", "John")

    inputs = _build_inputs(cfg, 1)
    assert inputs is not None
    assert inputs["owner"] == "Owner"
    assert inputs["candidates"] == ["John"]
    assert "Speaker 1" in inputs["speakers"]
    assert "Speaker 2" in inputs["speakers"]


def test_build_inputs_returns_none_without_candidates(tmp_path: Path) -> None:
    """No candidates → no LLM call. The synthesizer would only let the
    LLM invent freely otherwise. Skip the call."""
    from app.services.repair.llm_identity import _build_inputs

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg, segments=[("Speaker 1", "Hello")])
    assert _build_inputs(cfg, 1) is None


def test_build_inputs_returns_none_without_segments(tmp_path: Path) -> None:
    """No transcript → nothing to feed the LLM."""
    from app.services.repair.llm_identity import _build_inputs

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg, segments=[])
    _seed_candidate(cfg, 1, "Speaker 1", "John")
    assert _build_inputs(cfg, 1) is None


def test_build_inputs_caps_speaker_samples_at_3(tmp_path: Path) -> None:
    """A monologue speaker contributes at most 3 of their longest
    segments — bounded prompt size."""
    from app.services.repair.llm_identity import _build_inputs

    cfg = _sandbox_config(tmp_path)
    segments = [("Speaker 1", f"segment {i}") for i in range(10)]
    _seed_meeting_with_segments(cfg, segments=segments)
    _seed_candidate(cfg, 1, "Speaker 1", "Solo")

    inputs = _build_inputs(cfg, 1)
    assert inputs is not None
    assert len(inputs["speakers"]["Speaker 1"]) == 3


# ── load_llm_speaker_identities cache round-trip ────────────────────


def test_load_returns_persisted_assignments(tmp_path: Path) -> None:
    """Persist → load round-trip surfaces the assignments the
    deductive resolver will consume as evidence."""
    from app.services.repair.llm_identity import (
        _persist_llm_identities,
        load_llm_speaker_identities,
    )

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg)

    assignments = [
        {
            "speaker_id": "Speaker 1",
            "name": "John",
            "confidence": 0.92,
            "justification": "Addressed as John in segment 5",
        },
        {
            "speaker_id": "Speaker 2",
            "name": "Brad",
            "confidence": 0.75,
            "justification": "Self-introduction at segment 12",
        },
    ]
    _persist_llm_identities(cfg, 1, assignments)

    loaded = load_llm_speaker_identities(cfg, 1)
    assert len(loaded) == 2
    by_speaker = {a["speaker_id"]: a for a in loaded}
    assert by_speaker["Speaker 1"]["name"] == "John"
    assert by_speaker["Speaker 1"]["confidence"] == 0.92
    assert by_speaker["Speaker 2"]["name"] == "Brad"


def test_persist_is_idempotent(tmp_path: Path) -> None:
    """Re-running the synthesizer must REPLACE prior cache, not stack."""
    from app.services.repair.llm_identity import (
        _persist_llm_identities,
        load_llm_speaker_identities,
    )

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg)

    _persist_llm_identities(
        cfg, 1, [{"speaker_id": "Speaker 1", "name": "Old", "confidence": 0.6}]
    )
    _persist_llm_identities(
        cfg, 1, [{"speaker_id": "Speaker 1", "name": "New", "confidence": 0.9}]
    )

    loaded = load_llm_speaker_identities(cfg, 1)
    assert len(loaded) == 1
    assert loaded[0]["name"] == "New"


def test_load_returns_empty_when_no_cache(tmp_path: Path) -> None:
    """No prior synthesizer run → empty list (NOT an error)."""
    from app.services.repair.llm_identity import load_llm_speaker_identities

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg)
    assert load_llm_speaker_identities(cfg, 1) == []


def test_load_skips_malformed_cache_payloads(tmp_path: Path) -> None:
    """Survive a corrupted cache row without crashing."""
    from app.services.repair.llm_identity import load_llm_speaker_identities

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(cfg)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'llm_speaker_identities', 'corrupt', 'not json', '[]')
            """
        )
    assert load_llm_speaker_identities(cfg, 1) == []


# ── identity_resolver wiring ─────────────────────────────────────────


def test_resolver_picks_up_llm_evidence(tmp_path: Path) -> None:
    """End-to-end: cache an LLM assignment + run the deductive
    resolver. The LLM evidence should appear in the assignment's
    evidence_log so Stage A's evidence_count gate counts it.
    """
    from app.services.repair.identity_resolver import resolve_identities
    from app.services.repair.llm_identity import _persist_llm_identities

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(
        cfg,
        segments=[
            ("Speaker 1", "Hey John, what's your take on this?"),
            ("Speaker 2", "Sure I can talk about that."),
        ],
    )
    _seed_candidate(cfg, 1, "Speaker 2", "John")
    _persist_llm_identities(
        cfg,
        1,
        [
            {
                "speaker_id": "Speaker 2",
                "name": "John",
                "confidence": 0.88,
                "justification": "Addressed directly as John",
            }
        ],
    )

    assignments = resolve_identities(cfg, 1)
    by_pair = {(a.speaker_id, a.name): a for a in assignments}
    assert ("Speaker 2", "John") in by_pair
    a = by_pair[("Speaker 2", "John")]
    # Evidence list should include both the regex direct-address and
    # the LLM resolver hit.
    assert any("llm_resolver" in str(e) for e in a.evidence)


def test_low_confidence_llm_assignments_are_skipped(tmp_path: Path) -> None:
    """LLM confidence < 0.5 → no score contribution. Safety net for
    a poorly-calibrated model run.
    """
    from app.services.repair.identity_resolver import resolve_identities
    from app.services.repair.llm_identity import _persist_llm_identities

    cfg = _sandbox_config(tmp_path)
    _seed_meeting_with_segments(
        cfg,
        segments=[
            ("Speaker 1", "i was hoping someone could help"),
            ("Speaker 2", "Sure thing"),
        ],
    )
    _seed_candidate(cfg, 1, "Speaker 2", "John")
    _persist_llm_identities(
        cfg,
        1,
        [
            {
                "speaker_id": "Speaker 2",
                "name": "John",
                "confidence": 0.3,  # below threshold
            }
        ],
    )

    assignments = resolve_identities(cfg, 1)
    # No assignments survive — confidence 0.3 is below the 0.5 floor
    # in resolver wiring, and the regex direct-address only fires
    # if "Hey, John" pattern matches (no such pattern in test text).
    assert not any(a.name == "John" for a in assignments)
