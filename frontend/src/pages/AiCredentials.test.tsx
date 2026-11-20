import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CREDENTIALS_PATH,
  SECRET_SENTINEL,
  SETTINGS_PATH,
  acceptPut,
  administrator,
  combined,
  connection,
  credentialServer,
  managedConnection,
  ollama,
  settings,
  settingsServer,
  withSelection,
} from "../test/aiSettings";
import { apiError, installFakeApi, json, type FakeResponse, type RecordedCall } from "../test/fakeApi";
import { operator } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";
import type { AiSettings } from "../api/types";

const REF = "AP_AI_CRED_CORP_LLM";
const SECOND = "AP_AI_CRED_SECOND";
const detailPath = (reference: string) => `${CREDENTIALS_PATH}${reference}/`;

afterEach(() => {
  vi.restoreAllMocks();
});

type Api = ReturnType<typeof installFakeApi>;

async function open(initial: AiSettings, stored: Record<string, "set" | "unreadable"> = {}, options: Parameters<typeof credentialServer>[1] = {}) {
  const server = settingsServer(initial);
  const creds = credentialServer(stored, {
    readers: (reference) => server.state.current.connections.filter((c) => c.credential_source === "managed" && c.credential_ref === reference).map((c) => c.connection_id),
    ...options,
  });
  const api = installFakeApi(signedIn(combined(server.handler, creds.handler), administrator()));
  const user = userEvent.setup();
  renderApp("/settings/ai");
  await screen.findByRole("group", { name: "Extraction mode" });
  return { server, creds, api, user };
}

const panel = () => within(screen.getByRole("region", { name: "Managed credentials" }));
const row = (reference: string) => within(screen.getByRole("heading", { level: 3, name: reference }).closest("article")!);
const addForm = () => within(screen.getByRole("form", { name: "Store a new managed credential" }));
const callsTo = (api: Api, method: string, path: string) => api.callsTo(method, path);
const settingsPuts = (api: Api) => api.callsTo("PUT", SETTINGS_PATH);
const mutations = (api: Api) => api.calls.filter((c) => c.path.startsWith(CREDENTIALS_PATH) && c.method !== "GET");

/** Text a person (or a screen reader) can read anywhere on the page. The value of an <input> is not text content. */
const pageText = () => document.body.textContent ?? "";

async function storeNew(user: ReturnType<typeof userEvent.setup>, reference = SECOND, secret = SECRET_SENTINEL) {
  await user.type(addForm().getByLabelText("Credential reference"), reference);
  await user.type(addForm().getByLabelText("New secret value"), secret);
  await user.click(addForm().getByRole("button", { name: "Store new credential" }));
}

describe("access", () => {
  it("shows the credentials section to an administrator and nothing to anyone else, with no credential request", async () => {
    const api = installFakeApi(signedIn(combined(settingsServer(withSelection()).handler, credentialServer({}).handler), operator()));
    renderApp("/settings/ai");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Managed credentials" })).not.toBeInTheDocument();
    expect(document.querySelector("input[type='password']")).toBeNull();
    expect(api.calls.filter((c) => c.path.startsWith("/api/ai-"))).toHaveLength(0);
  });

  it("shows a non-administrator nothing even if the server would answer", async () => {
    const api = installFakeApi(signedIn(combined(settingsServer(withSelection()).handler, credentialServer({ [REF]: "set" }).handler), operator()));
    renderApp("/settings/ai");
    await screen.findByText("Not available for your account");
    expect(screen.queryByLabelText("New secret value")).not.toBeInTheDocument();
    expect(api.calls.filter((c) => c.path.startsWith(CREDENTIALS_PATH))).toHaveLength(0);
  });

  it("shows the section for an administrator", async () => {
    await open(settings());
    expect(screen.getByRole("heading", { level: 2, name: "Managed credentials" })).toBeInTheDocument();
    expect(panel().getByText(/Credentials set as backend environment variables are not managed here/)).toBeInTheDocument();
  });

  it("treats a 403 permission_denied from the credential endpoint as authoritative and changes nothing", async () => {
    const { user } = await open(settings(), {}, { fail: (call) => (call.method === "POST" ? apiError(403, "permission_denied") : undefined) });
    await storeNew(user);
    expect(await panel().findByText(/You do not have permission to manage credentials/)).toBeInTheDocument();
    expect(panel().queryByText("Worker restart required.")).not.toBeInTheDocument();
  });
});

describe("credential status", () => {
  it("lists the references the saved managed connections read and fetches the status of each, never a value", async () => {
    const { api } = await open(settings({ connections: [managedConnection(), connection({ connection_id: "env-one", credential_ref: "AP_AI_CRED_ENV" })] }), { [REF]: "set" });
    expect(await row(REF).findByText("Stored")).toBeInTheDocument();
    expect(row(REF).getByText("corp-llm")).toBeInTheDocument();
    expect(row(REF).getByText("dpapi_windows")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3, name: "AP_AI_CRED_ENV" })).not.toBeInTheDocument();
    expect(callsTo(api, "GET", detailPath(REF))).toHaveLength(1);
    expect(callsTo(api, "GET", detailPath("AP_AI_CRED_ENV"))).toHaveLength(0);
  });

  it("shows 'Not stored' with a Store action, and 'unreadable' with Replace and Delete", async () => {
    await open(settings({ connections: [managedConnection(), managedConnection({ connection_id: "second", display_name: "Second server", credential_ref: SECOND })] }), { [SECOND]: "unreadable" });
    expect(await row(REF).findByText("Not stored")).toBeInTheDocument();
    expect(row(REF).getByRole("button", { name: `Store credential ${REF}` })).toBeInTheDocument();
    expect(row(REF).queryByRole("button", { name: `Replace credential ${REF}` })).not.toBeInTheDocument();
    expect(await row(SECOND).findByText("Stored, but unreadable by the server")).toBeInTheDocument();
    expect(row(SECOND).getByText(/cannot decrypt it/)).toBeInTheDocument();
    expect(row(SECOND).getByRole("button", { name: `Replace credential ${SECOND}` })).toBeInTheDocument();
    expect(row(SECOND).getByRole("button", { name: `Delete credential ${SECOND}` })).toBeInTheDocument();
  });

  it("re-checks the status on request", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await row(REF).findByText("Stored");
    await user.click(row(REF).getByRole("button", { name: `Check status of ${REF}` }));
    await waitFor(() => expect(callsTo(api, "GET", detailPath(REF))).toHaveLength(2));
  });

  it("looks up any reference without a secret", async () => {
    const { api, user } = await open(settings(), { [SECOND]: "set" });
    expect(panel().getByText(/No saved connection uses a managed credential/)).toBeInTheDocument();
    await user.type(addForm().getByLabelText("Credential reference"), SECOND);
    await user.click(addForm().getByRole("button", { name: "Check status only" }));
    expect(await row(SECOND).findByText("Stored")).toBeInTheDocument();
    expect(mutations(api)).toHaveLength(0);
  });

  it("explains an unknown status when the check fails", async () => {
    await open(settings({ connections: [managedConnection()] }), {}, { fail: (call) => (call.method === "GET" ? apiError(503, "secret_store_unavailable") : undefined) });
    expect(await row(REF).findByText("Status unknown")).toBeInTheDocument();
    expect(row(REF).getByText("The secret store is unavailable, so the status is unknown.")).toBeInTheDocument();
  });

  it("shows the connection's own status with wording for a managed credential, and refreshes it after a store", async () => {
    const initial = settings({ connections: [managedConnection({ status: { configured: false, issue: "missing_api_key" } })] });
    const { server, user } = await open(initial, {}, {
      onChange: () => {
        server.state.current = { ...server.state.current, connections: server.state.current.connections.map((c) => ({ ...c, status: { configured: true, issue: null } })) };
      },
    });
    const card = within(screen.getByRole("heading", { level: 3, name: "Company model server" }).closest("article")!);
    expect(card.getByText(/The managed credential is not stored/)).toBeInTheDocument();
    expect(card.getByText("Managed (secret store)")).toBeInTheDocument();
    await user.click(await row(REF).findByRole("button", { name: `Store credential ${REF}` }));
    await user.type(row(REF).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Store credential" }));
    expect(await card.findByText("Configured")).toBeInTheDocument();
    expect(card.queryByText(/The managed credential is not stored/)).not.toBeInTheDocument();
  });
});

describe("create", () => {
  it("stores a new credential: POST {reference, value} with the CSRF token, then clears the field and shows the status", async () => {
    const { api, user, creds } = await open(settings());
    await storeNew(user);
    expect(await row(SECOND).findByText("Stored")).toBeInTheDocument();
    const post = callsTo(api, "POST", CREDENTIALS_PATH);
    expect(post).toHaveLength(1);
    expect(post[0]!.body).toEqual({ reference: SECOND, value: SECRET_SENTINEL });
    expect(post[0]!.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(post[0]!.headers["content-type"]).toBe("application/json");
    expect(post[0]!.credentials).toBe("same-origin");
    expect(creds.stored.get(SECOND)).toBe("set");
    expect(addForm().getByLabelText("New secret value")).toHaveValue("");
    expect(addForm().getByLabelText("Credential reference")).toHaveValue("");
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("stores the credential of a connection whose key is missing, from its own row", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }));
    await user.click(await row(REF).findByRole("button", { name: `Store credential ${REF}` }));
    const input = row(REF).getByLabelText("New secret value");
    expect(input).toHaveValue("");
    await user.type(input, SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Store credential" }));
    expect(await row(REF).findByText("Stored")).toBeInTheDocument();
    expect(callsTo(api, "POST", CREDENTIALS_PATH)[0]!.body).toEqual({ reference: REF, value: SECRET_SENTINEL });
    expect(row(REF).queryByLabelText("New secret value")).not.toBeInTheDocument();
  });

  it("checks the reference and the secret locally and sends nothing when they are not acceptable", async () => {
    const { api, user } = await open(settings());
    await user.type(addForm().getByLabelText("Credential reference"), "MY_KEY");
    await user.click(addForm().getByRole("button", { name: "Store new credential" }));
    expect(addForm().getAllByText(/The reference must be the prefix shown/)).toHaveLength(1);
    expect(addForm().getByText("This is required.")).toBeInTheDocument();
    await user.clear(addForm().getByLabelText("Credential reference"));
    await user.type(addForm().getByLabelText("Credential reference"), SECOND);
    await user.type(addForm().getByLabelText("New secret value"), " leading space");
    await user.click(addForm().getByRole("button", { name: "Store new credential" }));
    expect(addForm().getByText(/printable text with no space or line break at the start or end/)).toBeInTheDocument();
    expect(mutations(api)).toHaveLength(0);
    expect(pageText()).not.toContain("leading space");
  });

  it("refuses to double submit", async () => {
    const gate = deferred<FakeResponse>();
    const { api, user } = await open(settings(), {}, { fail: (call) => (call.method === "POST" ? gate.promise : undefined) });
    await user.type(addForm().getByLabelText("Credential reference"), SECOND);
    await user.type(addForm().getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(addForm().getByRole("button", { name: "Store new credential" }));
    expect(await addForm().findByRole("button", { name: "Working…" })).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(callsTo(api, "POST", CREDENTIALS_PATH)).toHaveLength(1);
    gate.resolve(json(201, { reference: SECOND, source: "managed", status: "set", store: "dpapi_windows", connections: [], apply: "worker_restart_required" }));
    await screen.findByRole("heading", { level: 3, name: SECOND });
    await row(SECOND).findByText("Stored");
  });
});

describe("replace", () => {
  it("asks for confirmation, then sends PUT {value} only", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await user.click(await row(REF).findByRole("button", { name: `Replace credential ${REF}` }));
    await user.type(row(REF).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    expect(row(REF).getByText(`Replace the stored credential ${REF}?`)).toBeInTheDocument();
    expect(callsTo(api, "PUT", detailPath(REF))).toHaveLength(0);
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(callsTo(api, "PUT", detailPath(REF))).toHaveLength(1));
    const put = callsTo(api, "PUT", detailPath(REF))[0]!;
    expect(put.body).toEqual({ value: SECRET_SENTINEL });
    expect(put.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(await panel().findByText("Worker restart required.")).toBeInTheDocument();
    expect(row(REF).queryByLabelText("New secret value")).not.toBeInTheDocument();
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await user.click(await row(REF).findByRole("button", { name: `Replace credential ${REF}` }));
    await user.type(row(REF).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    await user.click(row(REF).getByRole("button", { name: "Cancel" }));
    expect(mutations(api)).toHaveLength(0);
  });

  it("replaces an unreadable credential", async () => {
    const { creds, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "unreadable" });
    await user.click(await row(REF).findByRole("button", { name: `Replace credential ${REF}` }));
    await user.type(row(REF).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    expect(await row(REF).findByText("Stored")).toBeInTheDocument();
    expect(creds.stored.get(REF)).toBe("set");
  });
});

describe("delete", () => {
  it("asks first, and a cancelled confirmation sends nothing", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection({ credential_ref: SECOND })] }), { [SECOND]: "set" });
    await user.click(await row(SECOND).findByRole("button", { name: `Delete credential ${SECOND}` }));
    expect(row(SECOND).getByText(`Delete the credential ${SECOND}?`)).toBeInTheDocument();
    await user.click(row(SECOND).getByRole("button", { name: "Cancel" }));
    expect(mutations(api)).toHaveLength(0);
    expect(row(SECOND).getByText("Stored")).toBeInTheDocument();
  });

  it("deletes after confirmation with a DELETE that has no body, then reports 'Not stored' and the worker restart", async () => {
    const { api, user, creds } = await open(settings(), { [SECOND]: "set" });
    await user.type(addForm().getByLabelText("Credential reference"), SECOND);
    await user.click(addForm().getByRole("button", { name: "Check status only" }));
    await user.click(await row(SECOND).findByRole("button", { name: `Delete credential ${SECOND}` }));
    await user.click(row(SECOND).getByRole("button", { name: "Delete credential" }));
    expect(await row(SECOND).findByText("Not stored")).toBeInTheDocument();
    const del = callsTo(api, "DELETE", detailPath(SECOND));
    expect(del).toHaveLength(1);
    expect(del[0]!.body).toBeUndefined();
    expect(del[0]!.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(creds.stored.has(SECOND)).toBe(false);
    expect(await panel().findByText("Worker restart required.")).toBeInTheDocument();
  });

  it("explains 409 credential_in_use, names the connections and keeps the credential", async () => {
    const { user, creds } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await user.click(await row(REF).findByRole("button", { name: `Delete credential ${REF}` }));
    await user.click(row(REF).getByRole("button", { name: "Delete credential" }));
    expect(await row(REF).findByText(/still reads this credential \(corp-llm\)/)).toBeInTheDocument();
    expect(row(REF).getByText("Stored")).toBeInTheDocument();
    expect(creds.stored.has(REF)).toBe(true);
    expect(panel().queryByText("Worker restart required.")).not.toBeInTheDocument();
  });
});

describe("secret handling", () => {
  it("uses a masked input that starts empty, has no reveal control, and lives outside the settings form", async () => {
    await open(withSelection({ connections: [managedConnection(), ollama()] }), { [REF]: "set" });
    const input = addForm().getByLabelText("New secret value");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "new-password");
    expect(input).toHaveValue("");
    expect(screen.queryByRole("button", { name: /show|reveal|view/i })).not.toBeInTheDocument();
    expect(input.closest("form.ai-settings")).toBeNull();
    expect(document.querySelector(".ai-settings input[type='password']")).toBeNull();
  });

  it("never prefills an existing credential's field", async () => {
    const { user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await user.click(await row(REF).findByRole("button", { name: `Replace credential ${REF}` }));
    expect(row(REF).getByLabelText("New secret value")).toHaveValue("");
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("writes nothing to browser storage and logs nothing while storing, replacing and deleting", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const logs = (["log", "info", "warn", "error", "debug"] as const).map((level) => vi.spyOn(console, level).mockImplementation(() => undefined));
    const { user } = await open(settings());
    await storeNew(user);
    await user.click(await row(SECOND).findByRole("button", { name: `Replace credential ${SECOND}` }));
    await user.type(row(SECOND).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(SECOND).getByRole("button", { name: "Replace credential" }));
    await user.click(row(SECOND).getByRole("button", { name: "Replace credential" }));
    await user.click(await row(SECOND).findByRole("button", { name: `Delete credential ${SECOND}` }));
    await user.click(row(SECOND).getByRole("button", { name: "Delete credential" }));
    await row(SECOND).findByText("Not stored");
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    for (const log of logs) expect(log).not.toHaveBeenCalled();
  });

  it("never sends the secret in an ordinary AI settings PUT, and keeps unsaved settings edits across a credential change", async () => {
    const { api, user } = await open(withSelection({ connections: [managedConnection(), ollama()] }), {});
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.type(addForm().getByLabelText("Credential reference"), SECOND);
    await user.type(addForm().getByLabelText("New secret value"), SECRET_SENTINEL);
    // The secret is typed but not submitted: saving the settings must not carry it.
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(settingsPuts(api)).toHaveLength(1));
    expect(JSON.stringify(settingsPuts(api)[0]!.body)).not.toContain(SECRET_SENTINEL);
    expect(mutations(api)).toHaveLength(0);
    expect(Object.keys(settingsPuts(api)[0]!.body as object)).not.toContain("value");

    // A credential change with an unsaved settings edit leaves the edit alone and does not save it.
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(addForm().getByRole("button", { name: "Store new credential" }));
    await row(SECOND).findByText("Stored");
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("4500");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
    expect(settingsPuts(api)).toHaveLength(1);
    await waitFor(() => expect(callsTo(api, "POST", CREDENTIALS_PATH)).toHaveLength(1));
  });

  it("does not echo a refused secret in any message", async () => {
    const { user } = await open(settings(), {}, { fail: (call) => (call.method === "POST" ? apiError(400, "validation_error", { fields: { value: ["invalid_secret"] } }) : undefined) });
    await storeNew(user);
    expect(await addForm().findByText(/This secret cannot be stored/)).toBeInTheDocument();
    const text = Array.from(document.querySelectorAll(".field-error, [role='alert'], [role='status']")).map((e) => e.textContent ?? "").join(" ");
    expect(text).not.toContain(SECRET_SENTINEL);
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("drops a field a server adds to an answer, so a rogue value never reaches the screen", async () => {
    const { user } = await open(settings(), {}, {
      fail: (call) => (call.method === "POST" ? json(201, { reference: SECOND, source: "managed", status: "set", store: "dpapi_windows", connections: [], apply: "worker_restart_required", value: SECRET_SENTINEL, secret: SECRET_SENTINEL }) : undefined),
    });
    await storeNew(user);
    await row(SECOND).findByText("Stored");
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });
});

describe("CSRF", () => {
  it("refreshes a stale token and retries a credential change exactly once", async () => {
    let first = true;
    const { api, user } = await open(settings(), {}, {
      fail: (call) => {
        if (call.method === "POST" && first) {
          first = false;
          return apiError(403, "csrf_failed");
        }
        return undefined;
      },
    });
    await storeNew(user);
    expect(await row(SECOND).findByText("Stored")).toBeInTheDocument();
    expect(callsTo(api, "POST", CREDENTIALS_PATH)).toHaveLength(2);
    expect(api.calls.filter((c) => c.path === "/api/auth/csrf/")).toHaveLength(2);
  });
});

describe("errors", () => {
  const failing = (status: number, code: string, details?: Record<string, unknown>) => (call: RecordedCall) => (call.method !== "GET" ? apiError(status, code, details) : undefined);

  it.each([
    [503, "secret_store_disabled", /The secret store is disabled on the server/],
    [503, "secret_store_unavailable", /The secret store is unavailable\. Nothing was changed/],
    [503, "secret_store_busy", /The secret store is busy\. Nothing was changed/],
  ])("shows a fixed explanation for %s %s and keeps what was typed", async (status, code, text) => {
    const { user } = await open(settings(), {}, { fail: failing(status, code) });
    await storeNew(user);
    expect(await addForm().findByText(text)).toBeInTheDocument();
    expect(addForm().getByLabelText("New secret value")).toHaveValue(SECRET_SENTINEL);
    expect(panel().queryByText("Worker restart required.")).not.toBeInTheDocument();
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("warns that the secret store is disabled at section level when the status check finds out", async () => {
    await open(settings({ connections: [managedConnection()] }), {}, { fail: (call) => (call.method === "GET" ? apiError(503, "secret_store_disabled") : undefined) });
    expect(await panel().findByText(/The secret store is disabled on the server, so credentials cannot be stored, replaced or deleted/)).toBeInTheDocument();
  });

  it("shows the fields of a 400 validation_error beside them", async () => {
    const { user } = await open(settings(), {}, { fail: failing(400, "validation_error", { fields: { reference: ["invalid_reference"], value: ["too_long"] } }) });
    await storeNew(user);
    expect(await addForm().findByText(/The reference must be the prefix shown/)).toBeInTheDocument();
    expect(addForm().getByText(/The secret is too long/)).toBeInTheDocument();
  });

  it("on 409 credential_already_exists shows the current state, clears the secret and does not overwrite", async () => {
    const { api, user } = await open(settings(), { [SECOND]: "set" });
    await storeNew(user);
    expect(await addForm().findByText(/already stored under this reference\. It was not overwritten/)).toBeInTheDocument();
    expect(await row(SECOND).findByText("Stored")).toBeInTheDocument();
    expect(addForm().getByLabelText("New secret value")).toHaveValue("");
    expect(callsTo(api, "PUT", detailPath(SECOND))).toHaveLength(0);
    expect(panel().queryByText("Worker restart required.")).not.toBeInTheDocument();
  });

  it("on 404 credential_not_found while replacing refreshes the row and closes the form", async () => {
    const { creds, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await user.click(await row(REF).findByRole("button", { name: `Replace credential ${REF}` }));
    creds.stored.delete(REF);
    await user.type(row(REF).getByLabelText("New secret value"), SECRET_SENTINEL);
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    await user.click(row(REF).getByRole("button", { name: "Replace credential" }));
    expect(await row(REF).findByText("Not stored")).toBeInTheDocument();
    expect(row(REF).queryByLabelText("New secret value")).not.toBeInTheDocument();
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });

  it("explains a network failure as an unknown outcome, and keeps the form", async () => {
    const { user } = await open(settings(), {}, { fail: (call) => (call.method === "POST" ? "network-error" : undefined) });
    await storeNew(user);
    expect(await addForm().findByText(/The change may or may not have been applied: check the status/)).toBeInTheDocument();
    expect(addForm().getByLabelText("New secret value")).toHaveValue(SECRET_SENTINEL);
  });

  it("shows only the code for an unlisted answer, never a server message", async () => {
    const { user } = await open(settings(), {}, { fail: (call) => (call.method === "POST" ? json(500, { error: { code: "internal_error", message: SECRET_SENTINEL } }) : undefined) });
    await storeNew(user);
    expect(await addForm().findByText("The server reported an internal error. Try again later.")).toBeInTheDocument();
    expect(pageText()).not.toContain(SECRET_SENTINEL);
  });
});

describe("worker restart", () => {
  it("shows no restart notice before a change", async () => {
    await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    await row(REF).findByText("Stored");
    expect(panel().queryByText("Worker restart required.")).not.toBeInTheDocument();
  });

  it("says a restart is required after a change, does not claim it is active and offers no restart control", async () => {
    const { api, user } = await open(settings());
    await storeNew(user);
    const notice = (await panel().findByText("Worker restart required.")).closest(".notice")!;
    expect(notice).toHaveTextContent("not active until the worker is restarted");
    expect(notice).toHaveTextContent("This page cannot restart it");
    expect(pageText()).not.toMatch(/now active|active immediately|applied immediately|takes effect immediately/i);
    expect(screen.queryByRole("button", { name: /restart/i })).not.toBeInTheDocument();
    expect(api.calls.every((c) => c.path.startsWith("/api/auth/") || c.path.startsWith("/api/ai-"))).toBe(true);
  });

  it("keeps the notice after a later failed operation", async () => {
    let fail = false;
    const { user } = await open(settings(), {}, { fail: (call) => (fail && call.method === "POST" ? apiError(503, "secret_store_busy") : undefined) });
    await storeNew(user);
    await panel().findByText("Worker restart required.");
    fail = true;
    await storeNew(user, "AP_AI_CRED_THIRD");
    await addForm().findByText(/The secret store is busy/);
    expect(panel().getByText("Worker restart required.")).toBeInTheDocument();
  });
});

describe("credential source in the connection editor", () => {
  async function editCard(user: ReturnType<typeof userEvent.setup>, name: string) {
    const card = within(screen.getByRole("heading", { level: 3, name }).closest("article")!);
    await user.click(card.getByRole("button", { name: `Edit ${name}` }));
    return within(screen.getByRole("group", { name: /Edit connection/ }));
  }
  const saveButton = () => screen.getByRole("button", { name: "Save settings" });

  it("offers environment and managed, defaulting to environment for a new connection", async () => {
    const { user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.selectOptions(editor.getByLabelText("Provider type"), "openai_compatible");
    const source = editor.getByLabelText("Credential source");
    expect(source).toHaveValue("environment");
    expect(within(source).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["environment", "managed"]);
  });

  it("saves the chosen source with the connection, and the PUT carries no secret field", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    const editor = await editCard(user, "Company model server");
    await user.selectOptions(editor.getByLabelText("Credential source"), "managed");
    expect(editor.getByLabelText("Credential reference (name)")).toHaveValue(REF);
    await user.click(saveButton());
    await waitFor(() => expect(settingsPuts(api)).toHaveLength(1));
    const body = settingsPuts(api)[0]!.body as { connections: Array<Record<string, unknown>> };
    expect(body.connections[0]!.credential_source).toBe("managed");
    expect(body.connections[0]!.credential_ref).toBe(REF);
    expect(JSON.stringify(body)).not.toMatch(/"(value|secret|api_key|password|token)"/);
    expect(await screen.findByRole("heading", { level: 3, name: REF })).toBeInTheDocument();
  });

  it("requires a reference for a managed credential, with no request", async () => {
    const { api, user } = await open(settings({ connections: [connection({ credential_ref: null, locality: "local" })] }));
    const editor = await editCard(user, "Company model server");
    await user.selectOptions(editor.getByLabelText("Credential source"), "managed");
    await user.click(saveButton());
    expect(await screen.findByText("This is required.")).toBeInTheDocument();
    expect(settingsPuts(api)).toHaveLength(0);
  });

  it("keeps a managed connection managed when something else about it is edited", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    const editor = await editCard(user, "Company model server");
    await user.type(editor.getByLabelText("Display name"), "!");
    await user.click(saveButton());
    await waitFor(() => expect(settingsPuts(api)).toHaveLength(1));
    expect((settingsPuts(api)[0]!.body as { connections: Array<{ credential_source: string }> }).connections[0]!.credential_source).toBe("managed");
  });

  it("can switch a managed connection back to the environment explicitly", async () => {
    const { api, user } = await open(settings({ connections: [managedConnection()] }), { [REF]: "set" });
    const editor = await editCard(user, "Company model server");
    await user.selectOptions(editor.getByLabelText("Credential source"), "environment");
    await user.click(saveButton());
    await waitFor(() => expect(settingsPuts(api)).toHaveLength(1));
    expect((settingsPuts(api)[0]!.body as { connections: Array<{ credential_source: string }> }).connections[0]!.credential_source).toBe("environment");
  });

  it("offers no source for a provider type that takes no credential", async () => {
    const { user } = await open(settings({ connections: [ollama()] }));
    const editor = await editCard(user, "Ollama on this machine");
    expect(editor.queryByLabelText("Credential source")).not.toBeInTheDocument();
  });
});

describe("existing AI settings behaviour is unchanged", () => {
  it("still saves settings with the revision and shows the settings restart notice", async () => {
    const { api, user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(settingsPuts(api)).toHaveLength(1));
    expect(settingsPuts(api)[0]!.body).toMatchObject({ expected_revision: 3, timeout_seconds: 450 });
    expect(await screen.findByText("Worker restart required.")).toBeInTheDocument();
    expect(mutations(api)).toHaveLength(0);
  });

  it("makes no credential request for settings with no managed connection", async () => {
    const { api } = await open(withSelection());
    expect(api.calls.filter((c) => c.path.startsWith(CREDENTIALS_PATH))).toHaveLength(0);
  });

  it("keeps the credentials section after a stale-settings conflict is resolved", async () => {
    const server = settingsServer(withSelection(), { onPut: (call, current) => (call.method === "PUT" && current.revision === 3 ? apiError(409, "stale_settings_revision") : acceptPut({ current }, call)) });
    installFakeApi(signedIn(combined(server.handler, credentialServer({}).handler), administrator()));
    const user = userEvent.setup();
    renderApp("/settings/ai");
    await screen.findByRole("group", { name: "Extraction mode" });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("The settings were changed by someone else.")).toBeInTheDocument();
    expect(panel().getByText(/Credentials set as backend environment variables are not managed here/)).toBeInTheDocument();
  });
});
