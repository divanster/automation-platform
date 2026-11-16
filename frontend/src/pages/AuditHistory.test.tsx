import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { HASH, OTHER_ID, RUN_ID, USER_ID, detail } from "../test/fixtures";
import { APPROVER_ID, REV1, approver, auditEvent, auditPage, auditor, fakeServer, inReview } from "../test/review";
import { renderApp } from "../test/renderApp";
import { signedIn } from "../test/server";

const auditCalls = (api: ReturnType<typeof installFakeApi>) => api.callsTo("GET", `/api/requests/${RUN_ID}/audit/`);

const events = () => [
  auditEvent({ id: 1, action: "request.submitted", occurred_at: "2026-01-01T10:00:00Z" }),
  auditEvent({ id: 2, action: "revision.created", actor_kind: "system", actor_public_id: null, subject_type: "revision", details: { revision_number: 1, content_hash: HASH, origin: "extraction", blocking_count: 0, warning_count: 1 } }),
  auditEvent({ id: 3, action: "run.state_changed", actor_kind: "system", actor_public_id: null, subject_type: "run", details: { from_state: "extracting", to_state: "needs_review", cause: "extraction" } }),
  auditEvent({ id: 4, action: "approval.recorded", actor_public_id: APPROVER_ID, subject_type: "approval", details: { revision_id: REV1, content_hash: HASH, warning_count: 1 } }),
  auditEvent({ id: 5, action: "rejection.recorded", actor_public_id: OTHER_ID, subject_type: "rejection", details: { revision_id: REV1, content_hash: HASH, reason_code: "incorrect_content" } }),
];

describe("who sees the audit history", () => {
  it("is not shown, and never requested, for a user without the audit permission", async () => {
    const api = installFakeApi(signedIn(fakeServer(inReview(), undefined, auditPage(events())).handler));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(screen.queryByRole("heading", { name: "Audit history" })).not.toBeInTheDocument();
    expect(auditCalls(api)).toHaveLength(0);
  });

  it("is shown to a user who holds audit.view_auditevent", async () => {
    installFakeApi(signedIn(fakeServer(detail(), undefined, auditPage(events())).handler, auditor()));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByRole("heading", { name: "Audit history" })).toBeInTheDocument();
  });

  it("still relies on the server: a 403 from the audit endpoint is shown without a retry button", async () => {
    const server: Handler = (call) => (call.path === `/api/requests/${RUN_ID}/audit/` ? apiError(403, "permission_denied") : fakeServer(detail()).handler(call));
    installFakeApi(signedIn(server, auditor()));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Audit history" });
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Not available for your account");
    expect(within(panel).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("rendering events", () => {
  async function render(user = auditor()) {
    const api = installFakeApi(signedIn(fakeServer(detail(), undefined, auditPage(events())).handler, user));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Audit history" });
    await within(panel).findByText("Request submitted");
    return { api, panel };
  }

  it("shows the action, time, actor and details of each event, in the order the server sent them", async () => {
    const { panel } = await render();
    const rows = within(panel).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    const labels = rows.map((row) => within(row).getAllByRole("cell")[1]!.textContent);
    expect(labels[0]).toContain("Request submitted");
    expect(labels[1]).toContain("Revision created");
    expect(labels[2]).toContain("State changed");
    expect(labels[3]).toContain("Approved");
    expect(labels[4]).toContain("Rejected");
    for (const row of rows) expect(within(row).getAllByRole("cell")[0]!.textContent).toMatch(/2026|Jan/);
  });

  it("shows the revision reference when the event has one", async () => {
    const { panel } = await render();
    const created = within(panel).getAllByRole("row")[2]!;
    expect(within(created).getByText("Revision").nextElementSibling).toHaveTextContent("1");
    const approved = within(panel).getAllByRole("row")[4]!;
    expect(within(approved).getByText("Revision id")).toBeInTheDocument();
    expect(within(approved).getByText(REV1.slice(0, 8))).toBeInTheDocument();
  });

  it("shows state changes and reason codes as the server's tokens", async () => {
    const { panel } = await render();
    expect(within(panel).getByText("extracting")).toBeInTheDocument();
    expect(within(panel).getByText("needs_review")).toBeInTheDocument();
    expect(within(panel).getByText("incorrect_content")).toBeInTheDocument();
  });

  it("identifies actors by what the API sends: 'System', 'You', or a short public id, never a made-up name", async () => {
    const { panel } = await render(auditor({ public_id: APPROVER_ID }));
    const rows = within(panel).getAllByRole("row").slice(1);
    const actor = (i: number) => within(rows[i]!).getAllByRole("cell")[2]!;
    expect(actor(0)).toHaveTextContent(USER_ID.slice(0, 8)); // another user: the first group of the public id
    expect(actor(0)).toHaveTextContent(/^[0-9a-f]{8}$/);
    expect(actor(1)).toHaveTextContent("System");
    expect(actor(3)).toHaveTextContent("You"); // the signed-in auditor
    expect(actor(4)).toHaveTextContent(OTHER_ID.slice(0, 8));
    expect(panel).not.toHaveTextContent(/synthetic\.(operator|approver|auditor)/);
    expect(actor(0).querySelector("span")).toHaveAttribute("title", USER_ID); // the full id is available on hover
  });

  it("shows an unknown action and unknown detail keys as the raw codes instead of hiding or inventing them", async () => {
    installFakeApi(
      signedIn(fakeServer(detail(), undefined, auditPage([auditEvent({ action: "future.action", details: { brand_new_key: "token-1", flag: false, nothing: null } })])).handler, auditor()),
    );
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Audit history" });
    expect(await within(panel).findByText("future.action")).toBeInTheDocument();
    expect(within(panel).getByText("brand_new_key")).toBeInTheDocument();
    expect(within(panel).getByText("token-1")).toBeInTheDocument();
    expect(within(panel).getByText("no")).toBeInTheDocument();
    expect(within(panel).getByText("—")).toBeInTheDocument();
  });

  it("renders event content as plain text", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1">';
    installFakeApi(signedIn(fakeServer(detail(), undefined, auditPage([auditEvent({ action: hostile, details: { cause: hostile } })])).handler, auditor()));
    const { container } = renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("region", { name: "Audit history" });
    await screen.findAllByText(hostile);
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("loading, paging and errors", () => {
  it("says so when there are no events", async () => {
    installFakeApi(signedIn(fakeServer(detail(), undefined, auditPage([])).handler, auditor()));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByText("No audit events are recorded for this request.")).toBeInTheDocument();
  });

  it("pages through the events with the API's page parameter", async () => {
    const handler: Handler = (call) =>
      call.path === `/api/requests/${RUN_ID}/audit/` ? json(200, auditPage([auditEvent({ id: Number(call.query.get("page")) })], { count: 45, page: Number(call.query.get("page")) })) : fakeServer(detail()).handler(call);
    const api = installFakeApi(signedIn(handler, auditor()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByText("Page 1 of 3 (45 events)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page 2 of 3 (45 events)")).toBeInTheDocument();
    expect(auditCalls(api).at(-1)?.query.get("page")).toBe("2");
    expect(Object.fromEntries(auditCalls(api)[0]!.query)).toEqual({ page: "1", page_size: "20" });
  });

  it("shows a server error with a working retry", async () => {
    let fail = true;
    const handler: Handler = (call) => (call.path === `/api/requests/${RUN_ID}/audit/` ? (fail ? apiError(500, "internal_error") : json(200, auditPage(events()))) : fakeServer(detail()).handler(call));
    const user = userEvent.setup();
    installFakeApi(signedIn(handler, auditor()));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Audit history" });
    expect(await within(panel).findByRole("alert")).toHaveTextContent(/internal error/i);
    fail = false;
    await user.click(within(panel).getByRole("button", { name: "Try again" }));
    expect(await within(panel).findByText("Request submitted")).toBeInTheDocument();
  });

  it("is loaded again after an action, so new events appear", async () => {
    // A superuser holds every permission: it may approve and read the audit history.
    const profile = approver({ is_superuser: true, permissions: ["intake.view_any_request", "intake.approve_request", "intake.reject_request", "audit.view_auditevent"] });
    const server = fakeServer(inReview({ allowed_actions: ["approve"] }), (call) => (call.method === "POST" ? json(200, { approval_id: "a", revision_id: REV1, content_hash: HASH, run_state: "approved", acknowledged_warnings: [] }) : apiError(404, "not_found")), auditPage(events()));
    const api = installFakeApi(signedIn(server.handler, profile));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    const before = auditCalls(api).length;
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    await screen.findByText("Revision 1 approved.");
    expect(auditCalls(api).length).toBeGreaterThan(before);
  });
});
