// Review endpoints (backend/apps/api/views_requests.py): edit, approve, reject and the per-request audit history.
// Each function sends exactly the fields the serializers accept (unknown fields are a 400) and nothing else.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import type { ApproveResponse, AuditEvent, Draft, EditResponse, Page, RejectResponse, RejectionReason, WarningPair } from "./types";

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const base = (runId: string) => `/api/requests/${encodeURIComponent(runId)}`;

/**
 * Save `draft` as the next revision. `baseRevisionId` is the revision the draft was edited from, or null for the first
 * edit of a run whose extraction was invalid (no revision exists yet). The server refuses a base that is not the current
 * revision with 409 `stale_base_revision`.
 */
export async function editRequest(runId: string, baseRevisionId: string | null, draft: Draft, signal?: AbortSignal): Promise<EditResponse> {
  const body = await request<unknown>(`${base(runId)}/edit/`, {
    method: "POST",
    body: { base_revision_id: baseRevisionId, draft },
    signal,
  });
  if (!isObject(body) || typeof body.revision_id !== "string" || typeof body.revision_number !== "number") throw unexpected();
  return body as unknown as EditResponse;
}

export async function approveRequest(
  runId: string,
  revisionId: string,
  contentHash: string,
  acknowledgedWarnings: WarningPair[],
  signal?: AbortSignal,
): Promise<ApproveResponse> {
  const body = await request<unknown>(`${base(runId)}/approve/`, {
    method: "POST",
    body: {
      revision_id: revisionId,
      content_hash: contentHash,
      acknowledged_warnings: acknowledgedWarnings.map((w) => ({ code: w.code, field_path: w.field_path })),
    },
    signal,
  });
  if (!isObject(body) || typeof body.approval_id !== "string") throw unexpected();
  return body as unknown as ApproveResponse;
}

export async function rejectRequest(
  runId: string,
  revisionId: string,
  contentHash: string,
  reasonCode: RejectionReason,
  signal?: AbortSignal,
): Promise<RejectResponse> {
  const body = await request<unknown>(`${base(runId)}/reject/`, {
    method: "POST",
    body: { revision_id: revisionId, content_hash: contentHash, reason_code: reasonCode },
    signal,
  });
  if (!isObject(body) || typeof body.rejection_id !== "string") throw unexpected();
  return body as unknown as RejectResponse;
}

/** One page of the run's audit events, oldest first. Needs `audit.view_auditevent` (the server answers 403 otherwise). */
export async function listAudit(runId: string, page: number, pageSize: number, signal?: AbortSignal): Promise<Page<AuditEvent>> {
  const body = await request<unknown>(`${base(runId)}/audit/`, { query: { page, page_size: pageSize }, signal });
  if (!isObject(body) || !Array.isArray(body.results) || typeof body.count !== "number") throw unexpected();
  return body as unknown as Page<AuditEvent>;
}
