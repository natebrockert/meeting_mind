"""A meeting with three decisions seeded by different people.

Hand-labeled ground truth: decisions on rollout cadence, oncall
rotation, and incident review come from three different speakers.
Tests that decision_moment drivers attribute correctly to the *seeder*,
not just whoever pronounced the decision.

Expectations:
  - 3 decision_moment drivers (one per decision, pinned to seed segments).
  - decision_density = "high" (3 decisions in a 25-minute meeting).
  - Participation balance = "balanced" or "skewed" (depends on word
    distribution — left flexible because that's not what this fixture
    is testing).
"""

from __future__ import annotations

from ..harness import (
    DecisionSeed,
    Expectations,
    ExpectedDriver,
    Fixture,
    SegmentSeed,
    SpeakerAssignmentSeed,
)

FIXTURE = Fixture(
    name="decision_heavy_meeting",
    description=(
        "Ops team works through three small decisions: rollout cadence, "
        "oncall rotation, and incident review process."
    ),
    duration_seconds=1500.0,  # 25 min
    segments=[
        # === Decision 1: rollout cadence — seeded by A (idx 0) ===
        SegmentSeed(
            "A",
            "I think we should move from weekly rollouts to every two weeks. "
            "The hotfix overhead is eating our planning time.",
            0,
            22_000,
        ),
        SegmentSeed(
            "B",
            "Two weeks means slower bug-fix turnaround though. Are we sure "
            "the hotfix process is that broken?",
            22_000,
            55_000,
        ),
        SegmentSeed(
            "C",
            "From an SRE side, fewer rollouts means tighter monitoring "
            "windows. I'd support it if we can guarantee hotfix path stays "
            "fast for sev-1s.",
            55_000,
            105_000,
        ),
        SegmentSeed(
            "A",
            "We can keep an emergency rollout track for sev-1s. Two-week "
            "cadence for everything else.",
            105_000,
            145_000,
        ),
        SegmentSeed(
            "B",
            "Okay, that addresses my concern. Two-week cadence with sev-1 "
            "emergency path.",
            145_000,
            175_000,
        ),
        # === Decision 2: oncall rotation — seeded by C (idx 5) ===
        SegmentSeed(
            "C",
            "While we're talking ops — can we revisit the oncall rotation? "
            "We're at 1-week shifts and the burnout signals are clear.",
            175_000,
            225_000,
        ),
        SegmentSeed(
            "A",
            "What were you thinking — shorter shifts? Bigger pool?",
            225_000,
            245_000,
        ),
        SegmentSeed(
            "C",
            "Both. Move to 3-day shifts and add the platform team to the "
            "rotation. They want the experience anyway.",
            245_000,
            290_000,
        ),
        SegmentSeed(
            "B",
            "Platform team has been asking. I think this is the right call.",
            290_000,
            315_000,
        ),
        SegmentSeed(
            "A", "Agreed. Three-day shifts, platform team joins.", 315_000, 340_000
        ),
        # === Decision 3: incident review — seeded by B (idx 10) ===
        SegmentSeed(
            "B",
            "One more thing: incident reviews. We're skipping them when "
            "things calm down and then forgetting the lessons. Can we "
            "lock in a regular slot?",
            340_000,
            395_000,
        ),
        SegmentSeed(
            "A",
            "Yeah, that's been bugging me too. What if we make it part of "
            "the biweekly retro?",
            395_000,
            430_000,
        ),
        SegmentSeed(
            "C",
            "Tying it to retro is good — gives it a forcing function.",
            430_000,
            460_000,
        ),
        SegmentSeed(
            "B",
            "Done. Incident reviews appended to the biweekly retro agenda, "
            "with a rolling 'unresolved' list.",
            460_000,
            510_000,
        ),
    ],
    decisions=[
        DecisionSeed(
            title="Move to two-week rollout cadence with sev-1 emergency path",
            source_segment_indices=[0, 2, 3, 4],  # A seeded at idx 0
        ),
        DecisionSeed(
            title="Move to 3-day oncall shifts, add platform team",
            source_segment_indices=[5, 7, 9],  # C seeded at idx 5
        ),
        DecisionSeed(
            title="Lock incident reviews into biweekly retro",
            source_segment_indices=[10, 11, 13],  # B seeded at idx 10
        ),
    ],
    speaker_assignments=[
        SpeakerAssignmentSeed("A", "Avery"),
        SpeakerAssignmentSeed("B", "Briar"),
        SpeakerAssignmentSeed("C", "Cleo"),
    ],
    expectations=Expectations(
        drivers=[
            # decision_moment driver is anchored to the *pronouncement*
            # segment (max of source_segment_indices), not the seed.
            # CoG gravity credit still goes to the seeder — see
            # test_decision_moment_display_vs_gravity_credit.
            ExpectedDriver(kind="decision_moment", segment_index=4),
            ExpectedDriver(kind="decision_moment", segment_index=9),
            ExpectedDriver(kind="decision_moment", segment_index=13),
        ],
        decision_density="high",
    ),
)
