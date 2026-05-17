"""Standalone HTML export — mirrors the PDF content with full Observatory styling.

Renders a single self-contained .html file (inline CSS, no external assets)
that opens in any browser. Uses the Observatory PDF mode tokens (white bg,
deep chartreuse). Designed to be printed-to-PDF by the user for the highest
visual fidelity, while also serving as a sharable web page.
"""

from __future__ import annotations

import html
import re

from app.config import AppConfig
from app.services.obsidian_writer import build_meeting_overview


def render_meeting_html_string(config: AppConfig, meeting_id: int) -> str:
    overview = build_meeting_overview(config, meeting_id)
    return _render_html(overview)


def _render_html(overview: dict) -> str:
    duration_min = round(float(overview["duration_seconds"]) / 60)
    voices = len(overview["participants"])

    title_html = _accent_title(overview["title"])
    date_str = _format_datetime(overview["created_at"])

    # Monogram mark — matches the dashboard sidebar wordmark: midnight
    # rounded-square holding the chartreuse italic 'm' from "Meeting*m*ind".
    aperture_svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' width='40' height='40' "
        "aria-hidden='true'>"
        "<rect x='2' y='2' width='60' height='60' rx='16' fill='#0b1020'/>"
        "<rect x='2' y='2' width='60' height='60' rx='16' fill='none' "
        "stroke='#c8ff5b' stroke-opacity='0.18' stroke-width='1.2'/>"
        "<text x='32' y='48' text-anchor='middle' "
        "font-family='DM Serif Display, Georgia, serif' font-style='italic' "
        "font-size='44' fill='#c8ff5b'>m</text>"
        "</svg>"
    )

    parts: list[str] = []
    parts.append(_HTML_HEAD.format(title=html.escape(overview["title"])))
    parts.append("<body>")
    # Top header: monogram + wordmark on the left, light/dark toggle +
    # short export label on the right. No "FILE · 001" / version
    # cruft — those were vestigial design-deck holdovers that didn't
    # carry user value.
    parts.append("<header class='mm-pdf-header'>")
    parts.append(f"<div class='mm-pdf-mark'>{aperture_svg}<div class='mm-pdf-wordmark'>")
    parts.append("<div class='mm-pdf-mm'>Meeting<span class='mm-pdf-ital'>m</span>ind</div>")
    parts.append("<div class='mm-pdf-tag'>local first · yours</div>")
    parts.append("</div></div>")
    parts.append("<div class='mm-pdf-meta-right'>")
    parts.append(_theme_toggle_html())
    parts.append(f"<div class='mm-pdf-meta-line mm-pdf-mono'>{html.escape(date_str)}</div>")
    parts.append("</div></header>")

    # Hero — title + minimal meta. Status/eyebrow row dropped.
    parts.append("<section class='mm-pdf-hero'>")
    parts.append(f"<h1 class='mm-pdf-h1'>{title_html}</h1>")
    # Hero meta — duration + voice count only. The date already sits in
    # the page header at top-right; showing it again here was redundant.
    parts.append("<div class='mm-pdf-meta'>")
    parts.append(f"<span class='mm-pdf-mono'>{duration_min} min</span>")
    parts.append(
        f"<span>·</span><span class='mm-pdf-mono'>{voices} voice{'s' if voices != 1 else ''}</span>"
    )
    parts.append("</div>")
    parts.append("</section>")

    # Themes — short two-word lenses on this meeting, laid out as a pill row.
    # Skipped when the model produced none; same shape as the dashboard.
    themes = overview.get("themes") or []
    if themes:
        parts.append("<div class='mm-pdf-themes'>")
        for theme in themes:
            parts.append(
                f"<span class='mm-pdf-theme'>{html.escape(theme)}</span>"
            )
        parts.append("</div>")

    # TL;DR — wire-thin headline ahead of the long Summary block. Falls
    # back to the first sentence of the summary for legacy extractions.
    tldr = (overview.get("tldr") or "").strip()
    if tldr:
        parts.append("<div class='mm-pdf-tldr'>")
        parts.append("<div class='mm-pdf-lbl-strong'>TL;DR</div>")
        parts.append(f"<p class='mm-pdf-tldr-text'>{html.escape(_normalize(tldr))}</p>")
        parts.append("</div>")

    # Summary — narrative prose, full width. Confidence callout removed
    # (quality metric, not meeting content). At-a-glance grid removed —
    # date / duration / voices now live in the hero meta line above.
    parts.append(_section_rule("Summary"))
    parts.append(
        f"<p class='mm-pdf-summary-text'>{html.escape(_normalize(overview['summary']))}</p>"
    )

    # Stat callouts — big-number highlights from the meeting, laid out as
    # a grid of up to 3 cards. Collapses entirely when the model didn't
    # produce any.
    stat_callouts = overview.get("stat_callouts") or []
    if stat_callouts:
        parts.append(_section_rule("By the numbers", count=len(stat_callouts)))
        parts.append("<div class='mm-pdf-stats'>")
        for stat in stat_callouts:
            parts.append(
                "<div class='mm-pdf-stat'>"
                f"<div class='mm-pdf-stat-value'>{html.escape(stat.get('value', ''))}</div>"
                f"<div class='mm-pdf-stat-label'>{html.escape(stat.get('label', ''))}</div>"
                "</div>"
            )
        parts.append("</div>")

    # Tension points — sentiment-split cards for moments where the meeting
    # had two sides. 0-2 entries typically.
    tension_points = overview.get("tension_points") or []
    if tension_points:
        parts.append(_section_rule("Tension points", count=len(tension_points)))
        parts.append("<div class='mm-pdf-tensions'>")
        for tension in tension_points:
            parts.append("<div class='mm-pdf-tension'>")
            parts.append(
                f"<div class='mm-pdf-tension-title'>{html.escape(tension.get('title', ''))}</div>"
            )
            parts.append("<div class='mm-pdf-tension-sides'>")
            parts.append(
                f"<div class='mm-pdf-tension-pos'><div class='mm-pdf-lbl'>In favour</div>"
                f"<p>{html.escape(_normalize(tension.get('positive_side', '')))}</p></div>"
            )
            parts.append(
                f"<div class='mm-pdf-tension-neg'><div class='mm-pdf-lbl'>Against</div>"
                f"<p>{html.escape(_normalize(tension.get('negative_side', '')))}</p></div>"
            )
            parts.append("</div></div>")
        parts.append("</div>")

    # Key takeaways
    if overview["key_takeaways"]:
        parts.append(_section_rule("Key takeaways", count=len(overview["key_takeaways"])))
        # <ul> + custom mm-pdf-num span so the browser's default ordered-
        # list counter doesn't render alongside our hand-numbered "01"
        # span (which would double-number every row).
        parts.append("<ul class='mm-pdf-takeaways'>")
        for i, item in enumerate(overview["key_takeaways"], start=1):
            parts.append(
                f"<li><span class='mm-pdf-num'>{i:02d}</span>"
                f"<span>{html.escape(_normalize(item))}</span></li>"
            )
        parts.append("</ul>")

    # Voices
    if overview["participants"]:
        parts.append(_section_rule("Voices in attendance", count=voices))
        parts.append("<div class='mm-pdf-voices'>")
        for participant in overview["participants"]:
            initial = (participant.strip()[:1] or "?").upper()
            parts.append(
                f"<div class='mm-pdf-voice'>"
                f"<div class='mm-pdf-voice-circle'>{html.escape(initial)}</div>"
                f"<div><div class='mm-pdf-voice-name'>{html.escape(participant)}</div>"
                f"<div class='mm-pdf-lbl'>speaker</div></div>"
                f"</div>"
            )
        parts.append("</div>")

    # Topics — named threads with concrete one-sentence descriptions.
    # The confidence bars from the prior iteration were a quality-audit
    # surface; in an export they were noise. Names + descriptions only.
    if overview["workstreams"]:
        parts.append(_section_rule("Topics", count=len(overview["workstreams"])))
        descriptions = overview.get("workstream_descriptions") or {}
        parts.append("<ul class='mm-pdf-topics'>")
        for stream in overview["workstreams"]:
            desc = descriptions.get(stream, "")
            parts.append("<li>")
            parts.append(
                f"<div class='mm-pdf-topic-name'>{html.escape(stream)}</div>"
            )
            if desc:
                parts.append(
                    f"<p class='mm-pdf-topic-desc'>{html.escape(_normalize(desc))}</p>"
                )
            parts.append("</li>")
        parts.append("</ul>")

    # Actions + Decisions — surface "Your actions" first when an owner is
    # configured so the print artifact mirrors the dashboard's split.
    owner_meta = overview.get("owner") or {}
    your_actions = overview.get("your_actions") or []
    other_actions = (
        overview.get("other_actions")
        if overview.get("other_actions") is not None
        else overview["actions"]
    )
    if owner_meta.get("configured") and your_actions:
        owner_name = owner_meta.get("display_name") or "you"
        parts.append(
            _section_rule(f"Your actions · {owner_name}", count=len(your_actions))
        )
        parts.append("<ul class='mm-pdf-takeaways'>")
        for item in your_actions:
            parts.append(f"<li><span class='mm-pdf-q'>⌖</span><span>{html.escape(_normalize(item))}</span></li>")
        parts.append("</ul>")
    parts.append("<div class='mm-pdf-two-col'>")
    other_label = "Other actions" if (owner_meta.get("configured") and your_actions) else "Actions"
    for label, items, empty in [
        (other_label, other_actions, "no action items extracted"),
        ("Decisions", overview["decisions"], "no decisions extracted"),
    ]:
        parts.append("<div class='mm-pdf-panel'>")
        parts.append(_section_rule(label, count=len(items) if items else None, inline=True))
        if items:
            parts.append("<ul class='mm-pdf-panel-list'>")
            for item in items[:5]:
                parts.append(f"<li>{html.escape(_normalize(item))}</li>")
            extra = len(items) - 5
            if extra > 0:
                parts.append(
                    f"<li class='mm-pdf-panel-more'>+ {extra} more — open the meeting in MeetingMind to see all</li>"
                )
            parts.append("</ul>")
        else:
            parts.append(f"<div class='mm-pdf-empty'>— {html.escape(empty)}</div>")
        parts.append("</div>")
    parts.append("</div>")

    # Open questions
    if overview["open_questions"]:
        parts.append(_section_rule("Open questions", count=len(overview["open_questions"])))
        parts.append("<ul class='mm-pdf-questions'>")
        for question in overview["open_questions"]:
            parts.append(
                f"<li><span class='mm-pdf-q'>q.</span>"
                f"<span>{html.escape(_normalize(question))}</span></li>"
            )
        parts.append("</ul>")

    # Footer — single line: source recording (filename) + generated-by tag.
    # Sign-off block and "mm✦" decoration removed: this is a meeting note
    # export, not a contract artifact. Vault path is internal plumbing
    # and doesn't belong on a shareable document.
    parts.append(
        f"<footer class='mm-pdf-footer'>"
        # Footer used to print the user's recording filename (could leak
        # client/project names). Show a neutral generated-by attribution
        # plus the export date so recipients have provenance.
        f"<span class='mm-pdf-mono'>Generated with MeetingMind</span>"
        f"<span class='mm-pdf-mono'>{html.escape(date_str)}</span>"
        f"</footer>"
    )

    parts.append("</body></html>")
    return "".join(parts)


def _theme_toggle_html() -> str:
    """Light/dark toggle for the standalone HTML export.

    Stored on `<html data-export-mode="light|dark">`. Initial value
    comes from `prefers-color-scheme` (set by the inline boot script
    inside _HTML_HEAD); clicking toggles + persists to localStorage
    (key `mm-export-mode`). Buttons swap aria-pressed for screen
    readers. The actual color-flipping happens in the CSS via
    `[data-export-mode="dark"]` selectors below the :root tokens.
    """
    return (
        "<div class='mm-pdf-theme-toggle' role='group' aria-label='Theme'>"
        "<button type='button' class='mm-pdf-theme-btn' data-mode='light' "
        "aria-pressed='false' onclick='__mmSetMode(\"light\")'>"
        "<span aria-hidden='true'>☀</span> Light</button>"
        "<button type='button' class='mm-pdf-theme-btn' data-mode='dark' "
        "aria-pressed='false' onclick='__mmSetMode(\"dark\")'>"
        "<span aria-hidden='true'>☾</span> Dark</button>"
        "</div>"
    )


def _section_rule(label: str, *, count: int | str | None = None, inline: bool = False) -> str:
    count_html = (
        f"<span class='mm-pdf-lbl'>{html.escape(str(count))}</span>" if count is not None else ""
    )
    cls = "mm-pdf-rule-inline" if inline else "mm-pdf-section-rule"
    return (
        f"<div class='{cls}'>"
        f"<span class='mm-pdf-lbl-strong'>{html.escape(label)}</span>"
        f"{count_html}"
        f"</div>"
    )


def _accent_title(title: str) -> str:
    # Earlier versions italicised "the first word longer than 3 chars,"
    # which landed on random words for titles like "AWS Cost Review".
    # The italic accent is a flourish that's only meaningful on the
    # wordmark, not on user-supplied titles — render plain.
    return html.escape(title)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _format_datetime(value: str) -> str:
    return value.replace("T", " ").split(".")[0]


def _format_date(value: str) -> str:
    cleaned = _format_datetime(value)
    return cleaned.split(" ")[0] if " " in cleaned else cleaned


def _format_time(value: str) -> str:
    cleaned = _format_datetime(value)
    return cleaned.split(" ", 1)[1] if " " in cleaned else ""


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title} · MeetingMind</title>
<style>
/* Font stacks intentionally local — no Google Fonts CDN. The whole point
 * of MeetingMind is "your audio stays on your machine"; an export that
 * pings fonts.googleapis.com on open would phone home every time it's
 * viewed, and would also fail offline. System fonts render fine. */
:root {{
  --ink: #0b1020; --ink-2: #2e3658; --ink-3: #6a719c; --ink-4: #9aa1c4;
  --clay: #3f6308; --clay-2: #2d4806; --clay-soft: #dbe9b6;
  --rule: #c5cde3; --rule-soft: #dbe1ee; --bone: #ffffff; --bone-2: #f5f7fc; --bone-3: #e6eaf3;
  --sd: ui-serif, Georgia, "Iowan Old Style", "Apple Garamond", "Baskerville", serif;
  --ss: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --sm: ui-monospace, "SF Mono", Menlo, "JetBrains Mono", Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bone); color: var(--ink); font-family: var(--ss); font-size: 14px; line-height: 1.55; }}
body {{ max-width: 780px; margin: 0 auto; padding: 40px 48px 60px; }}
.mm-pdf-header {{ display: flex; justify-content: space-between; align-items: flex-end;
  padding-bottom: 14px; border-bottom: 1px solid var(--ink); margin-bottom: 28px; }}
.mm-pdf-mark {{ display: flex; align-items: center; gap: 12px; }}
.mm-pdf-mm {{ font-family: var(--sd); font-size: 18px; letter-spacing: -0.01em; line-height: 1; }}
.mm-pdf-ital {{ font-style: italic; color: var(--clay); }}
.mm-pdf-tag {{ font-family: var(--sm); font-size: 8px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink-3); margin-top: 4px; }}
.mm-pdf-meta-right {{ text-align: right; }}
.mm-pdf-meta-line {{ font-family: var(--sm); font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--ink-3); margin-top: 3px; }}
.mm-pdf-meta-line.mm-pdf-mono {{ letter-spacing: 0.06em; text-transform: none; color: var(--ink-2); font-size: 11px; }}
.mm-pdf-hero {{ margin: 14px 0 30px; }}
.mm-pdf-eyebrow {{ font-family: var(--sm); font-size: 10px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink); font-weight: 500; }}
.mm-pdf-h1 {{ font-family: var(--sd); font-weight: 400; font-size: 44px; letter-spacing: -0.02em; line-height: 0.98; margin: 12px 0 0; }}
.mm-pdf-meta {{ display: flex; gap: 12px; margin-top: 14px; font-size: 13px; color: var(--ink-3); flex-wrap: wrap; align-items: center; }}
.mm-pdf-status {{ color: var(--clay); font-weight: 600; }}
.mm-pdf-mono {{ font-family: var(--sm); }}
.mm-pdf-lbl {{ font-family: var(--sm); font-size: 9px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink-3); font-weight: 500; }}
.mm-pdf-lbl-strong {{ font-family: var(--sm); font-size: 10px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink); font-weight: 500; }}
.mm-pdf-section-rule {{ display: flex; align-items: baseline; gap: 14px; margin: 30px 0 14px; }}
.mm-pdf-section-rule::after {{ content: ''; flex: 1; height: 1px; background: var(--rule); }}
.mm-pdf-rule-inline {{ display: flex; align-items: baseline; gap: 10px; margin: 0 0 10px; }}
.mm-pdf-rule-inline::after {{ content: ''; flex: 1; height: 1px; background: var(--rule); }}
.mm-pdf-glance {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; border-top: 1px solid var(--rule-soft); border-bottom: 1px solid var(--rule-soft); }}
.mm-pdf-glance-cell {{ padding: 16px 16px 16px 0; border-right: 1px solid var(--rule-soft); }}
.mm-pdf-glance-cell:last-child {{ border-right: none; }}
.mm-pdf-glance-cell + .mm-pdf-glance-cell {{ padding-left: 16px; }}
.mm-pdf-glance-value {{ font-family: var(--sd); font-size: 22px; margin-top: 6px; letter-spacing: -0.02em; line-height: 1.05; }}
.mm-pdf-glance-sub {{ font-size: 11px; color: var(--ink-3); margin-top: 4px; }}
.mm-pdf-summary {{ display: grid; grid-template-columns: 100px 1fr; gap: 22px; align-items: start; }}
.mm-pdf-confidence {{ text-align: left; }}
.mm-pdf-conf-num {{ font-family: var(--sd); font-size: 44px; line-height: 1; color: var(--clay); }}
.mm-pdf-conf-pct {{ font-family: var(--sd); font-size: 18px; color: var(--clay); margin-left: 2px; }}
.mm-pdf-summary-text {{ font-family: var(--sd); font-size: 15px; line-height: 1.55; color: var(--ink-2); margin: 0; }}
.mm-pdf-themes {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 8px; }}
.mm-pdf-theme {{ display: inline-flex; align-items: center; padding: 4px 12px; border: 1px solid var(--clay); border-radius: 999px; font-family: var(--sm); font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--clay); background: var(--clay-soft); }}
.mm-pdf-tldr {{ margin: 18px 0 24px; padding: 18px 22px; border: 1px solid var(--rule); border-radius: 8px; background: var(--bone-2); }}
.mm-pdf-tldr .mm-pdf-lbl-strong {{ color: var(--clay); margin-bottom: 8px; }}
.mm-pdf-tldr-text {{ font-family: var(--sd); font-size: 17px; line-height: 1.45; color: var(--ink); margin: 0; }}
.mm-pdf-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 10px 0 22px; }}
.mm-pdf-stat {{ padding: 14px 16px; background: var(--bone-2); border: 1px solid var(--rule); border-radius: 8px; }}
.mm-pdf-stat-value {{ font-family: var(--sd); font-size: 26px; line-height: 1.05; letter-spacing: -0.02em; color: var(--clay); word-break: break-word; }}
.mm-pdf-stat-label {{ font-size: 11px; line-height: 1.4; color: var(--ink-2); margin-top: 6px; }}
.mm-pdf-tensions {{ display: flex; flex-direction: column; gap: 10px; margin: 10px 0 22px; }}
.mm-pdf-tension {{ padding: 14px 16px; background: var(--bone-2); border: 1px solid var(--rule); border-left: 3px solid var(--clay); border-radius: 8px; }}
.mm-pdf-tension-title {{ font-family: var(--sd); font-size: 14px; color: var(--ink); margin-bottom: 10px; }}
.mm-pdf-tension-sides {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.mm-pdf-tension-pos, .mm-pdf-tension-neg {{ padding: 8px 10px; background: var(--bone); border-radius: 6px; }}
.mm-pdf-tension-pos {{ border-left: 2px solid var(--clay); }}
.mm-pdf-tension-neg {{ border-left: 2px solid var(--ink-3); }}
.mm-pdf-tension-pos p, .mm-pdf-tension-neg p {{ margin: 4px 0 0; font-size: 11.5px; line-height: 1.5; color: var(--ink-2); }}
.mm-pdf-takeaways {{ list-style: none; padding: 0; margin: 0; }}
.mm-pdf-takeaways li {{ display: grid; grid-template-columns: 36px 1fr; gap: 10px; margin-bottom: 12px; font-size: 12.5px; line-height: 1.55; color: var(--ink-2); }}
.mm-pdf-num {{ font-family: var(--sd); font-size: 22px; color: var(--clay); line-height: 1; }}
.mm-pdf-num-small {{ font-family: var(--sm); font-size: 10px; color: var(--ink-3); padding-top: 2px; }}
.mm-pdf-voices {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.mm-pdf-voice {{ display: flex; align-items: center; gap: 12px; padding: 6px 0; }}
.mm-pdf-voice-circle {{ width: 32px; height: 32px; border-radius: 50%; background: var(--bone-2); border: 1px solid var(--ink); display: flex; align-items: center; justify-content: center; font-family: var(--sd); font-style: italic; font-size: 16px; color: var(--clay); }}
.mm-pdf-voice-name {{ font-family: var(--sd); font-size: 16px; letter-spacing: -0.01em; }}
.mm-pdf-pagebreak {{ page-break-before: always; break-before: page; margin-top: 48px; padding-top: 16px; border-top: 1px dashed var(--rule); }}
.mm-pdf-workstreams {{ list-style: none; padding: 0; margin: 0; }}
.mm-pdf-workstreams li {{ display: grid; grid-template-columns: 30px 1fr 110px 50px; gap: 12px; align-items: center; padding: 9px 0; border-bottom: 1px dotted var(--rule); }}
.mm-pdf-ws-name {{ font-size: 12.5px; font-weight: 500; letter-spacing: -0.005em; }}
.mm-pdf-ws-bar {{ display: block; height: 3px; background: var(--bone-3); border-radius: 999px; overflow: hidden; }}
.mm-pdf-ws-fill {{ display: block; height: 100%; }}
.mm-pdf-ws-high {{ background: var(--clay); }}
.mm-pdf-ws-mid {{ background: var(--ink-2); }}
.mm-pdf-ws-low {{ background: var(--ink-4); }}
.mm-pdf-ws-pct {{ font-size: 10px; color: var(--ink-3); text-align: right; }}
.mm-pdf-two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 22px; margin-top: 24px; }}
.mm-pdf-panel {{ background: var(--bone-2); border-radius: 8px; padding: 16px 18px; }}
.mm-pdf-panel-list {{ list-style: none; padding: 0; margin: 0; }}
.mm-pdf-panel-list li {{ font-size: 12px; line-height: 1.55; color: var(--ink-2); margin-bottom: 6px; padding-left: 14px; position: relative; }}
.mm-pdf-panel-list li::before {{ content: '·'; position: absolute; left: 0; color: var(--clay); font-weight: 700; }}
.mm-pdf-empty {{ font-family: var(--sd); font-style: italic; font-size: 12px; color: var(--ink-3); }}
.mm-pdf-questions {{ list-style: none; padding: 0; margin: 0; }}
.mm-pdf-questions li {{ display: grid; grid-template-columns: 24px 1fr; gap: 10px; margin-bottom: 8px; font-size: 13px; line-height: 1.55; color: var(--ink-2); }}
.mm-pdf-q {{ font-family: var(--sd); font-style: italic; color: var(--clay); }}
.mm-pdf-source {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 8px 0 4px; font-size: 11.5px; }}
.mm-pdf-source > div + div {{ padding-left: 14px; border-left: 1px solid var(--rule-soft); }}
.mm-pdf-signoff {{ margin-top: 30px; padding: 16px 18px; background: var(--bone-2); border-left: 3px solid var(--clay); display: flex; justify-content: space-between; align-items: center; }}
.mm-pdf-signoff-by {{ font-size: 11px; color: var(--ink-3); margin-top: 4px; }}
.mm-pdf-signoff-mark {{ font-family: var(--sd); font-style: italic; font-size: 22px; color: var(--clay); }}
.mm-pdf-footer {{ display: flex; justify-content: space-between; padding-top: 18px; margin-top: 30px; border-top: 1px solid var(--ink); font-family: var(--sm); font-size: 10px; color: var(--ink-3); letter-spacing: 0.06em; gap: 16px; flex-wrap: wrap; }}
/* Topics list — named threads with one-sentence descriptions. */
.mm-pdf-topics {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }}
.mm-pdf-topics li {{ padding: 12px 14px; background: var(--bone-2); border: 1px solid var(--rule); border-radius: 8px; }}
.mm-pdf-topic-name {{ font-family: var(--ss); font-size: 13px; font-weight: 500; color: var(--ink); letter-spacing: -0.005em; }}
.mm-pdf-topic-desc {{ margin: 4px 0 0; font-size: 12px; line-height: 1.55; color: var(--ink-2); }}
/* Light/dark toggle — top-right of the header, only visible on screen. */
.mm-pdf-theme-toggle {{ display: inline-flex; align-items: center; gap: 0; background: var(--bone-2); border: 1px solid var(--rule); border-radius: 999px; padding: 2px; margin-bottom: 8px; }}
.mm-pdf-theme-btn {{ font-family: var(--sm); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; padding: 5px 10px; background: transparent; border: none; border-radius: 999px; color: var(--ink-3); cursor: pointer; }}
.mm-pdf-theme-btn[aria-pressed="true"] {{ background: var(--bone); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,0.12); }}
/* Explicit-pick overrides — set on html[data-export-mode]. The
 * prefers-color-scheme media query below handles the no-pick case. */
html[data-export-mode="dark"] {{
  --ink: #eef2ff; --ink-2: #b9c2e8; --ink-3: #8a93bd; --ink-4: #5d6594;
  --clay: #c8ff5b; --clay-2: #9bd13b; --clay-soft: #2a3a14;
  --rule: #2e3658; --rule-soft: #1a2042; --bone: #0b1020; --bone-2: #131a36; --bone-3: #1c2447;
}}
html[data-export-mode="light"] {{
  --ink: #0b1020; --ink-2: #2e3658; --ink-3: #6a719c; --ink-4: #9aa1c4;
  --clay: #3f6308; --clay-2: #2d4806; --clay-soft: #dbe9b6;
  --rule: #c5cde3; --rule-soft: #dbe1ee; --bone: #ffffff; --bone-2: #f5f7fc; --bone-3: #e6eaf3;
}}
@media (prefers-color-scheme: dark) {{
  html:not([data-export-mode]) {{
    --ink: #eef2ff; --ink-2: #b9c2e8; --ink-3: #8a93bd; --ink-4: #5d6594;
    --clay: #c8ff5b; --clay-2: #9bd13b; --clay-soft: #2a3a14;
    --rule: #2e3658; --rule-soft: #1a2042; --bone: #0b1020; --bone-2: #131a36; --bone-3: #1c2447;
  }}
  html:not([data-export-mode]) .mm-pdf-voice-circle {{ background: var(--bone-2); border-color: var(--clay); }}
}}
html[data-export-mode="dark"] .mm-pdf-voice-circle {{ background: var(--bone-2); border-color: var(--clay); }}
@media print {{
  @page {{ size: letter; margin: 0.75in 0.875in; }}
  body {{ max-width: none; padding: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  .mm-pdf-theme-toggle {{ display: none; }}
  html, html[data-export-mode="dark"], html[data-export-mode="light"] {{
    --ink: #0b1020; --ink-2: #2e3658; --ink-3: #6a719c; --ink-4: #9aa1c4;
    --clay: #3f6308; --clay-2: #2d4806; --clay-soft: #dbe9b6;
    --rule: #c5cde3; --rule-soft: #dbe1ee; --bone: #ffffff; --bone-2: #f5f7fc; --bone-3: #e6eaf3;
  }}
}}
</style>
<script>
// Boot script — apply stored light/dark preference before paint. Runs in
// the head so the user never sees a flash of the wrong palette.
(function(){{
  try {{
    var stored = localStorage.getItem('mm-export-mode');
    if (stored === 'light' || stored === 'dark') {{
      document.documentElement.setAttribute('data-export-mode', stored);
    }}
  }} catch (e) {{}}
}})();
function __mmSetMode(mode) {{
  document.documentElement.setAttribute('data-export-mode', mode);
  try {{ localStorage.setItem('mm-export-mode', mode); }} catch (e) {{}}
  document.querySelectorAll('.mm-pdf-theme-btn').forEach(function(btn){{
    btn.setAttribute('aria-pressed', btn.dataset.mode === mode ? 'true' : 'false');
  }});
}}
// Reflect the effective mode (stored choice OR system preference) on the
// toggle buttons after they mount.
document.addEventListener('DOMContentLoaded', function() {{
  var effective = document.documentElement.getAttribute('data-export-mode');
  if (!effective) {{
    effective = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }}
  document.querySelectorAll('.mm-pdf-theme-btn').forEach(function(btn){{
    btn.setAttribute('aria-pressed', btn.dataset.mode === effective ? 'true' : 'false');
  }});
}});
</script>
</head>"""
