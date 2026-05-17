import React, { useMemo, useState } from "react";
import { api } from "../api";
import { renderMentions, useSpeakerSlot } from "../speakerColors";
import type { Observation, ObservationKind, Reflections } from "../types";

// The Reflections tab. Owner-only, experimental — see
// docs/design/meeting-output-improvements.md §4. Renders only when the
// parent has confirmed the feature flag is on (a 404 on
// /api/meetings/{id}/reflections hides the tab entirely upstream).
//
// Hard rules pinned by the design and enforced here:
//   1. Observation cards refuse to render when `evidence_segment_ids`
//      is empty (the backend already drops these, this is belt-and-
//      braces).
//   2. Confidence tone drives visual weight: low-confidence cards
//      fade so they read as "maybe" not "fact".
//   3. AI-generated footer is always present — the disclosure-effect
//      research says hiding it backfires worse than disclosing.
//   4. No aggregate score, no peer comparison, no streaks/badges.
//      The components below are intentionally observation-shaped, not
//      scorecard-shaped.

const KIND_LABEL: Record<ObservationKind, string> = {
  talk_time: "Talk time",
  interruption_pattern: "Interruption pattern",
  question_quality: "Question quality",
  unanswered_question: "Unanswered question",
  clarifying_question: "Clarifying question",
  uncertainty_admission: "Acknowledging uncertainty",
  invited_input: "Inviting input",
  specific_invitation: "Named invitation",
  paraphrase_check: "Paraphrase check",
  build_on_other: "Building on others",
  framing_quality: "Framing",
  loop_closure: "Closing the loop",
  delegation_balance: "Delegation",
  bluf_response: "Direct answer",
  decision_rationale: "Decision rationale",
  commitment: "Commitment made",
  decision_driven: "Decision driven",
};

// Plain-language explanations of why we suppressed Reflections. Keep
// these honest and short — "no Reflections — try again" is better than
// fabricating a "well-balanced meeting" message.
const SKIPPED_COPY: Record<
  Exclude<Reflections["skipped_reason"], null>,
  { title: string; body: string }
> = {
  no_owner_configured: {
    title: "Set yourself as the owner to see Reflections.",
    body:
      "Reflections are owner-only — they're about how you showed up in this " +
      "meeting. Configure yourself as the owner in Settings to enable them.",
  },
  skipped_per_meeting: {
    title: "Reflections off for this meeting.",
    body:
      "You marked this meeting as skipped. Click 'Enable for this meeting' " +
      "below if you want to generate Reflections after all.",
  },
  transcript_too_short: {
    title: "Not enough signal for Reflections.",
    body:
      "Reflections need at least 5 minutes of conversation to be meaningful. " +
      "Short meetings produce noisy observations, so we don't try.",
  },
  asr_confidence_too_low: {
    title: "Transcript quality is too low for Reflections.",
    body:
      "Average ASR confidence is below 0.6, so any observations the model " +
      "produced would be anchored to potentially mis-heard speech. Try " +
      "improving audio quality or re-running transcription.",
  },
  owner_spoke_too_little: {
    title: "You spoke less than a minute in this meeting.",
    body:
      "Reflections need at least 60 seconds of your speech to surface " +
      "anything useful. Nothing to reflect on here.",
  },
  compute_error: {
    title: "Couldn't generate Reflections.",
    body:
      "The model call failed — usually a transient network or local-model " +
      "issue. Try regenerating synthesis to retry.",
  },
};

type FeedbackValue = "helpful" | "unhelpful" | null;

function feedbackKey(meetingId: number, obs: Observation): string {
  // Local-only key. The thumbs-up/down NEVER leaves the browser per
  // design doc §6.9 — stored to localStorage and used only for the
  // user's own filtering preferences. (If we ever want to use it for
  // prompt tuning, that's a separate consent flow.)
  const ids = [...obs.evidence_segment_ids].sort((a, b) => a - b).join(",");
  return `mm-reflection-feedback:${meetingId}:${obs.kind}:${ids}`;
}


export function ReflectionsPanel({
  meetingId,
  reflections,
  onReload,
  onJumpToSegment,
}: {
  meetingId: number;
  reflections: Reflections;
  // Called after the user toggles skip-for-this-meeting so the parent
  // can re-fetch and the panel reflects the new state.
  onReload: () => void;
  // Click-through to the transcript. Optional so the panel can be
  // embedded in surfaces without transcript navigation.
  onJumpToSegment?: (segmentId: number) => void;
}) {
  const skipped = reflections.skipped_reason;
  const observations = reflections.observations;

  const handleSkipToggle = async (skip: boolean) => {
    try {
      await api.post(`/api/meetings/${meetingId}/reflections/skip?skip=${skip}`);
      onReload();
    } catch (err) {
      // Surface failures to console; the panel remains in its current
      // state so the user can retry. Toast wiring lives higher up.
      console.warn("reflections_skip_failed", err);
    }
  };

  return (
    <article className="mm-reflections-panel" aria-label="Reflections">
      <header className="mm-reflections-header">
        <h2 className="mm-reflections-title">Reflections</h2>
        <p className="mm-reflections-subtitle">
          {reflections.owner_display_name ? (
            <>
              How{" "}
              {renderMentions(reflections.owner_display_name)} showed up
              in this meeting.
            </>
          ) : (
            "How the owner showed up in this meeting."
          )}
          {" "}
          A mirror, not a judgement. Designed for frontier models — on
          small local models, observations may miss or misfire.
        </p>
      </header>

      <StatsStrip stats={reflections.stats} />

      {skipped && (
        <EmptyState reason={skipped} onUndoSkip={() => handleSkipToggle(false)} />
      )}

      {!skipped && observations.length === 0 && (
        <div className="mm-reflections-empty">
          <p className="mm-reflections-empty-title">
            Nothing notable surfaced this meeting.
          </p>
          <p className="mm-reflections-empty-body">
            The stats above are still here if you want them. Many
            meetings produce no observations — that's the intended
            behavior, not a bug.
          </p>
        </div>
      )}

      {!skipped && observations.length > 0 && (
        <ObservationsList
          meetingId={meetingId}
          observations={observations}
          onJumpToSegment={onJumpToSegment}
        />
      )}

      <footer className="mm-reflections-footer">
        <div className="mm-reflections-disclosure">
          Generated by an AI model. Each observation cites a specific
          segment — click through to verify.
        </div>
        {!skipped && (
          <button
            type="button"
            className="mm-reflections-skip-btn"
            onClick={() => handleSkipToggle(true)}
            title="Mark this meeting as opted-out. Persists across re-extractions."
          >
            Skip Reflections for this meeting
          </button>
        )}
      </footer>
    </article>
  );
}


function StatsStrip({ stats }: { stats: Reflections["stats"] }) {
  // Deterministic numerics — these are FACTS, not interpretations.
  // Always rendered, even on a skipped meeting, so the user can see
  // the raw signal even when no qualitative observations are surfaced.
  const pct = Math.round(stats.talk_time_pct * 100);
  const items: Array<{ label: string; value: string }> = [
    { label: "Talk time", value: `${pct}% · ${Math.round(stats.talk_time_seconds)}s` },
  ];
  if (stats.questions_asked > 0) {
    const openEnded =
      stats.questions_open_ended > 0 ? ` (${stats.questions_open_ended} open)` : "";
    items.push({
      label: "Questions asked",
      value: `${stats.questions_asked}${openEnded}`,
    });
  }
  if (stats.questions_unanswered > 0) {
    items.push({
      label: "Unanswered",
      value: `${stats.questions_unanswered}`,
    });
  }
  if (stats.commitments_made > 0) {
    items.push({
      label: "Commitments",
      value: `${stats.commitments_made}`,
    });
  }
  if (stats.uncertainty_admissions > 0) {
    items.push({
      label: "Admitted uncertainty",
      value: `${stats.uncertainty_admissions}×`,
    });
  }
  if (stats.inputs_invited > 0) {
    items.push({
      label: "Invited input",
      value: `${stats.inputs_invited}×`,
    });
  }
  return (
    <div className="mm-reflections-stats">
      {items.map((item) => (
        <div key={item.label} className="mm-reflections-stat">
          <div className="mm-reflections-stat-label">{item.label}</div>
          <div className="mm-reflections-stat-value">{item.value}</div>
        </div>
      ))}
    </div>
  );
}


function EmptyState({
  reason,
  onUndoSkip,
}: {
  reason: Exclude<Reflections["skipped_reason"], null>;
  onUndoSkip: () => void;
}) {
  const copy = SKIPPED_COPY[reason];
  return (
    <div className="mm-reflections-empty mm-reflections-empty-skipped">
      <p className="mm-reflections-empty-title">{copy.title}</p>
      <p className="mm-reflections-empty-body">{copy.body}</p>
      {reason === "skipped_per_meeting" && (
        <button
          type="button"
          className="mm-reflections-undo-btn"
          onClick={onUndoSkip}
        >
          Enable for this meeting
        </button>
      )}
    </div>
  );
}


function ObservationsList({
  meetingId,
  observations,
  onJumpToSegment,
}: {
  meetingId: number;
  observations: Observation[];
  onJumpToSegment?: (segmentId: number) => void;
}) {
  return (
    <ul className="mm-reflections-list">
      {observations.map((obs, index) => (
        <ObservationCard
          key={`${obs.kind}-${index}`}
          meetingId={meetingId}
          observation={obs}
          onJumpToSegment={onJumpToSegment}
        />
      ))}
    </ul>
  );
}


function ObservationCard({
  meetingId,
  observation,
  onJumpToSegment,
}: {
  meetingId: number;
  observation: Observation;
  onJumpToSegment?: (segmentId: number) => void;
}) {
  // Belt-and-braces: drop observations without evidence at render time
  // even though the backend already filters them. Defends against any
  // future code path that might bypass _coerce_observations.
  if (observation.evidence_segment_ids.length === 0) return null;

  const storageKey = feedbackKey(meetingId, observation);
  const initialFeedback = useMemo<FeedbackValue>(() => {
    if (typeof window === "undefined") return null;
    const stored = window.localStorage.getItem(storageKey);
    if (stored === "helpful" || stored === "unhelpful") return stored;
    return null;
  }, [storageKey]);
  const [feedback, setFeedback] = useState<FeedbackValue>(initialFeedback);

  const setFeedbackPersistent = (value: FeedbackValue) => {
    setFeedback(value);
    if (typeof window === "undefined") return;
    if (value === null) {
      window.localStorage.removeItem(storageKey);
    } else {
      window.localStorage.setItem(storageKey, value);
    }
  };

  return (
    <li className={`mm-reflection mm-reflection-${observation.confidence}`}>
      <div className="mm-reflection-kind">
        {KIND_LABEL[observation.kind] ?? observation.kind}
        <span className="mm-reflection-confidence" title="Model-reported confidence">
          {observation.confidence}
        </span>
      </div>
      <p className="mm-reflection-text">
        {renderMentions(observation.observation)}
      </p>

      <div className="mm-reflection-evidence">
        <span className="mm-reflection-evidence-label">Evidence:</span>
        {observation.evidence_segment_ids.map((sid) => (
          <button
            key={sid}
            type="button"
            className="mm-reflection-evidence-pill"
            onClick={onJumpToSegment ? () => onJumpToSegment(sid) : undefined}
            disabled={!onJumpToSegment}
            title={
              onJumpToSegment
                ? `Jump to segment ${sid}`
                : `Segment ${sid}`
            }
          >
            [{sid}]
          </button>
        ))}
      </div>

      {observation.why_this_matters && (
        <p className="mm-reflection-why">
          <span className="mm-reflection-why-label">Why this matters:</span>{" "}
          {renderMentions(observation.why_this_matters)}
        </p>
      )}
      {observation.suggested_next_time && (
        <p className="mm-reflection-next">
          <span className="mm-reflection-next-label">Next time:</span>{" "}
          {renderMentions(observation.suggested_next_time)}
        </p>
      )}

      {/* Helpful/unhelpful feedback stays local — never aggregated, never
       * shipped. Drives the user's own per-kind filtering preferences
       * (Phase E follow-up: settings page to mute specific kinds). */}
      <div className="mm-reflection-feedback" role="group" aria-label="Was this helpful?">
        <button
          type="button"
          className={`mm-reflection-thumb ${feedback === "helpful" ? "is-active" : ""}`}
          onClick={() =>
            setFeedbackPersistent(feedback === "helpful" ? null : "helpful")
          }
          aria-pressed={feedback === "helpful"}
          title="Helpful (stays local — never shipped)"
        >
          ▲ helpful
        </button>
        <button
          type="button"
          className={`mm-reflection-thumb ${feedback === "unhelpful" ? "is-active" : ""}`}
          onClick={() =>
            setFeedbackPersistent(feedback === "unhelpful" ? null : "unhelpful")
          }
          aria-pressed={feedback === "unhelpful"}
          title="Not helpful (stays local — never shipped)"
        >
          ▼ not helpful
        </button>
      </div>
    </li>
  );
}
