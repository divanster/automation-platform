import { useRef, useState } from "react";

import { approveRequest } from "../api/review";
import type { ApproveResponse, RequestDetail, WarningPair } from "../api/types";
import { ConfirmStep } from "./ConfirmStep";
import { FindingsList } from "./FindingsList";
import { Notice } from "./Status";

// Approve the CURRENT revision. This component is rendered only when the server lists "approve" in `allowed_actions`, and
// it sends the revision id, the content hash and the acknowledged warnings exactly as the server last reported them. The
// server re-checks everything (including who may approve) and may still refuse; its answer is shown by the page.

const pairKey = (pair: WarningPair) => `${pair.code}\u0000${pair.field_path}`;

/** The distinct warnings of the revision: one acknowledgement per (code, field path), which is what the server compares. */
function distinctWarnings(warnings: readonly WarningPair[]): WarningPair[] {
  const seen = new Map<string, WarningPair>();
  for (const w of warnings) if (!seen.has(pairKey(w))) seen.set(pairKey(w), w);
  return [...seen.values()];
}

export function ApprovePanel({
  detail,
  blocked,
  onApproved,
  onError,
}: {
  detail: RequestDetail;
  /** True while something else on the page (an open edit) makes approving the wrong thing to do right now. */
  blocked: boolean;
  onApproved: (result: ApproveResponse) => void;
  onError: (error: unknown) => void;
}) {
  const revision = detail.current_revision;
  const warnings = distinctWarnings(detail.warnings);
  const [acknowledged, setAcknowledged] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);

  if (revision === null) return null; // nothing to approve; the server would not allow it either

  const allAcknowledged = warnings.every((w) => acknowledged.has(pairKey(w)));
  const messageFor = (w: WarningPair) => detail.findings.find((f) => !f.blocking && f.code === w.code && f.field_path === w.field_path);
  const blocking = detail.findings.filter((f) => f.blocking);

  function toggle(w: WarningPair, checked: boolean) {
    setAcknowledged((current) => {
      const next = new Set(current);
      if (checked) next.add(pairKey(w));
      else next.delete(pairKey(w));
      return next;
    });
    setConfirming(false);
  }

  async function confirm() {
    if (submitting.current || revision === null) return;
    submitting.current = true;
    setBusy(true);
    try {
      const result = await approveRequest(detail.run_id, revision.id, revision.content_hash, warnings);
      onApproved(result);
    } catch (error) {
      setConfirming(false);
      onError(error);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="approve-title">
      <h2 id="approve-title">Approve</h2>
      <dl className="facts">
        <div>
          <dt>Revision to approve</dt>
          <dd>{revision.number}</dd>
        </div>
        <div className="wide">
          <dt>Content hash (SHA-256)</dt>
          <dd>
            <code className="hash">{revision.content_hash}</code>
          </dd>
        </div>
      </dl>
      <p className="muted">Approval applies to exactly this revision. If anyone edits the request afterwards, the approval no longer matches it.</p>

      {blocking.length > 0 ? (
        <>
          <h3>Blocking findings</h3>
          <FindingsList findings={blocking} />
        </>
      ) : null}

      {warnings.length > 0 ? (
        <fieldset className="ack">
          <legend>Warnings to acknowledge ({warnings.length})</legend>
          <p className="muted">Tick each warning to confirm that you have reviewed it. Every current warning must be acknowledged.</p>
          {warnings.map((w) => {
            const finding = messageFor(w);
            const id = `ack-${pairKey(w)}`;
            return (
              <div key={pairKey(w)} className="ack-row">
                <input id={id} type="checkbox" checked={acknowledged.has(pairKey(w))} disabled={busy || blocked} onChange={(e) => toggle(w, e.target.checked)} />
                <label htmlFor={id}>
                  <code>{w.code}</code> <code className="muted">{w.field_path || "(whole request)"}</code>
                  {finding ? <span className="ack-message"> {finding.message}</span> : null}
                  {finding?.excerpt ? <blockquote className="excerpt">{finding.excerpt}</blockquote> : null}
                </label>
              </div>
            );
          })}
        </fieldset>
      ) : (
        <p className="muted">This revision has no warnings.</p>
      )}

      {blocked ? <Notice kind="warning">Save or discard your open edit before approving.</Notice> : null}

      {confirming ? (
        <ConfirmStep title={`Approve revision ${revision.number}?`} confirmLabel="Confirm approval" busy={busy} onConfirm={confirm} onCancel={() => setConfirming(false)}>
          <p>
            You are approving revision {revision.number} (hash <code>{revision.content_hash.slice(0, 12)}…</code>)
            {warnings.length > 0 ? ` and acknowledging ${warnings.length} warning${warnings.length === 1 ? "" : "s"}` : ""}.
          </p>
        </ConfirmStep>
      ) : (
        <div className="form-actions">
          <button type="button" className="button button-primary" disabled={!allAcknowledged || blocked || busy} onClick={() => setConfirming(true)}>
            Approve revision {revision.number}…
          </button>
          {!allAcknowledged ? <span className="muted">Acknowledge every warning first.</span> : null}
        </div>
      )}
    </section>
  );
}
