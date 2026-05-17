"""End-to-end test for the synthetic Sample Co fixture.

Verifies that:
  - The fixture loads without error
  - `seed_fixture` populates segments, speakers, decisions, actions,
    open questions, workstreams, and the rich summary payload
  - The owner-action filter in `annotate_overview_for_owner` finds
    Alex's assigned actions (proves For-You renders correctly)
  - Meeting status flips to 'extracted' so the dashboard renders the
    full Mind Map instead of a "review pending" placeholder

This is the regression gate for the fixture — if a future schema
change breaks any of the seed paths, the test fails before the
fixture silently desyncs from production layout.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, OwnerConfig, PathConfig
from app.db.database import connect
from app.services.obsidian_writer import build_meeting_overview
from app.services.owner import annotate_overview_for_owner, load_owner
from tests.eval.fixtures.sample_company_q3_roadmap import FIXTURE
from tests.eval.harness import seed_fixture


def _sandbox_config(tmp_path: Path) -> AppConfig:
    """A throwaway AppConfig rooted in tmp_path — no overlap with the
    developer's live install.
    """
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


def test_fixture_seeds_all_extraction_outputs(tmp_path: Path) -> None:
    """Walks the seeded DB end-to-end and asserts every surface the
    dashboard renders has data.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, segment_ids = seed_fixture(cfg, FIXTURE)

    with connect(cfg.paths.database_path) as conn:
        meeting = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        seg_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()[0]
        speakers = conn.execute(
            "SELECT COUNT(*) FROM speaker_assignments "
            "WHERE meeting_id = ? AND confirmed_by_user = 1",
            (meeting_id,),
        ).fetchone()[0]
        actions = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()[0]
        # "Visible" actions = canonicals + standalones. Members are
        # folded under their canonical at read time.
        actions_visible = conn.execute(
            "SELECT COUNT(*) FROM action_items "
            "WHERE meeting_id = ? AND (cluster_role IS NULL OR cluster_role = 'canonical')",
            (meeting_id,),
        ).fetchone()[0]
        actions_with_owner = conn.execute(
            "SELECT COUNT(*) FROM action_items "
            "WHERE meeting_id = ? AND owner_person_id IS NOT NULL",
            (meeting_id,),
        ).fetchone()[0]
        decisions = conn.execute(
            "SELECT COUNT(*) FROM review_items "
            "WHERE meeting_id = ? AND kind = 'decision'",
            (meeting_id,),
        ).fetchone()[0]
        open_qs = conn.execute(
            "SELECT COUNT(*) FROM review_items "
            "WHERE meeting_id = ? AND kind = 'open_question'",
            (meeting_id,),
        ).fetchone()[0]
        workstreams = conn.execute(
            "SELECT COUNT(*) FROM review_items "
            "WHERE meeting_id = ? AND kind = 'workstream'",
            (meeting_id,),
        ).fetchone()[0]
        summary_row = conn.execute(
            "SELECT payload_json FROM review_items "
            "WHERE meeting_id = ? AND kind = 'summary'",
            (meeting_id,),
        ).fetchone()

    assert meeting["status"] == "extracted"
    assert seg_count == len(FIXTURE.segments)
    assert speakers == len(FIXTURE.speaker_assignments)
    assert actions == len(FIXTURE.actions)
    # The fixture deliberately includes one near-duplicate pair (the
    # first action + the "Friday" paraphrase), so visible (= canonical +
    # standalone) is one fewer than seeded actions.
    assert actions_visible == len(FIXTURE.actions) - 1
    # All-but-one actions are owner-assigned; the unassigned one stays
    # standalone in its own cluster.
    assert actions_with_owner == sum(1 for a in FIXTURE.actions if a.owner_speaker)
    assert decisions == len(FIXTURE.decisions)
    assert open_qs == len(FIXTURE.open_questions)
    assert workstreams == len(FIXTURE.workstreams)

    assert summary_row is not None
    payload = json.loads(summary_row["payload_json"])
    assert payload["tldr"].startswith("Sam reframed")
    assert len(payload["themes"]) == len(FIXTURE.summary.themes)
    assert len(payload["key_takeaways"]) == len(FIXTURE.summary.key_takeaways)
    # Stat callouts surface in the Mind Map's stat-tile row.
    assert len(payload["stat_callouts"]) == len(FIXTURE.summary.stat_callouts)
    # Participant contributions drive the Minutes "By person" view.
    contributors = {c["speaker"] for c in payload["participant_contributions"]}
    assert contributors == {"Alex", "Sam", "Riley", "Jordan", "Casey"}


def test_alex_as_owner_sees_assigned_actions(tmp_path: Path) -> None:
    """The For-You filter must find Alex's assigned actions after seeding.
    Without this the Mind Map's "your action items" section is empty —
    that was the P0 bug fixed in PR #46 and is the primary thing the
    fixture exists to demonstrate.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    # Mark Alex as the configured owner.
    with connect(cfg.paths.database_path) as conn:
        alex_id = conn.execute(
            "SELECT id FROM people WHERE display_name = ?", ("Alex",)
        ).fetchone()["id"]
    cfg.owner = OwnerConfig(
        person_id=int(alex_id), display_name="Alex", aliases=[]
    )

    overview = build_meeting_overview(cfg, meeting_id)
    owner_view = load_owner(cfg)
    annotate_overview_for_owner(overview, owner_view)

    # Alex owns 3 canonical actions in the fixture (move QA hiring +
    # dashboard IA session + check-in calendar invite). A fourth Alex-
    # owned action is a deliberate paraphrase of "move QA hiring..." and
    # gets folded under that canonical at read time, so the For-You
    # section still surfaces 3 rows, not 4.
    expected_alex_canonicals = 3
    assert overview["your_action_count"] == expected_alex_canonicals


def test_open_questions_carry_raised_by_attribution(tmp_path: Path) -> None:
    """The For-You "questions you raised" filter reads `raised_by` on
    open questions. The fixture should populate this for the questions
    Alex asks so that surface has content too.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    overview = build_meeting_overview(cfg, meeting_id)
    raised_by_alex = [
        oq for oq in overview.get("open_question_details", [])
        if oq.get("raised_by") == "Alex"
    ]
    expected = sum(
        1 for q in FIXTURE.open_questions if q.raised_by_speaker == "A"
    )
    assert len(raised_by_alex) == expected
    assert expected >= 1, "fixture should have at least one question raised by the owner"


def test_executive_recap_renders_three_sections(tmp_path: Path) -> None:
    """The pre-baked executive recap should round-trip into the overview
    with all three sections intact (reframe, strategy with bullets,
    risk). Editorial voice + visual treatment are the recap's value
    prop; this gate keeps the wire format honest.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    overview = build_meeting_overview(cfg, meeting_id)
    recap = overview.get("executive_recap")
    assert recap is not None, "executive_recap missing from overview"

    assert recap["reframe"]["header"].startswith("Sam saw")
    assert "**six weeks instead of four**" in recap["reframe"]["body"]
    # Italics used exactly once on the pivotal claim per the prompt rule.
    italics_count = recap["reframe"]["body"].count("*Sequencing only")
    assert italics_count == 1

    strategy = recap["strategy"]
    assert strategy["header"] == "Every commitment serves the new strategy."
    assert len(strategy["bullets"]) >= 4
    # Every bullet has owner + commitment; purpose may be None on bullets
    # the model couldn't honestly tie to the through-line. The fixture
    # has purposes on all five.
    for bullet in strategy["bullets"]:
        assert bullet["owner"]
        assert bullet["commitment"]
    purposes = [b["purpose"] for b in strategy["bullets"] if b["purpose"]]
    assert len(purposes) == len(strategy["bullets"]), (
        "fixture should populate purpose on every bullet"
    )
    assert strategy["trailer"], "fixture has a deferred-item trailer"

    risk = recap["risk"]
    assert risk["header"] == "Open risk"
    assert "QA hire" in risk["body"]
    # Risk-section voice rule: factual, not editorial.
    assert "not explicitly discussed" in risk["body"]


def test_executive_recap_absent_when_not_seeded(tmp_path: Path) -> None:
    """When the fixture has no executive_recap, the overview falls back
    to None — the frontend renders the existing tldr/summary surface
    rather than a half-empty recap.
    """
    from dataclasses import replace

    from tests.eval.fixtures.sample_company_q3_roadmap import FIXTURE as F

    no_recap_summary = replace(F.summary, executive_recap=None)
    no_recap_fixture = replace(F, summary=no_recap_summary)

    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, no_recap_fixture)
    overview = build_meeting_overview(cfg, meeting_id)
    assert overview.get("executive_recap") is None


def test_near_duplicate_actions_cluster(tmp_path: Path) -> None:
    """The deliberate near-dupe ("Move QA hiring..." canonical + the
    Friday-paraphrase member) should collapse into one visible action
    with the member attached as evidence.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    overview = build_meeting_overview(cfg, meeting_id)
    details = overview["action_details"]

    # Find the canonical for the QA-hiring cluster — the only action
    # whose details carry cluster_members.
    with_members = [d for d in details if d.get("cluster_members")]
    assert len(with_members) == 1, (
        "expected exactly one clustered action; saw "
        f"{[d['text'] for d in with_members]}"
    )
    canonical = with_members[0]
    assert "QA hiring" in canonical["text"]
    assert len(canonical["cluster_members"]) == 1
    member = canonical["cluster_members"][0]
    assert "Friday" in member["text"]

    # Due-date supersession: the later paraphrase (2026-07-25) wins,
    # the earlier commitment (2026-07-17) lands in history.
    assert canonical["due_date"] == "2026-07-25"
    history = canonical.get("due_date_history") or []
    assert any(h.get("date") == "2026-07-17" for h in history), (
        f"expected 2026-07-17 in due_date_history, got {history}"
    )


def test_clustered_member_segments_merge_onto_canonical(tmp_path: Path) -> None:
    """The canonical's source_segment_ids should include every member's
    citations so the evidence-pill row shows both mentions.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    overview = build_meeting_overview(cfg, meeting_id)
    canonical = next(
        d for d in overview["action_details"] if d.get("cluster_members")
    )
    seg_ids = set(canonical["source_segment_ids"])
    member_seg_ids = set(canonical["cluster_members"][0]["source_segment_ids"])
    assert member_seg_ids.issubset(seg_ids), (
        f"canonical missing member segments — canon={seg_ids}, "
        f"member={member_seg_ids}"
    )


def test_open_question_statuses_render(tmp_path: Path) -> None:
    """The Mind Map renders status pills (`partially_answered`,
    `deferred`) on open questions. The fixture deliberately includes
    both non-default statuses so the status-pill code path is exercised.
    """
    cfg = _sandbox_config(tmp_path)
    meeting_id, _ = seed_fixture(cfg, FIXTURE)

    overview = build_meeting_overview(cfg, meeting_id)
    statuses = {
        oq.get("status") for oq in overview.get("open_question_details", [])
    }
    assert "partially_answered" in statuses
    assert "deferred" in statuses
