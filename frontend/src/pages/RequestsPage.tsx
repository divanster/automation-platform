import { Link, useSearchParams } from "react-router";

import { listRequests } from "../api/requests";
import { RUN_STATES, type RunState } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Pagination } from "../components/Pagination";
import { StateBadge } from "../components/StateBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/Status";
import { describeError, isPermissionError } from "../lib/errors";
import { formatDateTime, isRunState, shortId, stateLabel } from "../lib/format";
import { useResource } from "../lib/useResource";

const PAGE_SIZE = 20;

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

  const { state: result, reload } = useResource((signal) => listRequests({ page, page_size: PAGE_SIZE, state }, signal), [page, state]);

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
      <div className="page-head">
        <h1>Requests</h1>
        {can("intake.submit_request") ? (
          <Link className="button button-primary" to="/requests/new">
            New request
          </Link>
        ) : null}
      </div>

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
        <button type="button" className="button" onClick={reload}>
          Refresh
        </button>
      </div>

      {result.status === "loading" ? <LoadingState label="Loading requests…" /> : null}
      {result.status === "error" ? (
        <ErrorState
          title={isPermissionError(result.error) ? "Not available for your account" : "Could not load requests"}
          message={describeError(result.error)}
          onRetry={isPermissionError(result.error) ? undefined : reload}
        />
      ) : null}
      {result.status === "ready" ? (
        result.data.results.length === 0 ? (
          <EmptyState>
            {state || page > 1 ? (
              <>
                No requests match.{" "}
                <button type="button" className="link-button" onClick={() => setParams(new URLSearchParams())}>
                  Clear filter
                </button>
              </>
            ) : (
              "There are no requests yet."
            )}
          </EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Reference</th>
                    <th scope="col">State</th>
                    <th scope="col">Revision</th>
                    <th scope="col">Extraction</th>
                    <th scope="col">Submitted by</th>
                    <th scope="col">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {result.data.results.map((item) => (
                    <tr key={item.run_id}>
                      <td>
                        <Link to={`/requests/${item.run_id}`}>{item.reference}</Link>
                      </td>
                      <td>
                        <StateBadge state={item.state} />
                        {item.failed_step ? <span className="muted"> at {item.failed_step}</span> : null}
                      </td>
                      <td>{item.current_revision_number ?? <span className="muted">—</span>}</td>
                      <td>{item.outcome_status ?? <span className="muted">—</span>}</td>
                      <td title={item.submitted_by}>{item.submitted_by === me ? "You" : shortId(item.submitted_by)}</td>
                      <td>{formatDateTime(item.created_at)}</td>
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
