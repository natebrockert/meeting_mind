"""Synthetic fixture meeting: 'Sample Co — Q3 Roadmap & Hiring Plan'.

The point of this fixture is to give the dashboard a realistic-feeling
meeting to render WITHOUT touching any real user data. Used by
`mm bootstrap-fixture` to seed a fresh install so an agent / new
contributor / curious tester can poke around the UI immediately.

Design notes:
  - All names, products, customers, and decisions are fully invented.
    Speakers: Alex (PM, owner) · Sam (Eng lead) · Riley (Design) ·
    Jordan (Sales) · Casey (Customer Success).
  - Structured to exercise every Mind Map surface:
      * Mixed-importance actions (some owned by Alex / `you`, some
        unassigned) to give the For-You filter something to do.
      * Multiple workstreams of different confidence levels.
      * Open questions raised by different people — including some
        raised by the owner so For-You's "questions you raised"
        section populates.
      * A `(partially_answered)` and a `(deferred)` OQ so the status
        pills render in different states.
      * Stat callouts that are concrete + memorable.
  - Duration ~18 minutes, ~30 segments. Long enough to feel like a
    real meeting; short enough that an agent can load it in seconds.

Reuses the existing `Fixture` / `seed_fixture` harness so the eval
test suite can also exercise it (the bare-meeting fields — segments,
speakers, chapters, decisions — are still valid eval input).
"""

from __future__ import annotations

from ..harness import (
    ActionSeed,
    ChapterSeed,
    DecisionSeed,
    Fixture,
    OpenQuestionSeed,
    SegmentSeed,
    SpeakerAssignmentSeed,
    SummarySeed,
    WorkstreamSeed,
)

# Speaker keys (diarization-style ids). Kept short so the segment list
# below stays readable.
A = "A"  # Alex — PM, owner
S = "S"  # Sam — Eng lead
R = "R"  # Riley — Design
J = "J"  # Jordan — Sales
C = "C"  # Casey — Customer Success


FIXTURE = Fixture(
    name="sample_company_q3_roadmap",
    description=(
        "Synthetic Q3 planning meeting for 'Sample Co'. Five-person team "
        "(Alex/PM, Sam/Eng, Riley/Design, Jordan/Sales, Casey/CS) "
        "discussing roadmap sequencing, hiring, and a renewal-risk "
        "customer. Drives the Mind Map / Minutes / For-You surfaces "
        "with realistic content; touches no real user data."
    ),
    duration_seconds=1080.0,  # ~18 minutes
    speaker_assignments=[
        SpeakerAssignmentSeed(A, "Alex"),
        SpeakerAssignmentSeed(S, "Sam"),
        SpeakerAssignmentSeed(R, "Riley"),
        SpeakerAssignmentSeed(J, "Jordan"),
        SpeakerAssignmentSeed(C, "Casey"),
    ],
    segments=[
        # 0:00 — opening, Alex frames the agenda
        SegmentSeed(A, "Thanks everyone for jumping on. Three things on the agenda: Q3 roadmap sequencing, the QA hiring backfill, and the Northwind renewal. Let's start with roadmap.", 0, 17_000),
        # 0:17 — Sam on engineering capacity
        SegmentSeed(S, "Before we sequence I want to flag that mobile is heavier than we'd estimated. We thought four weeks; it's looking more like six.", 17_000, 30_000),
        SegmentSeed(A, "Six weeks for mobile. That pushes the analytics rollout into late September if we keep the order.", 30_000, 40_000),
        SegmentSeed(R, "Design for analytics is mostly ready — I have one round of polish left. So design isn't the blocker, engineering capacity is.", 40_000, 55_000),
        # 0:55 — Casey raises a customer-visible risk
        SegmentSeed(C, "Can I jump in here? Northwind has been asking specifically about the analytics dashboard for two months. They're up for renewal October fifteenth and they've said it's a factor.", 55_000, 75_000),
        SegmentSeed(A, "How big is the renewal — what's the ARR?", 75_000, 80_000),
        SegmentSeed(J, "Twelve hundred a month. Mid-sized for us but they refer; we've gotten two warm intros from their CTO this year.", 80_000, 95_000),
        # 1:35 — Sam's reframe — the key pivot moment
        SegmentSeed(S, "What if the constraint isn't the order — what if it's QA? We've been short two QA people since June and that's why everything takes longer than estimated.", 95_000, 115_000),
        SegmentSeed(A, "Wait. So the bottleneck isn't engineering capacity, it's QA throughput.", 115_000, 125_000),
        SegmentSeed(S, "Right. If we hire the QA backfill in July, we can run mobile and analytics in parallel — QA is gating both right now.", 125_000, 145_000),
        SegmentSeed(R, "That changes everything. Design and PM aren't blocked either.", 145_000, 155_000),
        # 2:35 — Alex commits to action
        SegmentSeed(A, "Okay. I'll move QA hiring from the parking lot to top priority. Sam, can you draft the role spec by end of week so we can post Monday?", 155_000, 175_000),
        SegmentSeed(S, "Yeah, I can have a spec ready Friday.", 175_000, 180_000),
        # 3:00 — chapter shift: customer
        SegmentSeed(A, "Let me park roadmap for a second and come back to Northwind. Casey, what would they accept short of the full analytics rollout?", 180_000, 198_000),
        SegmentSeed(C, "Honestly, they want to know they're going to get it. If we can show them a roadmap with a real ship date and let them into the beta, I think that holds the renewal.", 198_000, 220_000),
        SegmentSeed(J, "That matches what their CTO told me last call. They don't need it shipped by October — they need to see it's real.", 220_000, 235_000),
        # 3:55 — open question from Alex
        SegmentSeed(A, "So what's the latest we can credibly commit to a beta date? Is mid-October realistic if QA lands in late July?", 235_000, 250_000),
        SegmentSeed(S, "If QA backfill closes by August fifteenth, beta by October tenth is doable. October twentieth is safe.", 250_000, 265_000),
        # 4:25 — Riley raises a design open question (deferred)
        SegmentSeed(R, "One thing — the dashboard navigation IA is still up in the air. Are we shipping the simplified version or the full tree?", 265_000, 280_000),
        SegmentSeed(A, "Let's park that. I'll set up a separate session with you and Sam next week to decide.", 280_000, 290_000),
        # 4:50 — chapter shift: hiring
        SegmentSeed(A, "Okay, hiring. Two QA backfills, role spec by Friday from Sam. Anything blocking us getting heads approved?", 290_000, 305_000),
        SegmentSeed(S, "Finance approved the headcount last quarter; the slots are open. We just need to post and source.", 305_000, 320_000),
        SegmentSeed(J, "I can ask around my network — I've had a few QA folks ping me on LinkedIn this month.", 320_000, 332_000),
        SegmentSeed(A, "That'd help, thanks Jordan.", 332_000, 337_000),
        # 5:37 — Casey raises a follow-up question (partially answered)
        SegmentSeed(C, "What's our story for Northwind in the meantime? Do I tell them October tenth or stay vague?", 337_000, 350_000),
        SegmentSeed(A, "Tell them late October beta, with a real date by mid-July once we have the QA hire signed. Don't promise October tenth yet.", 350_000, 367_000),
        SegmentSeed(C, "Got it. I'll send them an update this week.", 367_000, 375_000),
        # 6:15 — Riley on design polish, low-volume contribution
        SegmentSeed(R, "On my side I'll finish the analytics polish so we're ready when QA opens up. Two-day estimate.", 375_000, 388_000),
        # 6:28 — final action: cross-team check-in
        SegmentSeed(A, "Last thing: let's do a check-in two weeks from now to re-baseline on QA progress and the Northwind status. Same time, same channel.", 388_000, 410_000),
        SegmentSeed(S, "Works.", 410_000, 414_000),
        SegmentSeed(J, "Sounds good.", 414_000, 418_000),
        # 6:58 — Alex closes
        SegmentSeed(A, "Great, thanks all. I'll send a recap with the three commitments by end of day.", 418_000, 430_000),
    ],
    chapters=[
        ChapterSeed("Roadmap & QA bottleneck", 0, "Mobile slipping six weeks → Sam reframes constraint as QA capacity, not engineering."),
        ChapterSeed("Northwind renewal risk", 13, "Casey surfaces customer-visible analytics dependency; team agrees beta + ship-date narrative holds the renewal."),
        ChapterSeed("Hiring & next steps", 20, "QA backfills promoted from parking lot to top priority; check-in scheduled."),
    ],
    decisions=[
        DecisionSeed(
            "QA hiring is now top-priority; Sam writes role spec by Friday.",
            source_segment_indices=[11, 12],
            rationale="QA throughput is the actual bottleneck, not engineering capacity — hiring directly unblocks both mobile and analytics.",
        ),
        DecisionSeed(
            "Commit to a Northwind analytics beta in late October, firm date by mid-July.",
            source_segment_indices=[15, 16, 17],
            rationale="Customer needs visible commitment, not the shipped feature, to hold the October fifteenth renewal.",
        ),
        DecisionSeed(
            "Park the dashboard IA decision until a separate session next week.",
            source_segment_indices=[18, 19],
            rationale="IA scope is out of band for this meeting; needs focused design + eng time, not group discussion.",
        ),
        DecisionSeed(
            "Two-week check-in scheduled to re-baseline QA progress and Northwind status.",
            source_segment_indices=[28],
            rationale="Both threads have time-sensitive dependencies; group needs a forcing function before things slip.",
        ),
    ],
    actions=[
        ActionSeed(
            "Move QA hiring from parking lot to top priority + send commitments recap.",
            source_segment_indices=[11, 29],
            owner_speaker=A,
            due_date="2026-07-17",
            priority="high",
        ),
        ActionSeed(
            "Draft QA role spec for posting Monday.",
            source_segment_indices=[12],
            owner_speaker=S,
            due_date="2026-07-20",
            priority="high",
        ),
        ActionSeed(
            "Send Northwind a written update with the late-October beta framing.",
            source_segment_indices=[26],
            owner_speaker=C,
            due_date="2026-07-24",
        ),
        ActionSeed(
            "Source QA candidates from LinkedIn network.",
            source_segment_indices=[23],
            owner_speaker=J,
        ),
        ActionSeed(
            "Finish analytics dashboard design polish (two-day estimate).",
            source_segment_indices=[27],
            owner_speaker=R,
        ),
        ActionSeed(
            "Schedule a dashboard-IA decision session with Riley and Sam.",
            source_segment_indices=[19],
            owner_speaker=A,
            priority="normal",
        ),
        ActionSeed(
            "Set up the two-week check-in calendar invite.",
            source_segment_indices=[28],
            owner_speaker=A,
        ),
        # An intentionally-unassigned action to exercise the
        # "@unassigned" group in the Mind Map.
        ActionSeed(
            "Decide whether the simplified or full dashboard IA ships.",
            source_segment_indices=[18, 19],
        ),
        # Deliberate near-duplicate of the first action — same owner,
        # paraphrased wording, later in the transcript with a later
        # commitment date. Exercises (a) cluster collapse (member is
        # folded under the canonical "Move QA hiring..." row) and (b)
        # due-date supersession (canonical's stored date becomes the
        # later 2026-07-25 commitment, original 2026-07-17 lands in the
        # due_date_history trail).
        ActionSeed(
            "Move QA hiring to top priority and circulate the commitments recap to the team by Friday.",
            source_segment_indices=[31],
            owner_speaker=A,
            due_date="2026-07-25",
            priority="normal",
        ),
    ],
    open_questions=[
        OpenQuestionSeed(
            "What's the latest beta date we can credibly commit to Northwind?",
            source_segment_indices=[16],
            raised_by_speaker=A,
            status="partially_answered",
        ),
        OpenQuestionSeed(
            "Are we shipping the simplified dashboard IA or the full tree?",
            source_segment_indices=[18],
            raised_by_speaker=R,
            status="deferred",
        ),
        OpenQuestionSeed(
            "What's our story for Northwind in the meantime — firm date or vague?",
            source_segment_indices=[24],
            raised_by_speaker=C,
            status="partially_answered",
        ),
        OpenQuestionSeed(
            "Are there other customers on the same analytics-dependent renewal risk?",
            source_segment_indices=[4, 5],
            raised_by_speaker=A,
            status="unanswered",
        ),
    ],
    workstreams=[
        WorkstreamSeed(
            "QA capacity & hiring",
            "Two QA backfills, role spec by Friday, posting Monday — the gating constraint for both mobile and analytics work.",
            confidence=0.95,
        ),
        WorkstreamSeed(
            "Analytics dashboard launch",
            "Late-October beta target for Northwind retention; depends on QA hires landing in July.",
            confidence=0.88,
        ),
        WorkstreamSeed(
            "Northwind renewal",
            "Hold the October fifteenth renewal with a credible analytics commitment + customer-facing update from Casey.",
            confidence=0.82,
        ),
        WorkstreamSeed(
            "Dashboard information architecture",
            "Simplified vs full-tree IA decision parked to a dedicated session next week.",
            confidence=0.70,
        ),
    ],
    summary=SummarySeed(
        tldr=(
            "Sam reframed the Q3 sequencing problem from engineering capacity "
            "to QA throughput, unlocking parallel mobile + analytics work and "
            "letting the team commit to a late-October Northwind beta."
        ),
        summary=(
            "The team opened on Q3 roadmap sequencing with mobile slipping "
            "from four to six weeks. Sam's reframe — that QA capacity, not "
            "engineering, has been gating every release since June — shifted "
            "the conversation. Alex committed to making QA hiring top "
            "priority; Sam will draft the role spec for posting Monday.\n\n"
            "Casey then surfaced that Northwind, a renewal-risk account, has "
            "been waiting on the analytics dashboard. The group agreed a "
            "visible commitment plus beta access would hold the October "
            "fifteenth renewal — they don't need the feature shipped, they "
            "need to see it's real. Sam committed to a credible October "
            "tenth beta if QA hires land by August fifteenth.\n\n"
            "Closed with a two-week check-in to re-baseline QA progress and "
            "Northwind status. Dashboard IA decision was parked to a "
            "dedicated session."
        ),
        themes=["Roadmap sequencing", "Hiring", "Customer renewal"],
        key_takeaways=[
            "QA throughput, not engineering capacity, has been the actual "
            "bottleneck since June.",
            "Northwind's renewal hinges on a credible analytics commitment, "
            "not the shipped feature.",
            "Two QA backfills move from parking lot to top priority; role "
            "spec by Friday.",
            "Late-October beta is doable if QA hires close by August "
            "fifteenth; October twentieth is the safe date.",
            "Two-week check-in scheduled to re-baseline before anything "
            "slips.",
        ],
        participant_contributions={
            A: (
                "Framed the agenda and drove decisions. Promoted QA hiring "
                "to top priority after Sam's reframe; committed to "
                "owning the late-October Northwind framing and to "
                "sending a recap by end of day. Parked the dashboard IA "
                "decision to a focused session next week."
            ),
            S: (
                "Reframed the meeting's core constraint from engineering "
                "capacity to QA throughput — the highest-leverage moment "
                "of the call. Committed to a role spec by Friday and a "
                "credible October tenth beta date contingent on August "
                "fifteenth hires."
            ),
            R: (
                "Confirmed analytics dashboard design is mostly ready, "
                "two-day polish remaining. Surfaced the open IA decision "
                "and accepted the parking-lot path."
            ),
            J: (
                "Validated Northwind's renewal context with the customer-"
                "side perspective: they need visible commitment, not "
                "shipped feature. Offered to source QA candidates from "
                "LinkedIn network."
            ),
            C: (
                "Surfaced the Northwind renewal risk and negotiated the "
                "customer-facing commitment. Took the action to send a "
                "written update aligning expectations on the late-October "
                "beta framing."
            ),
        },
        stat_callouts=[
            ("6 weeks", "Mobile work estimate (up from 4)"),
            ("$14.4K ARR", "Northwind annual contract value"),
            ("2 hires", "QA backfills from parking lot → top priority"),
            ("Oct 15", "Northwind renewal date"),
        ],
        executive_recap={
            "reframe": {
                "header": "Sam saw a sequencing trap, not a sequencing problem.",
                "body": (
                    "Alex opened with the obvious move: mobile is **six weeks "
                    "instead of four**, so re-sequence analytics to protect "
                    "the late-October Northwind beta. Sam refused the frame. "
                    "*Sequencing only chooses which feature slips — it doesn't "
                    "make either feature faster.* The actual bottleneck wasn't "
                    "engineers writing code; it was **QA capacity gating both "
                    "releases at once.** Two new QA engineers compound across "
                    "mobile and analytics; re-ordering features expands neither. "
                    "The room saw the shift and the agenda collapsed into a "
                    "hiring problem."
                ),
            },
            "strategy": {
                "header": "Every commitment serves the new strategy.",
                "body": (
                    "The plan isn't seven tasks — it's one move (expand QA "
                    "throughput) split across owners, plus a holding pattern "
                    "for the customer while the move lands."
                ),
                "bullets": [
                    {
                        "owner": "Sam",
                        "commitment": "QA role spec by Monday",
                        "purpose": "fastest path to a posted req",
                    },
                    {
                        "owner": "Jordan",
                        "commitment": "LinkedIn outreach for QA candidates",
                        "purpose": "parallel pipeline",
                    },
                    {
                        "owner": "Casey",
                        "commitment": "Northwind written update this week with the late-October framing",
                        "purpose": "buys the time",
                    },
                    {
                        "owner": "Riley",
                        "commitment": "Analytics polish on a two-day estimate",
                        "purpose": "ready to consume new capacity",
                    },
                    {
                        "owner": "Alex",
                        "commitment": "Three-commitment recap by EOD; two-week check-in scheduled",
                        "purpose": "keeps the plan from drifting",
                    },
                ],
                "trailer": (
                    "The dashboard IA debate was pulled to a dedicated "
                    "session next week — Alex's call to keep this meeting on "
                    "the live problem."
                ),
            },
            "risk": {
                "header": "Open risk",
                "body": (
                    "The plan depends on a **QA hire signed inside three "
                    "weeks**. If that slips, the late-October Northwind "
                    "framing is at risk, and the mid-July firm date Casey "
                    "will now go promise becomes the next pressure point. "
                    "This risk was not explicitly discussed in the meeting."
                ),
            },
        },
    ),
)
