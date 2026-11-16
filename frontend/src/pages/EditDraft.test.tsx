import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Draft } from "../api/types";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { RUN_ID, detail } from "../test/fixtures";
import { HASH2, REV1, REV2, fakeServer, inReview, withWarnings, warning } from "../test/review";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";

const saved = (n = 2, invalidated = false) => ({ revision_id: REV2, revision_number: n, content_hash: HASH2, run_state: "needs_review", warnings: [], approval_invalidated: invalidated });
const editPosts = (api: ReturnType<typeof installFakeApi>) => api.callsTo("POST", `/api/requests/${RUN_ID}/edit/`);
const bodyOf = (call: { body: unknown }) => call.body as { base_revision_id: string | null; draft: Draft };

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Edit draft" }));
  return screen.getByRole("form", { name: /Edit draft/ });
}

describe("who is offered the editor", () => {
  it("shows Edit draft only when the server allows edit and there is a draft", async () => {
    installFakeApi(signedIn(fakeServer(detail({ allowed_actions: [] })).handler));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(screen.queryByRole("button", { name: "Edit draft" })).not.toBeInTheDocument();
  });

  it("does not offer it when the server allows edit but sends no draft", async () => {
    installFakeApi(signedIn(fakeServer(detail({ allowed_actions: ["edit"], draft: null, draft_source: null })).handler));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(screen.queryByRole("button", { name: "Edit draft" })).not.toBeInTheDocument();
  });
});

describe("initialization", () => {
  it("fills the form from the current revision and starts clean", async () => {
    installFakeApi(signedIn(fakeServer(inReview()).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    const form = await openEditor(user);

    expect(within(form).getByText(/based on revision 1/)).toBeInTheDocument();
    expect(within(form).getByLabelText("Name")).toHaveValue("Synthetic Requester");
    expect(within(form).getByLabelText("Contact")).toHaveValue("");
    expect(within(form).getByLabelText("Description (item 1)")).toHaveValue("Synthetic widget");
    expect(within(form).getByLabelText("Quantity (item 1)")).toHaveValue("2.50");
    expect(within(form).getByLabelText("Unit (item 1)")).toHaveValue("pcs");
    expect(within(form).getByLabelText("Notes (item 1)")).toHaveValue("");
    expect(within(form).getByLabelText("Notes")).toHaveValue("");
    expect(within(form).getByRole("button", { name: "Save as new revision" })).toBeDisabled();
    expect(within(form).getByText("No changes yet.")).toBeInTheDocument();
  });

  it("uses a text input for the quantity so the decimal text is never reformatted", async () => {
    installFakeApi(signedIn(fakeServer(inReview()).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    const quantity = screen.getByLabelText("Quantity (item 1)");
    expect(quantity).toHaveAttribute("type", "text");
    expect(quantity).toHaveAttribute("inputmode", "decimal");
  });

  it("never saves by itself: opening and typing send nothing until the button is pressed", async () => {
    const api = installFakeApi(signedIn(fakeServer(inReview()).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Name"), " changed");
    await new Promise((r) => setTimeout(r, 50));
    expect(editPosts(api)).toHaveLength(0);
  });
});

describe("saving", () => {
  it("sends the base revision id, keeps untouched values exact and decimals as strings, and then shows the new revision", async () => {
    const server = fakeServer(inReview(), (call) => {
      if (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/edit/`) {
        server.setDetail(inReview({ current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" } }));
        return json(200, saved());
      }
      return apiError(404, "not_found");
    });
    const api = installFakeApi(signedIn(server.handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);

    await user.clear(screen.getByLabelText("Description (item 1)"));
    await user.type(screen.getByLabelText("Description (item 1)"), "Changed widget");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    expect(await screen.findByText("Saved as revision 2.")).toBeInTheDocument();
    const post = editPosts(api)[0]!;
    expect(post.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(bodyOf(post).base_revision_id).toBe(REV1);
    expect(bodyOf(post).draft).toEqual({
      requester: { name: "Synthetic Requester", contact: null },
      items: [{ description: "Changed widget", quantity: "2.50", unit: "pcs", notes: null }],
      notes: null,
      source_refs: [{ field_path: "requester.name", excerpt: "Requester: Synthetic Requester", line: 1 }],
    });
    // The page loaded the request again; the editor is closed and the revision shown is the new one.
    expect(screen.queryByRole("form", { name: /Edit draft/ })).not.toBeInTheDocument();
    expect(within(await screen.findByRole("region", { name: "Current revision" })).getByText("2")).toBeInTheDocument();
  });

  it("sends a typed quantity as exact text", async () => {
    const api = installFakeApi(signedIn(fakeServer(inReview(), (call) => (call.method === "POST" ? json(200, saved()) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.clear(screen.getByLabelText("Quantity (item 1)"));
    await user.type(screen.getByLabelText("Quantity (item 1)"), "12345678901234567890.123456789");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 2.");
    expect(bodyOf(editPosts(api)[0]!).draft.items[0]?.quantity).toBe("12345678901234567890.123456789");
  });

  it("sends null for a field the user emptied, never a made-up value", async () => {
    const api = installFakeApi(signedIn(fakeServer(inReview(), (call) => (call.method === "POST" ? json(200, saved()) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.clear(screen.getByLabelText("Name"));
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 2.");
    expect(bodyOf(editPosts(api)[0]!).draft.requester.name).toBeNull();
  });

  it("says when saving an edit cancelled an earlier approval", async () => {
    installFakeApi(signedIn(fakeServer(inReview({ state: "approved" }), (call) => (call.method === "POST" ? json(200, saved(3, true)) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "extra");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    expect(await screen.findByText(/earlier approval no longer applies/)).toBeInTheDocument();
  });

  it("does not send twice when the button is double-clicked", async () => {
    const gate = deferred<ReturnType<typeof json>>();
    const api = installFakeApi(signedIn(fakeServer(inReview(), (call) => (call.method === "POST" ? gate.promise : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "x");
    await user.dblClick(screen.getByRole("button", { name: "Save as new revision" }));
    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(editPosts(api)).toHaveLength(1);
    gate.resolve(json(200, saved()));
    await screen.findByText("Saved as revision 2.");
  });

  it("explains a 409 unchanged_content answer and keeps the form", async () => {
    installFakeApi(signedIn(fakeServer(inReview(), (call) => (call.method === "POST" ? apiError(409, "unchanged_content") : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "x");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    expect(await screen.findByText(/identical to the current revision/)).toBeInTheDocument();
    expect(screen.getByLabelText("Notes")).toHaveValue("x");
  });

  it("reloads the request when the server says it can no longer be edited, and keeps what was typed", async () => {
    const server = fakeServer(inReview(), (call) => {
      if (call.method === "POST") {
        server.setDetail(detail({ state: "approved", allowed_actions: [] }));
        return apiError(409, "run_state_not_editable");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(signedIn(server.handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "keep me");
    const before = server.detailGets();
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    expect((await screen.findAllByText(/cannot be edited in its current state/)).length).toBeGreaterThan(0);
    expect(server.detailGets()).toBeGreaterThan(before);
    expect(screen.getByLabelText("Notes")).toHaveValue("keep me");
  });

  it("returns to the login when the session ended while editing", async () => {
    installFakeApi(signedIn(fakeServer(inReview(), (call) => (call.method === "POST" ? apiError(403, "not_authenticated") : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "x");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("validation findings beside their fields", () => {
  it("shows each blocking finding next to its field, marks the field invalid, and keeps the user's text", async () => {
    installFakeApi(
      signedIn(
        fakeServer(inReview(), (call) =>
          call.method === "POST"
            ? apiError(400, "draft_invalid", {
                findings: [
                  { code: "missing_required", field_path: "requester.name", blocking: true, message: "A value is required." },
                  { code: "invalid_value", field_path: "items[0].quantity", blocking: true, message: "Quantity must be positive." },
                  { code: "duplicate_item", field_path: "items[0]", blocking: false, message: "Possible duplicate." },
                ],
              })
            : apiError(404, "not_found"),
        ).handler,
      ),
    );
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.clear(screen.getByLabelText("Name"));
    await user.clear(screen.getByLabelText("Quantity (item 1)"));
    await user.type(screen.getByLabelText("Quantity (item 1)"), "-3");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    expect(await screen.findByText(/blocking findings and was not saved/)).toBeInTheDocument();
    const name = screen.getByLabelText("Name");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription(/missing_required.*A value is required/);
    const quantity = screen.getByLabelText("Quantity (item 1)");
    expect(quantity).toHaveAccessibleDescription(/invalid_value.*Quantity must be positive/);
    expect(quantity).toHaveValue("-3");
    // A warning on the whole item sits with the item, not on a field.
    expect(screen.getByRole("group", { name: "Item 1" })).toHaveTextContent("duplicate_item");
    expect(screen.getByLabelText("Description (item 1)")).not.toHaveAttribute("aria-invalid");
    expect(screen.getByText(/come from your last save attempt/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save as new revision" })).toBeEnabled();
  });

  it("lists a finding for a place the form has no field for, instead of dropping it", async () => {
    installFakeApi(
      signedIn(
        fakeServer(inReview(), (call) =>
          call.method === "POST"
            ? apiError(400, "draft_invalid", { findings: [{ code: "invalid_source_ref", field_path: "source_refs[0]", blocking: true, message: "Reference excerpt does not match the cited line." }] })
            : apiError(404, "not_found"),
        ).handler,
      ),
    );
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "x");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    const other = await screen.findByRole("region", { name: "Other findings" });
    expect(within(other).getByText(/invalid_source_ref/)).toBeInTheDocument();
  });

  it("shows transport validation errors (codes only) beside the field they name", async () => {
    installFakeApi(
      signedIn(
        fakeServer(inReview(), (call) =>
          call.method === "POST" ? apiError(400, "validation_error", { fields: { "draft.items[0].quantity": ["invalid"], "draft.requester.name": ["max_length"] } }) : apiError(404, "not_found"),
        ).handler,
      ),
    );
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.clear(screen.getByLabelText("Quantity (item 1)"));
    await user.type(screen.getByLabelText("Quantity (item 1)"), "abc");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText(/Some of the entered values are not valid/);
    expect(screen.getByLabelText("Quantity (item 1)")).toHaveAccessibleDescription(/This value is not accepted/);
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(/too long/);
  });

  it("shows the warnings of the loaded revision beside their fields before anything is saved", async () => {
    installFakeApi(signedIn(fakeServer(inReview(withWarnings(warning("value_not_in_source", "items[0].description")))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    expect(screen.getByLabelText("Description (item 1)")).toHaveAccessibleDescription(/value_not_in_source/);
    expect(screen.getByLabelText("Description (item 1)")).not.toHaveAttribute("aria-invalid");
  });
});

describe("a request whose extraction was invalid (first edit)", () => {
  const invalid = () =>
    detail({
      allowed_actions: ["edit"],
      outcome_status: "invalid",
      current_revision: null,
      draft_source: "extraction_outcome",
      draft: { requester: { name: null, contact: null }, items: [{ description: "Thing", quantity: null, unit: null, notes: null }], notes: null, source_refs: [] },
      findings: [
        { code: "missing_required", field_path: "requester.name", blocking: true, message: "A value is required.", excerpt: null },
        { code: "ambiguous_required_value", field_path: "items[0].quantity", blocking: true, message: "Ambiguous.", excerpt: "a few" },
      ],
    });

  it("starts from the extraction result, shows the blocking findings, and sends a null base revision", async () => {
    const api = installFakeApi(signedIn(fakeServer(invalid(), (call) => (call.method === "POST" ? json(200, saved(1)) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    expect(screen.getByText(/first edit \(no revision yet\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("Name")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Quantity (item 1)")).toHaveValue("");

    await user.type(screen.getByLabelText("Name"), "Synthetic");
    await user.type(screen.getByLabelText("Quantity (item 1)"), "4");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 1.");
    const body = bodyOf(editPosts(api)[0]!);
    expect(body.base_revision_id).toBeNull();
    expect(body.draft.requester).toEqual({ name: "Synthetic", contact: null });
    expect(body.draft.items[0]?.quantity).toBe("4");
  });
});

describe("a stale revision (someone else saved first)", () => {
  const latest = () =>
    inReview({
      current_revision: { id: REV2, number: 2, content_hash: HASH2, created_at: "2026-01-01T11:00:00Z" },
      draft: { requester: { name: "Colleague Edit", contact: null }, items: [{ description: "Colleague widget", quantity: "9", unit: null, notes: null }], notes: null, source_refs: [] },
    });

  function conflictServer() {
    let posts = 0;
    const server = fakeServer(inReview(), (call) => {
      if (call.method === "POST" && call.path === `/api/requests/${RUN_ID}/edit/`) {
        posts += 1;
        if (posts === 1) {
          server.setDetail(latest());
          return apiError(409, "stale_base_revision");
        }
        return json(200, saved(3));
      }
      return apiError(404, "not_found");
    });
    return server;
  }

  async function reachConflict(user: ReturnType<typeof userEvent.setup>) {
    await openEditor(user);
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "My unsaved name");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    return screen.findByText(/changed while you were editing/);
  }

  it("keeps the user's text, loads the latest version, and blocks saving until they choose", async () => {
    const api = installFakeApi(signedIn(conflictServer().handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await reachConflict(user);

    const alert = screen.getAllByRole("alert").find((el) => el.textContent?.includes("changed while you were editing"))!;
    expect(alert).toHaveTextContent("It is now at revision 2");
    expect(screen.getByLabelText("Name")).toHaveValue("My unsaved name"); // nothing was overwritten
    expect(screen.getByRole("button", { name: "Save as new revision" })).toBeDisabled();
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(editPosts(api)).toHaveLength(1); // no automatic retry
  });

  it("lets the user read the latest version before deciding", async () => {
    installFakeApi(signedIn(conflictServer().handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await reachConflict(user);
    await user.click(screen.getByText("Show the latest version"));
    expect(await screen.findByText("Colleague Edit")).toBeInTheDocument();
    expect(screen.getByText("Colleague widget")).toBeInTheDocument();
  });

  it("'keep my version' bases the next save on the latest revision, with a clear warning, and the user must save again", async () => {
    const api = installFakeApi(signedIn(conflictServer().handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await reachConflict(user);
    await user.click(screen.getByRole("button", { name: "Keep my version and base it on the latest" }));

    expect(screen.getByText(/will REPLACE the content of that revision/)).toBeInTheDocument();
    expect(screen.getByText(/based on revision 2/)).toBeInTheDocument();
    expect(editPosts(api)).toHaveLength(1); // still only the refused attempt
    expect(screen.getByLabelText("Name")).toHaveValue("My unsaved name");

    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 3.");
    expect(bodyOf(editPosts(api)[1]!).base_revision_id).toBe(REV2);
    expect(bodyOf(editPosts(api)[1]!).draft.requester.name).toBe("My unsaved name");
  });

  it("'discard' closes the editor and shows the latest version", async () => {
    installFakeApi(signedIn(conflictServer().handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await reachConflict(user);
    await user.click(screen.getByRole("button", { name: "Discard my changes and load the latest" }));
    expect(await screen.findByText("Colleague Edit")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: /Edit draft/ })).not.toBeInTheDocument();
  });

  it("keeps the user's text and says so when the latest version cannot be loaded", async () => {
    let posted = false;
    const server = fakeServer(inReview(), (call) => {
      if (call.method === "POST") {
        posted = true;
        return apiError(409, "stale_base_revision");
      }
      return apiError(404, "not_found");
    });
    installFakeApi(
      signedIn((call) => (posted && call.method === "GET" && call.path === `/api/requests/${RUN_ID}/` ? "network-error" : server.handler(call))),
    );
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "mine");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    expect(await screen.findByText(/latest version could not be loaded/)).toBeInTheDocument();
    expect(screen.getByLabelText("Notes")).toHaveValue("mine");
  });
});

describe("adding and removing items", () => {
  const twoItems = () =>
    inReview({
      draft: {
        requester: { name: "R", contact: null },
        items: [
          { description: "One", quantity: "1", unit: null, notes: null },
          { description: "Two", quantity: "2", unit: null, notes: null },
        ],
        notes: null,
        source_refs: [
          { field_path: "items[0].description", excerpt: "a", line: 1 },
          { field_path: "items[1].description", excerpt: "b", line: 2 },
        ],
      },
    });

  it("drops the removed item's source references, renumbers the others, and says so", async () => {
    const api = installFakeApi(signedIn(fakeServer(twoItems(), (call) => (call.method === "POST" ? json(200, saved()) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.click(screen.getByRole("button", { name: "Remove item 1" }));
    expect(screen.getByText(/1 source reference to removed items was dropped/)).toBeInTheDocument();
    expect(screen.getByLabelText("Description (item 1)")).toHaveValue("Two");

    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 2.");
    const body = bodyOf(editPosts(api)[0]!);
    expect(body.draft.items.map((i) => i.description)).toEqual(["Two"]);
    expect(body.draft.source_refs).toEqual([{ field_path: "items[0].description", excerpt: "b", line: 2 }]);
  });

  it("adds an empty item that the server then judges", async () => {
    const api = installFakeApi(signedIn(fakeServer(twoItems(), (call) => (call.method === "POST" ? json(200, saved()) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.click(screen.getByRole("button", { name: "Add item" }));
    expect(screen.getByLabelText("Description (item 3)")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText("Saved as revision 2.");
    expect(bodyOf(editPosts(api)[0]!).draft.items[2]).toEqual({ description: null, quantity: null, unit: null, notes: null });
  });

  it("clears earlier findings when items move, because their positions no longer match", async () => {
    installFakeApi(signedIn(fakeServer(twoItems(), (call) => (call.method === "POST" ? apiError(400, "draft_invalid", { findings: [{ code: "invalid_value", field_path: "items[1].quantity", blocking: true, message: "Bad." }] }) : apiError(404, "not_found"))).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "x");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));
    await screen.findByText(/last save attempt/);
    await user.click(screen.getByRole("button", { name: "Remove item 1" }));
    expect(screen.getByText(/earlier findings were cleared/)).toBeInTheDocument();
    expect(screen.getByLabelText("Quantity (item 1)")).not.toHaveAccessibleDescription(/invalid_value/);
  });
});

describe("leaving the editor", () => {
  it("closes at once when nothing was changed", async () => {
    installFakeApi(signedIn(fakeServer(inReview()).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form", { name: /Edit draft/ })).not.toBeInTheDocument();
  });

  it("asks before throwing changes away, and Escape or Cancel keeps editing", async () => {
    installFakeApi(signedIn(fakeServer(inReview()).handler));
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await openEditor(user);
    await user.type(screen.getByLabelText("Notes"), "work in progress");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const confirm = screen.getByRole("group", { name: "Discard your changes?" });
    expect(confirm).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Discard your changes?" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Notes")).toHaveValue("work in progress");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.queryByRole("form", { name: /Edit draft/ })).not.toBeInTheDocument();
  });
});

describe("rendering edited text", () => {
  it("shows hostile text in fields and findings as plain text", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1">';
    const draft: Draft = { requester: { name: hostile, contact: null }, items: [], notes: hostile, source_refs: [] };
    const { container } = (() => {
      installFakeApi(signedIn(fakeServer(inReview({ draft, findings: [{ code: "x", field_path: "notes", blocking: false, message: hostile, excerpt: null }], warnings: [] })).handler));
      return renderApp(`/requests/${RUN_ID}`);
    })();
    const user = userEvent.setup();
    await openEditor(user);
    expect(screen.getByLabelText("Name")).toHaveValue(hostile);
    expect(container.querySelector("img")).toBeNull();
  });
});
