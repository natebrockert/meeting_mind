import React, { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { formatDateTime } from "../format";
import { ConfirmModal } from "./ConfirmModal";
import type { Segment, SegmentComment, SegmentEdit } from "../types";

// Per-segment "extras" tray rendered under each TranscriptRow: comments
// (threaded, with reply / resolve / delete) + an inline edit-history
// diff with one-click revert. Extracted out of TranscriptRow.tsx in
// v0.1.5 so the row file stays focused on the row itself.
//
// This file owns four internal helpers that are only used inside the
// SegmentExtras tree: EditDiffRow, CommentBubble, CommentInput, and the
// LCS-based renderWordDiff. They're intentionally not exported.

// ── Per-segment extras: comments + edit history ──────────────────────────
export function SegmentExtras({
  meetingId,
  segment,
  onAfterRevert,
}: {
  meetingId: number;
  segment: Segment;
  onAfterRevert: () => void;
}) {
  const [showComments, setShowComments] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const [comments, setComments] = useState<SegmentComment[] | null>(null);
  const [edits, setEdits] = useState<SegmentEdit[] | null>(null);
  const [draft, setDraft] = useState("");
  const [replyTo, setReplyTo] = useState<number | null>(null);
  // Pending-revert target: replaces window.confirm with the themed
  // ConfirmModal so the destructive action matches the rest of the
  // dashboard rather than dropping an OS-native popup.
  const [pendingRevertText, setPendingRevertText] = useState<string | null>(null);

  useEffect(() => {
    if (!showComments) return;
    void api
      .get<{ comments: SegmentComment[] }>(`/api/meetings/${meetingId}/comments`)
      .then((data) => setComments(data.comments.filter((c) => c.segment_id === segment.id)))
      .catch(() => setComments([]));
  }, [showComments, meetingId, segment.id]);

  useEffect(() => {
    if (!showHistory) return;
    void api
      .get<{ edits: SegmentEdit[] }>(`/api/meetings/${meetingId}/segments/${segment.id}/edits`)
      .then((data) => setEdits(data.edits))
      .catch(() => setEdits([]));
  }, [showHistory, meetingId, segment.id]);

  const submitComment = async () => {
    const body = draft.trim();
    if (!body) return;
    try {
      const params = new URLSearchParams({ body });
      if (replyTo !== null) params.set("parent_id", String(replyTo));
      const added = await api.post<SegmentComment>(
        `/api/meetings/${meetingId}/segments/${segment.id}/comment?${params.toString()}`,
      );
      setComments((current) => [...(current ?? []), added]);
      setDraft("");
      setReplyTo(null);
    } catch {
      // silent — keep the draft so the user can retry
    }
  };

  const removeComment = async (commentId: number) => {
    try {
      await api.delete(`/api/meetings/${meetingId}/comments/${commentId}`);
      setComments((current) => (current ?? []).filter((c) => c.id !== commentId));
    } catch {
      // noop
    }
  };

  const toggleResolved = async (comment: SegmentComment) => {
    const next = comment.status === "resolved" ? false : true;
    try {
      const updated = await api.post<SegmentComment>(
        `/api/meetings/${meetingId}/comments/${comment.id}/resolve?resolved=${next}`,
      );
      setComments((current) =>
        (current ?? []).map((c) => (c.id === comment.id ? { ...c, ...updated } : c)),
      );
    } catch {
      // noop
    }
  };

  const revertTo = (text: string) => {
    setPendingRevertText(text);
  };

  const confirmRevert = async () => {
    const text = pendingRevertText;
    setPendingRevertText(null);
    if (text === null) return;
    try {
      await api.post(
        `/api/meetings/${meetingId}/segments/${segment.id}/revert?text=${encodeURIComponent(text)}`,
      );
      onAfterRevert();
    } catch {
      // noop
    }
  };

  // Group comments into threads keyed by their root id.
  const allComments = comments ?? [];
  const roots = allComments.filter((c) => c.parent_id === null);
  const childrenByParent = new Map<number, SegmentComment[]>();
  allComments.forEach((c) => {
    if (c.parent_id !== null) {
      const list = childrenByParent.get(c.parent_id) ?? [];
      list.push(c);
      childrenByParent.set(c.parent_id, list);
    }
  });
  const visibleRoots = showResolved ? roots : roots.filter((c) => c.status !== "resolved");
  const hiddenCount = roots.length - visibleRoots.length;

  return (
    <div className="mm-tx-extras">
      {pendingRevertText !== null && (
        <ConfirmModal
          title="Revert this segment?"
          body={
            <>
              <p style={{ margin: 0 }}>
                Replace the current segment text with the selected history
                version. The current text becomes a new entry in this
                segment's edit history — nothing is permanently lost.
              </p>
            </>
          }
          confirmLabel="↺ Revert"
          confirmTone="danger"
          onConfirm={() => void confirmRevert()}
          onCancel={() => setPendingRevertText(null)}
        />
      )}
      <div className="mm-tx-extras-bar">
        <button type="button" onClick={() => setShowComments((v) => !v)}>
          ✎ {showComments ? "hide" : "comments"}
          {!showComments && roots.length > 0 && (
            <span className="mm-pill" style={{ marginLeft: 6, fontSize: 10, padding: "1px 6px" }}>
              {roots.filter((c) => c.status !== "resolved").length}
            </span>
          )}
        </button>
        <button type="button" onClick={() => setShowHistory((v) => !v)}>
          ↻ {showHistory ? "hide" : "edit history"}
        </button>
      </div>
      {showComments && (
        <div className="mm-stack-2">
          {visibleRoots.map((root) => {
            const replies = childrenByParent.get(root.id) ?? [];
            const isResolved = root.status === "resolved";
            return (
              <div
                key={root.id}
                className={isResolved ? "mm-comment-thread is-resolved" : "mm-comment-thread"}
              >
                <CommentBubble
                  comment={root}
                  onDelete={() => void removeComment(root.id)}
                  onReply={() => setReplyTo(root.id)}
                  onResolve={() => void toggleResolved(root)}
                  isRoot
                />
                {replies.map((reply) => (
                  <div key={reply.id} className="mm-comment-reply">
                    <CommentBubble
                      comment={reply}
                      onDelete={() => void removeComment(reply.id)}
                    />
                  </div>
                ))}
                {replyTo === root.id && (
                  <div className="mm-comment-reply">
                    <CommentInput
                      value={draft}
                      onChange={setDraft}
                      onSubmit={() => void submitComment()}
                      onCancel={() => {
                        setReplyTo(null);
                        setDraft("");
                      }}
                      placeholder="Reply…"
                      autoFocus
                    />
                  </div>
                )}
              </div>
            );
          })}
          {!visibleRoots.length && roots.length === 0 && (
            <p className="mm-empty" style={{ fontSize: 12 }}>
              No comments yet — leave one to flag context for later review.
            </p>
          )}
          {hiddenCount > 0 && (
            <button
              type="button"
              className="mm-btn mm-btn-ghost mm-btn-sm"
              onClick={() => setShowResolved((v) => !v)}
              style={{ alignSelf: "flex-start" }}
            >
              {showResolved ? "hide" : "show"} {hiddenCount} resolved
            </button>
          )}
          {replyTo === null && (
            <CommentInput
              value={draft}
              onChange={setDraft}
              onSubmit={() => void submitComment()}
              placeholder="Add a comment…"
            />
          )}
        </div>
      )}
      {showHistory && (
        <div className="mm-edit-history">
          {(edits ?? []).map((edit) => (
            <EditDiffRow key={edit.id} edit={edit} onRevert={() => void revertTo(edit.original_text)} />
          ))}
          {edits && !edits.length && (
            <p className="mm-empty" style={{ fontSize: 12, margin: 0 }}>
              No prior edits recorded for this segment.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function EditDiffRow({ edit, onRevert }: { edit: SegmentEdit; onRevert: () => void }) {
  // Memoise the LCS-based diff so a long edit history doesn't recompute it
  // on every render of the parent.
  const diff = useMemo(
    () => renderWordDiff(edit.original_text, edit.corrected_text),
    [edit.original_text, edit.corrected_text],
  );
  return (
    <div className="mm-edit-history-row">
      <span className="mm-mono">{formatDateTime(edit.applied_at || edit.created_at)}</span>
      <div>
        <p style={{ margin: 0 }}>{diff}</p>
        <div className="mm-lbl" style={{ marginTop: 4, fontSize: 9 }}>
          {edit.reason || "manual edit"}
        </div>
      </div>
      <button
        type="button"
        className="mm-btn mm-btn-ghost mm-btn-sm"
        onClick={onRevert}
        title="Revert this segment to the original text"
      >
        revert
      </button>
    </div>
  );
}

function CommentBubble({
  comment,
  onDelete,
  onReply,
  onResolve,
  isRoot,
}: {
  comment: SegmentComment;
  onDelete: () => void;
  onReply?: () => void;
  onResolve?: () => void;
  isRoot?: boolean;
}) {
  const isResolved = comment.status === "resolved";
  return (
    <div className={isResolved ? "mm-comment is-resolved" : "mm-comment"}>
      <div>{comment.body}</div>
      <div className="mm-comment-meta">
        <span>
          {comment.author} · {formatDateTime(comment.created_at)}
          {isResolved && <span style={{ marginLeft: 8, color: "var(--mm-sage)" }}>· resolved</span>}
        </span>
        <div className="mm-row" style={{ gap: 8 }}>
          {isRoot && onResolve && (
            <button type="button" onClick={onResolve}>
              {isResolved ? "reopen" : "resolve"}
            </button>
          )}
          {onReply && (
            <button type="button" onClick={onReply}>
              reply
            </button>
          )}
          <button type="button" onClick={onDelete}>
            delete
          </button>
        </div>
      </div>
    </div>
  );
}

function CommentInput({
  value,
  onChange,
  onSubmit,
  onCancel,
  placeholder,
  autoFocus,
}: {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void;
  onCancel?: () => void;
  placeholder: string;
  autoFocus?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);
  return (
    <div className="mm-comment-input">
      <textarea
        ref={ref}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            onSubmit();
          }
        }}
      />
      <div className="mm-row" style={{ gap: 4 }}>
        {onCancel && (
          <button type="button" className="mm-btn mm-btn-ghost mm-btn-sm" onClick={onCancel}>
            cancel
          </button>
        )}
        <button
          type="button"
          className="mm-btn mm-btn-primary mm-btn-sm"
          onClick={onSubmit}
          disabled={!value.trim()}
        >
          ✓ post
        </button>
      </div>
    </div>
  );
}

/** Render an inline word diff between `before` and `after`. Adds get
 * .mm-diff-add styling, removals .mm-diff-del, unchanged inline. Used by
 * the per-segment edit history so users can see exactly what changed.
 */
function renderWordDiff(before: string, after: string): React.ReactNode {
  const beforeTokens = before.split(/(\s+)/);
  const afterTokens = after.split(/(\s+)/);
  // LCS-based diff over word tokens (case-insensitive equality).
  const m = beforeTokens.length;
  const n = afterTokens.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  const eq = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();
  for (let i = m - 1; i >= 0; i -= 1) {
    for (let j = n - 1; j >= 0; j -= 1) {
      dp[i][j] = eq(beforeTokens[i], afterTokens[j])
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: React.ReactNode[] = [];
  let i = 0;
  let j = 0;
  let key = 0;
  while (i < m && j < n) {
    if (eq(beforeTokens[i], afterTokens[j])) {
      out.push(<React.Fragment key={key++}>{afterTokens[j]}</React.Fragment>);
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      if (beforeTokens[i].trim()) {
        out.push(
          <span key={key++} className="mm-diff-del">
            {beforeTokens[i]}
          </span>,
        );
      } else {
        out.push(<React.Fragment key={key++}>{beforeTokens[i]}</React.Fragment>);
      }
      i += 1;
    } else {
      if (afterTokens[j].trim()) {
        out.push(
          <span key={key++} className="mm-diff-add">
            {afterTokens[j]}
          </span>,
        );
      } else {
        out.push(<React.Fragment key={key++}>{afterTokens[j]}</React.Fragment>);
      }
      j += 1;
    }
  }
  while (i < m) {
    out.push(
      <span key={key++} className="mm-diff-del">
        {beforeTokens[i++]}
      </span>,
    );
  }
  while (j < n) {
    out.push(
      <span key={key++} className="mm-diff-add">
        {afterTokens[j++]}
      </span>,
    );
  }
  return out;
}
