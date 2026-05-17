from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter

from app.config import AppConfig
from app.db.database import connect
from app.services.model_bus import ChatMessage, ModelBus

LOGGER = logging.getLogger(__name__)

STOP_WORDS = {
    "about",
    "across",
    "again",
    "also",
    "because",
    "before",
    "being",
    "business",
    "could",
    "from",
    "have",
    "meeting",
    "need",
    "needs",
    "people",
    "process",
    "really",
    "should",
    "that",
    "their",
    "there",
    "these",
    "thing",
    "things",
    "this",
    "through",
    "where",
    "with",
    "would",
}


def build_synthesis_snapshot(config: AppConfig, meeting_id: int) -> dict:
    with connect(config.paths.database_path) as conn:
        review_items = conn.execute(
            """
            SELECT kind, title, payload_json, confidence, source_segment_ids
            FROM review_items
            WHERE meeting_id = ?
            ORDER BY kind, confidence DESC, id
            """,
            (meeting_id,),
        ).fetchall()
        actions = conn.execute(
            "SELECT text, priority, source_segment_ids FROM action_items WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()
        transcript_rows = conn.execute(
            "SELECT text FROM transcript_segments WHERE meeting_id = ? ORDER BY start_ms",
            (meeting_id,),
        ).fetchall()
        words_available = conn.execute(
            "SELECT COUNT(*) FROM transcript_words WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()[0]

    summary = _summary_from_items(review_items)
    workstreams = [row["title"] for row in review_items if row["kind"] == "workstream"]
    decisions = [row["title"] for row in review_items if row["kind"] == "decision"]
    quality_count = sum(1 for row in review_items if row["kind"] == "transcript_quality")
    speaker_confidence_count = sum(
        1 for row in review_items if row["kind"] == "speaker_confidence"
    )
    key_terms = _key_terms_cached_or_llm(
        config=config,
        meeting_id=meeting_id,
        summary=summary,
        workstreams=workstreams,
        decisions=decisions,
        actions=[row["text"] for row in actions],
        transcript_rows=transcript_rows,
    )
    next_steps = _next_steps(
        quality_count=quality_count,
        speaker_confidence_count=speaker_confidence_count,
        action_count=len(actions),
        workstream_count=len(workstreams),
        words_available=int(words_available),
    )
    return {
        "summary": summary,
        "key_terms": key_terms,
        "workstreams": workstreams[:8],
        "decisions": decisions[:8],
        "action_count": len(actions),
        "quality_count": quality_count,
        "speaker_confidence_count": speaker_confidence_count,
        "words_available": int(words_available),
        "next_steps": next_steps,
    }


def _summary_from_items(review_items) -> str:
    for row in review_items:
        if row["kind"] != "summary":
            continue
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return ""
        summary = payload.get("summary")
        return summary if isinstance(summary, str) else ""
    return ""


def _key_terms_cached_or_llm(
    *,
    config: AppConfig,
    meeting_id: int,
    summary: str,
    workstreams: list[str],
    decisions: list[str],
    actions: list[str],
    transcript_rows,
    limit: int = 12,
) -> list[str]:
    """Fast path: pull cached key terms from `meeting_key_terms`. Slow path:
    call the quality LLM (which can take 20-30s on a remote OpenRouter
    model), persist the result, return it. The cache is invalidated when
    extraction re-runs for a meeting via `invalidate_key_terms_cache`."""
    cached = _load_cached_key_terms(config, meeting_id)
    if cached is not None:
        return cached
    terms = _key_terms_llm(
        config=config,
        summary=summary,
        workstreams=workstreams,
        decisions=decisions,
        actions=actions,
        transcript_rows=transcript_rows,
        limit=limit,
    )
    # Persist even when the LLM call fell back to the deterministic heuristic
    # — the fallback is stable for a given input, so we don't need to keep
    # recomputing it either.
    _persist_key_terms_cache(config, meeting_id, terms)
    return terms


def _load_cached_key_terms(config: AppConfig, meeting_id: int) -> list[str] | None:
    try:
        with connect(config.paths.database_path) as conn:
            row = conn.execute(
                "SELECT terms_json FROM meeting_key_terms WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        terms = json.loads(row["terms_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(terms, list) and all(isinstance(t, str) for t in terms):
        return terms
    return None


def _persist_key_terms_cache(
    config: AppConfig, meeting_id: int, terms: list[str]
) -> None:
    try:
        with connect(config.paths.database_path) as conn:
            conn.execute(
                """
                INSERT INTO meeting_key_terms (meeting_id, terms_json)
                VALUES (?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                  terms_json = excluded.terms_json,
                  computed_at = CURRENT_TIMESTAMP
                """,
                (meeting_id, json.dumps(terms)),
            )
    except sqlite3.Error as exc:
        LOGGER.warning("key_terms_cache_write_failed meeting=%d err=%s", meeting_id, exc)


def invalidate_key_terms_cache(config: AppConfig, meeting_id: int) -> None:
    """Drop the cached key-terms row for a meeting. Called whenever the
    meeting's content materially changes — re-extraction, transcript edits
    that significantly rewrite the text."""
    try:
        with connect(config.paths.database_path) as conn:
            conn.execute(
                "DELETE FROM meeting_key_terms WHERE meeting_id = ?",
                (meeting_id,),
            )
    except sqlite3.Error as exc:
        LOGGER.warning("key_terms_cache_invalidate_failed meeting=%d err=%s", meeting_id, exc)


def _key_terms_llm(
    *,
    config: AppConfig,
    summary: str,
    workstreams: list[str],
    decisions: list[str],
    actions: list[str],
    transcript_rows,
    limit: int = 12,
) -> list[str]:
    """Ask the local LLM for domain-specific terms worth highlighting in the
    transcript. Falls back to the deterministic frequency heuristic if the
    model is unreachable or returns garbage so the dashboard never breaks.
    """
    transcript_text = "\n".join(row["text"] for row in transcript_rows).strip()
    if not transcript_text:
        return []
    # Cap context so we don't blow the small Gemma model's window. Sample
    # three equal windows (start, middle, end) so a long meeting's middle
    # third stays visible to the key-term picker.
    capped = transcript_text
    if len(capped) > 7500:
        window = 2500
        mid_start = max(window, (len(transcript_text) - window) // 2)
        capped = (
            transcript_text[:window]
            + "\n…\n"
            + transcript_text[mid_start : mid_start + window]
            + "\n…\n"
            + transcript_text[-window:]
        )
    structured_hint = "\n".join(
        f"- {phrase}"
        for phrase in [*workstreams, *decisions, *actions][:20]
        if phrase
    ) or "(none)"
    prompt = (
        "You are highlighting key terms in a meeting transcript. Identify "
        f"up to {limit} short, domain-specific terms or named entities that "
        "are MEANINGFUL to highlight — proper nouns, product or project names, "
        "acronyms, technical jargon, customer/vendor names, distinctive "
        "metrics. Skip generic meeting/business vocabulary (team, project, "
        "meeting, process, etc.) and skip filler words. Each term is 1–4 "
        "words; prefer the exact casing used in the transcript. Return "
        'JSON of the shape {"terms": ["..."]} with no commentary.\n\n'
        f"Structured extractions already flagged for this meeting (do not "
        f"repeat verbatim, but use them as topical hints):\n{structured_hint}\n\n"
        "Summary:\n"
        f"{summary or '(no summary yet)'}\n\n"
        "Transcript excerpt:\n"
        f"{capped}"
    )
    schema = {
        "type": "object",
        "properties": {
            "terms": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": limit,
            }
        },
        "required": ["terms"],
    }
    try:
        payload = ModelBus(config).chat_json(
            [
                ChatMessage("system", "Return a tight JSON list of key terms."),
                ChatMessage("user", prompt),
            ],
            {"name": "KeyTerms", "schema": schema},
            # Use the quality model for term selection — the small default
            # frequently picks generic vocabulary; quality is worth the few
            # extra seconds since this gates highlight quality everywhere.
            model=config.models.quality_model or None,
            # Secondary enrichment. 45 s upper bound — long enough that
            # the quality model can finish on most local stacks, short
            # enough that a wedged model doesn't hold the whole synthesis
            # pass open indefinitely. The result is cached per-meeting
            # (see _key_terms_cached_or_llm) so this only runs once.
            timeout=45,
        )
        raw = payload.get("terms") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            cleaned: list[str] = []
            seen: set[str] = set()
            for term in raw:
                if not isinstance(term, str):
                    continue
                term = _clean_term(term)
                if term.casefold() in seen:
                    continue
                if not _is_meaningful_key_term(term):
                    continue
                # Skip terms that aren't actually in the transcript so the
                # frontend's case-insensitive match has something to attach to.
                if term.casefold() not in transcript_text.casefold():
                    continue
                cleaned.append(term)
                seen.add(term.casefold())
                if len(cleaned) >= limit:
                    break
            if cleaned:
                return cleaned
            LOGGER.info("key_terms_llm_empty_after_filter meeting_chars=%d", len(transcript_text))
    except Exception as exc:  # noqa: BLE001 — fall through to deterministic path
        LOGGER.warning("key_terms_llm_failed err=%s", exc)
    return _key_terms_fallback(workstreams, decisions, actions, transcript_rows, limit)


def _key_terms_fallback(
    workstreams: list[str],
    decisions: list[str],
    actions: list[str],
    transcript_rows,
    limit: int = 12,
) -> list[str]:
    phrase_candidates = [*workstreams, *decisions, *actions]
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in phrase_candidates:
        cleaned = _clean_term(phrase)
        if cleaned.casefold() in seen:
            continue
        if not _is_meaningful_key_term(cleaned):
            continue
        terms.append(cleaned)
        seen.add(cleaned.casefold())
        if len(terms) >= limit:
            return terms

    token_counts: Counter[str] = Counter()
    for row in transcript_rows:
        for token in re.findall(r"[A-Za-z][A-Za-z-]{3,}", row["text"]):
            key = token.casefold()
            if key in STOP_WORDS:
                continue
            token_counts[key] += 1
    for token, _count in token_counts.most_common(limit - len(terms)):
        if token in seen:
            continue
        terms.append(token)
        seen.add(token)
    return terms


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -:;,.")[:80]


# Stopwords blocked from key_terms even if the LLM proposes them. Pronouns,
# articles, conjunctions, modal verbs, generic meeting vocabulary — these
# produced noisy chartreuse highlights on the transcript view (e.g. "your",
# "what", "would") that the prompt's "skip generic vocab" instruction failed
# to suppress on small local models.
_KEY_TERM_STOPWORDS: frozenset[str] = frozenset(
    {
        # Pronouns
        "you", "your", "yours", "yourself", "yourselves", "me", "my", "mine",
        "myself", "we", "us", "our", "ours", "ourselves", "they", "them",
        "their", "theirs", "themselves", "he", "him", "his", "she", "her",
        "hers", "it", "its", "itself", "this", "that", "these", "those",
        # Interrogatives + relatives
        "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
        # Articles + common determiners
        "the", "a", "an", "some", "any", "each", "every", "all", "both",
        # Modal verbs + auxiliaries
        "can", "could", "would", "should", "will", "shall", "may", "might",
        "must", "do", "does", "did", "is", "are", "was", "were", "be",
        "been", "being", "have", "has", "had",
        # Conjunctions
        "and", "or", "but", "so", "if", "because", "though", "although",
        "while", "than",
        # Generic meeting vocabulary the prompt explicitly bans
        "team", "project", "meeting", "process", "discussion", "topic",
        "thing", "things", "people", "person",
        # Filler
        "yeah", "okay", "ok", "right", "well", "actually", "really", "just",
        "kind", "sort", "like",
    }
)


def _is_meaningful_key_term(term: str) -> bool:
    """Final guard before a candidate key_term is shown in the dashboard.

    Blocks: single stopwords, all-stopword multiwords, sub-3-char tokens.
    Keeps proper nouns regardless (any cased word stays) so 'Stripe' isn't
    confused with the stopword 'stripe'.
    """
    if not term or len(term) < 3:
        return False
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", term)
    if not tokens:
        return False
    has_capital = any(t[0].isupper() for t in tokens)
    if has_capital:
        return True
    # All-lowercase candidate must have at least one non-stopword token.
    return any(t.casefold() not in _KEY_TERM_STOPWORDS for t in tokens)


def _next_steps(
    quality_count: int,
    speaker_confidence_count: int,
    action_count: int,
    workstream_count: int,
    words_available: int,
) -> list[str]:
    steps: list[str] = []
    if quality_count:
        steps.append("Review ASR quality flags against the source audio.")
    if speaker_confidence_count:
        steps.append("Audit low-confidence speaker assignments before promotion.")
    if not words_available:
        steps.append("Enable word timestamps for higher-quality speaker-turn splits.")
    if workstream_count:
        steps.append("Confirm suggested workstreams before writing vault links.")
    if action_count:
        steps.append("Validate action owners and due dates.")
    return steps or ["Review synthesized transcript and promote when speakers are confirmed."]
