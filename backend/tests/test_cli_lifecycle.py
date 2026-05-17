"""Integration tests for the lifecycle / housekeeping commands on the
MeetingMind CLI. These exercise the command callbacks directly (no
subprocess.run on the meetingmind binary itself) and use a per-test
tmp_path-backed AppConfig so the real install isn't touched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from app.cli import (
    _git_working_tree_clean,
    _running_services,
    _selected_services,
    _service_log_path,
    _service_pid_path,
    backup,
    cli,
    logs,
    reset,
)
from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import initialize_database
from typer.testing import CliRunner


def _make_config(tmp_path: Path) -> AppConfig:
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
    return cfg


def test_selected_services_targets_both_when_no_flag() -> None:
    assert _selected_services(backend=False, frontend=False) == ("backend", "frontend")


def test_selected_services_targets_one_when_flag_given() -> None:
    assert _selected_services(backend=True, frontend=False) == ("backend",)
    assert _selected_services(backend=False, frontend=True) == ("frontend",)


def test_service_pid_and_log_paths_match_managed_services(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    for service in ("backend", "frontend"):
        pid_path = _service_pid_path(cfg, service)
        log_path = _service_log_path(cfg, service)
        assert pid_path.name == f"meetingmind-{service}.pid"
        assert log_path == cfg.paths.runtime_dir / "logs" / f"{service}.log"


def test_running_services_returns_empty_when_no_pid_files(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert _running_services(cfg) == []


def test_running_services_detects_a_live_pid(tmp_path: Path) -> None:
    import os

    cfg = _make_config(tmp_path)
    backend_pid_path = _service_pid_path(cfg, "backend")
    backend_pid_path.parent.mkdir(parents=True, exist_ok=True)
    backend_pid_path.write_text(str(os.getpid()))
    assert "backend" in _running_services(cfg)
    assert "frontend" not in _running_services(cfg)


def test_git_working_tree_clean_returns_false_when_not_a_repo(tmp_path: Path) -> None:
    # tmp_path is not a git repo, so `git status` exits non-zero.
    assert _git_working_tree_clean(tmp_path) is False


def test_git_working_tree_clean_returns_true_for_fresh_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=tmp_path, check=True)
    assert _git_working_tree_clean(tmp_path) is True


def test_git_working_tree_clean_returns_false_when_file_added(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "dirty.txt").write_text("hello")
    assert _git_working_tree_clean(tmp_path) is False


def _runner() -> CliRunner:
    return CliRunner()


def test_logs_rejects_unknown_service(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["logs", "nonsense"])
    assert result.exit_code == 2
    assert "Unknown service" in result.stdout


def test_logs_reports_missing_file_when_service_never_started(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["logs", "backend"])
    assert result.exit_code == 1
    assert "No log yet" in result.stdout


def test_logs_tails_existing_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    log_path = _service_log_path(cfg, "backend")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"line-{i}" for i in range(1, 6))
    log_path.write_text(body + "\n")
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["logs", "backend", "--tail", "3"])
    assert result.exit_code == 0
    for expected in ("line-3", "line-4", "line-5"):
        assert expected in result.stdout
    assert "line-1" not in result.stdout


def test_backup_produces_tarball_with_database_and_vault(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    note = cfg.paths.vault_dir / "Meetings" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# hello\n")
    destination = tmp_path / "out" / "snapshot.tar.gz"
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["backup", "--out", str(destination)])
    assert result.exit_code == 0, result.stdout
    assert destination.exists()
    import tarfile

    with tarfile.open(destination, "r:gz") as tar:
        names = tar.getnames()
    assert any(name.endswith("meetingmind.sqlite3") for name in names)
    assert any("note.md" in name for name in names)


def test_backup_exits_nonzero_when_nothing_to_archive(tmp_path: Path) -> None:
    import shutil as _shutil

    cfg = _make_config(tmp_path)
    cfg.paths.database_path.unlink()
    _shutil.rmtree(cfg.paths.vault_dir)
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["backup", "--out", str(tmp_path / "empty.tgz")])
    assert result.exit_code == 1
    assert "Nothing to back up" in result.stdout


def test_reset_requires_a_target_flag(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["reset", "--yes"])
    assert result.exit_code == 2
    assert "Nothing to do" in result.stdout


def test_reset_inbox_only_wipes_inbox(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    inbox_file = cfg.paths.inbox_dir / "leftover.m4a"
    inbox_file.write_bytes(b"audio")
    vault_file = cfg.paths.vault_dir / "Meetings" / "keep.md"
    vault_file.parent.mkdir(parents=True, exist_ok=True)
    vault_file.write_text("survive")
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["reset", "--inbox", "--yes"])
    assert result.exit_code == 0
    assert not inbox_file.exists()
    # The vault is *never* touched by reset, no matter the flags.
    assert vault_file.exists()


def test_reset_everything_wipes_runtime_but_preserves_vault(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    vault_file = cfg.paths.vault_dir / "Meetings" / "keep.md"
    vault_file.parent.mkdir(parents=True, exist_ok=True)
    vault_file.write_text("survive")
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["reset", "--everything", "--yes"])
    assert result.exit_code == 0
    assert not cfg.paths.database_path.exists()
    assert vault_file.exists()


def test_upgrade_dry_run_does_not_run_subprocesses(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg), patch(
        "app.cli._git_working_tree_clean", return_value=True
    ), patch("app.cli.subprocess.run") as fake_run:
        result = _runner().invoke(
            cli,
            ["upgrade", "--dry-run", "--no-restart"],
        )
    assert result.exit_code == 0, result.stdout
    fake_run.assert_not_called()
    assert "dry-run" in result.stdout.lower()


def test_upgrade_refuses_pull_when_tree_dirty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg), patch(
        "app.cli._git_working_tree_clean", return_value=False
    ):
        result = _runner().invoke(cli, ["upgrade", "--no-deps", "--no-restart"])
    assert result.exit_code == 2
    assert "Working tree is dirty" in result.stdout


def test_start_exits_when_database_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.database_path.unlink()
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["start", "--backend"])
    assert result.exit_code == 2
    assert "No MeetingMind database" in result.stdout
    assert "meetingmind install" in result.stdout


def test_doctor_export_writes_a_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    destination = tmp_path / "doctor.txt"
    with patch("app.cli.load_config", return_value=cfg):
        result = _runner().invoke(cli, ["doctor", "--export", str(destination)])
    assert result.exit_code == 0, result.stdout
    assert destination.exists()
    body = destination.read_text()
    assert "MeetingMind Doctor" in body


def test_module_imports_cleanly() -> None:
    assert callable(logs)
    assert callable(backup)
    assert callable(reset)
    assert sys.modules["app.cli"] is not None


def test_run_command_prints_invocation_when_verbose(capsys, tmp_path: Path) -> None:
    from app import cli as cli_module

    with patch.object(cli_module, "VERBOSE", True):
        ok = cli_module._run_command(["true"])
    assert ok is True
    # rich routes through stdout; CliRunner isn't involved here, so we
    # poll the actual fd via capsys.
    out = capsys.readouterr().out
    assert "true" in out
    assert "$" in out  # the dim "$ <cmd>" prompt prefix


def test_run_command_reports_missing_binary_hint(capsys) -> None:
    from app import cli as cli_module

    ok = cli_module._run_command(["this-binary-definitely-does-not-exist"])
    assert ok is False
    out = capsys.readouterr().out
    assert "Binary not found" in out


def test_upgrade_no_dry_run_invokes_run_command(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with patch("app.cli.load_config", return_value=cfg), patch(
        "app.cli._git_working_tree_clean", return_value=True
    ), patch("app.cli._run_command", return_value=True) as fake_run, patch(
        "app.cli._running_services", return_value=[]
    ):
        result = _runner().invoke(
            cli, ["upgrade", "--no-deps", "--no-restart", "--no-check"]
        )
    assert result.exit_code == 0, result.stdout
    # When dry-run is OFF, the `git pull --ff-only` invocation must reach
    # _run_command. This is the inverse of the dry-run test above.
    assert fake_run.called
    pulled_command = fake_run.call_args_list[0].args[0]
    assert pulled_command[:2] == ["git", "pull"]


def test_doctor_export_restores_console_even_on_error(tmp_path: Path) -> None:
    """If --export's destination directory can't be created, the module-level
    `console` must still be restored to its original value so the next CLI
    invocation in the same process renders to the real terminal.
    """
    from app import cli as cli_module

    cfg = _make_config(tmp_path)
    sentinel = cli_module.console
    bad_destination = tmp_path / "definitely-readonly-parent" / "doctor.txt"
    # Stub _doctor_body to no-op so we can isolate the restoration path.
    with patch("app.cli.load_config", return_value=cfg), patch(
        "app.cli._doctor_body"
    ), patch.object(Path, "write_text", side_effect=OSError("nope")):
        result = _runner().invoke(cli, ["doctor", "--export", str(bad_destination)])
    # The wrapper catches the OSError and reports it; the command still
    # exits cleanly so the console-restoration path runs.
    assert result.exit_code == 0
    assert cli_module.console is sentinel
