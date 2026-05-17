"""Owner identity: who's running this MeetingMind install.

When configured, "your" actions and mentions get visual priority across
the dashboard and exports. Keep the API tiny on purpose — the heavy
lifting is just deciding which strings count as "you" and which
person_id is canonical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import AppConfig, OwnerConfig, save_config
from app.db.database import connect


@dataclass(frozen=True)
class OwnerView:
    person_id: int | None
    display_name: str | None
    aliases: tuple[str, ...]
    configured: bool

    def matches(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        clean = candidate.strip().casefold()
        if not clean:
            return False
        if self.display_name and clean == self.display_name.casefold():
            return True
        return any(alias.casefold() == clean for alias in self.aliases)

    def mentioned_in(self, text: str | None) -> bool:
        """Case-insensitive token match — used to detect mentions in transcripts.
        Matches whole-word so "Sam" doesn't fire on "Samuel" *and* "Jones"
        doesn't fire on "Smith-Jones". `\\b` treats `-` as a word boundary,
        so we explicitly forbid leading/trailing letter or hyphen instead.
        """
        if not text:
            return False
        lowered = text.casefold()
        candidates: list[str] = []
        if self.display_name:
            candidates.append(self.display_name)
        candidates.extend(self.aliases)
        for token in candidates:
            token = token.strip()
            if not token:
                continue
            pattern = (
                r"(?<![A-Za-z0-9\-])"
                + re.escape(token.casefold())
                + r"(?![A-Za-z0-9\-])"
            )
            if re.search(pattern, lowered):
                return True
        return False


def load_owner(config: AppConfig) -> OwnerView:
    owner = config.owner
    aliases = tuple(alias for alias in owner.aliases if isinstance(alias, str) and alias.strip())
    return OwnerView(
        person_id=owner.person_id,
        display_name=owner.display_name,
        aliases=aliases,
        configured=bool(owner.person_id or owner.display_name),
    )


def suggest_owner(config: AppConfig) -> dict | None:
    """Best-guess owner: the person who's been the confirmed speaker on the
    most meetings. Returns None when there isn't enough data yet.
    """
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            """
            SELECT p.id, p.display_name, COUNT(DISTINCT sa.meeting_id) AS meeting_count
            FROM people p
            JOIN speaker_assignments sa
              ON sa.person_id = p.id AND sa.confirmed_by_user = 1
            GROUP BY p.id
            ORDER BY meeting_count DESC, p.display_name
            LIMIT 1
            """,
        ).fetchone()
    if not row or int(row["meeting_count"] or 0) == 0:
        return None
    return {
        "person_id": int(row["id"]),
        "display_name": str(row["display_name"]),
        "meeting_count": int(row["meeting_count"]),
    }


def set_owner(
    config: AppConfig,
    person_id: int | None,
    display_name: str | None,
    aliases: list[str] | None = None,
) -> OwnerConfig:
    """Persist the owner identity into `config/local.toml`. Either a
    person_id (preferred, canonical) or a display_name (raw string) is
    enough; both is best.
    """
    clean_aliases: list[str] = []
    for alias in aliases or []:
        if not isinstance(alias, str):
            continue
        text = alias.strip()
        if text and text.casefold() not in {a.casefold() for a in clean_aliases}:
            clean_aliases.append(text)

    resolved_name = (display_name or "").strip() or None
    resolved_id = person_id if isinstance(person_id, int) and person_id > 0 else None

    if resolved_id and not resolved_name:
        with connect(config.paths.database_path) as conn:
            row = conn.execute(
                "SELECT display_name FROM people WHERE id = ?", (resolved_id,)
            ).fetchone()
            if row:
                resolved_name = str(row["display_name"])

    config.owner = OwnerConfig(
        person_id=resolved_id,
        display_name=resolved_name,
        aliases=clean_aliases,
    )
    save_config(config)
    return config.owner


def clear_owner(config: AppConfig) -> None:
    config.owner = OwnerConfig()
    save_config(config)


def annotate_overview_for_owner(overview: dict, owner: OwnerView) -> dict:
    """Add `is_yours` / `mentions_you` annotations + rollup counts to an
    overview dict in place. Idempotent.
    """
    overview["owner"] = {
        "configured": owner.configured,
        "person_id": owner.person_id,
        "display_name": owner.display_name,
    }
    # An action is "yours" only when YOU are the assignee. We deliberately
    # do NOT split on `owner.mentioned_in(action)` — being merely mentioned
    # (e.g. "ping Alex by Friday" when you aren't Alex) shouldn't promote
    # someone else's task into your task list. Decisions and workstreams
    # still use the broader mention check because those are read-mostly
    # signals.
    #
    # Match by stable owner_person_id first (carried through action_details
    # by obsidian_writer.load_meeting_export_data) — the flat `actions`
    # strings are formatted as "[mm:ss] task" and the prior lead-name
    # regex never matched them, so every owner-assigned action silently
    # bucketed as "other". Fall back to name-prefix scraping for callers
    # that don't supply structured details.
    your_actions: list[str] = []
    other_actions: list[str] = []
    flat_actions = overview.get("actions", []) or []
    details = overview.get("action_details", []) or []
    for index, action_text in enumerate(flat_actions):
        detail = details[index] if index < len(details) else {}
        owner_pid = detail.get("owner_person_id")
        owner_name = detail.get("owner_display_name")
        is_yours = False
        if owner.configured and owner.person_id is not None and owner_pid is not None:
            try:
                is_yours = int(owner_pid) == int(owner.person_id)
            except (TypeError, ValueError):
                is_yours = False
        if not is_yours and owner_name:
            is_yours = owner.matches(owner_name)
        if not is_yours:
            is_yours = owner.matches(_extract_lead_name(action_text))
        if is_yours:
            your_actions.append(action_text)
        else:
            other_actions.append(action_text)
    overview["your_actions"] = your_actions
    overview["other_actions"] = other_actions
    overview["your_action_count"] = len(your_actions)

    your_decisions: list[str] = []
    for decision in overview.get("decisions", []) or []:
        if owner.mentioned_in(decision):
            your_decisions.append(decision)
    overview["your_decisions"] = your_decisions

    your_workstreams = [
        stream
        for stream in overview.get("workstreams", []) or []
        if owner.mentioned_in(stream)
    ]
    overview["your_workstreams"] = your_workstreams

    # participants we recognise as owner-aliases get a flag for the frontend
    overview["you_in_attendance"] = any(
        owner.matches(participant)
        for participant in overview.get("participants", []) or []
    )
    return overview


def _extract_lead_name(action_text: str) -> str | None:
    """Pull the first name-shaped token from an action string. Owner-name
    matching anchors mostly off the LLM's `owner:` prefix when present.
    """
    if not action_text:
        return None
    match = re.match(r"\s*@?([A-Za-z][A-Za-z'\-\.]+)", action_text)
    return match.group(1) if match else None
