"""Eval-fixture regression tests for Conversation Drivers + CoG.

Runs deterministic compute against realistic fixtures and asserts the
quality bar. LLM-judged kinds are off by default — see eval/README.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.conversation_drivers import compute_drivers_and_cog
from app.services.meeting_health import compute_meeting_health

from .fixtures.decision_heavy_meeting import FIXTURE as DECISION_FIXTURE
from .fixtures.dominated_strategy_meeting import FIXTURE as DOMINATED_FIXTURE
from .fixtures.pivot_question_meeting import FIXTURE as PIVOT_FIXTURE
from .harness import Fixture, make_test_config, seed_fixture
from .scoring import score_drivers

_FIXTURES = [PIVOT_FIXTURE, DOMINATED_FIXTURE, DECISION_FIXTURE]


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda f: f.name)
def test_driver_quality_against_fixture(tmp_path: Path, fixture: Fixture) -> None:
    cfg = make_test_config(tmp_path)
    meeting_id, segment_ids = seed_fixture(cfg, fixture)

    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )

    actual_kinds = [d.kind for d in drivers]
    actual_seg_ids = [d.segment_id for d in drivers]
    expected_kinds = [e.kind for e in fixture.expectations.drivers]
    expected_seg_ids = [
        segment_ids[e.segment_index] if e.segment_index is not None else None
        for e in fixture.expectations.drivers
    ]
    resolved_expected = [s for s in expected_seg_ids if s is not None]
    score = score_drivers(
        actual_kinds=actual_kinds,
        actual_segment_ids=actual_seg_ids,
        expected_kinds=expected_kinds,
        expected_segment_ids=expected_seg_ids,
        resolved_expected_seg_ids=resolved_expected,
    )

    # Quality bar — every fixture must recall ≥80% of expected driver
    # kinds and match every pinned segment. Tunable per fixture later
    # if a specific case needs leniency, but for the v1 set these are
    # the floor we want to hold across schema/threshold changes.
    assert score.recall_kind >= 0.8, (
        f"{fixture.name}: recall too low — got {score.recall_kind:.2f} on "
        f"expected kinds {expected_kinds}, actual {actual_kinds}"
    )
    assert score.segment_matches == score.segment_total, (
        f"{fixture.name}: {score.segment_matches}/{score.segment_total} "
        f"pinned-segment matches. actual=(kind,seg)="
        f"{list(zip(actual_kinds, actual_seg_ids, strict=False))}, "
        f"expected={list(zip(expected_kinds, expected_seg_ids, strict=False))}"
    )

    # CoG standout assertion — fixture says either a specific speaker
    # or explicitly None ("no surprise"). Both directions matter.
    expected_standout = fixture.expectations.standout_speaker_id
    assert cog.standout_speaker_id == expected_standout, (
        f"{fixture.name}: standout mismatch — "
        f"expected {expected_standout!r}, got {cog.standout_speaker_id!r} "
        f"(reason: {cog.standout_reason!r})"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda f: f.name)
def test_meeting_health_against_fixture(tmp_path: Path, fixture: Fixture) -> None:
    cfg = make_test_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, fixture)
    health = compute_meeting_health(cfg, meeting_id)

    if fixture.expectations.participation_balance is not None:
        assert health.participation_balance == fixture.expectations.participation_balance, (
            f"{fixture.name}: participation_balance "
            f"expected {fixture.expectations.participation_balance}, "
            f"got {health.participation_balance} (top {health.top_speaker_share})"
        )
    if fixture.expectations.decision_density is not None:
        assert health.decision_density == fixture.expectations.decision_density, (
            f"{fixture.name}: decision_density "
            f"expected {fixture.expectations.decision_density}, "
            f"got {health.decision_density} ({health.decision_count} decisions "
            f"over {fixture.duration_seconds}s)"
        )
    if fixture.expectations.action_clarity is not None:
        assert health.action_clarity == fixture.expectations.action_clarity, (
            f"{fixture.name}: action_clarity "
            f"expected {fixture.expectations.action_clarity}, "
            f"got {health.action_clarity}"
        )
