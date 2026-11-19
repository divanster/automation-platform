import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, UNEXPECTED_RESPONSE } from "../api/http";
import { useAuth } from "../auth/AuthContext";

export type LiveState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: ApiError };

export interface LiveOptions<T> {
  /**
   * Milliseconds until the next refresh, or null to stop refreshing. `polls` is the number of completed refreshes since
   * the inputs last changed (1 after the first answer).
   */
  interval: (data: T, polls: number) => number | null;
}

export interface Live<T> {
  state: LiveState<T>;
  /** A request is in flight. While `state` is ready this is a background refresh: the data stays on screen. */
  refreshing: boolean;
  /** The last background refresh failed. The last good data stays in `state`; refreshing is retried with a back-off. */
  refreshError: ApiError | null;
  lastUpdated: Date | null;
  /** Refresh now. The request in flight (if any) is aborted and its answer ignored. */
  reload(): void;
}

const RETRY_FALLBACK_MS = 5000;
const RETRY_MAX_MS = 30000;

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/** Only failures that may go away by themselves are retried: no connection, throttling, server errors, a garbled answer. */
function isRetryable(error: ApiError): boolean {
  return error.status === 0 || error.status === 429 || error.status >= 500 || error.code === UNEXPECTED_RESPONSE;
}

function isHidden(): boolean {
  return document.visibilityState === "hidden";
}

/**
 * Load a resource and keep it fresh.
 *
 * - One request at a time per hook: the next refresh is scheduled only after the previous one has finished.
 * - `deps` identify the resource. When they change the old data is dropped (state "loading"), the request in flight is
 *   aborted, and an answer that arrives late is ignored. A `reload()` refreshes in place and keeps the data on screen.
 * - Nothing is requested unless the user is signed in. Logout, sign-in expiry and unmount abort the request in flight and
 *   cancel the timer.
 * - While the tab is hidden no timer runs; when it becomes visible again the resource is refreshed immediately.
 * - A failed refresh keeps the last good data, sets `refreshError`, and retries with a growing delay (up to 30 s) when the
 *   failure may be temporary. A failure with no data yet is shown as state "error" and not retried (use `reload`).
 */
export function useLiveResource<T>(load: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[], options: LiveOptions<T>): Live<T> {
  const { state: auth } = useAuth();
  const authenticated = auth.status === "authenticated";

  const [state, setState] = useState<LiveState<T>>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<ApiError | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [version, setVersion] = useState(0);

  // Callers pass inline functions; the effect always uses the latest ones without restarting.
  const loadRef = useRef(load);
  const intervalRef = useRef(options.interval);
  loadRef.current = load;
  intervalRef.current = options.interval;
  const dataKey = useRef<string | null>(null);

  const key = deps.map(String).join("\u0000");

  useEffect(() => {
    if (!authenticated) {
      setRefreshing(false);
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let polls = 0;
    let failures = 0;
    let lastDelay = RETRY_FALLBACK_MS;
    let hasData = dataKey.current === key;

    if (!hasData) {
      setState({ status: "loading" });
      setRefreshError(null);
      setLastUpdated(null);
    }

    const clear = () => {
      clearTimeout(timer);
      timer = undefined;
    };

    const schedule = (delay: number) => {
      clear();
      if (isHidden()) return; // the visibilitychange handler restarts it
      timer = setTimeout(() => void run(), delay);
    };

    const run = async (): Promise<void> => {
      timer = undefined;
      if (signal.aborted || inFlight) return;
      inFlight = true;
      setRefreshing(true);
      let next: number | null = null;
      try {
        const data = await loadRef.current(signal);
        if (signal.aborted) return;
        polls += 1;
        failures = 0;
        hasData = true;
        dataKey.current = key;
        setState({ status: "ready", data });
        setRefreshError(null);
        setLastUpdated(new Date());
        next = intervalRef.current(data, polls);
        if (next !== null) lastDelay = next;
      } catch (error) {
        if (signal.aborted) return;
        const apiError = asApiError(error);
        if (hasData) {
          setRefreshError(apiError);
          if (isRetryable(apiError)) {
            failures += 1;
            next = Math.min(lastDelay * 2 ** failures, RETRY_MAX_MS);
          }
        } else {
          setState({ status: "error", error: apiError });
        }
      } finally {
        inFlight = false;
        if (!signal.aborted) setRefreshing(false);
      }
      if (!signal.aborted && next !== null) schedule(next);
    };

    const onVisibilityChange = () => {
      if (isHidden()) {
        clear();
      } else if (!inFlight) {
        clear();
        void run();
      }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    void run();
    return () => {
      controller.abort();
      clear();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [key, version, authenticated]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { state, refreshing, refreshError, lastUpdated, reload };
}
