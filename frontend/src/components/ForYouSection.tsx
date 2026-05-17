import React from "react";
import { renderMentions } from "../speakerColors";
import type { MeetingOverview } from "../types";

// Owner-aware lead section at the top of the Mind Map. Surfaces three
// things the OWNER specifically should know before reading anything
// else:
//
//   1. Action items assigned to them
//   2. Decisions made in this meeting that mention them (mention-based,
//      not authorship — labelled honestly)
//   3. Open questions they raised that are still unresolved
//
// All three are derived data from existing overview fields (no extra
// LLM call) and only render when there's something to show. If the
// owner is unset or had no presence in the meeting, the section is
// hidden entirely.

export function ForYouSection({
  overview,
  onJumpToSegment,
}: {
  overview: MeetingOverview;
  onJumpToSegment?: (segmentId: number) => void;
}) {
  const ownerName = overview.owner?.display_name;
  if (!overview.owner?.configured || !ownerName) return null;

  const yourActions = overview.your_actions ?? [];
  const yourDecisions = overview.your_decisions ?? [];
  const yourOpenQuestions =
    overview.open_question_details?.filter(
      (oq) =>
        oq.raised_by &&
        oq.raised_by.toLowerCase() === ownerName.toLowerCase(),
    ) ?? [];

  const hasContent =
    yourActions.length > 0 ||
    yourDecisions.length > 0 ||
    yourOpenQuestions.length > 0;
  if (!hasContent) return null;

  return (
    <section className="mm-for-you" aria-label={`For ${ownerName}`}>
      <h3 className="mm-for-you-heading">For {ownerName}</h3>
      <p className="mm-for-you-sub">
        Pulled from the rest of the page so you can see what this
        meeting means for you specifically.
      </p>

      {yourActions.length > 0 && (
        <div className="mm-for-you-block">
          <div className="mm-for-you-label">
            Your action items ({yourActions.length})
          </div>
          <ul className="mm-for-you-list">
            {yourActions.map((action, index) => (
              <li key={`a-${index}`}>{renderMentions(action)}</li>
            ))}
          </ul>
        </div>
      )}

      {yourOpenQuestions.length > 0 && (
        <div className="mm-for-you-block">
          <div className="mm-for-you-label">
            Open questions you raised ({yourOpenQuestions.length})
          </div>
          <ul className="mm-for-you-list">
            {yourOpenQuestions.map((oq, index) => {
              const button =
                onJumpToSegment && oq.source_segment_ids.length > 0;
              return (
                <li key={`oq-${index}`}>
                  {renderMentions(oq.question)}
                  {oq.status !== "unanswered" && (
                    <span className="mm-for-you-oq-status">
                      {" "}
                      · {oq.status.replace("_", " ")}
                    </span>
                  )}
                  {button && (
                    <button
                      type="button"
                      className="mm-for-you-jump"
                      onClick={() =>
                        onJumpToSegment(oq.source_segment_ids[0])
                      }
                    >
                      jump to clip
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {yourDecisions.length > 0 && (
        <div className="mm-for-you-block">
          <div className="mm-for-you-label">
            Decisions that mention you ({yourDecisions.length})
          </div>
          <ul className="mm-for-you-list">
            {yourDecisions.map((d, index) => (
              <li key={`d-${index}`}>{renderMentions(d)}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
