"""Regression tests for .env.local injection via settings endpoints.

Audit finding C-1: A POST to /api/settings/openrouter-key (or the HF token
sibling) with a value containing embedded newlines would inject extra lines
into .env.local. On next backend start those injected lines would be loaded
as real environment variables (HUGGING_FACE_HUB_TOKEN=stolen, etc.).

These tests pin the sanitizer behavior so the regression can't return.
"""

from __future__ import annotations

from app.api.routes import _sanitize_env_value


def test_strips_embedded_newlines() -> None:
    value = "sk-or-legit\nHUGGING_FACE_HUB_TOKEN=evil"
    assert _sanitize_env_value(value) == "sk-or-legitHUGGING_FACE_HUB_TOKEN=evil"
    assert "\n" not in _sanitize_env_value(value)


def test_strips_carriage_returns() -> None:
    value = "sk-or-legit\r\nMALICIOUS=1"
    cleaned = _sanitize_env_value(value)
    assert "\r" not in cleaned
    assert "\n" not in cleaned


def test_strips_null_bytes() -> None:
    assert "\x00" not in _sanitize_env_value("sk-or-\x00abc")


def test_strips_unicode_line_separators() -> None:
    """Audit H-A regression: U+2028 / U+2029 / NEL / FS/GS/RS / VT / FF all
    split lines via Python's str.splitlines() — they MUST be scrubbed or
    the C-1 injection class is re-opened.
    """
    for sep in [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"]:
        payload = f"sk-or-good{sep}HUGGING_FACE_HUB_TOKEN=evil"
        cleaned = _sanitize_env_value(payload)
        # Critical: no splitlines() recognisable break may remain
        assert len(cleaned.splitlines()) == 1, (
            f"sanitizer left a line-break for char U+{ord(sep):04X}: {cleaned!r}"
        )
        # And the injected payload tail is joined into the first line
        assert "HUGGING_FACE_HUB_TOKEN" in cleaned


def test_strips_surrounding_whitespace() -> None:
    assert _sanitize_env_value("  sk-or-abc  ") == "sk-or-abc"


def test_empty_input_returns_empty() -> None:
    assert _sanitize_env_value("") == ""
    assert _sanitize_env_value("   \n\r  ") == ""


def test_normal_key_unchanged() -> None:
    # Synthetic non-secret string; broken up so secret scanners don't
    # heuristically flag it as a real OpenRouter key fixture.
    key = "sk-" + "or-" + "v1-" + "abc" + "def"
    assert _sanitize_env_value(key) == key


def test_write_env_var_sanitizes_defensively(tmp_path, monkeypatch) -> None:
    """`_write_env_var` re-sanitizes even if the caller forgets — belt-and-braces.

    If this regresses, a future caller that skips _sanitize_env_value at the
    boundary could reopen the injection.
    """
    from app.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "_REPO_ROOT", tmp_path, raising=False)
    # _write_env_var imports REPO_ROOT lazily inside the function, so we have
    # to patch the import target. Easier: just patch the module the function
    # imports from.
    import app.config as config_mod

    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)

    routes_mod._write_env_var("TEST_VAR", "legit\nINJECTED=evil")

    env_path = tmp_path / ".env.local"
    contents = env_path.read_text()
    # Only one line should mention TEST_VAR, and INJECTED= should NOT be
    # written as a standalone line.
    assert contents.count("TEST_VAR=") == 1
    assert "\nINJECTED=" not in contents
    assert "TEST_VAR=legitINJECTED=evil" in contents
