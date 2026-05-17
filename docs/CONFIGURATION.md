# MeetingMind Configuration

MeetingMind is configured from tracked defaults, ignored local config, and environment variables.

## Files

- `.env.example` is tracked and contains placeholders only.
- `.env.local` is ignored and may contain local secrets such as Hugging Face tokens.
- `config/local.toml` is ignored and is created by `meetingmind install`.
- Runtime state is ignored under `runtime/`, `data/`, `output/`, and `vault/`.

## Required Local Values

Set one of these in `.env.local`:

```bash
HUGGING_FACE_HUB_TOKEN=<your-hugging-face-token>
```

The installer and doctor also accept `HUGGING_FACE_TOKEN` for compatibility.
The guided installer shows the required pyannote Hugging Face access pages, stores the token only in
ignored `.env.local`, and can run `uv sync --extra ml` plus a pyannote preload so the diarization and
embedding models are cached before the first meeting.

## Model Provider

MeetingMind is **local-first, no subscription, no lock-in**. Audio + raw transcript always stay
on the user's machine. The model provider only controls how structured extraction (summary,
actions, topics, attendee contributions) runs. Three options:

- `lm_studio` — local inference via LM Studio's OpenAI-compatible server (default)
- `ollama` — local inference via Ollama
- `openrouter` — opt-in cloud via [OpenRouter](https://openrouter.ai); BYO API key in
  `.env.local`. When selected, the cleaned transcript text is sent to OpenRouter for synthesis;
  audio never leaves the machine. Switchable back to fully-local any time.

```toml
[models]
provider = "lm_studio"
default_model = "nvidia/nemotron-3-nano-omni"
quality_model = "qwen3.6-35b-a3b-ud-mlx"
lm_studio_base_url = "http://127.0.0.1:1234/v1"
ollama_base_url = "http://127.0.0.1:11434"
openrouter_base_url = "https://openrouter.ai/api/v1"
openrouter_api_key_env = "OPENROUTER_API_KEY"
openrouter_default_model = "anthropic/claude-haiku-4.5"
openrouter_quality_model = "anthropic/claude-opus-4.7"
idle_ttl_seconds = 900
temperature = 0.1
```

The model bus discovers a running LM Studio server port, starts the local server when needed,
loads the configured model with a TTL, and uses the OpenAI-compatible API. For Ollama, the same
shape via `/api/chat`. For OpenRouter, the key is read from the env var named in
`openrouter_api_key_env` (default `OPENROUTER_API_KEY`); store it in `.env.local`, which is
gitignored — the key never lands in `config/local.toml`.

The dashboard Settings page and guided installer can switch between all three providers. During
install, MeetingMind discovers installed local-provider models, lets the user select one, or
offers to download a suggested Nemotron / Qwen / Gemma-class model with `lms get` or
`ollama pull`. Choosing OpenRouter prints a short setup checklist instead.

```toml
[runtime]
dashboard_port = 5173
backend_port = 8000
```

`MEETINGMIND_DASHBOARD_PORT` can override the dashboard port at runtime. After changing the port in
Settings, restart the frontend with the same value, for example:

```bash
MEETINGMIND_DASHBOARD_PORT=5174 uv run meetingmind start --frontend
```

`meetingmind start`, `meetingmind stop`, `meetingmind restart`, and `meetingmind status` manage
local backend/frontend processes through ignored PID and log files under `runtime/`.
`meetingmind restart` only restarts managed processes; it does not change config, runtime data,
source files, or the Obsidian vault. `meetingmind upgrade` pulls the repo and refreshes
Python/frontend dependencies.

## ASR

The default ASR profile is tuned to reduce runaway repetition without deleting raw transcript text.
Flagged spans remain visible and are scored for review.

```toml
[asr]
model_name = "mlx-community/whisper-large-v3-turbo"
condition_on_previous_text = false
vocabulary_path = "config/local.vocabulary.txt"
vocabulary_terms = []
vocabulary_prompt_max_chars = 1200
compression_ratio_threshold = 2.0
logprob_threshold = -1.0
no_speech_threshold = 0.6
hallucination_silence_threshold = 2.0
word_timestamps = true
candidate_limit = 8
candidate_clip_padding_ms = 500
candidate_conservative_compression_ratio_threshold = 1.8
candidate_conservative_hallucination_silence_threshold = 1.0
candidate_contextual_compression_ratio_threshold = 2.4
candidate_contextual_hallucination_silence_threshold = 2.0
candidate_long_segment_ms = 60000
candidate_long_segment_content_threshold = 0.9
auto_repair_after_process = true
candidate_auto_accept_score_threshold = 0.92
candidate_auto_accept_interpass_threshold = 0.86
```

Add names, company terms, product names, acronyms, or recurring workstream language to
`config/local.vocabulary.txt` with one term per line, or add stable shared terms to
`vocabulary_terms` in `config/local.toml`. The local vocabulary file is intentionally ignored by
Git. MeetingMind converts these terms into the Whisper initial prompt for the main transcription
pass and the alternate ASR candidate passes, capped by `vocabulary_prompt_max_chars` so the prompt
does not crowd out the audio task.

When `auto_repair_after_process` is enabled, multi-pass ASR automatically runs three alternate
profiles against suspicious source-audio spans. MeetingMind auto-applies only high-confidence,
high-agreement repairs and keeps the candidate log available as an advanced audit trail.
Set `word_timestamps = true` when quality is preferred over runtime; MeetingMind will persist timed
words and use them for more accurate speaker-turn splitting.

## Review Confidence

MeetingMind tracks separate confidence for transcript text and speaker assignment. Dashboard chips
show `Content` for ASR/text confidence and `Speaker Assignment` for diarization/speaker confidence. The legacy
`confidence` field remains a conservative composite for sorting and backwards compatibility.

```toml
[review]
transcript_uncertainty_threshold = 0.55
speaker_confidence_threshold = 0.7
turn_merge_max_gap_ms = 2500
turn_merge_max_duration_ms = 120000
vocal_presentation_cue_scoring_enabled = true
vocal_presentation_cue_max_boost = 0.03
```

Name suggestions currently use conversational evidence such as self-introductions and direct
address followed by a response from another speaker. The experimental voice cue assist setting is
one optional, capped scoring input for speaker assignment confidence, but it must never define
identity by itself; user-confirmed speaker cards remain the source of truth.

## Diarization

MeetingMind normalizes source audio to a repo-local ignored WAV cache before running pyannote.
This avoids container/sample-count failures from compressed formats such as `.m4a`.

```toml
[diarization]
model_name = "pyannote/speaker-diarization-community-1"
device = "auto"
normalized_sample_rate = 16000
min_speakers = 1
max_speakers = 10
```

Use `known_speaker_count` when the meeting speaker count is known. Otherwise leave it unset and
optionally use `min_speakers`/`max_speakers` as conservative hints.
Set `device = "auto"` to prefer Apple MPS or CUDA when available and fall back to CPU.

## CLI Reference

All commands are invoked as `uv run meetingmind <command> [...flags]`. Pass `--verbose` / `-v` before the
subcommand to print the exact shell command of every subprocess that runs.

### Lifecycle

| Command | What it does |
|---|---|
| `install [--no-wizard] [--dry-run] [--skip-core] [--skip-huggingface] [--skip-models] [--skip-obsidian]` | Create local folders, initialise SQLite, and (optionally) walk the install wizard. The four `--skip-*` flags let you run the wizard partially — useful in CI or headless installs. |
| `doctor [--export PATH]` | Read-only dependency / configuration report. `--export` writes the rendered report to a file for bug reports. |
| `start [--backend\|--frontend] [--wait] [--wait-timeout SECS]` | Launch managed background services. Refuses to start if `install` hasn't run. `--wait` blocks until each service responds on its HTTP endpoint. |
| `stop [--backend\|--frontend]` | Terminate managed services (SIGTERM, escalating to SIGKILL after a grace period). |
| `restart [--backend\|--frontend]` | Stop-then-start without touching local state. |
| `status` | Show PIDs, running status, URLs, and log paths for managed services. |
| `logs <backend\|frontend> [--tail N] [--follow]` | Tail the log file for a managed service. `--follow` streams new lines until Ctrl-C. |
| `dev` | Run uvicorn in reload mode (foreground). Exits with the actual uvicorn exit code on crash. |
| `upgrade [--no-pull] [--no-deps] [--no-restart] [--include-ml] [--force] [--npm-timeout SECS] [--dry-run]` | Pull repo changes, refresh deps, restart running services. Refuses to pull if the working tree is dirty unless `--force`. `--dry-run` shows what would happen without changing anything. |

### Housekeeping

| Command | What it does |
|---|---|
| `backup [--out PATH] [--include-audio]` | Snapshot the SQLite database and Obsidian vault into a single timestamped `.tar.gz`. The processed audio is excluded unless `--include-audio` is set (it's usually big). Default destination: `runtime/backups/meetingmind-YYYYMMDD-HHMMSS.tar.gz`. |
| `reset [--inbox] [--processed] [--runtime] [--everything] [--yes]` | Destructive cleanup helper. The Obsidian vault is **never** touched. Run `backup` first if you want a snapshot. |

### Pipeline (scriptable)

These exist for cron and automation. The dashboard exposes the same operations interactively.

| Command | What it does |
|---|---|
| `ingest-once` | Scan the inbox once and ingest any pending files. Safe to run from cron. |
| `process <meeting-id>` | Run ASR + diarization + quality checks for one meeting. |
| `extract <meeting-id>` | Run LLM extraction (workstreams, decisions, actions, key takeaways) for one meeting. |
| `asr-candidates <meeting-id>` | Generate alternative ASR transcripts for low-confidence spans. |
| `approve-speaker <meeting-id> <speaker-id> <label>` | Confirm a speaker assignment from the CLI. |
| `stage <meeting-id>` | Write the staged Obsidian preview for a meeting without promoting it. |
| `promote <meeting-id>` | Promote a meeting note into the Obsidian vault. |
| `vault-lint` | Validate the vault's generated files. |
| `run-scheduled-job <job-id>` | Execute one scheduled job by id (used by the daily maintenance scheduler). |

## Local Folders

`meetingmind install` creates:

- `data/inbox/`
- `data/processed/`
- `data/archive/`
- `data/delete-review/`
- `runtime/`
- `runtime/normalized-audio/`
- `vault/meeting_mind/`

These paths are intentionally gitignored.

## Platform Support

MeetingMind is developed and tested on macOS (Apple Silicon). Linux is supported on a
best-effort basis — the CLI, backend, and dashboard are pure Python/Node and should work,
but ASR uses MLX (`mlx-community/whisper-large-v3-turbo`) which is Apple-only. On Linux,
swap the ASR `model_name` for a faster-whisper or whisper.cpp build and set
`diarization.device` explicitly. Windows is not supported; use WSL2 if you must run on
Windows hardware.

The guided installer also asks whether to use Obsidian. If enabled, it can install Obsidian with
Homebrew Cask, connect an existing vault, create a new vault at a custom path, use the recommended
repo-local vault at `vault/meeting_mind`, or leave vault setup for later. The created vault includes
MeetingMind's managed folders for meetings, people, workstreams, actions, staging, manifests,
exports, review state, and source indexes.
