import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { REJECTION_REASONS } from "../api/types";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { HASH, RUN_ID, USER_ID, detail } from "../test/fixtures";
import { APPROVER_ID, HASH2, REV1, REV2, approver, fakeServer, inReview, warning, withWarnings } from "../test/review";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";

const approvePosts = (api: ReturnType<typeof installFakeApi>) => api.callsTo("POST", `/api/requests/${RUN_ID}/approve/`);
const rejectPosts = (api: ReturnType<typeof installFakeApi>) => api.callsTo("POST", `/api/requests/${RUN_ID}/reject/`);
const approved = { approval_id: "a1", revision_id: REV1, content_hash: HASH, run_state: "approved", acknowledged_warnings: [] };
const rejected = { rejection_id: "j1", revision_id: REV1, reason_code: "incorrect_content", run_state: "needs_review" };

/** Someone else's request in review, seen by an Approver: the server allows approve and reject. */
const theirs = (overrides = {}) => inReview({ submitted_by: USER_ID, allowed_actions: ["approve", "reject"], ...overrides });

describe("approval is offered only when the server allows it", () => {
  it("shows no Approve section when allowed_actions lacks approve", async () => {
    installFakeApi(signedIn(fakeServer(theirs({ allowed_actions: ["reject"] })).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(screen.queryByRole("region", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it("follows the server, not the user's permission list: no panel when the server says nothing is allowed", async () => {
    // The profile claims the approve permission, but the server's allowed_actions is empty (for example self-approval).
    const api = installFakeApi(signedIn(fakeServer(theirs({ allowed_actions: [], action_blockers: { approve: "self_approval_forbidden" } })).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(screen.queryByRole("region", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.getByText(/Approve is not possible now: you submitted or edited this request/)).toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(0);
  });

  it("shows the panel when the server allows approve even if the profile lists no approve permission (the server decides)", async () => {
    installFakeApi(signedIn(fakeServer(theirs()).handler, approver({ permissions: [] })));
    renderApp(`/requests/${RUN_ID}`);
    expect(await screen.findByRole("region", { name: "Approve" })).toBeInTheDocument();
  });

  it("sends nothing at all when no action is allowed", async () => {
    const api = installFakeApi(signedIn(fakeServer(detail({ allowed_actions: [] })).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /^(approve|reject|edit draft)/i })).not.toBeInTheDocument();
  });
});

describe("approving", () => {
  it("shows the revision number and the full content hash", async () => {
    installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    expect(within(panel).getByText("1")).toBeInTheDocument();
    expect(within(panel).getByText(HASH)).toBeInTheDocument();
  });

  it("approves a revision without warnings after an explicit confirmation, sending id, hash and an empty acknowledgement", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/approve/`) {
        server.setDetail(detail({ state: "approved", allowed_actions: [], approval_count: 1 }));
        return json(200, approved);
      }
      return apiError(404, "not_found");
    });
    const api = installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));

    expect(approvePosts(api)).toHaveLength(0); // the first click only opens the confirmation
    const confirm = screen.getByRole("group", { name: "Approve revision 1?" });
    expect(confirm).toHaveFocus();
    await user.click(within(confirm).getByRole("button", { name: "Confirm approval" }));

    expect(await screen.findByText("Revision 1 approved.")).toBeInTheDocument();
    const post = approvePosts(api)[0]!;
    expect(post.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(post.body).toEqual({ revision_id: REV1, content_hash: HASH, acknowledged_warnings: [] });
    // The page loaded the request again: it is approved and the Approve section is gone.
    expect(await screen.findByText("Approved", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("cancelling the confirmation sends nothing", async () => {
    const api = installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("group", { name: "Approve revision 1?" })).not.toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(0);
  });

  it("Escape closes the confirmation without sending", async () => {
    const api = installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Approve revision 1?" })).not.toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(0);
  });

  it("can be completed with the keyboard alone", async () => {
    const api = installFakeApi(signedIn(fakeServer(theirs(withWarnings(warning())), (call) => (call.method === "POST" ? json(200, approved) : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const box = await screen.findByRole("checkbox");
    box.focus();
    await user.keyboard(" "); // tick the warning
    await user.tab();
    expect(screen.getByRole("button", { name: "Approve revision 1…" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard("{Tab}{Enter}"); // from the focused group to "Confirm approval"
    expect(await screen.findByText("Revision 1 approved.")).toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(1);
  });

  it("does not send twice when the confirmation is clicked repeatedly", async () => {
    const gate = deferred<ReturnType<typeof json>>();
    const api = installFakeApi(signedIn(fakeServer(theirs(), (call) => (call.method === "POST" ? gate.promise : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    const confirm = screen.getByRole("button", { name: "Confirm approval" });
    await user.dblClick(confirm);
    expect(await screen.findByRole("button", { name: "Working…" })).toBeDisabled();
    expect(approvePosts(api)).toHaveLength(1);
    gate.resolve(json(200, approved));
    await screen.findByText("Revision 1 approved.");
  });
});

describe("warning acknowledgement", () => {
  const two = () => withWarnings(warning("value_not_in_source", "items[0].description"), warning("duplicate_item", "items[1]"));

  it("lists every current warning with its explanation and blocks approval until each one is acknowledged", async () => {
    installFakeApi(signedIn(fakeServer(theirs(two())).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    const boxes = within(panel).getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    expect(within(panel).getByText(/Synthetic explanation for value_not_in_source/)).toBeInTheDocument();
    const approve = within(panel).getByRole("button", { name: "Approve revision 1…" });
    expect(approve).toBeDisabled();
    expect(within(panel).getByText("Acknowledge every warning first.")).toBeInTheDocument();

    await user.click(boxes[0]!);
    expect(approve).toBeDisabled(); // one of two is not enough
    await user.click(boxes[1]!);
    expect(approve).toBeEnabled();
    await user.click(boxes[1]!);
    expect(approve).toBeDisabled(); // un-ticking blocks again
  });

  it("has no 'acknowledge all' shortcut", async () => {
    installFakeApi(signedIn(fakeServer(theirs(two())).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    expect(within(panel).queryByRole("button", { name: /all/i })).not.toBeInTheDocument();
  });

  it("sends exactly the current warnings as the acknowledgement", async () => {
    const api = installFakeApi(signedIn(fakeServer(theirs(two()), (call) => (call.method === "POST" ? json(200, approved) : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    for (const box of within(panel).getAllByRole("checkbox")) await user.click(box);
    await user.click(within(panel).getByRole("button", { name: "Approve revision 1…" }));
    expect(screen.getByText(/acknowledging 2 warnings/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    await screen.findByText("Revision 1 approved.");
    expect(approvePosts(api)[0]!.body).toEqual({
      revision_id: REV1,
      content_hash: HASH,
      acknowledged_warnings: [
        { code: "value_not_in_source", field_path: "items[0].description" },
        { code: "duplicate_item", field_path: "items[1]" },
      ],
    });
  });

  it("asks for one acknowledgement per distinct (code, path) and sends it once", async () => {
    const dup = withWarnings(warning(), warning());
    const api = installFakeApi(signedIn(fakeServer(theirs(dup), (call) => (call.method === "POST" ? json(200, approved) : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    expect(within(panel).getAllByRole("checkbox")).toHaveLength(1);
    await user.click(within(panel).getByRole("checkbox"));
    await user.click(within(panel).getByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    await screen.findByText("Revision 1 approved.");
    expect((approvePosts(api)[0]!.body as { acknowledged_warnings: unknown[] }).acknowledged_warnings).toHaveLength(1);
  });

  it("changing an acknowledgement closes an open confirmation", async () => {
    installFakeApi(signedIn(fakeServer(theirs(withWarnings(warning()))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    await user.click(within(panel).getByRole("checkbox"));
    await user.click(within(panel).getByRole("button", { name: "Approve revision 1…" }));
    expect(screen.getByRole("group", { name: "Approve revision 1?" })).toBeInTheDocument();
    await user.click(within(panel).getByRole("checkbox"));
    expect(screen.queryByRole("group", { name: "Approve revision 1?" })).not.toBeInTheDocument();
  });
});

describe("the backend stays authoritative", () => {
  it("shows the self-approval refusal if the server still refuses, and loads the request again", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST") {
        server.setDetail(theirs({ allowed_actions: [], action_blockers: { approve: "self_approval_forbidden" } }));
        return apiError(403, "self_approval_forbidden");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText("You cannot approve a request that you submitted or edited.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approve" })).not.toBeInTheDocument(); // the reloaded server view no longer allows it
  });

  it("on a stale revision records nothing, loads the new revision, and asks for the warnings again", async () => {
    const warns = withWarnings(warning());
    const server = fakeServer(theirs(warns), (call) => {
      if (call.method === "POST") {
        server.setDetail(theirs({ ...withWarnings(warning("duplicate_item", "items[0]")), current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" } }));
        return apiError(409, "stale_revision");
      }
      return apiError(404, "not_found");
    });
    const api = installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    await user.click(within(panel).getByRole("checkbox"));
    await user.click(within(panel).getByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    expect(await screen.findByText(/changed after you opened it, so nothing was recorded/)).toBeInTheDocument();
    const fresh = await screen.findByRole("button", { name: "Approve revision 2…" });
    expect(fresh).toBeDisabled(); // the new revision has its own warning, which must be acknowledged again
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(within(screen.getByRole("region", { name: "Approve" })).getByText(HASH2)).toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(1);
  });

  it("never carries an acknowledgement over to a new revision, even when it has the SAME warning code and path", async () => {
    // Revision 1's warning was ticked. Revision 2 has a warning with the same code and path but different text: it must be
    // reviewed again, because the user acknowledged what revision 1 said, not what revision 2 says.
    const sameWarning = warning("value_not_in_source", "items[0].description");
    const server = fakeServer(theirs(withWarnings(sameWarning)), (call) => {
      if (call.method === "POST") {
        const next = withWarnings(sameWarning);
        next.findings = [{ ...next.findings![0]!, message: "A DIFFERENT explanation in revision 2.", excerpt: "different excerpt" }];
        server.setDetail(theirs({ ...next, current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" } }));
        return apiError(409, "stale_revision");
      }
      return apiError(404, "not_found");
    });
    const api = installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Approve" });
    await user.click(within(panel).getByRole("checkbox"));
    await user.click(within(panel).getByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    expect(await screen.findByRole("button", { name: "Approve revision 2…" })).toBeDisabled();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(within(screen.getByRole("region", { name: "Approve" })).getByText(/A DIFFERENT explanation in revision 2/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /^Approve revision/ })).not.toBeInTheDocument();
    expect(approvePosts(api)).toHaveLength(1); // nothing was sent for revision 2
  });

  it("explains warnings_not_acknowledged and reloads", async () => {
    const server = fakeServer(theirs(), (call) => (call.method === "POST" ? apiError(400, "warnings_not_acknowledged", { missing: [{ code: "a", field_path: "x" }], unexpected: [] }) : apiError(404, "not_found")));
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    const before = server.detailGets();
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText(/The warnings changed/)).toBeInTheDocument();
    expect(server.detailGets()).toBeGreaterThan(before);
  });

  it("shows a permission refusal without reloading", async () => {
    const server = fakeServer(theirs(), (call) => (call.method === "POST" ? apiError(403, "permission_denied") : apiError(404, "not_found")));
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    const before = server.detailGets();
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText("You do not have permission to do this.")).toBeInTheDocument();
    expect(server.detailGets()).toBe(before);
  });

  it("reports a network failure and does not claim success", async () => {
    installFakeApi(signedIn(fakeServer(theirs(), (call) => (call.method === "POST" ? "network-error" : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument();
    expect(screen.queryByText(/approved\./)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve revision 1…" })).toBeEnabled();
  });

  it("goes back to the login when the session has ended", async () => {
    installFakeApi(signedIn(fakeServer(theirs(), (call) => (call.method === "POST" ? apiError(403, "not_authenticated") : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("approve and reject while a draft edit is open", () => {
  it("are disabled with an explanation, so the wrong revision cannot be approved by accident", async () => {
    installFakeApi(signedIn(fakeServer(inReview({ submitted_by: APPROVER_ID, allowed_actions: ["edit", "approve", "reject"] })).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await user.click(await screen.findByRole("button", { name: "Edit draft" }));
    expect(screen.getByRole("button", { name: "Approve revision 1…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject revision 1…" })).toBeDisabled();
    expect(screen.getByText(/Save or discard your open edit before approving/)).toBeInTheDocument();
  });
});

describe("rejecting", () => {
  it("offers exactly the API's reason codes as options, and no free-text field", async () => {
    installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    const radios = within(panel).getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual([...REJECTION_REASONS]);
    expect(within(panel).queryAllByRole("textbox")).toHaveLength(0);
    expect(panel.querySelectorAll("textarea, input[type=text]")).toHaveLength(0);
  });

  it("needs a reason, then an explicit confirmation, and sends id, hash and the reason code", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/reject/`) {
        server.setDetail(theirs({ rejection_count: 1, action_blockers: { approve: "revision_rejected" }, allowed_actions: ["reject"] }));
        return json(200, rejected);
      }
      return apiError(404, "not_found");
    });
    const api = installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    const button = within(panel).getByRole("button", { name: "Reject revision 1…" });
    expect(button).toBeDisabled();
    expect(within(panel).getByText("Choose a reason first.")).toBeInTheDocument();

    await user.click(within(panel).getByLabelText("The content is incorrect"));
    await user.click(button);
    expect(rejectPosts(api)).toHaveLength(0); // confirmation first
    const confirm = screen.getByRole("group", { name: "Reject revision 1?" });
    expect(confirm).toHaveTextContent("The content is incorrect");
    await user.click(within(confirm).getByRole("button", { name: "Confirm rejection" }));

    expect(await screen.findByText(/Revision 1 rejected/)).toBeInTheDocument();
    expect(rejectPosts(api)[0]!.body).toEqual({ revision_id: REV1, content_hash: HASH, reason_code: "incorrect_content" });
    expect(await screen.findByText(/Approve is not possible now: the current revision was rejected/)).toBeInTheDocument();
  });

  it("cancelling the confirmation, or pressing Escape, sends nothing", async () => {
    const api = installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Reject revision 1?" })).not.toBeInTheDocument();
    expect(rejectPosts(api)).toHaveLength(0);
  });

  it("changing the reason closes an open confirmation", async () => {
    installFakeApi(signedIn(fakeServer(theirs()).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.click(within(panel).getByLabelText("The content is incomplete"));
    expect(screen.queryByRole("group", { name: "Reject revision 1?" })).not.toBeInTheDocument();
  });

  it("does not send twice on a double click", async () => {
    const gate = deferred<ReturnType<typeof json>>();
    const api = installFakeApi(signedIn(fakeServer(theirs(), (call) => (call.method === "POST" ? gate.promise : apiError(404, "not_found"))).handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.dblClick(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(rejectPosts(api)).toHaveLength(1);
    gate.resolve(json(200, rejected));
    await screen.findByText(/rejected/);
  });

  it("shows the server's refusal when the request is no longer in review, and loads it again", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST") {
        server.setDetail(detail({ state: "approved", allowed_actions: [] }));
        return apiError(409, "run_not_in_review");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(await screen.findByText("The request is no longer waiting for review.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("forgets the chosen reason when a new revision arrives, so a reason given for one revision is never applied to another", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST") {
        server.setDetail(theirs({ current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" } }));
        return apiError(409, "stale_revision");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));

    const fresh = await screen.findByRole("button", { name: "Reject revision 2…" });
    expect(fresh).toBeDisabled();
    expect(screen.getByLabelText("Another reason")).not.toBeChecked();
  });

  it("handles a 409 stale_revision by loading the new revision", async () => {
    const server = fakeServer(theirs(), (call) => {
      if (call.method === "POST") {
        server.setDetail(theirs({ current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" } }));
        return apiError(409, "stale_revision");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(signedIn(server.handler, approver()));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const panel = await screen.findByRole("region", { name: "Reject" });
    await user.click(within(panel).getByLabelText("Another reason"));
    await user.click(within(panel).getByRole("button", { name: "Reject revision 1…" }));
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(await screen.findByRole("button", { name: "Reject revision 2…" })).toBeInTheDocument();
    expect(screen.getByText(/changed after you opened it/)).toBeInTheDocument();
  });
});
