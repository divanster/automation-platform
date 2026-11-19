import type { ApiError } from "../api/http";
import { describeError } from "../lib/errors";
import { formatTime } from "../lib/format";

/**
 * The quiet "live" strip of a screen: a spinner while a background refresh runs, when the data was last refreshed, and a
 * non-blocking note when the last refresh failed (the data on screen is then the last good copy).
 */
export function LiveStatus({
  refreshing,
  lastUpdated,
  error,
  onRetry,
}: {
  refreshing: boolean;
  lastUpdated: Date | null;
  error: ApiError | null;
  onRetry: () => void;
}) {
  return (
    <div className="live-status">
      {refreshing ? (
        <span className="live-refreshing" data-testid="live-refreshing">
          <span className="spinner" aria-hidden="true" />
          Refreshing…
        </span>
      ) : null}
      {lastUpdated ? (
        <span className="muted">
          Updated <time dateTime={lastUpdated.toISOString()}>{formatTime(lastUpdated)}</time>
        </span>
      ) : null}
      {error ? (
        <span className="live-error" role="status">
          Could not refresh: {describeError(error)}{" "}
          <button type="button" className="link-button" onClick={onRetry}>
            Try again
          </button>
        </span>
      ) : null}
    </div>
  );
}
