from __future__ import annotations

import re

from app.config import AppConfig

TERM_SPLIT_PATTERN = re.compile(r"[,;]")
CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")


def load_custom_vocabulary_terms(config: AppConfig) -> list[str]:
    terms: list[str] = []
    terms.extend(config.asr.vocabulary_terms)
    path = config.asr.vocabulary_path
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            terms.extend(TERM_SPLIT_PATTERN.split(stripped))
    return _dedupe_terms(terms)


def build_asr_initial_prompt(config: AppConfig) -> str | None:
    terms = load_custom_vocabulary_terms(config)
    if not terms:
        return None
    prefix = "Use these custom vocabulary terms exactly when they match the audio: "
    selected: list[str] = []
    for term in terms:
        candidate = prefix + "; ".join([*selected, term])
        if len(candidate) > config.asr.vocabulary_prompt_max_chars:
            break
        selected.append(term)
    if not selected:
        return None
    return prefix + "; ".join(selected)


def _dedupe_terms(terms: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = CONTROL_PATTERN.sub(" ", str(term))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped
