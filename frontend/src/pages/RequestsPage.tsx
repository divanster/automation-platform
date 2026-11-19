import { useMemo } from "react";
import { Link, useSearchParams } from "react-router";

import { listRequests } from "../api/requests";
import { RUN_STATES, type RunState } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StateBadge } from "../components/StateBadge";
import { LiveStatus } from "../components/LiveStatus";
import { EmptyState, ErrorState, LoadingState } from "../components/Status";
import { Icon } from "../components/ui/Icon";
import { describeError, isPermissionError } from "../lib/errors";
import { formatDateTime, isRunState, shortId, stateLabel } from "../lib/format";
import { LIST_POLL_MS } from "../lib/liveIntervals";
import { useLiveResource } from "../lib/useLiveResource";
import { useStateTransitionToasts } from "../lib/useStateTransitionToasts";

const PAGE_SIZE = 20;

// Shortcuts for the states people look for most. They set the same `state` parameter as the select below.
const QUICK_FILTERS: { label: string; state: RunState | undefined }[] = [
  { label: "All", state: undefined },
  { label: "Needs review", state: "needs_review" },
  { label: "Approved", state: "approved" },
  { label: "Failed", state: "failed" },
];

/** URL parameters are untrusted input: anything that is not a valid page or a known state is ignored, not sent. */
function readParams(params: URLSearchParams): { page: number; state: RunState | undefined } {
  const rawPage = Number(params.get("page"));
  const page = Number.isInteger(rawPage) && rawPage >= 1 && rawPage <= 1_000_000 ? rawPage : 1;
  const rawState = params.get("state");
  return { page, state: isRunState(rawState) ? rawState : undefined };
}

export function RequestsPage() {
  const [params, setParams] = useSearchParams();
  const { page, state } = readParams(params);
  const { state: auth, can } = useAuth();
  const me = auth.status === "authenticated" ? auth.user.public_id : null;

  // The page and filter come from the URL; changing either loads the new view, a background refresh keeps the rows on screen.
  const { state: result, reload, refreshing, refreshError, lastUpdated } = useLiveResource(
    (signal) => listRequests({ page, page_size: PAGE_SIZE, state }, signal),
    [page, state],
    { interval: () => LIST_POLL_MS },
  );
  const tracked = useMemo(
    () => (result.status === "ready" ? result.data.results.map((r) => ({ runId: r.run_id, reference: r.reference, state: r.state })) : null),
    [result],
  );
  useStateTransitionToasts(tracked);

  function update(next: { page?: number; state?: RunState | "" }) {
    const merged = new URLSearchParams();
    const nextState = next.state === undefined ? state : next.state || undefined;
    const nextPage = next.page ?? page;
    if (nextState) merged.set("state", nextState);
    if (nextPage > 1) merged.set("page", String(nextPage));
    setParams(merged);
  }

  return (
    <section>
      <PageHeader
        title="Requests"
        description="Submitted requests and where each one stands."
        actions={
          can("intake.submit_request") ? (
            <Link className="button button-primary" to="/requests/new">
              <Icon name="plus" />
              New request
            </Link>
          ) : null
        }
      />

      <div className="toolbar">
        <label>
          State
          <select value={state ?? ""} onChange={(e) => update({ state: e.target.value as RunState | "", page: 1 })}>
            <option value="">All states</option>
            {RUN_STATES.map((s) => (
              <option key={s} value={s}>
                {stateLabel(s)}
              </option>
            ))}
          </select>
        </label>
        <div className="chip-group" role="group" aria-label="Quick filters">
          {QUICK_FILTERS.map((chip) => (
            <button
              key={chip.label}
              type="button"
              className="chip"
              aria-pressed={chip.state === state}
              onClick={() => update({ state: chip.state ?? "", page: 1 })}
            >
              {chip.label}
            </button>
          ))}
        </div>
        <div className="toolbar-end live-toolbar">
          {result.status === "ready" ? <LiveStatus refreshing={refreshing} lastUpdated={lastUpdated} error={refreshError} onRetry={reload} /> : null}
          <button type="button" className="button" onClick={reload}>
            <Icon name="refresh" />
            Refresh
          </button>
        </div>
      </div>

      {result.status === "loading" ? <LoadingState label="Loading requests…" skeleton={5} /> : null}
      {result.status === "error" ? (
        <ErrorState
          title={isPermissionError(result.error) ? "Not available for your account" : "Could not load requests"}
          message={describeError(result.error)}
          onRetry={isPermissionError(result.error) ? undefined : reload}
        />
      ) : null}
      {result.status === "ready" ? (
        result.data.results.length === 0 ? (
          state || page > 1 ? (
            <EmptyState icon="inbox" title="Nothing to show">
              No requests match.{" "}
              <button type="button" className="link-button" onClick={() => setParams(new URLSearchParams())}>
                Clear filter
              </button>
            </EmptyState>
          ) : (
            <EmptyState
              icon="inbox"
              title="Nothing here yet"
              action={
                can("intake.submit_request") ? (
                  <Link className="button button-primary" to="/requests/new">
                    <Icon name="plus" />
                    New request
                  </Link>
                ) : undefined
              }
            >
              There are no requests yet.
            </EmptyState>
          )
        ) : (
          <>
            <div className="table-wrap">
              <table className="requests-table">
                <caption className="sr-only">Requests</caption>
                <thead>
                  <tr>
                    <th scope="col">Reference</th>
                    <th scope="col">State</th>
                    <th scope="col" className="num">Revision</th>
                    <th scope="col" className="col-optional">
                      Extraction
                    </th>
                    <th scope="col" className="col-optional">
                      Submitted by
                    </th>
                    <th scope="col" className="col-date">Created</th>
                    <th scope="col">
                      <span className="sr-only">Action</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.data.results.map((item) => (
                    <tr key={item.run_id}>
                      <td>
                        <Link className="ref-link" to={`/requests/${item.run_id}`}>{item.reference}</Link>
                      </td>
                      <td>
                        <StateBadge state={item.state} />
                        {item.failed_step ? <span className="muted"> at {item.failed_step}</span> : null}
                      </td>
                      <td className="num">{item.current_revision_number ?? <span className="muted">—</span>}</td>
                      <td className="col-optional">{item.outcome_status ?? <span className="muted">—</span>}</td>
                      <td className="col-optional" title={item.submitted_by}>
                        {item.submitted_by === me ? "You" : shortId(item.submitted_by)}
                      </td>
                      <td className="col-date">{formatDateTime(item.created_at)}</td>
                      <td className="table-actions">
                        <Link
                          className={item.state === "needs_review" ? "button button-small button-primary" : "button button-small"}
                          to={`/requests/${item.run_id}`}
                          aria-label={`${item.state === "needs_review" ? "Review" : "View"} ${item.reference}`}
                        >
                          {item.state === "needs_review" ? "Review" : "View"}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={result.data.page} pageSize={result.data.page_size} count={result.data.count} onPage={(p) => update({ page: p })} />
          </>
        )
      ) : null}
    </section>
  );
}
