import React, { createContext, useContext, type ReactNode } from "react";

// Speaker-color machinery shared by main.tsx and components/*.tsx.
// Lives in its own module so components don't create a circular import
// by reaching back into main.tsx. The mechanics:
//
//   - Each meeting builds a Map<display_name_lowercase, slot 1..6>.
//   - SpeakerNameProvider seeds the map into context for the meeting
//     subtree.
//   - renderMentions(text) builds a regex from the map's keys and wraps
//     matches in <span class="mm-spk-mention" data-spk={slot}> so the
//     CSS palette (already keyed off [data-spk=N]) drives the color.
//
// `useSpeakerSlot(name)` is a thin hook for components that have a
// specific speaker name in hand (e.g. CoG standout chip) and want to
// render it with the right palette color directly.

export type SpeakerNameSlots = Map<string, number>;

export const SpeakerNameContext = createContext<SpeakerNameSlots | null>(null);

export function SpeakerNameProvider({
  slots,
  children,
}: {
  slots: SpeakerNameSlots;
  children: ReactNode;
}) {
  return React.createElement(
    SpeakerNameContext.Provider,
    { value: slots },
    children,
  );
}

/**
 * Build the name→slot map from the per-meeting speakerNumberById map
 * and the display_name lookup. Keys are case-folded; the regex in
 * renderMentions is built case-insensitive so callers can match against
 * any casing the model wrote.
 */
export function buildSpeakerNameSlots(
  speakerDisplayNameById: Map<string, string>,
  speakerNumberOf: (id: string) => number,
): SpeakerNameSlots {
  const slots = new Map<string, number>();
  speakerDisplayNameById.forEach((name, speakerId) => {
    const clean = String(name || "").trim();
    if (!clean) return;
    const slot = ((speakerNumberOf(String(speakerId)) - 1) % 6) + 1;
    slots.set(clean.toLowerCase(), slot);
  });
  return slots;
}

/** Lookup the slot for a specific name (case-insensitive). */
export function useSpeakerSlot(name: string | null | undefined): number | undefined {
  const slots = useContext(SpeakerNameContext);
  if (!slots || !name) return undefined;
  return slots.get(name.trim().toLowerCase());
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Render free-form text, wrapping any confirmed speaker name (and the
 * legacy @Mention / Speaker N patterns) in palette-colored spans.
 * Reads the name→slot map from context so callers don't need to
 * thread it down.
 */
export function renderMentions(text: string): ReactNode {
  return (
    <SpeakerNameContext.Consumer>
      {(speakerSlots) => renderMentionsInner(text, speakerSlots)}
    </SpeakerNameContext.Consumer>
  );
}

function renderMentionsInner(
  text: string,
  speakerSlots: SpeakerNameSlots | null,
): ReactNode {
  // Longest-first so "Sam Chen" beats "Sam" when both are confirmed.
  const names = speakerSlots
    ? Array.from(speakerSlots.keys()).sort((a, b) => b.length - a.length)
    : [];
  const namePattern = names.length
    ? `\\b(?:${names.map(escapeRegex).join("|")})\\b`
    : "";
  const baseLegacy = "@[\\w-]+(?: [\\w-]+)?|Speaker \\d+";
  const combined = namePattern ? `(${namePattern}|${baseLegacy})` : `(${baseLegacy})`;
  const regex = new RegExp(combined, "gi");
  const parts = text.split(regex);
  return parts.map((part, index) => {
    if (!part) return React.createElement(React.Fragment, { key: index });
    const lowered = part.toLowerCase();
    const slot = speakerSlots?.get(lowered);
    if (slot !== undefined) {
      return React.createElement(
        "span",
        {
          key: index,
          className: "mm-spk-mention",
          "data-spk": slot,
        },
        part,
      );
    }
    if (/^@/.test(part) || /^Speaker \d+/i.test(part)) {
      return React.createElement(
        "span",
        { key: index, className: "mm-minutes-mention" },
        part,
      );
    }
    return React.createElement(React.Fragment, { key: index }, part);
  });
}
