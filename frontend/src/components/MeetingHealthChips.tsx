import React from "react";
import type { MeetingOverview } from "../types";
import { useSpeakerSlot } from "../speakerColors";

// Renders the deterministic team-level chip strip computed by
// meeting_health.py on the backend. Each chip only shows when its
// underlying signal exists (e.g. no chip for action_clarity when the
// meeting captured zero actions), so a sparse meeting doesn't get a
// half-empty strip. Hover surfaces an explanation per chip — the
// categorical label ("dominated", "low", etc.) alone doesn't tell the
// reader what threshold it represents.
export function MeetingHealthChips({
  health,
  centerOfGravity,
}: {
  health?: MeetingOverview["meeting_health"];
  // Conditional Center-of-Gravity chip. Renders only when the backend
  // detected a meaningful gravity-vs-talk-time divergence (i.e. a
  // low-talk, high-impact standout). Surfacing the boring case ("top
  // talker also drove the meeting") would be noise.
  centerOfGravity?: MeetingOverview["center_of_gravity"];
}) {
  if (!health && !centerOfGravity?.standout_speaker_id) return null;
  if (!health) {
    // Standout exists but health didn't compute — render just the CoG
    // chip on its own strip so it doesn't disappear.
    return (
      <div className="mm-health-strip" aria-label="Meeting health">
        <CoGChip cog={centerOfGravity} />
      </div>
    );
  }

  const chips: Array<{
    key: string;
    label: string;
    value: string;
    tone: "balanced" | "skewed" | "dominated" | "low" | "moderate" | "high" | "neutral";
    title: string;
  }> = [];

  if (health.participation_balance && health.top_speaker_share !== null) {
    const pct = Math.round(health.top_speaker_share * 100);
    const subtitleByBalance: Record<string, string> = {
      balanced: "Well-distributed across speakers.",
      skewed: "One voice carried significantly more of the meeting.",
      dominated: "One voice dominated the conversation.",
    };
    chips.push({
      key: "participation",
      label: "Participation",
      value:
        health.participation_balance === "balanced"
          ? `balanced · top ${pct}%`
          : `${health.participation_balance} · top ${pct}%`,
      tone: health.participation_balance,
      title:
        `Top speaker (${health.top_speaker_label ?? "unknown"}) used ${pct}% of words. ` +
        subtitleByBalance[health.participation_balance],
    });
  }

  if (health.speaker_count_silent > 0) {
    chips.push({
      key: "silent",
      label: "Quiet",
      value: `${health.speaker_count_silent} ${
        health.speaker_count_silent === 1 ? "speaker" : "speakers"
      } < 60s`,
      tone: "neutral",
      title: `${health.speaker_count_silent} attendee${
        health.speaker_count_silent === 1 ? " was" : "s were"
      } present but spoke for under a minute total.`,
    });
  }

  if (health.decision_density) {
    const densitySubtitle: Record<string, string> = {
      low: "Conversational pace; few firm decisions.",
      moderate: "Steady decision-making pace.",
      high: "Unusually high decision throughput.",
    };
    chips.push({
      key: "decision_density",
      label: "Decision density",
      value: health.decision_density,
      tone: health.decision_density,
      title: `${health.decision_count} ${
        health.decision_count === 1 ? "decision" : "decisions"
      } captured. ${densitySubtitle[health.decision_density]}`,
    });
  }

  if (health.unresolved_question_count > 0) {
    chips.push({
      key: "unresolved",
      label: "Unresolved",
      value: `${health.unresolved_question_count} ${
        health.unresolved_question_count === 1 ? "question" : "questions"
      }`,
      tone: "neutral",
      title:
        "Questions raised in the meeting that didn't get a clear on-topic " +
        "response. Worth a follow-up.",
    });
  }

  if (health.action_clarity && health.action_count > 0) {
    const claritySubtitle: Record<string, string> = {
      low: "Most actions are missing an owner or due date.",
      moderate: "Many actions have an owner and date; some are still floating.",
      high: "Most actions have both an owner and a due date.",
    };
    chips.push({
      key: "action_clarity",
      label: "Action clarity",
      value: health.action_clarity,
      tone: health.action_clarity,
      title: `${health.action_count} ${
        health.action_count === 1 ? "action" : "actions"
      } captured. ${claritySubtitle[health.action_clarity]}`,
    });
  }

  const hasCoG = !!centerOfGravity?.standout_speaker_id;
  if (chips.length === 0 && !hasCoG) return null;
  return (
    <div className="mm-health-strip" aria-label="Meeting health">
      {chips.map((chip) => (
        <div
          key={chip.key}
          className={`mm-health-chip mm-health-chip-${chip.tone}`}
          title={chip.title}
        >
          <span className="mm-health-chip-label">{chip.label}</span>
          <span className="mm-health-chip-value">{chip.value}</span>
        </div>
      ))}
      <CoGChip cog={centerOfGravity} />
    </div>
  );
}


// Renders only when the backend marked a standout speaker. The chip is
// heavier than the other health chips because the CoG flip is the
// non-obvious one to surface; the standout name renders in their own
// speaker-palette color so it visually ties to their transcript rows.
function CoGChip({
  cog,
}: {
  cog?: MeetingOverview["center_of_gravity"];
}) {
  const slot = useSpeakerSlot(cog?.standout_label);
  if (!cog?.standout_speaker_id || !cog.standout_label) return null;
  return (
    <div
      className="mm-health-chip mm-health-chip-cog"
      title={cog.standout_reason ?? undefined}
    >
      <span className="mm-health-chip-label">Driven by</span>
      <span
        className="mm-health-chip-value mm-spk-mention"
        data-spk={slot}
      >
        {cog.standout_label}
      </span>
    </div>
  );
}
