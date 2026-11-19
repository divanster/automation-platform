import { Link } from "react-router";

import { RUN_STATES, type RunState } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { LiveStatus } from "../components/LiveStatus";
import { PageHeader } from "../components/PageHeader";
import { StateBadge } from "../components/StateBadge";
import { EmptyState, ErrorState, LoadingState, Notice } from "../components/Status";
import { Icon } from "../components/ui/Icon";
import { Skeleton } from "../components/ui/Skeleton";
import {
  RECENT_LIMIT,
  countsDisagree,
  loadDashboard,
  metricCount,
  type Count,
  type DashboardSnapshot,
  type MetricId,
} from "../lib/dashboardData";
import { describeError, isPermissionError } from "../lib/errors";
import { formatDateTime, stateLabel } from "../lib/format";
import { DASHBOARD_POLL_MS } from "../lib/liveIntervals";
import { canViewRequests, seesAllRequests } from "../lib/permissions";
import { useLiveResource } from "../lib/useLiveResource";

interface Metric {
  id: MetricId;
  label: string;
  caption: string;
  /** Where the card leads. Processing has none: it is three states, and the list filters by one state. */
  to?: string;
}

const METRICS: readonly Metric[] = [
  { id: "total", label: "Total requests", caption: "Everything you can see", to: "/requests" },
  { id: "processing", label: "Processing", caption: "Queued, extracting or exporting" },
  { id: "needs_review", label: "Needs review", caption: "Waiting for a person", to: "/requests?state=needs_review" },
  { id: "approved", label: "Approved", caption: "Approved by a reviewer", to: "/requests?state=approved" },
  { id: "failed", label: "Failed", caption: "Processing did not finish", to: "/requests?state=failed" },
];

const UNAVAILABLE_TEXT = "Unavailable";
const UNAVAILABLE_HINT = "The server did not return this figure. It is not zero.";

const formatCount = (count: Count) => (count.available ? new Intl.NumberFormat().format(count.value) : UNAVAILABLE_TEXT);

function MetricCard({ metric, count }: { metric: Metric; count: Count }) {
  const body = (
    <>
      <span className="metric-label">{metric.label}</span>
      <span className="metric-value" data-available={count.available} title={count.available ? undefined : UNAVAILABLE_HINT}>
        {formatCount(count)}
      </span>
      <span className="metric-caption">{metric.caption}</span>
    </>
  );
  return (
    <li className={`metric metric-${metric.id}`} data-testid={`metric-${metric.id}`}>
      {metric.to ? (
        <Link className="metric-body" to={metric.to}>
          {body}
        </Link>
      ) : (
        <div className="metric-body">{body}</div>
      )}
    </li>
  );
}

function MetricSkeletons() {
  return (
    <ul className="metric-grid" aria-hidden="true" data-testid="metrics-loading">
      {METRICS.map((metric) => (
        <li key={metric.id} className="metric">
          <div className="metric-body">
            <Skeleton width="55%" />
            <Skeleton className="skeleton-title" width="35%" />
            <Skeleton width="80%" />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** A plain-text summary for assistive technology. It sits in a live region, so it is announced only when a figure changes. */
function summarize(snapshot: DashboardSnapshot): string {
  const parts = METRICS.map((metric) => `${metric.label}: ${formatCount(metricCount(snapshot, metric.id))}`);
  return `Dashboard figures. ${parts.join(". ")}.`;
}

function Distribution({ snapshot }: { snapshot: DashboardSnapshot }) {
  const total = snapshot.total;
  return (
    <section className="card" aria-labelledby="dash-distribution">
      <div className="section-head">
        <h2 id="dash-distribution">Status distribution</h2>
      </div>
      <ul className="distribution">
        {RUN_STATES.map((state: RunState) => {
          const count = snapshot.byState[state];
          // A share is only meaningful against a known total; without one only the exact count is shown.
          const share = count.available && total.available && total.value > 0 ? Math.min(100, (count.value / total.value) * 100) : null;
          return (
            <li key={state}>
              <Link className="distribution-row" to={`/requests?state=${state}`}>
                <span className="distribution-state">
                  <span className="sr-only">{stateLabel(state)}</span>
                  <span aria-hidden="true">
                    <StateBadge state={state} />
                  </span>
                </span>
                <span className="distribution-bar" aria-hidden="true">
                  {share !== null ? <span className="distribution-fill" style={{ width: `${share}%` }} /> : null}
                </span>
                <span className="distribution-count" data-available={count.available} title={count.available ? undefined : UNAVAILABLE_HINT}>
                  {formatCount(count)}
                  {share !== null ? <span className="muted"> ({Math.round(share)}%)</span> : null}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {countsDisagree(snapshot) ? (
        <p className="muted small" data-testid="counts-disagree">
          The figures were read a moment apart and a request changed state in between, so they may differ by a little. They correct themselves on the next
          refresh.
        </p>
      ) : null}
    </section>
  );
}

function RecentRequests({ recent, canSubmit }: { recent: DashboardSnapshot["recent"]; canSubmit: boolean }) {
  return (
    <section className="card" aria-labelledby="dash-recent">
      <div className="section-head">
        <h2 id="dash-recent">Recent requests</h2>
        <Link to="/requests">View all</Link>
      </div>
      {recent === null ? (
        <Notice kind="warning">Recent requests could not be loaded. They are retried automatically.</Notice>
      ) : recent.length === 0 ? (
        <EmptyState
          icon="inbox"
          title="Nothing here yet"
          action={
            canSubmit ? (
              <Link className="button button-primary" to="/requests/new">
                <Icon name="plus" />
                New request
              </Link>
            ) : undefined
          }
        >
          There are no requests yet.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="requests-table">
            <caption className="sr-only">The {RECENT_LIMIT} newest requests</caption>
            <thead>
              <tr>
                <th scope="col">Reference</th>
                <th scope="col">State</th>
                <th scope="col" className="col-date">
                  Created
                </th>
                <th scope="col">
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {recent.map((item) => (
                <tr key={item.run_id}>
                  <td>
                    <Link className="ref-link" to={`/requests/${item.run_id}`}>
                      {item.reference}
                    </Link>
                  </td>
                  <td>
                    <StateBadge state={item.state} />
                    {item.failed_step ? <span className="muted"> at {item.failed_step}</span> : null}
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
      )}
    </section>
  );
}

function QuickActions({ canSubmit }: { canSubmit: boolean }) {
  return (
    <section className="card" aria-labelledby="dash-actions">
      <div className="section-head">
        <h2 id="dash-actions">Quick actions</h2>
      </div>
      <div className="quick-actions">
        {canSubmit ? (
          <Link className="button button-primary" to="/requests/new">
            <Icon name="plus" />
            New request
          </Link>
        ) : null}
        <Link className="button" to="/requests?state=needs_review">
          <Icon name="check" />
          Review requests
        </Link>
        <Link className="button" to="/requests">
          <Icon name="list" />
          All requests
        </Link>
      </div>
    </section>
  );
}

function DashboardContent() {
  const { can } = useAuth();
  const { state: result, reload, refreshing, refreshError, lastUpdated } = useLiveResource(loadDashboard, [], {
    interval: () => DASHBOARD_POLL_MS,
  });
  const canSubmit = can("intake.submit_request");

  return (
    <>
      <div className="toolbar">
        <p className="result-summary dashboard-scope">
          {seesAllRequests(can) ? "Figures cover every request in the system." : "Figures cover the requests you submitted."} They are counted by the server.
        </p>
        <div className="toolbar-end live-toolbar">
          {result.status === "ready" ? <LiveStatus refreshing={refreshing} lastUpdated={lastUpdated} error={refreshError} onRetry={reload} /> : null}
          <button type="button" className="button" onClick={reload}>
            <Icon name="refresh" />
            Refresh
          </button>
        </div>
      </div>

      {result.status === "loading" ? (
        <>
          <LoadingState label="Loading dashboard…" />
          <MetricSkeletons />
        </>
      ) : null}

      {result.status === "error" ? (
        <ErrorState
          title={isPermissionError(result.error) ? "Not available for your account" : "Could not load the dashboard"}
          message={describeError(result.error)}
          onRetry={isPermissionError(result.error) ? undefined : reload}
        />
      ) : null}

      {result.status === "ready" ? (
        <>
          <div className="sr-only" role="status" data-testid="dashboard-announcer">
            {summarize(result.data)}
          </div>
          <section aria-labelledby="dash-metrics">
            <h2 id="dash-metrics" className="sr-only">
              Request figures
            </h2>
            <ul className="metric-grid">
              {METRICS.map((metric) => (
                <MetricCard key={metric.id} metric={metric} count={metricCount(result.data, metric.id)} />
              ))}
            </ul>
          </section>
          <div className="dashboard-columns">
            <div className="dashboard-main">
              <RecentRequests recent={result.data.recent} canSubmit={canSubmit} />
            </div>
            <div className="dashboard-side">
              <QuickActions canSubmit={canSubmit} />
              <Distribution snapshot={result.data} />
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}

export function DashboardPage() {
  const { can } = useAuth();
  const allowed = canViewRequests(can);
  return (
    <section>
      <PageHeader title="Dashboard" description="Where requests stand right now." />
      {allowed ? (
        <DashboardContent />
      ) : (
        <EmptyState icon="alert" title="Not available for your account">
          Your account cannot view requests, so there is nothing to summarize here.
        </EmptyState>
      )}
    </section>
  );
}
