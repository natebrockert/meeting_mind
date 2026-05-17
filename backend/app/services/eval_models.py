"""A/B eval harness for the structured extraction pipeline.

Runs the same meeting through N different OpenRouter models so you can
compare outputs side-by-side before committing to a default. Captures:

- Wall-clock per stage (atoms / drivers+enrichment / key_terms / reflections)
- Full JSON outputs (atoms, drivers, key_terms, reflections) per model
- Output line counts as a coarse "did the model produce useful structure" signal
- A diff-friendly directory layout so you can `diff` outputs side-by-side

Usage:

    mm eval-models 6 --models "x-ai/grok-4.1-fast,openai/gpt-oss-120b"

Outputs are written to `runtime/eval-models/{meeting_id}/{model_slug}/`
along with a top-level `summary.json` for the run. Existing outputs are
not overwritten by default — pass `--force` to redo a model.

This intentionally runs serially across models so token usage and timings
stay comparable. Within a model, the extraction pipeline still uses its
own parallelization (the `_precompute_downstream_caches` ThreadPool).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import AppConfig
from app.db.database import PER_MEETING_LLM_CACHE_TABLES, connect


@dataclass
class StageTiming:
    """Wall-clock for one stage of the pipeline."""

    stage: str
    seconds: float
    ok: bool
    error: str | None = None


@dataclass
class ModelRun:
    """Result of running extraction with a single model."""

    model_id: str
    output_dir: Path
    timings: list[StageTiming]
    total_seconds: float
    ok: bool
    error: str | None = None


def run_model_ab(
    config: AppConfig,
    meeting_id: int,
    model_ids: list[str],
    output_root: Path | None = None,
    force: bool = False,
) -> list[ModelRun]:
    """Run extraction once per model and snapshot outputs.

    Clears the relevant LLM caches before each model so we're comparing
    fresh runs, not cached output from a previous run. Restores the
    config's original `default_model` / `quality_model` after each pass
    so the eval doesn't permanently mutate the user's settings.

    `force=True` re-runs any model whose output already exists; otherwise
    we skip and reuse the prior snapshot — useful when adding a new
    model to an existing comparison without re-paying for the others.
    """
    if not model_ids:
        return []

    output_root = output_root or (config.paths.runtime_dir / "eval-models")
    target = output_root / str(meeting_id)
    target.mkdir(parents=True, exist_ok=True)

    original_default = config.models.default_model
    original_quality = config.models.quality_model

    runs: list[ModelRun] = []
    try:
        for model_id in model_ids:
            run = _run_one_model(config, meeting_id, model_id, target, force=force)
            runs.append(run)
    finally:
        config.models.default_model = original_default
        config.models.quality_model = original_quality

    _write_summary(target, runs)
    return runs


def _run_one_model(
    config: AppConfig,
    meeting_id: int,
    model_id: str,
    target_dir: Path,
    force: bool,
) -> ModelRun:
    """Drive one model through atoms → drivers → key_terms → reflections."""
    slug = _model_slug(model_id)
    out_dir = target_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    completed_marker = out_dir / "completed.json"
    if completed_marker.exists() and not force:
        existing = json.loads(completed_marker.read_text())
        return ModelRun(
            model_id=model_id,
            output_dir=out_dir,
            timings=[StageTiming(**t) for t in existing.get("timings", [])],
            total_seconds=float(existing.get("total_seconds", 0.0)),
            ok=bool(existing.get("ok", True)),
            error=existing.get("error"),
        )

    # Configure the running process to use this model for everything that
    # routes through ModelBus. We deliberately point both `default_model`
    # and `quality_model` at the same id so the eval is fully attributable
    # to one model — the production config can keep them split.
    config.models.default_model = model_id
    config.models.quality_model = model_id

    _clear_caches_for_meeting(config, meeting_id)

    timings: list[StageTiming] = []
    overall_ok = True
    overall_error: str | None = None
    total_start = time.time()

    # --- 1. Atoms (drives the whole structured extract) ---
    atoms_t = _time_stage(
        "atoms",
        lambda: _run_atoms(config, meeting_id, out_dir),
        timings,
    )
    if not atoms_t.ok:
        overall_ok = False
        overall_error = atoms_t.error
        # If atoms failed with an authentication-class error (bad API
        # key, expired token, etc.) every downstream stage will fail
        # identically — they all route through the same model bus.
        # Skip them so the user doesn't pay wall-clock time across N
        # models × M stages to learn the same "your key is wrong"
        # answer. Other atom failures (transient timeout, bad model
        # id) might be stage-specific, so we still run downstream
        # stages to surface a richer error.
        if _looks_like_auth_failure(atoms_t.error):
            timings.append(
                StageTiming(
                    stage="early_abort",
                    seconds=0.0,
                    ok=False,
                    error=(
                        "atoms failed with an auth-class error; skipping "
                        "drivers / key_terms / reflections to save wall-clock "
                        "across remaining models. Fix the API key and re-run."
                    ),
                )
            )
            total_seconds = time.time() - total_start
            completed_marker.write_text(
                json.dumps(
                    {
                        "model_id": model_id,
                        "ok": False,
                        "error": overall_error,
                        "timings": [asdict(t) for t in timings],
                        "total_seconds": total_seconds,
                        "early_abort": True,
                    },
                    indent=2,
                )
            )
            return ModelRun(
                model_id=model_id,
                output_dir=out_dir,
                timings=timings,
                total_seconds=total_seconds,
                ok=False,
                error=overall_error,
            )

    # --- 2. Drivers + enrichment (only if atoms ok) ---
    if atoms_t.ok:
        _time_stage(
            "drivers_and_enrichment",
            lambda: _run_drivers(config, meeting_id, out_dir),
            timings,
        )

    # --- 3. Key terms (independent; runs even if drivers failed) ---
    _time_stage(
        "key_terms",
        lambda: _run_key_terms(config, meeting_id, out_dir),
        timings,
    )

    # --- 4. Reflections (only if owner configured) ---
    if config.experimental.reflections_enabled and config.owner.person_id:
        _time_stage(
            "reflections",
            lambda: _run_reflections(config, meeting_id, out_dir),
            timings,
        )
    else:
        timings.append(
            StageTiming(
                stage="reflections",
                seconds=0.0,
                ok=True,
                error="skipped: no owner or flag off",
            )
        )

    total_seconds = time.time() - total_start
    completed_marker.write_text(
        json.dumps(
            {
                "model_id": model_id,
                "ok": overall_ok,
                "error": overall_error,
                "timings": [asdict(t) for t in timings],
                "total_seconds": total_seconds,
            },
            indent=2,
        )
    )
    return ModelRun(
        model_id=model_id,
        output_dir=out_dir,
        timings=timings,
        total_seconds=total_seconds,
        ok=overall_ok,
        error=overall_error,
    )


def _time_stage(name: str, fn, timings: list[StageTiming]) -> StageTiming:
    start = time.time()
    try:
        fn()
        elapsed = time.time() - start
        record = StageTiming(stage=name, seconds=elapsed, ok=True)
    except Exception as exc:  # noqa: BLE001 — eval must continue across stages
        elapsed = time.time() - start
        record = StageTiming(
            stage=name,
            seconds=elapsed,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    timings.append(record)
    return record


def _run_atoms(config: AppConfig, meeting_id: int, out_dir: Path) -> None:
    from app.services.extraction import extract_meeting_atoms

    atoms = extract_meeting_atoms(config, meeting_id)
    (out_dir / "atoms.json").write_text(atoms.model_dump_json(indent=2))


def _run_drivers(config: AppConfig, meeting_id: int, out_dir: Path) -> None:
    from app.services.conversation_drivers import compute_drivers_and_cog

    drivers, cog = compute_drivers_and_cog(config, meeting_id)
    (out_dir / "drivers.json").write_text(
        json.dumps(
            {
                "drivers": [d.model_dump() for d in drivers],
                "center_of_gravity": cog.model_dump(),
            },
            indent=2,
        )
    )


def _run_key_terms(config: AppConfig, meeting_id: int, out_dir: Path) -> None:
    from app.services.synthesis import build_synthesis_snapshot

    snapshot = build_synthesis_snapshot(config, meeting_id)
    (out_dir / "synthesis.json").write_text(json.dumps(snapshot, indent=2, default=str))


def _run_reflections(config: AppConfig, meeting_id: int, out_dir: Path) -> None:
    from app.services.reflections import compute_reflections

    result = compute_reflections(config, meeting_id)
    if result is None:
        (out_dir / "reflections.json").write_text(json.dumps({"skipped": "flag_off"}, indent=2))
        return
    (out_dir / "reflections.json").write_text(result.model_dump_json(indent=2))


def _clear_caches_for_meeting(config: AppConfig, meeting_id: int) -> None:
    """Drop the per-meeting LLM cache rows so the model under test is
    actually exercised, not just replayed from a previous run.

    Sources the table list from `PER_MEETING_LLM_CACHE_TABLES` in
    `app.db.database` so a future cache table added without updating
    this function won't silently corrupt A/B results with stale data.
    """
    with connect(config.paths.database_path) as conn:
        for table in PER_MEETING_LLM_CACHE_TABLES:
            # `table` interpolates from a hardcoded module-level tuple in
            # app.db.database — never user input — so the f-string is
            # safe. Bandit can't follow the constant; pin the suppression
            # to B608 specifically on the offending line.
            query = f"DELETE FROM {table} WHERE meeting_id = ?"  # nosec B608
            conn.execute(query, (meeting_id,))


def _looks_like_auth_failure(error_text: str | None) -> bool:
    """Heuristic: does this error string indicate the API rejected
    every call because of bad auth, vs. a stage-specific failure?

    We're conservative on purpose. False positives skip downstream
    stages unnecessarily (annoying but recoverable); false negatives
    re-pay wall-clock for every stage of every remaining model
    (expensive). When in doubt, return False so we keep running.

    Only word-form auth tokens — bare HTTP status codes like "401" or
    "403" appear in plenty of non-auth contexts (JSON position offsets,
    line numbers, port fragments) and would false-positive on any
    error string containing those digit sequences. Every real auth
    error from OpenRouter / Anthropic / OpenAI / httpx also includes
    one of these word tokens, so we don't lose coverage.
    """
    if not error_text:
        return False
    needle = error_text.lower()
    return any(
        token in needle
        for token in (
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "authentication",
            "auth failed",
            "no auth credentials",
            "api key not set",
        )
    )


def _model_slug(model_id: str) -> str:
    """Map "x-ai/grok-4.1-fast" → "x-ai__grok-4.1-fast" so it's a valid
    directory name on every filesystem.
    """
    return model_id.replace("/", "__").replace(":", "_")


def _write_summary(target_dir: Path, runs: list[ModelRun]) -> None:
    """Per-meeting summary.json with one row per model, plus a markdown
    table you can paste into a PR / slack message.
    """
    rows = []
    for run in runs:
        atoms_path = run.output_dir / "atoms.json"
        atoms_summary: dict[str, int] = {}
        if atoms_path.exists():
            data = json.loads(atoms_path.read_text())
            atoms_summary = {
                "actions": len(data.get("actions") or []),
                "decisions": len(data.get("decisions") or []),
                "open_questions": len(data.get("open_questions") or []),
                "workstreams": len(data.get("workstreams") or []),
                "themes": len(data.get("themes") or []),
                "key_takeaways": len(data.get("key_takeaways") or []),
            }
        drivers_path = run.output_dir / "drivers.json"
        drivers_count = None
        if drivers_path.exists():
            drivers_count = len(json.loads(drivers_path.read_text()).get("drivers") or [])
        rows.append(
            {
                "model_id": run.model_id,
                "ok": run.ok,
                "error": run.error,
                "total_seconds": run.total_seconds,
                "timings": [asdict(t) for t in run.timings],
                "atoms": atoms_summary,
                "drivers_count": drivers_count,
            }
        )
    (target_dir / "summary.json").write_text(json.dumps(rows, indent=2))
