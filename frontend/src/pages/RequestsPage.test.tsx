import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { OTHER_ID, RUN_ID, USER_ID, listItem, operator, page } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";

const SECOND_REF = "REQ-20260102-BBBB0002";

describe("request list", () => {
  it("renders the rows from the API with state badges, links and who submitted them", async () => {
    installFakeApi(
      signedIn(() =>
        json(
          200,
          page([
            listItem(),
            listItem({ run_id: OTHER_ID, reference: SECOND_REF, state: "failed", failed_step: "extract", current_revision_number: null, outcome_status: null, submitted_by: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }),
          ]),
        ),
      ),
    );
    renderApp("/requests");

    const link = await screen.findByRole("link", { name: "REQ-20260101-AAAA0001" });
    expect(link).toHaveAttribute("href", `/requests/${RUN_ID}`);
    const rows = screen.getAllByRole("row");
    const first = within(rows[1]!);
    expect(first.getByText("Needs review")).toHaveAttribute("data-state", "needs_review");
    expect(first.getByText("You")).toBeInTheDocument(); // submitted_by equals the signed-in public_id
    const second = within(rows[2]!);
    expect(second.getByText("Failed")).toHaveAttribute("data-state", "failed");
    expect(second.getByText(/at extract/)).toBeInTheDocument();
    expect(second.getByText("bbbbbbbb")).toBeInTheDocument(); // someone else: short id only
  });

  it("asks for the first page of 20 with no state filter by default", async () => {
    const api = installFakeApi(signedIn(() => json(200, page([listItem()]))));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    const call = api.callsTo("GET", "/api/requests/")[0]!;
    expect(Object.fromEntries(call.query)).toEqual({ page: "1", page_size: "20" });
  });

  it("filters by state through the API and resets to the first page", async () => {
    const handler: Handler = (call) =>
      call.query.get("state") === "failed" ? json(200, page([listItem({ reference: SECOND_REF, state: "failed", failed_step: "extract" })])) : json(200, page([listItem()], { count: 45 }));
    const api = installFakeApi(signedIn(handler));
    const user = userEvent.setup();
    renderApp("/requests?page=2");
    await screen.findByText("REQ-20260101-AAAA0001");

    await user.selectOptions(screen.getByLabelText("State"), "failed");
    expect(await screen.findByText(SECOND_REF)).toBeInTheDocument();
    const last = api.callsTo("GET", "/api/requests/").at(-1)!;
    expect(Object.fromEntries(last.query)).toEqual({ page: "1", page_size: "20", state: "failed" });
  });

  it("offers every run state the API knows in the filter", async () => {
    installFakeApi(signedIn(() => json(200, page([listItem()]))));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    const options = within(screen.getByLabelText("State")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value);
    expect(options).toEqual(["", "queued", "extracting", "needs_review", "approved", "exporting", "exported", "failed", "cancelled"]);
  });

  it("pages with Next and Previous and shows the totals", async () => {
    const api = installFakeApi(signedIn((call) => json(200, page([listItem()], { count: 45, page: Number(call.query.get("page")) }))));
    const user = userEvent.setup();
    renderApp("/requests");
    expect(await screen.findByText("Page 1 of 3 (45 requests)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page 2 of 3 (45 requests)")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/requests/").at(-1)?.query.get("page")).toBe("2");

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page 3 of 3 (45 requests)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("never sends an invalid page or state from the address bar to the API", async () => {
    const api = installFakeApi(signedIn(() => json(200, page([listItem()]))));
    renderApp("/requests?page=-3&state=not_a_state");
    await screen.findByText("REQ-20260101-AAAA0001");
    expect(Object.fromEntries(api.callsTo("GET", "/api/requests/")[0]!.query)).toEqual({ page: "1", page_size: "20" });
  });

  it("says so when there are no requests, and offers to clear a filter that matches nothing", async () => {
    installFakeApi(signedIn(() => json(200, page([], { count: 0 }))));
    const user = userEvent.setup();
    renderApp("/requests");
    expect(await screen.findByText("There are no requests yet.")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("State"), "approved");
    expect(await screen.findByText(/No requests match/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByText("There are no requests yet.")).toBeInTheDocument();
  });

  it("shows a permission message without a retry button when the backend answers 403 permission_denied", async () => {
    installFakeApi(signedIn(() => apiError(403, "permission_denied"), operator({ permissions: [], roles: [] })));
    renderApp("/requests");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Not available for your account");
    expect(alert).toHaveTextContent("You do not have permission to do this.");
    expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows a server error with a working retry", async () => {
    let fail = true;
    const api = installFakeApi(signedIn(() => (fail ? apiError(500, "internal_error") : json(200, page([listItem()])))));
    const user = userEvent.setup();
    renderApp("/requests");
    expect(await screen.findByRole("alert")).toHaveTextContent(/internal error/i);

    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/requests/")).toHaveLength(2);
  });

  it("ignores a slow response for an old filter when a newer one has already been shown", async () => {
    const slow = deferred<ReturnType<typeof json>>();
    installFakeApi(
      signedIn((call) => {
        if (call.query.get("state") === "queued") return slow.promise;
        if (call.query.get("state") === "failed") return json(200, page([listItem({ reference: SECOND_REF, state: "failed", failed_step: "extract" })]));
        return json(200, page([listItem()]));
      }),
    );
    const user = userEvent.setup();
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");

    await user.selectOptions(screen.getByLabelText("State"), "queued"); // slow, still pending
    await user.selectOptions(screen.getByLabelText("State"), "failed"); // answers first
    expect(await screen.findByText(SECOND_REF)).toBeInTheDocument();

    slow.resolve(json(200, page([listItem({ reference: "REQ-STALE-0000" })])));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("REQ-STALE-0000")).not.toBeInTheDocument();
    expect(screen.getByText(SECOND_REF)).toBeInTheDocument();
  });

  it("links to the new-request form only for users who may submit", async () => {
    installFakeApi(signedIn(() => json(200, page([listItem()]))));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    expect(screen.getAllByRole("link", { name: "New request" }).length).toBeGreaterThan(0);
  });

  it("uses the signed-in user's id to label their own rows", async () => {
    installFakeApi(signedIn(() => json(200, page([listItem({ submitted_by: USER_ID })]))));
    renderApp("/requests");
    expect(await screen.findByText("You")).toBeInTheDocument();
  });
});
