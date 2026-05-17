"""A meeting where a low-talk-time speaker asks the pivot question.

Hand-labeled ground truth: Briar (Speaker B) speaks ~12% of words but
asks the question that drives the next 4 minutes of discussion. This is
exactly the "low-talk, high-impact" standout case the CoG chip is
designed to surface.

Expectations:
  - At least one `pivot_question` driver pinned to segment index 4
    (Briar's question).
  - At least one `topic_introduction` driver pinned to segment 0
    (the chapter opener).
  - CoG standout = "B" (Briar). The chip should fire.
  - Participation balance = "skewed" (Avery dominates but Cleo and
    Briar are real participants).
"""

from __future__ import annotations

from ..harness import (
    ChapterSeed,
    DecisionSeed,
    Expectations,
    ExpectedDriver,
    Fixture,
    SegmentSeed,
    SpeakerAssignmentSeed,
)

FIXTURE = Fixture(
    name="pivot_question_meeting",
    description=(
        "Avery leads roadmap discussion. Briar (mostly quiet) asks the key "
        "question that reframes the conversation around QA capacity. Cleo "
        "and Avery carry the response."
    ),
    duration_seconds=600.0,
    segments=[
        SegmentSeed("A", "Let's walk through the Q3 roadmap.", 0, 8_000),
        SegmentSeed(
            "A",
            "I'm thinking we focus on the mobile rewrite, the auth refresh, "
            "and the analytics rollout. The mobile work is the heaviest lift "
            "and we'll need to start it in July to hit September launch.",
            8_000,
            55_000,
        ),
        SegmentSeed(
            "C",
            "The mobile dependencies are solid. We've done the spike on the "
            "new framework and the platform team is ready.",
            55_000,
            85_000,
        ),
        SegmentSeed(
            "A",
            "Good. So we slot mobile in July, auth in August, analytics in "
            "September. Anything blocking that we haven't talked about?",
            85_000,
            120_000,
        ),
        SegmentSeed(
            "B",
            "What's actually blocking the analytics rollout — is it really "
            "engineering or is it QA capacity?",
            120_000,
            134_000,
        ),
        SegmentSeed(
            "C",
            "Oh — that's a good point. QA is short two people right now "
            "and we haven't talked about that at all.",
            134_000,
            175_000,
        ),
        SegmentSeed(
            "A",
            "Hmm. If QA is the actual bottleneck then sequencing won't help. "
            "We'd need to either hire or reduce scope.",
            175_000,
            220_000,
        ),
        SegmentSeed(
            "C",
            "I think we look at scope first. Mobile is doable with current "
            "QA if we cut the analytics rollout to a smaller pilot.",
            220_000,
            265_000,
        ),
        SegmentSeed(
            "A",
            "Right. Let's say we do mobile full, auth full, analytics as a "
            "20-user pilot. That keeps Q3 honest without overcommitting QA.",
            265_000,
            310_000,
        ),
        SegmentSeed(
            "B", "That works for me.", 310_000, 314_000
        ),
        SegmentSeed(
            "C",
            "Agreed. I'll write up the scope cut and circulate.",
            314_000,
            340_000,
        ),
    ],
    chapters=[
        ChapterSeed(
            label="Q3 roadmap walkthrough",
            start_segment_index=0,
            summary="Avery walks through Q3 roadmap: mobile, auth, analytics.",
        ),
        ChapterSeed(
            label="QA capacity check",
            start_segment_index=4,
            summary="Briar surfaces QA as the actual bottleneck.",
        ),
    ],
    decisions=[
        DecisionSeed(
            title="Cut analytics rollout to a 20-user pilot",
            # Briar's pivot question seeded it; Avery announced it.
            source_segment_indices=[4, 6, 8],
            rationale="QA capacity is the real constraint, not engineering",
        ),
    ],
    speaker_assignments=[
        SpeakerAssignmentSeed("A", "Avery"),
        SpeakerAssignmentSeed("B", "Briar"),
        SpeakerAssignmentSeed("C", "Cleo"),
    ],
    expectations=Expectations(
        drivers=[
            ExpectedDriver(kind="topic_introduction", segment_index=0),
            ExpectedDriver(kind="pivot_question", segment_index=4),
            ExpectedDriver(kind="decision_moment"),  # any decision moment
        ],
        standout_speaker_id="B",
        participation_balance="skewed",
    ),
)
