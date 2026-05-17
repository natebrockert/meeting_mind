"""v0.2.10 OpenRouter resilience: `_parse_llm_json` repairs the common
LLM JSON malformations that previously crashed the extract stage with
a 500.

Real failure mode (logged during manual test of meeting 5):

    json.decoder.JSONDecodeError: Expecting property name enclosed in
    double quotes: line 45 column 1 (char 2641)

The model output had a trailing comma before a closing brace.
"""

from __future__ import annotations

import json

import pytest
from app.services.model_bus import _parse_llm_json


def test_plain_json_parses() -> None:
    assert _parse_llm_json('{"key": "value"}') == {"key": "value"}


def test_json_inside_markdown_fence_parses() -> None:
    raw = '```json\n{"key": "value"}\n```'
    assert _parse_llm_json(raw) == {"key": "value"}


def test_json_inside_plain_fence_parses() -> None:
    raw = '```\n{"key": 1}\n```'
    assert _parse_llm_json(raw) == {"key": 1}


def test_trailing_comma_before_brace_recovered() -> None:
    raw = '{"a": 1, "b": 2,}'
    assert _parse_llm_json(raw) == {"a": 1, "b": 2}


def test_trailing_comma_before_bracket_recovered() -> None:
    raw = '{"items": [1, 2, 3,]}'
    assert _parse_llm_json(raw) == {"items": [1, 2, 3]}


def test_leading_explanation_text_trimmed() -> None:
    raw = 'Sure, here is the JSON you requested: {"key": "value"}'
    assert _parse_llm_json(raw) == {"key": "value"}


def test_trailing_explanation_text_trimmed() -> None:
    raw = '{"key": "value"} — hope this helps!'
    assert _parse_llm_json(raw) == {"key": "value"}


def test_both_leading_and_trailing_text_trimmed() -> None:
    raw = 'Here you go: {"key": "value"} let me know if you need more'
    assert _parse_llm_json(raw) == {"key": "value"}


def test_nested_object_with_trailing_comma() -> None:
    raw = '{"outer": {"inner": "value",}}'
    assert _parse_llm_json(raw) == {"outer": {"inner": "value"}}


def test_real_world_failure_substring_extract() -> None:
    """Approximation of the failure logged on meeting 5: the model
    emitted a multi-line JSON object that had a stray trailing comma
    far into the payload.
    """
    raw = """{
        "decisions": [
            {"text": "foo"},
            {"text": "bar"},
        ],
        "actions": []
    }"""
    parsed = _parse_llm_json(raw)
    assert parsed == {
        "decisions": [{"text": "foo"}, {"text": "bar"}],
        "actions": [],
    }


def test_unrecoverable_raises_json_decode_error() -> None:
    """If nothing in our repair toolkit works, surface the original
    error so the caller can retry the LLM call.
    """
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json("definitely not json at all")


def test_empty_string_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json("")


def test_none_returns_decode_error_not_typeerror() -> None:
    """Defensive: some message payloads come through as None when the
    model emits empty content. The `content or ""` short-circuit makes
    it a JSONDecodeError, not a TypeError — callers can catch one
    exception type.
    """
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json(None)  # type: ignore[arg-type]


def test_openrouter_retry_succeeds_after_first_parse_failure(monkeypatch) -> None:
    """v0.2.10 audit-round-2 coverage: the retry path inside
    `_chat_json_openrouter` should successfully parse a clean response
    on the second attempt when the first attempt returned garbage.
    """
    import os

    from app.config import (
        AppConfig,
        AsrConfig,
        DiarizationConfig,
        ModelConfig,
        PathConfig,
    )
    from app.services.model_bus import ChatMessage, ModelBus

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-1234")

    cfg = AppConfig(
        config_path="/tmp/cfg",  # type: ignore[arg-type]
        paths=PathConfig(
            repo_root="/tmp",  # type: ignore[arg-type]
            data_dir="/tmp/data",  # type: ignore[arg-type]
            inbox_dir="/tmp/inbox",  # type: ignore[arg-type]
            processed_dir="/tmp/processed",  # type: ignore[arg-type]
            archive_dir="/tmp/archive",  # type: ignore[arg-type]
            delete_review_dir="/tmp/delete",  # type: ignore[arg-type]
            runtime_dir="/tmp/run",  # type: ignore[arg-type]
            database_path="/tmp/run/db.sqlite",  # type: ignore[arg-type]
            vault_dir="/tmp/vault",  # type: ignore[arg-type]
        ),
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        models=ModelConfig(provider="openrouter"),
    )
    bus = ModelBus(cfg)

    # Fake httpx.Client.post: first call returns garbage, second returns valid JSON.
    call_count = {"n": 0}

    class _Resp:
        def __init__(self, content: str, status: int = 200):
            self._content = content
            self.status_code = status
            self.text = content

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

        def json(self) -> dict:
            return {"choices": [{"message": {"content": self._content}}]}

    def fake_post(self, url, json=None, headers=None):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _Resp("not json at all")
        return _Resp('{"recovered": true}')

    monkeypatch.setattr("httpx.Client.post", fake_post)
    # Skip the api-key env-var noise — it's set above.
    _ = os.environ["OPENROUTER_API_KEY"]
    result = bus._chat_json_openrouter(
        messages=[ChatMessage("user", "give me json")],
        schema={"schema": {"type": "object"}},
        selected_model="test-model",
        request_timeout=10.0,
    )
    assert result == {"recovered": True}
    assert call_count["n"] == 2


def test_openrouter_retry_raises_runtime_error_on_double_failure(monkeypatch) -> None:
    """Both attempts return garbage → RuntimeError, not JSONDecodeError.
    Caller's existing try/except (extraction.py wraps each chunk in a
    broad except) flags the chunk as failed instead of 500'ing.
    """
    from app.config import (
        AppConfig,
        AsrConfig,
        DiarizationConfig,
        ModelConfig,
        PathConfig,
    )
    from app.services.model_bus import ChatMessage, ModelBus

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-1234")
    cfg = AppConfig(
        config_path="/tmp/cfg",  # type: ignore[arg-type]
        paths=PathConfig(
            repo_root="/tmp",  # type: ignore[arg-type]
            data_dir="/tmp/data",  # type: ignore[arg-type]
            inbox_dir="/tmp/inbox",  # type: ignore[arg-type]
            processed_dir="/tmp/processed",  # type: ignore[arg-type]
            archive_dir="/tmp/archive",  # type: ignore[arg-type]
            delete_review_dir="/tmp/delete",  # type: ignore[arg-type]
            runtime_dir="/tmp/run",  # type: ignore[arg-type]
            database_path="/tmp/run/db.sqlite",  # type: ignore[arg-type]
            vault_dir="/tmp/vault",  # type: ignore[arg-type]
        ),
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        models=ModelConfig(provider="openrouter"),
    )
    bus = ModelBus(cfg)

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "still not json"}}]}

    def fake_post(self, url, json=None, headers=None):  # type: ignore[no-untyped-def]
        return _Resp()

    monkeypatch.setattr("httpx.Client.post", fake_post)
    with pytest.raises(RuntimeError, match="invalid JSON after one retry"):
        bus._chat_json_openrouter(
            messages=[ChatMessage("user", "give me json")],
            schema={"schema": {"type": "object"}},
            selected_model="test-model",
            request_timeout=10.0,
        )
