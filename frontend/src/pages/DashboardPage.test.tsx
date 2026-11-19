import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RUN_STATES, type RunState } from "../api/types";
import { countsDisagree, loadDashboard, metricCount, type DashboardSnapshot } from "../lib/dashboardData";
import { DASHBOARD_POLL_MS } from "../lib/liveIntervals";
import { apiError, installFakeApi, json, type FakeResponse, type Handler, type RecordedCall } from "../test/fakeApi";
import { OTHER_ID, RUN_ID, detail, listItem, operator, page } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { approver } from "../test/review";
import { deferred, signedIn } from "../test/server";
import { signOut } from "../test/shell";

// FE-6: the dashboard. Counts must come from the server's `count` per state, never from the rows of a page, and a figure
// that could not be fetched must read "Unavailable", never 0. The fake clock follows real time (shouldAdvanceTime), so
// waits keep a margin instead of testing exact millisecond boundaries.

const MARGIN = 500;
const REF = "REQ-20260101-AAAA0001";

type Api = ReturnType<typeof installFakeApi>;
type Counts = Record<RunState, number>;

// 22 requests in total, far more than the 5 recent rows and the single row each per-state request returns.
const COUNTS: Counts = { queued: 1, extracting: 2, needs_review: 4, approved: 6, exporting: 0, exported: 3, failed: 5, cancelled: 1 };

const sum = (counts: Counts) => Object.values(counts).reduce((a, b) => a + b, 0);
const listCalls = (api: Api) => api.callsTo("GET", "/api/requests/");
const stateCalls = (api: Api) => listCalls(api).filter((c) => c.query.has("state"));
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

function setVisibility(initial: DocumentVisibilityState = "visible") {
  let current = initial;
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => current);
  return (next: DocumentVisibilityState) => {
    current = next;
    document.dispatchEvent(new Event("visibilitychange"));
  };
}

/** A fake list endpoint that behaves like the real one: `count` is the total for the filter, `results` only one page. */
function listServer(getCounts: () => Counts, override?: (call: RecordedCall) => FakeResponse | Promise<FakeResponse> | "network-error" | undefined): Handler {
  return (call) => {
    if (call.method !== "GET" || call.path !== "/api/requests/") return apiError(404, "not_found");
    const replaced = override?.(call);
    if (replaced !== undefined) return replaced;
    const counts = getCounts();
    const state = call.query.get("state") as RunState | null;
    const size = Number(call.query.get("page_size"));
    const count = state ? counts[state] : sum(counts);
    const rows = Array.from({ length: Math.min(count, size) }, (_, i) =>
      listItem({
        run_id: `00000000-0000-4000-8000-${String(i + 1).padStart(12, "0")}`,
        reference: `REQ-20260101-ROW${i + 1}`,
        state: state ?? "needs_review",
      }),
    );
    return json(200, page(rows, { count, page_size: size }));
  };
}

const metric = (id: string) => within(screen.getByTestId(`metric-${id}`));
const valueOf = (id: string) => metric(id).getByText(/^(\d[\d,.\s]*|Unavailable)$/).textContent;
const mainNav = () => within(screen.getByRole("navigation", { name: "Main" }));

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("routing and permissions", () => {
  it("is the landing page of an authenticated user who may view requests", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(mainNav().getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it("is reachable directly at /dashboard", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it("sends a user without a view permission to the requests page, without a Dashboard link", async () => {
    const api = installFakeApi(signedIn(() => apiError(403, "permission_denied"), operator({ permissions: ["intake.submit_request"] })));
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Requests" })).toBeInTheDocument();
    expect(mainNav().queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(stateCalls(api)).toHaveLength(0);
  });

  it("shows a user without a view permission a plain notice at /dashboard and makes no request", async () => {
    const api = installFakeApi(signedIn(() => apiError(403, "permission_denied"), operator({ permissions: ["intake.submit_request"] })));
    renderApp("/dashboard");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(listCalls(api)).toHaveLength(0);
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
  });

  it("describes the scope by permission: own requests for an operator, every request for a viewer of all", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    const view = renderApp("/dashboard");
    expect(await screen.findByText(/requests you submitted/)).toBeInTheDocument();
    view.unmount();
    cleanup();

    installFakeApi(signedIn(listServer(() => COUNTS), approver()));
    renderApp("/dashboard");
    expect(await screen.findByText(/every request in the system/)).toBeInTheDocument();
  });

  it("shows New request only to a user who may submit", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS), approver()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(screen.queryByRole("link", { name: "New request" })).not.toBeInTheDocument();
    cleanup();

    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(screen.getAllByRole("link", { name: "New request" }).length).toBeGreaterThan(0);
  });
});

describe("metrics", () => {
  it("shows the server's counts, not what the visible rows add up to", async () => {
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    expect(valueOf("total")).toBe("22"); // 5 rows are on the page
    expect(valueOf("processing")).toBe("3"); // queued 1 + extracting 2 + exporting 0
    expect(valueOf("needs_review")).toBe("4");
    expect(valueOf("approved")).toBe("6");
    expect(valueOf("failed")).toBe("5");

    // One unfiltered page of five plus one single-row request per state: the counts are not taken from page contents.
    expect(listCalls(api)).toHaveLength(1 + RUN_STATES.length);
    const unfiltered = listCalls(api).filter((c) => !c.query.has("state"));
    expect(unfiltered).toHaveLength(1);
    expect(Object.fromEntries(unfiltered[0]!.query)).toEqual({ page: "1", page_size: "5" });
    expect(stateCalls(api).map((c) => c.query.get("state")).sort()).toEqual([...RUN_STATES].sort());
    for (const call of stateCalls(api)) expect(call.query.get("page_size")).toBe("1");
  });

  it("shows a real zero as 0", async () => {
    installFakeApi(signedIn(listServer(() => ({ ...COUNTS, approved: 0 }))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("approved")).toBe("0");
  });

  it("shows Unavailable, never 0, for a figure whose request failed, and keeps the others", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS, (call) => (call.query.get("state") === "failed" ? apiError(500, "server_error") : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("failed")).toBe("Unavailable");
    expect(valueOf("needs_review")).toBe("4");
    expect(valueOf("total")).toBe("22");
    expect(metric("failed").queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard-announcer")).toHaveTextContent("Failed: Unavailable");
  });

  it("makes Processing unavailable when any of its states is missing instead of showing a partial sum", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS, (call) => (call.query.get("state") === "extracting" ? "network-error" : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("processing")).toBe("Unavailable");
    expect(valueOf("approved")).toBe("6");
  });

  it("makes the total unavailable when the unfiltered request fails, but still lists the states", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS, (call) => (call.query.has("state") ? undefined : apiError(500, "server_error")))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("total")).toBe("Unavailable");
    expect(valueOf("needs_review")).toBe("4");
    expect(screen.getByText(/recent requests could not be loaded/i)).toBeInTheDocument();
    // Without a total there is no share to show, only exact counts.
    expect(screen.queryByText(/\(\d+%\)/)).not.toBeInTheDocument();
  });

  it("shows the status distribution with counts and shares of the total", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Status distribution" }));
    expect(section.getAllByRole("link")).toHaveLength(RUN_STATES.length);
    expect(section.getByRole("link", { name: /Approved/ })).toHaveTextContent("6 (27%)");
    expect(section.getByRole("link", { name: /Exporting/ })).toHaveTextContent("0 (0%)");
    expect(section.getByRole("link", { name: /Approved/ })).toHaveAttribute("href", "/requests?state=approved");
  });

  it("says so when per-state counts and the total disagree because a request moved in between", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS, (call) => (call.query.has("state") ? undefined : json(200, page([], { count: 23 }))))));
    renderApp("/dashboard");
    expect(await screen.findByTestId("counts-disagree")).toBeInTheDocument();
  });

  it("does not say so when they agree", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(screen.queryByTestId("counts-disagree")).not.toBeInTheDocument();
  });

  it("links each card to the matching filter, and leaves Processing (three states) unlinked", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(metric("total").getByRole("link")).toHaveAttribute("href", "/requests");
    expect(metric("needs_review").getByRole("link")).toHaveAttribute("href", "/requests?state=needs_review");
    expect(metric("approved").getByRole("link")).toHaveAttribute("href", "/requests?state=approved");
    expect(metric("failed").getByRole("link")).toHaveAttribute("href", "/requests?state=failed");
    expect(metric("processing").queryByRole("link")).not.toBeInTheDocument();
  });

  it("gives every card an accessible name and value", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(metric("needs_review").getByRole("link", { name: /^Needs review\s*4/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Request figures" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("dashboardData", () => {
  const unavailable = { available: false } as const;
  const known = (value: number) => ({ available: true, value }) as const;
  const snapshot = (over: Partial<Record<RunState, ReturnType<typeof known> | typeof unavailable>> = {}): DashboardSnapshot => ({
    total: known(10),
    recent: [],
    byState: { queued: known(1), extracting: known(2), needs_review: known(3), approved: known(1), exporting: known(0), exported: known(2), failed: known(1), cancelled: known(0), ...over },
  });

  it("derives Processing as queued + extracting + exporting", () => {
    expect(metricCount(snapshot(), "processing")).toEqual(known(3));
  });

  it("derives nothing from a missing state", () => {
    expect(metricCount(snapshot({ exporting: unavailable }), "processing")).toEqual(unavailable);
    expect(countsDisagree(snapshot({ exporting: unavailable }))).toBe(false);
  });

  it("detects disagreement only when everything is known", () => {
    expect(countsDisagree(snapshot())).toBe(false);
    expect(countsDisagree({ ...snapshot(), total: known(11) })).toBe(true);
    expect(countsDisagree({ ...snapshot(), total: unavailable })).toBe(false);
  });

  it("rejects an invalid count instead of passing it on", async () => {
    installFakeApi(signedIn(() => json(200, { count: -1, page: 1, page_size: 1, results: [] })));
    const result = await loadDashboard(new AbortController().signal);
    expect(result.total).toEqual(unavailable);
  });

  it("throws when every request fails", async () => {
    installFakeApi(() => apiError(500, "server_error"));
    await expect(loadDashboard(new AbortController().signal)).rejects.toMatchObject({ status: 500 });
  });
});

describe("recent requests", () => {
  it("lists the newest requests with links to them and a Review shortcut", async () => {
    const rows = [
      listItem({ reference: REF, state: "needs_review" }),
      listItem({ run_id: OTHER_ID, reference: "REQ-20260102-BBBB0002", state: "failed", failed_step: "extract" }),
    ];
    installFakeApi(signedIn(listServer(() => COUNTS, (call) => (call.query.has("state") ? undefined : json(200, page(rows, { count: 22, page_size: 5 }))))));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getByRole("link", { name: REF })).toHaveAttribute("href", `/requests/${RUN_ID}`);
    expect(section.getByRole("link", { name: `Review ${REF}` })).toBeInTheDocument();
    expect(section.getByRole("link", { name: "View REQ-20260102-BBBB0002" })).toBeInTheDocument();
    expect(section.getByText(/at extract/)).toBeInTheDocument();
    expect(section.getByRole("link", { name: "View all" })).toHaveAttribute("href", "/requests");
  });

  it("shows an empty state with a New request action when there are no requests", async () => {
    installFakeApi(signedIn(listServer(() => ({ ...COUNTS, ...Object.fromEntries(RUN_STATES.map((s) => [s, 0])) }))));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getByText("There are no requests yet.")).toBeInTheDocument();
    expect(section.getByRole("link", { name: "New request" })).toHaveAttribute("href", "/requests/new");
    expect(valueOf("total")).toBe("0");
  });

  it("offers no New request action in the empty state to a user who may not submit", async () => {
    installFakeApi(signedIn(listServer(() => Object.fromEntries(RUN_STATES.map((s) => [s, 0])) as Counts), approver()));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getByText("There are no requests yet.")).toBeInTheDocument();
    expect(section.queryByRole("link", { name: "New request" })).not.toBeInTheDocument();
  });
});

describe("loading and errors", () => {
  it("shows skeleton cards and an announced loading state until the first answer", async () => {
    const gate = deferred<FakeResponse>();
    installFakeApi(signedIn(() => gate.promise));
    renderApp("/dashboard");
    expect(await screen.findByText("Loading dashboard…")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
    gate.resolve(json(200, page([], { count: 0 })));
    expect(await screen.findByTestId("metric-total")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-loading")).not.toBeInTheDocument();
  });

  it("shows an error with a retry when everything fails, and recovers on retry", async () => {
    let failing = true;
    installFakeApi(signedIn((call) => (failing ? apiError(500, "server_error") : listServer(() => COUNTS)(call))));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    expect(await screen.findByText("Could not load the dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument(); // no zeros in place of data

    failing = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("metric-total")).toBeInTheDocument();
    expect(valueOf("total")).toBe("22");
  });

  it("explains a refused request (403) without a retry button", async () => {
    installFakeApi(signedIn(() => apiError(403, "permission_denied")));
    renderApp("/dashboard");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

describe("live updates", () => {
  const callsPerRefresh = 1 + RUN_STATES.length;

  it("loads once on open without duplicate calls", async () => {
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await advance(MARGIN);
    expect(listCalls(api)).toHaveLength(callsPerRefresh);
  });

  it("refreshes every 10 seconds and shows the new figures in place", async () => {
    let counts = COUNTS;
    const api = installFakeApi(signedIn(listServer(() => counts)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    await advance(DASHBOARD_POLL_MS - 2 * MARGIN);
    expect(listCalls(api)).toHaveLength(callsPerRefresh); // not before the interval

    counts = { ...COUNTS, needs_review: 5, failed: 6 };
    await advance(2 * MARGIN + 50);
    await waitFor(() => expect(valueOf("total")).toBe("24")); // 22 + 1 + 1
    expect(listCalls(api)).toHaveLength(2 * callsPerRefresh);
    expect(valueOf("needs_review")).toBe("5");
    expect(valueOf("failed")).toBe("6");
    expect(screen.getByTestId("dashboard-announcer")).toHaveTextContent("Needs review: 5");

    await advance(DASHBOARD_POLL_MS);
    expect(listCalls(api)).toHaveLength(3 * callsPerRefresh);
  });

  it("keeps the content on screen during a refresh, without a loading state", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    installFakeApi(signedIn(listServer(() => COUNTS, () => (refresh ? gate.promise : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(await screen.findByTestId("live-refreshing")).toBeInTheDocument();
    expect(valueOf("total")).toBe("22");
    expect(screen.queryByText("Loading dashboard…")).not.toBeInTheDocument();
    expect(screen.queryByTestId("metrics-loading")).not.toBeInTheDocument();
    gate.resolve(json(200, page([], { count: 0 })));
  });

  it("never starts a second refresh while one is still in flight", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    const api = installFakeApi(signedIn(listServer(() => COUNTS, () => (refresh ? gate.promise : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(2 * callsPerRefresh); // one refresh started, none piled on top
    gate.resolve(json(200, page([], { count: 0 })));
  });

  it("keeps the last good figures and shows a quiet error when a refresh fails", async () => {
    let failing = false;
    installFakeApi(signedIn(listServer(() => COUNTS, () => (failing ? apiError(500, "server_error") : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    failing = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    const note = await screen.findByText(/could not refresh/i);
    expect(note.closest("[role=alert]")).toBeNull();
    expect(valueOf("total")).toBe("22");
    expect(valueOf("failed")).toBe("5");
  });

  it("pauses while the tab is hidden and refreshes once, at once, when it becomes visible", async () => {
    const setTab = setVisibility();
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    setTab("hidden");
    await advance(DASHBOARD_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(callsPerRefresh);

    setTab("visible");
    await advance(0);
    expect(listCalls(api)).toHaveLength(2 * callsPerRefresh);
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(3 * callsPerRefresh); // and the schedule goes on
  });

  it("does not poll at all when the page is opened in a hidden tab, until it becomes visible", async () => {
    const setTab = setVisibility("hidden");
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await advance(DASHBOARD_POLL_MS * 3);
    expect(listCalls(api)).toHaveLength(callsPerRefresh);
    setTab("visible");
    await advance(0);
    expect(listCalls(api)).toHaveLength(2 * callsPerRefresh);
  });

  it("aborts the requests in flight and stops polling when the user signs out", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    const api = installFakeApi(
      signedIn((call) => {
        if (call.path === "/api/auth/logout/") return { status: 204 };
        return listServer(() => COUNTS, () => (refresh ? gate.promise : undefined))(call);
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    const pending = api.fetchMock.mock.calls
      .filter(([url]) => String(url).startsWith("/api/requests/?"))
      .slice(callsPerRefresh)
      .map(([, init]) => init?.signal as AbortSignal);
    expect(pending).toHaveLength(callsPerRefresh);

    await signOut(user);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(pending.every((s) => s.aborted)).toBe(true);

    const before = listCalls(api).length;
    await advance(DASHBOARD_POLL_MS * 5);
    expect(listCalls(api)).toHaveLength(before);
  });

  it("stops polling when the user leaves the dashboard", async () => {
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await user.click(mainNav().getByRole("link", { name: "New request" }));
    await screen.findByRole("heading", { name: "New request" });
    const before = listCalls(api).length;
    await advance(DASHBOARD_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(before);
  });

  it("refreshes at once, in place, on the Refresh button", async () => {
    const api = installFakeApi(signedIn(listServer(() => COUNTS)));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await advance(0);
    expect(listCalls(api)).toHaveLength(2 * callsPerRefresh);
    expect(screen.queryByText("Loading dashboard…")).not.toBeInTheDocument();
  });
});

describe("navigation and existing routes", () => {
  it("lists Dashboard first in the sidebar and keeps the other entries", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(mainNav().getAllByRole("link").map((l) => l.textContent)).toEqual(["Dashboard", "Requests", "New request"]);
    expect(mainNav().getByRole("link", { name: "Requests" })).not.toHaveAttribute("aria-current");
  });

  it("follows a card, a quick action and the sidebar to the requests list with the right filter", async () => {
    installFakeApi(signedIn(listServer(() => COUNTS)));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    await user.click(metric("failed").getByRole("link"));
    expect(await screen.findByLabelText("State")).toHaveValue("failed");

    await user.click(mainNav().getByRole("link", { name: "Dashboard" }));
    await screen.findByTestId("metric-total");
    await user.click(screen.getByRole("link", { name: "Review requests" }));
    expect(await screen.findByLabelText("State")).toHaveValue("needs_review");
  });

  it("opens a recent request on its detail page", async () => {
    installFakeApi(
      signedIn((call) => (call.path === `/api/requests/${RUN_ID}/` ? json(200, detail()) : listServer(() => COUNTS, (c) => (c.query.has("state") ? undefined : json(200, page([listItem()], { count: 22 }))))(call))),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await user.click(await screen.findByRole("link", { name: REF }));
    expect(await screen.findByRole("heading", { name: new RegExp(REF) })).toBeInTheDocument();
  });

  it("keeps /requests, /requests/new, request details and the not-found page working", async () => {
    installFakeApi(signedIn((call) => (call.path === `/api/requests/${RUN_ID}/` ? json(200, detail()) : listServer(() => COUNTS)(call))));
    const checks: [string, string | RegExp][] = [
      ["/requests", "Requests"],
      ["/requests/new", "New request"],
      [`/requests/${RUN_ID}`, new RegExp(REF)],
      ["/no-such-page", /not found/i],
    ];
    for (const [path, heading] of checks) {
      const view = renderApp(path);
      expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
      view.unmount();
      cleanup();
    }
  });

  it("returns a signed-out visitor to the login page and then to the landing page", async () => {
    let authed = false;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return authed ? json(200, operator()) : apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "t" });
      if (call.path === "/api/auth/login/") {
        authed = true;
        return json(200, { ...operator(), csrf_token: "t2" });
      }
      return listServer(() => COUNTS)(call);
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/");
    await user.type(await screen.findByLabelText("Username"), "synthetic.operator");
    await user.type(screen.getByLabelText("Password"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    await screen.findByTestId("metric-total");
    expect(listCalls(api).length).toBeGreaterThan(0);
  });

  it("does not request any data for a signed-out visitor who opens /dashboard", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/me/" ? apiError(403, "not_authenticated") : apiError(403, "not_authenticated")));
    renderApp("/dashboard");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(listCalls(api)).toHaveLength(0);
  });
});
