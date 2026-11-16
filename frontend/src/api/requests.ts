// Request endpoints (backend/apps/api/views_requests.py): list, submit, detail.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import type { ListQuery, Page, RequestDetail, RequestListItem, SubmitResponse } from "./types";

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function listRequests(query: ListQuery, signal?: AbortSignal): Promise<Page<RequestListItem>> {
  const body = await request<unknown>("/api/requests/", { query: { ...query }, signal });
  if (!isObject(body) || !Array.isArray(body.results) || typeof body.count !== "number") throw unexpected();
  return body as unknown as Page<RequestListItem>;
}

export async function submitRequest(text: string, clientRequestId: string, signal?: AbortSignal): Promise<SubmitResponse> {
  const body = await request<unknown>("/api/requests/", {
    method: "POST",
    body: { text, client_request_id: clientRequestId },
    signal,
  });
  if (!isObject(body) || typeof body.run_id !== "string") throw unexpected();
  return body as unknown as SubmitResponse;
}

/**
 * DECIMALS: the API sends quantities as text. If one ever arrives as a JSON number, JSON.parse has already rounded
 * it, so it is refused instead of being shown as if it were exact.
 */
function quantitiesAreText(draft: unknown): boolean {
  if (draft === null) return true;
  if (!isObject(draft) || !Array.isArray(draft.items)) return false;
  return draft.items.every((item) => isObject(item) && (item.quantity === null || typeof item.quantity === "string"));
}

export async function getRequest(runId: string, signal?: AbortSignal): Promise<RequestDetail> {
  const body = await request<unknown>(`/api/requests/${encodeURIComponent(runId)}/`, { signal });
  if (
    !isObject(body) ||
    typeof body.run_id !== "string" ||
    typeof body.state !== "string" ||
    !Array.isArray(body.findings) ||
    !Array.isArray(body.allowed_actions) ||
    !quantitiesAreText(body.draft)
  ) {
    throw unexpected();
  }
  return body as unknown as RequestDetail;
}
