"""Hand-rolled PDF renderer for MeetingMind meeting notes.

The output mirrors the Observatory PDF surface from the design handoff:
- US Letter portrait, generous margins
- Helvetica family (Bold for display, Oblique for accents) — the three Type1
  fonts ship with every PDF reader, so no font embedding is required
- Deep chartreuse accent (#3f6308) + midnight ink (#0b1020) palette
- Section rules (label + hairline), at-a-glance grid, confidence callouts,
  numbered key takeaways, voices grid, workstreams with confidence bars,
  sign-off block
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConfig
from app.services.obsidian_writer import build_meeting_overview

# US Letter
PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT_MARGIN = 56
RIGHT_MARGIN = 56
TOP_MARGIN = 60
BOTTOM_MARGIN = 60

CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

# Colors (RGB 0-1) — match Observatory PDF mode tokens
INK = (0.043, 0.063, 0.125)        # #0b1020
INK_2 = (0.180, 0.211, 0.345)      # #2e3658
INK_3 = (0.416, 0.443, 0.612)      # #6a719c
CLAY = (0.247, 0.388, 0.031)       # #3f6308
CLAY_DEEP = (0.176, 0.282, 0.024)  # #2d4806
RULE_COLOR = (0.773, 0.804, 0.890) # #c5cde3
SOFT_FILL = (0.961, 0.969, 0.988)  # #f5f7fc


@dataclass
class Cmd:
    """A single drawing command in the PDF content stream."""

    text: str


def write_meeting_pdf(config: AppConfig, meeting_id: int) -> Path:
    overview = build_meeting_overview(config, meeting_id)
    output_dir = config.paths.runtime_dir / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{overview['slug']}.meetingmind.pdf"
    output.write_bytes(_render_pdf(overview))
    return output


# ── Render: drives the page-by-page layout ──────────────────────────────────


def _render_pdf(overview: dict) -> bytes:
    pages: list[list[Cmd]] = []
    builder = _PageBuilder(pages)
    _draw_meeting(builder, overview)
    return _assemble(pages)


def _draw_meeting(b: _PageBuilder, overview: dict) -> None:
    b.new_page()
    _draw_header(b, overview, page_num=1)

    # Title block — "ready for archive" only applies once promoted; for
    # everything earlier (transcribed, extracted, staged) just use a neutral
    # eyebrow so the export doesn't lie about the artifact's lifecycle stage.
    b.gap(8)
    if overview.get("promoted_at"):
        b.lbl_strong("note · ready for archive")
    else:
        b.lbl_strong("meeting note")
    b.gap(10)
    b.headline(overview["title"])
    b.gap(6)
    b.meta_row(overview)
    b.gap(16)
    _hline(b)

    # Themes — short two-word lenses on this meeting. Skipped when
    # the model didn't produce any.
    themes = overview.get("themes") or []
    if themes:
        _theme_pills(b, themes)
        b.gap(10)

    # TL;DR — wire-thin headline above the long Summary block.
    tldr = (overview.get("tldr") or "").strip()
    if tldr:
        _tldr_block(b, tldr)
        b.gap(14)

    # At a glance grid
    _section_rule(b, "At a glance")
    _at_a_glance(b, overview)
    b.gap(18)

    # Summary with confidence callout
    _section_rule(b, "Summary")
    _summary_block(b, overview)
    b.gap(8)

    # Stat callouts — big-number highlights, 0-3 cards.
    stat_callouts = overview.get("stat_callouts") or []
    if stat_callouts:
        _section_rule(b, "By the numbers", count=len(stat_callouts))
        _stat_callout_row(b, stat_callouts)
        b.gap(10)

    # Tension points — sentiment-split cards, 0-2 entries.
    tension_points = overview.get("tension_points") or []
    if tension_points:
        _section_rule(b, "Tension points", count=len(tension_points))
        _tension_blocks(b, tension_points)
        b.gap(10)

    # Key takeaways
    if overview["key_takeaways"]:
        _section_rule(b, "Key takeaways", count=len(overview["key_takeaways"]))
        _numbered_list(b, overview["key_takeaways"])
        b.gap(6)

    # Voices in attendance
    if overview["participants"]:
        _section_rule(b, "Voices in attendance", count=len(overview["participants"]))
        _voices_grid(b, overview["participants"])
        b.gap(6)

    _footer(b, overview, page_num=1)

    # ── Page 2 ─────────────────────────────────────────────────────────────
    b.new_page()
    _draw_header(b, overview, page_num=2)
    b.gap(8)
    b.lbl_strong(_truncate(overview["title"].lower(), 60) + " · continued")
    b.gap(14)

    # Topics — named threads with one-sentence descriptions when available.
    # Confidence bars from the prior iteration are gone (quality audit, not
    # meeting content); the section now reads like a topic outline.
    if overview["workstreams"]:
        _section_rule(b, "Topics", count=len(overview["workstreams"]))
        _topic_rows(b, overview["workstreams"], overview.get("workstream_descriptions") or {})
        b.gap(6)

    # Owner-aware action split: list "Your actions" first when configured,
    # then show "Other actions" alongside Decisions.
    owner_meta = overview.get("owner") or {}
    your_actions = overview.get("your_actions") or []
    other_actions = (
        overview.get("other_actions")
        if overview.get("other_actions") is not None
        else overview["actions"]
    )
    if owner_meta.get("configured") and your_actions:
        _section_rule(
            b,
            f"Your actions · {owner_meta.get('display_name') or 'you'}",
            count=len(your_actions),
        )
        _bulleted_list(b, your_actions)
        b.gap(6)
        other_label = "Other actions"
    else:
        other_label = "Actions"
    _two_column_panels(
        b,
        (other_label, other_actions, "No action items extracted"),
        ("Decisions", overview["decisions"], "No decisions extracted"),
    )
    b.gap(10)

    # Open questions
    if overview["open_questions"]:
        _section_rule(b, "Open questions", count=len(overview["open_questions"]))
        _bulleted_list(b, overview["open_questions"])
        b.gap(6)

    # Sign-off block dropped — this is a meeting note, not a contract.
    # Source filename appears in the footer instead.
    _footer(b, overview, page_num=2)


# ── Drawing helpers (all read from _PageBuilder cursor) ─────────────────────


_STATUS_LABELS = {
    "queued": "Awaiting ingest",
    "processing": "Processing",
    "transcribed": "Ready to review",
    "extracted": "Reviewed",
    "staged": "Staged",
    "promoted": "Promoted",
    "failed": "Needs attention",
}


def _status_label(overview: dict) -> str:
    raw = (overview.get("status") or "").strip().lower()
    return _STATUS_LABELS.get(raw, raw.title() or "—")


def _draw_header(b: _PageBuilder, overview: dict, *, page_num: int) -> None:
    # Aperture mark (left) — drawn as a midnight disc with chartreuse petals
    b.aperture(LEFT_MARGIN + 14, b.y - 14, radius=14)

    # Wordmark text next to the aperture
    b.text("Meetingmind", LEFT_MARGIN + 36, b.y - 18, size=14, font="HB", color=INK)
    b.text(
        "LOCAL FIRST · YOURS",
        LEFT_MARGIN + 36,
        b.y - 30,
        size=7,
        font="H",
        color=INK_3,
        char_spacing=0.8,
    )

    # Right-side meta — date stamp on every page. Page index dropped:
    # the document is short enough that "PAGE 1 / 2" was overhead.
    right = PAGE_WIDTH - RIGHT_MARGIN
    date = _format_datetime(overview["created_at"])
    b.text(date, right, b.y - 18, size=10, font="H", color=INK_2, align="right")
    b.text(
        f"PAGE {page_num}",
        right,
        b.y - 30,
        size=7,
        font="H",
        color=INK_3,
        align="right",
    )

    b.set_y(b.y - 44)
    _hline(b, thickness=1.0)
    b.gap(10)


def _footer(b: _PageBuilder, overview: dict, *, page_num: int) -> None:
    # Footer used to print the internal vault path (`vault/<slug>.md`),
    # which leaks the user's local note layout to anyone who receives the
    # PDF. Show a neutral product attribution instead — the meeting title
    # is already visible elsewhere on the page.
    b.set_y(BOTTOM_MARGIN + 18)
    _hline(b, thickness=0.5)
    b.set_y(BOTTOM_MARGIN + 8)
    b.text("Meetingmind · local first · yours", LEFT_MARGIN, b.y, size=8, font="H", color=INK_3)
    date = _format_date(overview["created_at"])
    b.text(
        date,
        PAGE_WIDTH / 2,
        b.y,
        size=8,
        font="H",
        color=INK_3,
        align="center",
    )
    b.text(
        f"{page_num:02d} / 02",
        PAGE_WIDTH - RIGHT_MARGIN,
        b.y,
        size=8,
        font="H",
        color=INK_3,
        align="right",
    )


def _section_rule(b: _PageBuilder, label: str, *, count: int | None = None) -> None:
    b.gap(18)
    label_width = b.text_width(label.upper(), size=9, font="HB")
    b.text(label.upper(), LEFT_MARGIN, b.y, size=9, font="HB", color=INK, char_spacing=1.6)
    after_label = LEFT_MARGIN + label_width + 18  # tracking accounts for char spacing
    if count is not None:
        count_str = str(count).zfill(2)
        b.text(count_str, after_label, b.y, size=8, font="H", color=INK_3, char_spacing=1.2)
        after_label += b.text_width(count_str, size=8, font="H") + 16
    b.hline_from(after_label, PAGE_WIDTH - RIGHT_MARGIN, y=b.y + 3, color=RULE_COLOR)
    b.set_y(b.y - 12)


def _hline(b: _PageBuilder, *, thickness: float = 0.6) -> None:
    b.hline_from(LEFT_MARGIN, PAGE_WIDTH - RIGHT_MARGIN, y=b.y, color=RULE_COLOR, thickness=thickness)
    b.set_y(b.y - 4)


def _at_a_glance(b: _PageBuilder, overview: dict) -> None:
    cells = [
        ("Conducted", _format_date(overview["created_at"]), _format_time(overview["created_at"])),
        (
            "Duration",
            f"{round(float(overview['duration_seconds']) / 60)} min",
            "audio kept locally",
        ),
        (
            "Voices",
            str(len(overview["participants"])),
            "all named" if overview["speaker_status"] == "complete" else "review pending",
        ),
        (
            "Status",
            _status_label(overview),
            "speaker review " + (overview["speaker_status"] or "pending"),
        ),
    ]
    col_width = CONTENT_WIDTH / 4
    start_y = b.y
    for i, (label, value, sub) in enumerate(cells):
        x = LEFT_MARGIN + i * col_width
        b.text(label.upper(), x, start_y, size=8, font="H", color=INK_3, char_spacing=1.4)
        b.text(_truncate(value, 20), x, start_y - 18, size=16, font="HB", color=INK)
        b.text(_truncate(sub, 32), x, start_y - 32, size=9, font="H", color=INK_3)
        if i < 3:
            b.vline(x + col_width - 12, start_y - 38, start_y + 6, color=RULE_COLOR)
    b.set_y(start_y - 44)


def _summary_block(b: _PageBuilder, overview: dict) -> None:
    # Confidence callout left, prose right
    confidence = _estimate_confidence(overview)
    callout_x = LEFT_MARGIN
    callout_w = 64
    b.text(
        f"{confidence}",
        callout_x,
        b.y - 24,
        size=36,
        font="HB",
        color=CLAY,
    )
    pct_x = callout_x + b.text_width(f"{confidence}", size=36, font="HB") + 1
    b.text("%", pct_x, b.y - 14, size=14, font="HB", color=CLAY)
    b.text("EST. QUALITY", callout_x, b.y - 38, size=7, font="H", color=INK_3, char_spacing=1.4)

    text_x = LEFT_MARGIN + callout_w + 14
    text_w = CONTENT_WIDTH - callout_w - 14
    quote = _truncate(overview["summary"], 720)
    b.paragraph(
        f"“{quote}”",
        x=text_x,
        width=text_w,
        size=10.5,
        font="HO",
        color=INK_2,
        leading=14,
    )
    # b.paragraph above already advanced b.y for the prose column. The
    # callout was drawn at fixed offsets and didn't move b.y, so the
    # paragraph's position is the lower of the two — just add a small gap.
    b.set_y(b.y - 6)


def _theme_pills(b: _PageBuilder, themes: list[str]) -> None:
    """Render theme labels as a pill row beneath the title block.

    Pills wrap to the next line when they exceed the content width.
    Hand-baked outline + fill so the print output matches the dashboard's
    chartreuse-on-soft theme treatment.
    """
    x = LEFT_MARGIN
    y = b.y - 12
    pad_x = 8
    pad_y = 4
    gap = 6
    size = 8
    for theme in themes[:6]:
        label = theme.upper()[:40]
        text_w = b.text_width(label, size=size, font="HB", char_spacing=1.4)
        pill_w = text_w + pad_x * 2
        pill_h = size + pad_y * 2
        if x + pill_w > LEFT_MARGIN + CONTENT_WIDTH:
            x = LEFT_MARGIN
            y -= pill_h + gap
        # Pill body: 1px chartreuse outline, light fill
        b.current.append(
            Cmd(
                f"q {CLAY[0]:.3f} {CLAY[1]:.3f} {CLAY[2]:.3f} RG "
                f"{0.97:.3f} {0.99:.3f} {0.93:.3f} rg "
                f"0.8 w {x:.2f} {y - pill_h + 1:.2f} {pill_w:.2f} {pill_h:.2f} re B Q"
            )
        )
        b.text(
            label,
            x + pad_x,
            y - pill_h + pad_y + 1,
            size=size,
            font="HB",
            color=CLAY_DEEP,
            char_spacing=1.4,
        )
        x += pill_w + gap
    b.set_y(y - 4)


def _tldr_block(b: _PageBuilder, tldr: str) -> None:
    """A wire-thin one-sentence headline ahead of the long Summary block."""
    box_x = LEFT_MARGIN
    box_w = CONTENT_WIDTH
    pad = 14
    # Estimate height: line count of wrapped text at 13pt + lbl + pad.
    estimated_lines = max(2, (len(tldr) // 110) + 2)
    box_h = pad + 16 + estimated_lines * 17 + pad
    # Soft fill box
    b.current.append(
        Cmd(
            f"q {SOFT_FILL[0]:.3f} {SOFT_FILL[1]:.3f} {SOFT_FILL[2]:.3f} rg "
            f"{box_x:.2f} {b.y - box_h:.2f} {box_w:.2f} {box_h:.2f} re f Q"
        )
    )
    # Border
    b.current.append(
        Cmd(
            f"q {RULE_COLOR[0]:.3f} {RULE_COLOR[1]:.3f} {RULE_COLOR[2]:.3f} RG "
            f"0.6 w {box_x:.2f} {b.y - box_h:.2f} {box_w:.2f} {box_h:.2f} re S Q"
        )
    )
    label_y = b.y - pad - 6
    b.text(
        "TL;DR",
        box_x + pad,
        label_y,
        size=8,
        font="HB",
        color=CLAY,
        char_spacing=1.4,
    )
    b.set_y(label_y - 12)
    b.paragraph(
        tldr,
        x=box_x + pad,
        width=box_w - pad * 2,
        size=12.5,
        font="HB",
        color=INK,
        leading=16,
    )
    b.set_y(b.y - pad)


def _stat_callout_row(b: _PageBuilder, stats: list[dict]) -> None:
    """Up to 3 stat cards laid out horizontally; truncates the rest."""
    cards = stats[:3]
    if not cards:
        return
    gap = 10
    card_w = (CONTENT_WIDTH - gap * (len(cards) - 1)) / len(cards)
    card_h = 70
    y = b.y - card_h
    for idx, stat in enumerate(cards):
        x = LEFT_MARGIN + idx * (card_w + gap)
        # Soft fill + rule
        b.current.append(
            Cmd(
                f"q {SOFT_FILL[0]:.3f} {SOFT_FILL[1]:.3f} {SOFT_FILL[2]:.3f} rg "
                f"{x:.2f} {y:.2f} {card_w:.2f} {card_h:.2f} re f Q"
            )
        )
        b.current.append(
            Cmd(
                f"q {RULE_COLOR[0]:.3f} {RULE_COLOR[1]:.3f} {RULE_COLOR[2]:.3f} RG "
                f"0.6 w {x:.2f} {y:.2f} {card_w:.2f} {card_h:.2f} re S Q"
            )
        )
        value = _truncate(str(stat.get("value", "")), 24)
        label = _truncate(str(stat.get("label", "")), 60)
        b.text(value, x + 14, y + card_h - 24, size=22, font="HB", color=CLAY)
        # paragraph() reads from b.y, so seek before drawing the label.
        b.set_y(y + card_h - 38)
        b.paragraph(
            label,
            x=x + 14,
            width=card_w - 28,
            size=9,
            font="H",
            color=INK_2,
            leading=12,
        )
    b.set_y(y - 8)


def _tension_blocks(b: _PageBuilder, tensions: list[dict]) -> None:
    """One block per tension point: title + positive/negative side-by-side."""
    for tension in tensions[:2]:
        title = _truncate(str(tension.get("title", "")), 90)
        positive = _truncate(str(tension.get("positive_side", "")), 220)
        negative = _truncate(str(tension.get("negative_side", "")), 220)
        block_x = LEFT_MARGIN
        block_w = CONTENT_WIDTH
        # Estimate height: title + 2 stacked-but-split bodies.
        block_h = 18 + 64
        y = b.y - block_h
        # Body wash + clay left rule
        b.current.append(
            Cmd(
                f"q {SOFT_FILL[0]:.3f} {SOFT_FILL[1]:.3f} {SOFT_FILL[2]:.3f} rg "
                f"{block_x:.2f} {y:.2f} {block_w:.2f} {block_h:.2f} re f Q"
            )
        )
        b.current.append(
            Cmd(
                f"q {CLAY[0]:.3f} {CLAY[1]:.3f} {CLAY[2]:.3f} rg "
                f"{block_x:.2f} {y:.2f} 2.5 {block_h:.2f} re f Q"
            )
        )
        b.text(title, block_x + 14, y + block_h - 14, size=11, font="HB", color=INK)
        half = (block_w - 22) / 2
        b.text(
            "IN FAVOUR",
            block_x + 14,
            y + block_h - 32,
            size=7,
            font="HB",
            color=INK_3,
            char_spacing=1.4,
        )
        b.set_y(y + block_h - 40)
        b.paragraph(
            positive,
            x=block_x + 14,
            width=half - 14,
            size=9,
            font="H",
            color=INK_2,
            leading=12,
        )
        b.text(
            "AGAINST",
            block_x + half + 14,
            y + block_h - 32,
            size=7,
            font="HB",
            color=INK_3,
            char_spacing=1.4,
        )
        b.set_y(y + block_h - 40)
        b.paragraph(
            negative,
            x=block_x + half + 14,
            width=half - 14,
            size=9,
            font="H",
            color=INK_2,
            leading=12,
        )
        b.set_y(y - 10)


def _numbered_list(b: _PageBuilder, items: list[str]) -> None:
    for i, item in enumerate(items, start=1):
        b.text(
            f"{i:02d}",
            LEFT_MARGIN,
            b.y - 10,
            size=14,
            font="HB",
            color=CLAY,
        )
        used_lines = b.paragraph(
            _normalize_whitespace(item),
            x=LEFT_MARGIN + 28,
            width=CONTENT_WIDTH - 28,
            size=10,
            font="H",
            color=INK_2,
            leading=13,
        )
        b.set_y(b.y - 4)
        del used_lines


def _bulleted_list(b: _PageBuilder, items: list[str]) -> None:
    for item in items:
        # bullet glyph
        b.text("·", LEFT_MARGIN + 4, b.y - 10, size=14, font="HB", color=CLAY)
        b.paragraph(
            _normalize_whitespace(item),
            x=LEFT_MARGIN + 18,
            width=CONTENT_WIDTH - 18,
            size=10,
            font="H",
            color=INK_2,
            leading=13,
        )
        b.set_y(b.y - 4)


def _voices_grid(b: _PageBuilder, participants: list[str]) -> None:
    # Three columns of name chips
    col_count = 3
    col_width = CONTENT_WIDTH / col_count
    row_height = 28
    rows = (len(participants) + col_count - 1) // col_count
    start_y = b.y
    for index, name in enumerate(participants):
        col = index % col_count
        row = index // col_count
        x = LEFT_MARGIN + col * col_width
        y = start_y - row * row_height
        # Mono circle with first initial
        initial = name.strip()[:1].upper() or "?"
        b.circle(x + 11, y - 11, radius=10, fill=SOFT_FILL, stroke=INK)
        b.text(initial, x + 11, y - 15, size=12, font="HBO", color=CLAY, align="center")  # pii-ok: HBO = Helvetica-BoldOblique (pdfme font code)
        b.text(
            _truncate(name, 26),
            x + 28,
            y - 10,
            size=11,
            font="HB",
            color=INK,
        )
        b.text("SPEAKER", x + 28, y - 21, size=7, font="H", color=INK_3, char_spacing=1.4)
    b.set_y(start_y - rows * row_height - 4)


def _topic_rows(
    b: _PageBuilder,
    workstreams: list[str],
    descriptions: dict,
) -> None:
    """Topic outline — name on its own line, one-sentence description
    beneath it when available. Replaces the confidence-bar treatment which
    was a quality-audit surface, not meeting content.
    """
    for stream in workstreams:
        b.text(
            _truncate(stream, 90),
            LEFT_MARGIN,
            b.y - 10,
            size=11,
            font="HB",
            color=INK,
        )
        b.set_y(b.y - 22)
        desc = descriptions.get(stream, "").strip()
        if desc:
            b.paragraph(
                desc,
                x=LEFT_MARGIN,
                width=CONTENT_WIDTH,
                size=9.5,
                font="H",
                color=INK_2,
                leading=12.5,
            )
        b.gap(8)


def _workstream_rows(b: _PageBuilder, workstreams: list[str], confidences: dict) -> None:
    for index, stream in enumerate(workstreams, start=1):
        raw = confidences.get(stream)
        conf = int(round(float(raw) * 100)) if raw is not None else None
        b.text(
            f"{index:02d}",
            LEFT_MARGIN,
            b.y - 10,
            size=9,
            font="H",
            color=INK_3,
        )
        name_w = CONTENT_WIDTH - 36 - 110 - 36
        b.text(
            _truncate(stream, 80),
            LEFT_MARGIN + 28,
            b.y - 10,
            size=10.5,
            font="HB",
            color=INK,
        )
        bar_x = LEFT_MARGIN + 28 + name_w + 18
        bar_w = 80
        bar_y = b.y - 8
        if conf is not None:
            # Track
            b.rect(bar_x, bar_y - 2, width=bar_w, height=3, fill=RULE_COLOR)
            # Fill
            b.rect(
                bar_x,
                bar_y - 2,
                width=int(bar_w * conf / 100),
                height=3,
                fill=CLAY if conf >= 80 else INK_2 if conf >= 60 else INK_3,
            )
            b.text(f"{conf}%", PAGE_WIDTH - RIGHT_MARGIN, b.y - 10, size=9, font="H", color=INK_3, align="right")
        else:
            b.text("—", PAGE_WIDTH - RIGHT_MARGIN, b.y - 10, size=9, font="H", color=INK_3, align="right")
        # Hairline under row
        b.hline_from(LEFT_MARGIN, PAGE_WIDTH - RIGHT_MARGIN, y=b.y - 18, color=RULE_COLOR, thickness=0.3, dotted=True)
        b.set_y(b.y - 22)


def _two_column_panels(
    b: _PageBuilder,
    left: tuple[str, list[str], str],
    right: tuple[str, list[str], str],
) -> None:
    panel_width = (CONTENT_WIDTH - 16) / 2
    start_y = b.y
    for i, (label, items, empty) in enumerate((left, right)):
        x = LEFT_MARGIN + i * (panel_width + 16)
        # Section rule scoped to half-width
        b.text(label.upper(), x, start_y, size=9, font="HB", color=INK, char_spacing=1.4)
        if items:
            b.text(f"{len(items):02d}", x + 70, start_y, size=8, font="H", color=INK_3, char_spacing=1.2)
        b.hline_from(x + 95, x + panel_width, y=start_y + 3, color=RULE_COLOR)
        b.rect(x, start_y - 70, width=panel_width, height=60, fill=SOFT_FILL)
        if items:
            for j, item in enumerate(items[:3]):
                b.text(
                    f"· {_truncate(item, 70)}",
                    x + 10,
                    start_y - 22 - j * 14,
                    size=9,
                    font="H",
                    color=INK_2,
                )
        else:
            b.text(empty, x + 10, start_y - 36, size=10, font="HO", color=INK_3)
    b.set_y(start_y - 84)


def _source_block(b: _PageBuilder, overview: dict) -> None:
    # Source filename only — vault path used to live here but it leaks
    # the user's local note layout to anyone who receives the PDF.
    col_w = CONTENT_WIDTH / 2
    start_y = b.y
    b.text("RECORDING", LEFT_MARGIN, start_y, size=8, font="H", color=INK_3, char_spacing=1.4)
    b.text(
        _truncate(overview.get("source_file") or "—", 48),
        LEFT_MARGIN,
        start_y - 14,
        size=10,
        font="H",
        color=INK,
    )
    b.text("STATUS", LEFT_MARGIN + col_w, start_y, size=8, font="H", color=INK_3, char_spacing=1.4)
    status_label = "Promoted to vault" if overview.get("promoted_at") else _status_label(overview)
    b.text(
        _truncate(status_label, 60),
        LEFT_MARGIN + col_w,
        start_y - 14,
        size=10,
        font="H",
        color=INK,
    )
    b.vline(LEFT_MARGIN + col_w - 12, start_y - 18, start_y + 4, color=RULE_COLOR)
    b.set_y(start_y - 24)


def _signoff_block(b: _PageBuilder, overview: dict) -> None:
    height = 36
    y = b.y
    # Background fill
    b.rect(LEFT_MARGIN, y - height, width=CONTENT_WIDTH, height=height, fill=SOFT_FILL)
    # Left accent rule
    b.rect(LEFT_MARGIN, y - height, width=3, height=height, fill=CLAY)
    b.text(
        "APPROVED & PROMOTED" if overview["status"] == "promoted" else "READY FOR APPROVAL",
        LEFT_MARGIN + 16,
        y - 14,
        size=9,
        font="HB",
        color=INK,
        char_spacing=1.4,
    )
    b.text(
        f"reviewed by you · {_format_datetime(overview['created_at'])}",
        LEFT_MARGIN + 16,
        y - 26,
        size=9,
        font="H",
        color=INK_3,
    )
    b.text(
        "mm✦",
        PAGE_WIDTH - RIGHT_MARGIN - 12,
        y - 22,
        size=18,
        font="HBO",  # pii-ok: HBO = Helvetica-BoldOblique (pdfme font code)
        color=CLAY,
        align="right",
    )
    b.set_y(y - height - 4)


# ── Page builder: tracks cursor + emits PDF content stream commands ─────────


class _PageBuilder:
    """Imperative cursor over PDF pages; emits raw content stream commands."""

    FONT_KEYS = {
        "H": "F1",   # Helvetica
        "HB": "F2",  # Helvetica-Bold
        "HO": "F3",  # Helvetica-Oblique
        "HBO": "F4", # Helvetica-BoldOblique  # pii-ok: HBO = Helvetica-BoldOblique (pdfme font code)
    }

    AVG_CHAR_WIDTH = {
        "H": 0.50,
        "HB": 0.53,
        "HO": 0.50,
        "HBO": 0.53,  # pii-ok: HBO = Helvetica-BoldOblique (pdfme font code)
    }

    def __init__(self, pages: list[list[Cmd]]):
        self.pages = pages
        self.y: float = PAGE_HEIGHT - TOP_MARGIN

    @property
    def current(self) -> list[Cmd]:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_HEIGHT - TOP_MARGIN

    def ensure_room(self, needed: float, overview: dict | None = None) -> None:
        """Insert a continuation page if drawing `needed` more points
        would overflow the bottom margin. Optional `overview` lets the
        caller redraw the header on the new page.
        """
        if self.y - needed < BOTTOM_MARGIN + 30:
            self.new_page()
            if overview is not None:
                _draw_header(self, overview, page_num=len(self.pages))

    def set_y(self, y: float) -> None:
        self.y = y

    def gap(self, dy: float) -> None:
        self.y -= dy

    # ── high-level helpers ────────────────────────────────────────────────

    def headline(self, text: str) -> None:
        # Break at ~36 chars for the headline width
        wrapped = textwrap.wrap(text, width=36) or [text]
        for line in wrapped:
            self.text(line, LEFT_MARGIN, self.y - 22, size=24, font="HB", color=INK)
            self.set_y(self.y - 28)

    def meta_row(self, overview: dict) -> None:
        # Duration + friendly status. Date was previously the first part
        # of this row — but the page header and the At-a-glance grid
        # below it both also carry the date, so it appeared four times
        # on a two-page document. Friendly status replaces the raw
        # `extracted` / `transcribed` schema string.
        parts = [
            f"{round(float(overview['duration_seconds']) / 60)} min",
            _status_label(overview),
        ]
        x = LEFT_MARGIN
        for i, part in enumerate(parts):
            if i:
                self.text(" · ", x, self.y - 10, size=10, font="H", color=INK_3)
                x += self.text_width(" · ", size=10, font="H")
            color = CLAY if i == len(parts) - 1 else INK_3
            font = "HB" if i == len(parts) - 1 else "H"
            self.text(part, x, self.y - 10, size=10, font=font, color=color)
            x += self.text_width(part, size=10, font=font)
        self.set_y(self.y - 14)

    def lbl_strong(self, text: str) -> None:
        self.text(text.upper(), LEFT_MARGIN, self.y - 9, size=9, font="HB", color=INK, char_spacing=1.4)
        self.set_y(self.y - 12)

    # ── primitives ────────────────────────────────────────────────────────

    def text(
        self,
        text: str,
        x: float,
        y: float,
        *,
        size: float,
        font: str,
        color: tuple[float, float, float],
        align: str = "left",
        char_spacing: float = 0.0,
    ) -> None:
        if not text:
            return
        if align == "right":
            x -= self.text_width(text, size=size, font=font, char_spacing=char_spacing)
        elif align == "center":
            x -= self.text_width(text, size=size, font=font, char_spacing=char_spacing) / 2
        font_key = self.FONT_KEYS.get(font, "F1")
        r, g, b_ = color
        cs = f"{char_spacing} Tc " if char_spacing else ""
        cmd = (
            f"BT {cs}{r:.3f} {g:.3f} {b_:.3f} rg "
            f"/{font_key} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm "
            f"({_pdf_text(text)}) Tj ET"
        )
        if char_spacing:
            cmd += " BT 0 Tc ET"
        self.current.append(Cmd(cmd))

    def paragraph(
        self,
        text: str,
        *,
        x: float,
        width: float,
        size: float,
        font: str,
        color: tuple[float, float, float],
        leading: float,
    ) -> int:
        char_w = self.AVG_CHAR_WIDTH.get(font, 0.5) * size
        chars_per_line = max(1, int(width / char_w))
        wrapped = textwrap.wrap(text, width=chars_per_line) or [text]
        for line in wrapped:
            self.text(line, x, self.y - size, size=size, font=font, color=color)
            self.set_y(self.y - leading)
        return len(wrapped)

    def text_width(self, text: str, *, size: float, font: str, char_spacing: float = 0.0) -> float:
        return len(text) * (self.AVG_CHAR_WIDTH.get(font, 0.5) * size + char_spacing)

    def hline_from(
        self,
        x1: float,
        x2: float,
        *,
        y: float,
        color: tuple[float, float, float],
        thickness: float = 0.5,
        dotted: bool = False,
    ) -> None:
        r, g, b_ = color
        dash = "[1 2] 0 d " if dotted else "[] 0 d "
        self.current.append(
            Cmd(
                f"q {r:.3f} {g:.3f} {b_:.3f} RG {thickness} w {dash}"
                f"{x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S Q"
            )
        )

    def vline(
        self,
        x: float,
        y1: float,
        y2: float,
        *,
        color: tuple[float, float, float],
    ) -> None:
        r, g, b_ = color
        self.current.append(
            Cmd(
                f"q {r:.3f} {g:.3f} {b_:.3f} RG 0.5 w "
                f"{x:.2f} {y1:.2f} m {x:.2f} {y2:.2f} l S Q"
            )
        )

    def rect(
        self,
        x: float,
        y: float,
        *,
        width: float,
        height: float,
        fill: tuple[float, float, float],
    ) -> None:
        r, g, b_ = fill
        self.current.append(
            Cmd(
                f"q {r:.3f} {g:.3f} {b_:.3f} rg "
                f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f Q"
            )
        )

    def circle(
        self,
        cx: float,
        cy: float,
        *,
        radius: float,
        fill: tuple[float, float, float],
        stroke: tuple[float, float, float] | None = None,
    ) -> None:
        # Approximate a circle with four cubic bezier arcs
        k = 0.5522847498 * radius
        fr, fg, fb = fill
        ops = [f"q {fr:.3f} {fg:.3f} {fb:.3f} rg "]
        if stroke:
            sr, sg, sb = stroke
            ops.append(f"{sr:.3f} {sg:.3f} {sb:.3f} RG 0.6 w ")
        ops.append(f"{cx + radius:.2f} {cy:.2f} m ")
        ops.append(f"{cx + radius:.2f} {cy + k:.2f} {cx + k:.2f} {cy + radius:.2f} {cx:.2f} {cy + radius:.2f} c ")
        ops.append(f"{cx - k:.2f} {cy + radius:.2f} {cx - radius:.2f} {cy + k:.2f} {cx - radius:.2f} {cy:.2f} c ")
        ops.append(f"{cx - radius:.2f} {cy - k:.2f} {cx - k:.2f} {cy - radius:.2f} {cx:.2f} {cy - radius:.2f} c ")
        ops.append(f"{cx + k:.2f} {cy - radius:.2f} {cx + radius:.2f} {cy - k:.2f} {cx + radius:.2f} {cy:.2f} c ")
        ops.append("B Q" if stroke else "f Q")
        self.current.append(Cmd("".join(ops)))

    def aperture(self, cx: float, cy: float, *, radius: float) -> None:
        """Monogram mark — matches the dashboard sidebar: midnight rounded
        square holding the chartreuse italic 'm' from "Meeting*m*ind". The
        name is kept for backwards compatibility with the old aperture mark.
        """
        side = radius * 2
        x = cx - radius
        y = cy - radius
        corner = radius * 0.42  # rx=16 on a 60-unit side in the dashboard SVG
        k = 0.5522847498 * corner  # cubic bezier circle approximation constant
        ir, ig, ib = INK
        # Filled rounded-square body (midnight). Path traces corners with
        # cubic beziers; PDF origin is bottom-left so we walk counter-clockwise
        # starting at the bottom-left corner's lower tangent point.
        ops = [
            f"q {ir:.3f} {ig:.3f} {ib:.3f} rg ",
            f"{x + corner:.2f} {y:.2f} m ",
            f"{x + side - corner:.2f} {y:.2f} l ",
            f"{x + side - corner + k:.2f} {y:.2f} "
            f"{x + side:.2f} {y + corner - k:.2f} "
            f"{x + side:.2f} {y + corner:.2f} c ",
            f"{x + side:.2f} {y + side - corner:.2f} l ",
            f"{x + side:.2f} {y + side - corner + k:.2f} "
            f"{x + side - corner + k:.2f} {y + side:.2f} "
            f"{x + side - corner:.2f} {y + side:.2f} c ",
            f"{x + corner:.2f} {y + side:.2f} l ",
            f"{x + corner - k:.2f} {y + side:.2f} "
            f"{x:.2f} {y + side - corner + k:.2f} "
            f"{x:.2f} {y + side - corner:.2f} c ",
            f"{x:.2f} {y + corner:.2f} l ",
            f"{x:.2f} {y + corner - k:.2f} "
            f"{x + corner - k:.2f} {y:.2f} "
            f"{x + corner:.2f} {y:.2f} c ",
            "h f Q",
        ]
        self.current.append(Cmd("".join(ops)))
        # Chartreuse italic "m" centered inside. Helvetica-Oblique stands in
        # for the dashboard's DM Serif Display since Type1 fonts are not
        # embedded; the chartreuse + italic + rounded square still reads as
        # the product monogram at a glance.
        glyph_size = radius * 2.0
        baseline_y = cy - radius * 0.55
        self.text(
            "m",
            cx,
            baseline_y,
            size=glyph_size,
            font="HBO",  # pii-ok: HBO = Helvetica-BoldOblique (pdfme font code)
            color=CLAY,
            align="center",
        )


# ── Assembly: turn the page commands into a real PDF file ───────────────────


def _assemble(pages: list[list[Cmd]]) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # object IDs are 1-indexed

    catalog_id = add(b"")  # placeholder, set after Pages built
    pages_id = add(b"")    # placeholder, replaced below

    font_h_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_hb_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    font_ho_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>")
    font_hbo_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-BoldOblique >>")

    page_ids: list[int] = []
    for page_cmds in pages:
        stream = "\n".join(cmd.text for cmd in page_cmds).encode("latin-1", errors="replace")
        content_id = add(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        page_id = add(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << "
                f"/F1 {font_h_id} 0 R /F2 {font_hb_id} 0 R "
                f"/F3 {font_ho_id} 0 R /F4 {font_hbo_id} 0 R "
                f">> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        page_ids.append(page_id)

    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    )

    output = bytearray(b"%PDF-1.4\n%\xc1\xc1\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


# ── tiny utilities ──────────────────────────────────────────────────────────


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(value: str, limit: int) -> str:
    value = _normalize_whitespace(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _format_datetime(value: str) -> str:
    cleaned = value.replace("T", " ").split(".")[0]
    return cleaned


def _format_date(value: str) -> str:
    cleaned = _format_datetime(value)
    return cleaned.split(" ")[0] if " " in cleaned else cleaned


def _format_time(value: str) -> str:
    cleaned = _format_datetime(value)
    return cleaned.split(" ", 1)[1] if " " in cleaned else ""


def _estimate_confidence(overview: dict) -> int:
    # Rough proxy: base 65 + bonuses for filled sections, capped at 96.
    score = 65
    if overview["summary"] and len(overview["summary"]) > 80:
        score += 10
    if len(overview["key_takeaways"]) >= 3:
        score += 7
    if overview["speaker_status"] == "complete":
        score += 8
    if len(overview["workstreams"]) >= 3:
        score += 6
    return min(96, score)


def _pdf_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )
