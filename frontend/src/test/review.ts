// TEST ONLY: synthetic fixtures and a small stateful fake server for the review screens.

import type { AuditEvent, Page, RequestDetail, WarningPair } from "../api/types";
import { apiError, json, type Handler, type RecordedCall } from "./fakeApi";
import { HASH, RUN_ID, USER_ID, detail, operator } from "./fixtures";

export const REV1 = "44444444-4444-4444-8444-444444444444";
export const REV2 = "55555555-5555-4555-8555-555555555555";
export const HASH2 = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
export const APPROVER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

export const warning = (code = "value_not_in_source", field_path = "items[0].description"): WarningPair => ({ code, field_path });

export function withWarnings(...pairs: WarningPair[]): Partial<RequestDetail> {
  return {
    warnings: pairs,
    findings: pairs.map((p) => ({ ...p, blocking: false, message: `Synthetic explanation for ${p.code}.`, excerpt: `excerpt of ${p.field_path}` })),
  };
}

/** A request in review that the signed-in user may edit, approve and reject (the server's view of it). */
export function inReview(overrides: Partial<RequestDetail> = {}): RequestDetail {
  return detail({ allowed_actions: ["edit", "approve", "reject"], ...overrides });
}

export function approver(overrides = {}) {
  return operator({
    public_id: APPROVER_ID,
    username: "synthetic.approver",
    roles: ["Approver"],
    permissions: ["intake.view_any_request", "intake.approve_request", "intake.reject_request"],
    ...overrides,
  });
}

export function auditor(overrides = {}) {
  return operator({
    public_id: APPROVER_ID,
    username: "synthetic.auditor",
    roles: ["Auditor"],
    permissions: ["intake.view_any_request", "audit.view_auditevent"],
    ...overrides,
  });
}

export function auditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    occurred_at: "2026-01-01T10:00:00Z",
    actor_kind: "user",
    actor_public_id: USER_ID,
    action: "request.submitted",
    subject_type: "run",
    subject_id: RUN_ID,
    details: { byte_length: 120, client_request_id_used: true },
    ...overrides,
  };
}

export function auditPage(events: AuditEvent[], overrides: Partial<Page<AuditEvent>> = {}): Page<AuditEvent> {
  return { count: events.length, page: 1, page_size: 20, results: events, ...overrides };
}

export interface FakeServer {
  handler: Handler;
  /** Replace what `GET /api/requests/{id}/` returns from now on. */
  setDetail: (next: RequestDetail) => void;
  detailGets: () => number;
}

/**
 * Serves the detail of RUN_ID (changeable) and the audit list, and passes every other call to `extra`. Everything the
 * app does after an action is a fresh GET, so tests change the detail to what the server would say next.
 */
export function fakeServer(initial: RequestDetail, extra: Handler = () => apiError(404, "not_found"), audit: Page<AuditEvent> = auditPage([])): FakeServer {
  let current = initial;
  let gets = 0;
  const handler: Handler = (call: RecordedCall) => {
    if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) {
      gets += 1;
      return json(200, current);
    }
    if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/audit/`) return json(200, audit);
    return extra(call);
  };
  return { handler, setDetail: (next) => (current = next), detailGets: () => gets };
}

export { HASH };
