# Security and Release Hygiene

MeetingMind is designed for local-first use in a public repository without committing private meeting data.

## Defaults

- No telemetry is enabled by default.
- Secrets are read from ignored local files or process environment only.
- Source audio, transcripts, runtime databases, generated vault files, screenshots, and planning docs are ignored.
- CI runs backend lint/tests, frontend build, and Gitleaks.

## Secret Handling

Do not commit `.env.local`, `.secrets/`, `config/local.toml`, runtime databases, raw audio, generated transcripts, or vault contents.

Before pushing:

```bash
git status --short --ignored
gitleaks git --redact
```

`gitleaks detect --no-git --source .` scans ignored local secret files too, so it is expected to flag `.env.local` on a configured development machine. Use `gitleaks git` for commit/history scanning.

## Promotion Gate

Obsidian promotion is blocked until detected speaker labels are explicitly approved. The default label can remain `Speaker 1`, `Speaker 2`, etc., but it must be confirmed.
