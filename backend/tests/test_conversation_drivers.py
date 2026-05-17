from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.conversation_drivers import compute_drivers_and_cog


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


def _seed_meeting(cfg: AppConfig, *, duration_seconds: float = 1800.0) -> int:
    _slug_counter["n"] += 1
    slug = f"drivers-{_slug_counter['n']}"
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
) -> int:
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, start_ms, end_ms, text, speaker),
        )
        return int(cursor.lastrowid)


def _confirm_speaker(cfg: AppConfig, meeting_id: int, diar_id: str, label: str) -> None:
    with connect(cfg.paths.database_path) as conn:
        person_id = int(
            conn.execute(
                "INSERT INTO people (display_name) VALUES (?)", (label,)
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, approved_label, person_id, confirmed_by_user)
            VALUES (?, ?, ?, ?, 1)
            """,
            (meeting_id, diar_id, label, person_id),
        )


def _set_summary_chapters(cfg: AppConfig, meeting_id: int, chapters: list[dict]) -> None:
    payload = {"chapter_markers": chapters, "summary": "", "tldr": ""}
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items (meeting_id, kind, title, payload_json)
            VALUES (?, 'summary', ?, ?)
            """,
            (meeting_id, "Test", json.dumps(payload)),
        )


def _add_decision(
    cfg: AppConfig,
    meeting_id: int,
    *,
    decision: str,
    source_segment_ids: list[int],
) -> None:
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (?, 'decision', ?, ?, ?)
            """,
            (
                meeting_id,
                decision[:80],
                json.dumps({"decision": decision}),
                json.dumps(source_segment_ids),
            ),
        )


def test_empty_meeting_returns_empty(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    # No transcript → no drivers, empty CoG rankings, no standout chip.
    assert drivers == []
    assert cog.rankings == []
    assert cog.standout_speaker_id is None


def test_chapter_intro_credits_first_speaker(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Speaker A opens a chapter; Speakers B and C have ample discussion
    # afterwards (well past the 30s + 2-other-speaker pivot threshold).
    seg_a = _add_segment(cfg, meeting_id, speaker="A",
                         text="Let's talk about the rollout plan.",
                         start_ms=0, end_ms=5_000)
    _add_segment(cfg, meeting_id, speaker="B",
                 text="I think we need to push to next quarter.",
                 start_ms=5_000, end_ms=40_000)
    _add_segment(cfg, meeting_id, speaker="C",
                 text="The infra constraints are the blocker.",
                 start_ms=40_000, end_ms=80_000)
    _confirm_speaker(cfg, meeting_id, "A", "Avery")
    _set_summary_chapters(cfg, meeting_id, [{"label": "Rollout", "start_segment_id": seg_a}])

    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    topic_intros = [d for d in drivers if d.kind == "topic_introduction"]
    assert len(topic_intros) == 1
    intro = topic_intros[0]
    assert intro.segment_id == seg_a
    assert intro.speaker_label == "Avery"
    assert intro.speaker_confirmed is True
    # B and C combined produce well over 30s of follow-on (35s + 40s).
    assert intro.impact_seconds >= 30.0
    # Confirmed speaker + strong signal → high confidence.
    assert intro.confidence == "high"
    # CoG should credit Avery with the one chapter introduction.
    avery_row = next(r for r in cog.rankings if r.speaker_label == "Avery")
    assert avery_row.chapters_introduced == 1


def test_pivot_question_threshold(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Speaker A asks a question. Speakers B and C respond at length →
    # crosses both the seconds and distinct-speakers thresholds.
    q_seg = _add_segment(cfg, meeting_id, speaker="A",
                         text="What is blocking the rollout?",
                         start_ms=0, end_ms=4_000)
    _add_segment(cfg, meeting_id, speaker="B",
                 text="We need stronger integration tests.",
                 start_ms=4_000, end_ms=34_000)
    _add_segment(cfg, meeting_id, speaker="C",
                 text="And the infra team is short-staffed.",
                 start_ms=34_000, end_ms=70_000)
    # Confirm A so the pivot-question driver surfaces in the panel
    # (unconfirmed-speaker drivers are now hidden — see
    # test_unconfirmed_speaker_drivers_are_hidden).
    _confirm_speaker(cfg, meeting_id, "A", "Avery")

    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    pivots = [d for d in drivers if d.kind == "pivot_question"]
    assert len(pivots) == 1
    assert pivots[0].segment_id == q_seg
    # CoG should credit A with the pivot — even though A's words are
    # the smallest share, gravity reflects the impact of the question.
    a_row = next(r for r in cog.rankings if r.speaker_id == "A")
    assert a_row.pivot_questions == 1


def test_pivot_question_skipped_when_no_real_discussion(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # A asks a question; B gives a one-liner. Only one distinct other
    # speaker + short follow-on → should NOT count as a pivot. Filters
    # rhetorical questions and dead-end questions from the panel.
    _add_segment(cfg, meeting_id, speaker="A",
                 text="Are we done?",
                 start_ms=0, end_ms=2_000)
    _add_segment(cfg, meeting_id, speaker="B",
                 text="Yes.",
                 start_ms=2_000, end_ms=4_000)

    drivers, _ = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    pivots = [d for d in drivers if d.kind == "pivot_question"]
    assert pivots == []


def test_decision_moment_display_vs_gravity_credit(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Speaker A seeds an idea at segment 1; B picks it up; C pronounces
    # the decision at segment 3. The decision_moment driver should be
    # anchored to C's pronouncement (latest segment in source_segment_ids)
    # because that's the moment the user wants to find in the transcript.
    # The CoG `decisions_seeded` credit should go to A — the seeder did
    # the harder work of starting the thread.
    s1 = _add_segment(cfg, meeting_id, speaker="A",
                      text="We should phase the launch.",
                      start_ms=0, end_ms=4_000)
    s2 = _add_segment(cfg, meeting_id, speaker="B",
                      text="That makes sense given the infra risk.",
                      start_ms=4_000, end_ms=34_000)
    s3 = _add_segment(cfg, meeting_id, speaker="C",
                      text="Agreed, we'll phase the launch over Q3.",
                      start_ms=34_000, end_ms=70_000)
    _add_decision(
        cfg,
        meeting_id,
        decision="Phase the launch over Q3",
        source_segment_ids=[s1, s2, s3],
    )
    # Confirm C (pronouncer) so the decision_moment driver surfaces.
    # Confirming A separately so the gravity-credit assertion below can
    # match by display name in either reading.
    _confirm_speaker(cfg, meeting_id, "A", "Avery")
    _confirm_speaker(cfg, meeting_id, "C", "Cleo")

    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    decisions = [d for d in drivers if d.kind == "decision_moment"]
    assert len(decisions) == 1
    # Driver anchored to the pronouncement, not the seed.
    assert decisions[0].segment_id == s3
    # Gravity credit goes to the seeder.
    a_row = next(r for r in cog.rankings if r.speaker_id == "A")
    assert a_row.decisions_seeded == 1
    # And not to the pronouncer.
    c_row = next(r for r in cog.rankings if r.speaker_id == "C")
    assert c_row.decisions_seeded == 0


def test_standout_surfaces_low_talk_high_impact_speaker(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Speaker B asks the pivot question that drives the whole meeting,
    # but otherwise stays mostly quiet. A and C carry the talk-time.
    # CoG should flag B as the standout: low talk-time, high gravity.
    q_seg = _add_segment(cfg, meeting_id, speaker="B",
                         text="What's actually blocking the rollout?",
                         start_ms=0, end_ms=4_000)
    # A and C take over discussion for the next ~60s.
    _add_segment(cfg, meeting_id, speaker="A",
                 text=" ".join(["foo"] * 60),
                 start_ms=4_000, end_ms=35_000)
    _add_segment(cfg, meeting_id, speaker="C",
                 text=" ".join(["bar"] * 60),
                 start_ms=35_000, end_ms=70_000)
    # B keeps a low profile after the question — just enough words to
    # show in the ranking.
    _add_segment(cfg, meeting_id, speaker="B",
                 text="Thanks for the context.",
                 start_ms=70_000, end_ms=72_000)
    _confirm_speaker(cfg, meeting_id, "B", "Briar")
    _ = q_seg  # captured for clarity

    drivers, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    # The pivot question fires.
    assert any(d.kind == "pivot_question" for d in drivers)
    # Standout should fire and point at Briar.
    assert cog.standout_speaker_id == "B"
    assert cog.standout_label == "Briar"
    # Reason should mention pivot questions and the percentage.
    assert cog.standout_reason is not None
    assert "pivot" in cog.standout_reason


def test_no_standout_when_top_talker_is_top_driver(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # A dominates talk-time AND introduces the chapter and asks the
    # pivot question. This is the "no surprise" case — no standout chip.
    seg_intro = _add_segment(cfg, meeting_id, speaker="A",
                             text="Let's discuss the roadmap.",
                             start_ms=0, end_ms=4_000)
    _add_segment(cfg, meeting_id, speaker="A",
                 text="What should we prioritise for Q3?",
                 start_ms=4_000, end_ms=8_000)
    _add_segment(cfg, meeting_id, speaker="B",
                 text="I think mobile.",
                 start_ms=8_000, end_ms=40_000)
    _add_segment(cfg, meeting_id, speaker="C",
                 text="Mobile makes sense given the data.",
                 start_ms=40_000, end_ms=80_000)
    _set_summary_chapters(cfg, meeting_id, [{"label": "Roadmap", "start_segment_id": seg_intro}])

    _, cog = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    # A is top by both metrics → no chip.
    assert cog.standout_speaker_id is None


def test_unconfirmed_speaker_drivers_are_hidden(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Same shape as the chapter-intro test but WITHOUT confirming the
    # speaker. The driver must NOT surface — surfacing a "needs speaker
    # review" placeholder pushes attribution work onto the user, which
    # is friction we explicitly don't want. The moment stays hidden
    # until the user confirms the speaker (which invalidates the cache
    # and lets it surface with the correct name).
    seg_a = _add_segment(cfg, meeting_id, speaker="Speaker_001",
                         text="Let's talk about the rollout plan.",
                         start_ms=0, end_ms=5_000)
    _add_segment(cfg, meeting_id, speaker="Speaker_002",
                 text="I think we need to push.",
                 start_ms=5_000, end_ms=40_000)
    _add_segment(cfg, meeting_id, speaker="Speaker_003",
                 text="The infra constraints are the blocker.",
                 start_ms=40_000, end_ms=80_000)
    _set_summary_chapters(cfg, meeting_id, [{"label": "Rollout", "start_segment_id": seg_a}])

    drivers, _ = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    assert all(d.speaker_confirmed for d in drivers), (
        f"Unconfirmed-speaker drivers leaked into the panel: "
        f"{[(d.kind, d.speaker_label) for d in drivers if not d.speaker_confirmed]}"
    )
    # In this fixture, no speakers are confirmed at all, so the panel
    # is empty rather than half-attributed.
    assert drivers == []


def test_drivers_capped_and_chronological(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    meeting_id = _seed_meeting(cfg)
    # Build a long meeting with 10 chapters. The top-_MAX_DRIVERS by
    # impact should win; the survivors should be returned in transcript
    # order (segment_id ascending), not impact order — so the panel
    # reads chronologically.
    chapter_segs: list[dict] = []
    for i in range(10):
        chapter_start = i * 120_000  # 2-min chapters
        seg = _add_segment(cfg, meeting_id, speaker=f"S{i}",
                           text=f"Chapter {i} opener.",
                           start_ms=chapter_start, end_ms=chapter_start + 3_000)
        # Multi-speaker follow-on so each candidate qualifies.
        _add_segment(cfg, meeting_id, speaker=f"X{i}",
                     text="response one " * 10,
                     start_ms=chapter_start + 3_000,
                     end_ms=chapter_start + 35_000)
        _add_segment(cfg, meeting_id, speaker=f"Y{i}",
                     text="response two " * 10,
                     start_ms=chapter_start + 35_000,
                     end_ms=chapter_start + 75_000)
        chapter_segs.append({"label": f"Chapter {i}", "start_segment_id": seg})
        # Confirm each chapter-intro speaker so the cap-test exercises
        # the survivor-selection logic. Without confirmation, the
        # unconfirmed-speaker filter would hide all candidates and the
        # cap behaviour wouldn't be observable.
        _confirm_speaker(cfg, meeting_id, f"S{i}", f"Speaker {i}")
    _set_summary_chapters(cfg, meeting_id, chapter_segs)

    drivers, _ = compute_drivers_and_cog(
        cfg, meeting_id, include_llm_drivers=False, enrich_descriptions=False,
    )
    # Cap enforced.
    assert len(drivers) <= 6
    # Chronological.
    ids = [d.segment_id for d in drivers]
    assert ids == sorted(ids)
