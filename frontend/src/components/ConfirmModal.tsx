import React, { useEffect } from "react";

// Themed replacement for `window.confirm`. Used for destructive actions
// (delete meeting, revert segment, etc.) so the dialog matches the rest of
// the dashboard instead of the OS-native popup. Escape and backdrop click
// both dismiss; the primary button auto-focuses.
//
// Extracted from main.tsx in v0.1.5 — used in 7 places so it earned its
// own file.

export function ConfirmModal({
  title,
  body,
  confirmLabel,
  cancelLabel = "Cancel",
  confirmTone = "primary",
  onConfirm,
  onCancel,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  confirmTone?: "primary" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={onCancel}>
      <section
        className="mm-modal mm-modal-confirm"
        role="alertdialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <div className="mm-lbl-strong">Confirm</div>
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onCancel}>
            Close ✕
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>{title}</h2>
        </div>
        <div className="mm-modal-section">
          <div style={{ fontSize: 13.5, lineHeight: 1.55, color: "var(--mm-ink-2)" }}>
            {body}
          </div>
          <div
            style={{
              display: "flex",
              gap: 8,
              justifyContent: "flex-end",
              marginTop: 16,
            }}
          >
            <button type="button" className="mm-btn" onClick={onCancel}>
              {cancelLabel}
            </button>
            <button
              type="button"
              className={
                confirmTone === "danger"
                  ? "mm-btn mm-btn-danger"
                  : "mm-btn mm-btn-primary"
              }
              onClick={onConfirm}
              autoFocus
            >
              {confirmLabel}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
