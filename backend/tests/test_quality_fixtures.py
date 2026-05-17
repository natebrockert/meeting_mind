from __future__ import annotations

import json
from pathlib import Path

from app.services.transcript_quality import detect_transcript_quality_issue


def test_transcript_quality_fixture_set() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "transcript_quality_cases.json"
    cases = json.loads(fixture_path.read_text())

    assert {case["name"] for case in cases} == {
        "repeated_phrase_hallucination",
        "cross_talk_marker",
        "silence_gap_placeholder",
        "noisy_audio_fragment",
    }
    for case in cases:
        issue = detect_transcript_quality_issue(case["text"])
        assert (issue.kind if issue else None) == case["expected_issue"]
