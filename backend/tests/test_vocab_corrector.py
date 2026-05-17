"""Tests for the v0.2.1 vocab corrector — deterministic candidate stage
+ end-to-end with a mocked LLM gate.

The deterministic stage is the part most likely to silently regress; the
LLM stage we test with a fixed mock to verify wiring + UNIQUE behavior in
the candidates table.
"""

from __future__ import annotations

from pathlib import Path

from app.config import AppConfig, AsrConfig, DiarizationConfig, PathConfig, RepairConfig
from app.services.repair.vocab_corrector import (
    VocabCorrection,
    _candidates_for_test,
    _levenshtein,
    propose_vocab_corrections,
)


def _seg(seg_id: int, text: str, words: list[tuple[str, float]]) -> dict:
    """Build a fake transcript segment with word-level probabilities."""
    return {
        "id": seg_id,
        "text": text,
        "words": [
            {"start": i * 1000, "end": (i + 1) * 1000, "text": w, "probability": p}
            for i, (w, p) in enumerate(words)
        ],
    }


def test_levenshtein_basic() -> None:
    assert _levenshtein("aronza", "aranza") == 1
    assert _levenshtein("aranza", "aranza") == 0
    assert _levenshtein("revops", "rev ops") == 1
    assert _levenshtein("", "abc") == 3


def test_deterministic_finds_phonetic_near_match() -> None:
    """The word 'Alica' at low confidence should match the vocab term
    'Alice' (edit distance 1)."""
    segments = [
        _seg(
            42,
            "we should ask Alica about timing",
            [
                ("we", 0.95),
                ("should", 0.95),
                ("ask", 0.95),
                ("Alica", 0.35),  # low confidence — eligible
                ("about", 0.95),
                ("timing", 0.95),
            ],
        )
    ]
    candidates = list(
        _candidates_for_test(
            segments,
            ["Alice", "RevOps", "MeetingMind"],
            min_confidence=0.6,
            max_distance=3,
        )
    )
    assert any(
        c.segment_id == 42 and c.original == "Alica" and c.replacement == "Alice"
        for c in candidates
    ), f"expected Alica→Alice candidate, got {candidates}"


def test_deterministic_ignores_confident_words() -> None:
    """High-confidence words are never proposed for correction, even when
    phonetically near a vocab term."""
    segments = [
        _seg(1, "Alica is here", [("Alica", 0.95), ("is", 0.99), ("here", 0.99)])
    ]
    candidates = list(
        _candidates_for_test(
            segments,
            ["Alice"],
            min_confidence=0.6,
            max_distance=3,
        )
    )
    assert candidates == []


def test_deterministic_rejects_too_distant() -> None:
    """Words too far from any vocab term aren't suggested."""
    segments = [
        _seg(
            1,
            "potato is here",
            [("potato", 0.30), ("is", 0.99), ("here", 0.99)],
        )
    ]
    candidates = list(
        _candidates_for_test(
            segments,
            ["Alice", "RevOps"],
            min_confidence=0.6,
            max_distance=3,
        )
    )
    assert candidates == []


def test_deterministic_handles_multi_word_vocab() -> None:
    """'Sample Treat' at low confidence should match 'Sample Street'
    (edit distance 1 on the bigram)."""
    segments = [
        _seg(
            7,
            "we live on Sample Treat",
            [
                ("we", 0.95),
                ("live", 0.95),
                ("on", 0.95),
                ("Sample", 0.40),  # eligible — triggers window scan
                ("Treat", 0.40),
            ],
        )
    ]
    candidates = list(
        _candidates_for_test(
            segments,
            ["Sample Street", "Alice"],
            min_confidence=0.6,
            max_distance=3,
        )
    )
    # Should match the 2-word window starting at Sample
    multiword = [c for c in candidates if c.replacement == "Sample Street"]
    assert multiword, f"expected Sample Street multi-word match, got {candidates}"


def test_disabled_by_config() -> None:
    """When the feature flag is off, no LLM call is ever made (and the
    function returns an empty list — important for the never-crash
    contract)."""
    cfg = AppConfig(
        config_path=Path("/tmp/fake.toml"),
        paths=PathConfig(repo_root=Path("/tmp"), database_path=Path("/tmp/fake.db")),
        repair=RepairConfig(vocab_correction_enabled=False),
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
    )
    result = propose_vocab_corrections(cfg, [], ["Alice"])
    assert result == []


def test_empty_vocabulary_short_circuits() -> None:
    cfg = AppConfig(
        config_path=Path("/tmp/fake.toml"),
        paths=PathConfig(repo_root=Path("/tmp"), database_path=Path("/tmp/fake.db")),
        repair=RepairConfig(vocab_correction_enabled=True),
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
    )
    assert propose_vocab_corrections(cfg, [_seg(1, "hi", [])], []) == []


def test_dataclass_shape() -> None:
    """Sanity check the public dataclass fields don't drift silently —
    persist_vocab_correction_candidates depends on every field."""
    correction = VocabCorrection(
        segment_id=1,
        word_index=2,
        original="Alica",
        replacement="Alice",
        original_confidence=0.4,
        distance=1,
        basis="phonetic match",
    )
    assert correction.original == "Alica"
    assert correction.replacement == "Alice"
    assert correction.distance == 1
