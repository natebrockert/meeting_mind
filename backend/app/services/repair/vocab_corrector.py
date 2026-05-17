"""Vocabulary corrector — post-ASR LLM repair pass.

Closes the most user-visible class of ASR errors: misheard named entities
and domain terms. Pure substitution from the configured `asr_vocabulary`
list, gated by an LLM yes/no decision so we don't apply phonetic-similar
substitutions that don't match the surrounding context.

Design constraints (audit-aware):
  - Pure substitution only — the LLM never generates new text. It picks
    YES/NO on a candidate (original, replacement) pair.
  - Two-stage filter: deterministic phonetic distance check first
    (cheap), LLM gate second (expensive, batched).
  - Confident words are never touched. Only word probabilities below
    `repair.vocab_correction_min_confidence` are eligible.
  - Edits are surfaced in the review UI as `transcript_candidates`
    rows (same schema the existing multi-pass Whisper repair uses),
    NOT applied silently. The user accepts or rejects each.

Added in v0.2.1 as the first of three planned LLM repair passes that
close the diarization/ASR quality gap left by the lite-stack default.

Returns: a list of proposed (segment_id, original_word, replacement_term,
basis) tuples. The pipeline writes them as `transcript_candidates` rows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.config import AppConfig

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class VocabCorrection:
    """A single proposed substitution surfaced for review."""

    segment_id: int
    word_index: int  # position of the word within the segment's `words` list
    original: str
    replacement: str
    original_confidence: float
    distance: int  # Levenshtein distance between original and replacement
    basis: str  # short LLM-provided justification for surfacing to user


# Public entry point ------------------------------------------------------


def propose_vocab_corrections(
    config: AppConfig,
    segments: list[dict],  # each segment must have id + text + words
    vocabulary: list[str],
) -> list[VocabCorrection]:
    """Scan an ASR transcript for low-confidence words that look like
    misheard vocabulary terms; ask the LLM to confirm each correction.

    Each input segment is expected to be a dict with:
      - id: int (the segment's DB id)
      - text: str
      - words: list[{start, end, text, probability}] (word-level timings)

    Empty `vocabulary` (or `repair.vocab_correction_enabled=False`)
    short-circuits to an empty list — no LLM call, no cost.
    """
    if not config.repair.vocab_correction_enabled:
        return []
    if not vocabulary:
        return []

    candidates = _deterministic_candidates(
        segments,
        vocabulary,
        min_confidence=config.repair.vocab_correction_min_confidence,
        max_distance=config.repair.vocab_correction_max_distance,
    )
    if not candidates:
        return []

    # Batch through the LLM. Each batch is sized to keep the prompt
    # under a few thousand tokens — vocab_correction_batch_size is
    # both a token-budget cap and a latency cap.
    accepted: list[VocabCorrection] = []
    batch_size = max(1, config.repair.vocab_correction_batch_size)
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        try:
            decisions = _llm_gate_batch(config, batch, vocabulary)
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline on repair
            _LOG.warning("vocab corrector LLM call failed: %s", exc)
            continue
        for cand, decision in zip(batch, decisions, strict=False):
            if decision.get("accept") is True:
                accepted.append(
                    VocabCorrection(
                        segment_id=cand.segment_id,
                        word_index=cand.word_index,
                        original=cand.original,
                        replacement=cand.replacement,
                        original_confidence=cand.original_confidence,
                        distance=cand.distance,
                        basis=str(decision.get("basis", ""))[:160],
                    )
                )
    return accepted


# Stage 1: deterministic phonetic-distance candidate generation ----------


@dataclass(frozen=True)
class _Candidate:
    segment_id: int
    word_index: int
    original: str
    replacement: str
    original_confidence: float
    distance: int
    context_before: str
    context_after: str


def _deterministic_candidates(
    segments: list[dict],
    vocabulary: list[str],
    *,
    min_confidence: float,
    max_distance: int,
) -> list[_Candidate]:
    """Pure-Python scan: for each low-confidence word, find vocabulary
    terms whose Levenshtein distance is below the cap. No LLM yet.

    Vocabulary terms with internal spaces (e.g. "Sample Street") are
    matched against bigrams or trigrams from the transcript so we can
    catch "Sample Treat" → "Sample Street".
    """
    out: list[_Candidate] = []
    # Pre-tokenize vocabulary by word count so we can match each against
    # word windows of the right size.
    by_word_count: dict[int, list[str]] = {}
    for term in vocabulary:
        cleaned = term.strip()
        if not cleaned:
            continue
        words = cleaned.split()
        by_word_count.setdefault(len(words), []).append(cleaned)

    for segment in segments:
        words = segment.get("words") or []
        for index, word in enumerate(words):
            text = _strip_punct(str(word.get("text", "")))
            if not text:
                continue
            probability = word.get("probability")
            if probability is None or probability >= min_confidence:
                continue
            # Single-token match
            for term in by_word_count.get(1, []):
                dist = _levenshtein(text.lower(), term.lower())
                if 0 < dist <= max_distance:
                    out.append(
                        _Candidate(
                            segment_id=int(segment["id"]),
                            word_index=index,
                            original=text,
                            replacement=term,
                            original_confidence=float(probability),
                            distance=dist,
                            context_before=_context_window(words, index, -4),
                            context_after=_context_window(words, index, +4),
                        )
                    )
            # Multi-token vocab terms: try a window starting at this word
            for n_words, terms in by_word_count.items():
                if n_words == 1:
                    continue
                window_words = words[index : index + n_words]
                if len(window_words) < n_words:
                    continue
                window_text = " ".join(
                    _strip_punct(str(w.get("text", ""))) for w in window_words
                ).strip()
                if not window_text:
                    continue
                for term in terms:
                    dist = _levenshtein(window_text.lower(), term.lower())
                    if 0 < dist <= max_distance:
                        out.append(
                            _Candidate(
                                segment_id=int(segment["id"]),
                                word_index=index,
                                original=window_text,
                                replacement=term,
                                original_confidence=float(probability),
                                distance=dist,
                                context_before=_context_window(words, index, -4),
                                context_after=_context_window(
                                    words, index + n_words - 1, +4
                                ),
                            )
                        )
    return out


def _strip_punct(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9'\-]", "", value).strip()


def _context_window(words: list[dict], anchor_index: int, offset: int) -> str:
    """Return up to `|offset|` words before (offset<0) or after (offset>0)
    the anchor, joined with spaces."""
    if offset == 0:
        return ""
    if offset < 0:
        start = max(0, anchor_index + offset)
        end = anchor_index
    else:
        start = anchor_index + 1
        end = min(len(words), anchor_index + 1 + offset)
    return " ".join(str(w.get("text", "")).strip() for w in words[start:end])


def _levenshtein(a: str, b: str) -> int:
    """Edit distance — small, hot path, so handrolled DP. Returns int."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


# Stage 2: LLM yes/no gating ---------------------------------------------


def _llm_gate_batch(
    config: AppConfig,
    batch: list[_Candidate],
    vocabulary: list[str],
) -> list[dict]:
    """Send a batch of (original, replacement, context) candidates to the
    configured LLM as a yes/no gating decision.

    The model is asked to return a JSON array of {accept: bool, basis: str}
    objects, one per input candidate, in input order. We use the default
    (small) model — this is a high-volume cheap call, not synthesis.
    """
    from app.services.model_bus import ChatMessage, ModelBus

    prompt = _build_prompt(batch, vocabulary)
    schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "accept": {"type": "boolean"},
                        "basis": {"type": "string"},
                    },
                    "required": ["accept"],
                },
            }
        },
        "required": ["decisions"],
    }
    bus = ModelBus(config=config)
    payload = bus.chat_json(
        [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=prompt),
        ],
        schema=schema,
        timeout=30,
    )
    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    if not isinstance(decisions, list):
        return [{"accept": False} for _ in batch]
    # Pad or trim so output length matches input length.
    if len(decisions) < len(batch):
        decisions = decisions + [{"accept": False}] * (len(batch) - len(decisions))
    return decisions[: len(batch)]


_SYSTEM_PROMPT = (
    "You are a conservative spell-checker for meeting transcripts. "
    "Your only job is to decide whether a low-confidence transcribed "
    "word should be corrected to a known vocabulary term. "
    "Reject if the words are not phonetically similar, OR if the context "
    "makes the vocabulary term implausible. "
    "When in doubt, say NO. False positives are worse than missed corrections."
)


def _build_prompt(batch: list[_Candidate], vocabulary: list[str]) -> str:
    # Audit M2 (v0.2.5): we no longer enumerate the full vocab list as
    # a separate block. Each candidate already pairs (original →
    # replacement); the LLM only needs to evaluate THAT specific pair.
    # Showing 50 alphabetical vocab terms was redundant AND silently
    # capped users with longer vocab lists. `vocabulary` is still
    # accepted for signature compat / future "alternative replacement"
    # uses.
    _ = vocabulary
    lines = [
        "CANDIDATES (decide for each: should the transcript word be corrected?):",
        "",
    ]
    for i, cand in enumerate(batch, start=1):
        ctx = (
            f"...{cand.context_before} [{cand.original}] {cand.context_after}..."
        ).strip()
        lines.append(
            f'{i}. transcript heard: "{cand.original}" '
            f"(confidence {cand.original_confidence:.2f})"
        )
        lines.append(f"   propose: \"{cand.replacement}\" (edit distance {cand.distance})")
        lines.append(f"   context: {ctx}")
        lines.append("")
    lines.append(
        'Return JSON: {"decisions": [{"accept": true|false, "basis": "short reason"}, ...]} '
        "with one decision per candidate, in the same order."
    )
    return "\n".join(lines)


# Helpers exposed for testing -------------------------------------------


def _candidates_for_test(*args, **kwargs) -> Iterable[_Candidate]:
    """Exposed so unit tests can exercise the deterministic stage without
    going through the LLM."""
    return _deterministic_candidates(*args, **kwargs)
