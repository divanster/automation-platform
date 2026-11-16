import { useState } from "react";
import { Link, useParams } from "react-router";

import type { RequestDetail } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { ApprovePanel } from "../components/ApprovePanel";
import { AUDIT_PERMISSION, AuditHistory } from "../components/AuditHistory";
import { DraftEditor } from "../components/DraftEditor";
import { DraftView } from "../components/DraftView";
import { FindingsList } from "../components/FindingsList";
import { RejectPanel } from "../components/RejectPanel";
import { StateBadge } from "../components/StateBadge";
import { ErrorState, LoadingState, Notice } from "../components/Status";
import { describeError, isNotFound, isOutOfDate } from "../lib/errors";
import { formatDateTime, isProcessingState, isUuid, shortId } from "../lib/format";
import { useRequestDetail } from "../lib/useRequestDetail";
import { NotFoundPage } from "./NotFoundPage";

const ACTION_LABELS: Record<string, string> = { edit: "Edit the draft", approve: "Approve", reject: "Reject" };

const BLOCKER_TEXT: Record<string, string> = {
  self_approval_forbidden: "you submitted or edited this request",
  run_not_in_review: "the request is not waiting for review",
  run_state_not_editable: "the request cannot be edited in its current state",
  no_current_revision: "there is no revision yet",
  nothing_to_edit: "there is nothing to edit",
  revision_rejected: "the current revision was rejected",
  blocking_findings: "the draft has blocking findings",
  warnings_not_acknowledged: "warnings must be acknowledged",
  stale_revision: "the revision is not the current one",
  content_hash_mismatch: "the revision is not the current one",
};

type Flash = { kind: "success" | "error" | "info"; text: string };

function ProcessingPanel({ detail }: { detail: RequestDetail }) {
  const { processing, state } = detail;
  return (
    <section className="card" aria-labelledby="processing-title">
      <h2 id="processing-title">Processing</h2>
      {state === "queued" ? (
        <Notice kind="info">Queued. The request waits until a background worker picks it up (the worker must be running).</Notice>
      ) : null}
      {state === "extracting" ? <Notice kind="info">Extracting… this page refreshes automatically.</Notice> : null}
      {state === "failed" ? (
        <Notice kind="error">
          Processing failed{detail.failed_step ? ` at the “${detail.failed_step}” step` : ""}
          {processing?.failure_code ? ` (${processing.failure_code})` : ""}.
        </Notice>
      ) : null}
      {processing ? (
        <dl className="facts">
          <div>
            <dt>Step</dt>
            <dd>{processing.step}</dd>
          </div>
          <div>
            <dt>Task</dt>
            <dd>{processing.task_state}</dd>
          </div>
          <div>
            <dt>Attempts</dt>
            <dd>{processing.attempt_count}</dd>
          </div>
          <div>
            <dt>Failure code</dt>
            <dd>{processing.failure_code ?? "—"}</dd>
          </div>
        </dl>
      ) : (
        <p className="muted">No processing task is recorded for this request.</p>
      )}
    </section>
  );
}

/** What the server says this user may do, and why other actions are blocked. Informational: the controls are below. */
function ActionsPanel({ detail }: { detail: RequestDetail }) {
  const blockers = Object.entries(detail.action_blockers);
  return (
    <section className="card" aria-labelledby="actions-title">
      <h2 id="actions-title">Your available actions</h2>
      <p className="muted">Reported by the server for your account and the request as it is now.</p>
      {detail.allowed_actions.length === 0 ? (
        <p>None right now.</p>
      ) : (
        <ul>
          {detail.allowed_actions.map((action) => (
            <li key={action}>{ACTION_LABELS[action] ?? action}</li>
          ))}
        </ul>
      )}
      {blockers.length > 0 ? (
        <ul className="blockers">
          {blockers.map(([action, reason]) => (
            <li key={action}>
              {ACTION_LABELS[action] ?? action} is not possible now: {BLOCKER_TEXT[reason] ?? reason}.
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

interface BodyProps {
  detail: RequestDetail;
  me: string | null;
  canSeeAudit: boolean;
  flash: Flash | null;
  editing: boolean;
  auditVersion: number;
  onEdit: () => void;
  onEditorClose: (options?: { reload?: boolean }) => void;
  onSaved: (result: { revision_number: number; approval_invalidated: boolean }) => void;
  onApproved: () => void;
  onRejected: () => void;
  onActionError: (error: unknown) => void;
  onOutOfDate: () => void;
}

function DetailBody(props: BodyProps) {
  const { detail, me, canSeeAudit, flash, editing, auditVersion } = props;
  const revision = detail.current_revision;
  const allowed = new Set<string>(detail.allowed_actions);
  const canEdit = allowed.has("edit") && detail.draft !== null;

  return (
    <>
      <div className="page-head">
        <div>
          <p className="crumbs">
            <Link to="/requests">Requests</Link> / {detail.reference}
          </p>
          <h1>
            {detail.reference} <StateBadge state={detail.state} />
          </h1>
        </div>
      </div>

      {flash ? <Notice kind={flash.kind === "success" ? "info" : flash.kind}>{flash.text}</Notice> : null}

      <dl className="facts card">
        <div>
          <dt>Created</dt>
          <dd>{formatDateTime(detail.created_at)}</dd>
        </div>
        <div>
          <dt>Submitted by</dt>
          <dd title={detail.submitted_by}>{detail.submitted_by === me ? "You" : shortId(detail.submitted_by)}</dd>
        </div>
        <div>
          <dt>Extraction</dt>
          <dd>{detail.outcome_status ?? (isProcessingState(detail.state) ? "in progress" : "—")}</dd>
        </div>
        <div>
          <dt>Approvals / rejections</dt>
          <dd>
            {detail.approval_count} / {detail.rejection_count}
          </dd>
        </div>
      </dl>

      <ProcessingPanel detail={detail} />

      <section className="card" aria-labelledby="revision-title">
        <h2 id="revision-title">Current revision</h2>
        {revision ? (
          <dl className="facts">
            <div>
              <dt>Revision</dt>
              <dd>{revision.number}</dd>
            </div>
            <div>
              <dt>Saved</dt>
              <dd>{formatDateTime(revision.created_at)}</dd>
            </div>
            <div className="wide">
              <dt>Content hash (SHA-256)</dt>
              <dd>
                <code className="hash">{revision.content_hash}</code>
              </dd>
            </div>
          </dl>
        ) : detail.outcome_status === "invalid" ? (
          <Notice kind="warning">
            Extraction found blocking problems, so no revision was created.
            {canEdit ? " Use “Edit draft” below to correct the values; saving creates revision 1." : ""}
          </Notice>
        ) : (
          <p className="muted">No revision yet.</p>
        )}
      </section>

      <section className="card" aria-labelledby="draft-title">
        <div className="section-head">
          <h2 id="draft-title">Draft</h2>
          {canEdit && !editing ? (
            <button type="button" className="button" onClick={props.onEdit}>
              Edit draft
            </button>
          ) : null}
        </div>
        {detail.draft ? (
          editing ? (
            <DraftEditor detail={detail} draft={detail.draft} onSaved={props.onSaved} onClose={props.onEditorClose} onOutOfDate={props.onOutOfDate} />
          ) : (
            <>
              <p className="muted">
                {detail.draft_source === "revision" ? "From the current revision." : "From the extraction result (no revision exists)."}
              </p>
              <DraftView draft={detail.draft} />
            </>
          )
        ) : (
          <p className="muted">There is no draft yet.</p>
        )}
      </section>

      <section className="card" aria-labelledby="findings-title">
        <h2 id="findings-title">Findings</h2>
        <FindingsList findings={detail.findings} />
      </section>

      <ActionsPanel detail={detail} />

      {/* Keyed by the revision (id and hash): an acknowledgement or a reason chosen for one revision is never applied to another. */}
      {allowed.has("approve") ? (
        <ApprovePanel key={`approve:${revision?.id}:${revision?.content_hash}`} detail={detail} blocked={editing} onApproved={props.onApproved} onError={props.onActionError} />
      ) : null}
      {allowed.has("reject") ? (
        <RejectPanel key={`reject:${revision?.id}:${revision?.content_hash}`} detail={detail} blocked={editing} onRejected={props.onRejected} onError={props.onActionError} />
      ) : null}

      {canSeeAudit ? <AuditHistory runId={detail.run_id} version={auditVersion} /> : null}
    </>
  );
}

export function RequestDetailPage() {
  const { runId = "" } = useParams();
  if (!isUuid(runId)) return <NotFoundPage />;
  return <LoadedDetail key={runId} runId={runId} />;
}

function LoadedDetail({ runId }: { runId: string }) {
  const { state, reload } = useRequestDetail(runId);
  const { state: auth, can } = useAuth();
  const me = auth.status === "authenticated" ? auth.user.public_id : null;
  const [flash, setFlash] = useState<Flash | null>(null);
  const [editing, setEditing] = useState(false);
  const [auditVersion, setAuditVersion] = useState(0);

  // After any action the request and its audit history are loaded again from the server; nothing is updated locally.
  const refresh = () => {
    reload();
    setAuditVersion((v) => v + 1);
  };

  if (state.status === "loading") return <LoadingState label="Loading request…" />;
  if (state.status === "error") {
    if (isNotFound(state.error)) return <NotFoundPage />;
    return <ErrorState title="Could not load the request" message={describeError(state.error)} onRetry={reload} />;
  }
  const detail = state.data;
  const revisionNumber = detail.current_revision?.number;

  return (
    <section>
      {state.refreshError ? (
        <Notice kind="error">
          Could not refresh: {describeError(state.refreshError)}{" "}
          <button type="button" className="link-button" onClick={reload}>
            Try again
          </button>
        </Notice>
      ) : null}
      <DetailBody
        detail={detail}
        me={me}
        canSeeAudit={can(AUDIT_PERMISSION)}
        flash={flash}
        editing={editing}
        auditVersion={auditVersion}
        onEdit={() => {
          setFlash(null);
          setEditing(true);
        }}
        onEditorClose={(options) => {
          setEditing(false);
          if (options?.reload) refresh();
        }}
        onSaved={(result) => {
          setEditing(false);
          setFlash({
            kind: "success",
            text: `Saved as revision ${result.revision_number}.${result.approval_invalidated ? " The earlier approval no longer applies; the request is back in review." : ""}`,
          });
          refresh();
        }}
        onApproved={() => {
          setFlash({ kind: "success", text: `Revision ${revisionNumber ?? ""} approved.`.replace("  ", " ") });
          refresh();
        }}
        onRejected={() => {
          setFlash({ kind: "success", text: `Revision ${revisionNumber ?? ""} rejected. It can no longer be approved; edit the draft to create a new revision.`.replace("  ", " ") });
          refresh();
        }}
        onActionError={(error) => {
          setFlash({ kind: "error", text: describeError(error) });
          if (isOutOfDate(error)) refresh();
        }}
        onOutOfDate={refresh}
      />
    </section>
  );
}
