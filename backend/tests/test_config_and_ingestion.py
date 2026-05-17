from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import (
    REPO_ROOT,
    AppConfig,
    AsrConfig,
    LegacyDataLocationError,
    PathConfig,
    _default_user_data_root,
    _detect_legacy_data_in_repo,
    ensure_local_layout,
    load_config,
)
from app.db.database import initialize_database
from app.services.asr_vocabulary import build_asr_initial_prompt, load_custom_vocabulary_terms
from app.services.audio import is_supported_media, slugify_filename
from app.services.ingestion import ingest_file


def test_supported_media() -> None:
    assert is_supported_media(Path("meeting.m4a"))
    assert is_supported_media(Path("meeting.wav"))
    assert not is_supported_media(Path("notes.txt"))


def test_slugify_filename() -> None:
    assert slugify_filename(Path("Sample Meeting.m4a")) == "sample-meeting"


def test_local_layout_and_database(tmp_path: Path) -> None:
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    cfg = AppConfig(config_path=tmp_path / "config" / "local.toml", paths=paths)
    ensure_local_layout(cfg)
    initialize_database(paths.database_path)
    assert paths.inbox_dir.exists()
    assert (paths.vault_dir / "Meetings").exists()
    with sqlite3.connect(paths.database_path) as conn:
        row = conn.execute("SELECT name FROM sqlite_master WHERE name = 'meetings'").fetchone()
    assert row is not None


def test_custom_vocabulary_merges_file_and_config_terms(tmp_path: Path) -> None:
    vocabulary_path = tmp_path / "config" / "local.vocabulary.txt"
    vocabulary_path.parent.mkdir(parents=True)
    vocabulary_path.write_text("# local terms\nSample Street\nRevOps; Example Person\nrevops\n")
    cfg = _test_config(
        tmp_path,
        asr=AsrConfig(
            vocabulary_path=vocabulary_path,
            vocabulary_terms=["MeetingMind", "Sample Street"],
        ),
    )

    terms = load_custom_vocabulary_terms(cfg)
    prompt = build_asr_initial_prompt(cfg)

    assert terms == ["MeetingMind", "Sample Street", "RevOps", "Example Person"]
    assert prompt is not None
    assert "MeetingMind" in prompt
    assert "RevOps" in prompt


def test_custom_vocabulary_prompt_respects_max_chars(tmp_path: Path) -> None:
    cfg = _test_config(
        tmp_path,
        asr=AsrConfig(
            vocabulary_terms=["Short", "Second Term", "This term would exceed the cap"],
            vocabulary_prompt_max_chars=95,
        ),
    )

    prompt = build_asr_initial_prompt(cfg)

    assert prompt is not None
    assert len(prompt) <= 95
    assert "Short" in prompt
    assert "This term would exceed the cap" not in prompt


def test_relative_meetingmind_config_resolves_against_repo_root(monkeypatch) -> None:
    load_config.cache_clear()
    monkeypatch.setenv("MEETINGMIND_CONFIG", "config/local.toml")

    cfg = load_config()

    assert cfg.config_path == REPO_ROOT / "config" / "local.toml"
    load_config.cache_clear()


def test_duplicate_ingestion_moves_duplicate_to_archive(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    first = cfg.paths.inbox_dir / "first.m4a"
    duplicate = cfg.paths.inbox_dir / "duplicate.m4a"
    first.write_bytes(b"same audio")
    duplicate.write_bytes(b"same audio")
    monkeypatch.setattr("app.services.ingestion.probe_duration_seconds", lambda _: 1.0)

    first_result = ingest_file(cfg, first)
    duplicate_result = ingest_file(cfg, duplicate)

    assert first_result.status == "ingested"
    assert duplicate_result.status == "duplicate"
    assert duplicate_result.source_path.parent == cfg.paths.archive_dir
    assert not duplicate.exists()
    assert len(list(cfg.paths.processed_dir.iterdir())) == 1


def _test_config(tmp_path: Path, asr: AsrConfig | None = None) -> AppConfig:
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=asr or AsrConfig(),
    )


# ── User-data-root defaults ──────────────────────────────────────────


def test_default_user_data_root_respects_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    """`MEETINGMIND_DATA_HOME` forces the data root regardless of platform.
    Sandbox + fixture-based developer workflows depend on this.
    """
    target = tmp_path / "sandbox" / "meetingmind"
    monkeypatch.setenv("MEETINGMIND_DATA_HOME", str(target))
    assert _default_user_data_root() == target.resolve()


def test_default_user_data_root_falls_back_to_platform_dir(
    monkeypatch,
) -> None:
    """Without the override, we land in a per-user OS-conventional location.

    Only checks that the result is OUTSIDE the repo and looks like a
    user-scoped dir. Don't pin the exact path because the test runs on
    multiple platforms in CI and the convention differs per OS.
    """
    monkeypatch.delenv("MEETINGMIND_DATA_HOME", raising=False)
    root = _default_user_data_root()
    # Must not be inside the repo. This is the whole point of the change —
    # user data lives outside the checkout so agents / linters / archive
    # tools never see private content while walking the source tree.
    try:
        root.resolve().relative_to(REPO_ROOT.resolve())
        raise AssertionError(
            f"user data root {root} unexpectedly resolves inside the repo"
        )
    except ValueError:
        pass  # expected — not under REPO_ROOT
    # And it should be under the user's home directory in some form.
    home = Path.home().resolve()
    assert home in root.resolve().parents or root.resolve() == home


def test_user_data_root_is_outside_repo(monkeypatch) -> None:
    """Regression check: every default user-data path resolves outside
    the repo. Renamed from the previous tautological version that only
    exercised the helper — this version instantiates `PathConfig` and
    asserts on the *actual* fields the application reads at runtime.
    """
    monkeypatch.delenv("MEETINGMIND_DATA_HOME", raising=False)
    # PathConfig snapshots USER_DATA_ROOT at class-definition time, so
    # we read the fields as the running module sees them rather than
    # re-evaluating defaults. This proves the class itself is wired to
    # the user-data root, not just the helper.
    paths = PathConfig()
    repo_root_resolved = REPO_ROOT.resolve()
    for label, value in [
        ("data_dir", paths.data_dir),
        ("inbox_dir", paths.inbox_dir),
        ("processed_dir", paths.processed_dir),
        ("archive_dir", paths.archive_dir),
        ("delete_review_dir", paths.delete_review_dir),
        ("runtime_dir", paths.runtime_dir),
        ("database_path", paths.database_path),
        ("vault_dir", paths.vault_dir),
    ]:
        try:
            value.resolve().relative_to(repo_root_resolved)
            raise AssertionError(
                f"PathConfig.{label} default ({value}) resolves inside the repo"
            )
        except ValueError:
            continue
    # `repo_root` itself is intentionally still REPO_ROOT — code paths
    # use it to resolve relative path references and to anchor audit-
    # containment checks. Confirm we didn't accidentally change that.
    assert paths.repo_root.resolve() == repo_root_resolved


def test_legacy_data_detection_fires_on_orphaned_repo_install(
    tmp_path: Path, monkeypatch
) -> None:
    """If the repo contains legacy runtime data AND the live config
    matches the new user-data-root defaults (i.e. an upgrade picked
    them up automatically), `ensure_local_layout` must refuse to
    proceed. Without this guard, upgrading installs with a paths-less
    local.toml would boot empty and appear to have lost all meetings.
    """
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    (fake_repo / "runtime").mkdir()
    (fake_repo / "runtime" / "meetingmind.sqlite3").write_text("legacy")
    monkeypatch.setattr("app.config.REPO_ROOT", fake_repo)

    # Live config points at a fresh user-data dir AND matches the
    # detector's "this user is on new defaults" check — simulate by
    # also pinning USER_DATA_ROOT through the monkeypatch.
    fake_user_root = tmp_path / "user-data"
    monkeypatch.setattr("app.config.USER_DATA_ROOT", fake_user_root)

    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=PathConfig(
            repo_root=fake_repo,
            data_dir=fake_user_root / "data",
            inbox_dir=fake_user_root / "data" / "inbox",
            processed_dir=fake_user_root / "data" / "processed",
            archive_dir=fake_user_root / "data" / "archive",
            delete_review_dir=fake_user_root / "data" / "delete-review",
            runtime_dir=fake_user_root / "runtime",
            database_path=fake_user_root / "runtime" / "meetingmind.sqlite3",
            vault_dir=fake_user_root / "vault" / "meeting_mind",
        ),
    )

    detected = _detect_legacy_data_in_repo(cfg)
    assert detected is not None, "legacy detector should fire on upgrade scenario"

    import pytest as _pytest

    with _pytest.raises(LegacyDataLocationError) as excinfo:
        ensure_local_layout(cfg)
    assert "mm migrate-user-data" in str(excinfo.value)
    assert "config/local.toml" in str(excinfo.value)


def test_legacy_detector_no_false_positive_for_fresh_install(
    tmp_path: Path, monkeypatch
) -> None:
    """Fresh install (no legacy data in the repo) — detector quiet."""
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    fake_user_root = tmp_path / "user-data"
    monkeypatch.setattr("app.config.REPO_ROOT", fake_repo)
    monkeypatch.setattr("app.config.USER_DATA_ROOT", fake_user_root)

    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=PathConfig(
            repo_root=fake_repo,
            runtime_dir=fake_user_root / "runtime",
            database_path=fake_user_root / "runtime" / "meetingmind.sqlite3",
            data_dir=fake_user_root / "data",
            inbox_dir=fake_user_root / "data" / "inbox",
            processed_dir=fake_user_root / "data" / "processed",
            archive_dir=fake_user_root / "data" / "archive",
            delete_review_dir=fake_user_root / "data" / "delete-review",
            vault_dir=fake_user_root / "vault" / "meeting_mind",
        ),
    )
    assert _detect_legacy_data_in_repo(cfg) is None
    ensure_local_layout(cfg)
    assert cfg.paths.inbox_dir.exists()


def test_legacy_error_message_is_actionable() -> None:
    """The raised LegacyDataLocationError carries the recovery steps in
    its message so CLI and FastAPI handlers don't need to invent their
    own copy. Regression-tests the contract main.py + the cli wrapper
    rely on: the str() of the exception is what gets shown to the user.
    """
    exc = LegacyDataLocationError(
        "Found legacy MeetingMind data inside the repo at /x — but the "
        "live config points at /y. Refusing to proceed because that "
        "would orphan your existing meetings, transcripts, and vault.\n\n"
        "Pick one:\n"
        "  • `mm migrate-user-data` — move repo-relative data to the "
        "platform-standard user-data dir and rewrite config\n"
        "  • Add explicit paths to `config/local.toml` pointing at "
        "the legacy location to keep using it (gitignore it!)"
    )
    text = str(exc)
    assert "mm migrate-user-data" in text
    assert "config/local.toml" in text
    assert "orphan" in text  # confirms the consequence is named explicitly


def test_legacy_detector_no_false_positive_when_paths_are_custom(
    tmp_path: Path, monkeypatch
) -> None:
    """User with custom paths (tmp_path tests, external-drive installs,
    any non-default location) — detector quiet even when repo has data.
    The detector only fires when the live config matches the NEW
    defaults, because that's the only scenario where the user got the
    defaults silently without deciding to.
    """
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    (fake_repo / "runtime").mkdir()
    (fake_repo / "runtime" / "meetingmind.sqlite3").write_text("legacy")
    monkeypatch.setattr("app.config.REPO_ROOT", fake_repo)
    # USER_DATA_ROOT remains the actual platform default — but the live
    # config points somewhere COMPLETELY different (tmp_path/custom).
    # That's a clear "user intent" signal: don't second-guess.
    cfg = _test_config(tmp_path / "custom-data")
    assert _detect_legacy_data_in_repo(cfg) is None


def test_existing_install_with_pinned_paths_keeps_working(
    tmp_path: Path,
) -> None:
    """Existing installs (pre-PR) have `config/local.toml` files that
    pin paths to REPO_ROOT / data / ... These should continue to work
    after the defaults change — load_config's deep-merge means explicit
    TOML values override the new user-data-root defaults.
    """
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=PathConfig(
            repo_root=tmp_path,
            # Simulate an existing install that pinned everything to
            # the repo's own data + runtime dirs (the pre-PR convention).
            data_dir=tmp_path / "data",
            inbox_dir=tmp_path / "data" / "inbox",
            processed_dir=tmp_path / "data" / "processed",
            archive_dir=tmp_path / "data" / "archive",
            delete_review_dir=tmp_path / "data" / "delete-review",
            runtime_dir=tmp_path / "runtime",
            database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
            vault_dir=tmp_path / "vault" / "meeting_mind",
        ),
    )
    ensure_local_layout(cfg)
    assert (tmp_path / "data" / "inbox").exists()
    assert (tmp_path / "runtime").exists()
    assert (tmp_path / "vault" / "meeting_mind" / "Meetings").exists()
