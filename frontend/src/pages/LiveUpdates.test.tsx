import { act, cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Draft, RequestDetail } from "../api/types";
import { ACTIVE_POLL_MS, LIST_POLL_MS, REVIEW_POLL_MS } from "../lib/liveIntervals";
import { isNotableTransition } from "../lib/useStateTransitionToasts";
import { apiError, installFakeApi, json, type FakeResponse, type Handler } from "../test/fakeApi";
import { HASH, OTHER_ID, RUN_ID, USER_ID, detail, listItem, page } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { HASH2, REV1, REV2, approver, fakeServer, inReview } from "../test/review";
import { deferred, signedIn, signedOut } from "../test/server";
import { signOut } from "../test/shell";

// FE-5: background synchronization. The fake clock also follows real time (shouldAdvanceTime), so every wait leaves a
// margin of at least half a second instead of testing exact millisecond boundaries.

const MARGIN = 500;
const REF = "REQ-20260101-AAAA0001";

type Api = ReturnType<typeof installFakeApi>;

const listCalls = (api: Api) => api.callsTo("GET", "/api/requests/");
const detailCalls = (api: Api) => api.callsTo("GET", `/api/requests/${RUN_ID}/`);
/** The AbortSignal of the n-th call whose URL starts with `prefix`. */
function signalOf(api: Api, prefix: string, n: number): AbortSignal {
  const matching = api.fetchMock.mock.calls.filter(([url]) => String(url).startsWith(prefix));
  return matching[n]![1]!.signal as AbortSignal;
}

/** Moves the fake clock inside act(), so the state updates of timer-driven refreshes are flushed before the next assertion. */
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

function setVisibility(initial: DocumentVisibilityState = "visible") {
  let current = initial;
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => current);
  return (next: DocumentVisibilityState) => {
    current = next;
    document.dispatchEvent(new Event("visibilitychange"));
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("transitions worth a notification", () => {
  it.each([
    ["queued", "needs_review", true],
    ["extracting", "needs_review", true],
    ["extracting", "failed", true],
    ["queued", "failed", true],
    ["needs_review", "approved", false],
    ["needs_review", "needs_review", false],
    ["failed", "failed", false],
    ["queued", "extracting", false],
    [undefined, "needs_review", false],
    [undefined, "failed", false],
  ] as const)("%s -> %s: %s", (previous, next, expected) => {
    expect(isNotableTransition(previous, next)).toBe(expected);
  });
});

describe("requests list: background refresh", () => {
  it("refreshes every 5 seconds with the same filter, page and URL parameters", async () => {
    const api = installFakeApi(signedIn(() => json(200, page([listItem({ state: "failed", failed_step: "extract" })], { count: 45, page: 2 }))));
    renderApp("/requests?state=failed&page=2");
    await screen.findByText(REF);
    expect(listCalls(api)).toHaveLength(1);

    await advance(LIST_POLL_MS - 2 * MARGIN);
    expect(listCalls(api)).toHaveLength(1); // not before the interval
    await advance(2 * MARGIN + LIST_POLL_MS);
    expect(listCalls(api)).toHaveLength(3);

    for (const call of listCalls(api)) expect(Object.fromEntries(call.query)).toEqual({ page: "2", page_size: "20", state: "failed" });
    expect(screen.getByLabelText("State")).toHaveValue("failed");
    expect(screen.getByText(/page 2/i)).toBeInTheDocument();
  });

  it("keeps the rows on screen while a refresh is running, then shows what changed and when", async () => {
    const second = deferred<FakeResponse>();
    let calls = 0;
    const api = installFakeApi(signedIn(() => (++calls === 2 ? second.promise : json(200, page([listItem()])))));
    renderApp("/requests");
    await screen.findByText(REF);

    await advance(LIST_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(2);
    expect(await screen.findByTestId("live-refreshing")).toBeInTheDocument();
    expect(screen.getByText(REF)).toBeInTheDocument(); // the existing row is still there
    expect(screen.queryByText("Loading requests…")).not.toBeInTheDocument(); // and no full-page loading state

    second.resolve(json(200, page([listItem(), listItem({ run_id: OTHER_ID, reference: "REQ-20260102-BBBB0002" })])));
    expect(await screen.findByText("REQ-20260102-BBBB0002")).toBeInTheDocument();
    expect(screen.queryByTestId("live-refreshing")).not.toBeInTheDocument();
    expect(screen.getByText(/^Updated/)).toBeInTheDocument();
  });

  it("never has two requests for the list in flight, however slow the server is", async () => {
    const second = deferred<FakeResponse>();
    let calls = 0;
    const api = installFakeApi(signedIn(() => (++calls === 2 ? second.promise : json(200, page([listItem()])))));
    renderApp("/requests");
    await screen.findByText(REF);

    await advance(LIST_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(2); // the second one is still pending, so nothing else was started

    second.resolve(json(200, page([listItem()])));
    await advance(LIST_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(3);
  });

  it("ignores an answer for an earlier filter and aborts its request", async () => {
    const slow = deferred<FakeResponse>();
    let calls = 0;
    const handler: Handler = (call) => {
      calls += 1;
      if (call.query.get("state") === "failed") return json(200, page([listItem({ reference: "REQ-FAILED-ONE", state: "failed", failed_step: "extract" })]));
      return calls === 2 ? slow.promise : json(200, page([listItem()]));
    };
    const api = installFakeApi(signedIn(handler));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/requests");
    await screen.findByText(REF);

    await advance(LIST_POLL_MS + MARGIN); // a background refresh of the unfiltered list is now pending
    await user.selectOptions(screen.getByLabelText("State"), "failed");
    expect(await screen.findByText("REQ-FAILED-ONE")).toBeInTheDocument();
    expect(signalOf(api, "/api/requests/?", 1).aborted).toBe(true);

    slow.resolve(json(200, page([listItem({ reference: "REQ-OLD-UNFILTERED" })])));
    await advance(MARGIN);
    expect(screen.queryByText("REQ-OLD-UNFILTERED")).not.toBeInTheDocument();
    expect(screen.getByText("REQ-FAILED-ONE")).toBeInTheDocument();
  });

  it("pauses while the tab is hidden and refreshes at once when it becomes visible", async () => {
    const setTab = setVisibility();
    const api = installFakeApi(signedIn(() => json(200, page([listItem()]))));
    renderApp("/requests");
    await screen.findByText(REF);

    setTab("hidden");
    await advance(LIST_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(1);

    setTab("visible");
    await advance(0);
    expect(listCalls(api)).toHaveLength(2); // immediately, not after the interval
    await advance(LIST_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(3); // and the schedule goes on
  });

  it("does not start a second refresh when the tab becomes visible during one", async () => {
    const setTab = setVisibility();
    const second = deferred<FakeResponse>();
    let calls = 0;
    const api = installFakeApi(signedIn(() => (++calls === 2 ? second.promise : json(200, page([listItem()])))));
    renderApp("/requests");
    await screen.findByText(REF);
    await advance(LIST_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(2);

    setTab("hidden");
    setTab("visible");
    await advance(MARGIN);
    expect(listCalls(api)).toHaveLength(2);
    second.resolve(json(200, page([listItem()])));
  });

  it("aborts the request in flight and stops polling when the user signs out", async () => {
    const second = deferred<FakeResponse>();
    let calls = 0;
    const api = installFakeApi(
      signedIn((call) => {
        if (call.path === "/api/auth/logout/") return { status: 204 };
        return ++calls === 2 ? second.promise : json(200, page([listItem()]));
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/requests");
    await screen.findByText(REF);
    await advance(LIST_POLL_MS + MARGIN);
    expect(listCalls(api)).toHaveLength(2);

    await signOut(user);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(signalOf(api, "/api/requests/?", 1).aborted).toBe(true);

    const before = listCalls(api).length;
    await advance(LIST_POLL_MS * 6);
    expect(listCalls(api)).toHaveLength(before);
  });

  it("aborts the request in flight and stops polling when the user leaves the page", async () => {
    const second = deferred<FakeResponse>();
    let calls = 0;
    const api = installFakeApi(signedIn(() => (++calls === 2 ? second.promise : json(200, page([listItem()])))));
    const { unmount } = renderApp("/requests");
    await screen.findByText(REF);
    await advance(LIST_POLL_MS + MARGIN);

    unmount();
    expect(signalOf(api, "/api/requests/?", 1).aborted).toBe(true);
    await advance(LIST_POLL_MS * 6);
    expect(listCalls(api)).toHaveLength(2);
  });

  it("never polls for a user who is not signed in", async () => {
    const api = installFakeApi(signedOut());
    renderApp("/requests");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await advance(LIST_POLL_MS * 4);
    expect(listCalls(api)).toHaveLength(0);
  });

  it("keeps the rows and shows a quiet error when a refresh fails, then recovers by itself", async () => {
    let calls = 0;
    const api = installFakeApi(signedIn(() => (++calls === 2 ? "network-error" : json(200, page([listItem()])))));
    renderApp("/requests");
    await screen.findByText(REF);

    await advance(LIST_POLL_MS + MARGIN);
    const note = await screen.findByText(/could not refresh/i);
    expect(note).toBeInTheDocument();
    expect(note.closest("[role=alert]")).toBeNull(); // not an interruption
    expect(screen.getByText(REF)).toBeInTheDocument();

    await advance(LIST_POLL_MS * 2 + MARGIN); // the retry waits longer (back-off)
    expect(listCalls(api)).toHaveLength(3);
    expect(screen.queryByText(/could not refresh/i)).not.toBeInTheDocument();
  });

  it("refreshes at once, in place, on the Refresh button", async () => {
    const api = installFakeApi(signedIn(() => json(200, page([listItem()]))));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp("/requests");
    await screen.findByText(REF);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await advance(0);
    expect(listCalls(api)).toHaveLength(2);
    expect(screen.queryByText("Loading requests…")).not.toBeInTheDocument();
  });
});

describe("notifications", () => {
  it("tells the user once when a request moves from processing to needs review, without any request content", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let state: "queued" | "needs_review" = "queued";
    installFakeApi(signedIn(() => json(200, page([listItem({ state })]))));
    renderApp("/requests");
    await screen.findByText(REF);
    expect(screen.queryByText(/ready for review/)).not.toBeInTheDocument(); // the first data is a baseline

    state = "needs_review";
    await advance(LIST_POLL_MS + MARGIN);
    const toast = await screen.findByText(`Request ${REF} is ready for review.`);
    expect(toast).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    await advance(LIST_POLL_MS * 3);
    expect(screen.queryByText(/ready for review/)).not.toBeInTheDocument(); // later refreshes with the same state say nothing
  });

  it("does not notify for a request that was already waiting for review when the page opened", async () => {
    installFakeApi(signedIn(() => json(200, page([listItem({ state: "needs_review" })]))));
    renderApp("/requests");
    await screen.findByText(REF);
    await advance(LIST_POLL_MS * 3);
    expect(screen.queryByText(/ready for review/)).not.toBeInTheDocument();
  });

  it("reports a failure once, as an alert", async () => {
    let state: "extracting" | "failed" = "extracting";
    installFakeApi(signedIn(() => json(200, page([listItem({ state, failed_step: state === "failed" ? "extract" : "" })]))));
    renderApp("/requests");
    await screen.findByText(REF);
    state = "failed";
    await advance(LIST_POLL_MS + MARGIN);
    const toast = await screen.findByText(`Request ${REF} failed during processing.`);
    expect(toast.closest("[role=alert]")).not.toBeNull();
    await advance(LIST_POLL_MS * 3);
    expect(screen.getAllByText(/failed during processing/)).toHaveLength(1);
  });

  it("also notifies on the detail page, once, and does not repeat after later refreshes", async () => {
    const queued = detail({ state: "queued", processing: { step: "extract", task_state: "queued", attempt_count: 0, failure_code: null }, outcome_status: null, current_revision: null, draft: null, draft_source: null });
    const server = fakeServer(queued);
    installFakeApi(signedIn(server.handler));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);

    server.setDetail(detail());
    await advance(ACTIVE_POLL_MS + MARGIN);
    expect(await screen.findByText(`Request ${REF} is ready for review.`)).toBeInTheDocument();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const region = within(screen.getByRole("region", { name: "Notifications" }));
    expect(region.queryByText(/Synthetic Requester/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    await advance(REVIEW_POLL_MS * 3);
    expect(screen.queryByText(/ready for review/)).not.toBeInTheDocument();
  });
});

describe("request detail: background refresh", () => {
  it("shows when the page was last updated", async () => {
    installFakeApi(signedIn(fakeServer(detail()).handler));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: new RegExp(REF) });
    expect(screen.getByText(/^Updated/)).toBeInTheDocument();
  });

  it("aborts the refresh in flight when the user navigates away", async () => {
    const pending = deferred<FakeResponse>();
    let gets = 0;
    const api = installFakeApi(
      signedIn((call) => {
        if (call.path === `/api/requests/${RUN_ID}/`) return ++gets === 2 ? pending.promise : json(200, detail());
        return json(200, page([listItem()]));
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: new RegExp(REF) });
    await advance(REVIEW_POLL_MS + MARGIN);
    expect(detailCalls(api)).toHaveLength(2);

    await user.click(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByRole("link", { name: "Requests" }));
    expect(await screen.findByRole("heading", { name: "Requests", level: 1 })).toBeInTheDocument();
    expect(signalOf(api, `/api/requests/${RUN_ID}/`, 1).aborted).toBe(true);

    pending.resolve(json(200, detail({ state: "failed" })));
    await advance(60_000);
    expect(detailCalls(api)).toHaveLength(2); // nothing polls the old route any more
  });

  it("never overwrites unsaved edits when the page refreshes", async () => {
    const server = fakeServer(inReview());
    installFakeApi(signedIn(server.handler));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Edit draft" }));
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "My unsaved name");

    server.setDetail(inReview({ rejection_count: 2 })); // same revision, something else changed
    await advance(REVIEW_POLL_MS + MARGIN);

    const facts = await screen.findByText("0 / 2"); // the page did refresh
    expect(facts).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("My unsaved name");
    expect(screen.getByRole("button", { name: "Save as new revision" })).toBeEnabled();
    expect(screen.queryByText(/changed while you were editing/)).not.toBeInTheDocument();
  });

  describe("a newer revision appears while editing", () => {
    const colleague: Draft = {
      requester: { name: "Colleague Edit", contact: null },
      items: [{ description: "Colleague widget", quantity: "1", unit: null, notes: null }],
      notes: null,
      source_refs: [],
    };
    const newer = () => inReview({ current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" }, draft: colleague });
    const saved = { revision_id: "66666666-6666-4666-8666-666666666666", revision_number: 3, content_hash: HASH, run_state: "needs_review", warnings: [], approval_invalidated: false };

    it("keeps the user's text, warns, blocks saving, and sends nothing until the user chooses", async () => {
      const server = fakeServer(inReview());
      const api = installFakeApi(signedIn(server.handler));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderApp(`/requests/${RUN_ID}`);
      await user.click(await screen.findByRole("button", { name: "Edit draft" }));
      await user.clear(screen.getByLabelText("Name"));
      await user.type(screen.getByLabelText("Name"), "My unsaved name");

      server.setDetail(newer());
      await advance(REVIEW_POLL_MS + MARGIN);

      const alert = await screen.findByText(/changed while you were editing/);
      expect(alert.closest("[role=alert]")).toHaveTextContent("It is now at revision 2");
      expect(screen.getByLabelText("Name")).toHaveValue("My unsaved name");
      expect(screen.getByLabelText("Name")).toBeDisabled();
      expect(screen.getByRole("button", { name: "Save as new revision" })).toBeDisabled();
      expect(api.callsTo("POST", `/api/requests/${RUN_ID}/edit/`)).toHaveLength(0);

      // Later refreshes of the same newer revision change nothing.
      await advance(REVIEW_POLL_MS * 2);
      expect(screen.getByLabelText("Name")).toHaveValue("My unsaved name");
      expect(screen.getByText(/changed while you were editing/)).toBeInTheDocument();
    });

    it("'keep my version' bases the save on the newer revision and the page's older data does not block it", async () => {
      const server = fakeServer(inReview(), (call) => (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/edit/` ? json(200, saved) : apiError(404, "not_found")));
      const api = installFakeApi(signedIn(server.handler));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderApp(`/requests/${RUN_ID}`);
      await user.click(await screen.findByRole("button", { name: "Edit draft" }));
      await user.clear(screen.getByLabelText("Name"));
      await user.type(screen.getByLabelText("Name"), "My unsaved name");
      server.setDetail(newer());
      await advance(REVIEW_POLL_MS + MARGIN);
      await screen.findByText(/changed while you were editing/);

      await user.click(screen.getByRole("button", { name: "Keep my version and base it on the latest" }));
      expect(screen.getByText(/will REPLACE the content of that revision/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save as new revision" })).toBeEnabled();

      await user.click(screen.getByRole("button", { name: "Save as new revision" }));
      expect(await screen.findByText("Saved as revision 3.")).toBeInTheDocument();
      const post = api.callsTo("POST", `/api/requests/${RUN_ID}/edit/`)[0]!;
      expect((post.body as { base_revision_id: string }).base_revision_id).toBe(REV2);
      expect(post.headers["x-csrftoken"]).toBe("csrf-test-token");
    });

    it("'discard' closes the editor and shows the newer revision", async () => {
      const server = fakeServer(inReview());
      installFakeApi(signedIn(server.handler));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderApp(`/requests/${RUN_ID}`);
      await user.click(await screen.findByRole("button", { name: "Edit draft" }));
      await user.type(screen.getByLabelText("Name"), " typed");
      server.setDetail(newer());
      await advance(REVIEW_POLL_MS + MARGIN);
      await screen.findByText(/changed while you were editing/);

      await user.click(screen.getByRole("button", { name: "Discard my changes and load the latest" }));
      expect(await screen.findByText("Colleague Edit")).toBeInTheDocument();
      expect(screen.queryByRole("form", { name: /Edit draft/ })).not.toBeInTheDocument();
    });

    it("quietly loads the newer revision into a form the user has not touched", async () => {
      const server = fakeServer(inReview());
      installFakeApi(signedIn(server.handler));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderApp(`/requests/${RUN_ID}`);
      await user.click(await screen.findByRole("button", { name: "Edit draft" }));
      server.setDetail(newer());
      await advance(REVIEW_POLL_MS + MARGIN);

      expect(await screen.findByText(/Updated to revision 2/)).toBeInTheDocument();
      const form = screen.getByRole("form", { name: /Edit draft/ });
      expect(within(form).getByLabelText("Name")).toHaveValue("Colleague Edit");
      expect(within(form).getByText(/based on revision 2/)).toBeInTheDocument();
      expect(screen.queryByText(/changed while you were editing/)).not.toBeInTheDocument();
    });
  });

  it("after an approval, refreshes at once and ignores an older refresh that was still in flight", async () => {
    const stale = deferred<FakeResponse>();
    let current: RequestDetail = inReview({ submitted_by: USER_ID, allowed_actions: ["approve", "reject"] });
    let gets = 0;
    const handler: Handler = (call) => {
      if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) {
        gets += 1;
        if (gets === 2) {
          const old = current; // what the server said when this refresh was sent
          return stale.promise.then(() => json(200, old));
        }
        return json(200, current);
      }
      if (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/approve/`) {
        current = detail({ state: "approved", allowed_actions: [], approval_count: 1 });
        return json(200, { approval_id: "a1", revision_id: REV1, content_hash: HASH, run_state: "approved", acknowledged_warnings: [] });
      }
      return apiError(404, "not_found");
    };
    const api = installFakeApi(signedIn(handler, approver()));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("button", { name: "Approve revision 1…" });

    await advance(REVIEW_POLL_MS + MARGIN); // a background refresh is now waiting for the server
    expect(gets).toBe(2);

    await user.click(screen.getByRole("button", { name: "Approve revision 1…" }));
    await user.click(within(screen.getByRole("group", { name: "Approve revision 1?" })).getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText("Revision 1 approved.")).toBeInTheDocument();
    expect(api.callsTo("POST", `/api/requests/${RUN_ID}/approve/`)).toHaveLength(1);
    expect(await screen.findByText("Approved", { selector: ".badge" })).toBeInTheDocument();
    expect(gets).toBe(3); // loaded again straight after the action, without waiting for the interval
    expect(signalOf(api, `/api/requests/${RUN_ID}/`, 1).aborted).toBe(true);

    stale.resolve(json(200, current)); // the old answer arrives late and is thrown away
    await advance(MARGIN);
    expect(screen.getByText("Approved", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.queryByText("Needs review", { selector: ".badge" })).not.toBeInTheDocument();
  });

  it("shows a quiet error on a failed refresh and keeps the page usable", async () => {
    let gets = 0;
    installFakeApi(signedIn((call) => (call.path === `/api/requests/${RUN_ID}/` ? (++gets === 2 ? "network-error" : json(200, detail())) : apiError(404, "not_found"))));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: new RegExp(REF) });
    await advance(REVIEW_POLL_MS + MARGIN);
    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: new RegExp(REF) })).toBeInTheDocument();
  });
});

