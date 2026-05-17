"""Audio MIME and migration DB-path-rewrite tests.

Covers two adjacent bugs we shipped fixes for:

1. The audio endpoint used `mimetypes.guess_type` defaults, which return
   `audio/mp4a-latm` for `.m4a` — a technically-correct LATM-AAC container
   MIME that browsers refuse to play. The fix swaps to an explicit
   browser-compatible MIME table in `_audio_media_type`.

2. `mm migrate-user-data` moved files on disk and rewrote `config/local.toml`
   but left DB rows pointing at the old absolute paths, so audio playback
   404'd until the DB caught up. The fix adds `_plan_db_path_rewrites` +
   `_apply_db_path_rewrites` plus a `--repair-paths-only` mode.
"""

from __future__ import annotations

from pathlib import Path

from app.api.routes import _AUDIO_MEDIA_TYPES, _audio_media_type
from app.cli import _plan_db_path_rewrites, _rewrite_legacy_path

# ── audio MIME ───────────────────────────────────────────────────────


def test_m4a_returns_browser_compatible_mime() -> None:
    """`.m4a` → `audio/mp4`, not Python's default `audio/mp4a-latm`.
    Regression-tests the bug where Chrome would stall at readyState 0
    forever because it doesn't recognize LATM-AAC as playable."""
    assert _audio_media_type(Path("meeting.m4a")) == "audio/mp4"


def test_all_supported_extensions_return_browser_safe_mime() -> None:
    """Every entry in the table should be a MIME `HTMLMediaElement.canPlayType`
    accepts in modern browsers — confirms we don't ship something like
    `audio/mp4a-latm` for any extension.
    """
    browser_safe_prefixes = ("audio/",)
    for ext, mime in _AUDIO_MEDIA_TYPES.items():
        assert ext.startswith("."), f"extension {ext!r} should start with '.'"
        assert any(mime.startswith(p) for p in browser_safe_prefixes), (
            f"extension {ext}: MIME {mime!r} should start with 'audio/'"
        )
        # No LATM variant should slip in — that was the original bug.
        assert "latm" not in mime.lower(), f"{ext} maps to LATM variant"


def test_unknown_extension_falls_through_to_octet_stream() -> None:
    """Unknown extensions get `application/octet-stream` so the browser
    download falls back to "save as" rather than choking on a falsely-
    advertised playable type. Better than silently advertising as audio
    and failing in the player.
    """
    assert _audio_media_type(Path("meeting.xyz")) == "application/octet-stream"
    assert _audio_media_type(Path("meeting")) == "application/octet-stream"


def test_extension_match_is_case_insensitive() -> None:
    """A meeting file uploaded as `.M4A` should still get `audio/mp4`."""
    assert _audio_media_type(Path("meeting.M4A")) == "audio/mp4"
    assert _audio_media_type(Path("meeting.WAV")) == "audio/wav"


# ── migration DB-path rewrite ────────────────────────────────────────


def test_rewrite_legacy_path_remaps_data_subtree(tmp_path: Path) -> None:
    """A path under `<legacy>/data/processed/foo.m4a` rewrites to
    `<new_data>/processed/foo.m4a`.
    """
    legacy_data = tmp_path / "old_repo" / "data"
    new_data = tmp_path / "user_data" / "data"
    legacy_vault = tmp_path / "old_repo" / "vault"
    new_vault = tmp_path / "user_data" / "vault"
    legacy_data.mkdir(parents=True)
    new_data.mkdir(parents=True)
    legacy_vault.mkdir(parents=True)
    new_vault.mkdir(parents=True)

    old_value = str(legacy_data / "processed" / "Healthcare meeting.m4a")
    rewritten = _rewrite_legacy_path(
        old_value,
        legacy_data_dir=legacy_data,
        legacy_vault_root=legacy_vault,
        new_data_dir=new_data,
        new_vault_root=new_vault,
    )
    assert rewritten == str(new_data / "processed" / "Healthcare meeting.m4a")


def test_rewrite_legacy_path_remaps_vault_subtree(tmp_path: Path) -> None:
    legacy_data = tmp_path / "old_repo" / "data"
    new_data = tmp_path / "user_data" / "data"
    legacy_vault = tmp_path / "old_repo" / "vault"
    new_vault = tmp_path / "user_data" / "vault"
    for p in (legacy_data, new_data, legacy_vault, new_vault):
        p.mkdir(parents=True)

    old_value = str(legacy_vault / "meeting_mind" / "Meetings" / "2026" / "foo.md")
    rewritten = _rewrite_legacy_path(
        old_value,
        legacy_data_dir=legacy_data,
        legacy_vault_root=legacy_vault,
        new_data_dir=new_data,
        new_vault_root=new_vault,
    )
    assert rewritten == str(new_vault / "meeting_mind" / "Meetings" / "2026" / "foo.md")


def test_rewrite_legacy_path_returns_none_for_unrelated_paths(
    tmp_path: Path,
) -> None:
    """Paths outside both data/ and vault/ subtrees (custom locations
    the user set up manually) should be left alone — silently rewriting
    them would be worse than the user noticing and fixing manually.
    """
    legacy_data = tmp_path / "old_repo" / "data"
    new_data = tmp_path / "user_data" / "data"
    legacy_vault = tmp_path / "old_repo" / "vault"
    new_vault = tmp_path / "user_data" / "vault"
    for p in (legacy_data, new_data, legacy_vault, new_vault):
        p.mkdir(parents=True)

    rewritten = _rewrite_legacy_path(
        "/some/totally/unrelated/path.m4a",
        legacy_data_dir=legacy_data,
        legacy_vault_root=legacy_vault,
        new_data_dir=new_data,
        new_vault_root=new_vault,
    )
    assert rewritten is None


def test_plan_db_path_rewrites_skips_missing_tables(tmp_path: Path) -> None:
    """If the live DB schema is missing one of the path columns (older
    install pre-some-migration), the planner should log + skip that
    table rather than crashing the whole repair.
    """
    from app.config import AppConfig, PathConfig
    from app.db.database import connect

    db_path = tmp_path / "tiny.sqlite3"
    with connect(db_path) as conn:
        # Deliberately incomplete schema — only `meetings` exists, no
        # `source_files` or `obsidian_exports`. The planner should
        # warn-and-skip the missing tables.
        conn.execute(
            "CREATE TABLE meetings (id INTEGER PRIMARY KEY, "
            "source_path TEXT, imported_path TEXT)"
        )
        conn.execute(
            "INSERT INTO meetings (id, source_path, imported_path) "
            "VALUES (1, '/legacy/data/inbox/x.m4a', '/legacy/data/processed/x.m4a')"
        )

    cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        paths=PathConfig(
            repo_root=tmp_path,
            data_dir=tmp_path / "new" / "data",
            inbox_dir=tmp_path / "new" / "data" / "inbox",
            processed_dir=tmp_path / "new" / "data" / "processed",
            archive_dir=tmp_path / "new" / "data" / "archive",
            delete_review_dir=tmp_path / "new" / "data" / "delete-review",
            runtime_dir=tmp_path / "new" / "runtime",
            database_path=db_path,
            vault_dir=tmp_path / "new" / "vault" / "meeting_mind",
        ),
    )
    # Should not raise even though source_files / obsidian_exports
    # don't exist in this tiny test DB.
    rewrites = _plan_db_path_rewrites(cfg, old_root=Path("/legacy"))
    # The two meeting rows match the legacy data subtree → both rewritten.
    assert len(rewrites) == 2
    assert all(r[0] == "meetings" for r in rewrites)
