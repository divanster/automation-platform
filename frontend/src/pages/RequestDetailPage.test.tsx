import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RequestDetail } from "../api/types";
import { FAST_POLL_COUNT, FAST_POLL_MS, SLOW_POLL_MS } from "../lib/useRequestDetail";
import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { HASH, OTHER_ID, RUN_ID, USER_ID, detail } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { signedIn } from "../test/server";

const queued = () =>
  detail({
    state: "queued",
    processing: { step: "extract", task_state: "queued", attempt_count: 0, failure_code: null },
    outcome_status: null,
    current_revision: null,
    draft: null,
    draft_source: null,
  });

/** Serves `sequence` one entry per detail fetch; the last entry repeats. */
function sequence(...items: Array<RequestDetail | "network-error">) {
  let index = 0;
  const handler: Handler = (call) => {
    if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) {
      const item = items[Math.min(index, items.length - 1)]!;
      index += 1;
      return item === "network-error" ? item : json(200, item);
    }
    return apiError(404, "not_found");
  };
  return handler;
}

function detailCalls(api: ReturnType<typeof installFakeApi>) {
  return api.callsTo("GET", `/api/requests/${RUN_ID}/`).length;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("status polling", () => {
  it("polls while queued and extracting, shows each state, and stops when the state is stable", async () => {
    const api = installFakeApi(signedIn(sequence(queued(), detail({ ...queued(), state: "extracting", processing: { step: "extract", task_state: "running", attempt_count: 1, failure_code: null } }), detail())));
    renderApp(`/requests/${RUN_ID}`);

    expect(await screen.findByText(/queued\. the request waits/i)).toBeInTheDocument();
    expect(detailCalls(api)).toBe(1);

    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(await screen.findByText(/extracting…/i)).toBeInTheDocument();
    expect(detailCalls(api)).toBe(2);

    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(await screen.findByText("Needs review", { selector: ".badge" })).toBeInTheDocument();
    expect(detailCalls(api)).toBe(3);

    await vi.advanceTimersByTimeAsync(5 * 60 * 1000); // five minutes in a stable state: no more requests
    expect(detailCalls(api)).toBe(3);
  });

  it.each(["needs_review", "approved", "exported", "failed", "cancelled"] as const)("does not poll a request that is already %s", async (state) => {
    const api = installFakeApi(
      signedIn(sequence(detail({ state, failed_step: state === "failed" ? "extract" : "", processing: { step: "extract", task_state: state === "failed" ? "failed" : "succeeded", attempt_count: 3, failure_code: state === "failed" ? "handler_error" : null } }))),
    );
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(detailCalls(api)).toBe(1);
  });

  it("slows down after the first stretch of fast polls, and keeps going while still processing", async () => {
    const api = installFakeApi(signedIn(sequence(queued())));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);

    await vi.advanceTimersByTimeAsync(FAST_POLL_MS * FAST_POLL_COUNT);
    const afterFast = detailCalls(api);
    expect(afterFast).toBe(FAST_POLL_COUNT + 1);

    await vi.advanceTimersByTimeAsync(SLOW_POLL_MS * 3);
    expect(detailCalls(api)).toBe(afterFast + 3);
  });

  it("stops polling when the user leaves the page", async () => {
    const api = installFakeApi(signedIn(sequence(queued())));
    const { unmount } = renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);
    unmount();
    const before = detailCalls(api);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(detailCalls(api)).toBe(before);
  });

  it("keeps the last good data, reports the failed refresh, stops polling, and resumes on request", async () => {
    const api = installFakeApi(signedIn(sequence(queued(), "network-error", detail())));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);

    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getByText(/queued\. the request waits/i)).toBeInTheDocument(); // last good data still shown
    await vi.advanceTimersByTimeAsync(60_000);
    expect(detailCalls(api)).toBe(2); // polling stopped

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Needs review", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.queryByText(/could not refresh/i)).not.toBeInTheDocument();
  });

  it("does not fetch while the tab is hidden and continues when it is visible again", async () => {
    let hidden = false;
    vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    const api = installFakeApi(signedIn(sequence(queued())));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);

    hidden = true;
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS * 5);
    expect(detailCalls(api)).toBe(1);

    hidden = false;
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS * 2);
    expect(detailCalls(api)).toBeGreaterThan(1);
  });

  it("returns to the login when a poll finds the session gone", async () => {
    let expired = false;
    installFakeApi(
      signedIn((call) => {
        if (call.path === `/api/requests/${RUN_ID}/`) return expired ? apiError(403, "not_authenticated") : json(200, queued());
        return apiError(404, "not_found");
      }),
    );
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByText(/queued\. the request waits/i);
    expired = true;
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText(/your session has ended/i)).toBeInTheDocument();
  });
});

describe("what the detail shows", () => {
  it("shows the extraction outcome, current revision and the full content hash", async () => {
    installFakeApi(signedIn(sequence(detail())));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });

    const revision = screen.getByRole("region", { name: "Current revision" });
    expect(within(revision).getByText("1")).toBeInTheDocument();
    expect(within(revision).getByText(HASH)).toBeInTheDocument();
    expect(screen.getByText("valid")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
  });

  it("prints decimal quantities exactly as the API sent them", async () => {
    const quantity = "12345678901234567890.123456789";
    const base = detail();
    installFakeApi(signedIn(sequence(detail({ draft: { ...base.draft!, items: [{ description: "Big", quantity, unit: "kg", notes: null }, { description: "Fine", quantity: "2.50", unit: null, notes: null }] } }))));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByText(quantity)).toBeInTheDocument();
    expect(screen.getByText("2.50")).toBeInTheDocument();
  });

  it("renders findings as text and never as HTML", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
    installFakeApi(
      signedIn(
        sequence(
          detail({
            findings: [
              { code: "value_not_in_source", field_path: "items[0].description", blocking: false, message: hostile, excerpt: hostile },
              { code: "missing_required", field_path: "requester.name", blocking: true, message: "A value is required.", excerpt: null },
            ],
            draft: { requester: { name: hostile, contact: null }, items: [], notes: hostile, source_refs: [{ field_path: "notes", excerpt: hostile, line: 2 }] },
          }),
        ),
      ),
    );
    const { container } = renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(screen.getAllByText(hostile, { exact: false }).length).toBeGreaterThanOrEqual(3);
  });

  it("separates blocking findings from warnings and shows the stable code and field path", async () => {
    installFakeApi(
      signedIn(
        sequence(
          detail({
            findings: [
              { code: "duplicate_item", field_path: "items[1]", blocking: false, message: "Possible duplicate.", excerpt: null },
              { code: "missing_required", field_path: "requester.name", blocking: true, message: "A value is required.", excerpt: "Requester:" },
            ],
          }),
        ),
      ),
    );
    renderApp(`/requests/${RUN_ID}`);
    const blocking = await screen.findByRole("region", { name: "Blocking findings" });
    expect(within(blocking).getByText("missing_required")).toBeInTheDocument();
    expect(within(blocking).getByText("requester.name")).toBeInTheDocument();
    expect(within(blocking).getByText("Requester:")).toBeInTheDocument();
    const warnings = screen.getByRole("region", { name: "Warnings" });
    expect(within(warnings).getByText("duplicate_item")).toBeInTheDocument();
  });

  it("explains an invalid extraction: no revision, the draft comes from the extraction result", async () => {
    installFakeApi(
      signedIn(
        sequence(
          detail({
            outcome_status: "invalid",
            current_revision: null,
            draft_source: "extraction_outcome",
            findings: [{ code: "missing_required", field_path: "items", blocking: true, message: "At least one item is required.", excerpt: null }],
          }),
        ),
      ),
    );
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByText(/no revision was created/i)).toBeInTheDocument();
    expect(screen.getByText(/from the extraction result/i)).toBeInTheDocument();
    expect(screen.getByText("invalid")).toBeInTheDocument();
  });

  it("shows why a failed request failed, using only the safe codes", async () => {
    installFakeApi(
      signedIn(
        sequence(
          detail({
            state: "failed",
            failed_step: "extract",
            outcome_status: null,
            current_revision: null,
            draft: null,
            draft_source: null,
            processing: { step: "extract", task_state: "failed", attempt_count: 3, failure_code: "handler_error" },
          }),
        ),
      ),
    );
    renderApp(`/requests/${RUN_ID}`);
    const alert = await screen.findByText(/processing failed/i);
    expect(alert).toHaveTextContent("extract");
    expect(alert).toHaveTextContent("handler_error");
    expect(screen.getByText("There is no draft yet.")).toBeInTheDocument();
  });

  it("shows another user's request with a short id for the submitter", async () => {
    installFakeApi(signedIn(sequence(detail({ submitted_by: OTHER_ID }))));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByText("22222222")).toBeInTheDocument();
    expect(screen.queryByText("You")).not.toBeInTheDocument();
  });
});

describe("allowed actions come from the backend", () => {
  it("lists what the server allows and why other actions are blocked, and offers no review buttons yet", async () => {
    installFakeApi(signedIn(sequence(detail({ allowed_actions: ["reject"], action_blockers: { approve: "self_approval_forbidden" }, submitted_by: USER_ID }))));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Your available actions" });
    expect(within(panel).getByText("Reject")).toBeInTheDocument();
    expect(within(panel).getByText(/Approve is not possible now: you submitted or edited this request/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|reject|edit/i })).not.toBeInTheDocument();
  });

  it("shows 'None right now' when the server allows nothing, and an unknown code as it is", async () => {
    installFakeApi(signedIn(sequence(detail({ allowed_actions: [], action_blockers: { approve: "some_future_reason" } }))));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Your available actions" });
    expect(within(panel).getByText("None right now.")).toBeInTheDocument();
    expect(within(panel).getByText(/some_future_reason/)).toBeInTheDocument();
  });
});

describe("detail errors", () => {
  it("shows not found for an unknown or hidden request (the API answers 404 either way)", async () => {
    installFakeApi(signedIn(() => apiError(404, "not_found")));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });

  it("does not call the API for an id that is not a UUID", async () => {
    const api = installFakeApi(signedIn(() => apiError(404, "not_found")));
    renderApp("/requests/not-a-uuid");
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
    expect(api.calls.filter((c) => c.path.startsWith("/api/requests"))).toHaveLength(0);
  });

  it("shows a server error with a retry that loads the request", async () => {
    let fail = true;
    installFakeApi(signedIn((call) => (call.path === `/api/requests/${RUN_ID}/` ? (fail ? apiError(500, "internal_error") : json(200, detail())) : apiError(404, "not_found"))));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(/internal error/i);
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ })).toBeInTheDocument();
  });

  it("shows a permission message for 403 permission_denied", async () => {
    installFakeApi(signedIn(() => apiError(403, "permission_denied")));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission to do this.");
  });
});
