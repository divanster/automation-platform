import { Link, useParams } from "react-router";

import type { RequestDetail } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { DraftView } from "../components/DraftView";
import { FindingsList } from "../components/FindingsList";
import { StateBadge } from "../components/StateBadge";
import { ErrorState, LoadingState, Notice } from "../components/Status";
import { describeError, isNotFound } from "../lib/errors";
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
};

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

function ActionsPanel({ detail }: { detail: RequestDetail }) {
  const blockers = Object.entries(detail.action_blockers);
  return (
    <section className="card" aria-labelledby="actions-title">
      <h2 id="actions-title">Your available actions</h2>
      <p className="muted">
        Reported by the server for your account. Editing, approving and rejecting are not part of this screen yet.
      </p>
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

function DetailBody({ detail, me }: { detail: RequestDetail; me: string | null }) {
  const revision = detail.current_revision;
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
            Extraction found blocking problems, so no revision was created. Fixing the draft is not part of this screen yet.
          </Notice>
        ) : (
          <p className="muted">No revision yet.</p>
        )}
      </section>

      <section className="card" aria-labelledby="draft-title">
        <h2 id="draft-title">Draft</h2>
        {detail.draft ? (
          <>
            <p className="muted">
              {detail.draft_source === "revision" ? "From the current revision." : "From the extraction result (no revision exists)."}
            </p>
            <DraftView draft={detail.draft} />
          </>
        ) : (
          <p className="muted">There is no draft yet.</p>
        )}
      </section>

      <section className="card" aria-labelledby="findings-title">
        <h2 id="findings-title">Findings</h2>
        <FindingsList findings={detail.findings} />
      </section>

      <ActionsPanel detail={detail} />
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
  const { state: auth } = useAuth();
  const me = auth.status === "authenticated" ? auth.user.public_id : null;

  if (state.status === "loading") return <LoadingState label="Loading request…" />;
  if (state.status === "error") {
    if (isNotFound(state.error)) return <NotFoundPage />;
    return <ErrorState title="Could not load the request" message={describeError(state.error)} onRetry={reload} />;
  }
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
      <DetailBody detail={state.data} me={me} />
    </section>
  );
}
