// Dashboard summary endpoint (backend/apps/api/views_dashboard.py): every figure and the newest requests in one response.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import { RUN_STATES, type DashboardSummary, type RequestListItem } from "./types";

const SCALAR_COUNTS = ["total", "processing", "needs_review", "approved", "failed"] as const;

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/** Only what the dashboard renders is checked; a row it could not render is refused instead of crashing the screen. */
function isRecentItem(value: unknown): value is RequestListItem {
  return (
    isObject(value) &&
    typeof value.run_id === "string" &&
    typeof value.reference === "string" &&
    typeof value.created_at === "string" &&
    typeof value.failed_step === "string" &&
    typeof value.state === "string" &&
    (RUN_STATES as readonly string[]).includes(value.state)
  );
}

/**
 * One request, and the figures are the server's: nothing is added up or derived here. A body that does not have the exact
 * documented shape (a missing or negative count, a state that is missing from `by_state` or unknown to the client) is an
 * unexpected response, never a zero.
 */
export async function getDashboardSummary(signal?: AbortSignal): Promise<DashboardSummary> {
  const body = await request<unknown>("/api/dashboard/summary/", { signal });
  if (!isObject(body) || !SCALAR_COUNTS.every((key) => isCount(body[key]))) throw unexpected();
  const byState = body.by_state;
  if (!isObject(byState) || Object.keys(byState).length !== RUN_STATES.length || !RUN_STATES.every((state) => isCount(byState[state]))) {
    throw unexpected();
  }
  if (!Array.isArray(body.recent_requests) || !body.recent_requests.every(isRecentItem)) throw unexpected();
  return body as unknown as DashboardSummary;
}
