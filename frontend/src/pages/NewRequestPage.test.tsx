import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { RUN_ID, detail, operator } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const created = { run_id: RUN_ID, case_id: "33333333-3333-4333-8333-333333333333", reference: "REQ-20260101-AAAA0001", state: "queued", created: true };

const accept: Handler = (call) => {
  if (call.method === "POST" && call.path === "/api/requests/") return json(201, created);
  if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) return json(200, detail({ state: "queued", processing: { step: "extract", task_state: "queued", attempt_count: 0, failure_code: null }, outcome_status: null, current_revision: null, draft: null, draft_source: null }));
  return apiError(404, "not_found");
};

describe("new request form", () => {
  it("posts the text and a client_request_id with the CSRF token, then opens the created request", async () => {
    const api = installFakeApi(signedIn(accept));
    const user = userEvent.setup();
    renderApp("/requests/new");

    await user.type(await screen.findByLabelText("Request text"), "Requester: Synthetic");
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    expect(await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ })).toBeInTheDocument();
    const post = api.callsTo("POST", "/api/requests/")[0]!;
    expect(post.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(post.body).toEqual({ text: "Requester: Synthetic", client_request_id: expect.stringMatching(UUID) });
    expect(Object.keys(post.body as object).sort()).toEqual(["client_request_id", "text"]);
    expect(screen.getByText("Queued", { selector: ".badge" })).toBeInTheDocument();
  });

  // The application never trims or rewrites the text. A browser <textarea> itself reports line breaks as "\n" (the HTML
  // standard normalizes "\r\n" in `.value`), so CRLF pasted by a user reaches the server as LF; see the slice document.
  it("sends the text as the textarea reports it: no trimming, leading and trailing whitespace kept", async () => {
    const api = installFakeApi(signedIn(accept));
    const user = userEvent.setup();
    renderApp("/requests/new");
    const box = await screen.findByLabelText("Request text");
    await user.click(box);
    await user.paste("  line one\r\nline two  \n\n");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await screen.findByRole("heading", { name: /REQ-/ });
    expect((api.callsTo("POST", "/api/requests/")[0]!.body as { text: string }).text).toBe("  line one\nline two  \n\n");
  });

  it("keeps the submit button disabled until there is text", async () => {
    installFakeApi(signedIn(accept));
    const user = userEvent.setup();
    renderApp("/requests/new");
    expect(await screen.findByRole("button", { name: "Submit request" })).toBeDisabled();
    await user.type(screen.getByLabelText("Request text"), "x");
    expect(screen.getByRole("button", { name: "Submit request" })).toBeEnabled();
  });

  it("disables the form while submitting, so a double click cannot submit twice", async () => {
    const gate = deferred<ReturnType<typeof json>>();
    const api = installFakeApi(signedIn((call) => (call.method === "POST" && call.path === "/api/requests/" ? gate.promise : accept(call))));
    const user = userEvent.setup();
    renderApp("/requests/new");
    await user.type(await screen.findByLabelText("Request text"), "abc");
    await user.dblClick(screen.getByRole("button", { name: "Submit request" }));

    expect(await screen.findByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(screen.getByLabelText("Request text")).toBeDisabled();
    expect(api.callsTo("POST", "/api/requests/")).toHaveLength(1);
    gate.resolve(json(201, created));
    await screen.findByRole("heading", { name: /REQ-/ });
  });

  it("shows the server's reason for refusing the text and keeps what was typed", async () => {
    installFakeApi(signedIn((call) => (call.method === "POST" ? apiError(400, "invalid_request_text", { reason: "blank" }) : accept(call))));
    const user = userEvent.setup();
    renderApp("/requests/new");
    await user.type(await screen.findByLabelText("Request text"), "   ");
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the request text.");
    expect(screen.getByLabelText("Request text")).toHaveValue("   ");
    expect(screen.getByRole("button", { name: "Submit request" })).toBeEnabled();
  });

  it("shows a permission message when the backend refuses with 403 permission_denied", async () => {
    installFakeApi(signedIn((call) => (call.method === "POST" ? apiError(403, "permission_denied") : accept(call))));
    const user = userEvent.setup();
    renderApp("/requests/new");
    await user.type(await screen.findByLabelText("Request text"), "x");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission to do this.");
  });

  it("reports a network failure and allows resubmitting the same text with the SAME client_request_id", async () => {
    let attempt = 0;
    const api = installFakeApi(signedIn((call) => {
      if (call.method === "POST" && call.path === "/api/requests/") return (attempt += 1) === 1 ? "network-error" : json(200, { ...created, created: false });
      return accept(call);
    }));
    const user = userEvent.setup();
    renderApp("/requests/new");
    await user.type(await screen.findByLabelText("Request text"), "same text");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be reached/i);

    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await screen.findByRole("heading", { name: /REQ-/ });
    const ids = api.callsTo("POST", "/api/requests/").map((c) => (c.body as { client_request_id: string }).client_request_id);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });

  it("uses a NEW client_request_id once the text has been edited", async () => {
    let attempt = 0;
    const api = installFakeApi(signedIn((call) => {
      if (call.method === "POST" && call.path === "/api/requests/") return (attempt += 1) === 1 ? "network-error" : json(201, created);
      return accept(call);
    }));
    const user = userEvent.setup();
    renderApp("/requests/new");
    const box = await screen.findByLabelText("Request text");
    await user.type(box, "first");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await screen.findByRole("alert");

    await user.type(box, " edited");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await screen.findByRole("heading", { name: /REQ-/ });
    const ids = api.callsTo("POST", "/api/requests/").map((c) => (c.body as { client_request_id: string }).client_request_id);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("says plainly that extraction uses a test parser, not AI", async () => {
    installFakeApi(signedIn(accept));
    renderApp("/requests/new");
    expect(await screen.findByText(/test parser/i)).toBeInTheDocument();
    expect(screen.getByText(/Requester: <name>/)).toBeInTheDocument();
  });

  it("does not offer the form to a user without the submit permission", async () => {
    const api = installFakeApi(signedIn(accept, operator({ permissions: ["intake.view_any_request"], roles: ["Auditor"] })));
    renderApp("/requests/new");
    expect(await screen.findByText(/does not have permission to submit/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Request text")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/requests/")).toHaveLength(0);
  });
});
