#!/usr/bin/env python3
"""Block PII leakage in committed file content.

Scans for terms that identify the repo owner or their real meetings.
Real first/last names are OK as git committer identities (those live
in commit metadata, not file content) but should not appear in source,
tests, docs, or fixtures.

Override on a single line with `pii-ok` in a comment. Used sparingly
— legitimate cases are this script's own blocklist literal and any
docstring that has to explain the PII rule itself.

Usage:
    check_pii.py FILE [FILE ...]    # pre-commit mode: scan given files
    check_pii.py --all              # scan every tracked file
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Blocklist terms. Whole-word, case-insensitive. Add new terms here
# as new domain tells surface. The line itself is allow-listed via the
# `pii-ok` marker at the end so this scanner doesn't trip on its own
# data.
BLOCKLIST = [
    "nate",  # pii-ok
    "nathaniel",  # pii-ok
    "brockert",  # pii-ok
    "wound care",  # pii-ok
    "hyperbaric",  # pii-ok — also catches "hyperbarics" via trailing-s match
    "site selection",  # pii-ok
    "healogics",  # pii-ok
    "standalone wound",  # pii-ok — domain phrase only; bare "standalone" is too common in software contexts
    "hbo",  # pii-ok — hyperbaric oxygen (regex stops on T, so HBOT needs its own entry)
    "hbot",  # pii-ok — hyperbaric oxygen therapy
]

ALLOW_MARKER = "pii-ok"

# Fingerprint patterns. Each entry is (label, compiled_regex,
# optional_predicate). The regex finds candidates; the predicate (if
# present) decides whether the match is a real PII leak or a product
# default that should pass.
#
# These catch tells the literal blocklist can't: Tailscale tailnet
# IDs, MAC addresses, and personal IPv4 addresses that aren't on the
# product-default allow list (loopback, RFC1918, CGNAT, multicast,
# docs ranges, Tailscale anycast, common public DNS).
_TAILNET_ID_RE = re.compile(r"\btail[0-9a-f]{6,}\b", re.IGNORECASE)
_MAC_ADDR_RE = re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _ipv4_is_allowed(addr: str) -> bool:
    """True if `addr` is a product-default IPv4 that should not be
    flagged as a fingerprint."""
    try:
        octets = [int(p) for p in addr.split(".")]
    except ValueError:
        return True  # malformed (can't be a real fingerprint)
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        return True  # malformed (e.g. octet > 255 in a test fixture)
    a, b, c, d = octets
    # 0.0.0.0/8 (any-interface + null route) and 127.0.0.0/8 (loopback)
    if a == 0 or a == 127:
        return True
    # 10.0.0.0/8 (RFC1918)
    if a == 10:
        return True
    # 172.16.0.0/12 (RFC1918)
    if a == 172 and 16 <= b <= 31:
        return True
    # 192.168.0.0/16 (RFC1918) + 192.0.2.0/24 (TEST-NET-1 docs)
    if a == 192 and (b == 168 or (b == 0 and c == 2)):
        return True
    # 100.64.0.0/10 (Tailscale CGNAT) — covers 100.64.0.0 to 100.127.255.255
    if a == 100 and 64 <= b <= 127:
        return True
    # 100.100.100.100 (Tailscale anycast MagicDNS resolver, product-public)
    if a == 100 and b == 100 and c == 100 and d == 100:
        return True
    # 169.254.0.0/16 (link-local)
    if a == 169 and b == 254:
        return True
    # 198.51.100.0/24, 203.0.113.0/24 (TEST-NET-2/3 docs)
    if a == 198 and b == 51 and c == 100:
        return True
    if a == 203 and b == 0 and c == 113:
        return True
    # 224.0.0.0/4 (multicast)
    if 224 <= a <= 239:
        return True
    # 240.0.0.0/4 (reserved, includes 255.255.255.255 broadcast)
    if a >= 240:
        return True
    # Common public DNS (often used in examples/docs)
    if (a, b, c, d) in {(8, 8, 8, 8), (8, 8, 4, 4), (1, 1, 1, 1), (1, 0, 0, 1)}:
        return True
    return False


def _find_fingerprints(line: str) -> list[str]:
    """Return labels for any fingerprint patterns found on this line."""
    hits: list[str] = []
    if _TAILNET_ID_RE.search(line):
        hits.append("tailnet-id")
    if _MAC_ADDR_RE.search(line):
        hits.append("mac-address")
    for m in _IPV4_RE.finditer(line):
        if not _ipv4_is_allowed(m.group(0)):
            hits.append(f"public-ipv4 ({m.group(0)})")
            break  # one IP report per line is enough
    return hits

SCAN_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".md", ".txt", ".rst",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".sql", ".sh", ".html", ".css", ".scss",
}

SKIP_DIR_PARTS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "data", "runtime", "vault",
    "dist", "build", ".next", ".turbo",
    "htmlcov", ".pytest_cache", ".ruff_cache", ".mypy_cache",
}


def _compile_patterns(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for term in terms:
        # Leading boundary + permissive trailing edge: allow trailing
        # `s` (possessive/plural — derived forms) and any  # pii-ok
        # non-letter char (hyphens, punctuation, EOL), but NOT other
        # letters. That distinguishes possessive/compound forms (e.g.
        # "<firstname>-mac-studio" — a real PII leak, caught) from URL
        # slugs like a GitHub username embedded in a clone URL (by
        # design embedded in repo URLs we keep, skipped by the leading
        # boundary against the prior letter).
        # Multi-word terms accept any mix of whitespace, hyphens, or  # pii-ok
        # underscores between words so the hyphen / underscore  # pii-ok
        # variants all match. Caught a real PII miss in PR #8 audit  # pii-ok
        # where the hyphen form slipped through the literal-space match.
        if " " in term:
            parts = term.split(" ")
            body = r"[\s_-]+".join(re.escape(p) for p in parts)
        else:
            body = re.escape(term)
        pat = re.compile(
            rf"(?<!\w){body}(?![A-Za-rt-z])",
            re.IGNORECASE,
        )
        out.append((term, pat))
    return out


def _scan_text(text: str, patterns) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line.lower():
            continue
        line_str = line.strip()[:200]
        matched = False
        for term, pat in patterns:
            if pat.search(line):
                hits.append((lineno, term, line_str))
                matched = True
                break
        if matched:
            continue
        for label in _find_fingerprints(line):
            hits.append((lineno, label, line_str))
            break  # report once per line
    return hits


def _should_scan(path: Path) -> bool:
    if any(part in SKIP_DIR_PARTS for part in path.parts):
        return False
    if path.suffix and path.suffix.lower() not in SCAN_EXTS:
        return False
    return True


def _tracked_files() -> list[Path]:
    out = subprocess.check_output(
        ["git", "ls-files"], text=True, cwd=Path.cwd()
    )
    return [Path(f) for f in out.splitlines()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("files", nargs="*", help="Files to scan (pre-commit mode)")
    p.add_argument("--all", action="store_true", help="Scan all tracked files")
    args = p.parse_args()

    paths = _tracked_files() if args.all else [Path(f) for f in args.files]
    patterns = _compile_patterns(BLOCKLIST)

    any_hit = False
    for path in paths:
        if not path.is_file():
            continue
        if not _should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, term, line in _scan_text(text, patterns):
            any_hit = True
            print(f"{path}:{lineno}: PII term {term!r} — {line}")

    if any_hit:
        print()
        print(
            "PII check failed. Scrub the matches above, or add a "
            "`pii-ok` comment on the same line if the use is "
            "intentional (e.g. the blocklist itself)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
