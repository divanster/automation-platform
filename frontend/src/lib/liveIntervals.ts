import { isProcessingState } from "./format";

// How often each screen refreshes in the background. Provisional values, chosen for a single local worker and a handful of
// users; every screen also refreshes at once when the tab becomes visible again and on a manual refresh.

/** The requests list. */
export const LIST_POLL_MS = 5000;

/** The dashboard: one summary request per refresh (see api/dashboard.ts); it still refreshes less often than the list. */
export const DASHBOARD_POLL_MS = 10000;

/** A request the system is still working on (queued, extracting, exporting). */
export const ACTIVE_POLL_MS = 3000;
/** After this many refreshes of a request that is still processing (about three minutes), refresh less often. */
export const ACTIVE_FAST_POLLS = 60;
export const ACTIVE_SLOW_POLL_MS = 10000;
/** Waiting for a person (needs review): someone else may edit, approve or reject. */
export const REVIEW_POLL_MS = 10000;
/** Nothing is expected to happen any more (approved, exported, failed, cancelled). */
export const TERMINAL_POLL_MS = 30000;

export function detailPollDelay(state: string, polls: number): number {
  if (isProcessingState(state)) return polls <= ACTIVE_FAST_POLLS ? ACTIVE_POLL_MS : ACTIVE_SLOW_POLL_MS;
  return state === "needs_review" ? REVIEW_POLL_MS : TERMINAL_POLL_MS;
}
