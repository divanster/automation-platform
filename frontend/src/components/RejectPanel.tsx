import { useRef, useState } from "react";

import { rejectRequest } from "../api/review";
import { REJECTION_REASONS, type RejectResponse, type RejectionReason, type RequestDetail } from "../api/types";
import { ConfirmStep } from "./ConfirmStep";
import { Notice } from "./Status";

// Reject the CURRENT revision. Rendered only when the server lists "reject" in `allowed_actions`. The reason is one of the
// API's fixed codes: there is no free-text field, because free text would end up in audit events, logs and exports.

export const REASON_LABELS: Record<RejectionReason, string> = {
  incorrect_content: "The content is incorrect",
  incomplete_content: "The content is incomplete",
  not_supported_by_source: "The content is not supported by the source text",
  other: "Another reason",
};

export function RejectPanel({
  detail,
  blocked,
  onRejected,
  onError,
}: {
  detail: RequestDetail;
  blocked: boolean;
  onRejected: (result: RejectResponse) => void;
  onError: (error: unknown) => void;
}) {
  const revision = detail.current_revision;
  const [reason, setReason] = useState<RejectionReason | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);

  if (revision === null) return null;

  async function confirm() {
    if (submitting.current || revision === null || reason === null) return;
    submitting.current = true;
    setBusy(true);
    try {
      const result = await rejectRequest(detail.run_id, revision.id, revision.content_hash, reason);
      onRejected(result);
    } catch (error) {
      setConfirming(false);
      onError(error);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="reject-title">
      <h2 id="reject-title">Reject</h2>
      <p className="muted">
        Rejecting records that revision {revision.number} is not acceptable. It can no longer be approved; the way forward is an edit, which creates a new revision.
      </p>
      <fieldset disabled={busy || blocked}>
        <legend>Reason</legend>
        {REJECTION_REASONS.map((code) => (
          <div key={code} className="radio-row">
            <input
              id={`reason-${code}`}
              type="radio"
              name="reject-reason"
              value={code}
              checked={reason === code}
              onChange={() => {
                setReason(code);
                setConfirming(false);
              }}
            />
            <label htmlFor={`reason-${code}`}>{REASON_LABELS[code]}</label>
          </div>
        ))}
      </fieldset>

      {blocked ? <Notice kind="warning">Save or discard your open edit before rejecting.</Notice> : null}

      {confirming && reason !== null ? (
        <ConfirmStep title={`Reject revision ${revision.number}?`} confirmLabel="Confirm rejection" busy={busy} danger onConfirm={confirm} onCancel={() => setConfirming(false)}>
          <p>
            Reason: <strong>{REASON_LABELS[reason]}</strong>. This is recorded in the audit history and cannot be undone.
          </p>
        </ConfirmStep>
      ) : (
        <div className="form-actions">
          <button type="button" className="button button-danger" disabled={reason === null || blocked || busy} onClick={() => setConfirming(true)}>
            Reject revision {revision.number}…
          </button>
          {reason === null ? <span className="muted">Choose a reason first.</span> : null}
        </div>
      )}
    </section>
  );
}
