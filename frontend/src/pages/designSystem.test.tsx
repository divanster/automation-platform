import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RUN_STATES } from "../api/types";
import { StateBadge } from "../components/StateBadge";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { RUN_ID, USER_ID, listItem, operator as reviewOperator, page } from "../test/fixtures";
import { approver, fakeServer, inReview } from "../test/review";
import { renderApp } from "../test/renderApp";
import { signedIn } from "../test/server";
import { stateLabel } from "../lib/format";

describe("request status rendering", () => {
  it.each(RUN_STATES)("renders %s as a labelled badge that does not rely on colour", (state) => {
    render(<StateBadge state={state} />);
    const badge = screen.getByText(stateLabel(state));
    expect(badge).toHaveClass("badge", `badge-${state}`);
    expect(badge).toHaveAttribute("data-state", state);
    expect(badge.textContent?.trim()).not.toBe("");
  });

  it("shows an unknown state as text instead of failing", () => {
    render(<StateBadge state="something_new" />);
    const badge = screen.getByText(stateLabel("something_new"));
    expect(badge).toHaveClass("badge-unknown");
    expect(badge).toHaveAttribute("data-state", "something_new");
  });

  it("renders every state in the requests table with the matching row action", async () => {
    const items = RUN_STATES.map((state, i) =>
      listItem({ run_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`, reference: `REQ-STATE-${String(i).padStart(4, "0")}`, state }),
    );
    installFakeApi(signedIn((call) => (call.path === "/api/requests/" ? json(200, page(items)) : apiError(404, "not_found"))));
    renderApp("/requests");
    await screen.findByText("REQ-STATE-0000");
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(RUN_STATES.length);
    rows.forEach((row, i) => {
      const state = RUN_STATES[i]!;
      expect(within(row).getByText(stateLabel(state), { selector: ".badge" })).toHaveAttribute("data-state", state);
      const action = within(row).getByRole("link", { name: new RegExp(`^(Review|View) REQ-STATE-${String(i).padStart(4, "0")}$`) });
      expect(action).toHaveTextContent(state === "needs_review" ? "Review" : "View");
    });
  });

  it("marks the active quick filter and filters by the same state parameter as the select", async () => {
    const api = installFakeApi(signedIn((call) => (call.path === "/api/requests/" ? json(200, page([listItem()])) : apiError(404, "not_found"))));
    renderApp("/requests?state=failed");
    await screen.findByText("REQ-20260101-AAAA0001");
    const group = within(screen.getByRole("group", { name: "Quick filters" }));
    expect(group.getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    expect(group.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "false");
    expect(api.callsTo("GET", "/api/requests/")[0]!.query.get("state")).toBe("failed");
  });
});

describe("review actions are grouped by role and follow the server", () => {
  it("lists edit under Operator and approve and reject under Approver", async () => {
    installFakeApi(signedIn(fakeServer(inReview({ submitted_by: USER_ID, allowed_actions: ["edit", "approve", "reject"] })).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    const actions = within(await screen.findByRole("region", { name: "Your available actions" }));
    expect(within(actions.getByRole("heading", { name: "Operator" }).parentElement!).getByText("Edit the draft")).toBeInTheDocument();
    const approverGroup = within(actions.getByRole("heading", { name: "Approver" }).parentElement!);
    expect(approverGroup.getByText("Approve")).toBeInTheDocument();
    expect(approverGroup.getByText("Reject")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Approver decision" })).toBeInTheDocument();
  });

  it("shows no Approver group and no decision area to an operator who may only edit", async () => {
    installFakeApi(signedIn(fakeServer(inReview({ allowed_actions: ["edit"] })).handler, reviewOperator()));
    renderApp(`/requests/${RUN_ID}`);
    const actions = within(await screen.findByRole("region", { name: "Your available actions" }));
    expect(actions.getByRole("heading", { name: "Operator" })).toBeInTheDocument();
    expect(actions.queryByRole("heading", { name: "Approver" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Approver decision" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit draft" })).toBeInTheDocument();
  });

  it("offers nothing when the server allows nothing", async () => {
    installFakeApi(signedIn(fakeServer(inReview({ allowed_actions: [] })).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    const actions = within(await screen.findByRole("region", { name: "Your available actions" }));
    expect(actions.getByText("None right now.")).toBeInTheDocument();
    expect(actions.queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit draft" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Approver decision" })).not.toBeInTheDocument();
  });
});
