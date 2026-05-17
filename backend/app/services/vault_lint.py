from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import AppConfig


@dataclass(frozen=True)
class VaultLintResult:
    """Summary of generated-vault health checks."""

    ok: bool
    checked_files: int
    issues: list[str] = field(default_factory=list)


def lint_vault(config: AppConfig) -> VaultLintResult:
    """Validate Obsidian frontmatter, wiki links, and legacy generated markers."""
    vault_dir = config.paths.vault_dir
    issues: list[str] = []
    checked = 0
    if not vault_dir.exists():
        return VaultLintResult(False, 0, [f"Vault path does not exist: {vault_dir}"])

    markdown_files = sorted(vault_dir.rglob("*.md"))
    known_notes = {
        path.relative_to(vault_dir).with_suffix("").as_posix()
        for path in markdown_files
        if ".meetingmind" not in path.parts
    }
    known_stems = {
        path.stem
        for path in markdown_files
        if ".meetingmind" not in path.parts
    }

    for path in markdown_files:
        if ".meetingmind" in path.parts:
            continue
        checked += 1
        text = path.read_text()
        _lint_frontmatter(path, text, issues)
        _lint_managed_sections(path, text, issues)
        _lint_wiki_links(vault_dir, path, text, known_notes, known_stems, issues)
        if "meetingmind:section:start" in text and "meetingmind:section:end" not in text:
            issues.append(f"{path}: managed section start without end")

    return VaultLintResult(not issues, checked, issues)


def _lint_frontmatter(path: Path, text: str, issues: list[str]) -> None:
    if not text.startswith("---\n"):
        issues.append(f"{path}: missing YAML frontmatter")
        return
    if "\n---\n" not in text[4:]:
        issues.append(f"{path}: unclosed YAML frontmatter")


def _lint_managed_sections(path: Path, text: str, issues: list[str]) -> None:
    """Catch malformed legacy marker pairs; current exports no longer emit markers."""
    starts = [line for line in text.splitlines() if "meetingmind:section:start" in line]
    ends = [line for line in text.splitlines() if "meetingmind:section:end" in line]
    if len(starts) != len(ends):
        issues.append(f"{path}: managed section start/end count mismatch")


def _lint_wiki_links(
    vault_dir: Path,
    path: Path,
    text: str,
    known_notes: set[str],
    known_stems: set[str],
    issues: list[str],
) -> None:
    for raw_target in re.findall(r"\[\[([^]|#]+)", text):
        target = raw_target.strip().removesuffix(".md")
        if not target:
            continue
        if target in known_notes or target.split("/")[-1] in known_stems:
            continue
        rel_path = path.relative_to(vault_dir)
        issues.append(f"{rel_path}: broken wiki link -> {target}")
