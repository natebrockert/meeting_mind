from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect
from app.services.speaker_learning import (
    known_speaker_profile_summary,
    profile_similarity_suggestions,
)

_NAME_PATTERN = r"(?P<name>[A-Z][A-Za-z'-]{1,24})"
# v0.2.10: when a surrounding pattern uses re.IGNORECASE (so the trigger
# words can match any casing), the leading [A-Z] in _NAME_PATTERN would
# otherwise also become case-insensitive and grab lowercase words as
# "names". (?-i:...) turns IGNORECASE off for the first letter only —
# the rest of the name can be any case.
_NAME_PATTERN_CI_SAFE = r"(?P<name>(?-i:[A-Z])[A-Za-z'-]{1,24})"
# v0.2.12: ASR commonly UNDER-capitalizes names in casual business
# meetings ("you know scott i was telling him" instead of "Scott").
# The case-sensitive patterns above miss every lowercase name. This
# variant accepts EITHER case for the leading letter; safety relies on
# STOP_NAMES being comprehensive enough to filter common-word
# matches. Min 3 chars to drop "I", "we", "go", etc. before they ever
# reach STOP_NAMES.
_NAME_PATTERN_ANY_CASE = r"(?P<name>[A-Za-z][A-Za-z'-]{2,24})"
# v0.2.10: panel-discussion handoff patterns. Existing `question for X` /
# `thanks X` shapes don't cover the actual English used in moderated
# panels — "Question for you, Paul", "Janet, thank you for that wonderful
# introduction", or the host introducing a panelist with "Pat Smith.
# He serves as research professor at...". Three new pattern groups:
#
#  1. _DIRECT_ADDRESS_PATTERNS — extended with optional "you,"/"everyone,"
#     filler between the trigger and the name. Binding rule unchanged
#     (name → next different speaker who responds within response_gap_ms).
#
#  2. _VOCATIVE_THANK_PATTERNS — vocative-first address ("Janet, thank
#     you..."). The NAME refers to the PREVIOUS different speaker who
#     just finished talking, not the next one. New evidence_type
#     `vocative_thank` is bound to the prior speaker.
#
#  3. _HOST_INTRO_PATTERNS — "FirstName LastName. He/She/They serves as
#     ..." identifies a panelist by host introduction. Binding is
#     deferred: we collect the (first-name) pool from these matches and
#     boost confidence on any later direct-address candidate whose name
#     is in the pool.
_DIRECT_ADDRESS_PATTERNS = [
    re.compile(rf"\b(?:thanks|thank you|over to|go ahead)\s+{_NAME_PATTERN}\b"),
    # v0.2.10: "question for you, Paul" / "question for everyone, Paul" /
    # "question for the group, Paul". Optional filler is a single short
    # noun-phrase followed by a comma (or space).
    re.compile(
        rf"\bquestion for\s+(?:(?:you|everyone|the\s+(?:group|panel)|all)[,\s]+)?"
        rf"{_NAME_PATTERN_CI_SAFE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:let'?s bring|bring)\s+{_NAME_PATTERN_CI_SAFE}\s+(?:in|into)\b",
        re.IGNORECASE,
    ),
    # v0.2.10: passing the mic — "to Paul", "over to Paul, what do you
    # think". Only fires when "to" is preceded by sentence-ending
    # punctuation or "let's go" so we don't catch every "talk to John".
    re.compile(
        rf"(?:[.!?]\s+|\blet'?s go\s+|\bnow\s+)over to\s+{_NAME_PATTERN_CI_SAFE}\b",
        re.IGNORECASE,
    ),
    # v0.2.12: vocative-then-question — "Alex, I don't know if you've
    # done this", "Scott, you also have", "becky, do you remember".
    # The name addresses the next speaker who responds. Common business-
    # meeting pattern that the panel-tuned patterns above completely
    # miss. Optional "hey,"/"oh," prefix handles "Oh, hey, Brent,
    # thanks..." (technically a vocative_thank, but listing here keeps
    # the binding-to-next-speaker semantics consistent).
    re.compile(
        rf"(?:^|[.!?]\s+|(?:hey|oh|well|listen)[,]?\s+)"
        rf"{_NAME_PATTERN_ANY_CASE},\s+"
        r"(?:I\b|you\b|are\b|do\b|did\b|can\b|could\b|would\b|will\b"
        r"|how\b|when\b|what\b|tell\b|let\b|please\b|that\b|where\b"
        r"|why\b|which\b|who\b|here\b|there\b)",
        re.IGNORECASE,
    ),
    # v0.2.12: embedded vocative after conversational filler — "you know
    # scott I was telling him that...", "you know those becky I don't",
    # "hey brett, like that". Captures the name right after the filler.
    re.compile(
        rf"\b(?:you know(?: those)?|hey|listen|right)\s+"
        rf"{_NAME_PATTERN_ANY_CASE}\b"
        r"(?=,|\s+(?:I|you|do|can|did|are|was|were|like|that|the)\b)",
        re.IGNORECASE,
    ),
    # v0.2.14: greeting vocative — "Hey, John. Happy Friday", "Hi, John."
    # The name is followed by a period (or "!"), not a question phrase.
    # MUST be preceded by an explicit greeting word — `^|[.!?]\s+`
    # alone is too permissive (catches any sentence-final adverb like
    # "Conversely. The thing is..." or "Frankly.").
    re.compile(
        rf"\b(?:hey|hi|hello|oh|yo)[,\s]+"
        rf"{_NAME_PATTERN_ANY_CASE}\s*[.!?]",
        re.IGNORECASE,
    ),
    # v0.2.14: object-of-attention vocative — "tell me X", "hearing X",
    # "hard to hear X", "talk to X" where the speaker addresses or
    # references someone in the conversation. Common in remote meetings
    # ("hard to hear you Matthew", "asked Brad to send data"). Same
    # binding rule as direct-address (next different speaker responds).
    re.compile(
        rf"\b(?:hear(?:ing)?|tell|talk(?:ed|ing)?\s+to|ask(?:ed|ing)?|"
        rf"call(?:ed|ing)?|email(?:ed|ing)?|text(?:ed|ing)?)\s+"
        rf"(?:you\s+)?{_NAME_PATTERN_ANY_CASE}\b"
        r"(?=,|[.!?]|\s+(?:I|you|please|that|the|right|to|about|if|whether)\b)",
        re.IGNORECASE,
    ),
    # v0.2.14: sentence-coordinating vocative — "and John does that
    # make sense", "but Matt, what do you think", "so Wolf, can you".
    # Coordinating conjunctions ("and", "but", "so") at sentence start
    # followed by a name + question/comma cue.
    re.compile(
        rf"(?:^|[.!?]\s+)(?:and|but|so|okay|alright)\s+"
        rf"{_NAME_PATTERN_ANY_CASE}\b"
        r"(?=,|\s+(?:do|does|did|can|could|would|will|are|is|was|were|what|how|why|please|I|you)\b)",
        re.IGNORECASE,
    ),
]
# v0.2.10: vocative-first patterns. "Janet, thank you for..." has Janet
# referring to the PREVIOUS speaker, so the binding rule is reversed —
# we look BACK for the previous different speaker.
_VOCATIVE_THANK_PATTERNS = [
    # v0.2.12: allow "hey,"/"oh,"/"well," prefix and lowercase names so
    # "Oh, hey, Brent, thanks for joining" matches (Brent was preceded
    # by interjections, not by sentence-end punctuation).
    re.compile(
        rf"(?:^|[.!?]\s+|(?:hey|oh|well|listen)[,]?\s+)"
        rf"{_NAME_PATTERN_ANY_CASE},\s+"
        r"(?:thanks|thank you|that was|appreciate|good (?:point|question|answer))\b",
        re.IGNORECASE,
    ),
]
# v0.2.10: host-introduces-panelist. Captures the first-name of a
# {FirstName LastName}. {He/She/They} {role-verb} pattern. The match
# itself doesn't bind to a speaker — instead, the first names enter
# a "known panelist pool" that boosts direct-address candidates.
_HOST_INTRO_PATTERNS = [
    re.compile(
        rf"\b{_NAME_PATTERN}\s+[A-Z][A-Za-z'-]{{1,24}}\."
        r"\s+(?:He|She|They)\s+"
        r"(?:serves|is|was|works|joined|leads|directs|teaches|writes|sits|chairs|founded|hosts|presents|holds|earned|received)\b"
    ),
    # v0.2.12: "Pat Smith is charged with..." — informal host
    # intro without the He/She/They pronoun. Requires identity-style
    # verbs ("is charged", "is the", "serves as") rather than generic
    # ones, so geo-name false positives like "Clark County is a..."
    # don't match (no "is a" trigger, just specific role/intro verbs).
    re.compile(
        rf"\b{_NAME_PATTERN}\s+[A-Z][A-Za-z'-]{{1,24}}\s+"
        r"(?:is\s+(?:charged|the\s+(?:lead|head|director|VP|SVP|EVP|CEO|CTO|CFO|founder)|our\s+(?:lead|head|VP|SVP|EVP)|part\s+of|going\s+to)"
        r"|will\s+(?:lead|head|join|present|drive)"
        r"|serves\s+as|works\s+at|joined\s+(?:us|the\s+team))\b",
        re.IGNORECASE,
    ),
]
_SELF_INTRO_PATTERNS = [
    re.compile(rf"\b(?:I am|I'm|my name is)\s+{_NAME_PATTERN}\b", re.IGNORECASE),
]
_STORY_PATTERNS = [
    re.compile(rf"\b{_NAME_PATTERN}\s+(?:said|says|told|asked|mentioned|thinks|thought)\b"),
    re.compile(rf"\b(?:about|with|from)\s+{_NAME_PATTERN}\b", re.IGNORECASE),
]
_STOP_NAMES = {
    # v0.2.10: extended with gerunds and progressive-aspect verbs that
    # commonly follow "I'm" / "I am" and generate false-positive name
    # candidates (e.g., "I'm sitting on the panel" → fake name "Sitting").
    # Add new entries here in alphabetical order.
    "ai",
    "also",
    "actually",
    "all",
    "an",
    "and",
    "any",
    "asking",
    "at",
    "by",
    "for",
    "in",
    "into",
    "of",
    "on",
    "or",
    "out",
    "so",
    "up",
    "anything",
    "as",
    "back",
    "because",
    "board",
    "business",
    "can",
    "cloud",
    "could",
    "customer",
    "do",
    "esteemed",
    "from",
    "going",
    "guests",
    "guys",
    "how",
    "it",
    "kind",
    "let",
    "located",
    "machine",
    "meeting",
    "more",
    "operator",
    "over",
    "process",
    "question",
    "service",
    "sensing",
    "some",
    "thanks",
    "that",
    "the",
    "there",
    "this",
    "to",
    "what",
    "where",
    "with",
    "would",
    "we",
    "worth",
    "you",
    # v0.2.12 + v0.2.13: conversational fillers, pronouns, gerunds,
    # generic nouns — anything that's both (a) likely to follow a
    # vocative anchor in the transcript and (b) clearly NOT a name.
    # Added wholesale based on real-transcript audits.
    "alright",
    "anybody",
    "but",
    "ending",
    "everybody",
    "everyone",
    "fine",
    "joining",
    "let's",
    "like",
    "maybe",
    "nobody",
    "no",
    "no one",
    "now",
    "oh",
    "okay",
    "perhaps",
    "right",
    "she",
    "somebody",
    "someone",
    "such",
    "they",
    "them",
    "their",
    "those",
    "true",
    "wait",
    "yeah",
    "yes",
    "yep",
    # v0.2.10: -ing forms commonly following "I'm" / "I am" / "I was"
    "coming",
    "doing",
    "eating",
    "feeling",
    "good",
    "happy",
    "hearing",
    "here",
    "hoping",
    "just",
    "leaving",
    "listening",
    "looking",
    "losing",
    "moving",
    "not",
    "open",
    "ready",
    "running",
    "saying",
    "seeing",
    "sitting",
    "sorry",
    "sort",
    "speaking",
    "standing",
    "starting",
    "sure",
    "talking",
    "telling",
    "thinking",
    "trying",
    "walking",
    "watching",
    "well",
    "wondering",
    "working",
    # v0.2.14: surfaced as candidates on a real meeting upload —
    # contractions, numbers, modal verbs, gerunds, and informal
    # contractions the prior list missed. Lowercase as required by
    # `_valid_name`'s `.lower()` comparison.
    "i'm",
    "it's",
    "that's",
    "we're",
    "we've",
    "you're",
    "you've",
    "don't",
    "doesn't",
    "didn't",
    "won't",
    "wouldn't",
    "shouldn't",
    "couldn't",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "haven't",
    "hasn't",
    "hadn't",
    "gonna",
    "wanna",
    "kinda",
    "sorta",
    "gotta",
    "lemme",
    "should",
    "must",
    # "may" is intentionally NOT in this list — it's a common English
    # given name (and surname). The modal-verb usage almost never
    # surfaces as a name candidate because the candidate regex requires
    # direct-address or vocative context, neither of which fits "you
    # may proceed" / "we may want to". Keeping "may" out of the stop
    # list avoids a real false-negative on people named May.
    "might",
    "shall",
    "having",
    "being",
    "getting",
    "making",
    "taking",
    "knowing",
    "showing",
    "putting",
    "calling",
    "reading",
    "writing",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "first",
    "second",
    "third",
    "next",
    "last",
    "every",
    "each",
    "another",
    "few",
    "many",
    "most",
    "much",
    "lots",
    "plenty",
    # v0.2.14 part 2: adverbs + question words that the new recall
    # patterns let through as false positives on a real meeting:
    # "About" via "asking about X", "Conversely. / Frankly." via the
    # initial greeting-vocative pattern (since tightened), "When" /
    # "Where" / "Why" via coordinating-vocative.
    "about",
    "again",
    "against",
    "almost",
    "already",
    "always",
    "anyway",
    "around",
    "basically",
    "behind",
    "below",
    "beside",
    "besides",
    "between",
    "beyond",
    "clearly",
    "conversely",
    "currently",
    "elsewhere",
    "equally",
    "especially",
    "essentially",
    "eventually",
    "exactly",
    "finally",
    "frankly",
    "further",
    "generally",
    "hence",
    "honestly",
    "however",
    "immediately",
    "indeed",
    "initially",
    "instead",
    "literally",
    "lately",
    "later",
    "likely",
    "mainly",
    "meanwhile",
    "mostly",
    "namely",
    "naturally",
    "nearly",
    "never",
    "nevertheless",
    "obviously",
    "often",
    "originally",
    "otherwise",
    "particularly",
    "personally",
    "possibly",
    "presumably",
    "previously",
    "probably",
    "quickly",
    "quite",
    "rather",
    "really",
    "recently",
    "regardless",
    "regularly",
    "seemingly",
    "seriously",
    "similarly",
    "simply",
    "slightly",
    "slowly",
    "specifically",
    "still",
    "subsequently",
    "suddenly",
    "supposedly",
    "surely",
    "thankfully",
    "then",
    "thus",
    "today",
    "tomorrow",
    "totally",
    "ultimately",
    "unfortunately",
    "usually",
    "very",
    "when",
    "whenever",
    "whereas",
    "wherever",
    "whether",
    "while",
    "whoever",
    "why",
    "yesterday",
}


@dataclass(frozen=True)
class NameEvidence:
    name: str
    speaker_id: str
    segment_id: int
    evidence_type: str
    phrase: str


def persist_speaker_name_candidates(config: AppConfig, meeting_id: int) -> int:
    candidates = _speaker_name_candidates(config, meeting_id)
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "speaker_name_candidate"),
        )
        for evidence_group in candidates:
            first = evidence_group[0]
            confidence = _name_candidate_confidence(config, evidence_group)
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "speaker_name_candidate",
                    f"Possible name for {first.speaker_id}: {first.name.title()}",
                    json.dumps(
                        {
                            "speaker_id": first.speaker_id,
                            "candidate_name": first.name.title(),
                            "confidence_basis": "direct-address/self-introduction evidence only",
                            "identity_rule": (
                                "conversation evidence only; vocal presentation cues cannot "
                                "assign identity alone"
                            ),
                            "vocal_presentation_cue_policy": (
                                "disabled by default; when enabled it is capped as a weak "
                                "confidence signal and never creates identity by itself"
                            ),
                            "evidence": [item.__dict__ for item in evidence_group],
                        }
                    ),
                    confidence,
                    json.dumps(sorted({item.segment_id for item in evidence_group})),
                ),
            )
    persist_known_speaker_profile_candidates(config, meeting_id)
    return len(candidates)


def persist_known_speaker_profile_candidates(config: AppConfig, meeting_id: int) -> int:
    with connect(config.paths.database_path) as conn:
        name_candidates = conn.execute(
            """
            SELECT *
            FROM review_items
            WHERE meeting_id = ? AND kind = ? AND status = 'open'
            ORDER BY id
            """,
            (meeting_id, "speaker_name_candidate"),
        ).fetchall()

    matches: dict[tuple[str, str], dict] = {}
    for item in name_candidates:
        payload = json.loads(item["payload_json"])
        candidate_name = str(payload.get("candidate_name", "")).strip()
        speaker_id = str(payload.get("speaker_id", "")).strip()
        if not candidate_name or not speaker_id:
            continue
        summary = known_speaker_profile_summary(
            config,
            candidate_name,
            exclude_meeting_id=meeting_id,
        )
        if not summary:
            continue
        base_confidence = float(item["confidence"] or 0.5)
        confidence = round(
            min(0.97, base_confidence + 0.1 + min(0.12, 0.03 * summary["meeting_count"])),
            3,
        )
        matches[(speaker_id, candidate_name.casefold())] = {
            "speaker_id": speaker_id,
            "candidate_name": candidate_name,
            "profile_summary": summary,
            "confidence": confidence,
            "source_segment_ids": item["source_segment_ids"],
            "confidence_basis": (
                "conversation evidence plus prior confirmed local speaker profile"
            ),
            "match_method": "name_evidence_profile_match",
        }

    for suggestion in profile_similarity_suggestions(config, meeting_id):
        key = (suggestion["speaker_id"], suggestion["candidate_name"].casefold())
        if key in matches and matches[key]["confidence"] >= suggestion["confidence"]:
            continue
        matches[key] = {
            "speaker_id": suggestion["speaker_id"],
            "candidate_name": suggestion["candidate_name"],
            "profile_summary": suggestion["profile_summary"],
            "confidence": suggestion["confidence"],
            "source_segment_ids": json.dumps(suggestion["source_segment_ids"]),
            "confidence_basis": (
                "similar transcript fingerprint compared with a prior confirmed "
                "local speaker profile"
            ),
            "match_method": "lexical_profile_similarity",
            "similarity": suggestion["similarity"],
            "overlap_terms": suggestion["overlap_terms"],
        }

    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "speaker_profile_match"),
        )
        count = 0
        for match in matches.values():
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "speaker_profile_match",
                    (
                        f"Known speaker candidate for {match['speaker_id']}: "
                        f"{match['candidate_name']}"
                    ),
                    json.dumps(
                        {
                            "speaker_id": match["speaker_id"],
                            "candidate_name": match["candidate_name"],
                            "profile_summary": match["profile_summary"],
                            "confidence_basis": match["confidence_basis"],
                            "match_method": match["match_method"],
                            "similarity": match.get("similarity"),
                            "overlap_terms": match.get("overlap_terms", []),
                            "identity_rule": "review suggestion only; never auto-assign identity",
                        }
                    ),
                    match["confidence"],
                    match["source_segment_ids"],
                ),
            )
            count += 1
    return count


def _speaker_name_candidates(
    config: AppConfig,
    meeting_id: int,
    response_gap_ms: int = 10000,
) -> list[list[NameEvidence]]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()

    grouped: dict[tuple[str, str], list[NameEvidence]] = defaultdict(list)

    # v0.2.10: build the "host-introduced panelist pool" across all rows
    # first so direct-address candidates can boost when a name was
    # previously introduced as a panelist by the host. Pool is a set of
    # casefolded first names that appeared in a host-intro pattern.
    panelist_pool: set[str] = set()
    for row in rows:
        for name, _phrase in _extract_names(str(row["text"]), _HOST_INTRO_PATTERNS):
            panelist_pool.add(name)

    for index, row in enumerate(rows):
        text = str(row["text"])
        speaker_id = str(row["diarization_speaker_id"])

        for name, phrase in _extract_names(text, _SELF_INTRO_PATTERNS):
            # Reject "I'm yelling" / "I'm misremembering" / "I'm hoping"
            # style gerund false positives. Real -ing names (King,
            # Sterling, Manning) survive via the allowlist.
            if _is_self_intro_gerund(name):
                continue
            grouped[(speaker_id, name)].append(
                NameEvidence(
                    name=name,
                    speaker_id=speaker_id,
                    segment_id=int(row["id"]),
                    evidence_type="self_introduction",
                    phrase=phrase,
                )
            )

        for name, phrase in _extract_names(text, _DIRECT_ADDRESS_PATTERNS):
            response = _next_different_speaker(rows, index, response_gap_ms)
            if response is None:
                continue
            evidence_type = (
                "response_after_direct_address_panelist"
                if name in panelist_pool
                else "response_after_direct_address"
            )
            grouped[(str(response["diarization_speaker_id"]), name)].append(
                NameEvidence(
                    name=name,
                    speaker_id=str(response["diarization_speaker_id"]),
                    segment_id=int(row["id"]),
                    evidence_type=evidence_type,
                    phrase=f"{phrase}; response segment {response['id']}",
                )
            )

        # v0.2.10: vocative-first thanks address the PREVIOUS speaker.
        # "Janet, thank you for that wonderful introduction" → Janet is
        # whoever just stopped talking, not who's about to start.
        for name, phrase in _extract_names(text, _VOCATIVE_THANK_PATTERNS):
            previous = _previous_different_speaker(rows, index, response_gap_ms)
            if previous is None:
                continue
            grouped[(str(previous["diarization_speaker_id"]), name)].append(
                NameEvidence(
                    name=name,
                    speaker_id=str(previous["diarization_speaker_id"]),
                    segment_id=int(row["id"]),
                    evidence_type="vocative_thank",
                    phrase=f"{phrase}; addressee segment {previous['id']}",
                )
            )

    return [
        evidence
        for evidence in grouped.values()
        if evidence
        and _name_candidate_confidence(config, evidence)
        >= config.review.transcript_uncertainty_threshold
    ]


def _extract_names(text: str, patterns: list[re.Pattern[str]]) -> list[tuple[str, str]]:
    story_mentions = {
        match.group("name").casefold()
        for pattern in _STORY_PATTERNS
        for match in pattern.finditer(text)
        if _valid_name(match.group("name"))
    }
    names: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group("name").casefold()
            if _valid_name(name) and name not in story_mentions:
                names.append((name, match.group(0)))
    return names


# Real first/last names that legitimately end in "-ing" — the gerund
# rejector below would otherwise block these. Anything ending in "-ing"
# that's NOT in this set is treated as a gerund false-positive in the
# self_introduction context (where "i'm misremembering" / "i'm yelling"
# generate fake candidates). Add new entries here when a real "-ing"
# name turns up on a live meeting.
_REAL_ING_NAMES = frozenset({
    # Western surnames + given names
    "king",
    "sterling",
    "manning",
    "channing",
    "browning",
    "downing",
    "fleming",
    "harding",
    "cumming",
    "tilling",
    # Chinese / Asian given names and surnames — 4-char -ing forms
    # that the prior allowlist missed.
    "wing",
    "bing",
    "ming",
    "ping",
    "ling",
    "ting",
    "ying",
    "hing",
    "sing",
    "ring",
    "huang",
    "young",
    "wong",  # not -ing; defensive
})


def _is_self_intro_gerund(name: str) -> bool:
    """Reject `-ing` words as self-introduction names.

    Self-introduction patterns match `I'm X` / `I am X`, which is the
    high-frequency spot for transcript filler like "I'm misremembering"
    / "I'm yelling" / "I'm hoping" to generate fake name candidates.
    Real first names ending in -ing exist (King, Sterling, Manning,
    Wing, Ming, Ping, etc.) so they're allowlisted; anything else
    gets rejected when seen in this context. Direct-address /
    vocative / host-intro contexts skip this check — gerunds in
    those contexts are much less common.
    """
    lower = name.lower()
    if not lower.endswith("ing"):
        return False
    if len(lower) <= 3:  # "ing" alone — defensive lower bound
        return False
    return lower not in _REAL_ING_NAMES


def _valid_name(name: str) -> bool:
    # Compare against the stop list case-insensitively. The regex
    # preserves original casing ("Gonna", "I'M", "Should"), but the
    # stop entries are lowercased — without `.lower()` the comparison
    # fails for every capitalized word and the entire stop list
    # silently does nothing on transcripts that start utterances
    # with capitalized fillers (which is most of them).
    return len(name) > 1 and name.lower() not in _STOP_NAMES


def _next_different_speaker(rows, index: int, response_gap_ms: int):
    row = rows[index]
    for candidate in rows[index + 1 : index + 4]:
        if candidate["diarization_speaker_id"] == row["diarization_speaker_id"]:
            continue
        if int(candidate["start_ms"]) - int(row["end_ms"]) <= response_gap_ms:
            return candidate
        return None
    return None


# v0.2.10: vocative-first patterns ("Janet, thank you for...") address the
# PREVIOUS speaker, so we need a mirror of `_next_different_speaker` that
# looks backward. Same 3-row window + gap constraint, just reversed.
def _previous_different_speaker(rows, index: int, response_gap_ms: int):
    row = rows[index]
    start_idx = max(0, index - 3)
    for candidate in reversed(rows[start_idx:index]):
        if candidate["diarization_speaker_id"] == row["diarization_speaker_id"]:
            continue
        if int(row["start_ms"]) - int(candidate["end_ms"]) <= response_gap_ms:
            return candidate
        return None
    return None


def _name_candidate_confidence(config: AppConfig, evidence: list[NameEvidence]) -> float:
    # v0.2.10: scale base confidence by evidence strength.
    #   - self_introduction stays at 0.72 (strongest single signal)
    #   - vocative_thank ("Janet, thank you...") is high-confidence; the
    #     speaker handing off uses the name explicitly while pointing to
    #     who just spoke. Treat as 0.70.
    #   - response_after_direct_address_panelist ("Question for Paul" +
    #     Paul was previously introduced as a panelist by the host)
    #     rates higher than a bare direct-address — 0.68.
    #   - response_after_direct_address bare stays at 0.58.
    has_self = any(item.evidence_type == "self_introduction" for item in evidence)
    has_vocative = any(item.evidence_type == "vocative_thank" for item in evidence)
    has_panelist = any(
        item.evidence_type == "response_after_direct_address_panelist" for item in evidence
    )
    if has_self:
        base = 0.72
    elif has_vocative:
        base = 0.70
    elif has_panelist:
        base = 0.68
    else:
        base = 0.58
    repeat_bonus = min(0.3, 0.12 * (len(evidence) - 1))
    vocal_presentation_cue_boost = config.review.vocal_presentation_cue_max_boost if (
        config.review.vocal_presentation_cue_scoring_enabled
        and any(
            item.evidence_type
            in ("response_after_direct_address", "response_after_direct_address_panelist")
            for item in evidence
        )
    ) else 0.0
    return round(min(0.95, base + repeat_bonus + vocal_presentation_cue_boost), 3)
