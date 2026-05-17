from __future__ import annotations

from pathlib import Path

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.scheduler import (
    configure_daily_maintenance,
    list_scheduled_jobs,
    run_scheduled_job,
    set_scheduled_job_enabled,
)


def test_scheduler_defaults_and_vault_lint_job(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    jobs = list_scheduled_jobs(cfg)

    assert [job["name"] for job in jobs] == [
        "Health check",
        "Vault lint",
        "Nightly improvements",
        "Action rollup rebuild",
        "Uncertainty queue sweep",
        "Retention cleanup",
    ]
    assert jobs[0]["enabled"] == 1
    assert jobs[1]["enabled"] == 1
    assert jobs[2]["enabled"] == 0

    set_scheduled_job_enabled(cfg, int(jobs[2]["id"]), True)
    updated = list_scheduled_jobs(cfg)
    assert updated[2]["enabled"] == 1

    result = run_scheduled_job(cfg, int(jobs[1]["id"]))
    assert result.status == "complete"


def test_daily_maintenance_collapses_enabled_jobs_and_time(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    configure_daily_maintenance(cfg, enabled=True, run_time="03:30")
    jobs = list_scheduled_jobs(cfg)
    enabled_by_type = {job["job_type"]: job["enabled"] for job in jobs}
    schedule_by_type = {job["job_type"]: job["schedule"] for job in jobs}

    assert enabled_by_type["health_check"] == 1
    assert enabled_by_type["vault_lint"] == 1
    assert enabled_by_type["action_rollup_rebuild"] == 1
    # Maintenance toggle must not silently disable unrelated jobs.
    assert enabled_by_type["nightly_improvements"] == 0
    # Only the three maintenance jobs share the configured schedule;
    # other jobs keep their seeded "daily" default.
    assert schedule_by_type["health_check"] == "daily 03:30"
    assert schedule_by_type["vault_lint"] == "daily 03:30"
    assert schedule_by_type["action_rollup_rebuild"] == "daily 03:30"
    assert schedule_by_type["nightly_improvements"] == "daily"

    configure_daily_maintenance(cfg, enabled=False, run_time="99:99")
    jobs = list_scheduled_jobs(cfg)
    enabled_by_type = {job["job_type"]: job["enabled"] for job in jobs}
    schedule_by_type = {job["job_type"]: job["schedule"] for job in jobs}
    assert enabled_by_type["health_check"] == 0
    assert enabled_by_type["vault_lint"] == 0
    assert enabled_by_type["action_rollup_rebuild"] == 0
    assert schedule_by_type["health_check"] == "daily 02:00"


def test_scheduler_model_policy_tracks_configured_provider(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    cfg.models.provider = "ollama"
    cfg.models.default_model = "gpt-oss:20b"
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    jobs = list_scheduled_jobs(cfg)
    model_jobs = {
        job["job_type"]: job["model_policy_json"]
        for job in jobs
        if job["job_type"] in {"nightly_improvements", "uncertainty_queue_sweep"}
    }

    assert all("gpt-oss:20b" in policy for policy in model_jobs.values())
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET model_policy_json = ? WHERE job_type = ?",
            ('{"provider":"lm_studio","model":"stale"}', "nightly_improvements"),
        )
    refreshed = list_scheduled_jobs(cfg)
    nightly = next(job for job in refreshed if job["job_type"] == "nightly_improvements")
    assert "gpt-oss:20b" in nightly["model_policy_json"]


def _test_config(tmp_path: Path) -> AppConfig:
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
    return AppConfig(config_path=tmp_path / "config" / "local.toml", paths=paths)
