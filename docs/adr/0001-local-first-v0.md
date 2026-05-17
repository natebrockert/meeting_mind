# ADR 0001: Local-First v0 Architecture

## Status

Accepted for v0.

## Context

MeetingMind must start private, protect secrets and source audio, and later be publishable without leaking user data. The first user workflow should run on a Mac with LM Studio, Hugging Face pyannote access, and a repo-local Obsidian vault.

## Decision

- Use a Python FastAPI backend with SQLite runtime state.
- Use a React/Vite dashboard for review, speaker approval, scheduling, and promotion controls.
- Keep all source audio, generated vault files, runtime state, screenshots, and local config gitignored.
- Use LM Studio as the default local LLM provider through a model bus that can discover/start the server and load models with TTL.
- Use `mlx-whisper` for local transcription and pyannote for diarization.
- Write Obsidian Markdown only after staging and explicit speaker approval.

## Consequences

The repo can be pushed publicly without local data if `.gitignore` is respected. v0 remains local-first and avoids hosted services except for downloading approved model artifacts.
