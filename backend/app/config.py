"""Typed runtime configuration for a local, gitignored MeetingMind install."""

from __future__ import annotations

import os
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_user_data_root() -> Path:
    """Return the platform-standard per-user data directory for MeetingMind.

    Honors `MEETINGMIND_DATA_HOME` if set (lets developers point a checkout
    at a sandbox/fixture directory). Otherwise picks the OS convention:

    - macOS:   `~/Library/Application Support/MeetingMind`
    - Linux:   `$XDG_DATA_HOME/meetingmind` (default `~/.local/share/meetingmind`)
    - Windows: `%APPDATA%/MeetingMind` (default `~/AppData/Roaming/MeetingMind`)
    - Fallback: `~/.meetingmind` (everything else)

    The repo's `runtime/` is intentionally NOT the default anymore. Real
    user data living under a clone of the public repo means routine `git`
    operations (status, clean, archive) and any tool that walks the
    checkout (linters, indexers, code-assist agents) all see private
    audio + transcripts. Defaulting to a user-data dir keeps the repo
    a code-only artifact.
    """
    override = os.getenv("MEETINGMIND_DATA_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "MeetingMind"
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "").strip()
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "MeetingMind"
    # Linux / BSD / other POSIX: follow XDG Base Directory.
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else home / ".local" / "share"
    return base / "meetingmind"


# Resolved once at module load — `MEETINGMIND_DATA_HOME` is a deployment
# choice, not a per-call switch. Callers can read this directly if they
# need the canonical root.
USER_DATA_ROOT = _default_user_data_root()


class PathConfig(BaseModel):
    """Filesystem locations for a per-user install.

    Defaults route every data path through `USER_DATA_ROOT` so a fresh
    clone of the repo contains only code, never user content. Existing
    installs whose `config/local.toml` pins paths inside the repo
    continue to work — those values override these defaults via
    `load_config`'s deep-merge.
    """

    repo_root: Path = REPO_ROOT
    data_dir: Path = USER_DATA_ROOT / "data"
    inbox_dir: Path = USER_DATA_ROOT / "data" / "inbox"
    processed_dir: Path = USER_DATA_ROOT / "data" / "processed"
    archive_dir: Path = USER_DATA_ROOT / "data" / "archive"
    delete_review_dir: Path = USER_DATA_ROOT / "data" / "delete-review"
    runtime_dir: Path = USER_DATA_ROOT / "runtime"
    database_path: Path = USER_DATA_ROOT / "runtime" / "meetingmind.sqlite3"
    vault_dir: Path = USER_DATA_ROOT / "vault" / "meeting_mind"


class ModelConfig(BaseModel):
    """Local model provider settings for the model bus."""

    # `provider` is intentionally kept as a free-form string (validated at the
    # API layer) so users with custom proxies aren't blocked. Currently the
    # bus understands: lm_studio, ollama, openrouter.
    provider: str = "lm_studio"
    default_model: str = "nvidia/nemotron-3-nano-omni"
    quality_model: str = "qwen3.6-35b-a3b-ud-mlx"
    lm_studio_base_url: str = "http://127.0.0.1:1234/v1"
    ollama_base_url: str = "http://127.0.0.1:11434"
    # OpenRouter (https://openrouter.ai) — a "local-first, no lock-in" cloud
    # option. API key is read from env (OPENROUTER_API_KEY by default) so
    # secrets stay out of the TOML, and `.env.local` is already gitignored.
    # When this provider is selected, transcripts leave the user's machine.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Primary env var name — matches the placeholder in .env.example so a
    # user dropping `OPENROUTER=sk-or-v1-...` into .env.local just works.
    # The bus also accepts OPENROUTER_API_KEY as a fallback for users who
    # already had that name set.
    openrouter_api_key_env: str = "OPENROUTER"
    openrouter_default_model: str = "tencent/hy3-preview"
    idle_ttl_seconds: int = 900
    temperature: float = 0.1


class ChunkConfig(BaseModel):
    target_chars: int = 7500
    hard_max_chars: int = 10000
    overlap_chars: int = 1200
    overlap_seconds: int = 120


class AsrConfig(BaseModel):
    """Whisper/ASR tuning, including conservative repair-pass thresholds."""

    # Default is `faster_whisper` (CTranslate2, runs on any platform, no HF
    # token, no Apple-Silicon requirement). `mlx_whisper` stays as an opt-in
    # for users on Apple Silicon who want slightly different output. Flipped
    # to faster_whisper as the default in v0.2.0 alongside the diarization
    # provider swap to FoxNoseTech.
    engine: Literal["mlx_whisper", "faster_whisper"] = "faster_whisper"
    # Model name interpretation depends on `engine`:
    #   - mlx_whisper: HF repo id like "mlx-community/whisper-large-v3-turbo"
    #   - faster_whisper: a faster-whisper / openai-whisper short name
    #     ("large-v3", "medium", "small", etc.) or HF repo id
    model_name: str = "mlx-community/whisper-large-v3-turbo"
    faster_whisper_model_name: str = "large-v3-turbo"
    faster_whisper_compute_type: str = "int8"  # int8 → CPU-friendly
    condition_on_previous_text: bool = False
    vocabulary_path: Path = REPO_ROOT / "config" / "local.vocabulary.txt"
    vocabulary_terms: list[str] = Field(default_factory=list)
    vocabulary_prompt_max_chars: int = 1200
    compression_ratio_threshold: float = 2.0
    logprob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    hallucination_silence_threshold: float = 2.0
    word_timestamps: bool = True
    candidate_limit: int = 8
    candidate_clip_padding_ms: int = 500
    candidate_conservative_compression_ratio_threshold: float = 1.8
    candidate_conservative_hallucination_silence_threshold: float = 1.0
    candidate_contextual_compression_ratio_threshold: float = 2.4
    candidate_contextual_hallucination_silence_threshold: float = 2.0
    candidate_long_segment_ms: int = 60000
    candidate_long_segment_content_threshold: float = 0.9
    # Experimental multi-pass Whisper repair. Default off — adds 2-3 extra
    # ASR passes per meeting (slower) for marginal accuracy gains on the
    # specific spans flagged as low confidence. Frontier-model synthesis
    # often closes the same gap downstream without the extra cost. Re-
    # enable from Settings → Experimental.
    auto_repair_after_process: bool = False
    candidate_auto_accept_score_threshold: float = 0.92
    candidate_auto_accept_interpass_threshold: float = 0.86


class RepairConfig(BaseModel):
    """LLM-driven post-ASR repair passes (added in v0.2.1).

    Conservative by design: every pass is a gate, not a generator — the
    LLM picks from a constrained set of edits proposed by deterministic
    pre-filters, and every accepted edit is surfaced in the review UI
    as a suggestion the user can reject. No free-form text generation,
    no silent overwrites.

    Each pass has its own enable flag so individual users can keep what
    works for them and turn off what doesn't.
    """

    # Pass A: vocabulary corrector. Scans for low-confidence words that
    # are phonetically close to a configured vocabulary term; asks the
    # LLM to confirm the substitution is contextually plausible.
    vocab_correction_enabled: bool = True
    # Only consider words whose word-level probability is below this.
    # Confident words are left alone — don't risk regressing what works.
    vocab_correction_min_confidence: float = 0.6
    # Levenshtein distance cap for phonetic similarity. 0 = exact match
    # only (useless). 4 = generous. 3 catches most ASR substitutions
    # without producing nonsense candidates.
    vocab_correction_max_distance: int = 3
    # Max number of candidate corrections to ship to the LLM per call.
    # Keeps token cost bounded; meetings with hundreds of low-confidence
    # spans get processed in batches.
    vocab_correction_batch_size: int = 24

    # Pass C: speaker re-attribution. Reads windows of transcript with
    # current diarization labels and asks the LLM to flag segments where
    # the conversational context (introductions, direct address, Q→A
    # patterns) suggests the label is wrong. This is the biggest single
    # lever for closing the AMI DER gap on the lite-stack diarizer.
    speaker_reattribution_enabled: bool = True
    # Window size = number of adjacent segments shipped to the LLM per
    # call. Smaller = more context-fragmentation; larger = more tokens
    # per call. 12 is a reasonable balance for ~3-5 minute conversational
    # chunks at meeting cadence.
    speaker_reattribution_window_size: int = 12
    # Only surface proposals at or above this confidence (from the LLM).
    # Low-confidence proposals are noisy and overwhelm the review UI.
    speaker_reattribution_min_confidence: float = 0.6
    # Hard cap on segments evaluated per meeting (across all windows) so
    # a 2-hour meeting doesn't generate hundreds of LLM calls.
    speaker_reattribution_max_segments: int = 240

    # Pass D (v0.2.10): segment-split repair. Scans low-confidence
    # segments for a discourse-opener pattern at the tail ("Okay, so I
    # am of") that suggests the diarizer's boundary lagged and the next
    # speaker's words got stitched onto this segment. Proposes a split
    # at the word-level timestamp; user accepts/rejects in the UI.
    segment_split_enabled: bool = True
    # Only segments whose speaker_confidence (falling back to
    # confidence) is below this threshold are scanned. Confident
    # diarization output is left alone.
    segment_split_min_confidence: float = 0.55

    # Pass E (v0.2.13): deductive speaker-identity resolver. Collects
    # every name mention in the transcript, classifies each as
    # vocative/3rd-person/future/welcome/past-in-meeting, applies
    # exclusion + scoring rules, then runs greedy assignment to bind
    # speakers to names. Uses the same three-tier auto-apply model as
    # Passes C and D.
    identity_resolver_enabled: bool = True

    # v0.2.11: three-tier auto-accept for repair proposals. The product
    # north star is "click upload → occasionally rename a speaker";
    # forcing the user to triage every repair proposal violated that.
    # High-confidence repairs now apply silently; mid-confidence apply
    # with an inline notice ("Applied N corrections — view"); only
    # genuinely ambiguous proposals (below the mid threshold) still
    # land in the manual review banner.
    #
    # Applies to: Pass D segment-split proposals AND Pass C speaker-
    # reattribution proposals. Set `auto_apply_enabled = False` to
    # revert to v0.2.10 behavior (every proposal manual).
    auto_apply_enabled: bool = True
    # Silent tier: at/above this confidence, repairs apply without a
    # toast. They still log to the auto-applied-repairs audit list so
    # the user can review/undo from the meeting view.
    #
    # v0.2.16: raised 0.90 → 0.95. The lower threshold was producing
    # silent auto-applies of identity assignments built on a single
    # piece of regex evidence (one direct-address hit, confidence 0.74)
    # — see the "Speaker 1 → Two" incident on the live test. Silent
    # tier should be reserved for evidence that's both quantitatively
    # strong AND independently corroborated (see evidence_count below).
    auto_apply_silent_threshold: float = 0.95
    # Toast tier: at/above this confidence, repairs apply but the user
    # gets an inline notice with a count and an "expand to review"
    # affordance. Below this threshold, repairs surface in the manual
    # review banner as before.
    #
    # v0.2.16: raised 0.70 → 0.85. The lower threshold accepted thin
    # single-piece evidence into the auto-apply pipeline; raising it
    # to 0.85 means a single direct-address hit (score 3.0 →
    # confidence 0.74) lands in manual review instead of silently
    # binding to a wrong name.
    auto_apply_toast_threshold: float = 0.85
    # v0.2.16 Stage C: LLM-based speaker identity resolver. Adds one
    # OpenRouter call per meeting (~$0.01) that emits per-speaker
    # identity assignments which the deductive resolver consumes as
    # an additional evidence source. Disabled = the resolver runs on
    # regex + voice-match evidence only.
    llm_identity_enabled: bool = True


class ReviewConfig(BaseModel):
    """Review thresholds used to decide what needs user confirmation."""

    mode: str = "balanced"
    speaker_assignment_required: bool = True
    workstream_confidence_threshold: float = 0.65
    transcript_uncertainty_threshold: float = 0.55
    speaker_confidence_threshold: float = 0.7
    turn_merge_max_gap_ms: int = 2500
    turn_merge_max_duration_ms: int = 120000
    neighbor_consistency_boost: float = 0.08
    vocal_presentation_cue_scoring_enabled: bool = True
    vocal_presentation_cue_max_boost: float = 0.03


class DiarizationConfig(BaseModel):
    # Default is `foxnose` — FoxNoseTech CPU-only stack, no HF token, no
    # gated models, runs on any platform. `pyannote` stays as an opt-in
    # config for users who want the higher accuracy of the HF-gated path
    # (and are willing to set up the HF account + accept the TOS).
    #
    # Quality trade: pyannote ~9% AMI DER, foxnose ~14.96% AMI DER. We
    # close most of that gap with LLM repair passes (vocab corrector,
    # beam reranker, speaker re-attribution — see services/repair/).
    # See PR #33 for the live A/B that motivated the swap.
    provider: Literal["pyannote", "foxnose"] = "foxnose"
    # Embedding provider for the "remember this speaker across meetings"
    # feature. Default is `wespeaker` — uses the same WeSpeaker VoxCeleb
    # ResNet34-LM ONNX model FoxnoseTech uses internally, so the rename-
    # once embedding space is aligned with the diarizer. `pyannote` stays
    # available for users on the pyannote diarization path.
    embedding_provider: Literal["pyannote", "wespeaker"] = "wespeaker"
    model_name: str = "pyannote/speaker-diarization-community-1"
    embedding_model_name: str = "pyannote/embedding"
    device: str = "auto"
    normalized_sample_rate: int = 16000
    known_speaker_count: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    voice_similarity_threshold: float = 0.72


class UploadConfig(BaseModel):
    max_upload_bytes: int = 1_500_000_000


class DashboardConfig(BaseModel):
    """UI-level preferences. Defaults favour signal density over chrome:
    transcript-quality decorations are hidden until the user opts in.
    """

    show_key_term_highlights: bool = False
    show_transcript_confidence_chips: bool = False
    # Template stamped on freshly-ingested meetings. Picked once in Settings
    # and pre-fills the per-batch picker on the Inbox screen; individual
    # ingests can still override at the call site.
    default_template: str = "general"
    # When true, the pipeline auto-promotes a meeting to the Obsidian vault
    # as soon as speaker review is complete. Default off — user clicks
    # "Send to Obsidian" explicitly. The flag is checked at the end of
    # the extract stage.
    auto_send_to_obsidian: bool = False


class RuntimeConfig(BaseModel):
    dashboard_port: int = 5173
    backend_port: int = 8000


class OwnerConfig(BaseModel):
    """Identifies the user running this install. When set, MeetingMind
    weights actions assigned to / mentions of this person higher across
    every surface (sidebar, mind map, minutes, transcript, archive).

    `person_id` is the canonical link to a row in the `people` table. The
    `aliases` list is consulted for transcript-mention matching (case-
    insensitive substring): include nicknames, formal names, initials.
    """

    person_id: int | None = None
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)


class ExperimentalConfig(BaseModel):
    """Opt-in experimental features.

    Each flag gates a surface that's behind a known quality risk (needs
    a frontier model to land safely) or carries ick-risk that warrants
    explicit user opt-in. Defaults all False so a fresh install ships
    the conservative surface.
    """

    # Owner-only "Reflections" — source-anchored observations about how
    # the owner showed up in each meeting. See
    # docs/design/meeting-output-improvements.md §4. Designed for
    # frontier / near-frontier models; small local models trigger a
    # quality warning in the UI.
    reflections_enabled: bool = False


class AppConfig(BaseModel):
    """Top-level app configuration persisted to ignored `config/local.toml`."""

    config_path: Path = REPO_ROOT / "config" / "local.toml"
    paths: PathConfig = Field(default_factory=PathConfig)
    models: ModelConfig = Field(default_factory=ModelConfig)
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    uploads: UploadConfig = Field(default_factory=UploadConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    experimental: ExperimentalConfig = Field(default_factory=ExperimentalConfig)


def _read_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _deep_update(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_update(base[key], value)
        else:
            base[key] = value
    return base


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    """Load config from defaults, ignored local TOML, and environment overrides."""
    _read_env_file(REPO_ROOT / ".env.local")
    config_path = Path(os.getenv("MEETINGMIND_CONFIG", REPO_ROOT / "config" / "local.toml"))
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    cfg = AppConfig(config_path=config_path)
    if config_path.exists():
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg = AppConfig.model_validate(_deep_update(cfg.model_dump(), raw))

    if base_url := os.getenv("MEETINGMIND_LM_STUDIO_BASE_URL"):
        cfg.models.lm_studio_base_url = base_url
    if dashboard_port := os.getenv("MEETINGMIND_DASHBOARD_PORT"):
        cfg.runtime.dashboard_port = int(dashboard_port)
    if backend_port := os.getenv("MEETINGMIND_BACKEND_PORT"):
        cfg.runtime.backend_port = int(backend_port)
    return cfg


def save_config(config: AppConfig) -> None:
    """Persist local configuration and clear the in-process config cache."""
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(
        tomli_w.dumps(config.model_dump(mode="json", exclude_none=True))
    )
    load_config.cache_clear()


class LegacyDataLocationError(RuntimeError):
    """Raised when an upgrading install has its data inside the repo path.

    Pre-user-data-dir versions of MeetingMind defaulted every data path
    to `REPO_ROOT / ...`. After this PR the defaults moved to a per-user
    OS-conventional directory. An upgrading install whose `local.toml`
    doesn't pin paths would silently switch to the new defaults and
    appear empty — the user's audio + DB + vault are still on disk at
    the old repo-relative locations, but the app is no longer pointing
    at them.

    We refuse to start in that state and surface this error with clear
    next steps: run `mm migrate-user-data`, or pin the old paths in
    `config/local.toml`. Both choices preserve user data; the failure
    mode being prevented is "data appears to vanish."
    """


def _detect_legacy_data_in_repo(config: AppConfig) -> Path | None:
    """Return the legacy repo-relative data path when an upgrading
    install is about to silently orphan its data.

    Fires only in the precise scenario this guard exists to catch:
      1. The repo has actual data at `REPO_ROOT/runtime` (legacy v0.2
         install pattern).
      2. The live config is pointing at the new user-data root
         (i.e. the user upgraded and got the new defaults).

    Returns None in every other case:
      - Fresh install (no legacy data in the repo).
      - User deliberately pinned paths at the legacy location (intent).
      - User pinned paths at a custom location like a tmp_path in tests
        or an external drive (they know what they're doing).
    """
    legacy_db = REPO_ROOT / "runtime" / "meetingmind.sqlite3"
    legacy_runtime = REPO_ROOT / "runtime"
    if not legacy_db.exists() and not (legacy_runtime / "normalized-audio").exists():
        return None  # no legacy install in the repo
    # Only fire when the live config matches what `PathConfig()` would
    # produce today — i.e. the user inherited the new defaults. Any
    # other configuration (custom paths, test fixtures, explicit legacy
    # pinning) is treated as user intent.
    try:
        configured_runtime = config.paths.runtime_dir.resolve()
    except OSError:
        return None
    default_runtime = (USER_DATA_ROOT / "runtime").resolve()
    if configured_runtime != default_runtime:
        return None
    return legacy_runtime


def ensure_local_layout(config: AppConfig) -> None:
    """Create all gitignored local folders needed by the app and generated vault.

    Also guards against the upgrade footgun: if the repo contains a
    legacy data dir (runtime/ + DB) but the live config is pointing
    elsewhere, refuse to proceed and tell the user how to migrate.
    Without this check, an upgrading install with a paths-less
    `local.toml` would silently boot against an empty user-data dir
    and look like all their meetings vanished.
    """
    legacy = _detect_legacy_data_in_repo(config)
    if legacy is not None:
        raise LegacyDataLocationError(
            f"Found legacy MeetingMind data inside the repo at {legacy} — "
            f"but the live config points at {config.paths.runtime_dir}. "
            "Refusing to proceed because that would orphan your existing "
            "meetings, transcripts, and vault.\n\n"
            "Pick one:\n"
            "  • `mm migrate-user-data` — move repo-relative data to the "
            "platform-standard user-data dir and rewrite config\n"
            "  • Add explicit paths to `config/local.toml` pointing at "
            "the legacy location to keep using it (gitignore it!)"
        )
    for path in [
        config.paths.inbox_dir,
        config.paths.processed_dir,
        config.paths.archive_dir,
        config.paths.delete_review_dir,
        config.paths.runtime_dir,
        config.paths.vault_dir / "Meetings",
        config.paths.vault_dir / "People",
        config.paths.vault_dir / "Workstreams",
        config.paths.vault_dir / "Actions",
        config.paths.vault_dir / "Staging",
        config.paths.runtime_dir / "normalized-audio",
        config.paths.vault_dir / ".meetingmind" / "manifests",
        config.paths.vault_dir / ".meetingmind" / "exports",
        config.paths.vault_dir / ".meetingmind" / "review-state",
        config.paths.vault_dir / ".meetingmind" / "source-index",
    ]:
        path.mkdir(parents=True, exist_ok=True)

    if not config.config_path.exists():
        config.config_path.parent.mkdir(parents=True, exist_ok=True)
        local_config = config.model_dump(mode="json", exclude_none=True)
        config.config_path.write_text(tomli_w.dumps(local_config))
