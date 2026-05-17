"""Tests for the eval-models A/B harness.

Covers the auth-failure early-abort and model-slug sanitization. The
end-to-end driving of `run_model_ab` against real models is intentionally
not tested here — it would require live OpenRouter access and is
exercised manually via `mm eval-models`. This suite is for the local
helpers that gate the expensive paths.
"""

from __future__ import annotations

from app.services.eval_models import _looks_like_auth_failure, _model_slug

# ── auth-failure heuristic ───────────────────────────────────────────


def test_auth_failure_detects_common_provider_messages() -> None:
    """Every common shape of "your API key is wrong" should trip the
    detector. Each pattern is what we've observed across OpenRouter,
    Anthropic, OpenAI, and httpx error formatters.
    """
    for error in (
        "HTTPStatusError: 401 Unauthorized",
        "AuthenticationError: invalid api key",
        "{'error': {'message': 'No auth credentials', 'type': 'auth'}}",
        # Real OpenRouter 403s include the status text + reason, so the
        # word-form "unauthorized" or "invalid api key" hits the heuristic.
        "HTTPStatusError: 403 Forbidden: invalid api key",
        "API key not set",
        "invalid_api_key",
    ):
        assert _looks_like_auth_failure(error), error


def test_auth_failure_ignores_non_auth_errors() -> None:
    """Stage-specific failures (timeout, bad model id, malformed JSON
    from a working call) should NOT trigger an early abort.

    Also regression-tests the bare-numeric false-positive class — early
    versions of this heuristic matched the substring "401" or "403" and
    would false-trigger on JSON byte offsets, line numbers, port
    fragments, or schema versions that happened to contain those
    digits. The word-form token list catches every real auth error
    without trapping these.
    """
    for error in (
        "TimeoutError after 60s",
        "JSONDecodeError: Expecting value at line 1 column 1",
        "openrouter_status_500",
        "Connection refused",
        # Bare-numeric false-positive cases:
        "Failed to parse JSON at position 401",
        "Connection to 192.168.1.403 refused",
        "RuntimeError at line 4012 of grpc_async_handler.py",
        None,
        "",
    ):
        assert not _looks_like_auth_failure(error), error


# ── model slug sanitization ──────────────────────────────────────────


def test_model_slug_normalizes_path_separators() -> None:
    """OpenRouter model ids use `/` as a vendor/family separator;
    that has to become something filesystem-safe. The expected output
    is `vendor__model`.
    """
    assert _model_slug("x-ai/grok-4.1-fast") == "x-ai__grok-4.1-fast"
    assert _model_slug("openai/gpt-oss-120b") == "openai__gpt-oss-120b"


def test_model_slug_handles_colon_versioning() -> None:
    """Some routes use `model:tag` (e.g. `:free` tier suffixes). Colon
    becomes underscore so the slug stays valid on every filesystem.
    """
    assert _model_slug("openai/gpt-oss-120b:free") == "openai__gpt-oss-120b_free"
