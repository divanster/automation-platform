import { useState } from "react";

import { listAudit } from "../api/review";
import type { AuditDetailValue, AuditEvent } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { describeError, isPermissionError } from "../lib/errors";
import { formatDateTime, shortId } from "../lib/format";
import { useResource } from "../lib/useResource";
import { Pagination } from "./Pagination";
import { EmptyState, ErrorState, LoadingState } from "./Status";

// The audit events of ONE request (GET /api/requests/{id}/audit/, oldest first). The API returns identifiers, state names,
// counts, hashes and reason codes only, and the actor as a public id (`actor_public_id`) or "system"; it returns no names.
// Nothing is invented here: an event is shown with what the server sent, and unknown actions and detail keys are shown as
// the raw codes.

export const AUDIT_PERMISSION = "audit.view_auditevent";
const PAGE_SIZE = 20;

const ACTION_LABELS: Record<string, string> = {
  "request.submitted": "Request submitted",
  "revision.created": "Revision created",
  "request.edited": "Draft edited",
  "run.state_changed": "State changed",
  "approval.recorded": "Approved",
  "approval.invalidated": "Approval invalidated by an edit",
  "rejection.recorded": "Rejected",
};

const DETAIL_LABELS: Record<string, string> = {
  revision_number: "Revision",
  revision_id: "Revision id",
  base_revision_id: "Based on revision id",
  by_revision_id: "Replaced by revision id",
  content_hash: "Content hash",
  origin: "Origin",
  blocking_count: "Blocking findings",
  warning_count: "Warnings",
  first_edit: "First edit",
  from_state: "From state",
  to_state: "To state",
  cause: "Cause",
  reason_code: "Reason",
  approval_count: "Approvals invalidated",
  byte_length: "Text size (bytes)",
  client_request_id_used: "Client request id used",
};

// Shown first, when present: the revision the event refers to.
const REVISION_KEYS = ["revision_number", "revision_id"];

function formatValue(key: string, value: AuditDetailValue): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (key === "content_hash" || key.endsWith("revision_id")) return typeof value === "string" ? shortId(value) : String(value);
  return String(value);
}

function Details({ details }: { details: Record<string, AuditDetailValue> }) {
  const keys = Object.keys(details).sort((a, b) => {
    const ai = REVISION_KEYS.indexOf(a);
    const bi = REVISION_KEYS.indexOf(b);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return a.localeCompare(b);
  });
  if (keys.length === 0) return <span className="muted">—</span>;
  return (
    <dl className="audit-details">
      {keys.map((key) => (
        <div key={key}>
          <dt>{DETAIL_LABELS[key] ?? key}</dt>
          <dd title={typeof details[key] === "string" ? String(details[key]) : undefined}>{formatValue(key, details[key] ?? null)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Actor({ event, me }: { event: AuditEvent; me: string | null }) {
  if (event.actor_kind === "system") return <>System</>;
  if (event.actor_public_id === null) return <span className="muted">unknown</span>;
  return <span title={event.actor_public_id}>{event.actor_public_id === me ? "You" : shortId(event.actor_public_id)}</span>;
}

export function AuditHistory({ runId, version }: { runId: string; /** Bumped by the page after an action, to load the new events. */ version: number }) {
  const { state: auth } = useAuth();
  const me = auth.status === "authenticated" ? auth.user.public_id : null;
  const [page, setPage] = useState(1);
  const { state, reload } = useResource((signal) => listAudit(runId, page, PAGE_SIZE, signal), [runId, page, version]);

  return (
    <section className="card" aria-labelledby="audit-title">
      <h2 id="audit-title">Audit history</h2>
      {state.status === "loading" ? <LoadingState label="Loading audit history…" /> : null}
      {state.status === "error" ? (
        <ErrorState
          title={isPermissionError(state.error) ? "Not available for your account" : "Could not load the audit history"}
          message={describeError(state.error)}
          onRetry={isPermissionError(state.error) ? undefined : reload}
        />
      ) : null}
      {state.status === "ready" ? (
        state.data.results.length === 0 ? (
          <EmptyState>No audit events are recorded for this request.</EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Action</th>
                    <th scope="col">By</th>
                    <th scope="col">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.results.map((event) => (
                    <tr key={event.id}>
                      <td>{formatDateTime(event.occurred_at)}</td>
                      <td>
                        {ACTION_LABELS[event.action] ?? event.action}
                        <div className="muted small">{event.subject_type}</div>
                      </td>
                      <td>
                        <Actor event={event} me={me} />
                      </td>
                      <td>
                        <Details details={event.details} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={state.data.page} pageSize={state.data.page_size} count={state.data.count} noun="event" onPage={setPage} />
          </>
        )
      ) : null}
    </section>
  );
}
