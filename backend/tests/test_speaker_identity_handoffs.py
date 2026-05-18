"""v0.2.10: panel-discussion handoff patterns for speaker identification.

The original `speaker_identity._DIRECT_ADDRESS_PATTERNS` only caught
`"question for {Name}"` and `"thanks {Name}"` with the name immediately
following the trigger. That misses three patterns that show up
constantly in moderated panels:

  1. "Question for you, Paul"          — filler ("you,") between
                                          trigger and name
  2. "Janet, thank you for that..."    — vocative-first ordering;
                                          the name addresses the
                                          PREVIOUS speaker
  3. "Pat Smith. He serves as..."   — host introducing a panelist;
                                          should mark "Paul" as a known
                                          panelist name and boost any
                                          later direct-address candidate
                                          for that name

Plus a stop-word regression: "I'm sitting on the panel" was generating
a fake `Sitting` self-introduction candidate.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from app.config import AppConfig, AsrConfig, DiarizationConfig, PathConfig, ReviewConfig
from app.db.database import initialize_database
from app.services.speaker_identity import persist_speaker_name_candidates


def _cfg(tmp_path: Path) -> AppConfig:
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
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        review=ReviewConfig(transcript_uncertainty_threshold=0.5),
    )
    initialize_database(paths.database_path)
    with sqlite3.connect(paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'Demo', 'demo', '/dev/null', '/dev/null', 60, 'complete')
            """
        )
    return cfg


def _seed(cfg: AppConfig, rows: list[tuple]) -> None:
    """rows: (id, start_ms, end_ms, text, diarization_speaker_id)."""
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 1, ?, ?, ?, ?)
            """,
            rows,
        )


def _candidates(cfg: AppConfig) -> list[tuple[str, str, float, str]]:
    """Return (speaker_id, candidate_name, confidence, evidence_types) for
    every open speaker_name_candidate review item."""
    persist_speaker_name_candidates(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT payload_json, confidence
            FROM review_items
            WHERE kind = 'speaker_name_candidate' AND status = 'open'
            ORDER BY id
            """
        ).fetchall()
    out = []
    for payload_json, confidence in rows:
        payload = json.loads(payload_json)
        kinds = ",".join(sorted({e["evidence_type"] for e in payload["evidence"]}))
        out.append((payload["speaker_id"], payload["candidate_name"], confidence, kinds))
    return out


def test_question_for_you_with_filler_binds_to_next_speaker(tmp_path: Path) -> None:
    """'Question for you, Paul' should bind name=Paul to whichever
    speaker responds next, despite the 'you,' filler between trigger
    and name. Pre-v0.2.10 the regex required the name immediately after
    'for', so this pattern was silently missed.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Question for you, Paul.", "Speaker 2"),
            (2, 5500, 10000, "Well, I think we're in a bubble.", "Speaker 4"),
        ],
    )
    candidates = _candidates(cfg)
    paul = [c for c in candidates if c[1] == "Paul"]
    assert paul, f"expected Paul candidate, got: {candidates}"
    speaker_id, _name, _conf, kinds = paul[0]
    assert speaker_id == "Speaker 4"
    assert "response_after_direct_address" in kinds


def test_vocative_first_thanks_binds_to_previous_speaker(tmp_path: Path) -> None:
    """'Janet, thank you for that wonderful introduction' addresses the
    PREVIOUS speaker (Janet just spoke), not the next one. New
    evidence_type 'vocative_thank' captures the reversed direction.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Welcome to the panel everyone, let's begin.", "Speaker 1"),
            (2, 5500, 10000, "Janet, thank you for that wonderful introduction.", "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    janet = [c for c in candidates if c[1] == "Janet"]
    assert janet, f"expected Janet candidate, got: {candidates}"
    speaker_id, _name, conf, kinds = janet[0]
    assert speaker_id == "Speaker 1"
    assert "vocative_thank" in kinds
    assert conf >= 0.7  # vocative_thank base


def test_host_intro_boosts_direct_address_panelist(tmp_path: Path) -> None:
    """Host introduces 'Pat Smith' early in the meeting. Later, a
    different speaker says 'Question for Pat' — because Paul was
    introduced by the host, this gets a higher base confidence than a
    bare direct-address (0.68 vs 0.58).
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 8000, "We have a great panel today. Pat Smith. He serves as research professor at City University.", "Speaker 1"),
            (2, 9000, 14000, "Now let me bring in our second guest.", "Speaker 1"),
            (3, 14500, 19000, "Question for Pat on the topic.", "Speaker 2"),
            (4, 19500, 24000, "Well, I think we are witnessing a bubble.", "Speaker 4"),
        ],
    )
    candidates = _candidates(cfg)
    pat = [c for c in candidates if c[1] == "Pat"]
    assert pat, f"expected Pat candidate, got: {candidates}"
    speaker_id, _name, conf, kinds = pat[0]
    assert speaker_id == "Speaker 4"
    assert "response_after_direct_address_panelist" in kinds
    # With the panelist boost, base is 0.68 (vs 0.58 bare).
    assert conf >= 0.68


@pytest.mark.parametrize(
    "phrase",
    [
        # v0.2.10 round 2: false-positives observed on real healthcare-
        # meeting transcript that the first STOP_NAMES pass missed.
        "I'm not sure about that.",
        "I'm just thinking out loud.",
        "I'm hearing a lot of noise.",
        "I am good, thanks.",
        "I'm sort of leaning that way.",
        "I'm losing my train of thought.",
        "I'm well aware of that.",
    ],
)
def test_common_filler_doesnt_generate_fake_intro(
    tmp_path: Path, phrase: str
) -> None:
    """Regression for v0.2.10 round 2 STOP_NAMES additions. These
    'I'm <filler>' patterns generated bogus self-introduction
    candidates on the healthcare-meeting transcript despite the
    Phase 1 STOP_NAMES list. The fix extends the list with the
    remaining short adverbs / progressive verbs / quality
    adjectives that appear after 'I'm' / 'I am'.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, phrase, "Speaker 1"),
            (2, 5500, 10000, "ok, got it.", "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    assert not candidates, f"phrase {phrase!r} produced bogus: {candidates}"


# ── v0.2.12: business-meeting vocative patterns ─────────────────────────
#
# These tests cover the 28 name mentions across 9 distinct names that the
# v0.2.10 panel-tuned patterns missed on a real business-strategy meeting.
# Each test reproduces a phrase observed in that transcript.


@pytest.mark.parametrize(
    "phrase,expected_name,reply",
    [
        # "Alex, I don't know if you've done this" — vocative + question
        ("Alex, I don't know if you've done this.", "Alex", "Yeah, I have."),
        # Lowercase name (ASR uncapitalized)
        ("becky, I have a question for you.", "Becky", "Sure, ask away."),
        # Vocative + you-pronoun
        ("Scott, you also have a stake in this.", "Scott", "Yes I do."),
        # Vocative + are
        ("Brett, are you from Minneapolis originally?", "Brett", "Yes I am."),
        # Vocative + tell/let/please imperative
        ("Pam, tell us what you think.", "Pam", "I think..."),
    ],
)
def test_vocative_then_question_binds_to_next_speaker(
    tmp_path: Path, phrase: str, expected_name: str, reply: str
) -> None:
    """v0.2.12: vocative-then-question is the dominant address pattern in
    casual business meetings. The name addresses the NEXT speaker who
    responds (standard direct-address binding).
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, phrase, "Speaker 1"),
            (2, 5500, 10000, reply, "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    hits = [c for c in candidates if c[1].lower() == expected_name.lower()]
    assert hits, f"phrase {phrase!r}: expected {expected_name}, got {candidates}"
    assert hits[0][0] == "Speaker 2"


def test_embedded_vocative_after_you_know(tmp_path: Path) -> None:
    """'you know scott i was telling him that...' — the name follows a
    conversational filler. The next different speaker is the addressee.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "you know scott i was telling him that we should look at this.", "Speaker 3"),
            (2, 5500, 10000, "Right, I remember that.", "Speaker 1"),
        ],
    )
    candidates = _candidates(cfg)
    scott = [c for c in candidates if c[1] == "Scott"]
    assert scott, f"expected Scott candidate, got: {candidates}"
    assert scott[0][0] == "Speaker 1"


def test_oh_hey_brent_thanks_for_joining(tmp_path: Path) -> None:
    """Vocative-thank with a hey/oh prefix: 'Oh, hey, Brent, thanks for joining.'
    The anchor must accept hey/oh prefixes before the name, not just
    sentence-terminal punctuation.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Brent's segment.", "Speaker 5"),
            (2, 5500, 10000, "Oh, hey, Brent, thanks for joining.", "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    brent = [c for c in candidates if c[1] == "Brent"]
    assert brent, f"expected Brent candidate, got: {candidates}"
    # Vocative-thank addresses the PREVIOUS speaker.
    assert brent[0][0] == "Speaker 5"


def test_host_intro_without_period_caught(tmp_path: Path) -> None:
    """'Pat Smith is charged with finding the right markets.' — host-
    intro pattern without a period between first and last name. The
    expanded pattern keys on identity-style verbs ('is charged', 'is the
    lead', etc.) so it doesn't match generic 'X County is a...' geos.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "something that Pat Smith is charged with finding.", "Speaker 4"),
            (2, 5500, 10000, "Great I'll talk with Arthi tomorrow.", "Speaker 1"),
            (3, 11000, 15000, "Question for Arthi on the timeline.", "Speaker 1"),
            (4, 15500, 20000, "Sure happy to discuss.", "Speaker 6"),
        ],
    )
    candidates = _candidates(cfg)
    arthi = [c for c in candidates if c[1] == "Arthi"]
    # Should bind via the panelist boost when the host-intro pool kicks
    # in on the subsequent direct-address.
    assert arthi, f"expected Arthi candidate, got: {candidates}"


def test_clark_county_is_not_caught_as_host_intro(tmp_path: Path) -> None:
    """Critical guard: 'Clark County is a great market' must NOT match
    the host-intro pattern. The expanded verb list keys on identity-
    style verbs only, never bare 'is a'.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Clark County is a great market for us.", "Speaker 4"),
            (2, 5500, 10000, "I agree about Clark County.", "Speaker 3"),
        ],
    )
    candidates = _candidates(cfg)
    bogus = [c for c in candidates if c[1].lower() in ("clark", "county")]
    assert not bogus, f"geo name leaked as candidate: {bogus}"


def test_im_sitting_no_longer_generates_sitting_candidate(tmp_path: Path) -> None:
    """Pre-v0.2.10 regression: 'I'm sitting on the panel' produced a
    self-introduction candidate with name='Sitting' at 0.72 confidence.
    Adding 'sitting' (and friends) to _STOP_NAMES kills the false-positive.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Hi, I'm sitting on the panel today.", "Speaker 5"),
            (2, 5500, 10000, "Thanks for joining us.", "Speaker 1"),
        ],
    )
    candidates = _candidates(cfg)
    bogus = [c for c in candidates if c[1].lower() in ("sitting", "looking", "trying")]
    assert not bogus, f"expected zero gerund false-positives, got: {bogus}"


def test_real_self_intro_still_works(tmp_path: Path) -> None:
    """Sanity check that the STOP_NAMES expansion didn't accidentally
    nuke a legitimate self-introduction (the most important signal).
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, "Hello, I'm Pat Smith, glad to be here.", "Speaker 4"),
        ],
    )
    candidates = _candidates(cfg)
    pat = [c for c in candidates if c[1] == "Pat"]
    assert pat, f"expected Pat self-intro candidate, got: {candidates}"
    assert pat[0][0] == "Speaker 4"


def test_question_for_amy_unchanged_behavior(tmp_path: Path) -> None:
    """Regression guard: the simple 'question for Amy' shape (no filler)
    that the original test in test_pipeline.py covers must still work
    after the pattern split.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 3000, "question for Amy on the timeline", "Speaker 1"),
            (2, 3500, 5000, "yes the first milestone is next week", "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    amy = [c for c in candidates if c[1] == "Amy"]
    assert amy
    assert amy[0][0] == "Speaker 2"


def test_lowercase_word_not_picked_up_as_name(tmp_path: Path) -> None:
    """The re.IGNORECASE on the new patterns must NOT make _NAME_PATTERN
    case-insensitive — that would catch 'question for everyone' as
    name='everyone'. (?-i:[A-Z]) on the first letter guards against this.
    """
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 3000, "I have a question for everyone here today", "Speaker 1"),
            (2, 3500, 5000, "Sure, happy to answer.", "Speaker 2"),
        ],
    )
    candidates = _candidates(cfg)
    bogus = [
        c for c in candidates
        if c[1].lower() in ("everyone", "here", "you", "all")
    ]
    assert not bogus, f"expected zero lowercase-trigger false-positives, got: {bogus}"


@pytest.mark.parametrize(
    "phrase,expected_name,expected_speaker",
    [
        # Trigger casing varies — sentence-start "Question" vs mid-string "question"
        ("Thank you. Question for you, Paul.", "Paul", "Speaker 4"),
        # "everyone, Paul" filler variant
        ("Question for everyone, Paul, how do you see this?", "Paul", "Speaker 4"),
        # Sentence-end → over to handoff
        ("That covers my view. Over to Paul now.", "Paul", "Speaker 4"),
    ],
)
def test_filler_variants_bind_to_next_speaker(
    tmp_path: Path, phrase: str, expected_name: str, expected_speaker: str
) -> None:
    cfg = _cfg(tmp_path)
    _seed(
        cfg,
        [
            (1, 0, 5000, phrase, "Speaker 2"),
            (2, 5500, 10000, "Well, I think we are at a tipping point.", "Speaker 4"),
        ],
    )
    candidates = _candidates(cfg)
    hits = [c for c in candidates if c[1] == expected_name]
    assert hits, f"phrase {phrase!r}: expected {expected_name}, got {candidates}"
    assert hits[0][0] == expected_speaker
