// Observatory primitives — marks, wordmark, sidebar, and small UI atoms.
// Every component reads from CSS vars defined in styles.css. No hardcoded hex.

import React from "react";

// ── Brand mark: midnight rounded-square holding the wordmark's chartreuse
// italic "m". Mirrors the accent character in "Meetingmind" so the standalone
// mark reads as the wordmark in monogram form. Replaces an earlier camera-iris
// aperture mark that read as too abstract.
export function Aperture({ size = 38 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="60" height="60" rx="16" fill="#0b1020" />
      <rect x="2" y="2" width="60" height="60" rx="16" fill="none" stroke="#c8ff5b" strokeOpacity="0.18" strokeWidth="1.2" />
      <text
        x="32"
        y="48"
        textAnchor="middle"
        fontFamily="DM Serif Display, Georgia, serif"
        fontStyle="italic"
        fontSize="44"
        fill="#c8ff5b"
      >
        m
      </text>
    </svg>
  );
}

// ── Wordmark — Aperture + "Meetingmind" with italic middle 'm' ─────────────
export function Wordmark({ tag = "local first · yours" }: { tag?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Aperture size={38} />
      <div style={{ lineHeight: 1 }}>
        <div
          className="mm-display"
          style={{ fontSize: 22, letterSpacing: "-0.02em", color: "var(--mm-ink)" }}
        >
          Meeting
          <span style={{ fontStyle: "italic", color: "var(--mm-clay)" }}>m</span>
          ind
        </div>
        <div className="mm-lbl" style={{ fontSize: 9, marginTop: 4 }}>
          {tag}
        </div>
      </div>
    </div>
  );
}

// ── Live indicator dot ─────────────────────────────────────────────────────
export function LiveDot({ label = "" }: { label?: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span className="mm-live" />
      {label && (
        <span
          className="mm-mono"
          style={{
            fontSize: 11,
            letterSpacing: "0.06em",
            color: "var(--mm-ink-2)",
            textTransform: "uppercase",
          }}
        >
          {label}
        </span>
      )}
    </span>
  );
}

// ── Sidebar ────────────────────────────────────────────────────────────────
export type SidebarKey =
  | "inbox"
  | "review"
  | "workstreams"
  | "people"
  | "archive"
  | "settings";

export function Sidebar({
  active,
  onSelect,
  inboxCount,
  reviewCount,
  workstreamCount,
  peopleCount,
  mode,
  onToggleMode,
  backendUrl,
  onOpenPalette,
  ownerName,
  onOpenOnboarding,
}: {
  active: SidebarKey;
  onSelect: (key: SidebarKey) => void;
  inboxCount: number;
  reviewCount: number;
  workstreamCount: number;
  peopleCount: number;
  mode: "night" | "day";
  onToggleMode: () => void;
  backendUrl?: string;
  onOpenPalette?: () => void;
  ownerName?: string | null;
  onOpenOnboarding?: () => void;
}) {
  const items: Array<{ key: SidebarKey; label: string; glyph: string; count?: number }> = [
    { key: "inbox", label: "Inbox", glyph: "★", count: inboxCount },
    { key: "review", label: "Review", glyph: "◐", count: reviewCount },
    { key: "workstreams", label: "Workstreams", glyph: "∴", count: workstreamCount },
    { key: "people", label: "People", glyph: "☉", count: peopleCount },
    { key: "archive", label: "Archive", glyph: "◇" },
    { key: "settings", label: "Settings", glyph: "⊕" },
  ];
  return (
    <aside className="mm-sidebar">
      <div className="mm-sidebar-brand">
        <Wordmark tag="local first · yours" />
      </div>
      <hr className="mm-rule" style={{ margin: "0 20px 20px" }} />
      <nav className="mm-sidebar-nav">
        {items.map((item) => (
          <button
            key={item.key}
            type="button"
            className={item.key === active ? "mm-nav-item is-active" : "mm-nav-item"}
            onClick={() => onSelect(item.key)}
          >
            <span className="mm-glyph" aria-hidden="true">{item.glyph}</span>
            <span style={{ flex: 1 }}>{item.label}</span>
            {item.count !== undefined && item.count > 0 && (
              <span className="mm-count">{String(item.count).padStart(2, "0")}</span>
            )}
          </button>
        ))}
      </nav>
      <div className="mm-sidebar-foot">
        {onOpenOnboarding && (
          <button
            type="button"
            className="mm-sidebar-identity"
            onClick={onOpenOnboarding}
            title={ownerName ? `Change identity (currently ${ownerName})` : "Set your identity"}
          >
            <span className="mm-pill-yours" style={{ marginRight: 6 }}>⌖</span>
            <span style={{ flex: 1, textAlign: "left" }}>
              {ownerName || "Set your identity"}
            </span>
            <span style={{ opacity: 0.55, fontSize: 10 }}>edit</span>
          </button>
        )}
        <div className="mm-lbl" style={{ marginBottom: 8 }}>
          {mode === "day" ? "day mode" : "night mode"}
        </div>
        <div className="mm-foot-row">
          <LiveDot label={backendUrl ? `${backendUrl.replace(/^https?:\/\//, "")} · LOCAL` : "127.0.0.1 · LOCAL"} />
        </div>
        {onOpenPalette && (
          <button
            type="button"
            className="mm-btn"
            style={{
              marginTop: 12,
              fontSize: 11,
              padding: "6px 12px",
              width: "100%",
              justifyContent: "space-between",
            }}
            onClick={onOpenPalette}
            title="Open command palette"
          >
            <span>⌘ palette</span>
            <span style={{ opacity: 0.6, fontFamily: "var(--mm-font-mono)" }}>⌘K</span>
          </button>
        )}
        <button
          type="button"
          className="mm-btn"
          style={{
            marginTop: 8,
            fontSize: 11,
            padding: "6px 12px",
            width: "100%",
            justifyContent: "center",
          }}
          onClick={onToggleMode}
          aria-label={mode === "day" ? "Switch to night mode" : "Switch to day mode"}
        >
          <span aria-hidden="true">{mode === "day" ? "☾ enter dusk" : "☀ enter day"}</span>
        </button>
      </div>
    </aside>
  );
}

// ── Speaker chip + avatar ──────────────────────────────────────────────────
export function SpeakerChip({
  name,
  speakerNumber,
  onClick,
  title,
}: {
  name: string;
  speakerNumber: number;
  onClick?: () => void;
  title?: string;
}) {
  const slot = ((speakerNumber - 1) % 6) + 1;
  // When non-interactive, render as <span> so keyboard users don't tab
  // into a non-actionable element and screen readers don't announce a
  // button role for a static chip.
  if (!onClick) {
    return (
      <span className="mm-spk-chip" data-spk={slot} title={title} style={{ cursor: "default" }}>
        {name}
      </span>
    );
  }
  return (
    <button type="button" className="mm-spk-chip" data-spk={slot} onClick={onClick} title={title}>
      {name}
    </button>
  );
}

export function SpeakerAvatar({
  name,
  speakerNumber,
  size = 40,
}: {
  name: string;
  speakerNumber: number;
  size?: number;
}) {
  const slot = ((speakerNumber - 1) % 6) + 1;
  return (
    <span
      className="mm-spk-avatar"
      data-spk={slot}
      style={{ width: size, height: size, fontSize: size * 0.5 }}
    >
      {name.trim().charAt(0).toUpperCase() || "?"}
    </span>
  );
}

// Tiny circular dot used in the meeting-card stack
export function SpeakerDot({ speakerNumber }: { speakerNumber: number }) {
  const slot = ((speakerNumber - 1) % 6) + 1;
  return <span className="mm-spk-dot" data-spk={slot} />;
}

// ── Confidence bar ─────────────────────────────────────────────────────────
export function ConfidenceBar({
  percent,
  width = 54,
}: {
  percent: number | null | undefined;
  width?: number;
}) {
  const value = Math.max(0, Math.min(100, percent ?? 0));
  const tone = percent === undefined || percent === null ? "is-mid" : value < 70 ? "is-low" : value >= 80 ? "" : "is-mid";
  const label =
    percent === undefined || percent === null
      ? "Confidence unknown"
      : `Confidence ${Math.round(value)} percent`;
  return (
    <div
      className={`mm-confidence-bar ${tone}`}
      style={{ width, flex: "none" }}
      role="img"
      aria-label={label}
    >
      <i style={{ width: `${value}%` }} />
    </div>
  );
}

// ── Pill (status + filter) ─────────────────────────────────────────────────
export function Pill({
  children,
  tone,
  dot,
  onClick,
  active,
}: {
  children: React.ReactNode;
  tone?: "clay" | "sage" | "quiet";
  dot?: boolean;
  onClick?: () => void;
  active?: boolean;
}) {
  const classes = ["mm-pill"];
  if (tone === "clay") classes.push("mm-pill-clay");
  if (tone === "sage") classes.push("mm-pill-sage");
  if (tone === "quiet") classes.push("mm-pill-quiet");
  if (active) classes.push("mm-pill-active");
  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      className={classes.join(" ")}
      onClick={onClick}
      style={onClick ? { cursor: "pointer", border: "none" } : undefined}
    >
      {dot && <span className="mm-dot" />}
      {children}
    </Tag>
  );
}

// ── SectionRule (label + hairline fill) ────────────────────────────────────
export function SectionRule({ label, count }: { label: string; count?: string | number }) {
  return (
    <div className="mm-section-rule">
      <span className="mm-lbl-strong">{label}</span>
      {count !== undefined && <span className="mm-lbl">{count}</span>}
    </div>
  );
}

// ── Italicized accent helper ───────────────────────────────────────────────
export function Ital({ children }: { children: React.ReactNode }) {
  return <span className="mm-ital">{children}</span>;
}
