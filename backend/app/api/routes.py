"""HTTP routes for the MeetingMind backend.

SECURITY NOTE — these routes have NO authentication and include destructive
operations (delete meetings, delete inbox files, delete workstreams). They are
designed for loopback-only use bound to 127.0.0.1. Do NOT expose this API on a
public interface, behind a reverse proxy, or via `--host 0.0.0.0` without
adding an authentication layer in front of it.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Literal

from app.config import AppConfig, ensure_local_layout, load_config, save_config
from app.db.database import connect
from app.services.asr_candidates import (
    accept_transcript_candidate,
    reject_transcript_candidate,
    run_asr_candidate_passes,
)
from app.services.asr_vocabulary import load_custom_vocabulary_terms
from app.services.audio import is_supported_media, looks_like_audio
from app.services.aux_features import (
    add_segment_comment,
    build_archive_timeline,
    build_waveform,
    delete_segment_comment,
    get_person,
    list_people,
    list_segment_comments,
    list_segment_edits,
    rename_person,
    resolve_segment_comment,
    revert_segment_to,
)
from app.services.extraction import (
    TEMPLATE_PROMPTS,
    build_transcript_markdown,
    extract_meeting_atoms,
    regenerate_meeting_synthesis,
    update_meeting_summary,
)
from app.services.html_export import render_meeting_html_string
from app.services.ingestion import ingest_pending_files
from app.services.model_bus import (
    RECOMMENDED_MODELS,
    list_lm_studio_models,
    list_ollama_models,
)
from app.services.obsidian_writer import (
    build_meeting_overview,
    delete_generated_workstream,
    delete_meeting,
    promote_meeting,
    rename_generated_workstream,
    render_promoted_meeting_preview,
    write_staged_meeting,
)
from app.services.owner import clear_owner, load_owner, set_owner, suggest_owner
from app.services.pdf_export import write_meeting_pdf
from app.services.pipeline import process_meeting_audio
from app.services.review import approve_speaker_label
from app.services.scheduler import (
    configure_daily_maintenance,
    list_scheduled_jobs,
    run_scheduled_job,
    seed_default_scheduled_jobs,
    set_scheduled_job_enabled,
)
from app.services.search import search_meeting_index, workstream_intelligence
from app.services.synthesis import build_synthesis_snapshot
from app.services.transcript_editor import (
    correct_segment_text,
    merge_segment_with_next,
    reassign_segment_speaker,
    reassign_speaker_segments,
    split_segment_at_ms,
)
from app.services.vault_lint import lint_vault
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
LOGGER = logging.getLogger(__name__)

# UPDATE used to clear orphan `processing_jobs` at restart time. Pulled
# out so tests reuse the exact SQL instead of duplicating it.
#
# Status enum: 'running', 'queued', 'pending' are the non-terminal
# states. 'partial' (written by asr_candidates.py:135 when SOME but not
# all ASR candidates succeed) is intentionally OMITTED — it's written
# alongside `completed_at = CURRENT_TIMESTAMP` in the same UPDATE, so
# it's terminal-by-construction and can't be orphaned. 'complete' and
# 'failed' are skipped for the same reason.
_SWEEP_ORPHAN_JOBS_SQL = (
    "UPDATE processing_jobs SET status = 'failed', "
    "error = 'orphaned at restart', "
    "completed_at = CURRENT_TIMESTAMP "
    "WHERE status IN ('running', 'queued', 'pending')"
)
UPLOAD_FILE = File(...)
NOT_FOUND_ERRORS = {
    "candidate_not_found",
    "meeting_not_found",
    "next_segment_not_found",
    "segment_not_found",
    "source_not_found",
    # v0.2.8 audit M1: review-item-not-found should be 404, not 400.
    "review_item_not_found",
    # v0.2.10: rename-target person doesn't exist → 404.
    "person_not_found",
}
CONFLICT_ERRORS = {
    "candidate_not_suggested",
    "merge_requires_same_speaker",
    # v0.2.8 audit M1: trying to accept-twice or accept-rejected returns 409.
    "already_resolved",
    # v0.2.8 audit M1: hitting an accept/reject endpoint with a review
    # item of the wrong kind is a 409, not a 400.
    "not_a_reattribution",
    # v0.2.10 Pass D: wrong-kind for accept/reject-split endpoints.
    "not_a_split_proposal",
    # v0.2.10 audit H2: live segment text drifted from the proposal's
    # snapshot; refuse to apply rather than silently overwriting the
    # user's manual edits.
    "segment_changed",
    # v0.2.10: renaming a person to the same name they already have →
    # 409 (caller's UI should treat as a no-op rather than an error).
    "same_name",
}


def _tildefy_path(path: Path) -> str:
    """Replace the user's home prefix with `~` for display in API responses.

    Lets the dashboard show users where their inbox / vault is without
    leaking the local username over Tailscale or to any other observer on
    the same host. If the path isn't under HOME (e.g. a repo-local layout),
    returns the absolute path unchanged — that's not username-bearing.
    """
    try:
        home = Path.home().resolve()
        resolved = path.resolve()
        return f"~/{resolved.relative_to(home)}"
    except (ValueError, OSError):
        return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    """Path.is_relative_to that returns bool instead of raising."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_repo_path(cfg: AppConfig, stored_path: str) -> Path:
    """Resolve a DB-stored audio path and enforce it lives in a data dir.

    Audit findings H-2 (v0.1.2) and H-B (v0.1.3): `repo_root / stored_path`
    does NOT protect against absolute or `..`-laden paths — Python's `/`
    operator drops the left operand when the right is absolute (so
    `repo_root / "/etc/passwd"` is literally `/etc/passwd`). The v0.1.2
    fix asserted containment against `repo_root`, which was too generous:
    a tampered DB row could still target `.env.local`, the SQLite file
    itself, or source code inside the repo. This tightened version
    asserts the resolved path lives in one of the actual audio data
    dirs (`processed_dir`, `delete_review_dir`, `archive_dir` if set).

    Raises HTTPException(404) on containment failure — same code as
    "audio_not_found" so we don't leak the existence of the targeted file.
    """
    allowed_roots = [
        cfg.paths.processed_dir.resolve(),
        cfg.paths.delete_review_dir.resolve(),
    ]
    # archive_dir is optional in older configs; tolerate absence.
    archive_dir = getattr(cfg.paths, "archive_dir", None)
    if archive_dir is not None:
        allowed_roots.append(Path(archive_dir).resolve())
    candidate = (cfg.paths.repo_root / stored_path).resolve()
    if not any(_is_relative_to(candidate, root) for root in allowed_roots):
        raise HTTPException(status_code=404, detail="audio_not_found")
    return candidate


def _raise_api_value_error(exc: ValueError) -> None:
    """Translate service-layer validation errors into stable API status codes."""
    detail = str(exc)
    if detail in NOT_FOUND_ERRORS:
        status_code = 404
    elif detail in CONFLICT_ERRORS:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=detail) from exc


class DashboardSettingsUpdate(BaseModel):
    """Validated settings payload mirrored by the dashboard Settings page."""

    dashboard_port: int = Field(ge=1024, le=65535)
    backend_port: int = Field(ge=1024, le=65535)
    model_provider: Literal["lm_studio", "ollama", "openrouter"]
    default_model: str = Field(min_length=1)
    quality_model: str
    lm_studio_base_url: str = Field(min_length=1)
    ollama_base_url: str = Field(min_length=1)
    model_idle_ttl_seconds: int = Field(ge=60, le=86400)
    model_temperature: float = Field(ge=0.0, le=2.0)
    auto_audio_repair: bool
    vocal_presentation_cue_scoring: bool
    show_key_term_highlights: bool = False
    show_transcript_confidence_chips: bool = False
    default_template: str = "general"
    auto_send_to_obsidian: bool = False
    asr_vocabulary_terms: list[str] = Field(default_factory=list)

    @field_validator("default_model", "quality_model", mode="before")
    @classmethod
    def _strip_model_name(cls, value: str) -> str:
        cleaned = str(value).strip()
        # Audit M-A: reject obviously-malformed model names at the API
        # boundary so they never reach `lms load --ttl ... <model>` in
        # the first place. quality_model is optional so empty is fine.
        if cleaned and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./@:-]{0,200}", cleaned):
            raise ValueError("invalid_model_name")
        return cleaned

    @field_validator("lm_studio_base_url", "ollama_base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        clean = str(value).strip()
        if not clean.startswith(("http://", "https://")):
            raise ValueError("base_url_must_start_with_http")
        return clean


def _obsidian_available(cfg: AppConfig) -> bool:
    """Heuristic: the user has Obsidian if the vault directory exists.
    The install wizard either creates it (when the user opted into the
    repo-local vault) or points to one the user already had. Either way,
    presence = ready to receive a 'Send to Obsidian' click. If absent,
    the UI disables the send button.
    """
    return cfg.paths.vault_dir.exists()


@router.get("/health")
def health() -> dict:
    cfg = load_config()
    if cfg.models.provider == "ollama":
        model_status = {"ollama_models": list_ollama_models()}
    else:
        model_status = {"lm_studio_models": list_lm_studio_models()}
    # Audit finding M-4: absolute filesystem paths leak host topology
    # and the local username (`/Users/<name>/...`). Substitute `~` for the
    # home directory so the dashboard can still show users *where* their
    # inbox / vault lives without exposing the username to anyone else on
    # the Tailnet. Removed `asr_vocabulary_path` outright — the dashboard
    # never displayed it.
    return {
        "status": "ok",
        "inbox": _tildefy_path(cfg.paths.inbox_dir),
        "vault": _tildefy_path(cfg.paths.vault_dir),
        "obsidian_available": _obsidian_available(cfg),
        "provider": cfg.models.provider,
        "dashboard_port": cfg.runtime.dashboard_port,
        "asr_vocabulary_terms": len(load_custom_vocabulary_terms(cfg)),
    } | model_status


@router.get("/setup-status")
def setup_status() -> dict:
    """Dashboard-facing setup checklist. Mirrors the same checks `doctor`
    runs but returns structured JSON so the Inbox screen can show a
    welcoming checklist to first-time testers until everything is green.
    """
    cfg = load_config()
    items: list[dict] = []

    has_hf = bool(
        os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_TOKEN", "").strip()
    )
    items.append({
        "id": "hf_token",
        "label": "Hugging Face token",
        "ok": has_hf,
        "detail": "set" if has_hf else "Paste your HF token in Settings · Models",
        "action": "settings:models" if not has_hf else None,
    })

    if cfg.models.provider == "openrouter":
        has_or_key = bool(
            os.environ.get(cfg.models.openrouter_api_key_env, "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )
        items.append({
            "id": "openrouter_key",
            "label": "OpenRouter API key",
            "ok": has_or_key,
            "detail": "set" if has_or_key else "Paste your OpenRouter key in Settings · Models",
            "action": "settings:models" if not has_or_key else None,
        })
    elif cfg.models.provider in ("lm_studio", "ollama"):
        models = list_lm_studio_models() if cfg.models.provider == "lm_studio" else list_ollama_models()
        provider_label = "LM Studio" if cfg.models.provider == "lm_studio" else "Ollama"
        items.append({
            "id": "local_provider",
            "label": f"{provider_label} model loaded",
            "ok": bool(models),
            "detail": f"{len(models)} models available" if models else f"Start {provider_label} and load at least one model",
            "action": None,
        })

    owner = load_owner(cfg)
    items.append({
        "id": "owner_identity",
        "label": "Your identity set",
        "ok": owner.configured,
        "detail": f"You are '{owner.display_name}'" if owner.configured else "Tell MeetingMind who you are in People · Onboarding",
        "action": "people:onboarding" if not owner.configured else None,
    })

    pyannote_cached = (
        (Path.home() / ".cache" / "huggingface" / "hub" / "models--pyannote--speaker-diarization-community-1").exists()
        and (Path.home() / ".cache" / "huggingface" / "hub" / "models--pyannote--embedding").exists()
    )
    items.append({
        "id": "pyannote_cached",
        "label": "Pyannote diarization models cached",
        "ok": pyannote_cached,
        "detail": "ready" if pyannote_cached else "Run `meetingmind doctor --fix` to pre-download (skips the first-ingest stall)",
        "action": None,
    })

    blocking = [item for item in items if not item["ok"]]
    return {
        "items": items,
        "ready": not blocking,
        "blocker_count": len(blocking),
    }


@router.get("/settings")
def settings() -> dict:
    cfg = load_config()
    lm_studio_models = list_lm_studio_models()
    ollama_models = list_ollama_models()
    openrouter_api_key_set = bool(
        os.environ.get(cfg.models.openrouter_api_key_env, "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
    )
    huggingface_token_set = bool(
        os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_TOKEN", "").strip()
    )
    return {
        "config_path": str(cfg.config_path),
        "dashboard": {
            "port": cfg.runtime.dashboard_port,
            "url": f"http://127.0.0.1:{cfg.runtime.dashboard_port}",
            "restart_required_for_port": True,
        },
        "backend": {
            "port": cfg.runtime.backend_port,
            "url": f"http://127.0.0.1:{cfg.runtime.backend_port}",
            "restart_required_for_port": True,
        },
        "models": {
            "provider": cfg.models.provider,
            "default_model": cfg.models.default_model,
            "quality_model": cfg.models.quality_model,
            "lm_studio_base_url": cfg.models.lm_studio_base_url,
            "ollama_base_url": cfg.models.ollama_base_url,
            "idle_ttl_seconds": cfg.models.idle_ttl_seconds,
            "temperature": cfg.models.temperature,
            "lm_studio_models": lm_studio_models,
            "ollama_models": ollama_models,
            "recommendations": RECOMMENDED_MODELS,
            "openrouter": {
                "base_url": cfg.models.openrouter_base_url,
                "api_key_env": cfg.models.openrouter_api_key_env,
                "api_key_set": openrouter_api_key_set,
                "default_model": cfg.models.openrouter_default_model,
            },
        },
        "huggingface": {
            "token_set": huggingface_token_set,
            "model_access_urls": [
                "https://huggingface.co/pyannote/speaker-diarization-community-1",
                "https://huggingface.co/pyannote/embedding",
            ],
        },
        "transcription": {
            "auto_audio_repair": cfg.asr.auto_repair_after_process,
            "vocal_presentation_cue_scoring": (
                cfg.review.vocal_presentation_cue_scoring_enabled
            ),
            "vocal_presentation_cue_max_boost": cfg.review.vocal_presentation_cue_max_boost,
            "asr_vocabulary_path": str(cfg.asr.vocabulary_path),
            "asr_vocabulary_terms": cfg.asr.vocabulary_terms,
            "asr_vocabulary_file_terms": len(load_custom_vocabulary_terms(cfg)),
        },
        "dashboard_prefs": {
            "show_key_term_highlights": cfg.dashboard.show_key_term_highlights,
            "show_transcript_confidence_chips": (
                cfg.dashboard.show_transcript_confidence_chips
            ),
            "default_template": cfg.dashboard.default_template,
            "auto_send_to_obsidian": cfg.dashboard.auto_send_to_obsidian,
        },
    }


@router.post("/settings")
def update_settings(payload: DashboardSettingsUpdate) -> dict:
    cfg = load_config().model_copy(deep=True)
    cfg.runtime.dashboard_port = payload.dashboard_port
    cfg.runtime.backend_port = payload.backend_port
    cfg.models.provider = payload.model_provider
    cfg.models.default_model = payload.default_model.strip()
    cfg.models.quality_model = payload.quality_model.strip() or cfg.models.default_model
    cfg.models.lm_studio_base_url = payload.lm_studio_base_url.strip()
    cfg.models.ollama_base_url = payload.ollama_base_url.strip()
    cfg.models.idle_ttl_seconds = payload.model_idle_ttl_seconds
    cfg.models.temperature = payload.model_temperature
    cfg.asr.auto_repair_after_process = payload.auto_audio_repair
    cfg.review.vocal_presentation_cue_scoring_enabled = (
        payload.vocal_presentation_cue_scoring
    )
    cfg.dashboard.show_key_term_highlights = payload.show_key_term_highlights
    cfg.dashboard.show_transcript_confidence_chips = (
        payload.show_transcript_confidence_chips
    )
    cfg.dashboard.auto_send_to_obsidian = payload.auto_send_to_obsidian
    default_template = (payload.default_template or "general").strip()
    if default_template not in TEMPLATE_PROMPTS:
        default_template = "general"
    cfg.dashboard.default_template = default_template
    cfg.asr.vocabulary_terms = [
        term.strip()
        for term in payload.asr_vocabulary_terms
        if term.strip()
    ][:200]
    save_config(cfg)
    return {"status": "ok", "settings": settings()}


class OpenRouterKeyUpdate(BaseModel):
    api_key: str


def _sanitize_env_value(raw: str) -> str:
    """Strip every character Python's `str.splitlines()` treats as a line break.

    The v0.1.2 fix only handled `\\n` / `\\r` / `\\x00`, but a follow-up audit
    showed that `_read_env_file` uses `splitlines()` which ALSO splits on:
      - `\\v` (`\\x0b`), `\\f` (`\\x0c`)
      - `\\x1c` (FS), `\\x1d` (GS), `\\x1e` (RS)
      - `\\x85` (NEL)
      - U+2028 (LINE SEPARATOR), U+2029 (PARAGRAPH SEPARATOR)
    Any of those surviving in a saved secret splits into a fresh
    `KEY=value` line on the next backend start, re-opening the C-1
    injection class. We strip the full set here.
    """
    cleaned = raw.strip()
    return _ENV_VALUE_SCRUB_RE.sub("", cleaned)


# Every character Python's str.splitlines() recognises as a line break.
# Keep the explicit \u escapes — those U+2028/U+2029 chars are otherwise
# invisible in editor diffs and easy to lose in copy-paste.
_ENV_VALUE_SCRUB_RE = re.compile(
    "[\r\n\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]"
)


_VALID_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _write_env_var(var_name: str, new_value: str) -> None:
    """Idempotent .env.local rewrite: replace VAR=… in place or append.

    `new_value` MUST already be sanitized via `_sanitize_env_value` — callers
    are responsible. We re-sanitize defensively here so a missed caller can't
    inject `.env.local` lines.

    Audit M-D: validate `var_name` against the conventional UPPER_SNAKE
    env-var shape so a misconfigured `openrouter_api_key_env` (operator-
    editable in `local.toml`) can never clobber `PATH`, `HOME`, etc. on
    next process start.
    """
    if not _VALID_ENV_VAR_NAME_RE.fullmatch(var_name):
        raise HTTPException(status_code=400, detail="invalid_env_var_name")
    from app.config import REPO_ROOT as _REPO_ROOT

    new_value = _sanitize_env_value(new_value)
    env_path = _REPO_ROOT / ".env.local"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    found = False
    rewritten: list[str] = []
    for line in lines:
        if line.lstrip().startswith(f"{var_name}="):
            rewritten.append(f"{var_name}={new_value}")
            found = True
        else:
            rewritten.append(line)
    if not found:
        rewritten.append(f"{var_name}={new_value}")
    env_path.write_text("\n".join(rewritten) + "\n")
    os.environ[var_name] = new_value


@router.post("/settings/openrouter-key")
def update_openrouter_key(payload: OpenRouterKeyUpdate) -> dict:
    """Persist the OpenRouter API key into .env.local and load it into
    the running process so it takes effect without a backend restart.

    .env.local is gitignored. The key never lands in config/local.toml.
    Empty/whitespace values clear the existing key.
    """
    cfg = load_config()
    var_name = cfg.models.openrouter_api_key_env
    new_value = _sanitize_env_value(payload.api_key)
    _write_env_var(var_name, new_value)
    return {
        "status": "ok",
        "api_key_set": bool(new_value),
        "env_var": var_name,
    }


class HFTokenUpdate(BaseModel):
    token: str


@router.post("/settings/huggingface-token")
def update_hf_token(payload: HFTokenUpdate) -> dict:
    """Persist a Hugging Face token to .env.local. The token gates
    download of the pyannote diarization models. Treated the same as the
    OpenRouter key — gitignored, loaded into the running process so the
    diarization preload picks it up without restart.
    """
    new_value = _sanitize_env_value(payload.token)
    # Hugging Face official tokens are "hf_…" (typically 37 chars total).
    # Reject obviously invalid input rather than silently writing junk.
    if new_value and not new_value.startswith("hf_"):
        raise HTTPException(status_code=400, detail="hf_token_must_start_with_hf_")
    _write_env_var("HUGGING_FACE_HUB_TOKEN", new_value)
    return {"status": "ok", "token_set": bool(new_value)}


_VERSION_CACHE: dict = {"checked_at": 0.0, "payload": None}
_VERSION_CACHE_TTL_SECONDS = 3600  # 1 hour — GitHub rate-limits unauthenticated GETs at 60/hr


def _local_version() -> str:
    """Read the installed package version from importlib metadata."""
    try:
        from importlib.metadata import version

        return version("meetingmind")
    except Exception:  # noqa: BLE001 — never fail the dashboard over this
        return "0.0.0"


def _normalize_version(raw: str) -> tuple[int, ...]:
    """Crude semver comparator: strip leading v, split on '.', coerce ints.
    Non-numeric suffixes (rc1, dev0) are ignored. Good enough for our
    `vMAJOR.MINOR.PATCH` tagging scheme."""
    cleaned = raw.lstrip("vV ").strip()
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits or "0"))
    return tuple(parts)


@router.get("/system/version")
def system_version(force: bool = False) -> dict:
    """Current installed version + the latest GitHub release. Cached for
    an hour to avoid hammering the unauthenticated GitHub API (60 req/hr)."""
    now = time.time()
    cached_at = float(_VERSION_CACHE.get("checked_at") or 0.0)
    cached_payload = _VERSION_CACHE.get("payload")
    current = _local_version()
    # If the running interpreter's installed package version no longer
    # matches what the cached payload was built against — typically
    # because the user ran `mm upgrade` / `uv sync` outside the dashboard
    # while uvicorn kept running — drop the cache and re-fetch. Avoids
    # the stale-pill case where the dashboard reads "v0.2.14 available"
    # on an install that's already at 0.2.14.
    cache_current = (cached_payload or {}).get("current")
    cache_matches_install = cache_current is None or cache_current == current
    if (
        not force
        and cached_payload is not None
        and cache_matches_install
        and (now - cached_at) < _VERSION_CACHE_TTL_SECONDS
    ):
        return cached_payload
    latest: str | None = None
    notes: str = ""
    published_at: str | None = None
    error: str | None = None
    try:
        import httpx

        response = httpx.get(
            "https://api.github.com/repos/natebrockert/meeting_mind/releases/latest",
            timeout=4.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code == 200:
            data = response.json()
            raw_tag = (data.get("tag_name") or "").strip() or None
            # Audit finding M-3: `tag_name` comes from the GitHub API and
            # we used to interpolate it into `release_url` without
            # validation. GitHub release tags can theoretically contain
            # `/` and other URL-significant characters. We're the only
            # ones who cut tags for this repo so this is defensive — but
            # an attacker who compromised the GitHub account could push
            # a release tagged with junk to redirect users elsewhere.
            if raw_tag and re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?[\w.\-]*", raw_tag):
                latest = raw_tag
            else:
                latest = None
            notes = (data.get("body") or "").strip()
            published_at = data.get("published_at")
        elif response.status_code == 404:
            # No releases cut yet (or repo was renamed). Treat as "up to date".
            latest = None
        else:
            error = f"github_status_{response.status_code}"
    except Exception as exc:  # noqa: BLE001 — network issues are non-fatal
        error = type(exc).__name__

    upgrade_available = False
    if latest:
        try:
            upgrade_available = _normalize_version(latest) > _normalize_version(current)
        except Exception:  # noqa: BLE001
            upgrade_available = False

    payload = {
        "current": current,
        "latest": latest,
        "upgrade_available": upgrade_available,
        "release_notes": notes,
        "release_published_at": published_at,
        "release_url": (
            f"https://github.com/natebrockert/meeting_mind/releases/tag/{latest}"
            if latest
            else None
        ),
        "error": error,
        "checked_at": now,
    }
    _VERSION_CACHE["checked_at"] = now
    _VERSION_CACHE["payload"] = payload
    return payload


_UPGRADE_STATE = {"in_progress": False, "started_at": 0.0}
_UPGRADE_LOCK = threading.Lock()
# Stale "in_progress" flag protection: if a previous upgrade crashed before
# the backend restarted, the flag would stay True forever. After 15 minutes
# we consider the previous attempt dead and let a new one start.
_UPGRADE_STALE_SECONDS = 15 * 60


@router.post("/system/upgrade")
def system_upgrade() -> dict:
    """Spawn `meetingmind upgrade` in a detached subprocess and return.

    Same pattern as /system/restart — the backend can't run a process that
    will kill its own uvicorn from inside a request handler without
    losing the response. Frontend polls /api/health (and /api/system/version)
    until the new backend answers, then refreshes.

    Audit M-B sibling: two concurrent clicks on the Upgrade button (or
    two browser tabs both hitting /api/system/upgrade) would spawn two
    `uv run meetingmind upgrade` processes racing on the same git checkout.
    Guarded with a process-local lock + a stale-flag timeout so a crashed
    upgrade can't permanently disable the endpoint.
    """
    import subprocess as _sp

    from app.config import REPO_ROOT as _REPO_ROOT

    now = time.monotonic()
    with _UPGRADE_LOCK:
        if _UPGRADE_STATE["in_progress"]:
            age = now - _UPGRADE_STATE["started_at"]
            if age < _UPGRADE_STALE_SECONDS:
                raise HTTPException(status_code=409, detail="upgrade_already_in_progress")
            # Stale flag — previous attempt crashed without restarting us.
        _UPGRADE_STATE["in_progress"] = True
        _UPGRADE_STATE["started_at"] = now

    try:
        _sp.Popen(  # noqa: S603 — invoking our own CLI, no user input
            ["uv", "run", "meetingmind", "upgrade", "--auto-fix"],
            cwd=str(_REPO_ROOT),
            start_new_session=True,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            stdin=_sp.DEVNULL,
        )
    except FileNotFoundError as exc:
        # Spawn failed → release the flag immediately so the user can retry.
        with _UPGRADE_LOCK:
            _UPGRADE_STATE["in_progress"] = False
        raise HTTPException(
            status_code=500,
            detail=f"upgrade_failed: uv binary missing ({exc})",
        ) from exc
    # Invalidate version cache so the next call after restart shows fresh state.
    _VERSION_CACHE["checked_at"] = 0.0
    _VERSION_CACHE["payload"] = None
    return {"status": "upgrading"}


@router.post("/system/restart")
def restart_system() -> dict:
    """Spawn `meetingmind restart` in a detached subprocess and return.

    The backend can't kill its own uvicorn process from inside a request
    handler — we'd kill the response. The CLI's restart command already
    handles the kill+start dance, so we shell out to it in a new session
    and let it manage the lifecycle. The frontend then polls /api/health
    until the new process answers.

    Before the kill, sweep `processing_jobs` rows in non-terminal states.
    The dying process can't finish them, and the new process has no way
    to resume — they're inherently orphaned at the moment of restart.
    Without this sweep, the dashboard's "TRANSCRIPTION RUNNING" banner
    sticks forever on the restarted process, gating the user behind a
    fake in-progress job. Mark them 'failed' with an explicit error so
    the audit trail is preserved; the user can re-trigger from the
    dashboard.
    """
    import subprocess as _sp

    from app.config import REPO_ROOT as _REPO_ROOT
    from app.db.database import connect as _db_connect

    cfg = load_config()
    try:
        with _db_connect(cfg.paths.database_path) as conn:
            cursor = conn.execute(_SWEEP_ORPHAN_JOBS_SQL)
            swept_count = cursor.rowcount
    except Exception as exc:  # noqa: BLE001 — sweep is best-effort
        LOGGER.warning("restart_sweep_failed err=%s", exc)
        swept_count = 0

    try:
        _sp.Popen(  # noqa: S603 — invoking our own CLI, no user input
            ["uv", "run", "meetingmind", "restart"],
            cwd=str(_REPO_ROOT),
            start_new_session=True,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            stdin=_sp.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"restart_failed: uv binary missing ({exc})",
        ) from exc
    return {"status": "restarting", "swept_orphan_jobs": swept_count}


@router.post("/install")
def install() -> dict:
    cfg = load_config()
    ensure_local_layout(cfg)
    seed_default_scheduled_jobs(cfg)
    return {"status": "ok"}


def _resolve_template(cfg: AppConfig, explicit: str | None) -> str | None:
    """Pick the template to stamp on newly-ingested meetings.

    Priority: explicit query param > dashboard default > None (= 'general'
    is applied implicitly downstream by _template_for_meeting). Unknown
    values fall back to the dashboard default so a stale UI doesn't write
    an invalid template into the DB.
    """
    candidate = (explicit or "").strip()
    if candidate and candidate in TEMPLATE_PROMPTS:
        return candidate
    default = (cfg.dashboard.default_template or "").strip()
    if default and default in TEMPLATE_PROMPTS:
        return default
    return None


@router.post("/ingest")
def ingest(
    template: str | None = None,
    templates_json: str | None = None,
) -> dict:
    """Ingest pending files from the inbox folder.

    Per-file template overrides may be supplied as `templates_json` — a
    URL-encoded JSON map of {filename: template_id}. Files not in the map
    fall back to the `template` query param (legacy / bulk default), and
    unknown template ids fall through to None (downstream extraction
    treats None as 'general').
    """
    cfg = load_config()
    bulk_default = _resolve_template(cfg, template)
    per_file: dict[str, str | None] = {}
    if templates_json:
        try:
            raw = json.loads(templates_json)
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                resolved = _resolve_template(cfg, str(value or ""))
                if isinstance(name, str):
                    per_file[name] = resolved
    results = ingest_pending_files(cfg, template=bulk_default, template_map=per_file)
    return {
        "template": bulk_default,
        "results": [
            result.__dict__ | {"source_path": str(result.source_path)}
            for result in results
        ],
    }


@router.get("/inbox")
def inbox() -> dict:
    cfg = load_config()
    ensure_local_layout(cfg)
    files = []
    for path in sorted(cfg.paths.inbox_dir.iterdir()):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "supported": is_supported_media(path),
            }
        )
    return {"files": files}


@router.post("/upload")
def upload(
    file: UploadFile = UPLOAD_FILE,
    template: str | None = None,
) -> dict:
    cfg = load_config()
    ensure_local_layout(cfg)
    safe_name = Path(file.filename or "upload").name
    destination = _unique_inbox_destination(cfg.paths.inbox_dir, safe_name)
    max_bytes = cfg.uploads.max_upload_bytes
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=413, detail="upload_too_large")
    # Streaming size enforcement: Content-Length isn't always set (Transfer-
    # Encoding: chunked, multipart edge cases), so the size check above can
    # be bypassed. Count bytes as we copy and abort if the running total
    # exceeds the limit — the partial file is deleted so the inbox stays
    # clean.
    written = 0
    chunk_size = 1 << 20  # 1 MiB
    try:
        with destination.open("wb") as fh:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    fh.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="upload_too_large")
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    # Magic-byte check: reject obvious mismatches between the file
    # extension and the actual bytes (e.g. a renamed executable). The
    # check is suffix-aware; supported suffixes that fail the magic test
    # are rejected, and the partial upload is removed.
    if not looks_like_audio(destination):
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=415, detail="not_recognized_as_audio")
    chosen = _resolve_template(cfg, template)
    # Audit M-B: scope ingest to the file we just wrote. Without this,
    # two concurrent uploads each fired a full inbox scan and could
    # ingest the other's file, returning the wrong result to the caller.
    result = ingest_pending_files(cfg, template=chosen, only_filename=destination.name)
    return {
        "status": "ok",
        "template": chosen,
        "results": [item.__dict__ | {"source_path": str(item.source_path)} for item in result],
    }


@router.get("/meetings")
def list_meetings() -> dict:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, slug, duration_seconds, status, created_at
            FROM meetings
            ORDER BY id DESC
            """
        ).fetchall()
    return {"meetings": [dict(row) for row in rows]}


@router.get("/search")
def search(q: str = "", limit: int = Query(25, ge=1, le=100)) -> dict:
    cfg = load_config()
    return {"results": search_meeting_index(cfg, q, limit=limit)}


@router.get("/workstreams/intelligence")
def workstreams_intelligence(limit: int = Query(25, ge=1, le=100)) -> dict:
    cfg = load_config()
    return {"workstreams": workstream_intelligence(cfg, limit=limit)}


@router.delete("/workstreams")
def delete_workstream(title: str) -> dict:
    cfg = load_config()
    result = delete_generated_workstream(cfg, title)
    return {"status": "ok", **result}


@router.put("/workstreams")
def rename_workstream(title: str, new_title: str) -> dict:
    cfg = load_config()
    try:
        result = rename_generated_workstream(cfg, title, new_title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result}


class MeetingRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.patch("/meetings/{meeting_id}")
def rename_meeting(meeting_id: int, payload: MeetingRename) -> dict:
    """Rename a meeting. The slug is regenerated when the meeting hasn't
    been promoted yet (vault file doesn't exist, so nothing breaks), and
    preserved once promoted so vault paths don't move on rename.
    """
    cfg = load_config()
    new_title = payload.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="title_required")
    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT id, status FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="meeting_not_found")
        if row["status"] == "promoted":
            # Slug is load-bearing once the vault note exists; keep the
            # filename stable to avoid orphaning the previously-promoted
            # note on disk.
            conn.execute(
                "UPDATE meetings SET title = ? WHERE id = ?",
                (new_title, meeting_id),
            )
        else:
            from pathlib import Path as _Path

            from app.services.audio import slugify_filename
            from app.services.ingestion import _unique_slug

            base = slugify_filename(_Path(new_title))
            slug = _unique_slug(conn, base)
            conn.execute(
                "UPDATE meetings SET title = ?, slug = ? WHERE id = ?",
                (new_title, slug, meeting_id),
            )
    return {"status": "ok", "title": new_title}


@router.delete("/meetings/{meeting_id}")
def delete_meeting_route(meeting_id: int) -> dict:
    cfg = load_config()
    try:
        result = delete_meeting(cfg, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", **result}


@router.delete("/inbox")
def delete_inbox_file(path: str) -> dict:
    cfg = load_config()
    ensure_local_layout(cfg)
    inbox_root = cfg.paths.inbox_dir.resolve()
    if not inbox_root.is_dir():
        raise HTTPException(status_code=500, detail="inbox_misconfigured")
    candidate = Path(path).resolve()
    # Refuse anything outside the configured inbox directory (path traversal guard)
    try:
        candidate.relative_to(inbox_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path_not_in_inbox") from exc
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="file_not_found")
    try:
        candidate.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "removed": str(candidate)}


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: int) -> dict:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        meeting = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if not meeting:
            raise HTTPException(status_code=404, detail="meeting_not_found")
        segments = conn.execute(
            "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY start_ms",
            (meeting_id,),
        ).fetchall()
        review_items = conn.execute(
            "SELECT * FROM review_items WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
        assignments = conn.execute(
            """
            SELECT *
            FROM speaker_assignments
            WHERE meeting_id = ?
            ORDER BY diarization_speaker_id
            """,
            (meeting_id,),
        ).fetchall()
        source = conn.execute(
            """
            SELECT *
            FROM source_files
            WHERE meeting_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
        candidates = conn.execute(
            """
            SELECT *
            FROM transcript_candidates
            WHERE meeting_id = ?
              AND status != 'stale'
              AND status != 'superseded'
              AND status != 'rejected'
            ORDER BY segment_id, score DESC, id
            """,
            (meeting_id,),
        ).fetchall()
        speaker_evidence = conn.execute(
            """
            SELECT *
            FROM speaker_assignment_evidence
            WHERE meeting_id = ?
            ORDER BY segment_id
            """,
            (meeting_id,),
        ).fetchall()
        # v0.2.9: surface overlap hints persisted by the v0.2.2 detector
        # so the frontend can render a badge on each affected transcript
        # row. Small table — typical meeting has <50 hints.
        #
        # Audit H1 (pre-merge): the detector can emit multiple kinds for
        # the same segment (e.g., a yield_marker + a rapid_alternation),
        # and there's no UNIQUE constraint on (meeting_id, segment_id).
        # The frontend collapses to a Map<segment_id, hint>, so which row
        # wins must be deterministic. Order by confidence DESC, then kind,
        # so the highest-confidence hint per segment is the one rendered.
        overlap_hints = conn.execute(
            """
            SELECT segment_id, partner_segment_id, kind, evidence, confidence
            FROM segment_overlap_hints
            WHERE meeting_id = ?
            ORDER BY segment_id, confidence DESC, kind
            """,
            (meeting_id,),
        ).fetchall()
    # Cold-cache parallelization: synthesis_snapshot fires key-terms,
    # build_meeting_overview fires drivers + enrichment + health. On a
    # freshly-extracted meeting these are independent ~30-60s LLM calls
    # against OpenRouter; running them concurrently roughly halves the
    # first-load wait. Warm-cache (every reload after extraction) each
    # path is ~10ms so the executor overhead is negligible.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        synthesis_future = executor.submit(build_synthesis_snapshot, cfg, meeting_id)
        overview_future = executor.submit(build_meeting_overview, cfg, meeting_id)
        synthesis = synthesis_future.result()
        overview = overview_future.result()
    return {
        "meeting": dict(meeting),
        "segments": [dict(row) for row in segments],
        "review_items": [dict(row) for row in review_items],
        "assignments": [dict(row) for row in assignments],
        "source_file": dict(source) if source else None,
        "candidates": [dict(row) for row in candidates],
        "speaker_evidence": [dict(row) for row in speaker_evidence],
        "overlap_hints": [dict(row) for row in overlap_hints],
        "synthesis": synthesis,
        "overview": overview,
        "transcript_markdown": build_transcript_markdown(meeting_id, cfg.paths.database_path),
    }


# ── Owner identity (the user running this install) ───────────────────────


@router.get("/owner")
def get_owner_route() -> dict:
    cfg = load_config()
    view = load_owner(cfg)
    return {
        "configured": view.configured,
        "person_id": view.person_id,
        "display_name": view.display_name,
        "aliases": list(view.aliases),
    }


@router.get("/owner/suggest")
def owner_suggest() -> dict:
    cfg = load_config()
    suggestion = suggest_owner(cfg)
    if not suggestion:
        return {"suggestion": None}
    return {"suggestion": suggestion}


@router.post("/owner")
def post_owner_route(
    person_id: int | None = None,
    display_name: str | None = None,
    aliases: str | None = None,
) -> dict:
    cfg = load_config()
    alias_list = [
        alias.strip()
        for alias in (aliases or "").split(",")
        if alias.strip()
    ]
    if person_id is None and not (display_name or "").strip():
        raise HTTPException(status_code=400, detail="owner_identity_required")
    owner = set_owner(cfg, person_id=person_id, display_name=display_name, aliases=alias_list)
    return {
        "configured": True,
        "person_id": owner.person_id,
        "display_name": owner.display_name,
        "aliases": owner.aliases,
    }


@router.delete("/owner")
def delete_owner_route() -> dict:
    cfg = load_config()
    clear_owner(cfg)
    return {"status": "ok"}


# ── People directory ──────────────────────────────────────────────────────


@router.get("/people")
def people_index() -> dict:
    cfg = load_config()
    return {"people": list_people(cfg)}


@router.get("/people/{person_id}")
def people_detail(person_id: int) -> dict:
    cfg = load_config()
    try:
        return get_person(cfg, person_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/people/{person_id}")
def people_delete(person_id: int) -> dict:
    """Remove a person from the directory.

    Nulls out all speaker_assignments + action_items pointing at this
    person (the meetings + transcript text are untouched — only the
    name attribution goes away) and clears the configured owner if
    they pointed at this person. The person row itself is deleted.
    """
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT id, display_name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="person_not_found")
        conn.execute(
            "UPDATE speaker_assignments SET person_id = NULL, confirmed_by_user = 0 "
            "WHERE person_id = ?",
            (person_id,),
        )
        conn.execute(
            "UPDATE transcript_segments SET assigned_person_id = NULL "
            "WHERE assigned_person_id = ?",
            (person_id,),
        )
        conn.execute(
            "UPDATE action_items SET owner_person_id = NULL WHERE owner_person_id = ?",
            (person_id,),
        )
        # speaker_profile_observations.person_id is NOT NULL with an FK to
        # people, so the row has to be deleted (not nulled) before the
        # people row can drop. Otherwise SQLite raises
        # `FOREIGN KEY constraint failed` and the whole DELETE rolls back.
        conn.execute(
            "DELETE FROM speaker_profile_observations WHERE person_id = ?",
            (person_id,),
        )
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    # If they were the configured "you", forget that mapping too.
    owner = load_owner(cfg)
    if owner.person_id == person_id:
        clear_owner(cfg)
    return {"status": "ok", "deleted": row["display_name"]}


@router.post("/people/{person_id}/rename")
def people_rename(
    person_id: int,
    # v0.2.10 audit H1: bound the rename payload. Unbounded `new_name`
    # would let a single request write hundreds of KB into
    # `people.display_name` and cascade across every
    # `speaker_assignments.approved_label` row. 200 chars matches the
    # cap on /meetings/{id}/title and is far above any plausible
    # legitimate name.
    new_name: str = Query(..., min_length=1, max_length=200),
) -> dict:
    """Rename a person and cascade the new label across every meeting.

    v0.2.10: closes the rename gap that forced users to dig into each
    segment's speaker-edit modal to fix a mis-typed name. Two outcomes:

      - ``renamed``: no other person already has the target name; we
        update `people.display_name` + every `speaker_assignments.
        approved_label` for this person in one transaction.
      - ``merged``: the target name belongs to a different person row.
        All FK references (speaker assignments, transcript segments,
        action items, speaker_profile_observations) are repointed and
        the source row is deleted. Useful when a user enters a wrong
        name on one meeting and later wants to consolidate.

    Owner config migrates automatically if the renamed person was the
    configured "you".
    """
    cfg = load_config()
    try:
        return rename_person(cfg, person_id, new_name)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "error"}  # unreachable; placates mypy on the raise path


@router.post("/people/prune-orphans")
def people_prune_orphans() -> dict:
    """Delete every person with zero confirmed speaker assignments AND zero
    action items. Surfaces in the People screen as a "tidy up" button so
    deleting all of someone's meetings doesn't leave a ghost entry behind.
    """
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        orphans = conn.execute(
            """
            SELECT p.id, p.display_name FROM people p
            LEFT JOIN speaker_assignments sa
              ON sa.person_id = p.id AND sa.confirmed_by_user = 1
            LEFT JOIN action_items ai ON ai.owner_person_id = p.id
            WHERE sa.id IS NULL AND ai.id IS NULL
            """
        ).fetchall()
        # Audit quality MED: collapse the per-row loop into two batched
        # DELETEs. Same FK-purge as DELETE /api/people/{id} — observations
        # have a NOT NULL person_id FK that has to drop first.
        if orphans:
            pids = [int(row["id"]) for row in orphans]
            placeholders = ",".join("?" for _ in pids)
            conn.execute(
                f"DELETE FROM speaker_profile_observations WHERE person_id IN ({placeholders})",  # nosec B608
                pids,
            )
            conn.execute(
                f"DELETE FROM people WHERE id IN ({placeholders})",  # nosec B608
                pids,
            )
        owner = load_owner(cfg)
        if owner.person_id is not None and any(int(r["id"]) == owner.person_id for r in orphans):
            clear_owner(cfg)
    return {"status": "ok", "removed": [r["display_name"] for r in orphans]}


# ── Archive (timeline + heatmap) ──────────────────────────────────────────


@router.get("/timeline")
def timeline(weeks: int = Query(16, ge=4, le=52)) -> dict:
    cfg = load_config()
    return build_archive_timeline(cfg, weeks=weeks)


# ── Meeting templates (chooseable extraction prompts) ─────────────────────


@router.get("/templates")
def templates() -> dict:
    return {
        "templates": [
            {"id": key, "name": key.replace("_", " ").title()}
            for key in TEMPLATE_PROMPTS.keys()
        ]
    }


@router.post("/meetings/{meeting_id}/template")
def set_meeting_template(meeting_id: int, template: str) -> dict:
    if template not in TEMPLATE_PROMPTS:
        raise HTTPException(status_code=400, detail="unknown_template")
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            "UPDATE meetings SET template = ? WHERE id = ?", (template, meeting_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="meeting_not_found")
    return {"status": "ok", "template": template}


# ── Reflections (experimental, owner-only) ──────────────────────────────
#
# Hard rule: Reflections must NEVER appear in HTML / PDF / Obsidian
# exports, in the meeting-detail payload, or in any "share / copy /
# email" surface. See docs/design/meeting-output-improvements.md §6.5a
# — this dedicated endpoint is the ONLY way Reflections data leaves
# the SQLite store, and only the in-app Reflections tab calls it.
# Tests in backend/tests/test_reflections.py assert the export boundary
# (no Reflections content in render_meeting_note / build_meeting_overview
# / html_export / pdf_export output).


def _assert_meeting_exists(cfg, meeting_id: int) -> None:
    """Return 404 when meeting_id doesn't resolve to a real row.

    Without this guard, the Reflections endpoints silently return
    `skipped_reason="transcript_too_short"` (because total speech is
    zero) or do nothing on the skip toggle. Matches the existence
    check pattern used elsewhere in this module.
    """
    from app.db.database import connect

    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")


@router.get("/meetings/{meeting_id}/reflections")
def get_reflections(meeting_id: int) -> dict:
    cfg = load_config()
    _assert_meeting_exists(cfg, meeting_id)
    # `compute_reflections` returns None when the experimental flag is
    # off → 404 so the frontend hides the tab entirely instead of
    # rendering an empty surface. When the flag is on but the meeting
    # can't produce Reflections (no owner, opted out, short transcript),
    # returns a Reflections with `skipped_reason` set so the UI can
    # render an honest empty state.
    from app.services.reflections import compute_reflections

    result = compute_reflections(cfg, meeting_id)
    if result is None:
        raise HTTPException(status_code=404, detail="reflections_disabled")
    return result.model_dump()


@router.post("/meetings/{meeting_id}/reflections/skip")
def set_meeting_reflections_skip(meeting_id: int, skip: bool = True) -> dict:
    """Toggle per-meeting opt-out. Sticky across re-extractions so a
    sensitive meeting (1:1, therapy, legal) doesn't regenerate
    Reflections after a transcript edit.
    """
    cfg = load_config()
    _assert_meeting_exists(cfg, meeting_id)
    from app.services.reflections import set_meeting_skip_reflections

    set_meeting_skip_reflections(cfg, meeting_id, skip=skip)
    return {"status": "ok", "skip_reflections": skip}


# ── Segment comments ──────────────────────────────────────────────────────


@router.get("/meetings/{meeting_id}/comments")
def list_comments(meeting_id: int) -> dict:
    cfg = load_config()
    return {"comments": list_segment_comments(cfg, meeting_id)}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/comment")
def add_comment(
    meeting_id: int,
    segment_id: int,
    body: str,
    author: str = "you",
    parent_id: int | None = None,
) -> dict:
    cfg = load_config()
    try:
        return add_segment_comment(cfg, meeting_id, segment_id, body, author, parent_id=parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/meetings/{meeting_id}/comments/{comment_id}/resolve")
def resolve_comment(meeting_id: int, comment_id: int, resolved: bool = True) -> dict:
    cfg = load_config()
    try:
        return resolve_segment_comment(cfg, meeting_id, comment_id, resolved=resolved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/meetings/{meeting_id}/comments/{comment_id}")
def delete_comment(meeting_id: int, comment_id: int) -> dict:
    cfg = load_config()
    try:
        delete_segment_comment(cfg, meeting_id, comment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


# ── Transcript edit history (revert) ──────────────────────────────────────


@router.get("/meetings/{meeting_id}/segments/{segment_id}/edits")
def segment_edits(meeting_id: int, segment_id: int) -> dict:
    cfg = load_config()
    return {"edits": list_segment_edits(cfg, meeting_id, segment_id)}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/revert")
def segment_revert(meeting_id: int, segment_id: int, text: str) -> dict:
    cfg = load_config()
    try:
        return revert_segment_to(cfg, meeting_id, segment_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Audio waveform overlay ────────────────────────────────────────────────


@router.get("/meetings/{meeting_id}/waveform")
def meeting_waveform(meeting_id: int) -> dict:
    cfg = load_config()
    try:
        result = build_waveform(cfg, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "sample_rate_hz": result.sample_rate_hz,
        "samples_per_bucket": result.samples_per_bucket,
        "bucket_ms": result.bucket_ms,
        "peaks": result.peaks,
        "speaker_segments": result.speaker_segments,
    }


# ── Extract progress stream (SSE) ─────────────────────────────────────────


def _latest_processing_job(meeting_id: int) -> dict | None:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT stage, status, progress, error
            FROM processing_jobs
            WHERE meeting_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "stage": row["stage"],
        "status": row["status"],
        "progress": float(row["progress"] or 0),
        "error": row["error"],
    }


@router.get("/meetings/{meeting_id}/extract/stream")
def extract_stream(meeting_id: int) -> StreamingResponse:
    """Tail processing_jobs for this meeting and stream updates via SSE.
    Async so a long-running stream doesn't burn a threadpool slot for the
    entire 10-minute window.
    """

    async def _events():
        seen_status: str | None = None
        # Track the highest progress we've already emitted in this stream
        # so a stale row from a prior re-extract can't make the client see
        # the bar jump backwards.
        max_emitted_progress = 0.0
        deadline = time.time() + 600  # 10 min hard cap
        # Send an initial "waiting" event so the client gets immediate
        # feedback before the job row is inserted by the worker thread.
        yield 'data: {"status":"waiting"}\n\n'
        while time.time() < deadline:
            payload = await asyncio.to_thread(_latest_processing_job, meeting_id)
            if payload is not None:
                token = f"{payload['stage']}:{payload['status']}:{payload['progress']:.2f}"
                progress = payload["progress"]
                terminal = payload["status"] in {"complete", "failed"}
                if token != seen_status and (terminal or progress >= max_emitted_progress):
                    yield f"data: {json.dumps(payload)}\n\n"
                    seen_status = token
                    if progress > max_emitted_progress:
                        max_emitted_progress = progress
                if terminal:
                    return
            await asyncio.sleep(0.5)
        yield 'data: {"status":"timeout"}\n\n'

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.get("/meetings/{meeting_id}/audio")
def get_meeting_audio(meeting_id: int) -> FileResponse:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        source, meeting = _load_source_and_meeting(conn, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    stored_path = source["storage_path"] if source else meeting["imported_path"]
    path = _safe_repo_path(cfg, stored_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio_not_found")
    # Force a browser-friendly MIME instead of letting Starlette /
    # `mimetypes.guess_type` pick. Python's default for `.m4a` is
    # `audio/mp4a-latm` (the technical LATM-AAC container MIME) which
    # browsers refuse to play in an HTMLAudioElement — the `readyState`
    # stays at 0 forever, no error event fires, the dashboard's Play
    # button silently does nothing. Map common audio extensions to the
    # MIMEs HTMLMediaElement.canPlayType reliably accepts.
    return FileResponse(path, media_type=_audio_media_type(path))


_AUDIO_MEDIA_TYPES: dict[str, str] = {
    # `.m4a` is the user-facing extension for AAC-in-MP4 audio; browsers
    # play it as `audio/mp4` (matching the AAC family handler) or the
    # historical `audio/x-m4a`. We pick `audio/mp4` for broader support.
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _audio_media_type(path: Path) -> str:
    """Return the browser-compatible MIME for an audio file by extension.

    Defaults to `application/octet-stream` for unknown extensions so a
    file we didn't anticipate doesn't get advertised as a playable type
    the browser then chokes on — the user sees a clear failure (the
    download falls back to "save as") rather than a silent black hole.
    """
    return _AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


@router.get("/meetings/{meeting_id}/pdf")
def get_meeting_pdf(meeting_id: int) -> FileResponse:
    cfg = load_config()
    try:
        output = write_meeting_pdf(cfg, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        output,
        media_type="application/pdf",
        filename=output.name,
    )


@router.get("/meetings/{meeting_id}/html")
def get_meeting_html(meeting_id: int) -> Response:
    cfg = load_config()
    try:
        html_body = render_meeting_html_string(cfg, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=html_body, media_type="text/html; charset=utf-8")


@router.post("/meetings/{meeting_id}/process")
def process_meeting(meeting_id: int) -> dict:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        source, meeting = _load_source_and_meeting(conn, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    stored_path = source["storage_path"] if source else meeting["imported_path"]
    count = process_meeting_audio(cfg, meeting_id, _safe_repo_path(cfg, stored_path))
    return {"status": "ok", "segments": count}


@router.post("/meetings/{meeting_id}/extract")
def extract(meeting_id: int) -> dict:
    cfg = load_config()
    atoms = extract_meeting_atoms(cfg, meeting_id)
    # v0.2.15: flip meeting.status from 'transcribed' → 'extracted' so the
    # frontend's auto-resubscribe heuristic (which treats 'transcribed' as
    # "still in flight") stops re-opening the SSE stream on every reload
    # and replaying the stale "extract complete 100%" toast.
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "UPDATE meetings SET status = 'extracted' WHERE id = ?",
            (meeting_id,),
        )
    return {"status": "ok", "atoms": atoms.model_dump()}


@router.post("/meetings/{meeting_id}/regenerate-synthesis")
def regenerate_synthesis(meeting_id: int) -> dict:
    cfg = load_config()
    try:
        atoms = regenerate_meeting_synthesis(cfg, meeting_id)
    except ValueError as exc:
        status = 404 if str(exc) == "meeting_not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"status": "ok", "atoms": atoms.model_dump()}


@router.post("/meetings/{meeting_id}/summary")
def update_summary(meeting_id: int, summary: str) -> dict:
    cfg = load_config()
    try:
        update_meeting_summary(cfg, meeting_id, summary)
    except ValueError as exc:
        status = 404 if str(exc) == "meeting_not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/asr-candidates")
def asr_candidates(meeting_id: int, limit: int | None = None) -> dict:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        source, meeting = _load_source_and_meeting(conn, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    stored_path = source["storage_path"] if source else meeting["imported_path"]
    results = run_asr_candidate_passes(
        cfg,
        meeting_id,
        _safe_repo_path(cfg, stored_path),
        limit=limit,
    )
    return {"status": "ok", "candidates": [result.__dict__ for result in results]}


@router.post("/meetings/{meeting_id}/asr-candidates/{candidate_id}/accept")
def accept_asr_candidate(meeting_id: int, candidate_id: int) -> dict:
    cfg = load_config()
    try:
        accept_transcript_candidate(cfg, meeting_id, candidate_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/asr-candidates/{candidate_id}/reject")
def reject_asr_candidate(meeting_id: int, candidate_id: int) -> dict:
    cfg = load_config()
    try:
        reject_transcript_candidate(cfg, meeting_id, candidate_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/stage")
def stage(meeting_id: int) -> dict:
    cfg = load_config()
    output = write_staged_meeting(cfg, meeting_id)
    return {"status": "ok", "path": str(output)}


@router.get("/meetings/{meeting_id}/promotion-diff")
def promotion_diff(meeting_id: int) -> dict:
    cfg = load_config()
    preview = render_promoted_meeting_preview(cfg, meeting_id)
    year = preview.created_at[:4]
    output = cfg.paths.vault_dir / "Meetings" / year / f"{preview.slug}.md"
    existing = output.read_text().splitlines() if output.exists() else []
    proposed = preview.content.splitlines()
    diff = "\n".join(
        difflib.unified_diff(
            existing,
            proposed,
            fromfile=str(output),
            tofile=f"{output} (proposed)",
            lineterm="",
        )
    )
    return {"status": "ok", "path": str(output), "diff": diff}


@router.post("/meetings/{meeting_id}/promote")
def promote(meeting_id: int) -> dict:
    cfg = load_config()
    try:
        output = promote_meeting(cfg, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", "path": str(output)}


@router.post("/meetings/{meeting_id}/speakers/{speaker_id}/approve")
def approve_speaker(meeting_id: int, speaker_id: str, label: str) -> dict:
    cfg = load_config()
    approve_speaker_label(cfg, meeting_id, speaker_id, label)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/correct")
def correct_segment(
    meeting_id: int,
    segment_id: int,
    corrected_text: str,
    reason: str = "",
) -> dict:
    cfg = load_config()
    try:
        correct_segment_text(cfg, meeting_id, segment_id, corrected_text, reason)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/reassign")
def reassign_segment(meeting_id: int, segment_id: int, speaker_id: str) -> dict:
    cfg = load_config()
    try:
        reassign_segment_speaker(cfg, meeting_id, segment_id, speaker_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/speakers/{speaker_id}/reassign-all")
def reassign_speaker(meeting_id: int, speaker_id: str, target_speaker_id: str) -> dict:
    cfg = load_config()
    try:
        count = reassign_speaker_segments(cfg, meeting_id, speaker_id, target_speaker_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok", "segments": count}


@router.post(
    "/meetings/{meeting_id}/review-items/{review_item_id}/accept-reattribution"
)
def accept_reattribution(meeting_id: int, review_item_id: int) -> dict:
    """Apply an accepted speaker-reattribution proposal from v0.2.4.

    The Pass-C reattributer persists proposals as `review_items` rows
    with `kind='speaker_reattribution'`. This route turns an accept
    click into an actual transcript update + marks the review item
    resolved.
    """
    cfg = load_config()
    try:
        from app.services.repair.speaker_reattributer import accept_reattribution_proposal

        result = accept_reattribution_proposal(cfg, meeting_id, review_item_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok", **result}


@router.post(
    "/meetings/{meeting_id}/review-items/{review_item_id}/reject-reattribution"
)
def reject_reattribution(meeting_id: int, review_item_id: int) -> dict:
    """Mark a v0.2.4 reattribution proposal rejected. Transcript
    speaker labels are left alone — diarizer's original assignment
    stands."""
    cfg = load_config()
    try:
        from app.services.repair.speaker_reattributer import reject_reattribution_proposal

        reject_reattribution_proposal(cfg, meeting_id, review_item_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


# v0.2.10 Pass D — accept/reject endpoints for segment-split proposals.
# Mirror the v0.2.4 reattribution accept/reject pair so the UI can reuse
# the same RepairProposalsBanner pattern (one click apply / one click
# dismiss; both idempotent on already-resolved or already-rejected).
@router.post(
    "/meetings/{meeting_id}/review-items/{review_item_id}/accept-split"
)
def accept_split(meeting_id: int, review_item_id: int) -> dict:
    """Apply a v0.2.10 Pass D segment-split proposal.

    Shrinks the head segment's end_ms + text, inserts a new tail
    segment with the proposed speaker_id, and repoints transcript_words
    that fall after the split. All three writes happen atomically.
    """
    cfg = load_config()
    try:
        from app.services.repair.segment_splitter import accept_split_proposal

        result = accept_split_proposal(cfg, meeting_id, review_item_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok", **result}


@router.post(
    "/meetings/{meeting_id}/review-items/{review_item_id}/reject-split"
)
def reject_split(meeting_id: int, review_item_id: int) -> dict:
    """Mark a v0.2.10 segment-split proposal rejected. The original
    segment is left as the diarizer produced it.
    """
    cfg = load_config()
    try:
        from app.services.repair.segment_splitter import reject_split_proposal

        result = reject_split_proposal(cfg, meeting_id, review_item_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok", **result}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/split")
def split_segment(meeting_id: int, segment_id: int, split_ms: int) -> dict:
    cfg = load_config()
    try:
        new_segment_id = split_segment_at_ms(cfg, meeting_id, segment_id, split_ms)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok", "new_segment_id": new_segment_id}


@router.post("/meetings/{meeting_id}/segments/{segment_id}/merge-next")
def merge_next_segment(meeting_id: int, segment_id: int) -> dict:
    cfg = load_config()
    try:
        merge_segment_with_next(cfg, meeting_id, segment_id)
    except ValueError as exc:
        _raise_api_value_error(exc)
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/source/delete-review")
def move_source_to_delete_review(meeting_id: int) -> dict:
    cfg = load_config()
    with connect(cfg.paths.database_path) as conn:
        source = conn.execute(
            "SELECT * FROM source_files WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        if not source:
            raise HTTPException(status_code=404, detail="source_not_found")
        current = _safe_repo_path(cfg, source["storage_path"])
        destination = cfg.paths.delete_review_dir / current.name
        if current.exists():
            shutil.move(str(current), destination)
        conn.execute(
            """
            UPDATE source_files
            SET storage_path = ?, retention_status = ?
            WHERE id = ?
            """,
            (str(destination), "delete_review", source["id"]),
        )
    return {"status": "ok", "path": str(destination)}


@router.get("/scheduler/jobs")
def scheduler_jobs() -> dict:
    cfg = load_config()
    return {"jobs": list_scheduled_jobs(cfg)}


@router.post("/scheduler/daily-maintenance")
def daily_maintenance(enabled: bool = True, run_time: str = "02:00") -> dict:
    cfg = load_config()
    configure_daily_maintenance(cfg, enabled, run_time)
    return {"status": "ok", "jobs": list_scheduled_jobs(cfg)}


@router.post("/scheduler/jobs/{job_id}/enable")
def enable_scheduler_job(job_id: int) -> dict:
    cfg = load_config()
    set_scheduled_job_enabled(cfg, job_id, True)
    return {"status": "ok"}


@router.post("/scheduler/jobs/{job_id}/disable")
def disable_scheduler_job(job_id: int) -> dict:
    cfg = load_config()
    set_scheduled_job_enabled(cfg, job_id, False)
    return {"status": "ok"}


@router.post("/scheduler/jobs/{job_id}/run")
def run_scheduler_job(job_id: int) -> dict:
    cfg = load_config()
    result = run_scheduled_job(cfg, job_id)
    if result.status != "complete":
        raise HTTPException(status_code=500, detail=result.detail)
    return {"status": result.status, "detail": result.detail}


@router.get("/vault/lint")
def vault_lint() -> dict:
    result = lint_vault(load_config())
    return {"ok": result.ok, "checked_files": result.checked_files, "issues": result.issues}


def _unique_inbox_destination(inbox_dir: Path, filename: str) -> Path:
    base = inbox_dir / filename
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    for index in range(1, 10_000):
        candidate = inbox_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="could_not_create_unique_upload_path")


def _load_source_and_meeting(conn, meeting_id: int):
    source = conn.execute(
        """
        SELECT storage_path
        FROM source_files
        WHERE meeting_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (meeting_id,),
    ).fetchone()
    meeting = conn.execute(
        "SELECT imported_path FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    return source, meeting
