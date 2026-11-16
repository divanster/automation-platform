import { useEffect, useId, useRef, type ReactNode } from "react";

/**
 * An inline confirmation for a consequential action. It is not a modal: it appears where the action was started, takes
 * keyboard focus when it opens (so a screen reader announces it and Tab reaches its two buttons next), and Escape cancels.
 * While `busy` both buttons are disabled, so the action cannot be sent twice.
 */
export function ConfirmStep({
  title,
  children,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
  danger = false,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
}) {
  const titleId = useId();
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    container.current?.focus();
  }, []);

  return (
    <div
      ref={container}
      className={danger ? "confirm confirm-danger" : "confirm"}
      role="group"
      aria-labelledby={titleId}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) onCancel();
      }}
    >
      <p id={titleId} className="confirm-title">
        {title}
      </p>
      <div className="confirm-body">{children}</div>
      <div className="form-actions">
        <button type="button" className={danger ? "button button-danger" : "button button-primary"} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
        <button type="button" className="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
