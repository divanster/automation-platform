import { useCallback, useEffect, useState } from "react";

import { ApiError, UNEXPECTED_RESPONSE } from "../api/http";
import { getRequest } from "../api/requests";
import type { RequestDetail } from "../api/types";
import { isProcessingState } from "./format";

export type DetailState =
  | { status: "loading" }
  | { status: "ready"; data: RequestDetail; refreshError: ApiError | null }
  | { status: "error"; error: ApiError };

export const FAST_POLL_MS = 2000;
export const SLOW_POLL_MS = 10000;
export const FAST_POLL_COUNT = 30;

function pollDelay(polls: number): number {
  return polls <= FAST_POLL_COUNT ? FAST_POLL_MS : SLOW_POLL_MS;
}

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/**
 * One request's detail, refreshed while the system is still working on it.
 *
 * Polling runs only while the run is in a processing state (queued, extracting, exporting). It stops when the state
 * becomes stable, when a refresh fails (the last good data stays on screen with the error, and `reload` resumes), and
 * when the component unmounts or `runId` changes (the in-flight request is aborted). A hidden tab skips its fetches.
 */
export function useRequestDetail(runId: string) {
  const [state, setState] = useState<DetailState>({ status: "loading" });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;

    setState((previous) =>
      previous.status === "ready" && previous.data.run_id === runId ? { ...previous, refreshError: null } : { status: "loading" },
    );

    const refresh = async (): Promise<void> => {
      if (document.hidden && polls > 0) {
        timer = setTimeout(refresh, pollDelay(polls));
        return;
      }
      try {
        const data = await getRequest(runId, controller.signal);
        if (controller.signal.aborted) return;
        setState({ status: "ready", data, refreshError: null });
        if (isProcessingState(data.state)) {
          polls += 1;
          timer = setTimeout(refresh, pollDelay(polls));
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        const apiError = asApiError(error);
        setState((previous) =>
          previous.status === "ready" && previous.data.run_id === runId
            ? { ...previous, refreshError: apiError }
            : { status: "error", error: apiError },
        );
      }
    };

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [runId, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { state, reload };
}
