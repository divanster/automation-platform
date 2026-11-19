// The dashboard's data. The API has no statistics endpoint, so every number comes from `GET /api/requests/`, whose `count`
// is the total of ALL requests the user may view that match the optional `state` filter, whatever the page size. One
// request per state (page_size 1) therefore gives an exact count per state; nothing is ever added up from rows on a page.

import { listRequests } from "../api/requests";
import { RUN_STATES, type RequestListItem, type RunState } from "../api/types";
import { isProcessingState } from "./format";

export const RECENT_LIMIT = 5;

/** A count the server reported, or the fact that it could not be obtained. Unavailable is never the same as zero. */
export type Count = { available: true; value: number } | { available: false };

export const UNAVAILABLE: Count = { available: false };

export interface DashboardSnapshot {
  /** Count of every request the user may view (the unfiltered list). */
  total: Count;
  byState: Record<RunState, Count>;
  /** The newest requests, or null when that request failed. */
  recent: RequestListItem[] | null;
}

function toCount(result: PromiseSettledResult<{ count: number }>): Count {
  if (result.status !== "fulfilled") return UNAVAILABLE;
  const { count } = result.value;
  return Number.isInteger(count) && count >= 0 ? { available: true, value: count } : UNAVAILABLE;
}

/**
 * One refresh: the newest requests plus one count request per run state, all started together. Each answer stands on its
 * own, so one failing request makes only its own figures unavailable. When every request fails the first error is thrown,
 * so the screen can show a real error (or keep the last good data during a background refresh).
 */
export async function loadDashboard(signal: AbortSignal): Promise<DashboardSnapshot> {
  const [recent, ...perState] = await Promise.allSettled([
    listRequests({ page: 1, page_size: RECENT_LIMIT }, signal),
    ...RUN_STATES.map((state) => listRequests({ page: 1, page_size: 1, state }, signal)),
  ]);
  const all = [recent, ...perState];
  const firstFailure = all.find((r): r is PromiseRejectedResult => r.status === "rejected");
  if (firstFailure && all.every((r) => r.status === "rejected")) throw firstFailure.reason;

  const byState = Object.fromEntries(RUN_STATES.map((state, i) => [state, toCount(perState[i]!)])) as Record<RunState, Count>;
  return {
    total: toCount(recent),
    byState,
    recent: recent.status === "fulfilled" ? recent.value.results : null,
  };
}

export type MetricId = "total" | "processing" | "needs_review" | "approved" | "failed";

/** Sum of several states; unavailable as soon as one of them is, because a partial sum would be a wrong number. */
export function sumStates(snapshot: DashboardSnapshot, states: readonly RunState[]): Count {
  let value = 0;
  for (const state of states) {
    const count = snapshot.byState[state];
    if (!count.available) return UNAVAILABLE;
    value += count.value;
  }
  return { available: true, value };
}

export const PROCESSING_STATES = RUN_STATES.filter(isProcessingState);

export function metricCount(snapshot: DashboardSnapshot, id: MetricId): Count {
  switch (id) {
    case "total":
      return snapshot.total;
    case "processing":
      return sumStates(snapshot, PROCESSING_STATES);
    default:
      return snapshot.byState[id];
  }
}

/**
 * The counts are read by separate requests a moment apart, so a request that changes state in between can make the
 * per-state figures add up to something other than the total. True when that happened (only checkable when all are known).
 */
export function countsDisagree(snapshot: DashboardSnapshot): boolean {
  const sum = sumStates(snapshot, RUN_STATES);
  return snapshot.total.available && sum.available && snapshot.total.value !== sum.value;
}
