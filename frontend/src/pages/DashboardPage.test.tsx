import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getDashboardSummary } from "../api/dashboard";
import { RUN_STATES, type DashboardSummary, type RequestListItem, type RunState } from "../api/types";
import { DASHBOARD_POLL_MS } from "../lib/liveIntervals";
import { apiError, installFakeApi, json, type FakeResponse, type Handler, type RecordedCall } from "../test/fakeApi";
import { OTHER_ID, RUN_ID, detail, listItem, operator, page } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { approver } from "../test/review";
import { deferred, signedIn } from "../test/server";
import { signOut } from "../test/shell";

// FE-DASH-2: the dashboard reads BE-DASH-1's `GET /api/dashboard/summary/`, one request per refresh. Every figure is the
// server's: nothing is counted or summed in the browser. The fake clock follows real time (shouldAdvanceTime), so waits
// keep a margin instead of testing exact millisecond boundaries.

const MARGIN = 500;
const REF = "REQ-20260101-AAAA0001";
const SUMMARY_PATH = "/api/dashboard/summary/";

type Api = ReturnType<typeof installFakeApi>;
type Counts = Record<RunState, number>;

const COUNTS: Counts = { queued: 1, extracting: 2, needs_review: 4, approved: 6, exporting: 0, exported: 3, failed: 5, cancelled: 1 };
const ZEROS = Object.fromEntries(RUN_STATES.map((s) => [s, 0])) as Counts;

const recentRows = (n = 5): RequestListItem[] =>
  Array.from({ length: n }, (_, i) =>
    listItem({
      run_id: `00000000-0000-4000-8000-${String(i + 1).padStart(12, "0")}`,
      reference: `REQ-20260101-ROW${i + 1}`,
      state: "needs_review",
    }),
  );

/** The body the real endpoint sends: the server's own numbers for the given per-state counts. */
function summaryBody(counts: Counts = COUNTS, recent: RequestListItem[] = recentRows()): DashboardSummary {
  return {
    total: Object.values(counts).reduce((a, b) => a + b, 0),
    processing: counts.queued + counts.extracting + counts.exporting,
    needs_review: counts.needs_review,
    approved: counts.approved,
    failed: counts.failed,
    by_state: { ...counts },
    recent_requests: recent,
  };
}

type Override = (call: RecordedCall) => FakeResponse | Promise<FakeResponse> | "network-error" | undefined;

/** Answers the summary endpoint; the requests list (a navigation target) gets an empty page. Nothing else exists. */
function summaryServer(getBody: () => unknown = () => summaryBody(), override?: Override): Handler {
  return (call) => {
    const replaced = override?.(call);
    if (replaced !== undefined) return replaced;
    if (call.method === "GET" && call.path === SUMMARY_PATH) return json(200, getBody());
    if (call.method === "GET" && call.path === "/api/requests/") return json(200, page([]));
    return apiError(404, "not_found");
  };
}

const summaryCalls = (api: Api) => api.callsTo("GET", SUMMARY_PATH);
const listCalls = (api: Api) => api.callsTo("GET", "/api/requests/");
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

function setVisibility(initial: DocumentVisibilityState = "visible") {
  let current = initial;
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => current);
  return (next: DocumentVisibilityState) => {
    current = next;
    document.dispatchEvent(new Event("visibilitychange"));
  };
}

const metric = (id: string) => within(screen.getByTestId(`metric-${id}`));
const valueOf = (id: string) => metric(id).getByText(/^\d[\d,.\s]*$/).textContent;
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
    installFakeApi(signedIn(summaryServer()));
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(mainNav().getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it("is reachable directly at /dashboard", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it("sends a user without a view permission to the requests page, without a Dashboard link or a summary request", async () => {
    const api = installFakeApi(signedIn(() => apiError(403, "permission_denied"), operator({ permissions: ["intake.submit_request"] })));
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Requests" })).toBeInTheDocument();
    expect(mainNav().queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(summaryCalls(api)).toHaveLength(0);
  });

  it("shows a user without a view permission a plain notice at /dashboard and makes no request", async () => {
    const api = installFakeApi(signedIn(() => apiError(403, "permission_denied"), operator({ permissions: ["intake.submit_request"] })));
    renderApp("/dashboard");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(summaryCalls(api)).toHaveLength(0);
    expect(listCalls(api)).toHaveLength(0);
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
  });

  it("describes the scope by permission: own requests for an operator, every request for a viewer of all", async () => {
    installFakeApi(signedIn(summaryServer()));
    const view = renderApp("/dashboard");
    expect(await screen.findByText(/requests you submitted/)).toBeInTheDocument();
    view.unmount();
    cleanup();

    installFakeApi(signedIn(summaryServer(), approver()));
    renderApp("/dashboard");
    expect(await screen.findByText(/every request in the system/)).toBeInTheDocument();
  });

  it("shows New request only to a user who may submit", async () => {
    installFakeApi(signedIn(summaryServer(), approver()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(screen.queryByRole("link", { name: "New request" })).not.toBeInTheDocument();
    cleanup();

    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(screen.getAllByRole("link", { name: "New request" }).length).toBeGreaterThan(0);
  });
});

describe("the summary request", () => {
  it("sends exactly one parameterless GET to the summary endpoint, with the session cookie and no CSRF header", async () => {
    const api = installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await advance(MARGIN);

    expect(summaryCalls(api)).toHaveLength(1);
    const [call] = summaryCalls(api);
    expect(call!.url).toBe(SUMMARY_PATH);
    expect([...call!.query]).toEqual([]);
    expect(call!.credentials).toBe("same-origin");
    expect(call!.headers["x-csrftoken"]).toBeUndefined();
    // Nothing else is requested for the dashboard's data: no list request at all, let alone one per state.
    expect(listCalls(api)).toHaveLength(0);
    expect(api.calls.filter((c) => !c.path.startsWith("/api/auth/"))).toHaveLength(1);
  });

  it("returns the server's body unchanged", async () => {
    const body = summaryBody();
    installFakeApi(() => json(200, body));
    await expect(getDashboardSummary()).resolves.toEqual(body);
  });

  it("accepts an empty recent list and all-zero counts", async () => {
    installFakeApi(() => json(200, summaryBody(ZEROS, [])));
    await expect(getDashboardSummary()).resolves.toMatchObject({ total: 0, recent_requests: [] });
  });

  const bad: [string, (body: Record<string, unknown>) => unknown][] = [
    ["not an object", () => []],
    ["a missing total", ({ total: _t, ...rest }) => rest],
    ["a missing processing count", ({ processing: _p, ...rest }) => rest],
    ["a negative count", (b) => ({ ...b, failed: -1 })],
    ["a fractional count", (b) => ({ ...b, approved: 1.5 })],
    ["a count sent as text", (b) => ({ ...b, needs_review: "4" })],
    ["a null count", (b) => ({ ...b, total: null })],
    ["a missing by_state", ({ by_state: _s, ...rest }) => rest],
    ["a state missing from by_state", (b) => ({ ...b, by_state: { ...COUNTS, failed: undefined } })],
    ["a state unknown to the client in by_state", (b) => ({ ...b, by_state: { ...COUNTS, paused: 1 } })],
    ["a negative per-state count", (b) => ({ ...b, by_state: { ...COUNTS, queued: -2 } })],
    ["recent_requests that is not a list", (b) => ({ ...b, recent_requests: {} })],
    ["a recent request without a reference", (b) => ({ ...b, recent_requests: [{ ...listItem(), reference: undefined }] })],
    ["a recent request in an unknown state", (b) => ({ ...b, recent_requests: [{ ...listItem(), state: "paused" }] })],
  ];
  it.each(bad)("refuses %s as an unexpected response", async (_name, mutate) => {
    installFakeApi(() => json(200, mutate({ ...summaryBody() })));
    await expect(getDashboardSummary()).rejects.toMatchObject({ code: "unexpected_response" });
  });

  it("refuses an answer that is not JSON", async () => {
    installFakeApi(() => ({ status: 200, rawBody: "<html>" }));
    await expect(getDashboardSummary()).rejects.toMatchObject({ code: "unexpected_response" });
  });
});

describe("metrics", () => {
  it("shows the server's figures", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    expect(valueOf("total")).toBe("22"); // 5 rows are listed
    expect(valueOf("processing")).toBe("3");
    expect(valueOf("needs_review")).toBe("4");
    expect(valueOf("approved")).toBe("6");
    expect(valueOf("failed")).toBe("5");
  });

  it("shows the server's numbers even where they differ from what the browser could derive", async () => {
    // A server that counted processing differently (or a later definition) must win: no figure is recomputed here.
    installFakeApi(signedIn(summaryServer(() => ({ ...summaryBody(), total: 40, processing: 9 }))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("total")).toBe("40");
    expect(valueOf("processing")).toBe("9");
    const section = within(screen.getByRole("region", { name: "Status distribution" }));
    expect(section.getByRole("link", { name: /Approved/ })).toHaveTextContent("6 (15%)"); // 6 of the server's total 40
  });

  it("shows a real zero as 0", async () => {
    installFakeApi(signedIn(summaryServer(() => summaryBody({ ...COUNTS, approved: 0 }))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(valueOf("approved")).toBe("0");
  });

  it("shows zeros, not blanks, for a system with no requests", async () => {
    installFakeApi(signedIn(summaryServer(() => summaryBody(ZEROS, []))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    for (const id of ["total", "processing", "needs_review", "approved", "failed"]) expect(valueOf(id)).toBe("0");
    expect(screen.getByTestId("dashboard-announcer")).toHaveTextContent("Total requests: 0. Processing: 0.");
  });

  it("shows the status distribution with counts and shares of the total", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Status distribution" }));
    expect(section.getAllByRole("link")).toHaveLength(RUN_STATES.length);
    expect(section.getByRole("link", { name: /Approved/ })).toHaveTextContent("6 (27%)");
    expect(section.getByRole("link", { name: /Exporting/ })).toHaveTextContent("0 (0%)");
    expect(section.getByRole("link", { name: /Approved/ })).toHaveAttribute("href", "/requests?state=approved");
  });

  it("shows counts without a share when the total is zero", async () => {
    installFakeApi(signedIn(summaryServer(() => summaryBody(ZEROS, []))));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Status distribution" }));
    expect(section.getAllByRole("link")).toHaveLength(RUN_STATES.length);
    expect(section.queryByText(/\(\d+%\)/)).not.toBeInTheDocument();
  });

  it("links each card to the matching filter, and leaves Processing (three states) unlinked", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(metric("total").getByRole("link")).toHaveAttribute("href", "/requests");
    expect(metric("needs_review").getByRole("link")).toHaveAttribute("href", "/requests?state=needs_review");
    expect(metric("approved").getByRole("link")).toHaveAttribute("href", "/requests?state=approved");
    expect(metric("failed").getByRole("link")).toHaveAttribute("href", "/requests?state=failed");
    expect(metric("processing").queryByRole("link")).not.toBeInTheDocument();
  });

  it("gives every card an accessible name and value", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(metric("needs_review").getByRole("link", { name: /^Needs review\s*4/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Request figures" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("recent requests", () => {
  it("lists the newest requests in the server's order, with links to them and a Review shortcut", async () => {
    const rows = [
      listItem({ reference: REF, state: "needs_review" }),
      listItem({ run_id: OTHER_ID, reference: "REQ-20260102-BBBB0002", state: "failed", failed_step: "extract" }),
    ];
    installFakeApi(signedIn(summaryServer(() => summaryBody(COUNTS, rows))));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getByRole("link", { name: REF })).toHaveAttribute("href", `/requests/${RUN_ID}`);
    expect(section.getByRole("link", { name: `Review ${REF}` })).toBeInTheDocument();
    expect(section.getByRole("link", { name: "View REQ-20260102-BBBB0002" })).toBeInTheDocument();
    expect(section.getByText(/at extract/)).toBeInTheDocument();
    expect(section.getByRole("link", { name: "View all" })).toHaveAttribute("href", "/requests");
    const refs = section.getAllByRole("link", { name: /^REQ-/ }).map((l) => l.textContent);
    expect(refs).toEqual([REF, "REQ-20260102-BBBB0002"]);
  });

  it("lists all five rows the server sends", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getAllByRole("row")).toHaveLength(1 + 5); // header row + five
  });

  it("shows an empty state with a New request action when there are no requests", async () => {
    installFakeApi(signedIn(summaryServer(() => summaryBody(ZEROS, []))));
    renderApp("/dashboard");
    const section = within(await screen.findByRole("region", { name: "Recent requests" }));
    expect(section.getByText("There are no requests yet.")).toBeInTheDocument();
    expect(section.getByRole("link", { name: "New request" })).toHaveAttribute("href", "/requests/new");
    expect(valueOf("total")).toBe("0");
  });

  it("offers no New request action in the empty state to a user who may not submit", async () => {
    installFakeApi(signedIn(summaryServer(() => summaryBody(ZEROS, [])), approver()));
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
    gate.resolve(json(200, summaryBody()));
    expect(await screen.findByTestId("metric-total")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-loading")).not.toBeInTheDocument();
  });

  it("shows an error with a retry when the request fails, and recovers on retry", async () => {
    let failing = true;
    installFakeApi(signedIn((call) => (failing ? apiError(500, "server_error") : summaryServer()(call))));
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
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
  });

  it("shows an error with a retry when the server cannot be reached", async () => {
    installFakeApi(signedIn((call) => (call.path === SUMMARY_PATH ? "network-error" : apiError(404, "not_found"))));
    renderApp("/dashboard");
    expect(await screen.findByText("Could not load the dashboard")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
  });

  it("shows an error, not zeros, for a malformed answer", async () => {
    installFakeApi(signedIn(summaryServer(() => ({ total: 3 }))));
    renderApp("/dashboard");
    expect(await screen.findByText("Could not load the dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("metric-total")).not.toBeInTheDocument();
  });

  it("returns to the login page when the session has ended (403 not_authenticated)", async () => {
    let ended = false;
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return ended ? apiError(403, "not_authenticated") : json(200, operator());
      if (call.path === SUMMARY_PATH) {
        ended = true;
        return apiError(403, "not_authenticated");
      }
      return apiError(404, "not_found");
    });
    renderApp("/dashboard");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("live updates", () => {
  it("loads once on open without duplicate calls", async () => {
    const api = installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await advance(MARGIN);
    expect(summaryCalls(api)).toHaveLength(1);
  });

  it("sends one request per 10-second cycle and shows the new figures in place", async () => {
    let counts = COUNTS;
    const api = installFakeApi(signedIn(summaryServer(() => summaryBody(counts))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    await advance(DASHBOARD_POLL_MS - 2 * MARGIN);
    expect(summaryCalls(api)).toHaveLength(1); // not before the interval

    counts = { ...COUNTS, needs_review: 5, failed: 6 };
    await advance(2 * MARGIN + 50);
    await waitFor(() => expect(valueOf("total")).toBe("24")); // 22 + 1 + 1
    expect(summaryCalls(api)).toHaveLength(2);
    expect(valueOf("needs_review")).toBe("5");
    expect(valueOf("failed")).toBe("6");
    expect(screen.getByTestId("dashboard-announcer")).toHaveTextContent("Needs review: 5");

    await advance(DASHBOARD_POLL_MS);
    expect(summaryCalls(api)).toHaveLength(3);
    expect(listCalls(api)).toHaveLength(0);
  });

  it("updates the recent requests on a refresh", async () => {
    let rows = recentRows(1);
    installFakeApi(signedIn(summaryServer(() => summaryBody(COUNTS, rows))));
    renderApp("/dashboard");
    await screen.findByRole("link", { name: "REQ-20260101-ROW1" });

    rows = [listItem({ run_id: OTHER_ID, reference: "REQ-20260202-NEW00001" }), ...rows];
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(await screen.findByRole("link", { name: "REQ-20260202-NEW00001" })).toBeInTheDocument();
  });

  it("keeps the content on screen during a refresh, without a loading state", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    installFakeApi(signedIn(summaryServer(undefined, () => (refresh ? gate.promise : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(await screen.findByTestId("live-refreshing")).toBeInTheDocument();
    expect(valueOf("total")).toBe("22");
    expect(screen.queryByText("Loading dashboard…")).not.toBeInTheDocument();
    expect(screen.queryByTestId("metrics-loading")).not.toBeInTheDocument();
    gate.resolve(json(200, summaryBody()));
  });

  it("never starts a second refresh while one is still in flight", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    const api = installFakeApi(signedIn(summaryServer(undefined, () => (refresh ? gate.promise : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS * 4);
    expect(summaryCalls(api)).toHaveLength(2); // one refresh started, none piled on top
    gate.resolve(json(200, summaryBody()));
  });

  it("keeps the last good figures and shows a quiet error when a refresh fails", async () => {
    let failing = false;
    installFakeApi(signedIn(summaryServer(undefined, () => (failing ? apiError(500, "server_error") : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    failing = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    const note = await screen.findByText(/could not refresh/i);
    expect(note.closest("[role=alert]")).toBeNull();
    expect(valueOf("total")).toBe("22");
    expect(valueOf("failed")).toBe("5");
  });

  it("keeps the last good figures when a refresh cannot reach the server, and recovers", async () => {
    let down = false;
    const api = installFakeApi(signedIn(summaryServer(undefined, (call) => (down && call.path === SUMMARY_PATH ? "network-error" : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    down = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(valueOf("approved")).toBe("6");
    expect(summaryCalls(api)).toHaveLength(2);

    down = false;
    await advance(DASHBOARD_POLL_MS * 2);
    await waitFor(() => expect(screen.queryByText(/could not refresh/i)).not.toBeInTheDocument());
  });

  it("keeps the last good figures when a refresh is refused (403)", async () => {
    let refused = false;
    installFakeApi(signedIn(summaryServer(undefined, () => (refused ? apiError(403, "permission_denied") : undefined))));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refused = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(valueOf("total")).toBe("22");
  });

  it("pauses while the tab is hidden and refreshes once, at once, when it becomes visible", async () => {
    const setTab = setVisibility();
    const api = installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    setTab("hidden");
    await advance(DASHBOARD_POLL_MS * 4);
    expect(summaryCalls(api)).toHaveLength(1);

    setTab("visible");
    await advance(0);
    expect(summaryCalls(api)).toHaveLength(2);
    await advance(DASHBOARD_POLL_MS + MARGIN);
    expect(summaryCalls(api)).toHaveLength(3); // and the schedule goes on
  });

  it("does not poll at all when the page is opened in a hidden tab, until it becomes visible", async () => {
    const setTab = setVisibility("hidden");
    const api = installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await advance(DASHBOARD_POLL_MS * 3);
    expect(summaryCalls(api)).toHaveLength(1);
    setTab("visible");
    await advance(0);
    expect(summaryCalls(api)).toHaveLength(2);
  });

  it("aborts the request in flight and stops polling when the user signs out", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    const api = installFakeApi(
      signedIn((call) => {
        if (call.path === "/api/auth/logout/") return { status: 204 };
        return summaryServer(undefined, () => (refresh ? gate.promise : undefined))(call);
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    const pending = api.fetchMock.mock.calls.filter(([url]) => String(url) === SUMMARY_PATH).slice(1).map(([, init]) => init?.signal as AbortSignal);
    expect(pending).toHaveLength(1);
    expect(pending[0]!.aborted).toBe(false);

    await signOut(user);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(pending[0]!.aborted).toBe(true);

    const before = summaryCalls(api).length;
    await advance(DASHBOARD_POLL_MS * 5);
    expect(summaryCalls(api)).toHaveLength(before);
  });

  it("aborts a request in flight and stops polling when the user leaves the dashboard", async () => {
    const gate = deferred<FakeResponse>();
    let refresh = false;
    const api = installFakeApi(signedIn(summaryServer(undefined, () => (refresh ? gate.promise : undefined))));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");

    refresh = true;
    await advance(DASHBOARD_POLL_MS + MARGIN);
    const [, init] = api.fetchMock.mock.calls.filter(([url]) => String(url) === SUMMARY_PATH)[1]!;
    const signal = init?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    await user.click(mainNav().getByRole("link", { name: "New request" }));
    await screen.findByRole("heading", { name: "New request" });
    expect(signal.aborted).toBe(true);

    const before = summaryCalls(api).length;
    await advance(DASHBOARD_POLL_MS * 4);
    expect(summaryCalls(api)).toHaveLength(before);
  });

  it("refreshes at once, in place, on the Refresh button", async () => {
    const api = installFakeApi(signedIn(summaryServer()));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await advance(0);
    expect(summaryCalls(api)).toHaveLength(2);
    expect(screen.queryByText("Loading dashboard…")).not.toBeInTheDocument();
  });
});

describe("navigation and existing routes", () => {
  it("lists Dashboard first in the sidebar and keeps the other entries", async () => {
    installFakeApi(signedIn(summaryServer()));
    renderApp("/dashboard");
    await screen.findByTestId("metric-total");
    expect(mainNav().getAllByRole("link").map((l) => l.textContent)).toEqual(["Dashboard", "Requests", "New request"]);
    expect(mainNav().getByRole("link", { name: "Requests" })).not.toHaveAttribute("aria-current");
  });

  it("follows a card, a quick action and the sidebar to the requests list with the right filter", async () => {
    installFakeApi(signedIn(summaryServer()));
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
      signedIn((call) =>
        call.path === `/api/requests/${RUN_ID}/` ? json(200, detail()) : summaryServer(() => summaryBody(COUNTS, [listItem()]))(call),
      ),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/dashboard");
    await user.click(await screen.findByRole("link", { name: REF }));
    expect(await screen.findByRole("heading", { name: new RegExp(REF) })).toBeInTheDocument();
  });

  it("keeps /requests, /requests/new, request details and the not-found page working", async () => {
    installFakeApi(signedIn((call) => (call.path === `/api/requests/${RUN_ID}/` ? json(200, detail()) : summaryServer()(call))));
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
      return summaryServer()(call);
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/");
    await user.type(await screen.findByLabelText("Username"), "synthetic.operator");
    await user.type(screen.getByLabelText("Password"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    await screen.findByTestId("metric-total");
    expect(summaryCalls(api)).toHaveLength(1);
  });

  it("does not request any data for a signed-out visitor who opens /dashboard", async () => {
    const api = installFakeApi(() => apiError(403, "not_authenticated"));
    renderApp("/dashboard");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(summaryCalls(api)).toHaveLength(0);
    expect(listCalls(api)).toHaveLength(0);
  });
});
