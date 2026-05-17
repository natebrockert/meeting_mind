import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ConfidenceBar, Ital, SpeakerChip } from "../observatory";
import { formatMs, formatPctShort } from "../format";
import { highlightImportantText } from "./highlight";
import { playExactAudioSpan } from "../audio";
import { SegmentExtras } from "./SegmentExtras";
import type { OverlapHint, Segment } from "../types";

// Transcript row — one rendered entry per diarized segment in the
// Transcript view. Owns:
//
//   - The per-row UI (speaker chip, edit/speaker buttons, mini transport
//     for clip playback, highlighted body text).
//   - A locally-scoped SegmentEditModal (Edit button → focused textarea
//     overlay with Cmd/Ctrl-Enter to save).
//   - A SegmentExtras footer (lives in ./SegmentExtras.tsx) for comments
//     + edit history + revert.
//
// Wrapped in React.memo so playhead ticks don't re-render every row in a
// long transcript. For memo to do real work the parent (in main.tsx) must
// pass stable identities for `onCorrectSegment` / `onEditSegmentSpeaker` /
// `onAfterRevert` (use `useCallback`). The row rebuilds its own per-segment
// closures internally via useCallback keyed on `segment`.
//
// Extracted from main.tsx in v0.1.5 along with SegmentEditModal.
export const TranscriptRow = memo(function TranscriptRow({
  meetingId,
  segment,
  speakerName,
  speakerNumber,
  highlightTerms,
  ownerTerms,
  showConfidenceChips,
  active,
  overlapHint,
  onCorrectSegment,
  onEditSegmentSpeaker,
  onAfterRevert,
}: {
  meetingId: number;
  segment: Segment;
  speakerName: string;
  speakerNumber: number;
  highlightTerms: string[];
  ownerTerms: string[];
  showConfidenceChips: boolean;
  active: boolean;
  overlapHint?: OverlapHint;
  onCorrectSegment: (segment: Segment, text: string) => void;
  onEditSegmentSpeaker: (segment: Segment) => void;
  onAfterRevert: () => void;
}) {
  const onSaveText = useCallback(
    (value: string) => onCorrectSegment(segment, value),
    [onCorrectSegment, segment],
  );
  const onEditSpeaker = useCallback(
    () => onEditSegmentSpeaker(segment),
    [onEditSegmentSpeaker, segment],
  );
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(segment.text);
  const [playing, setPlaying] = useState(false);
  const [audioReady, setAudioReady] = useState(false);
  const [audioError, setAudioError] = useState("");
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    setDraft(segment.text);
    setEditing(false);
  }, [segment.id, segment.text]);

  // Audit N1 (v0.1.6): the audio element keeps firing `timeupdate` /
  // `pause` events after the row unmounts (the listeners were attached
  // by playExactAudioSpan, not by useEffect). Those handlers call
  // `setPlaying(false)` etc., which React silently ignores on an
  // unmounted component but warns about in strict-mode dev. Guard with
  // a mounted ref so the onStop / onError / onReady callbacks no-op
  // after unmount.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
      audioRef.current?.pause();
    },
    [],
  );

  const speakerConf = (segment.speaker_confidence ?? segment.confidence ?? null) as number | null;
  const textConf = (segment.text_confidence ?? segment.confidence ?? null) as number | null;
  const lowAssign = (speakerConf ?? 1) * 100 < 60;

  // Audit M2 (v0.1.5): highlightImportantText builds a RegExp from the term
  // arrays on every call. Memoise so a parent re-render doesn't redo the
  // regex work for every row — only when the segment text or term list
  // changes for THIS row.
  const highlighted = useMemo(
    () => highlightImportantText(segment.text, highlightTerms, ownerTerms),
    [segment.text, highlightTerms, ownerTerms],
  );

  const togglePlay = () => {
    if (playing) {
      audioRef.current?.pause();
      setPlaying(false);
      return;
    }
    setAudioReady(false);
    setAudioError("");
    audioRef.current = playExactAudioSpan(
      meetingId,
      segment.start_ms,
      segment.end_ms,
      () => {
        if (!mountedRef.current) return;
        setPlaying(false);
        setAudioReady(false);
      },
      () => {
        if (!mountedRef.current) return;
        setAudioReady(true);
      },
      () => {
        if (!mountedRef.current) return;
        setPlaying(false);
        setAudioReady(false);
        setAudioError("Audio unavailable. Confirm the backend is running and the source file exists.");
      },
    );
    setPlaying(true);
  };

  const seekClip = (deltaSeconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const startSeconds = Math.max(0, segment.start_ms / 1000);
    const endSeconds = Math.max(startSeconds + 0.1, segment.end_ms / 1000);
    audio.currentTime = Math.min(endSeconds - 0.1, Math.max(startSeconds, audio.currentTime + deltaSeconds));
  };

  return (
    <article
      id={`segment-${segment.id}`}
      className={[
        "mm-tx-row",
        active ? "is-active" : "",
        lowAssign ? "is-low" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="mm-tx-meta">
        <span className="mm-mono" style={{ fontSize: 12, color: "var(--mm-ink-3)" }}>
          {formatMs(segment.start_ms)}
        </span>
        <SpeakerChip
          name={speakerName}
          speakerNumber={speakerNumber}
          onClick={onEditSpeaker}
          title="Edit speaker"
        />
        {overlapHint && <OverlapBadge hint={overlapHint} />}
        {showConfidenceChips && (
          <>
            <div className="mm-tx-conf">
              <span className="mm-lbl" style={{ fontSize: 9 }}>
                cont
              </span>
              <ConfidenceBar percent={textConf !== null ? textConf * 100 : null} />
              <span className="mm-mono">{formatPctShort(textConf)}</span>
            </div>
            <div className="mm-tx-conf">
              <span className="mm-lbl" style={{ fontSize: 9 }}>
                spk
              </span>
              <ConfidenceBar percent={speakerConf !== null ? speakerConf * 100 : null} />
              <span
                className="mm-mono"
                style={{ color: lowAssign ? "var(--mm-clay)" : "var(--mm-ink-3)" }}
              >
                {formatPctShort(speakerConf)}
              </span>
            </div>
          </>
        )}
        <div className="mm-tx-actions">
          <div className="mm-tx-action-group">
            <button type="button" className="mm-btn mm-btn-sm" onClick={onEditSpeaker}>
              ⇆ Speaker
            </button>
            <button type="button" className="mm-btn mm-btn-sm" onClick={() => setEditing(true)}>
              ✎ Edit
            </button>
          </div>
          <div className="mm-tx-action-group mm-tx-action-transport">
            <button
              type="button"
              className="mm-icon-btn"
              onClick={() => seekClip(-5)}
              disabled={!audioReady}
              aria-label="Rewind 5 seconds"
              title="Rewind 5 seconds"
            >
              ⤺
            </button>
            <button
              type="button"
              className="mm-icon-btn"
              onClick={togglePlay}
              aria-label={playing ? "Pause" : "Play"}
              title={playing ? "Pause" : "Play"}
            >
              {playing ? "❚❚" : "▷"}
            </button>
            <button
              type="button"
              className="mm-icon-btn"
              onClick={() => seekClip(5)}
              disabled={!audioReady}
              aria-label="Skip 5 seconds"
              title="Skip 5 seconds"
            >
              ⤻
            </button>
          </div>
        </div>
      </div>
      {audioError && <p className="mm-tx-error">{audioError}</p>}
      <p>{highlighted}</p>
      {editing && (
        <SegmentEditModal
          segment={segment}
          speakerName={speakerName}
          speakerNumber={speakerNumber}
          draft={draft}
          setDraft={setDraft}
          onClose={() => {
            setDraft(segment.text);
            setEditing(false);
          }}
          onSave={(value) => {
            onSaveText(value);
            setEditing(false);
          }}
        />
      )}
      <SegmentExtras meetingId={meetingId} segment={segment} onAfterRevert={onAfterRevert} />
    </article>
  );
});

// ── Overlap hint badge — v0.2.9. Surfaces v0.2.2's linguistic overlap
// detector results on the row whose segment is flagged. The kind tells you
// what pattern fired (yield_marker / stutter_interrupt / rapid_alternation),
// the title shows the evidence string the detector matched on.
//
// v0.2.10: when the hint carries a partner_segment_id, the badge becomes
// a button — clicking it scrolls the partner row into view so reviewers
// can see both sides of the overlap without scrubbing.
function OverlapBadge({ hint }: { hint: OverlapHint }) {
  // v0.2.10 polish: distinct color cues per kind so the user can scan
  // the transcript for the patterns they care about. Sage = polite
  // handoff (yield), clay = collision (interrupt), berry = chaos
  // (crosstalk). The class names compose with the existing
  // mm-overlap-badge sizing so layout stays identical.
  const label =
    hint.kind === "yield_marker"
      ? "yield"
      : hint.kind === "stutter_interrupt"
        ? "interrupt"
        : hint.kind === "rapid_alternation"
          ? "crosstalk"
          : "overlap";
  const toneClass =
    hint.kind === "yield_marker"
      ? "mm-overlap-badge-yield"
      : hint.kind === "stutter_interrupt"
        ? "mm-overlap-badge-interrupt"
        : hint.kind === "rapid_alternation"
          ? "mm-overlap-badge-crosstalk"
          : "";
  const tooltip = `${hint.kind} · ${hint.evidence} · ${formatPctShort(hint.confidence)}`;
  // Audit M1 (pre-merge): use title for the visible hover-tooltip and
  // aria-label for the screen reader. Don't set both with the same text —
  // SRs would announce the badge twice. The decorative glyph is hidden
  // from AT via aria-hidden so the label reads cleanly.
  const partnerId = hint.partner_segment_id;
  const scrollToPartner = useCallback(() => {
    if (partnerId == null) return;
    const el = document.getElementById(`segment-${partnerId}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [partnerId]);

  if (partnerId == null) {
    return (
      <span
        className={`mm-pill mm-pill-quiet mm-overlap-badge ${toneClass}`}
        title={tooltip}
        aria-label={`Overlap hint: ${label}, ${hint.evidence}`}
      >
        <span aria-hidden="true">⤬ </span>
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`mm-pill mm-pill-quiet mm-overlap-badge mm-overlap-badge-link ${toneClass}`}
      title={`${tooltip} · click to jump to partner`}
      aria-label={`Overlap hint: ${label}, ${hint.evidence}. Jump to partner segment.`}
      onClick={scrollToPartner}
    >
      <span aria-hidden="true">⤬ </span>
      {label}
    </button>
  );
}

// ── Segment text edit modal — mirrors SpeakerEditModal so Edit and Speaker
// share the same overlay UX instead of one popping inline and one as a modal.
function SegmentEditModal({
  segment,
  speakerName,
  speakerNumber,
  draft,
  setDraft,
  onClose,
  onSave,
}: {
  segment: Segment;
  speakerName: string;
  speakerNumber: number;
  draft: string;
  setDraft: (value: string) => void;
  onClose: () => void;
  onSave: (value: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Audit L2 (v0.1.6): the keydown listener was re-registered every
  // keystroke because `draft` was in the deps array. Stash the latest
  // draft + save/close handlers in refs so the listener can stay
  // mounted for the modal's lifetime.
  //
  // Refs are assigned inline at render top (rather than via useEffect)
  // so the latest value is visible synchronously on the same render —
  // a post-fix audit flagged that a useEffect-based sync would have a
  // sub-frame window where a Cmd+Enter could read a stale draft.
  const draftRef = useRef(draft);
  const onSaveRef = useRef(onSave);
  const onCloseRef = useRef(onClose);
  draftRef.current = draft;
  onSaveRef.current = onSave;
  onCloseRef.current = onClose;

  useEffect(() => {
    textareaRef.current?.focus();
    textareaRef.current?.select();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        onSaveRef.current(draftRef.current);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="mm-modal"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <SpeakerChip name={speakerName} speakerNumber={speakerNumber} />
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onClose}>
            Close ✕
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>
            Edit <Ital>transcript</Ital>
          </h2>
          <div className="mm-modal-sub">
            at {formatMs(segment.start_ms)} · saved edits keep the original in history
          </div>
        </div>
        <div className="mm-modal-section">
          <textarea
            ref={textareaRef}
            className="mm-input mm-input-square"
            style={{ width: "100%", minHeight: 160, fontFamily: "var(--mm-font-body)" }}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <div
            style={{
              display: "flex",
              gap: 8,
              marginTop: 12,
              justifyContent: "flex-end",
            }}
          >
            <button type="button" className="mm-btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="mm-btn mm-btn-primary"
              onClick={() => onSave(draft)}
            >
              ✓ Approve
            </button>
          </div>
          <small style={{ display: "block", marginTop: 8, opacity: 0.7 }}>
            ⌘/Ctrl + Enter to save · Esc to cancel
          </small>
        </div>
      </section>
    </div>
  );
}

