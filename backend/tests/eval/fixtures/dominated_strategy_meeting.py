"""One-voice strategy briefing — Avery monopolizes word share.

Hand-labeled ground truth: Avery owns the conversation start-to-finish.
This is the "no surprise" case for CoG (top talker IS the top driver
of what little discussion happens) — the standout chip should stay off.

Expectations:
  - Participation balance = "dominated".
  - No CoG standout chip (top talker == top driver).
  - Topic introduction credited to Avery.
"""

from __future__ import annotations

from ..harness import (
    ChapterSeed,
    Expectations,
    ExpectedDriver,
    Fixture,
    SegmentSeed,
    SpeakerAssignmentSeed,
)

FIXTURE = Fixture(
    name="dominated_strategy_meeting",
    description=(
        "Avery delivers a strategy briefing with brief acks from two "
        "listeners. Heavy word imbalance; no real discussion."
    ),
    duration_seconds=480.0,
    segments=[
        SegmentSeed(
            "A",
            "Today I want to walk through where we are on the platform "
            "strategy. We've been bouncing between three different framings "
            "and I think it's time to lock one in.",
            0,
            45_000,
        ),
        SegmentSeed(
            "A",
            "The first framing is platform-as-a-product: we build the "
            "underlying APIs, package them, and let internal teams self-serve. "
            "That's where we've been investing for the last six months.",
            45_000,
            105_000,
        ),
        SegmentSeed("B", "Mhm.", 105_000, 106_500),
        SegmentSeed(
            "A",
            "The second framing is platform-as-a-toolkit: we ship libraries "
            "but each team owns their integration. Lower coupling, faster "
            "movement for the teams, but harder to enforce conventions.",
            106_500,
            170_000,
        ),
        SegmentSeed("C", "Got it.", 170_000, 172_000),
        SegmentSeed(
            "A",
            "The third framing — and this is the one I've been gravitating "
            "toward — is platform-as-a-foundation: we provide the runtime "
            "primitives, observability, and security, but let teams build "
            "their own developer experience on top.",
            172_000,
            240_000,
        ),
        SegmentSeed(
            "A",
            "I think we go with foundation. It plays to our strengths, "
            "it doesn't require us to be a product team, and the toolkit "
            "framing is too loose for the compliance requirements coming "
            "down the pipe.",
            240_000,
            305_000,
        ),
        SegmentSeed("B", "Sounds right.", 305_000, 307_000),
        SegmentSeed("C", "Agreed.", 307_000, 308_500),
        SegmentSeed(
            "A",
            "Okay. I'll write this up as an ADR and circulate by end of "
            "week. We can iterate on the rollout plan next meeting.",
            308_500,
            360_000,
        ),
    ],
    chapters=[
        ChapterSeed(
            label="Platform strategy framings",
            start_segment_index=0,
            summary="Avery walks through three framings and lands on foundation.",
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
        ],
        standout_speaker_id=None,  # No surprise — top talker is top driver.
        participation_balance="dominated",
    ),
)
