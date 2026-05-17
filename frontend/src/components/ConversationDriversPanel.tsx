import React from "react";
import { renderMentions, useSpeakerSlot } from "../speakerColors";
import type { MeetingOverview } from "../types";

// Renders the "What drove this meeting" panel from
// conversation_drivers.py output. Surfaces 3-6 specific moments
// (chapter intros, pivot questions, decision seedings) that triggered
// the most follow-on discussion. Each row is clickable and jumps the
// transcript to the cited segment.
//
// Speaker confirmation matters: when speaker_confirmed=false the moment
// still renders, but with a "needs speaker review" pill instead of a
// name. Phase B+ accommodates the design-doc caveat that diarization
// quality is currently weak — the *moment* is reliable, the
// *attribution* often isn't until the user confirms it.

const KIND_LABELS: Record<string, string> = {
  topic_introduction: "Opened a chapter",
  pivot_question: "Pivot question",
  decision_moment: "Decision moment",
  reframing: "Reframing",
  challenge: "Challenge",
  unstick: "Unstuck the discussion",
};

export function ConversationDriversPanel({
  drivers,
  onJumpToSegment,
}: {
  drivers?: MeetingOverview["conversation_drivers"];
  // Optional click handler that scrolls the transcript view to the
  // cited segment. When absent, the row label still renders but isn't
  // interactive — allows the panel to be embedded in surfaces (e.g.
  // PDF preview) where transcript navigation isn't available.
  onJumpToSegment?: (segmentId: number) => void;
}) {
  if (!drivers || drivers.length === 0) return null;

  return (
    <div className="mm-drivers-panel" aria-label="What drove this meeting">
      <h4 className="mm-drivers-heading">What drove this meeting</h4>
      <ul className="mm-drivers-list">
        {drivers.map((driver) => (
          <DriverRow
            key={`${driver.kind}-${driver.segment_id}`}
            driver={driver}
            onJumpToSegment={onJumpToSegment}
          />
        ))}
      </ul>
    </div>
  );
}

function DriverRow({
  driver,
  onJumpToSegment,
}: {
  driver: NonNullable<MeetingOverview["conversation_drivers"]>[number];
  onJumpToSegment?: (segmentId: number) => void;
}) {
  const slot = useSpeakerSlot(driver.speaker_label);
  const interactive = !!onJumpToSegment;
  const Row = interactive ? "button" : "div";
  return (
    <li className={`mm-driver-row mm-driver-row-${driver.confidence}`}>
      <Row
        type={interactive ? "button" : undefined}
        className="mm-driver-button"
        onClick={
          interactive ? () => onJumpToSegment?.(driver.segment_id) : undefined
        }
      >
        <div className="mm-driver-kind">
          {KIND_LABELS[driver.kind] ?? driver.kind}
          {driver.source === "llm" && (
            <span
              className="mm-driver-source-badge"
              title={
                "Identified by the quality model. " +
                "Heuristic kinds (chapter intros, pivot questions, " +
                "decision moments) are computed deterministically; " +
                "this kind required interpretation."
              }
            >
              AI
            </span>
          )}
        </div>
        <div className="mm-driver-description">
          {renderMentions(driver.description)}
        </div>
        <div className="mm-driver-meta">
          {driver.speaker_confirmed ? (
            <span
              className="mm-driver-speaker mm-spk-mention"
              data-spk={slot}
            >
              {driver.speaker_label}
            </span>
          ) : (
            <span
              className="mm-driver-speaker mm-driver-speaker-unconfirmed"
              title="Speaker not yet confirmed — review on the Transcript page."
            >
              needs speaker review
            </span>
          )}
          <span className="mm-driver-impact">
            · {Math.round(driver.impact_seconds)}s follow-on
          </span>
        </div>
      </Row>
    </li>
  );
}
