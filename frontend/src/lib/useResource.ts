import { useCallback, useEffect, useState } from "react";

import { ApiError, UNEXPECTED_RESPONSE } from "../api/http";

export type ResourceState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: ApiError };

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/**
 * Load a resource when `deps` change. An answer that arrives after the inputs changed or the component unmounted is
 * discarded (the request is aborted and its result ignored), so a slow response can never overwrite a newer one.
 */
export function useResource<T>(load: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[]) {
  const [state, setState] = useState<ResourceState<T>>({ status: "loading" });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ status: "ready", data });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setState({ status: "error", error: asApiError(error) });
      },
    );
    return () => controller.abort();
    // `load` is intentionally not a dependency: callers pass an inline function and list the inputs in `deps`.
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { state, reload };
}
