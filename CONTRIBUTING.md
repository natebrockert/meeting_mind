# Contributing to MeetingMind

Thanks for taking the time to look at this. MeetingMind is a v0 preview — every bug report and polish PR is genuinely useful right now.

## Quick start

```bash
git clone https://github.com/natebrockert/meeting_mind.git
cd meeting_mind
uv sync --extra dev --extra ml
uv run meetingmind install
uv run meetingmind doctor   # confirm everything is green
uv run meetingmind start    # dashboard + backend together
```

Open a PR against `main`. CI runs `ruff check`, `pytest`, `tsc`, `npm run lint`, and `gitleaks` on every push.

## Reporting bugs

[Open an issue](https://github.com/natebrockert/meeting_mind/issues/new) and include:

- macOS version + chip (Apple Silicon vs Intel)
- Python version (`python3 --version`)
- Node version (`node --version`)
- Output of `uv run meetingmind doctor`
- The relevant section of `runtime/logs/backend.log` or `runtime/logs/frontend.log` if anything errored

If the bug touches the dashboard, a screenshot or screen recording is gold.

## Requesting features

[Open an issue](https://github.com/natebrockert/meeting_mind/issues/new) labeled `enhancement`. Describe the workflow you're trying to support, not just the feature you imagine — usually there's a smaller change that solves the underlying problem.

## Code style

- **Python:** `ruff check backend` + `pytest -q` must pass. Follow the patterns in the existing service layer (no big abstractions, no premature configurability).
- **TypeScript/React:** `tsc --noEmit` + `eslint src` must pass. State lives in `App` and flows down via props — there is no Redux/Zustand/Context layer by design. Keep components readable; no clever hooks.
- **Comments:** explain *why* something exists, never *what* the code does. The code says what.
- **Tests:** every backend service module has a `test_<module>.py` next door. Add one if you're touching a service.

## License

MeetingMind is licensed under [PolyForm Noncommercial 1.0.0](LICENSE). By contributing you agree your contributions are licensed under the same terms. The project may also be offered under a separate commercial license in the future — your contributions enable that dual-licensing.

## Questions

DM [@Naternet on X](https://x.com/Naternet) for anything that doesn't fit an issue.
