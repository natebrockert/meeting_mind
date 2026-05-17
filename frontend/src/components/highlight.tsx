import React from "react";

// Highlights "important" terms in a string of transcript text. Used by
// TranscriptRow to render `<mark>` around model-extracted key terms +
// owner-name mentions. Pulled out of main.tsx in v0.1.5 so TranscriptRow
// can move into components/.
//
// Returns either:
//   - the original `text` string if no terms apply, OR
//   - an array of React.Fragments / <mark> elements ready to render

// Audit L1 (v0.1.6): internal helper — not exported because nothing
// outside this module references it.
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function highlightImportantText(
  text: string,
  terms: string[],
  ownerTerms: string[] = [],
): React.ReactNode {
  const cleanTerms = terms
    .map((term) => term.trim())
    .filter((term) => term.length >= 3)
    .sort((left, right) => right.length - left.length)
    .slice(0, 16);
  const cleanOwnerTerms = ownerTerms
    .map((term) => term.trim())
    .filter((term) => term.length >= 2);
  if (!cleanTerms.length && !cleanOwnerTerms.length) return text;
  // Owner terms must use \b word boundaries so "Sam" doesn't fire on "Samuel".
  const ownerPattern = cleanOwnerTerms.length
    ? cleanOwnerTerms.map((term) => `\\b${escapeRegExp(term)}\\b`).join("|")
    : null;
  const termPattern = cleanTerms.length ? cleanTerms.map(escapeRegExp).join("|") : null;
  const combined = [ownerPattern, termPattern].filter(Boolean).join("|");
  const pattern = new RegExp(`(${combined})`, "ig");
  return text.split(pattern).map((part, index) => {
    if (!part) return <React.Fragment key={`${part}-${index}`}>{part}</React.Fragment>;
    const lower = part.toLowerCase();
    const isOwner = cleanOwnerTerms.some((term) => term.toLowerCase() === lower);
    if (isOwner) {
      return (
        <mark key={`owner-${index}`} className="mm-mark-owner" title="Mentions you">
          {part}
        </mark>
      );
    }
    const matched = cleanTerms.some((term) => term.toLowerCase() === lower);
    if (!matched) return <React.Fragment key={`${part}-${index}`}>{part}</React.Fragment>;
    return <mark key={`${part}-${index}`}>{part}</mark>;
  });
}
