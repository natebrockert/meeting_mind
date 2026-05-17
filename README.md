<div align="center">

<img src="docs/assets/logo.svg" width="84" height="84" alt="MeetingMind logo" />

# MeetingMind

**A local-first meeting memory system.**
Drop in a recording → get a reviewable note in three passes (transcribe → synthesize → review). Out comes a mind map, action items with owners, named topics, an Obsidian-ready Markdown page, and printable PDF/HTML — all without your audio leaving your machine.

[![CI](https://github.com/natebrockert/meeting_mind/actions/workflows/ci.yml/badge.svg)](https://github.com/natebrockert/meeting_mind/actions)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm--NC%201.0.0-c8ff5b?style=flat-square)](LICENSE)
![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-eef2ff?style=flat-square)
![Node 20+](https://img.shields.io/badge/Node-20%2B-eef2ff?style=flat-square)
![Cross-platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-eef2ff?style=flat-square)
![Local-first](https://img.shields.io/badge/Privacy-Local--first-c8ff5b?style=flat-square)

![Review page](docs/screenshots/dashboard-mindmap.png)

</div>

> **Status — v0.2.14.** Multi-platform default via the **lite stack** (FoxNoseTech diarize + WeSpeaker ONNX + faster-whisper) — no HF token, no gated models, runs on Apple Silicon, Linux, Intel macOS, and Windows. Apple Silicon users can opt into the higher-accuracy pyannote + mlx-whisper stack via `uv sync --extra ml`. Still a tester preview — open an issue if you hit something.

> **Hi 👋** Thanks for taking a look at MeetingMind. If you have suggestions, improvements, or just want to share how you're using it, DM me [@Naternet on X](https://x.com/Naternet). Pull requests welcome — fork it, tinker, send it back.

---

## Table of contents

- [About](#about)
- [Install](#install)
- [Daily use](#daily-use)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Security & privacy](#security--privacy)
- [FAQ](#faq)
- [Development](#development)
- [License](#license)

## About

MeetingMind turns a single audio file into a reviewable knowledge artifact in three passes:

| Pass | What happens |
|---|---|
| **Transcribe** | Local ASR (faster-whisper by default) + CPU-friendly diarization (FoxNoseTech `diarize` + WeSpeaker ONNX) → speaker-labeled segments with confidence. No HF token, runs on any platform. Apple Silicon can opt into pyannote + mlx-whisper for higher accuracy. |
| **Synthesize** | Local LLM (LM Studio or Ollama) — or opt-in OpenRouter (BYO API key) — produces TL;DR, action items with owners, decisions, named topics, per-attendee contributions, and uncertainties. Same pass triggers four auto-repair passes (vocab, speaker re-attribution, segment-split, identity resolver) that quietly fix common ASR/diarizer mistakes before you see the result. |
| **Review & promote** | Dashboard built around speaker confidence, low-confidence spans, and reversible edits → export to Obsidian, PDF, or standalone HTML. |

**Why local-first.** Audio + raw transcript always stay on your disk. The backend binds to `127.0.0.1`, no telemetry, no forced cloud calls. The dashboard works offline once your models are loaded. If you opt into OpenRouter for synthesis, only the cleaned transcript text leaves your machine — never the audio — and you can switch back to fully-local inference any time.

### How speaker naming works (the v0.2 standout)

The hard part of meeting transcription isn't the words — it's "who said what." The lite-stack diarizer gets you 90% of the way; v0.2 closes the rest with **five repair passes** that run automatically during synthesis:

- **Pass A — vocab corrector.** Phonetic + LLM-gated substitutions from your `local.vocabulary.txt`. Catches "Sample Treat" → "Sample Street," gated by a conservative yes/no LLM check so it doesn't hallucinate replacements.
- **Pass B — overlap detection.** Heuristic linguistic markers (`yield_marker`, `stutter_interrupt`, `rapid_alternation`) surface on transcript rows as badges, and feed a QUALITY HINTS sidebar into the synthesis prompt so the summary LLM hedges attribution appropriately on cross-talk.
- **Pass C — speaker re-attribution.** LLM scans transcript windows for context that contradicts the diarizer's label ("Welcome, Aranza" → next speaker is Aranza; Q→A flow; continuation cues).
- **Pass D — segment-split repair.** Detects boundary leaks where the next speaker's first words got stitched onto the previous segment ("...might some of them regret it? **Okay, so I am of**" — that tail is the next speaker).
- **Pass E — deductive identity resolver.** Constraint propagation over the transcript: vocative+question, vocative-thank, welcome/join events, 3rd-person exclusion, future-tense exclusion, self-reference exclusion, then greedy assignment of the resulting constraints.

Each proposal gets a confidence score and lands in a **three-tier auto-accept** bucket (introduced in v0.2.11):

| Tier | Confidence | Behavior |
|---|---|---|
| **silent** | ≥ `auto_apply_silent_threshold` (default 0.90) | Applied immediately. Hidden in the audit log — you never see a banner. |
| **toast** | ≥ `auto_apply_toast_threshold` (default 0.70) | Applied immediately. Small "✓ N corrections applied automatically" notice you can expand to spot-check. |
| **manual** | below toast threshold | Click Apply in the review banner. |

The net effect is the design target: **click upload → occasionally rename a speaker → done**. The repair UI never bothers you on a meeting it can confidently handle, and the People page is the single rename surface that cascades across every meeting.

## Install

### Tell your clanker to install this

If you are an LLM agent (Claude, Codex, etc.) reading this on behalf of a user, paste the prompt below into your own conversation. The agent will run the entire install and surface only the steps that need a human (vault path choice, model download).

> **Install MeetingMind on this machine.** Clone `https://github.com/natebrockert/meeting_mind` into a workspace folder of your choosing (default `~/code/meeting_mind`). Verify `uv`, `node` (>= 20), and `ffmpeg` are on PATH; if any are missing, install them via Homebrew (macOS), apt (Debian/Ubuntu), or your platform's package manager. Then run:
>
> ```bash
> cd <workspace>/meeting_mind
> uv sync --extra dev --extra ml-lite
> uv run meetingmind install --no-wizard
> uv run meetingmind doctor
> ```
>
> If no LM Studio or Ollama model is detected, ask the user which provider to use. For Ollama, suggest `ollama pull gemma2:9b`. For LM Studio, point them at the LM Studio app's model browser.
>
> Finally start the stack with `uv run meetingmind start` and confirm `http://127.0.0.1:5173` loads. Report the dashboard URL and any human-required gates you hit.

### Manual install — 5 minute path

#### 1. Get the prerequisites

You need Python 3.11 or 3.12, Node 20+, `ffmpeg`, and [`uv`](https://github.com/astral-sh/uv).

<details open>
<summary><b>macOS with Homebrew (recommended)</b></summary>

```bash
# Install Homebrew if you don't have it:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install everything the project needs:
brew install uv node ffmpeg
```
</details>

<details>
<summary><b>Linux (Debian/Ubuntu)</b></summary>

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv nodejs npm ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh
```
</details>

<details>
<summary><b>macOS without Homebrew</b></summary>

- **uv:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Python 3.11/3.12:** https://www.python.org/downloads/macos/
- **Node 20+ (LTS):** https://nodejs.org/en/download
- **ffmpeg:** https://evermeet.cx/ffmpeg/ — put both `ffmpeg` and `ffprobe` on your `PATH`
</details>

#### 2. Clone + install

```bash
git clone https://github.com/natebrockert/meeting_mind.git
cd meeting_mind
uv sync --extra dev --extra ml-lite
uv run meetingmind install
```

The installer asks for your model-provider choice (LM Studio / Ollama / OpenRouter) and your Obsidian vault path. **No Hugging Face account required** with the default lite stack — diarization (FoxNoseTech) and ASR (faster-whisper) don't need a token. The WeSpeaker ONNX (~25 MB) auto-downloads from a public HF mirror on first ingest, SHA256-verified. The install also drops a global `mm` launcher into `~/.local/bin` so you can run `mm upgrade` / `mm status` from any directory.

##### Pick the right ML extra

| Extra | Stack | When to use |
|---|---|---|
| `ml-lite` (default) | FoxNoseTech diarize + WeSpeaker ONNX + faster-whisper | Any platform. No HF token, no gated models. Recommended starting point. |
| `ml` | pyannote + mlx-whisper | Apple Silicon only. Higher diarization accuracy (~9% AMI DER vs ~15%), but requires HF token + accepting [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) and [pyannote/embedding](https://huggingface.co/pyannote/embedding) licenses. |
| `ml-cpu` | pyannote + faster-whisper | Linux / Intel macOS / Windows users who want pyannote's diarization without mlx-whisper. Same HF gating as `ml`. |

Switch tiers by re-running `uv sync --extra <name>` and setting `diarization.provider` + `asr.engine` in `config/local.toml`.

#### 3. Start the stack

```bash
uv run meetingmind start
```

The dashboard auto-opens at http://127.0.0.1:5173 once both services are ready. If a setup item is still missing (model loaded, identity set), the Inbox shows a checklist with one-click jumps to the right Settings panel.

#### 4. First ingest

Either drop an audio file in `data/inbox/` and click **Ingest**, or use the upload button in the dashboard. Supported: `.m4a`, `.mp3`, `.mp4`, `.wav`, `.aac`, `.flac`, `.opus`.

The pipeline transcribes, diarizes, synthesizes, and runs the repair passes. High-confidence corrections apply silently; lower-confidence ones surface a small notice. **Open the People page to rename any speaker** — the rename cascades to every meeting that references them, and auto-merges if the target name already exists.

### Upgrading

Once you're on a release, this is the one-line update path:

```bash
uv run meetingmind upgrade
```

That pulls the latest code (`git pull --ff-only`), refreshes dependencies (`uv sync`), and bounces any running backend/dashboard so they pick up the new code. **Your local state is preserved** — `config/local.toml`, `.env.local`, `data/`, `runtime/`, and `vault/` are gitignored and never touched.

Options:
- `--dry-run` — show what would happen, change nothing
- `--preview` — show pending commits without pulling
- `--auto-fix` — chain `doctor --fix` afterwards
- `--no-restart` — pull and sync, but leave running services alone

A `pre-upgrade-<stamp>` git tag is laid down before each upgrade so rollback is one `git reset` away.

If anything's off after upgrade, run the auto-remediation:

```bash
uv run meetingmind doctor --fix         # prompts before each fix
uv run meetingmind doctor --fix --yes   # non-interactive, apply all
```

`doctor --fix` is tier-aware: on the lite stack it offers to install `faster-whisper` + `diarize`, pre-fetch the WeSpeaker ONNX, create missing local folders, and install frontend `node_modules`. On the pyannote opt-in path it adds HF-token checks and model-license verification. It never touches data, vault, or credentials.

## Daily use

The v0.2 design target is **upload-and-walk-away**:

| Step | Where | What |
|---|---|---|
| 1 | Inbox | Drop audio into `data/inbox/` or upload from the dashboard. |
| 2 | Inbox | Click **Ingest** — MeetingMind transcribes, diarizes, runs LLM synthesis, applies repair passes (A/C/D/E auto-accept on high-confidence corrections). |
| 3 | Review · **Mind map** | Hero metrics + attributed pull quote + category-coded summary tiles. |
| 4 | Review · **Minutes** | Structured prose: action items, decisions, open questions, detailed minutes with speaker attribution and chapter ownership per bullet. |
| 5 | Review · **Transcript** | Editable surface — audio scrubber with chapter ticks, key-term highlighting, overlap badges on flagged rows, per-segment edit. |
| 6 | People page | **Rename any speaker** — cascades across every meeting, auto-merges into an existing person if the name already exists. |
| 7 | Header | **Stage** to preview the Obsidian note, or **Approve & promote** to write it. **Export** → PDF or HTML; both are exact content mirrors. |
| 8 | Workstreams | Review, rename, filter, or delete cross-meeting threads. |

Supported audio: anything FFmpeg reads — `.m4a`, `.mp3`, `.mp4`, `.wav`, `.aac`, `.flac`, `.opus`.

### CLI

```bash
uv run meetingmind install      # guided local setup (--skip-obsidian / --skip-models / --skip-core flags available)
uv run meetingmind doctor       # tier-aware dependency/config check (--fix to remediate, --export <path> to write a report)
uv run meetingmind start        # start backend + dashboard (--wait blocks until both respond on HTTP)
uv run meetingmind status       # show managed process state and URLs
uv run meetingmind stop         # stop managed processes
uv run meetingmind restart      # restart without changing config/data
uv run meetingmind logs backend --follow   # tail a service log (try --tail N)
uv run meetingmind upgrade      # pull repo + refresh deps + restart (--dry-run, --preview, --auto-fix, --no-restart)
uv run meetingmind backup       # snapshot SQLite + vault into runtime/backups/meetingmind-*.tar.gz
uv run meetingmind reset --inbox --processed --yes   # wipe local state (vault is never touched)

# scripted pipeline ops
uv run meetingmind ingest-once
uv run meetingmind process <meeting-id>
uv run meetingmind extract <meeting-id>
uv run meetingmind stage   <meeting-id>
uv run meetingmind promote <meeting-id>

# diagnostics + evals
uv run meetingmind eval                       # run lite-stack pipeline against tests/eval/fixtures, report WER + speaker match
uv run meetingmind naming-report <meeting>    # print which name candidates the extractor found and why
uv run meetingmind ab-diarize <meeting>       # side-by-side foxnose vs pyannote on one meeting
uv run meetingmind vault-lint
```

Pass `--verbose` (or `-v`) before any command to print every subprocess invocation. Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Configuration

Tracked defaults live in code; local overrides live in ignored files:

- `.env.local` — secrets (HF token if you opt into pyannote, OpenRouter key)
- `config/local.toml` — ports, model selection, paths, repair-pass tuning
- `config/local.vocabulary.txt` — names, acronyms, project terms passed to ASR and the vocab corrector

The dashboard's **Settings** screen exposes the everyday knobs (ports, provider, primary/quality model, base URLs, keep-alive TTL, temperature, voice cue assist, vocabulary, daily maintenance). See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.

### Repair-pass flags (`[repair]` in `config/local.toml`)

| Flag | Default | What it does |
|---|---|---|
| `vocab_correction_enabled` | `true` | Pass A: phonetic + LLM-gated vocab substitutions. |
| `vocab_correction_min_confidence` | `0.6` | Word-level confidence floor for considering a correction. |
| `vocab_correction_max_distance` | `3` | Max Levenshtein distance for phonetic candidates. |
| `speaker_reattribution_enabled` | `true` | Pass C: LLM scan for diarizer mistakes. |
| `speaker_reattribution_window_size` | `12` | Segments per LLM call. |
| `speaker_reattribution_min_confidence` | `0.6` | Floor for surfacing a proposal. |
| `speaker_reattribution_max_segments` | `240` | Hard cap so a 2hr meeting doesn't fan out. |
| `segment_split_enabled` | `true` | Pass D: boundary-leak detection. |
| `segment_split_min_confidence` | `0.55` | Only scans segments below this speaker confidence. |
| `identity_resolver_enabled` | `true` | Pass E: deductive constraint propagation. |
| `auto_apply_enabled` | `true` | Master switch for the three-tier auto-accept. Set `false` to land every proposal in the manual banner (v0.2.10 behavior). |
| `auto_apply_silent_threshold` | `0.90` | Confidence at which corrections apply with no UI notice. |
| `auto_apply_toast_threshold` | `0.70` | Confidence at which corrections apply with a small expandable notice. |

### Local folders the installer creates (all gitignored)

```
data/inbox/                    incoming audio
data/processed/                ingested + ready for the pipeline
data/archive/                  user-archived recordings
runtime/                       database + per-meeting artifacts
runtime/normalized-audio/      diarizer-ready WAV cache
runtime/exports/               generated PDF + HTML
vault/meeting_mind/            default Obsidian vault
```

## Architecture

```
data/inbox/  ──▶  Ingest  ──▶  ASR (faster-whisper)  ──▶  Diarization (FoxNoseTech + WeSpeaker)
                                       │
                                       ▼
                              Transcript segments
                                       │
                                       ▼
                     Repair passes (Pass A vocab, Pass B overlap)
                                       │
                                       ▼
                       Atom extraction  ──  LM Studio / Ollama / OpenRouter
                                       │
                                       ▼
                  Repair passes (Pass C reattribution, D split, E identity)
                                       │
                                       ▼
                  Three-tier auto-accept (silent / toast / manual)
                                       │
                                       ▼
                  Dashboard review (Observatory UI)
                                       │
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
                  Obsidian note     PDF export     HTML export
```

- **Backend:** FastAPI + SQLite, audio pipeline in `backend/app/services/`, repair passes in `backend/app/services/repair/`. No external services beyond the local model bus.
- **Frontend:** Vite + React 19 + TypeScript, single-page app with a CSS-variable design system (see `frontend/src/styles.css` and `frontend/src/observatory.tsx`).
- **Default (lite) stack:** `faster-whisper` for ASR (CTranslate2, runs anywhere), FoxNoseTech `diarize` (Silero VAD + WeSpeaker ONNX + sklearn) for diarization, WeSpeaker ResNet34-LM (~25 MB, SHA256-pinned) for cross-meeting speaker profiles, your local LLM for synthesis and repair gating.
- **Pyannote opt-in:** `mlx-whisper` (Apple Silicon) or `faster-whisper` (CPU) for ASR, `pyannote/speaker-diarization-community-1` + `pyannote/embedding` for diarization and embeddings.

See [CHANGELOG.md](CHANGELOG.md) for the full repair-pass history (v0.2.1 through v0.2.14).

## Security & privacy

- **Local-first.** No telemetry. No cloud calls outside model downloads on first run (and license verification if you opt into the pyannote stack).
- **Loopback by default.** The backend binds to `127.0.0.1`. The dashboard talks to it via the Vite dev proxy. CORS allows only `localhost` / `127.0.0.1` and private mesh ranges (RFC1918 + Tailscale CGNAT `100.64.0.0/10` + `*.ts.net` MagicDNS), with CSRF protection via `Origin` checks on every mutating request.
- **No auth on the API.** Destructive endpoints (`DELETE /api/meetings`, `DELETE /api/inbox`, `DELETE /api/workstreams`, `DELETE /api/people`) have no authentication and assume loopback-only use. **Do not expose this API on a public interface without adding an auth layer in front of it.**
- **Secrets stay out of Git.** `.env.local`, `config/local.toml`, `config/local.vocabulary.txt`, vault files, audio, and runtime artifacts are all gitignored. CI runs Gitleaks on every push.
- **Speaker review gates promotion.** Vault notes never get the wrong name attached unless you confirm it.
- **Reversible-by-default.** Every model output goes through `review_items`. Deleting a workstream re-promotes affected meeting notes so vault links stay coherent.
- **Model integrity.** The WeSpeaker ONNX is SHA256-verified on every download AND every cache load (v0.2.7).

Before pushing anything new, run:

```bash
git status --short --ignored
gitleaks git --redact
```

See [docs/SECURITY.md](docs/SECURITY.md) for the disclosure policy.

## FAQ

**Does my audio ever leave my machine?**
No. Audio + raw transcripts are written to disk on `127.0.0.1` only. If you opt into OpenRouter for synthesis, the cleaned transcript text is sent — never the audio file. Switch the provider back to LM Studio or Ollama in Settings to keep everything fully local.

**What's the difference between `ml-lite`, `ml`, and `ml-cpu`?**
`ml-lite` is the default: FoxNoseTech + WeSpeaker + faster-whisper, works everywhere, no HF token. `ml` is the Apple-Silicon-only high-accuracy path (pyannote + mlx-whisper, needs HF token + license acceptance). `ml-cpu` is for Linux / Intel macOS users who want pyannote's diarization but can't run MLX.

**What does auto-apply do, and can I turn it off?**
Yes. v0.2.11's three-tier auto-accept applies high-confidence repair-pass corrections (vocab, speaker labels, segment boundaries) so you don't have to triage every one. Set `repair.auto_apply_enabled = false` in `config/local.toml` to revert to v0.2.10 behavior where every proposal lands in the manual review banner. Or tune `auto_apply_silent_threshold` / `auto_apply_toast_threshold` to taste.

**A speaker is named wrong everywhere — how do I fix it?**
Open the People page, click ✎ Rename on the person. The rename cascades to every meeting that references them in one transaction. If the new name already belongs to another person, the two rows are auto-merged (all FKs repointed, source row deleted). No manual cleanup required.

**My LLM extracted no action items even though there clearly were some.**
Try a stronger model (Settings → Models) or re-run extraction. Smaller models absorb commitments into narrative prose; the extraction prompt scans aggressively for "I will / we should / let me follow up" phrasing, but a weak model can still miss them.

**Can I run this on a public server?**
No. The API has no auth and includes destructive endpoints. `Origin` checks block cross-origin mutations from your browser, but exposing the backend on `0.0.0.0` is unsupported. Treat it as a single-user local app.

**Can I use the dashboard from my phone?**
Yes — the dashboard is fully responsive (bottom-tab navigation, sheet modals, 44pt touch targets, floating ⌘ palette button). Because the backend is loopback-only by design, put your laptop on a private mesh — [Tailscale](https://tailscale.com), Nebula, WireGuard, or any VPN that gives both devices a stable address — then load the dashboard URL from the phone over that mesh. CORS allows the `*.ts.net` MagicDNS suffix and the `100.64.0.0/10` CGNAT range out of the box.

**How do I uninstall?**
`uv run meetingmind reset --everything --yes` wipes runtime state. Then `rm -rf .venv frontend/node_modules config/local.toml vault/` from the repo root for a clean slate.

## Development

```bash
# backend
uv run ruff check backend
uv run pytest -q     # 294 passing as of v0.2.14

# frontend
cd frontend && npm ci && npm run lint && npm run build
```

`make doctor`, `make lint`, `make test`, `make backend`, `make frontend` wrap the common commands. Eval corpus: drop fixtures under `tests/eval/fixtures/<slug>/` (see `tests/eval/README.md`) and run `uv run meetingmind eval` to compare lite-stack output against your references.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution flow.

The repo ships with `.gitignore`, `.env.example`, `.gitleaks.toml`, GitHub Actions CI, and a pull request template intended for public-release hygiene.

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Full text in [LICENSE](LICENSE).

**Plain English summary** (not the legal text — read the actual license for that):

- ✅ **Use it.** Personal projects, hobbies, research, learning — go for it.
- ✅ **Fork it.** Clone, modify, send PRs, redistribute the modified version (still under PolyForm-NC).
- ✅ **Use it at a school, charity, government, or public-health org.** Explicitly allowed.
- ❌ **Don't sell it or use it to make money.** That includes hosting a paid SaaS version, bundling it into a commercial product, or charging clients to run it for them.
- 📧 **Want a commercial license?** DM [@Naternet on X](https://x.com/Naternet) — happy to talk.

---

<div align="center">
<sub>MeetingMind — local first · yours</sub>
</div>
