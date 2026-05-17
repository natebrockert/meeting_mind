"""Eval harness: seed a fixture, run pipeline steps, compare to expected.

Mirrors the pattern of the unit tests in backend/tests/test_*.py
(each test creates its own AppConfig + SQLite DB via a tmp_path fixture)
but takes a richer "Fixture" object so a single fixture can drive many
test assertions.

LLM-touching paths are off by default. Set MEETINGMIND_EVAL_REAL_LLM=1
to enable real LLM calls during eval — used during prompt iterations,
not on every CI run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database

REAL_LLM_ENV_FLAG = "MEETINGMIND_EVAL_REAL_LLM"


def real_llm_enabled() -> bool:
    """True iff the operator wants LLM-touching eval steps to run."""
    return os.environ.get(REAL_LLM_ENV_FLAG, "").strip() in {"1", "true", "yes"}


@dataclass(frozen=True)
class SegmentSeed:
    speaker: str
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ChapterSeed:
    label: str
    # Index into the fixture's `segments` list — resolved to a real
    # segment_id at seed time. Indices are cleaner than ids because they
    # survive fixture edits.
    start_segment_index: int
    summary: str = ""


@dataclass(frozen=True)
class DecisionSeed:
    title: str
    # Indices into `segments`. Earliest is treated as the seed segment.
    source_segment_indices: list[int]
    rationale: str | None = None


@dataclass(frozen=True)
class ActionSeed:
    """A pre-extracted action item. `owner_speaker` is one of the
    fixture's diarization speaker ids — resolved to a `person_id` at
    seed time so the For-You filter and ownership UI behave like a
    real meeting. Leave it `None` for an unassigned action.
    """

    text: str
    source_segment_indices: list[int]
    owner_speaker: str | None = None
    due_date: str | None = None
    priority: str = "normal"


@dataclass(frozen=True)
class OpenQuestionSeed:
    """A pre-extracted open question. `raised_by_speaker` is one of the
    fixture's diarization speaker ids; resolved to a display name at
    seed time so the For-You "open questions you raised" filter has
    something to match.
    """

    question: str
    source_segment_indices: list[int]
    raised_by_speaker: str | None = None
    status: str = "unanswered"  # unanswered | partially_answered | deferred


@dataclass(frozen=True)
class WorkstreamSeed:
    title: str
    description: str = ""
    confidence: float = 0.85


@dataclass(frozen=True)
class SummarySeed:
    """The atoms-shaped summary payload baked into the fixture. Lets a
    seeded fixture render the full Mind Map / Minutes / Reflections
    flow without requiring a live LLM extraction call.
    """

    tldr: str
    summary: str
    themes: list[str] = field(default_factory=list)
    key_takeaways: list[str] = field(default_factory=list)
    # Per-speaker contributions for the Minutes "By person" view.
    # Speaker keys are diarization ids; resolved to display names at
    # seed time.
    participant_contributions: dict[str, str] = field(default_factory=dict)
    # Optional: stat callouts ("$2M ARR", "92% retention") shown as
    # tiles on the Mind Map. Each entry is (value, label).
    stat_callouts: list[tuple[str, str]] = field(default_factory=list)
    # Optional pre-baked executive recap. When present, seed_fixture
    # writes it as the executive_recap review_items row so the dashboard
    # renders the full three-section recap without a live LLM call.
    # Shape mirrors backend.app.services.extraction.ExecutiveRecap:
    #   {"reframe": {"header": str, "body": str},
    #    "strategy": {"header": str, "body": str,
    #                 "bullets": [{"owner": str, "commitment": str,
    #                              "purpose": str | None}],
    #                 "trailer": str | None},
    #    "risk": {"header": str | None, "body": str | None}}
    executive_recap: dict | None = None


@dataclass(frozen=True)
class SpeakerAssignmentSeed:
    diarization_speaker_id: str
    display_name: str


@dataclass(frozen=True)
class ExpectedDriver:
    kind: str
    # Index into `segments` of the moment we expect surfaced. None means
    # "any driver of this kind is fine — we don't pin to a specific seg."
    segment_index: int | None = None


@dataclass(frozen=True)
class Expectations:
    drivers: list[ExpectedDriver] = field(default_factory=list)
    # The speaker we expect to surface as the CoG standout, or None when
    # the "no surprise" case should apply (top talker is also top driver
    # → no standout chip).
    standout_speaker_id: str | None = None
    # Bucket assertions on Meeting Health.
    participation_balance: str | None = None
    decision_density: str | None = None
    action_clarity: str | None = None


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    duration_seconds: float
    segments: list[SegmentSeed]
    chapters: list[ChapterSeed] = field(default_factory=list)
    decisions: list[DecisionSeed] = field(default_factory=list)
    speaker_assignments: list[SpeakerAssignmentSeed] = field(default_factory=list)
    expectations: Expectations = field(default_factory=Expectations)
    # Below: optional baked-in extraction output so a fixture can render
    # as a fully-processed meeting in the dashboard without requiring a
    # live LLM call. `mm bootstrap-fixture` consumes these to seed an
    # extracted-state install in one shot.
    actions: list[ActionSeed] = field(default_factory=list)
    open_questions: list[OpenQuestionSeed] = field(default_factory=list)
    workstreams: list[WorkstreamSeed] = field(default_factory=list)
    # When present, used in place of the chapter-only `summary` payload
    # so the Mind Map renders TL;DR, themes, stat callouts, etc.
    summary: SummarySeed | None = None


def make_test_config(tmp_path: Path) -> AppConfig:
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


def seed_fixture(cfg: AppConfig, fixture: Fixture) -> tuple[int, list[int]]:
    """Seed the fixture into a fresh DB and return (meeting_id, segment_ids).

    `segment_ids[i]` is the persisted SQLite id for `fixture.segments[i]`,
    so callers can resolve `ExpectedDriver.segment_index` → real id.
    """
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fixture.name,
                f"eval-{fixture.name}",
                "fixture.m4a",
                f"p/eval-{fixture.name}.m4a",
                fixture.duration_seconds,
                "transcribed",
            ),
        )
        meeting_id = int(cursor.lastrowid)

        segment_ids: list[int] = []
        for seg in fixture.segments:
            c = conn.execute(
                """
                INSERT INTO transcript_segments
                  (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (meeting_id, seg.start_ms, seg.end_ms, seg.text, seg.speaker),
            )
            segment_ids.append(int(c.lastrowid))

        for assignment in fixture.speaker_assignments:
            person_id = int(
                conn.execute(
                    "INSERT INTO people (display_name) VALUES (?)",
                    (assignment.display_name,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO speaker_assignments
                  (meeting_id, diarization_speaker_id, approved_label,
                   person_id, confirmed_by_user)
                VALUES (?, ?, ?, ?, 1)
                """,
                (
                    meeting_id,
                    assignment.diarization_speaker_id,
                    assignment.display_name,
                    person_id,
                ),
            )

        # Map diarization id → person_id so action / open-question
        # seeds can resolve their owner / raised_by references to the
        # person rows we just inserted.
        person_id_by_speaker: dict[str, int] = {}
        for assignment in fixture.speaker_assignments:
            row = conn.execute(
                "SELECT id FROM people WHERE display_name = ?",
                (assignment.display_name,),
            ).fetchone()
            if row is not None:
                person_id_by_speaker[assignment.diarization_speaker_id] = int(row["id"])

        # Single summary row: either the rich SummarySeed (TLDR, themes,
        # contributions, stats) or the chapter-only fallback that the
        # original `seed_fixture` produced.
        summary_payload: dict[str, Any] | None = None
        if fixture.summary is not None:
            contributions = []
            for sid, body in fixture.summary.participant_contributions.items():
                display_name = next(
                    (a.display_name for a in fixture.speaker_assignments
                     if a.diarization_speaker_id == sid),
                    sid,
                )
                contributions.append({
                    "speaker": display_name,
                    "contribution": body,
                    "source_segment_ids": [],
                })
            summary_payload = {
                "tldr": fixture.summary.tldr,
                "summary": fixture.summary.summary,
                "themes": list(fixture.summary.themes),
                "key_takeaways": list(fixture.summary.key_takeaways),
                "participant_contributions": contributions,
                "stat_callouts": [
                    {"value": v, "label": label, "source_segment_ids": []}
                    for v, label in fixture.summary.stat_callouts
                ],
                "chapter_markers": [
                    {
                        "label": c.label,
                        "start_segment_id": segment_ids[c.start_segment_index],
                        **({"summary": c.summary} if c.summary else {}),
                    }
                    for c in fixture.chapters
                ],
            }
        elif fixture.chapters:
            summary_payload = {
                "chapter_markers": [
                    {
                        "label": c.label,
                        "start_segment_id": segment_ids[c.start_segment_index],
                        **({"summary": c.summary} if c.summary else {}),
                    }
                    for c in fixture.chapters
                ],
                "summary": "",
                "tldr": "",
            }

        if summary_payload is not None:
            conn.execute(
                """
                INSERT INTO review_items (meeting_id, kind, title, payload_json)
                VALUES (?, 'summary', ?, ?)
                """,
                (meeting_id, fixture.name, json.dumps(summary_payload)),
            )
            if fixture.summary is not None and fixture.summary.executive_recap is not None:
                conn.execute(
                    """
                    INSERT INTO review_items (meeting_id, kind, title, payload_json)
                    VALUES (?, 'executive_recap', ?, ?)
                    """,
                    (
                        meeting_id,
                        fixture.name[:120],
                        json.dumps(fixture.summary.executive_recap),
                    ),
                )

        for decision in fixture.decisions:
            seg_id_list = [segment_ids[i] for i in decision.source_segment_indices]
            payload = {"decision": decision.title}
            if decision.rationale:
                payload["rationale"] = decision.rationale
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, source_segment_ids)
                VALUES (?, 'decision', ?, ?, ?)
                """,
                (
                    meeting_id,
                    decision.title[:80],
                    json.dumps(payload),
                    json.dumps(seg_id_list),
                ),
            )

        # Mirror production: apply the same clustering pass extraction.py
        # runs so seeded fixtures expose canonicals + members + cluster
        # metadata exactly like a real extracted meeting.
        from app.services.extraction import (
            ExtractedAction,
            _cluster_actions,
            _resolve_cluster_due_date,
        )

        seed_extracted = [
            ExtractedAction(
                task=a.text,
                owner=a.owner_speaker,
                due_date=a.due_date,
                priority=a.priority,
                source_segment_ids=[
                    segment_ids[i] for i in a.source_segment_indices
                ],
            )
            for a in fixture.actions
        ]
        seed_owner_ids = [
            person_id_by_speaker.get(a.owner_speaker) if a.owner_speaker else None
            for a in fixture.actions
        ]
        clusters = _cluster_actions(seed_extracted, seed_owner_ids)
        for cluster in clusters:
            if len(cluster) == 1:
                idx = cluster[0]
                action = seed_extracted[idx]
                conn.execute(
                    """
                    INSERT INTO action_items
                      (meeting_id, owner_person_id, text, due_date,
                       due_date_source, priority, source_segment_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        seed_owner_ids[idx],
                        action.task,
                        action.due_date,
                        action.due_date_source,
                        action.priority,
                        json.dumps(action.source_segment_ids),
                    ),
                )
                continue
            canon_idx = cluster[0]
            canonical = seed_extracted[canon_idx]
            member_actions = [seed_extracted[i] for i in cluster]
            merged_segs: list[int] = []
            for a in member_actions:
                for seg in a.source_segment_ids:
                    if seg not in merged_segs:
                        merged_segs.append(seg)
            due_date, due_src, due_history = _resolve_cluster_due_date(member_actions)
            cursor = conn.execute(
                """
                INSERT INTO action_items
                  (meeting_id, owner_person_id, text, due_date,
                   due_date_source, priority, source_segment_ids, cluster_role)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'canonical')
                """,
                (
                    meeting_id,
                    seed_owner_ids[canon_idx],
                    canonical.task,
                    due_date,
                    due_src,
                    canonical.priority,
                    json.dumps(merged_segs),
                ),
            )
            canonical_id = int(cursor.lastrowid)
            for member_idx in cluster[1:]:
                member = seed_extracted[member_idx]
                conn.execute(
                    """
                    INSERT INTO action_items
                      (meeting_id, owner_person_id, text, due_date,
                       due_date_source, priority, source_segment_ids,
                       cluster_id, cluster_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'member')
                    """,
                    (
                        meeting_id,
                        seed_owner_ids[member_idx],
                        member.task,
                        member.due_date,
                        member.due_date_source,
                        member.priority,
                        json.dumps(member.source_segment_ids),
                        canonical_id,
                    ),
                )
            if due_history:
                conn.execute(
                    """
                    INSERT INTO review_items
                      (meeting_id, kind, title, payload_json)
                    VALUES (?, 'action_cluster_meta', ?, ?)
                    """,
                    (
                        meeting_id,
                        f"action:{canonical_id}",
                        json.dumps({"due_date_history": due_history}),
                    ),
                )

        for open_q in fixture.open_questions:
            seg_id_list = [segment_ids[i] for i in open_q.source_segment_indices]
            raised_by_name = None
            if open_q.raised_by_speaker:
                raised_by_name = next(
                    (a.display_name for a in fixture.speaker_assignments
                     if a.diarization_speaker_id == open_q.raised_by_speaker),
                    None,
                )
            payload = {
                "text": open_q.question,
                "status": open_q.status,
                **({"raised_by": raised_by_name} if raised_by_name else {}),
            }
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, source_segment_ids)
                VALUES (?, 'open_question', ?, ?, ?)
                """,
                (
                    meeting_id,
                    open_q.question[:120],
                    json.dumps(payload),
                    json.dumps(seg_id_list),
                ),
            )

        for workstream in fixture.workstreams:
            payload = {
                "description": workstream.description,
                "confidence": workstream.confidence,
            }
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence)
                VALUES (?, 'workstream', ?, ?, ?)
                """,
                (
                    meeting_id,
                    workstream.title[:80],
                    json.dumps(payload),
                    workstream.confidence,
                ),
            )

        # Mark the meeting as fully extracted so the dashboard renders
        # the full Mind Map / Minutes / Transcript flow rather than a
        # "review pending" placeholder.
        if fixture.summary is not None or fixture.actions or fixture.decisions:
            conn.execute(
                "UPDATE meetings SET status = 'extracted' WHERE id = ?",
                (meeting_id,),
            )

    return meeting_id, segment_ids
