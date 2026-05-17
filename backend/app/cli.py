from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.config import (
    AppConfig,
    LegacyDataLocationError,
    load_config,
    save_config,
)
from app.config import (
    ensure_local_layout as _raw_ensure_local_layout,
)
from app.db.database import initialize_database
from app.services.asr_vocabulary import load_custom_vocabulary_terms
from app.services.model_bus import list_lm_studio_models, list_ollama_models
from app.services.scheduler import run_scheduled_job, seed_default_scheduled_jobs
from app.services.vault_lint import lint_vault

console = Console()
_LOGGER = logging.getLogger(__name__)


def ensure_local_layout(config: AppConfig) -> None:
    """Wrap `app.config.ensure_local_layout` so any `LegacyDataLocationError`
    surfaces as a clean Rich-formatted message + non-zero exit, not as a
    raw Python traceback. Every CLI command that builds the runtime
    layout (install, start, dev, restart, reset, ingest, etc.) goes
    through this shim so the user sees the actionable recovery steps
    rather than a stack trace.

    Non-CLI callers (FastAPI startup, ingestion library calls, tests)
    should keep using `app.config.ensure_local_layout` directly — they
    have their own exception-presentation strategies (FastAPI exception
    handler, library-level propagation, pytest.raises).
    """
    try:
        _raw_ensure_local_layout(config)
    except LegacyDataLocationError as exc:
        console.print(
            "\n[red]MeetingMind cannot start: legacy data layout detected.[/red]\n"
            f"{exc}\n"
        )
        raise typer.Exit(2) from exc
cli = typer.Typer(help="MeetingMind local CLI")

# Module-level verbose flag so individual helpers can opt into printing the
# raw command they're about to run without piping a flag through every layer.
VERBOSE = False


def _set_verbose(value: bool) -> None:
    global VERBOSE
    VERBOSE = value


@cli.callback()
def _root(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print every subprocess command before it runs and surface cwd in errors.",
    ),
) -> None:
    """MeetingMind CLI root callback — wires global flags before subcommands."""
    _set_verbose(verbose)

DEFAULT_OLLAMA_MODEL = "gemma3:4b"
DEFAULT_LM_STUDIO_MODEL_QUERY = "gemma-4-e4b-it@q4_k_m"
PYANNOTE_ACCESS_URLS = [
    "https://huggingface.co/pyannote/speaker-diarization-community-1",
    "https://huggingface.co/pyannote/embedding",
]
# The lifecycle commands only control services started by this CLI.
MANAGED_SERVICES = ("backend", "frontend")
# Three call sites depend on this tuple staying in lockstep with the
# `_selected_services` / `_running_services` helpers and the `status` /
# `restart` / `upgrade --restart` commands. Treat it as the single source
# of truth for "managed lifecycle service"; resist the urge to turn it
# into a set or pull it from config without auditing the helpers above.


def _check_binary(name: str) -> str:
    return shutil.which(name) or ""


def _total_ram_gb() -> float | None:
    """Return total physical RAM in GB, or None if we can't tell.

    Uses `sysctl hw.memsize` on darwin and `os.sysconf` on Linux. No new
    dependency required — psutil would work too but isn't worth pulling
    in for one read.
    """
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
            return int(result.stdout.strip()) / (1024**3)
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) / (1024**3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _check_memory(
    table,
    next_steps: list[str],
    cfg,
    fixable_actions: list[tuple[str, str, object]] | None = None,
) -> None:
    """Doctor row for physical RAM with a low-memory nudge.

    The default Whisper model (whisper-large-v3-turbo) peaks around
    3-4 GB on Apple Silicon. Combined with pyannote + the dashboard +
    the OS, that easily swap-thrashes an 8 GB machine. Surface a
    suggestion to switch to whisper-medium on tight hardware so testers
    don't get a degraded experience with no idea why.
    """
    ram_gb = _total_ram_gb()
    if ram_gb is None:
        return
    detail = f"{ram_gb:.1f} GB physical"
    using_large_whisper = "large" in (cfg.asr.model_name or "")
    if ram_gb < 12 and using_large_whisper:
        table.add_row("memory", "[yellow]tight[/yellow]", detail)
        next_steps.append(
            _dependency_hint(
                "Low memory",
                "switch to `mlx-community/whisper-medium-mlx` in config/local.toml "
                "or Settings → ASR to avoid swap thrashing on <12 GB machines.",
            )
        )
        if fixable_actions is not None:
            fixable_actions.append(
                (
                    "Switch ASR model to whisper-medium-mlx (lower memory)",
                    "config/local.toml [asr] model_name = mlx-community/whisper-medium-mlx",
                    lambda: _swap_asr_model(cfg, "mlx-community/whisper-medium-mlx"),
                )
            )
    else:
        table.add_row("memory", "[green]info[/green]", detail)


def _swap_asr_model(cfg, new_model_name: str) -> bool:
    """Persist an ASR model swap to config/local.toml without clobbering
    other settings. Re-reads + writes via save_config so the rest of
    the config is preserved exactly as the user left it.
    """
    cfg.asr.model_name = new_model_name
    save_config(cfg)
    console.print(
        f"[green]ASR model set to[/green] [bold]{new_model_name}[/bold]. "
        "First ingest after restart will download the model (~700 MB)."
    )
    return True


def _has_hf_token() -> bool:
    """True if a HF token is in env (loaded from .env.local by the wizard)."""
    return bool(
        os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_TOKEN", "").strip()
    )


def _pyannote_models_cached() -> bool:
    """Cheap probe: are both gated pyannote model directories on disk?"""
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    return all(
        (hf_cache / f"models--pyannote--{name}").exists()
        for name in ("speaker-diarization-community-1", "embedding")
    )


def _wespeaker_model_cached() -> bool:
    """Cheap probe: is the WeSpeaker ONNX cached locally?

    Both wespeakerruntime AND FoxNoseTech's `diarize` use this path. If
    the file is here, neither library hits the Tencent CDN.
    """
    cache_path = Path.home() / ".wespeaker" / "en" / "model.onnx"
    try:
        return cache_path.exists() and cache_path.stat().st_size > 1_000_000
    except OSError:
        return False


def _prewarm_wespeaker_model() -> None:
    """Download the WeSpeaker speaker-embedding ONNX from the public HF
    mirror. Default lite-stack diarizer (FoxNoseTech) needs this; the
    rename-once embedding provider does too. ~25 MB, one-time per machine.
    """
    from app.services.diarization.wespeaker_embedding_provider import (
        _ensure_model_cached,
    )

    cache_path = _ensure_model_cached()
    console.print(f"[green]WeSpeaker ONNX cached:[/green] {cache_path}")


def _prewarm_pyannote_models() -> None:
    """Trigger HuggingFace download of both gated pyannote models so the
    first ingest doesn't stall. Runs the import in a subprocess so we don't
    drag torch into the doctor's main process if the user is on the
    `ml-cpu` path or similar."""
    preload_script = (
        "import os; "
        "from pyannote.audio import Model, Pipeline; "
        "token=os.getenv('HUGGING_FACE_HUB_TOKEN') or os.getenv('HUGGING_FACE_TOKEN'); "
        "Pipeline.from_pretrained("
        "'pyannote/speaker-diarization-community-1', token=token); "
        "Model.from_pretrained('pyannote/embedding', token=token); "
        "print('pyannote models cached')"
    )
    _run_command(
        ["uv", "run", "python", "-c", preload_script],
        timeout=900,
    )


def _check_local_models(
    table,
    next_steps: list[str],
    cfg,
    models: list[str],
) -> None:
    """Doctor sub-check for LM Studio / Ollama: is the configured model
    actually present in the local model inventory? OpenRouter has its
    own check upstream and doesn't use this helper."""
    default_model_ready = cfg.models.default_model in models
    quality_model_ready = cfg.models.quality_model in models
    table.add_row(
        "primary model",
        _status(default_model_ready),
        cfg.models.default_model,
    )
    table.add_row(
        "quality model",
        _optional_status(quality_model_ready),
        cfg.models.quality_model,
    )
    if not default_model_ready:
        next_steps.append(
            _dependency_hint(
                "Primary model",
                "select an installed model in dashboard Settings or config/local.toml.",
            )
        )


def _status(value: bool) -> str:
    return "[green]ok[/green]" if value else "[red]missing[/red]"


def _optional_status(value: bool) -> str:
    return "[green]ok[/green]" if value else "[yellow]optional[/yellow]"


def _dependency_hint(label: str, detail: str) -> str:
    return f"[bold]{label}[/bold]: {detail}"


def _module_available(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def _merge_env_file_text(existing_text: str, updates: dict[str, str]) -> str:
    """Merge env values while preserving comments and unrelated keys."""
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            output.append(f"{normalized_key}={updates[normalized_key]}")
            seen.add(normalized_key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output).rstrip() + "\n"


def _write_env_values(env_path: Path, updates: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = env_path.read_text() if env_path.exists() else ""
    env_path.write_text(_merge_env_file_text(existing_text, updates))
    for key, value in updates.items():
        os.environ[key] = value


def _ensure_env_template(env_path: Path) -> None:
    if env_path.exists():
        return
    env_path.write_text(
        "# Local secrets stay ignored by git.\n"
        "# Add your token after accepting the pyannote Hugging Face model terms.\n"
        "HUGGING_FACE_HUB_TOKEN=\n"
    )


def _run_command(
    command: list[str],
    dry_run: bool = False,
    timeout: int | None = None,
    cwd: Path | None = None,
) -> bool:
    rendered = shlex.join(command)
    if dry_run:
        console.print(f"[cyan]Would run:[/cyan] {rendered}")
        return True
    if VERBOSE:
        # Print the full command before launch so a hang or a cryptic error
        # always has the exact invocation in scrollback for debugging.
        location = f" (cwd={cwd})" if cwd else ""
        console.print(f"[dim]$ {rendered}{location}[/dim]")
    try:
        subprocess.run(command, check=True, timeout=timeout, cwd=cwd)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        console.print(f"[red]Command failed:[/red] {rendered}")
        if isinstance(exc, subprocess.TimeoutExpired):
            console.print(
                f"[red]Timed out after {exc.timeout:.0f}s.[/red] "
                "Pass a larger timeout or fix the underlying network/installer."
            )
        elif isinstance(exc, subprocess.CalledProcessError):
            console.print(
                f"[red]Exit code {exc.returncode}.[/red] Re-run with [cyan]--verbose[/cyan] "
                "to see the exact command and cwd."
            )
        elif isinstance(exc, FileNotFoundError):
            console.print(
                f"[red]Binary not found:[/red] {command[0]}. "
                "Install it (Homebrew or your package manager) and try again."
            )
        console.print(str(exc))
        return False


def _install_with_brew(
    label: str,
    package: str,
    *,
    cask: bool = False,
    dry_run: bool = False,
) -> bool:
    if not _check_binary("brew"):
        console.print(
            f"[yellow]Homebrew is not available. Install {label} manually, "
            "then rerun doctor.[/yellow]"
        )
        return False
    command = ["brew", "install"]
    if cask:
        command.append("--cask")
    command.append(package)
    return _run_command(command, dry_run=dry_run)


def _save_openrouter_key(env_var: str, key: str, dry_run: bool) -> None:
    """Persist the user's OpenRouter API key into .env.local.

    Mirrors the /api/settings/openrouter-key endpoint so the CLI install
    flow gets the same in-place upsert behaviour. .env.local is gitignored
    so the secret never enters version control or config/local.toml.
    """
    if dry_run:
        console.print(f"  [yellow]Dry-run: would update .env.local {env_var}=...[/yellow]")
        return
    from app.config import REPO_ROOT

    path = REPO_ROOT / ".env.local"
    lines = path.read_text().splitlines() if path.exists() else []
    found = False
    rewritten: list[str] = []
    for line in lines:
        if line.lstrip().startswith(f"{env_var}="):
            rewritten.append(f"{env_var}={key}")
            found = True
        else:
            rewritten.append(line)
    if not found:
        rewritten.append(f"{env_var}={key}")
    path.write_text("\n".join(rewritten) + "\n")


def _prompt_choice(label: str, options: list[str], default: int = 1) -> int:
    console.print(f"\n[bold]{label}[/bold]")
    for index, option in enumerate(options, start=1):
        marker = " (default)" if index == default else ""
        console.print(f"  {index}. {option}{marker}")
    while True:
        raw = typer.prompt("Select", default=str(default)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        console.print(f"[yellow]Enter a number from 1 to {len(options)}.[/yellow]")


def _app_installed(app_name: str) -> bool:
    return (Path("/Applications") / f"{app_name}.app").exists() or (
        Path.home() / "Applications" / f"{app_name}.app"
    ).exists()


def _prompt_path(label: str, default: Path | None = None) -> Path:
    prompt_default = str(default.expanduser()) if default else None
    raw = typer.prompt(label, default=prompt_default).strip()
    return Path(raw).expanduser()


def _setup_core_dependencies(dry_run: bool) -> None:
    for binary_name, brew_package in {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffmpeg",
        "node": "node",
        "npm": "node",
    }.items():
        if _check_binary(binary_name):
            continue
        if typer.confirm(f"{binary_name} was not found. Install {brew_package} with Homebrew?"):
            _install_with_brew(binary_name, brew_package, dry_run=dry_run)

    # Install frontend node_modules so `meetingmind start` doesn't fail
    # silently — without this, `npm run dev` exits immediately and the
    # spawned PID looks alive in `meetingmind status` while the dashboard
    # URL never loads.
    cfg = load_config()
    frontend_dir = cfg.paths.repo_root / "frontend"
    node_modules = frontend_dir / "node_modules"
    if frontend_dir.exists() and not node_modules.exists() and _check_binary("npm"):
        console.print("[cyan]Installing frontend dependencies…[/cyan]")
        _run_command(["npm", "install"], dry_run=dry_run, cwd=frontend_dir, timeout=900)


def _setup_hugging_face(env_path: Path, dry_run: bool) -> None:
    token = os.getenv("HUGGING_FACE_HUB_TOKEN") or os.getenv("HUGGING_FACE_TOKEN")
    if token:
        console.print("[green]Hugging Face token already configured.[/green]")
    elif typer.confirm(
        "Configure Hugging Face/pyannote now? You can also set this up later.",
        default=True,
    ):
        console.print("Before entering a token, accept access for:")
        for url in PYANNOTE_ACCESS_URLS:
            console.print(f"- {url}")
        new_token = typer.prompt("Hugging Face token", hide_input=True).strip()
        if new_token:
            if dry_run:
                console.print("[cyan]Would store Hugging Face token in ignored .env.local.[/cyan]")
            else:
                _write_env_values(env_path, {"HUGGING_FACE_HUB_TOKEN": new_token})
                console.print("[green]Stored token in ignored .env.local.[/green]")

    if typer.confirm(
        "Install ML dependencies and preload pyannote models now? This can take several minutes.",
        default=False,
    ) and _run_command(["uv", "sync", "--extra", "ml"], dry_run=dry_run):
        preload_script = (
            "import os; "
            "from pyannote.audio import Model, Pipeline; "
            "token=os.getenv('HUGGING_FACE_HUB_TOKEN') or os.getenv('HUGGING_FACE_TOKEN'); "
            "Pipeline.from_pretrained("
            "'pyannote/speaker-diarization-community-1', token=token); "
            "Model.from_pretrained('pyannote/embedding', token=token); "
            "print('pyannote models cached')"
        )
        preload_command = ["uv", "run", "python", "-c", preload_script]
        _run_command(preload_command, dry_run=dry_run, timeout=900)


def _print_recommended_models(provider: str, role: str = "default") -> None:
    """Show curated tier suggestions before the install wizard model prompt.

    Saves users from guessing across the raw ``lms ls`` output. The list comes
    from RECOMMENDED_MODELS in the model bus; this is the single source of
    truth shared with the dashboard Settings page.
    """
    from app.services.model_bus import RECOMMENDED_MODELS

    relevant = [r for r in RECOMMENDED_MODELS if r["role"] == role]
    if not relevant:
        return
    console.print(
        f"\n[bold]Recommended {provider} {role} models:[/bold]"
    )
    for rec in relevant:
        tier_color = "green" if rec["tier"] == "recommended" else "yellow"
        console.print(
            f"  [{tier_color}]{rec['tier']:>11}[/{tier_color}]  "
            f"[cyan]{rec['id']}[/cyan]\n               {rec['note']}"
        )
    console.print()


def _select_model_from_pool(provider: str, models: list[str], default_model: str) -> str | None:
    _print_recommended_models(provider)
    if not models:
        return None
    options = [*models, "Use current configured model", "Enter another model id", "Set up later"]
    default = 1 if default_model not in models else models.index(default_model) + 1
    selected = _prompt_choice(f"Select {provider} model", options, default=default)
    if selected <= len(models):
        return models[selected - 1]
    option = options[selected - 1]
    if option == "Use current configured model":
        return default_model
    if option == "Enter another model id":
        return typer.prompt("Model id").strip()
    return None


def _setup_model_provider(config: AppConfig, dry_run: bool) -> None:
    has_lms = bool(_check_binary("lms"))
    has_ollama = bool(_check_binary("ollama"))
    console.print(
        "\n[bold]MeetingMind is local-first.[/bold] Audio + transcription "
        "always stay on your machine. The model provider controls only how "
        "structured extraction (summaries, action items, topics) runs.\n"
        "  • [green]LM Studio[/green] or [green]Ollama[/green] — fully local, "
        "no subscription, no data leaves your Mac.\n"
        "  • [yellow]OpenRouter[/yellow] — opt-in cloud, BYO API key, no "
        "lock-in. Only the cleaned transcript text is sent for higher-quality "
        "synthesis.\n"
    )
    options = ["LM Studio (local)", "Ollama (local)", "OpenRouter (cloud, BYO key)", "Set up later"]
    default = (
        1
        if config.models.provider == "lm_studio"
        else 2
        if config.models.provider == "ollama"
        else 3
        if config.models.provider == "openrouter"
        else 1
    )
    selected = _prompt_choice("Model provider", options, default=default)
    if selected == 4:
        console.print(
            "[yellow]Skipping model provider setup. Configure it later in Settings.[/yellow]"
        )
        return

    if selected == 3:
        config.models.provider = "openrouter"
        console.print(
            "\n[bold]OpenRouter setup[/bold]\n"
            "Create a key at [cyan]https://openrouter.ai/keys[/cyan]\n"
        )
        if typer.confirm("Paste your OpenRouter API key now?", default=True):
            key = typer.prompt(
                f"  {config.models.openrouter_api_key_env}",
                hide_input=True,
                default="",
                show_default=False,
            ).strip()
            if key:
                _save_openrouter_key(config.models.openrouter_api_key_env, key, dry_run)
                console.print(
                    f"[green]✓ Saved to .env.local as {config.models.openrouter_api_key_env}=…[/green] "
                    "(gitignored; never enters config/local.toml)"
                )
            else:
                console.print(
                    "[yellow]No key entered. Add it later from Settings → "
                    "Models, or paste into .env.local manually.[/yellow]"
                )
        # Model selection — curated short list, free option first.
        suggested = [
            "tencent/hy3-preview",
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.7",
            "openai/gpt-5",
            "openai/gpt-5-mini",
            "google/gemini-2.5-pro",
            "Enter another model id",
        ]
        chosen_index = _prompt_choice(
            "Default OpenRouter model (tencent/hy3-preview is free-tier)",
            suggested,
            default=1,
        )
        if chosen_index <= len(suggested) - 1:
            config.models.default_model = suggested[chosen_index - 1]
        else:
            config.models.default_model = (
                typer.prompt("Model id (e.g. anthropic/claude-opus-4.7)").strip()
                or config.models.openrouter_default_model
            )
        config.models.quality_model = config.models.default_model
        console.print(
            f"[green]✓ Provider set to OpenRouter with model "
            f"`{config.models.default_model}`.[/green]"
        )
        return

    provider = "lm_studio" if selected == 1 else "ollama"
    if provider == "lm_studio" and not has_lms and typer.confirm(
        "LM Studio was not found. Install LM Studio with Homebrew Cask?"
    ):
        _install_with_brew("LM Studio", "lm-studio", cask=True, dry_run=dry_run)
    if provider == "ollama" and not has_ollama and typer.confirm(
        "Ollama was not found. Install Ollama with Homebrew?"
    ):
        _install_with_brew("Ollama", "ollama", dry_run=dry_run)

    config.models.provider = provider
    if provider == "lm_studio":
        if not dry_run and not _check_binary("lms"):
            console.print(
                "[yellow]LM Studio CLI is still unavailable. Open LM Studio once, "
                "then rerun install or select a model in Settings.[/yellow]"
            )
            return
        models = list_lm_studio_models()
        selected_model = _select_model_from_pool("LM Studio", models, config.models.default_model)
        if selected_model:
            config.models.default_model = selected_model
            config.models.quality_model = selected_model
        elif typer.confirm(
            f"No LM Studio model selected. Download suggested Gemma model "
            f"`{DEFAULT_LM_STUDIO_MODEL_QUERY}` with `lms get`?",
            default=True,
        ):
            if _run_command(
                ["lms", "get", DEFAULT_LM_STUDIO_MODEL_QUERY, "--gguf", "-y"],
                dry_run=dry_run,
                timeout=3600,
            ):
                refreshed = list_lm_studio_models()
                if refreshed:
                    config.models.default_model = refreshed[0]
                    config.models.quality_model = refreshed[0]
                else:
                    config.models.default_model = DEFAULT_LM_STUDIO_MODEL_QUERY
                    config.models.quality_model = DEFAULT_LM_STUDIO_MODEL_QUERY
        return

    if not dry_run and not _check_binary("ollama"):
        console.print(
            "[yellow]Ollama CLI is still unavailable. Start Ollama, then rerun install "
            "or select a model in Settings.[/yellow]"
        )
        return
    models = list_ollama_models()
    selected_model = _select_model_from_pool("Ollama", models, config.models.default_model)
    if selected_model:
        config.models.default_model = selected_model
        config.models.quality_model = selected_model
    elif typer.confirm(
        f"No Ollama model selected. Pull suggested Gemma model `{DEFAULT_OLLAMA_MODEL}`?",
        default=True,
    ):
        if _run_command(["ollama", "pull", DEFAULT_OLLAMA_MODEL], dry_run=dry_run, timeout=3600):
            config.models.default_model = DEFAULT_OLLAMA_MODEL
            config.models.quality_model = DEFAULT_OLLAMA_MODEL


def _setup_obsidian(config: AppConfig, dry_run: bool) -> None:
    if not typer.confirm("Use Obsidian output in this setup?", default=True):
        console.print(
            "[yellow]Skipping Obsidian setup. You can configure the vault later.[/yellow]"
        )
        return

    if not _app_installed("Obsidian") and typer.confirm(
        "Obsidian was not found. Install Obsidian with Homebrew Cask?"
    ):
        _install_with_brew("Obsidian", "obsidian", cask=True, dry_run=dry_run)

    recommended = config.paths.repo_root / "vault" / "meeting_mind"
    choice = _prompt_choice(
        "Obsidian vault",
        [
            f"Create/use repo-local MeetingMind vault at {recommended}",
            "Use an existing vault",
            "Create a new vault at a different location",
            "Set up later",
        ],
        default=1,
    )
    if choice == 1:
        config.paths.vault_dir = recommended
    elif choice == 2:
        config.paths.vault_dir = _prompt_path("Existing vault path", config.paths.vault_dir)
    elif choice == 3:
        config.paths.vault_dir = _prompt_path("New vault path", recommended)
    else:
        console.print(
            "[yellow]Keeping current vault path; configure it later in Settings.[/yellow]"
        )
        return

    if dry_run:
        console.print(
            f"[cyan]Would create MeetingMind vault folders under:[/cyan] {config.paths.vault_dir}"
        )
        return
    ensure_local_layout(config)
    console.print(f"[green]MeetingMind vault folders ready:[/green] {config.paths.vault_dir}")


def _run_install_wizard(
    config: AppConfig,
    env_path: Path,
    dry_run: bool,
    *,
    skip_core: bool = False,
    skip_hugging_face: bool = False,
    skip_models: bool = False,
    skip_obsidian: bool = False,
) -> None:
    """Modular wizard so headless / CI installs can skip individual sections.

    Skipping a section prints a one-liner so the user (or a CI log) sees
    that the step was deliberately omitted.
    """
    if skip_core:
        console.print("[dim]Skipping core dependency setup (--skip-core).[/dim]")
    else:
        _setup_core_dependencies(dry_run)
    if skip_hugging_face:
        console.print("[dim]Skipping Hugging Face setup (--skip-huggingface).[/dim]")
    else:
        _setup_hugging_face(env_path, dry_run)
    if skip_models:
        console.print("[dim]Skipping model provider setup (--skip-models).[/dim]")
    else:
        _setup_model_provider(config, dry_run)
    if skip_obsidian:
        console.print("[dim]Skipping Obsidian setup (--skip-obsidian).[/dim]")
    else:
        _setup_obsidian(config, dry_run)


def _service_pid_path(config: AppConfig, service: str) -> Path:
    return config.paths.runtime_dir / f"meetingmind-{service}.pid"


def _service_log_path(config: AppConfig, service: str) -> Path:
    return config.paths.runtime_dir / "logs" / f"{service}.log"


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except ValueError:
        return None


def _process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _clear_stale_pid(pid_path: Path) -> None:
    pid = _read_pid(pid_path)
    if pid is None or not _process_running(pid):
        pid_path.unlink(missing_ok=True)


def _service_url(config: AppConfig, service: str) -> str:
    if service == "backend":
        return f"http://127.0.0.1:{config.runtime.backend_port}"
    return f"http://127.0.0.1:{config.runtime.dashboard_port}"


def _service_command(config: AppConfig, service: str) -> tuple[list[str], Path, dict[str, str]]:
    env = os.environ.copy()
    env["MEETINGMIND_BACKEND_PORT"] = str(config.runtime.backend_port)
    env["MEETINGMIND_DASHBOARD_PORT"] = str(config.runtime.dashboard_port)
    if service == "backend":
        return (
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                "backend",
                "--host",
                "127.0.0.1",
                "--port",
                str(config.runtime.backend_port),
            ],
            config.paths.repo_root,
            env,
        )
    return (
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(config.runtime.dashboard_port),
        ],
        config.paths.repo_root / "frontend",
        env,
    )


def _start_service(config: AppConfig, service: str) -> None:
    """Start one managed service in the background and record its PID/log path."""
    ensure_local_layout(config)
    pid_path = _service_pid_path(config, service)
    _clear_stale_pid(pid_path)
    existing_pid = _read_pid(pid_path)
    if _process_running(existing_pid):
        console.print(f"[yellow]{service} already running:[/yellow] pid {existing_pid}")
        return

    log_path = _service_log_path(config, service)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command, cwd, env = _service_command(config, service)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid))
    console.print(
        f"[green]Started {service}[/green] pid {process.pid} at {_service_url(config, service)}"
    )
    console.print(f"Log: {log_path}")


def _stop_service(config: AppConfig, service: str, timeout_seconds: float = 8.0) -> None:
    """Stop one managed service by PID, escalating only if it ignores SIGTERM."""
    pid_path = _service_pid_path(config, service)
    pid = _read_pid(pid_path)
    if not _process_running(pid):
        pid_path.unlink(missing_ok=True)
        console.print(f"[yellow]{service} is not running.[/yellow]")
        return
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        console.print(f"[yellow]{service} was already stopped.[/yellow]")
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_running(pid):
            pid_path.unlink(missing_ok=True)
            console.print(f"[green]Stopped {service}.[/green]")
            return
        time.sleep(0.2)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    console.print(f"[yellow]Force-stopped {service}.[/yellow]")


def _selected_services(backend: bool, frontend: bool) -> tuple[str, ...]:
    if backend or frontend:
        services: list[str] = []
        if backend:
            services.append("backend")
        if frontend:
            services.append("frontend")
        return tuple(services)
    return MANAGED_SERVICES


@cli.command()
def doctor(
    export: Path | None = typer.Option(
        None,
        "--export",
        help="Write the doctor report (plain text) to this path instead of just the terminal.",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help=(
            "After printing the report, offer to apply safe remediations "
            "(install ml deps, create folders, switch to whisper-medium "
            "on low-RAM hosts, install frontend deps). Skips anything "
            "that needs credentials or external accounts."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Apply --fix actions non-interactively (assume yes to every prompt).",
    ),
) -> None:
    """Report local setup status. Optionally remediate with --fix."""
    cfg = load_config()
    next_steps: list[str] = []
    fixable_actions: list[tuple[str, str, object]] = []
    # When --export is set, route every console.print through a recording
    # console so we can dump a copy after rendering. Rich strips ANSI for us.
    global console
    original_console = console
    recording = Console(record=True) if export is not None else None
    if recording is not None:
        console = recording
    try:
        _doctor_body(cfg, next_steps, fixable_actions)
    finally:
        # Restore the module-level console FIRST so any failure in the
        # write/mkdir path below can't leave subsequent CLI invocations
        # (e.g. in the same test session) using the recording instance.
        console = original_console
        if recording is not None and export is not None:
            try:
                export.parent.mkdir(parents=True, exist_ok=True)
                export.write_text(recording.export_text())
                original_console.print(f"[green]Doctor report written to[/green] {export}")
            except OSError as exc:
                original_console.print(f"[red]Could not write {export}:[/red] {exc}")

    if fix:
        _apply_doctor_fixes(fixable_actions, assume_yes=yes)


def _doctor_body(
    cfg: AppConfig,
    next_steps: list[str],
    fixable_actions: list[tuple[str, str, object]],
) -> None:
    table = Table(title="MeetingMind Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    binaries = {
        "python": shutil.which("python3") or shutil.which("python") or "",
        "uv": _check_binary("uv"),
        "node": _check_binary("node"),
        "npm": _check_binary("npm"),
        "ffmpeg": _check_binary("ffmpeg"),
        "ffprobe": _check_binary("ffprobe"),
        "lms": _check_binary("lms"),
        "ollama": _check_binary("ollama"),
    }
    for name, binary_path in binaries.items():
        table.add_row(name, _status(bool(binary_path)), binary_path or "not found")
        if name in {"uv", "node", "npm", "ffmpeg", "ffprobe"} and not binary_path:
            next_steps.append(_dependency_hint(name, "install this dependency and rerun doctor."))

    obsidian_ready = _app_installed("Obsidian")
    table.add_row(
        "Obsidian",
        _optional_status(obsidian_ready),
        "installed" if obsidian_ready else "optional; installer can install it with Homebrew Cask",
    )

    # Audit H1 (v0.2.5): tier-aware deps. Pick required modules + the
    # install command we suggest based on what the user actually
    # configured. The lite-stack default (v0.2.0+) doesn't need mlx-whisper
    # OR pyannote.audio OR an HF token — telling a lite-stack user to
    # install those is actively wrong.
    is_lite_stack = (
        cfg.diarization.provider == "foxnose"
        and cfg.asr.engine == "faster_whisper"
    )
    token = os.getenv("HUGGING_FACE_HUB_TOKEN") or os.getenv("HUGGING_FACE_TOKEN")
    if is_lite_stack:
        table.add_row(
            "huggingface token",
            _optional_status(bool(token)),
            "optional on lite stack; only needed for the pyannote opt-in path",
        )
    else:
        table.add_row("huggingface token", _status(bool(token)), "environment/.env.local")
        if not token:
            next_steps.append(
                _dependency_hint(
                    "Hugging Face token",
                    (
                        "add HUGGING_FACE_HUB_TOKEN to .env.local after accepting "
                        "the pyannote model terms."
                    ),
                )
            )

    required_modules = (
        # Lite stack: faster-whisper + diarize. WeSpeaker handled separately
        # below (it has its own download-from-HF-mirror flow in
        # wespeaker_embedding_provider._ensure_model_cached).
        {"faster_whisper": "faster-whisper", "diarize": "diarize"}
        if is_lite_stack
        else {"mlx_whisper": "mlx-whisper", "pyannote.audio": "pyannote.audio"}
    )
    install_extra = "ml-lite" if is_lite_stack else "ml"
    missing_ml = False
    for module, package in required_modules.items():
        installed = _module_available(module)
        table.add_row(
            package,
            _status(installed),
            "installed" if installed else f"run `uv sync --extra {install_extra}`",
        )
        if not installed:
            missing_ml = True
            next_steps.append(
                _dependency_hint(
                    package,
                    f"run `uv sync --extra {install_extra}` before transcription/diarization.",
                )
            )
    if missing_ml:
        fixable_actions.append(
            (
                f"Install ML dependencies ({install_extra})",
                f"uv sync --extra {install_extra}",
                lambda extra=install_extra: _run_command(
                    ["uv", "sync", "--extra", extra], timeout=900
                ),
            )
        )
    elif not is_lite_stack and _has_hf_token() and not _pyannote_models_cached():
        # Pyannote path: pre-warm the gated downloads (only when user is
        # actually on the pyannote stack).
        fixable_actions.append(
            (
                "Pre-download pyannote diarization models",
                "Model.from_pretrained for speaker-diarization-community-1 + embedding",
                _prewarm_pyannote_models,
            )
        )

    # Lite-stack (default since v0.2.0): the WeSpeaker speaker-embedding
    # ONNX is needed for FoxNoseTech diarization + the wespeaker rename-
    # once flow. We download from the public HF mirror to bypass the
    # unreliable Tencent Cloud CDN that wespeakerruntime defaults to.
    if not _wespeaker_model_cached():
        table.add_row(
            "WeSpeaker ONNX",
            _status(False),
            "run `meetingmind doctor --fix` (or first ingest triggers download)",
        )
        fixable_actions.append(
            (
                "Pre-download WeSpeaker speaker-embedding ONNX (~25 MB)",
                "Download from hbredin/wespeaker-voxceleb-resnet34-LM on Hugging Face",
                _prewarm_wespeaker_model,
            )
        )
    else:
        table.add_row(
            "WeSpeaker ONNX",
            _status(True),
            "cached at ~/.wespeaker/en/model.onnx",
        )

    missing_folders = False
    for label, folder_path in {
        "inbox": cfg.paths.inbox_dir,
        "processed": cfg.paths.processed_dir,
        "archive": cfg.paths.archive_dir,
        "delete review": cfg.paths.delete_review_dir,
        "vault": cfg.paths.vault_dir,
    }.items():
        table.add_row(label, _status(folder_path.exists()), str(folder_path))
        if not folder_path.exists():
            missing_folders = True
            next_steps.append(_dependency_hint(label, "run `uv run meetingmind install`."))
    if missing_folders:
        fixable_actions.append(
            (
                "Create missing local folders",
                "ensure_local_layout(cfg)",
                lambda: ensure_local_layout(cfg),
            )
        )

    frontend_modules = cfg.paths.repo_root / "frontend" / "node_modules"
    frontend_ready = frontend_modules.exists()
    table.add_row(
        "frontend dependencies",
        _status(frontend_ready),
        "installed" if frontend_ready else "run `cd frontend && npm install`",
    )
    if not frontend_ready:
        fixable_actions.append(
            (
                "Install frontend dependencies",
                "npm install (in frontend/)",
                lambda: _run_command(
                    ["npm", "install"],
                    cwd=cfg.paths.repo_root / "frontend",
                    timeout=900,
                ),
            )
        )
    if not frontend_ready:
        next_steps.append(
            _dependency_hint("Frontend dependencies", "run `cd frontend && npm install`.")
        )

    vocabulary_terms = load_custom_vocabulary_terms(cfg)
    table.add_row(
        "ASR vocabulary",
        "[green]optional[/green]" if vocabulary_terms else "[yellow]not configured[/yellow]",
        f"{len(vocabulary_terms)} term(s) from {cfg.asr.vocabulary_path}",
    )

    if cfg.models.provider == "openrouter":
        # OpenRouter models live on a remote service, not in a local
        # inventory — check the API key instead of trying to match the
        # configured model against an "installed" list.
        api_key_set = bool(
            os.environ.get(cfg.models.openrouter_api_key_env, "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )
        table.add_row(
            "OpenRouter API key",
            _status(api_key_set),
            cfg.models.openrouter_api_key_env if api_key_set else "set via dashboard Settings",
        )
        table.add_row("primary model", "[green]info[/green]", cfg.models.default_model)
        table.add_row("quality model", "[green]info[/green]", cfg.models.quality_model)
        if not api_key_set:
            next_steps.append(
                _dependency_hint(
                    "OpenRouter API key",
                    "paste your key in dashboard Settings → OpenRouter.",
                )
            )
    elif cfg.models.provider == "ollama":
        models = list_ollama_models()
        detail = f"{len(models)} models" if models else "Ollama unavailable or no models"
        table.add_row("Ollama models", _status(bool(models)), detail)
        if not models:
            next_steps.append(
                _dependency_hint("Ollama", "start Ollama and pull/select at least one local model.")
            )
        if models:
            _check_local_models(table, next_steps, cfg, models)
    else:
        models = list_lm_studio_models()
        detail = (
            f"{len(models)} models" if models else "LM Studio server/CLI unavailable or no models"
        )
        table.add_row("LM Studio models", _status(bool(models)), detail)
        if not models:
            next_steps.append(
                _dependency_hint(
                    "LM Studio",
                    "open LM Studio and make sure local models are available.",
                )
            )
        if models:
            _check_local_models(table, next_steps, cfg, models)
    table.add_row("platform", "[green]info[/green]", f"{platform.system()} {platform.machine()}")
    _check_memory(table, next_steps, cfg, fixable_actions)
    console.print(table)
    if next_steps:
        console.print(
            Panel(
                "\n".join(dict.fromkeys(next_steps)),
                title="Next Steps",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                (
                    "Local setup looks ready. Put audio in data/inbox, start the backend "
                    "and dashboard, then ingest."
                ),
                title="Ready",
                border_style="green",
            )
        )


def _apply_doctor_fixes(
    actions: list[tuple[str, str, object]],
    *,
    assume_yes: bool,
) -> None:
    """Interactive remediation loop. Each action prints its label + the
    underlying command preview, then prompts y/N (or runs immediately
    when --yes). Never touches data/, runtime/, vault/, .env.local.
    Auth-y fixes (HF token paste, OpenRouter key paste) are
    intentionally NOT here — those need Settings or the install wizard.
    """
    if not actions:
        console.print("[green]Nothing to fix — every check that doctor can fix is already in place.[/green]")
        return
    console.print()
    console.print(Panel(
        f"{len(actions)} fixable issue(s) — running interactive remediation.",
        title="Doctor · Fix",
        border_style="cyan",
    ))
    for label, preview, action in actions:
        console.print()
        console.print(f"[bold]{label}[/bold]")
        console.print(f"  [dim]would run:[/dim] {preview}")
        if not assume_yes and not typer.confirm("Apply this fix?", default=True):
            console.print("[yellow]Skipped.[/yellow]")
            continue
        try:
            action()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 — surface to user, keep going
            console.print(f"[red]Fix failed:[/red] {exc}")
    console.print()
    console.print(
        "[cyan]Re-run `uv run meetingmind doctor` to verify, "
        "and `uv run meetingmind restart` if any service config changed.[/cyan]"
    )


@cli.command()
def install(
    wizard: bool = typer.Option(
        True,
        "--wizard/--no-wizard",
        help="Run the guided dependency, model, Hugging Face, and Obsidian setup flow.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show install commands and config choices without running external installers.",
    ),
    skip_core: bool = typer.Option(
        False, "--skip-core", help="Skip Homebrew / core dependency installs."
    ),
    skip_huggingface: bool = typer.Option(
        False,
        "--skip-huggingface",
        help="Skip the Hugging Face token + pyannote access walkthrough.",
    ),
    skip_models: bool = typer.Option(
        False,
        "--skip-models",
        help="Skip the LM Studio / Ollama model picker.",
    ),
    skip_obsidian: bool = typer.Option(
        False,
        "--skip-obsidian",
        help="Skip the Obsidian vault setup step (use a custom vault later via config).",
    ),
) -> None:
    """Create local folders, initialize SQLite, and optionally run guided local setup."""
    cfg = load_config()
    env_path = cfg.paths.repo_root / ".env.local"
    if not dry_run:
        ensure_local_layout(cfg)
        initialize_database(cfg.paths.database_path)
        seed_default_scheduled_jobs(cfg)
        _ensure_env_template(env_path)
    if wizard:
        _run_install_wizard(
            cfg,
            env_path,
            dry_run,
            skip_core=skip_core,
            skip_hugging_face=skip_huggingface,
            skip_models=skip_models,
            skip_obsidian=skip_obsidian,
        )
        if not dry_run:
            save_config(cfg)
            ensure_local_layout(cfg)
            initialize_database(cfg.paths.database_path)
            seed_default_scheduled_jobs(cfg)
    if dry_run:
        console.print(
            "[cyan]MeetingMind install dry run complete; no local files were changed.[/cyan]"
        )
    else:
        console.print("[green]MeetingMind local layout initialized.[/green]")
    console.print(f"Config: {cfg.config_path}")
    console.print(f"Inbox: {cfg.paths.inbox_dir}")
    console.print(f"Vault: {cfg.paths.vault_dir}")
    # Optionally drop a global `mm` launcher into the user's PATH so they
    # can run `mm upgrade` / `mm status` from any directory instead of
    # cd'ing into the repo and using `uv run meetingmind`. Skips silently
    # if writing to ~/.local/bin isn't safe.
    if not dry_run:
        _install_global_launcher(cfg)

    console.print(
        Panel(
            "\n".join(
                [
                    "1. Run `uv run meetingmind doctor` and resolve any required blockers.",
                    (
                        "2. Run `uv sync --extra ml` before real transcription/diarization "
                        "if you skipped ML dependency setup."
                    ),
                    "3. Run `cd frontend && npm install` if frontend dependencies are missing.",
                    "4. Start the stack: `uv run meetingmind start` (auto-opens the dashboard).",
                    f"   Dashboard: http://127.0.0.1:{cfg.runtime.dashboard_port}",
                    "5. Add recordings via the dashboard or drop them in data/inbox.",
                ]
            ),
            title="New User Next Steps",
            border_style="green",
        )
    )


def _install_global_launcher(cfg: AppConfig) -> None:
    """Drop `~/.local/bin/mm` so `mm upgrade` / `mm status` work from any
    directory. Idempotent: rewrites the script every time install runs so
    a moved repo or upgraded uv path stays correct.
    """
    bin_dir = Path.home() / ".local" / "bin"
    target = bin_dir / "mm"
    repo_root = cfg.paths.repo_root
    script = f"""#!/usr/bin/env bash
# Auto-generated by `meetingmind install`. Re-runs `meetingmind install`
# regenerate this file. Edits will be overwritten.
set -e
cd "{repo_root}"
exec uv run meetingmind "$@"
"""
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(script)
        target.chmod(0o755)
    except OSError:
        return
    # Tell the user where it is + how to put it on PATH if it isn't.
    path_env = os.environ.get("PATH", "")
    if str(bin_dir) not in path_env.split(":"):
        console.print(
            f"[yellow]Installed[/yellow] global launcher at {target}. "
            f"Add to your shell PATH for `mm` to work from any directory:\n"
            f"  [cyan]echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc[/cyan]"
        )
    else:
        console.print(
            f"[green]Installed[/green] global launcher: type `mm` from any "
            f"directory ({target})."
        )


@cli.command("ingest-once")
def ingest_once() -> None:
    """Process pending files in the inbox once."""
    from app.services.ingestion import ingest_pending_files

    cfg = load_config()
    initialize_database(cfg.paths.database_path)
    results = ingest_pending_files(cfg)
    for result in results:
        console.print(f"{result.status}: {result.source_path} -> meeting {result.meeting_id}")
    if not results:
        console.print("No pending files.")


@cli.command("process")
def process(meeting_id: int) -> None:
    """Run local transcription and diarization for an ingested meeting."""
    from app.db.database import connect
    from app.services.pipeline import process_meeting_audio

    cfg = load_config()
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        meeting = conn.execute(
            """
            SELECT COALESCE(sf.storage_path, m.imported_path) AS audio_path
            FROM meetings m
            LEFT JOIN source_files sf ON sf.meeting_id = m.id
            WHERE m.id = ?
            ORDER BY sf.id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    if not meeting:
        raise typer.BadParameter(f"Meeting {meeting_id} not found")
    audio_path = cfg.paths.repo_root / meeting["audio_path"]
    segment_count = process_meeting_audio(cfg, meeting_id, audio_path)
    console.print(
        f"[green]Processed meeting {meeting_id}: {segment_count} transcript segment(s).[/green]"
    )


@cli.command("extract")
def extract(meeting_id: int) -> None:
    """Run structured extraction through the configured local model bus."""
    from app.services.extraction import extract_meeting_atoms

    cfg = load_config()
    atoms = extract_meeting_atoms(cfg, meeting_id)
    console.print(
        f"[green]Extracted atoms for meeting {meeting_id}: {atoms.suggested_title}[/green]"
    )


@cli.command("asr-candidates")
def asr_candidates(meeting_id: int, limit: int | None = None) -> None:
    """Generate alternate ASR candidates for low-confidence spans."""
    from app.db.database import connect
    from app.services.asr_candidates import run_asr_candidate_passes

    cfg = load_config()
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        meeting = conn.execute(
            """
            SELECT COALESCE(sf.storage_path, m.imported_path) AS audio_path
            FROM meetings m
            LEFT JOIN source_files sf ON sf.meeting_id = m.id
            WHERE m.id = ?
            ORDER BY sf.id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    if not meeting:
        raise typer.BadParameter(f"Meeting {meeting_id} not found")
    results = run_asr_candidate_passes(
        cfg,
        meeting_id,
        cfg.paths.repo_root / meeting["audio_path"],
        limit=limit,
    )
    console.print(
        f"[green]Generated {len(results)} ASR candidate(s) for meeting {meeting_id}.[/green]"
    )


@cli.command("approve-speaker")
def approve_speaker(meeting_id: int, speaker_id: str, label: str) -> None:
    """Approve a diarized speaker label before Obsidian promotion."""
    from app.services.review import approve_speaker_label

    cfg = load_config()
    approve_speaker_label(cfg, meeting_id, speaker_id, label)
    console.print(f"[green]Approved {speaker_id} as {label}.[/green]")


@cli.command("stage")
def stage(meeting_id: int) -> None:
    """Write a staged Obsidian note for review."""
    from app.services.obsidian_writer import write_staged_meeting

    cfg = load_config()
    output = write_staged_meeting(cfg, meeting_id)
    console.print(f"[green]Staged meeting note:[/green] {output}")


@cli.command("promote")
def promote(meeting_id: int) -> None:
    """Promote an approved meeting into the Obsidian vault."""
    from app.services.obsidian_writer import promote_meeting

    cfg = load_config()
    try:
        output = promote_meeting(cfg, meeting_id)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Promoted meeting note:[/green] {output}")


@cli.command("eval")
def eval_command(
    corpus_dir: str = typer.Argument(
        "tests/eval/fixtures",
        help="Path to the eval corpus directory (default tests/eval/fixtures).",
    ),
    json_out: str | None = typer.Option(
        None,
        "--json",
        help="Optional path to write the full eval report as JSON.",
    ),
) -> None:
    """Run the lite-stack pipeline against a corpus of fixture meetings.

    Each fixture is a subdirectory containing `audio.wav` and an optional
    `reference.json` (transcript, speaker count, expected keywords).
    Reports WER, detected speaker count, keyword recall, and wall-clock
    time. Use as a regression gate before merging changes that touch the
    pipeline — see `tests/eval/README.md` for the corpus format.

    Empty corpus → empty report with exit code 0 (lets you check in the
    harness without committing fixture audio).
    """
    from app.config import load_config
    from app.services.eval.runner import run_eval

    cfg = load_config()
    target = Path(corpus_dir)
    if not target.is_absolute():
        target = cfg.paths.repo_root / target
    console.print(f"[bold]Eval corpus:[/bold] {target}")
    report = run_eval(cfg, target)

    if not report.fixtures:
        console.print("[yellow]No fixtures found.[/yellow] Add subdirectories to the corpus to begin.")
        if json_out:
            Path(json_out).write_text(json.dumps(report.to_json(), indent=2))
        return

    for fixture in report.fixtures:
        if fixture.error:
            console.print(f"[red]✗ {fixture.slug}[/red]: {fixture.error}")
            continue
        wer_str = f"WER {fixture.wer:.3f}" if fixture.wer is not None else "WER —"
        spk_str = (
            f"speakers {fixture.detected_speaker_count}"
            + (
                f"/{fixture.expected_speaker_count}"
                if fixture.expected_speaker_count is not None
                else ""
            )
        )
        kw_missed = [k for k, found in fixture.keywords_found.items() if not found]
        kw_str = "" if not fixture.keywords_found else (
            f" · kw missed: {', '.join(kw_missed)}" if kw_missed else " · kw all found"
        )
        console.print(
            f"[green]✓ {fixture.slug}[/green]: "
            f"{wer_str} · {spk_str} · {fixture.wall_clock_seconds:.1f}s{kw_str}"
        )

    console.print(
        f"\n[bold]Summary:[/bold] {report.passed_count}/{report.total_count} fixtures "
        f"clean · total {report.total_wall_clock_seconds:.1f}s"
    )

    if json_out:
        Path(json_out).write_text(json.dumps(report.to_json(), indent=2))
        console.print(f"Wrote JSON report to {json_out}")

    if report.passed_count < report.total_count:
        raise typer.Exit(1)


@cli.command("eval-models")
def eval_models_command(
    meeting_id: int = typer.Argument(
        ..., help="Meeting id to run each model against."
    ),
    models: str = typer.Option(
        ...,
        "--models",
        help=(
            "Comma-separated list of model ids to test "
            "(e.g. 'x-ai/grok-4.1-fast,openai/gpt-oss-120b'). "
            "Each id is what would go in `default_model` in local.toml."
        ),
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        help=(
            "Where to write per-model snapshots. Default: "
            "runtime/eval-models/{meeting_id}/."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force/--no-force",
        help=(
            "Re-run a model even when its snapshot exists. Default off so "
            "you can add a new model to an existing comparison without "
            "re-paying for the others."
        ),
    ),
) -> None:
    """Run extraction once per model and snapshot outputs for side-by-side
    comparison.

    Reads the OpenRouter API key from the environment (same path as the
    normal extraction pipeline). Clears per-meeting LLM caches before
    each model so the comparison is on fresh runs, not replayed cache.

    Outputs:
      runtime/eval-models/{meeting_id}/
        ├── summary.json              # one row per model: timings + atom counts
        ├── x-ai__grok-4.1-fast/
        │   ├── atoms.json
        │   ├── drivers.json
        │   ├── synthesis.json
        │   ├── reflections.json
        │   └── completed.json        # marker + timings
        └── openai__gpt-oss-120b/
            └── ... same layout
    """
    from app.services.eval_models import run_model_ab

    cfg = load_config()
    if cfg.models.provider != "openrouter":
        console.print(
            "[yellow]Warning:[/yellow] models.provider is "
            f"'{cfg.models.provider}'. The eval will pin both default and "
            "quality models per pass but still route through the configured "
            "provider — for OpenRouter-only routes (Grok, gpt-oss-120b, etc.) "
            "you'll want to set provider='openrouter' first."
        )
    model_ids = [m.strip() for m in models.split(",") if m.strip()]
    if not model_ids:
        raise typer.BadParameter("--models must list at least one model id")

    target_root = Path(output_dir) if output_dir else None
    console.print(
        f"[bold]Eval models:[/bold] meeting_id={meeting_id} "
        f"· {len(model_ids)} models · force={force}"
    )
    runs = run_model_ab(
        config=cfg,
        meeting_id=meeting_id,
        model_ids=model_ids,
        output_root=target_root,
        force=force,
    )
    for run in runs:
        status = "[green]✓[/green]" if run.ok else "[red]✗[/red]"
        timing_str = " · ".join(f"{t.stage} {t.seconds:.1f}s" for t in run.timings)
        console.print(
            f"{status} {run.model_id} — total {run.total_seconds:.1f}s · {timing_str}"
        )
        if run.error:
            console.print(f"  [red]error:[/red] {run.error}")
    if runs:
        console.print(
            f"\n[bold]Outputs:[/bold] {runs[0].output_dir.parent}"
        )


@cli.command("naming-report")
def naming_report_command(
    meeting_id: int = typer.Argument(..., help="Meeting id to analyze."),
    json_out: str | None = typer.Option(
        None,
        "--json",
        help="Optional path to write the full report as JSON.",
    ),
) -> None:
    """Show which speaker-name candidates the v0.2.10 extractor finds on a
    meeting, plus why each one fired.

    Useful when adjusting `speaker_identity._DIRECT_ADDRESS_PATTERNS` or
    `_STOP_NAMES` — run the report before and after the change to see
    which new detections appear and which false-positives disappear.
    Pure read-only: doesn't persist anything to the database.
    """
    from app.config import load_config
    from app.services.speaker_identity import _speaker_name_candidates

    cfg = load_config()
    groups = _speaker_name_candidates(cfg, meeting_id)
    if not groups:
        console.print(f"[yellow]No candidates for meeting {meeting_id}.[/yellow]")
        if json_out:
            Path(json_out).write_text(json.dumps({"meeting_id": meeting_id, "candidates": []}, indent=2))
        return

    rows: list[dict] = []
    for group in groups:
        first = group[0]
        kinds = sorted({e.evidence_type for e in group})
        console.print(
            f"[green]{first.speaker_id}[/green] → "
            f"[bold]{first.name.title()}[/bold] "
            f"({len(group)} evidence, types: {', '.join(kinds)})"
        )
        for e in group:
            console.print(f"    seg {e.segment_id} · {e.evidence_type}: [dim]{e.phrase!r}[/dim]")
        rows.append(
            {
                "speaker_id": first.speaker_id,
                "candidate_name": first.name.title(),
                "evidence_count": len(group),
                "evidence_types": kinds,
                "evidence": [
                    {
                        "segment_id": e.segment_id,
                        "type": e.evidence_type,
                        "phrase": e.phrase,
                    }
                    for e in group
                ],
            }
        )
    if json_out:
        Path(json_out).write_text(
            json.dumps({"meeting_id": meeting_id, "candidates": rows}, indent=2)
        )
        console.print(f"Wrote JSON report to {json_out}")


@cli.command("ab-diarize")
def ab_diarize(meeting_id: int) -> None:
    """Run both diarization providers against the same meeting audio and
    print a side-by-side comparison.

    Added in v0.1.7 to evaluate the lite-stack swap. Useful before
    flipping the default provider for new installs.

    Reports per-provider:
      - wall-clock time
      - detected speaker count
      - turn count + total speech seconds
      - first-second-of-speech sanity check
      - per-second speaker-id agreement (rough alignment)
    """
    import time as _time

    from app.config import load_config
    from app.db.database import connect
    from app.services.audio import normalize_audio_for_diarization
    from app.services.diarization.factory import create_diarization_provider

    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT m.title AS title,
                   COALESCE(sf.storage_path, m.imported_path) AS audio_path
            FROM meetings m
            LEFT JOIN source_files sf ON sf.meeting_id = m.id
            WHERE m.id = ?
            ORDER BY sf.id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    if not row or not row["audio_path"]:
        console.print(f"[red]Meeting {meeting_id} not found or has no audio.[/red]")
        raise typer.Exit(1)
    audio_path = Path(row["audio_path"])
    if not audio_path.exists():
        console.print(f"[red]Audio file missing on disk:[/red] {audio_path}")
        raise typer.Exit(1)

    # Both providers need a mono 16 kHz WAV; FoxnoseTech can't decode M4A
    # directly and pyannote's torchcodec backend is fragile on macOS. Use
    # the same ffmpeg normalize step the production pipeline uses so the
    # A/B is apples-to-apples.
    normalized = normalize_audio_for_diarization(
        audio_path,
        cfg.paths.processed_dir,
        cfg.diarization.normalized_sample_rate,
    )

    console.print(f"\n[bold]A/B diarize:[/bold] {row['title']} (#{meeting_id})")
    console.print(f"  source:     {audio_path}")
    console.print(f"  normalized: {normalized}\n")
    audio_path = normalized

    results: dict[str, dict] = {}
    for provider_name in ("pyannote", "foxnose"):
        console.print(f"[cyan]→ {provider_name}[/cyan] ...")
        provider_cfg = cfg.diarization.model_copy(update={"provider": provider_name})
        run_cfg = cfg.model_copy(update={"diarization": provider_cfg})
        try:
            provider = create_diarization_provider(run_cfg)
            t0 = _time.monotonic()
            turns = provider.diarize(audio_path)
            elapsed = _time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 — show the error per provider
            console.print(f"   [red]{type(exc).__name__}: {exc}[/red]\n")
            results[provider_name] = {"error": str(exc)}
            continue
        speakers = {t.speaker_id for t in turns}
        total_ms = sum(max(0, t.end_ms - t.start_ms) for t in turns)
        results[provider_name] = {
            "elapsed_s": elapsed,
            "turn_count": len(turns),
            "speaker_count": len(speakers),
            "total_speech_s": total_ms / 1000.0,
            "turns": turns,
        }
        console.print(
            f"   {elapsed:6.2f}s · {len(turns):4d} turns · {len(speakers):2d} speakers · "
            f"{total_ms / 1000:7.1f}s speech\n"
        )

    if "pyannote" in results and "foxnose" in results:
        py = results["pyannote"].get("turns")
        fx = results["foxnose"].get("turns")
        if py and fx:
            agreement = _speaker_agreement_per_second(py, fx)
            console.print(
                f"[bold]Per-second speaker-id agreement:[/bold] "
                f"{agreement * 100:.1f}% (label-aligned best match)"
            )


def _speaker_agreement_per_second(turns_a, turns_b) -> float:
    """Crude similarity score: bucket each second of audio into a
    speaker id from each provider, find the best speaker-label mapping
    between the two via greedy matching, then report the fraction of
    seconds where the two providers agree.

    Lower bound on quality match — doesn't penalize correct boundary
    differences, just gross disagreement on "who is speaking now."
    """
    from app.services.diarization.base import SpeakerTurn

    def _to_per_second(turns: list[SpeakerTurn]) -> list[str]:
        if not turns:
            return []
        last = max(t.end_ms for t in turns)
        seconds = [""] * (last // 1000 + 1)
        for t in turns:
            for s in range(t.start_ms // 1000, t.end_ms // 1000 + 1):
                if 0 <= s < len(seconds):
                    seconds[s] = t.speaker_id
        return seconds

    a_per = _to_per_second(turns_a)
    b_per = _to_per_second(turns_b)
    n = min(len(a_per), len(b_per))
    if n == 0:
        return 0.0
    # Build a co-occurrence count of (a_label, b_label) pairs.
    from collections import Counter

    pair_counts: Counter[tuple[str, str]] = Counter()
    for a, b in zip(a_per[:n], b_per[:n], strict=False):
        if a and b:
            pair_counts[(a, b)] += 1
    # Greedy match: walk pairs by count desc, claim each a/b label once.
    a_used: set[str] = set()
    b_used: set[str] = set()
    mapping: dict[str, str] = {}
    for (a, b), _ in pair_counts.most_common():
        if a in a_used or b in b_used:
            continue
        mapping[a] = b
        a_used.add(a)
        b_used.add(b)
    # Score: fraction of seconds where mapping[a_per[i]] == b_per[i].
    hits = 0
    total = 0
    for a, b in zip(a_per[:n], b_per[:n], strict=False):
        if not a or not b:
            continue
        total += 1
        if mapping.get(a) == b:
            hits += 1
    return hits / total if total else 0.0


@cli.command("vault-lint")
def vault_lint() -> None:
    """Validate generated Obsidian Markdown structure."""
    result = lint_vault(load_config())
    if result.ok:
        console.print(
            f"[green]Vault lint passed.[/green] Checked {result.checked_files} Markdown file(s)."
        )
        return
    for issue in result.issues:
        console.print(f"[red]{issue}[/red]")
    raise typer.Exit(1)


@cli.command("run-scheduled-job")
def run_job(job_id: int) -> None:
    """Run a persisted scheduler job once."""
    cfg = load_config()
    initialize_database(cfg.paths.database_path)
    seed_default_scheduled_jobs(cfg)
    result = run_scheduled_job(cfg, job_id)
    console.print(f"{result.status}: {result.detail}")
    if result.status != "complete":
        raise typer.Exit(1)


def _require_initialized(cfg: AppConfig) -> None:
    """Refuse to start the stack when `meetingmind install` hasn't run yet —
    otherwise uvicorn dies inside its own import chain and the user has to
    decode a SQLite or pyannote stack trace.
    """
    if not cfg.paths.database_path.exists():
        console.print(
            "[red]No MeetingMind database found at[/red] "
            f"{cfg.paths.database_path}\n"
            "[yellow]Run `uv run meetingmind install` first.[/yellow]"
        )
        raise typer.Exit(code=2)


def _await_service_healthy(
    cfg: AppConfig, service: str, timeout_seconds: float = 30.0
) -> bool:
    """Poll the service URL until it responds or the timeout elapses.

    Backend is considered healthy when `/api/health` returns 2xx; the
    dashboard is healthy when the Vite root page returns 2xx.
    """
    import urllib.error
    import urllib.request

    base = _service_url(cfg, service)
    url = base + ("/api/health" if service == "backend" else "/")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            # Fixed http:// localhost health-check URL, not user input.
            with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310
                if 200 <= response.status < 300:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return False


@cli.command()
def start(
    backend: bool = typer.Option(False, "--backend", help="Start only the backend service."),
    frontend: bool = typer.Option(False, "--frontend", help="Start only the dashboard service."),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help=(
            "Block until each launched service responds on its HTTP endpoint. "
            "Default ON — guarantees the dashboard URL is ready before the "
            "command returns. Use --no-wait to fire-and-forget."
        ),
    ),
    wait_timeout: float = typer.Option(
        60.0,
        "--wait-timeout",
        help=(
            "Seconds to wait per service when --wait is set. Default 60 s — "
            "raise to 120–300 s if LM Studio / Ollama is cold-starting a model."
        ),
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help=(
            "Open the dashboard in your default browser once the frontend "
            "is healthy. Default ON. Disable with --no-open for headless or "
            "SSH-tunneled setups."
        ),
    ),
) -> None:
    """Start the backend and dashboard as managed local background processes."""
    cfg = load_config()
    _require_initialized(cfg)
    initialize_database(cfg.paths.database_path)
    seed_default_scheduled_jobs(cfg)
    services = _selected_services(backend, frontend)
    for service in services:
        _start_service(cfg, service)
    frontend_healthy = False
    if wait:
        unhealthy: list[str] = []
        for service in services:
            console.print(f"[dim]Waiting for {service} at {_service_url(cfg, service)}…[/dim]")
            if _await_service_healthy(cfg, service, timeout_seconds=wait_timeout):
                console.print(f"[green]{service} ready.[/green]")
                if service == "frontend":
                    frontend_healthy = True
            else:
                console.print(
                    f"[yellow]{service} did not respond within {wait_timeout:.0f}s.[/yellow]\n"
                    f"  · tail {_service_log_path(cfg, service)}\n"
                    f"  · if a model is still loading, retry with "
                    f"[cyan]--wait-timeout 180[/cyan] (or 300 for cold starts)."
                )
                unhealthy.append(service)
        if open_browser and frontend_healthy:
            _open_dashboard(cfg)
        if unhealthy:
            raise typer.Exit(code=1)
    elif open_browser and "frontend" in services:
        # Without --wait we can't confirm health, but most boots are <5s
        # on a warm cache. Give it a beat and try to open anyway.
        time.sleep(2.0)
        _open_dashboard(cfg)


def _open_dashboard(cfg: AppConfig) -> None:
    """Open the dashboard URL in the user's default browser. Best-effort."""
    url = f"http://127.0.0.1:{cfg.runtime.dashboard_port}"
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            import webbrowser

            webbrowser.open(url)
        console.print(f"[green]Opened[/green] {url}")
    except (OSError, subprocess.SubprocessError):
        # Headless / no DISPLAY / xdg-open missing — just print the URL.
        console.print(f"[cyan]Dashboard ready:[/cyan] {url}")


@cli.command()
def stop(
    backend: bool = typer.Option(False, "--backend", help="Stop only the backend service."),
    frontend: bool = typer.Option(False, "--frontend", help="Stop only the dashboard service."),
) -> None:
    """Stop managed MeetingMind background processes."""
    cfg = load_config()
    for service in _selected_services(backend, frontend):
        _stop_service(cfg, service)


@cli.command()
def status() -> None:
    """Show managed service state and local URLs."""
    cfg = load_config()
    table = Table(title="MeetingMind Services")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("URL")
    table.add_column("Log")
    for service in MANAGED_SERVICES:
        pid_path = _service_pid_path(cfg, service)
        _clear_stale_pid(pid_path)
        pid = _read_pid(pid_path)
        running = _process_running(pid)
        table.add_row(
            service,
            "[green]running[/green]" if running else "[yellow]stopped[/yellow]",
            str(pid) if running and pid else "",
            _service_url(cfg, service),
            str(_service_log_path(cfg, service)),
        )
    console.print(table)


@cli.command()
def restart(
    backend: bool = typer.Option(False, "--backend", help="Restart only the backend service."),
    frontend: bool = typer.Option(False, "--frontend", help="Restart only the dashboard service."),
) -> None:
    """Restart managed backend/frontend processes without changing local state or config."""
    cfg = load_config()
    services = _selected_services(backend, frontend)
    for service in reversed(services):
        _stop_service(cfg, service)
    initialize_database(cfg.paths.database_path)
    seed_default_scheduled_jobs(cfg)
    for service in services:
        _start_service(cfg, service)


def _git_working_tree_clean(repo_root: Path) -> bool:
    """True when `git status --porcelain` returns no lines (no uncommitted
    changes, no untracked files inside tracked paths).
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # If git isn't available, treat as "can't verify" and let the caller
        # decide. Returning True would silently mask real local changes.
        return False
    return not completed.stdout.strip()


@cli.command()
def upgrade(
    pull: bool = typer.Option(True, "--pull/--no-pull", help="Pull latest git changes first."),
    deps: bool = typer.Option(True, "--deps/--no-deps", help="Refresh Python and frontend deps."),
    include_ml: bool = typer.Option(
        True,
        "--include-ml/--no-ml",
        help=(
            "Include optional ML dependencies (mlx-whisper, pyannote.audio, "
            "omegaconf) while syncing. Default true — almost every user needs these."
        ),
    ),
    restart: bool = typer.Option(
        True,
        "--restart/--no-restart",
        help="Restart any running backend/dashboard so they pick up the new code.",
    ),
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help=(
            "After the upgrade, chain `doctor --fix` to auto-remediate any "
            "drift (missing folders, frontend node_modules, low-RAM model swap). "
            "Interactive — prompts before each fix."
        ),
    ),
    auto_fix: bool = typer.Option(
        False,
        "--auto-fix",
        help=(
            "Like --check but applies every fix non-interactively. Used by the "
            "dashboard's in-app upgrade button, which can't show y/N prompts. "
            "Implies --check."
        ),
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help=(
            "Show the commits that would be pulled, then exit. Read-only — "
            "doesn't pull, sync, restart, or check anything."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Run --pull even when the working tree is dirty (you've been warned).",
    ),
    npm_timeout: int = typer.Option(
        1800,
        "--npm-timeout",
        help="Seconds to allow `npm install` before giving up.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would happen without pulling, installing, or restarting.",
    ),
) -> None:
    """Pull repo changes, refresh deps, and (optionally) bounce the running stack."""
    cfg = load_config()

    if preview:
        # Read-only changelog of what's pending. Useful before pulling
        # for the cautious tester who wants to know what they're getting.
        _print_pending_changes(cfg.paths.repo_root)
        return

    if dry_run:
        console.print("[cyan]Upgrade dry-run — no changes will be made.[/cyan]")

    # Pre-upgrade safety tag. If anything in the upgrade goes sideways the
    # user can `git reset --hard pre-upgrade-<stamp>` and be back where
    # they started. Trivial to create, zero-cost if unused.
    if pull and not dry_run:
        _create_pre_upgrade_tag(cfg.paths.repo_root)

    if pull:
        if not _git_working_tree_clean(cfg.paths.repo_root) and not force:
            console.print(
                "[red]Working tree is dirty.[/red] Commit/stash your changes first, "
                "or re-run with [cyan]--force[/cyan] (which still uses --ff-only and "
                "will refuse to merge over local commits)."
            )
            raise typer.Exit(code=2)
        if dry_run:
            console.print("[cyan]Would run:[/cyan] git pull --ff-only")
        else:
            if not _run_command(["git", "pull", "--ff-only"], cwd=cfg.paths.repo_root):
                console.print(
                    "[red]Pull failed. Skipping dependency refresh + restart "
                    "to avoid leaving the install half-upgraded.[/red]\n"
                    "[yellow]Common causes:[/yellow]\n"
                    "  • Lost network — check connectivity, then re-run.\n"
                    "  • Local commits on main — `git log @{u}..` to see them; "
                    "rebase or move them onto a branch.\n"
                    "  • Tags conflicting — `git fetch --prune-tags --tags`."
                )
                raise typer.Exit(code=1)
    if deps:
        uv_command = ["uv", "sync", "--extra", "dev"]
        if include_ml:
            # Audit H1 (v0.2.5): pick the extra that matches the user's
            # actually-configured stack. Don't drag the pyannote/mlx
            # dependencies onto a user who's on the lite default.
            is_lite_stack = (
                cfg.diarization.provider == "foxnose"
                and cfg.asr.engine == "faster_whisper"
            )
            uv_command.extend(["--extra", "ml-lite" if is_lite_stack else "ml"])
        if not _run_command(uv_command, dry_run=dry_run):
            raise typer.Exit(code=1)
        if not _run_command(
            ["npm", "install"],
            dry_run=dry_run,
            timeout=npm_timeout,
            cwd=cfg.paths.repo_root / "frontend",
        ):
            raise typer.Exit(code=1)
    if dry_run:
        console.print(
            "[cyan]Would re-initialize local layout + database and seed scheduled jobs.[/cyan]"
        )
    else:
        ensure_local_layout(cfg)
        initialize_database(cfg.paths.database_path)
        seed_default_scheduled_jobs(cfg)
    if restart:
        running = _running_services(cfg)
        if running:
            label = "Would restart" if dry_run else "Restarting running services to pick up the upgrade"
            console.print(f"[yellow]{label}:[/yellow] {', '.join(running)}")
            if not dry_run:
                for service in running:
                    _stop_service(cfg, service)
                for service in running:
                    _start_service(cfg, service)
                console.print("[green]Upgrade complete; processes restarted.[/green]")
                console.print("Run `uv run meetingmind status` to confirm.")
                return
        else:
            console.print("[dim]Nothing was running; upgrade-only pass complete.[/dim]")
    if dry_run:
        console.print("[cyan]Dry run complete.[/cyan]")
        return

    # Refresh the global `mm` launcher every upgrade. Idempotent — re-
    # writes the shell shim so the cached repo path stays accurate if the
    # user moved the checkout, and tester ergonomics don't drift.
    _install_global_launcher(cfg)

    if check or auto_fix:
        # Re-import the fix flow rather than invoking the Typer command —
        # we already have `cfg` loaded and want to avoid `load_config()`
        # firing twice. The detector code lives in _doctor_body which
        # populates fixable_actions for us.
        console.print()
        console.print(Panel(
            "Running post-upgrade health check…",
            title="Doctor",
            border_style="cyan",
        ))
        next_steps: list[str] = []
        fixable_actions: list[tuple[str, str, object]] = []
        _doctor_body(cfg, next_steps, fixable_actions)
        if fixable_actions:
            # --auto-fix is for the dashboard upgrade endpoint and other
            # non-tty invocations: applies every fix without prompting.
            # `--check` keeps the interactive default for terminal users.
            _apply_doctor_fixes(fixable_actions, assume_yes=auto_fix)
        else:
            console.print(
                "[green]Post-upgrade check clean — nothing to fix.[/green]"
            )
    else:
        console.print(
            "[green]Upgrade routine complete.[/green] "
            "[dim]Run `uv run meetingmind doctor` to verify.[/dim]"
        )


def _print_pending_changes(repo_root: Path) -> None:
    """Show commits between local HEAD and origin/main. Read-only."""
    if not _run_command(
        ["git", "fetch", "--quiet", "origin", "main"],
        cwd=repo_root,
    ):
        console.print(
            "[yellow]Couldn't fetch origin/main — check your network.[/yellow]"
        )
        return
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--decorate", "HEAD..origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.SubprocessError as exc:
        console.print(f"[red]Couldn't read commit log:[/red] {exc}")
        return
    pending = result.stdout.strip()
    if not pending:
        console.print(
            "[green]You're on the latest.[/green] Nothing to pull from origin/main."
        )
        return
    n_commits = len(pending.splitlines())
    console.print(
        Panel(
            pending,
            title=f"{n_commits} commit(s) pending",
            border_style="cyan",
        )
    )
    console.print(
        "[dim]Run `uv run meetingmind upgrade` (no --preview) to apply.[/dim]"
    )


def _create_pre_upgrade_tag(repo_root: Path) -> None:
    """Tag the current HEAD as `pre-upgrade-<stamp>` so the user has a
    one-command rollback (`git reset --hard pre-upgrade-<stamp>`)."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    tag = f"pre-upgrade-{stamp}"
    try:
        subprocess.run(
            ["git", "tag", "--no-sign", tag],
            cwd=repo_root,
            capture_output=True,
            check=True,
            timeout=5,
        )
        console.print(
            f"[dim]Tagged pre-upgrade state as `{tag}` (use "
            f"`git reset --hard {tag}` to roll back).[/dim]"
        )
    except subprocess.SubprocessError:
        # Tag creation is a nice-to-have; don't block the upgrade if it fails.
        pass


def _running_services(cfg: AppConfig) -> list[str]:
    """Return the set of managed services whose pid file points at a live process."""
    running: list[str] = []
    for name in MANAGED_SERVICES:
        pid = _read_pid(_service_pid_path(cfg, name))
        if pid is not None and _process_running(pid):
            running.append(name)
    return running


@cli.command()
def logs(
    service: str = typer.Argument(
        "backend",
        help="Which managed service's log to read: backend or frontend.",
    ),
    tail: int = typer.Option(80, "--tail", "-n", help="Show the last N lines."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new lines as they're written."),
) -> None:
    """Tail the log file for a managed service started by `meetingmind start`."""
    if service not in MANAGED_SERVICES:
        console.print(
            f"[red]Unknown service '{service}'.[/red] "
            f"Use one of: {', '.join(MANAGED_SERVICES)}."
        )
        raise typer.Exit(code=2)
    cfg = load_config()
    path = _service_log_path(cfg, service)
    if not path.exists():
        console.print(
            f"[yellow]No log yet at[/yellow] {path}\n"
            f"Start the service first with `uv run meetingmind start --{service}`."
        )
        raise typer.Exit(code=1)
    # Print last N lines straight from disk for the initial render.
    with path.open("rb") as fh:
        # Cheap "last N lines" approximation: read from end in chunks.
        block = 4096
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= tail:
            read_size = min(block, size)
            size -= read_size
            fh.seek(size)
            data = fh.read(read_size) + data
        lines = data.splitlines()[-tail:]
    for line in lines:
        console.print(line.decode(errors="replace"))
    if not follow:
        return
    console.print(f"[dim]── following {path} (Ctrl-C to stop) ──[/dim]")
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped following.[/dim]")


@cli.command()
def backup(
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Destination path for the snapshot tarball. Defaults to "
        "runtime/backups/meetingmind-YYYYMMDD-HHMMSS.tar.gz.",
    ),
    include_audio: bool = typer.Option(
        False,
        "--include-audio",
        help="Also include processed audio (data/processed). Off by default — large.",
    ),
) -> None:
    """Snapshot the SQLite database + Obsidian vault (optionally audio) into a
    single timestamped .tar.gz under runtime/backups/.
    """
    import datetime
    import tarfile

    cfg = load_config()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    default_dir = cfg.paths.runtime_dir / "backups"
    default_dir.mkdir(parents=True, exist_ok=True)
    destination = (out or default_dir / f"meetingmind-{stamp}.tar.gz").expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    included: list[Path] = []
    if cfg.paths.database_path.exists():
        included.append(cfg.paths.database_path)
    if cfg.paths.vault_dir.exists():
        included.append(cfg.paths.vault_dir)
    if include_audio and cfg.paths.processed_dir.exists():
        included.append(cfg.paths.processed_dir)
    if not included:
        console.print("[yellow]Nothing to back up yet — no database or vault on disk.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[dim]Writing {destination}…[/dim]")
    with tarfile.open(destination, "w:gz") as tar:
        for path in included:
            if path.is_relative_to(cfg.paths.repo_root):
                arcname = str(path.relative_to(cfg.paths.repo_root))
            else:
                # An out-of-repo path (e.g. an iCloud-hosted Obsidian vault).
                # Preserve the full path under an "external/" prefix so a
                # vault and a processed-audio dir that happen to share the
                # same basename can't collide inside the tarball.
                arcname = "external/" + str(path).lstrip("/")
            tar.add(path, arcname=arcname)
    size_mb = destination.stat().st_size / 1024 / 1024
    console.print(
        f"[green]Backup complete:[/green] {destination} ({size_mb:.1f} MB)"
    )
    for path in included:
        console.print(f"  · {path}")


@cli.command()
def reset(
    inbox: bool = typer.Option(False, "--inbox", help="Wipe data/inbox/*."),
    processed: bool = typer.Option(False, "--processed", help="Wipe data/processed/*."),
    runtime: bool = typer.Option(
        False,
        "--runtime",
        help="Wipe runtime/ artifacts AND the SQLite database. Forces a fresh `install` next start.",
    ),
    everything: bool = typer.Option(
        False,
        "--everything",
        help="Shortcut for --inbox --processed --runtime (the vault is never touched).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
) -> None:
    """Destructive cleanup helper. The Obsidian vault is *never* touched —
    if you need to wipe that too, do it manually.
    """
    cfg = load_config()
    targets: list[tuple[str, Path, bool]] = []  # (label, path, wipe-dir-content-only)
    if everything or inbox:
        targets.append(("inbox files", cfg.paths.inbox_dir, True))
    if everything or processed:
        targets.append(("processed audio", cfg.paths.processed_dir, True))
    if everything or runtime:
        # Runtime is a parent dir; we delete its children (database, embeddings,
        # waveforms, exports, etc.) but keep the dir itself so the layout
        # initializer doesn't have to re-create it.
        targets.append(("runtime artifacts (database, exports, caches)", cfg.paths.runtime_dir, True))
    if not targets:
        console.print(
            "[yellow]Nothing to do.[/yellow] Pass one of "
            "[cyan]--inbox[/cyan], [cyan]--processed[/cyan], [cyan]--runtime[/cyan], "
            "or [cyan]--everything[/cyan]."
        )
        raise typer.Exit(code=2)
    console.print(Panel(
        "\n".join(f"· {label} — {path}" for label, path, _ in targets),
        title="About to delete",
        border_style="red",
    ))
    console.print(
        "[yellow]The Obsidian vault is preserved. "
        "Run `meetingmind backup` first if you want a snapshot.[/yellow]"
    )
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Exit(code=1)
    for label, root, contents_only in targets:
        if not root.exists():
            continue
        if contents_only:
            for child in root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        else:
            shutil.rmtree(root, ignore_errors=True)
        console.print(f"[green]Cleared:[/green] {label}")
    ensure_local_layout(cfg)
    if everything or runtime:
        console.print(
            "[yellow]Runtime directories were recreated empty but the SQLite "
            "schema was not.[/yellow] Run [cyan]uv run meetingmind install[/cyan] "
            "before starting services."
        )
    else:
        console.print("[dim]Local layout re-ensured.[/dim]")


@cli.command("bootstrap-fixture")
def bootstrap_fixture_command(
    fixture_name: str = typer.Argument(
        "sample_company_q3_roadmap",
        help=(
            "Fixture module name (file under tests/eval/fixtures/ "
            "without the .py extension). Defaults to the synthetic "
            "Sample Co Q3 roadmap meeting."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Allow seeding when the live DB already has meetings. "
            "Default off — bootstrapping is meant for a fresh dev "
            "install; loading a fixture on top of real data would "
            "muddy the agent's view of what's synthetic vs real."
        ),
    ),
) -> None:
    """Seed a synthetic meeting into the live DB so the dashboard renders
    a fully-extracted meeting without needing any real user data.

    Use this on a fresh clone (with MEETINGMIND_DATA_HOME pointing at a
    sandbox directory) to give an agent / new contributor / curious
    tester something to poke around immediately. The fixture is fully
    synthetic — invented names, products, customers, decisions — so it
    is safe for an AI coding agent's working memory to ingest.

    Examples:
        mm bootstrap-fixture                          # default fixture
        mm bootstrap-fixture sample_company_q3_roadmap
        MEETINGMIND_DATA_HOME=$(mktemp -d) mm bootstrap-fixture
    """
    import importlib

    from app.db.database import connect, initialize_database

    cfg = load_config()
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    # Refuse to seed on top of an existing install unless --force.
    # Mixing synthetic fixture data with real meetings is the exact
    # contamination this command exists to prevent.
    with connect(cfg.paths.database_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    if existing > 0 and not force:
        console.print(
            f"[yellow]Refusing to bootstrap: live DB already has {existing} "
            "meeting(s).[/yellow]\n"
            "Bootstrap is intended for a fresh sandbox install. Either:\n"
            "  • Point MEETINGMIND_DATA_HOME at a new directory and re-run, or\n"
            "  • Pass --force to seed alongside existing data (not recommended)."
        )
        raise typer.Exit(1)

    if not re.fullmatch(r"[a-zA-Z0-9_]+", fixture_name):
        console.print(
            f"[red]Invalid fixture name '{fixture_name}'.[/red] "
            "Only letters, digits, and underscores are allowed."
        )
        raise typer.Exit(1)
    module_path = f"tests.eval.fixtures.{fixture_name}"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        console.print(
            f"[red]Fixture '{fixture_name}' not found.[/red] "
            f"Looked for `{module_path}` — error: {exc}"
        )
        raise typer.Exit(1) from exc
    fixture = getattr(module, "FIXTURE", None)
    if fixture is None:
        console.print(
            f"[red]Module `{module_path}` doesn't expose a FIXTURE constant.[/red]"
        )
        raise typer.Exit(1)

    from tests.eval.harness import seed_fixture

    meeting_id, segment_ids = seed_fixture(cfg, fixture)
    console.print(
        f"\n[green]Seeded fixture:[/green] {fixture.name} "
        f"(meeting_id={meeting_id}, {len(segment_ids)} segments)"
    )
    console.print(f"[dim]{fixture.description}[/dim]")
    console.print(
        "\nOpen the dashboard at "
        f"http://127.0.0.1:{cfg.runtime.dashboard_port}/ to review."
    )


@cli.command("migrate-user-data")
def migrate_user_data_command(
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Destination directory. Defaults to the platform-standard "
            "user-data location (macOS: ~/Library/Application Support/MeetingMind; "
            "Linux: $XDG_DATA_HOME/meetingmind; Windows: %APPDATA%/MeetingMind)."
        ),
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Default ON: print the moves without touching disk. Re-run with --apply once you've reviewed.",
    ),
    repair_paths_only: bool = typer.Option(
        False,
        "--repair-paths-only",
        help=(
            "Skip the file move and only rewrite DB-stored absolute paths "
            "(audio source / processed / vault export targets). Use this "
            "when a previous migration moved files on disk but left DB "
            "rows pointing at the old location — audio playback breaks "
            "until the DB catches up. Idempotent; safe to re-run."
        ),
    ),
) -> None:
    """Move user data (runtime + data + vault) out of the repo path.

    Pre-user-data-dir installs wrote audio, transcripts, the SQLite DB,
    and the Obsidian vault into folders inside the cloned repo. That
    works but means routine `git` operations and any tool walking the
    checkout (linters, indexers, AI coding agents) see private content.
    This command relocates everything to the platform-standard user-data
    directory and rewrites `config/local.toml` to point at the new
    location.

    Default is `--dry-run`: prints every move and config edit, no disk
    changes. Re-run with `--apply` to execute. On apply we use
    `shutil.move`, which DELETES the source after a successful copy
    (this is how `mv` works on POSIX too). Before the move runs you
    get one interactive confirmation prompt showing the full plan, so
    a typo on the command line can't cost you data. If you'd rather
    keep the originals as a manual safety belt, copy them somewhere
    else first.
    """
    import shutil

    from app.config import REPO_ROOT, _default_user_data_root, save_config

    cfg = load_config()
    target_root = Path(target).expanduser().resolve() if target else _default_user_data_root()

    # --repair-paths-only mode: an earlier migration moved disk paths but
    # left DB rows pinned to the old absolute paths (audio playback
    # blows up because storage_path / imported_path point at files that
    # no longer exist). This branch does ONLY the DB rewrite using the
    # repo's REPO_ROOT as the old root and the current live config as
    # the new root. Idempotent.
    if repair_paths_only:
        if target:
            # `--target` only steers the file-move destination; in
            # repair mode there's no move, so the flag is a user
            # misconception. Warn loudly rather than silently ignoring.
            console.print(
                "[yellow]--target is ignored in --repair-paths-only mode "
                "(no file move happens; DB paths are repaired to match "
                "the live config).[/yellow]"
            )
        rewrites = _plan_db_path_rewrites(cfg, old_root=REPO_ROOT.resolve())
        if not rewrites:
            console.print(
                "[green]Nothing to repair — every DB-stored path already "
                "points at the live data location.[/green]"
            )
            return
        console.print(
            f"[bold]DB path repair plan:[/bold] {len(rewrites)} row(s) carry "
            "stale absolute paths."
        )
        for table, column, old_value, new_value in rewrites[:10]:
            console.print(f"  {table}.{column}: {old_value} → {new_value}")
        if len(rewrites) > 10:
            console.print(f"  … and {len(rewrites) - 10} more")
        if dry_run:
            console.print("\n[cyan]Dry run. Re-run with --apply to execute.[/cyan]")
            return
        _apply_db_path_rewrites(cfg, rewrites)
        console.print(
            f"\n[green]Done.[/green] Rewrote {len(rewrites)} DB path(s). "
            "Restart any running MeetingMind services."
        )
        return

    # Map of (label, current path on disk, new path under target_root).
    # Built from the resolved live config so an install with custom paths
    # (e.g. a user who already moved their vault) gets handled correctly.
    moves: list[tuple[str, Path, Path]] = []
    for label, attr, subpath in [
        ("data dir", "data_dir", "data"),
        ("runtime", "runtime_dir", "runtime"),
        ("vault", "vault_dir", "vault/meeting_mind"),
    ]:
        src = getattr(cfg.paths, attr).resolve()
        dst = (target_root / subpath).resolve()
        if src == dst:
            continue  # already at the target — nothing to do
        moves.append((label, src, dst))

    if not moves:
        console.print("[green]Nothing to migrate — every path is already outside the repo.[/green]")
        return

    repo_root = REPO_ROOT.resolve()
    repo_relative_moves = [
        (label, src, dst)
        for label, src, dst in moves
        if _path_within(src, repo_root)
    ]
    if not repo_relative_moves:
        console.print(
            "[yellow]All configured paths already live outside the repo — "
            "no migration needed.[/yellow]"
        )
        return

    console.print(f"[bold]Migration plan:[/bold] target={target_root}")
    for label, src, dst in moves:
        in_repo = "[red](inside repo)[/red]" if _path_within(src, repo_root) else "[dim](already external)[/dim]"
        console.print(f"  {label:8s}  {src}  →  {dst}  {in_repo}")
    console.print(
        "\nConfig file will be updated: "
        f"{cfg.config_path} (paths section rewritten to the new locations)"
    )

    if dry_run:
        console.print("\n[cyan]Dry run. Re-run with --apply to execute.[/cyan]")
        return

    # Interactive confirm — `--apply` alone isn't enough of a gate for
    # an operation that DELETES source directories after copying. A
    # mistyped path or a wrong working directory could otherwise destroy
    # the user's only copy of their meeting data. typer.confirm prints
    # to stderr by default; abort=True raises typer.Abort on "no".
    typer.confirm(
        "\nThis MOVES (copies, then deletes) the source directories listed above. "
        "Continue?",
        abort=True,
    )

    # Apply phase: move directories, then rewrite config. Use shutil.move
    # which falls back to copy+delete across filesystems — important
    # because the user-data dir often lives on a different volume than
    # the repo (cloud-synced ~/Library vs an external drive). The source
    # is removed on success.
    target_root.mkdir(parents=True, exist_ok=True)
    for label, src, dst in moves:
        if not src.exists():
            console.print(f"[dim]Skipping {label}: source {src} doesn't exist[/dim]")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            console.print(
                f"[red]Refusing to overwrite existing {label} at {dst}.[/red] "
                "Move or delete it first, then re-run."
            )
            raise typer.Exit(1)
        console.print(f"Moving {label}: {src} → {dst}")
        shutil.move(str(src), str(dst))

    # Rewrite config to point at the new locations. The deep-merge in
    # load_config will pick these up on the next start.
    cfg.paths.data_dir = target_root / "data"
    cfg.paths.inbox_dir = target_root / "data" / "inbox"
    cfg.paths.processed_dir = target_root / "data" / "processed"
    cfg.paths.archive_dir = target_root / "data" / "archive"
    cfg.paths.delete_review_dir = target_root / "data" / "delete-review"
    cfg.paths.runtime_dir = target_root / "runtime"
    cfg.paths.database_path = target_root / "runtime" / "meetingmind.sqlite3"
    cfg.paths.vault_dir = target_root / "vault" / "meeting_mind"
    save_config(cfg)
    ensure_local_layout(cfg)

    # Rewrite DB-stored absolute paths so audio playback / vault exports
    # don't break. Without this, `meetings.imported_path`,
    # `meetings.source_path`, `source_files.storage_path`, and
    # `obsidian_exports.output_path` would keep pointing at the old
    # repo-relative location and every `/api/meetings/{id}/audio` call
    # would 404 even though the file is on disk at the new path.
    rewrites = _plan_db_path_rewrites(cfg, old_root=REPO_ROOT.resolve())
    if rewrites:
        console.print(f"\nRewriting {len(rewrites)} DB path(s)…")
        _apply_db_path_rewrites(cfg, rewrites)

    console.print(
        f"\n[green]Done.[/green] User data now lives at {target_root}. "
        "Restart any running MeetingMind services to pick up the new paths."
    )


def _path_within(child: Path, parent: Path) -> bool:
    """Return True iff `child` resolves under `parent`. Used by the
    migrate-user-data command to decide which paths are "inside the
    repo" and therefore need to move.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# (table, column, optional sibling-column-anchor) — DB columns that
# store absolute filesystem paths and need rewriting when user data
# moves. Keep this list in sync with the schema in `app.db.database`.
# `meetings` has BOTH source_path (inbox-relative) and imported_path
# (processed-relative); they came from different `data/` subdirs so
# the rewrite swaps `data/inbox/...` and `data/processed/...` chunks
# correctly. `obsidian_exports.output_path` is a vault path, rewritten
# under the new vault_dir.
_DB_PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("meetings", "source_path"),
    ("meetings", "imported_path"),
    ("source_files", "storage_path"),
    ("obsidian_exports", "output_path"),
)


def _plan_db_path_rewrites(
    cfg: AppConfig, *, old_root: Path
) -> list[tuple[str, str, str, str]]:
    """Walk every DB row in `_DB_PATH_COLUMNS` and figure out which
    ones still start with the legacy `old_root` path. Returns a list
    of `(table, column, old_value, new_value)` tuples that the apply
    step can run as parameterized UPDATEs.

    Paths under `old_root/data/...` get rewritten to
    `cfg.paths.data_dir/...`; paths under `old_root/vault/...` rewrite
    under `cfg.paths.vault_dir`'s parent so the meeting subfolder
    structure is preserved. Other path shapes that don't fit either
    pattern are skipped — they may be intentional custom locations
    the user set up, and silently rewriting them would be worse than
    leaving them alone for the user to fix manually.
    """
    from app.db.database import connect

    old_root_str = str(old_root)
    legacy_data_dir = (old_root / "data").resolve()
    legacy_vault_root = (old_root / "vault").resolve()
    new_data_dir = cfg.paths.data_dir.resolve()
    new_vault_root = cfg.paths.vault_dir.parent.resolve()

    rewrites: list[tuple[str, str, str, str]] = []
    with connect(cfg.paths.database_path) as conn:
        for table, column in _DB_PATH_COLUMNS:
            # Table + column come from the hardcoded `_DB_PATH_COLUMNS`
            # tuple at module load; the only user-supplied value is the
            # legacy root which goes through a parameter. Pin B608 on
            # the assembled query line so bandit's per-line scan picks
            # up the suppression.
            query = f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE ?"  # nosec B608
            try:
                rows = conn.execute(
                    query, (f"{old_root_str}%",)
                ).fetchall()
            except Exception as exc:  # noqa: BLE001 — schema drift shouldn't crash
                _LOGGER.warning(
                    "migrate_db_paths_skipped table=%s column=%s err=%s",
                    table, column, exc,
                )
                continue
            for row in rows:
                old_value = str(row[column])
                new_value = _rewrite_legacy_path(
                    old_value,
                    legacy_data_dir=legacy_data_dir,
                    legacy_vault_root=legacy_vault_root,
                    new_data_dir=new_data_dir,
                    new_vault_root=new_vault_root,
                )
                if new_value and new_value != old_value:
                    rewrites.append((table, column, old_value, new_value))
    return rewrites


def _rewrite_legacy_path(
    old_value: str,
    *,
    legacy_data_dir: Path,
    legacy_vault_root: Path,
    new_data_dir: Path,
    new_vault_root: Path,
) -> str | None:
    """Map a single legacy absolute path to its new location, or return
    None when the path doesn't fit either the data/ or vault/ rewrite
    pattern (caller leaves those alone for manual review).
    """
    try:
        old_path = Path(old_value).resolve()
    except OSError:
        return None
    # Data subtree: includes inbox/, processed/, archive/, delete-review/
    try:
        rel = old_path.relative_to(legacy_data_dir)
        return str(new_data_dir / rel)
    except ValueError:
        pass
    # Vault subtree: meeting_mind/ + arbitrary year/slug subpaths
    try:
        rel = old_path.relative_to(legacy_vault_root)
        return str(new_vault_root / rel)
    except ValueError:
        pass
    return None


def _apply_db_path_rewrites(
    cfg: AppConfig, rewrites: list[tuple[str, str, str, str]]
) -> None:
    """Run the planned rewrites as parameterized UPDATEs inside a
    single transaction. All-or-nothing — if any UPDATE fails the
    whole transaction rolls back and the user is left in the prior
    consistent state.
    """
    from app.db.database import connect

    with connect(cfg.paths.database_path) as conn:
        for table, column, old_value, new_value in rewrites:
            # Table + column come from the hardcoded `_DB_PATH_COLUMNS`
            # tuple; the user-supplied values go through parameters.
            # B608 pinned on the assembled query line so bandit's
            # per-line scan picks up the suppression.
            query = f"UPDATE {table} SET {column} = ? WHERE {column} = ?"  # nosec B608
            conn.execute(query, (new_value, old_value))


@cli.command("dev")
def dev() -> None:
    """Start the FastAPI backend in reload mode for local development."""
    cfg = load_config()
    _require_initialized(cfg)
    command = [
        "uvicorn",
        "app.main:app",
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        str(cfg.runtime.backend_port),
        "--reload",
    ]
    if VERBOSE:
        console.print(f"[dim]$ {shlex.join(command)}[/dim]")
    try:
        # check=True so an uvicorn crash surfaces a non-zero exit instead
        # of returning to the prompt as if the dev server was never alive.
        subprocess.run(command, check=True)
    except KeyboardInterrupt:
        # Clean Ctrl-C is normal; just exit quietly.
        raise typer.Exit(code=0)
    except FileNotFoundError as exc:
        console.print(f"[red]uvicorn not found:[/red] {exc}")
        console.print(
            "[yellow]Run `uv sync --extra dev` to install the dev dependencies.[/yellow]"
        )
        raise typer.Exit(code=127)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]uvicorn exited with code {exc.returncode}.[/red]")
        console.print(
            "Re-run with [cyan]uv run meetingmind --verbose dev[/cyan] to see the exact "
            "invocation, or check `runtime/logs/backend.log` if you were running via `start`."
        )
        raise typer.Exit(code=exc.returncode)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
