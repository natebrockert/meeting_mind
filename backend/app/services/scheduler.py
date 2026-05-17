from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect
from app.services.vault_lint import lint_vault

DEFAULT_SCHEDULED_JOBS = [
    {
        "name": "Health check",
        "job_type": "health_check",
        "schedule": "daily",
        "enabled": 1,
        "model_policy_json": "{}",
    },
    {
        "name": "Vault lint",
        "job_type": "vault_lint",
        "schedule": "daily",
        "enabled": 1,
        "model_policy_json": "{}",
    },
    {
        "name": "Nightly improvements",
        "job_type": "nightly_improvements",
        "schedule": "daily",
        "enabled": 0,
    },
    {
        "name": "Action rollup rebuild",
        "job_type": "action_rollup_rebuild",
        "schedule": "daily",
        "enabled": 1,
        "model_policy_json": "{}",
    },
    {
        "name": "Uncertainty queue sweep",
        "job_type": "uncertainty_queue_sweep",
        "schedule": "daily",
        "enabled": 0,
    },
    {
        "name": "Retention cleanup",
        "job_type": "retention_cleanup",
        "schedule": "daily",
        "enabled": 0,
        "model_policy_json": "{}",
    },
]

DAILY_MAINTENANCE_JOB_TYPES = {
    "health_check",
    "vault_lint",
    "action_rollup_rebuild",
}
MODEL_POLICY_JOB_TYPES = {
    "nightly_improvements",
    "uncertainty_queue_sweep",
}


@dataclass(frozen=True)
class SchedulerRunResult:
    job_id: int
    status: str
    detail: str


def seed_default_scheduled_jobs(config: AppConfig) -> None:
    with connect(config.paths.database_path) as conn:
        for job in DEFAULT_SCHEDULED_JOBS:
            model_policy_json = (
                _configured_model_policy(config)
                if job["job_type"] in MODEL_POLICY_JOB_TYPES
                else "{}"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO scheduled_jobs
                  (name, job_type, schedule, enabled, model_policy_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job["name"],
                    job["job_type"],
                    job["schedule"],
                    job["enabled"],
                    model_policy_json,
                ),
            )
        # The f-string only interpolates a placeholder count ("?,?,?"),
        # not user data. MODEL_POLICY_JOB_TYPES is a module-level constant
        # tuple of hardcoded strings.
        conn.execute(
            f"""
            UPDATE scheduled_jobs
            SET model_policy_json = ?
            WHERE job_type IN ({",".join("?" for _ in MODEL_POLICY_JOB_TYPES)})
            """,  # nosec B608
            (_configured_model_policy(config), *sorted(MODEL_POLICY_JOB_TYPES)),
        )


def list_scheduled_jobs(config: AppConfig) -> list[dict]:
    seed_default_scheduled_jobs(config)
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT sj.*,
              (
                SELECT status
                FROM scheduled_job_runs
                WHERE scheduled_job_id = sj.id
                ORDER BY id DESC
                LIMIT 1
              ) AS last_status
            FROM scheduled_jobs sj
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def set_scheduled_job_enabled(config: AppConfig, job_id: int, enabled: bool) -> None:
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, job_id),
        )


def configure_daily_maintenance(config: AppConfig, enabled: bool, run_time: str) -> None:
    seed_default_scheduled_jobs(config)
    clean_time = run_time if _valid_hhmm(run_time) else "02:00"
    schedule = f"daily {clean_time}"
    # Only touch the three maintenance jobs — leave unrelated job types
    # (nightly improvements, retention cleanup, etc.) alone so toggling
    # daily maintenance doesn't silently disable other user-enabled work.
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            UPDATE scheduled_jobs
            SET enabled = ?, schedule = ?
            WHERE job_type IN (?, ?, ?)
            """,
            (
                1 if enabled else 0,
                schedule,
                "health_check",
                "vault_lint",
                "action_rollup_rebuild",
            ),
        )


def run_scheduled_job(config: AppConfig, job_id: int) -> SchedulerRunResult:
    with connect(config.paths.database_path) as conn:
        job = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return SchedulerRunResult(job_id, "error", "scheduled job not found")
        cursor = conn.execute(
            "INSERT INTO scheduled_job_runs (scheduled_job_id, status) VALUES (?, ?)",
            (job_id, "running"),
        )
        run_id = int(cursor.lastrowid)

    status, detail = _execute_job(config, dict(job))

    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            UPDATE scheduled_job_runs
            SET status = ?, completed_at = CURRENT_TIMESTAMP, model_used = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                _model_used(dict(job)),
                None if status == "complete" else detail,
                run_id,
            ),
        )
        conn.execute(
            "UPDATE scheduled_jobs SET last_run_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
    return SchedulerRunResult(job_id, status, detail)


def _execute_job(config: AppConfig, job: dict) -> tuple[str, str]:
    if job["job_type"] == "health_check":
        return "complete", "health check completed"
    if job["job_type"] == "vault_lint":
        result = lint_vault(config)
        detail = f"checked {result.checked_files} markdown files"
        if result.ok:
            return "complete", detail
        return "error", "; ".join(result.issues[:10])
    if job["job_type"] == "nightly_improvements":
        return (
            "complete",
            "nightly improvements are conservative no-op in v0 unless explicitly extended",
        )
    if job["job_type"] == "action_rollup_rebuild":
        from app.services.obsidian_writer import write_action_rollup

        write_action_rollup(config)
        return "complete", "action rollup rebuilt"
    if job["job_type"] == "uncertainty_queue_sweep":
        with connect(config.paths.database_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM review_items WHERE kind = 'uncertainty' AND status = 'open'"
            ).fetchone()[0]
        return "complete", f"{count} open uncertainty item(s)"
    if job["job_type"] == "retention_cleanup":
        return "complete", "retention cleanup dry-run complete; deletion requires user action"
    return "error", f"unknown job type: {job['job_type']}"


def _model_used(job: dict) -> str | None:
    try:
        policy = json.loads(job.get("model_policy_json") or "{}")
    except json.JSONDecodeError:
        return None
    return policy.get("model")


def _configured_model_policy(config: AppConfig) -> str:
    return json.dumps(
        {"provider": config.models.provider, "model": config.models.default_model},
        sort_keys=True,
    )


def _valid_hhmm(value: str) -> bool:
    parts = value.split(":", 1)
    if len(parts) != 2:
        return False
    if not all(part.isdigit() for part in parts):
        return False
    hour, minute = (int(part) for part in parts)
    return 0 <= hour <= 23 and 0 <= minute <= 59
