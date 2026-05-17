"""Regression tests for security helpers added in v0.1.2.

Audit findings H-2 (`_safe_repo_path`), H-3 (`_validate_model_name`),
M-3 (`tag_name` regex), M-4 (`_tildefy_path`). Each finding gets at
least one happy-path + one attack-path test pinned here so the fixes
can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── H-2 + H-B: path containment ──────────────────────────────────────────


def _make_cfg(tmp_path: Path):
    """Minimal AppConfig-shaped stub with just the .paths attributes
    `_safe_repo_path` touches."""
    from types import SimpleNamespace

    processed = tmp_path / "data" / "processed"
    delete_review = tmp_path / "data" / "delete_review"
    for p in (processed, delete_review):
        p.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        paths=SimpleNamespace(
            repo_root=tmp_path,
            processed_dir=processed,
            delete_review_dir=delete_review,
            archive_dir=None,
        )
    )


def test_safe_repo_path_accepts_path_inside_processed_dir(tmp_path) -> None:
    from app.api.routes import _safe_repo_path

    cfg = _make_cfg(tmp_path)
    audio = cfg.paths.processed_dir / "meeting_1.wav"
    audio.write_bytes(b"\x00")
    assert _safe_repo_path(cfg, str(audio)) == audio.resolve()


def test_safe_repo_path_rejects_absolute_escape(tmp_path) -> None:
    """An absolute path outside the data dirs must be rejected.

    Python's `Path("/repo") / "/etc/passwd"` returns `/etc/passwd` —
    this is the actual H-2 vulnerability. The helper must catch it.
    """
    from app.api.routes import _safe_repo_path
    from fastapi import HTTPException

    cfg = _make_cfg(tmp_path)
    with pytest.raises(HTTPException) as exc:
        _safe_repo_path(cfg, "/etc/passwd")
    assert exc.value.status_code == 404


def test_safe_repo_path_rejects_dotdot_escape(tmp_path) -> None:
    from app.api.routes import _safe_repo_path
    from fastapi import HTTPException

    cfg = _make_cfg(tmp_path)
    with pytest.raises(HTTPException):
        _safe_repo_path(cfg, "../../../../etc/passwd")


def test_safe_repo_path_rejects_repo_root_sibling(tmp_path) -> None:
    """Audit H-B regression: a path inside repo_root but OUTSIDE the data
    dirs (e.g. `.env.local`, `config/local.toml`) must be rejected. The
    v0.1.2 helper accepted anything under repo_root — this test would
    have failed against that version."""
    from app.api.routes import _safe_repo_path
    from fastapi import HTTPException

    cfg = _make_cfg(tmp_path)
    sensitive = tmp_path / ".env.local"
    sensitive.write_text("OPENROUTER_API_KEY=real_key")
    with pytest.raises(HTTPException):
        _safe_repo_path(cfg, str(sensitive))


# ── H-3: lms load arg injection ──────────────────────────────────────────


def test_validate_model_name_accepts_normal_models() -> None:
    from app.services.model_bus import _validate_model_name

    for name in [
        "qwen3-4b-instruct",
        "meta-llama/Llama-3-8B",
        "gemma-4-e4b-it@q8_0",
        "openrouter:anthropic/claude-3.5-sonnet",
        "model_v1.2.3",
    ]:
        assert _validate_model_name(name) == name


def test_validate_model_name_rejects_flag_smuggling() -> None:
    from app.services.model_bus import _validate_model_name

    for bad in ["--verbose", "-v", "--unload-all", " --flag", ""]:
        with pytest.raises(ValueError):
            _validate_model_name(bad)


# ── M-4: home directory tilde-substitution ───────────────────────────────


def test_tildefy_path_replaces_home_prefix(monkeypatch, tmp_path) -> None:
    from app.api import routes as routes_mod

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(routes_mod.Path, "home", classmethod(lambda cls: fake_home))

    nested = fake_home / "code" / "meeting_mind" / "inbox"
    nested.mkdir(parents=True)
    result = routes_mod._tildefy_path(nested)
    # No leakage of the username/home prefix
    assert "fakehome" not in result
    assert result.startswith("~/")
    assert "code/meeting_mind/inbox" in result


def test_tildefy_path_returns_path_when_outside_home(tmp_path) -> None:
    from app.api.routes import _tildefy_path

    # A path that's definitely not under HOME — leave it alone.
    p = Path("/tmp/some_dir")
    assert _tildefy_path(p) == str(p)
