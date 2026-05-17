from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect

WORD_RE = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class TranscriptQualityIssue:
    kind: str
    title: str
    detail: str
    confidence: float


def detect_transcript_quality_issue(text: str) -> TranscriptQualityIssue | None:
    tokens = [token.casefold() for token in WORD_RE.findall(text)]
    if len(tokens) < 12:
        return None

    repeated_token, repeated_count = Counter(tokens).most_common(1)[0]
    repeated_ratio = repeated_count / len(tokens)
    consecutive_count = _max_consecutive_count(tokens)
    if repeated_count >= 12 and (repeated_ratio >= 0.45 or consecutive_count >= 8):
        return TranscriptQualityIssue(
            kind="repetition",
            title="Possible ASR repetition hallucination",
            detail=(
                f"Segment contains repeated token '{repeated_token}' {repeated_count} times "
                f"across {len(tokens)} tokens."
            ),
            confidence=min(0.99, max(repeated_ratio, consecutive_count / len(tokens))),
        )
    phrase = _repeated_phrase(tokens)
    if phrase:
        phrase_text, count, phrase_size = phrase
        repeated_words = count * phrase_size
        return TranscriptQualityIssue(
            kind="phrase_repetition",
            title="Possible ASR phrase repetition hallucination",
            detail=(
                f"Segment repeats phrase '{phrase_text}' {count} times "
                f"across {len(tokens)} tokens."
            ),
            confidence=min(0.98, repeated_words / len(tokens)),
        )
    return None


def safe_transcript_text(segment_id: int, text: str) -> str:
    issue = detect_transcript_quality_issue(text)
    if not issue:
        return text
    return (
        f"[Low-confidence ASR; review segment {segment_id}. {issue.detail}] {text}"
    )


def persist_transcript_quality_issues(config: AppConfig, meeting_id: int) -> int:
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "transcript_quality"),
        )
        rows = conn.execute(
            """
            SELECT id, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        count = 0
        for row in rows:
            issue = detect_transcript_quality_issue(str(row["text"]))
            if not issue:
                continue
            segment_id = int(row["id"])
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "transcript_quality",
                    f"{issue.title}: segment {segment_id}",
                    json.dumps(
                        {
                            "issue_kind": issue.kind,
                            "detail": issue.detail,
                            "segment_id": segment_id,
                        }
                    ),
                    issue.confidence,
                    json.dumps([segment_id]),
                ),
            )
            count += 1
    return count


def _max_consecutive_count(tokens: list[str]) -> int:
    longest = 1
    current = 1
    for previous, token in zip(tokens, tokens[1:], strict=False):
        if token == previous:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _repeated_phrase(tokens: list[str]) -> tuple[str, int, int] | None:
    for phrase_size in (2, 3, 4):
        if len(tokens) < phrase_size * 6:
            continue
        phrases = [
            tuple(tokens[index : index + phrase_size])
            for index in range(0, len(tokens) - phrase_size + 1, phrase_size)
        ]
        phrase, count = Counter(phrases).most_common(1)[0]
        if count >= 6 and (count * phrase_size) / len(tokens) >= 0.38:
            return " ".join(phrase), count, phrase_size
    return None
