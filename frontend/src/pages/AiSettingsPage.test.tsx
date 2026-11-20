import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SECRET_SENTINEL, SETTINGS_PATH, acceptPut, administrator, catalog, connection, ollama, settings, settingsServer, withSelection } from "../test/aiSettings";
import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { operator } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { signedIn } from "../test/server";
import type { AiSettings } from "../api/types";

type User = ReturnType<typeof userEvent.setup>;

afterEach(() => {
  vi.restoreAllMocks();
});

async function open(initial: AiSettings, options: Parameters<typeof settingsServer>[1] = {}) {
  const server = settingsServer(initial, options);
  const api = installFakeApi(signedIn(server.handler, administrator()));
  const user = userEvent.setup();
  renderApp("/settings/ai");
  await screen.findByRole("group", { name: "Extraction mode" });
  return { server, api, user };
}

const puts = (api: ReturnType<typeof installFakeApi>) => api.callsTo("PUT", SETTINGS_PATH);
const putBody = (api: ReturnType<typeof installFakeApi>, index = 0) => puts(api)[index]!.body as Record<string, unknown>;
const card = (name: string) => within(screen.getByRole("heading", { level: 3, name }).closest("article")!);
const saveButton = () => screen.getByRole("button", { name: "Save settings" });

async function editCard(user: User, name: string) {
  await user.click(card(name).getByRole("button", { name: `Edit ${name}` }));
  return within(screen.getByRole("group", { name: /Edit connection/ }));
}

describe("routing and permissions", () => {
  it("offers AI settings in the navigation to an administrator and opens the page", async () => {
    installFakeApi(signedIn(settingsServer(settings()).handler, administrator()));
    renderApp("/requests");
    const link = await screen.findByRole("link", { name: "AI settings" });
    expect(link).toHaveAttribute("href", "/settings/ai");
    await userEvent.setup().click(link);
    expect(await screen.findByRole("heading", { level: 1, name: "AI settings" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI settings" })).toHaveAttribute("aria-current", "page");
  });

  it("does not list AI settings for a user who is not an administrator", async () => {
    installFakeApi(signedIn(() => json(200, { count: 0, page: 1, page_size: 20, results: [] }), operator()));
    renderApp("/requests");
    await screen.findByRole("heading", { level: 1, name: "Requests" });
    expect(screen.queryByRole("link", { name: "AI settings" })).not.toBeInTheDocument();
  });

  it("shows a non-administrator who opens the URL nothing, and makes no settings request", async () => {
    const api = installFakeApi(signedIn(settingsServer(withSelection()).handler, operator()));
    renderApp("/settings/ai");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(screen.queryByText("Company model server")).not.toBeInTheDocument();
    expect(api.calls.filter((c) => c.path === SETTINGS_PATH)).toHaveLength(0);
  });

  it("treats the server's 403 as authoritative even for a user the page believed to be an administrator", async () => {
    installFakeApi(signedIn(() => apiError(403, "permission_denied"), administrator()));
    renderApp("/settings/ai");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(screen.getByText("You do not have permission to do this.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("asks for the settings with a GET that sends no CSRF header and uses the session cookie", async () => {
    const { api } = await open(settings());
    const get = api.callsTo("GET", SETTINGS_PATH)[0]!;
    expect(get.credentials).toBe("same-origin");
    expect(get.headers["x-csrftoken"]).toBeUndefined();
  });
});

describe("what the page shows", () => {
  it("starts in deterministic mode with no AI fields when no AI is configured", async () => {
    await open(settings());
    expect(screen.getByRole("radio", { name: /Deterministic parser/ })).toBeChecked();
    expect(screen.queryByLabelText("Active connection")).not.toBeInTheDocument();
    expect(screen.getByText(/No connections yet/)).toBeInTheDocument();
    expect(screen.getByText("0 of 10")).toBeInTheDocument();
  });

  it("lists the named connections with provider, locality, enabled state, status and the active one", async () => {
    await open(withSelection({ connections: [connection(), ollama({ enabled: false, status: { configured: false, issue: "invalid_configuration" } })] }));
    const corp = card("Company model server");
    expect(corp.getByText("OpenAI-compatible server")).toBeInTheDocument();
    expect(corp.getByText("Company endpoint")).toBeInTheDocument();
    expect(corp.getByText("Enabled")).toBeInTheDocument();
    expect(corp.getByText("Configured")).toBeInTheDocument();
    expect(corp.getByText("Active")).toBeInTheDocument();
    expect(corp.getByText("AP_AI_CRED_CORP_LLM")).toBeInTheDocument();
    expect(corp.getByText("corp-small")).toBeInTheDocument();
    const local = card("Ollama on this machine");
    expect(local.getByText("Disabled")).toBeInTheDocument();
    expect(local.getByText("Not configured")).toBeInTheDocument();
    expect(local.getByText("The configuration is not valid in the backend environment.")).toBeInTheDocument();
    expect(local.queryByText("Active")).not.toBeInTheDocument();
    expect(screen.getByText("2 of 10")).toBeInTheDocument();
  });

  it("shows the saved selection, policy and timeout", async () => {
    await open(withSelection());
    expect(screen.getByRole("radio", { name: /AI extraction/ })).toBeChecked();
    expect(screen.getByLabelText("Active connection")).toHaveValue("connection:corp-llm");
    expect(screen.getByLabelText("Model profile")).toHaveValue("corp-small");
    expect(screen.getByLabelText("Data locality policy")).toHaveValue("company_endpoint");
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("45");
  });

  it.each([
    ["missing_api_key", "The credential variable is not set in the backend environment."],
    ["invalid_configuration", "The configuration is not valid in the backend environment."],
    ["endpoint_not_approved", "The endpoint is not in the operator's approved list."],
  ])("explains the configuration status %s without any value", async (issue, text) => {
    await open(settings({ connections: [connection({ status: { configured: false, issue } })] }));
    expect(card("Company model server").getByText(text)).toBeInTheDocument();
  });

  it("warns that the worker will not start when the selected connection is not configured", async () => {
    await open(withSelection({ connections: [connection({ status: { configured: false, issue: "missing_api_key" } }), ollama()] }));
    expect(screen.getByText(/The selected connection is/)).toHaveTextContent("not configured");
  });

  it("states that a saved change needs a worker restart, and that nothing falls back or downloads, before any save", async () => {
    await open(settings());
    expect(screen.getByText(/Saved changes reach the worker only when it starts/)).toBeInTheDocument();
    expect(screen.getByText(/There is no automatic fallback/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is downloaded automatically/)).toBeInTheDocument();
    expect(screen.queryByText("Worker restart required.")).not.toBeInTheDocument();
  });

  it("reports connections the server could not read", async () => {
    await open(settings({ unreadable_connections: 2 }));
    expect(screen.getByText(/2 stored connections could not be read/)).toBeInTheDocument();
  });

  it("shows a loading state and then an error with a retry when the settings cannot be loaded", async () => {
    let fail = true;
    const server = settingsServer(settings());
    installFakeApi(signedIn((call) => (fail ? "network-error" : server.handler(call)), administrator()));
    const user = userEvent.setup();
    renderApp("/settings/ai");
    expect(await screen.findByText("Could not load the AI settings")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("group", { name: "Extraction mode" })).toBeInTheDocument();
  });

  it("refuses a malformed answer instead of showing something", async () => {
    installFakeApi(signedIn(() => json(200, { settings: {} }), administrator()));
    renderApp("/settings/ai");
    expect(await screen.findByText("Could not load the AI settings")).toBeInTheDocument();
  });
});

describe("selecting the connection, model, policy and timeout", () => {
  it("saves an explicit selection with no fallback fields", async () => {
    const { api, user } = await open(settings({ connections: [connection(), ollama()] }));
    await user.click(screen.getByRole("radio", { name: /AI extraction/ }));
    await user.selectOptions(screen.getByLabelText("Active connection"), "connection:ollama-local");
    await user.selectOptions(screen.getByLabelText("Model profile"), "llama");
    await user.selectOptions(screen.getByLabelText("Data locality policy"), "local_only");
    await user.clear(screen.getByLabelText("Timeout (seconds)"));
    await user.type(screen.getByLabelText("Timeout (seconds)"), "30");
    await user.click(saveButton());

    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).toEqual({
      extraction_mode: "ai",
      provider_id: "ollama",
      connection_id: "ollama-local",
      profile_id: "llama",
      policy_mode: "local_only",
      timeout_seconds: 30,
      expected_revision: 3,
    });
  });

  it("never chooses a model or a policy by itself when the connection changes", async () => {
    const { user } = await open(withSelection());
    await user.selectOptions(screen.getByLabelText("Active connection"), "connection:ollama-local");
    expect(screen.getByLabelText("Model profile")).toHaveValue("");
    await user.click(saveButton());
    expect(await screen.findByText("This is required.")).toBeInTheDocument();
  });

  it("offers only a connection's own models and only enabled connections", async () => {
    await open(withSelection({ connections: [connection(), ollama({ enabled: false })] }));
    const profile = within(screen.getByLabelText("Model profile"));
    expect(profile.getByRole("option", { name: "corp-small — corp-model-8b" })).toBeInTheDocument();
    expect(profile.queryByRole("option", { name: /llama/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Ollama on this machine.*disabled/ })).toBeDisabled();
  });

  it("can select the environment-configured provider and sends no connection identifier for it", async () => {
    const { api, user } = await open(settings());
    await user.click(screen.getByRole("radio", { name: /AI extraction/ }));
    await user.selectOptions(screen.getByLabelText("Active connection"), "legacy:kpmg_workbench");
    await user.selectOptions(screen.getByLabelText("Model profile"), "workbench-gpt5");
    await user.selectOptions(screen.getByLabelText("Data locality policy"), "company_endpoint");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).toMatchObject({ extraction_mode: "ai", provider_id: "kpmg_workbench", connection_id: null, profile_id: "workbench-gpt5", timeout_seconds: 45 });
    expect(putBody(api)).not.toHaveProperty("connections");
  });

  it("returns to deterministic mode with every AI field null", async () => {
    const { api, user } = await open(withSelection());
    await user.click(screen.getByRole("radio", { name: /Deterministic parser/ }));
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).toEqual({ extraction_mode: "deterministic", provider_id: null, connection_id: null, profile_id: null, policy_mode: null, timeout_seconds: null, expected_revision: 3 });
  });
});

describe("policy and field validation", () => {
  it("disables the policies the catalog does not permit for the selected locality", async () => {
    await open(withSelection());
    expect(screen.getByRole("option", { name: /Local only/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: "Company endpoint" })).toBeEnabled();
    expect(screen.getByRole("option", { name: "Approved cloud" })).toBeEnabled();
  });

  it("refuses to send a policy the locality does not permit, and says why", async () => {
    const vendor = connection({ connection_id: "vendor", display_name: "Vendor cloud", locality: "cloud", profiles: [{ profile_id: "vendor-chat", model_id: "vendor-model" }] });
    const loaded = settings({ settings: { extraction_mode: "ai", provider_id: "openai_compatible", connection_id: "vendor", profile_id: "vendor-chat", policy_mode: "company_endpoint", timeout_seconds: 45 }, connections: [vendor] });
    const { api, user } = await open(loaded);
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    expect(await screen.findByText("This data policy does not permit the selected connection's locality.")).toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);
  });

  it("asks for explicit approval before saving a selection that sends text to a cloud provider", async () => {
    const vendor = connection({ connection_id: "vendor", display_name: "Vendor cloud", locality: "cloud", profiles: [{ profile_id: "vendor-chat", model_id: "vendor-model" }] });
    const { api, user } = await open(settings({ connections: [vendor] }));
    await user.click(screen.getByRole("radio", { name: /AI extraction/ }));
    await user.selectOptions(screen.getByLabelText("Active connection"), "connection:vendor");
    await user.selectOptions(screen.getByLabelText("Model profile"), "vendor-chat");
    await user.selectOptions(screen.getByLabelText("Data locality policy"), "approved_cloud");
    await user.click(saveButton());
    expect(await screen.findByText("Approve sending request text to a cloud provider?")).toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Approve and save" }));
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).toMatchObject({ connection_id: "vendor", policy_mode: "approved_cloud" });
  });

  it.each([
    ["", "This is required."],
    ["abc", "This value is not valid."],
    ["4", "This value is outside the allowed range."],
    ["700", "This value is outside the allowed range."],
  ])("refuses the timeout %j before sending anything", async (value, message) => {
    const { api, user } = await open(withSelection());
    const field = screen.getByLabelText("Timeout (seconds)");
    await user.clear(field);
    if (value) await user.type(field, value);
    await user.click(saveButton());
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(puts(api)).toHaveLength(0);
  });

  it("states the allowed timeout range from the catalog", async () => {
    await open(withSelection({ catalog: catalog({ timeout: { min: 10, max: 90, default: 60 } }) }));
    expect(screen.getByText(/Between 10 and 90 seconds; the default is 60/)).toBeInTheDocument();
  });

  it("keeps Save disabled until something changed", async () => {
    const { user } = await open(withSelection());
    expect(saveButton()).toBeDisabled();
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    expect(saveButton()).toBeEnabled();
  });
});

describe("adding, editing, disabling and removing connections", () => {
  it("adds a connection and sends the complete list with the revision it was based on", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.type(editor.getByLabelText("Display name"), "Local Ollama");
    await user.type(editor.getByLabelText("Identifier"), "ollama-two");
    await user.selectOptions(editor.getByLabelText("Provider type"), "ollama");
    await user.type(editor.getByLabelText("Endpoint"), "http://127.0.0.1:11434");
    await user.click(editor.getByRole("button", { name: "Add model profile" }));
    await user.type(editor.getByLabelText("Profile 1 identifier"), "llama");
    await user.type(editor.getByLabelText("Profile 1 model"), "llama3.1:8b");
    expect(screen.getByText("2 of 10")).toBeInTheDocument();
    await user.click(saveButton());

    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).toEqual({
      extraction_mode: "deterministic",
      provider_id: null,
      connection_id: null,
      profile_id: null,
      policy_mode: null,
      timeout_seconds: null,
      expected_revision: 3,
      connections: [
        { connection_id: "corp-llm", display_name: "Company model server", provider_type: "openai_compatible", locality: "company_endpoint", endpoint: "https://llm.corp.example.invalid/v1", credential_ref: "AP_AI_CRED_CORP_LLM", enabled: true, profiles: [{ profile_id: "corp-small", model_id: "corp-model-8b" }] },
        { connection_id: "ollama-two", display_name: "Local Ollama", provider_type: "ollama", locality: "local", endpoint: "http://127.0.0.1:11434", credential_ref: null, enabled: true, profiles: [{ profile_id: "llama", model_id: "llama3.1:8b" }] },
      ],
    });
    expect(await screen.findByRole("heading", { level: 3, name: "Local Ollama" })).toBeInTheDocument();
  });

  it("starts the fields of a connection again when its provider type changes", async () => {
    const { user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.selectOptions(editor.getByLabelText("Provider type"), "openai_compatible");
    expect(editor.getByLabelText("Endpoint")).toBeInTheDocument();
    expect(editor.getByLabelText("Credential reference (variable name)")).toBeInTheDocument();
    await user.selectOptions(editor.getByLabelText("Provider type"), "ollama");
    expect(editor.queryByLabelText("Credential reference (variable name)")).not.toBeInTheDocument();
    expect(editor.getByLabelText("Endpoint")).toHaveValue("");
  });

  it("offers the fixed model profiles of a provider that has them as a choice, never a free-text model", async () => {
    const { api, user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.type(editor.getByLabelText("Display name"), "Workbench");
    await user.type(editor.getByLabelText("Identifier"), "workbench");
    expect(editor.queryByLabelText("Profile 1 model")).not.toBeInTheDocument();
    await user.click(editor.getByRole("checkbox", { name: /workbench-gpt5/ }));
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect((putBody(api).connections as Array<{ profiles: unknown }>)[0]!.profiles).toEqual([{ profile_id: "workbench-gpt5", model_id: "gpt-5" }]);
  });

  it("explains that models are never downloaded", async () => {
    const { user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.selectOptions(editor.getByLabelText("Provider type"), "ollama");
    expect(editor.getByText(/never downloads, pulls, lists or deletes models/)).toBeInTheDocument();
  });

  it("edits an existing connection; its identifier and provider type are fixed", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    const editor = await editCard(user, "Company model server");
    expect(editor.getByLabelText("Identifier")).toHaveAttribute("readonly");
    expect(editor.getByLabelText("Provider type")).toBeDisabled();
    await user.clear(editor.getByLabelText("Display name"));
    await user.type(editor.getByLabelText("Display name"), "Renamed server");
    await user.clear(editor.getByLabelText("Endpoint"));
    await user.type(editor.getByLabelText("Endpoint"), "https://llm2.corp.example.invalid/v1");
    expect(screen.getByText("Edited")).toBeInTheDocument();
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    const sent = (putBody(api).connections as Array<Record<string, unknown>>)[0]!;
    expect(sent).toMatchObject({ connection_id: "corp-llm", display_name: "Renamed server", endpoint: "https://llm2.corp.example.invalid/v1" });
  });

  it("leaves the stored connection list alone when only other settings change", async () => {
    const { api, user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api)).not.toHaveProperty("connections");
  });

  it("asks before removing a connection, and sends nothing until the settings are saved", async () => {
    const { api, user } = await open(settings({ connections: [connection(), ollama()] }));
    await user.click(card("Ollama on this machine").getByRole("button", { name: "Remove Ollama on this machine" }));
    expect(screen.getByText("Remove “Ollama on this machine”?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("heading", { level: 3, name: "Ollama on this machine" })).toBeInTheDocument();

    await user.click(card("Ollama on this machine").getByRole("button", { name: "Remove Ollama on this machine" }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(screen.queryByRole("heading", { level: 3, name: "Ollama on this machine" })).not.toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);

    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect((putBody(api).connections as Array<{ connection_id: string }>).map((c) => c.connection_id)).toEqual(["corp-llm"]);
  });

  it("can remove the last connection by sending an empty list", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    await user.click(card("Company model server").getByRole("button", { name: "Remove Company model server" }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(putBody(api).connections).toEqual([]);
  });

  it("asks before disabling a connection and then saves it as disabled", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    await user.click(card("Company model server").getByRole("button", { name: "Disable Company model server" }));
    expect(screen.getByText("Disable “Company model server”?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Disable connection" }));
    expect(card("Company model server").getByText("Disabled")).toBeInTheDocument();
    expect(card("Company model server").getByRole("button", { name: "Enable Company model server" })).toBeInTheDocument();
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect((putBody(api).connections as Array<{ enabled: boolean }>)[0]!.enabled).toBe(false);
  });

  it("does not allow the active connection to be disabled or removed", async () => {
    await open(withSelection());
    const corp = card("Company model server");
    expect(corp.getByRole("button", { name: "Disable Company model server" })).toBeDisabled();
    expect(corp.getByRole("button", { name: "Remove Company model server" })).toBeDisabled();
    expect(corp.getByText(/Choose another one, or deterministic mode, before disabling or removing it/)).toBeInTheDocument();
    expect(card("Ollama on this machine").getByRole("button", { name: "Remove Ollama on this machine" })).toBeEnabled();
  });

  it("discards an unsaved new connection without asking, because nothing exists to lose", async () => {
    const { user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    await user.click(screen.getByRole("button", { name: "Remove New connection" }));
    expect(screen.queryByRole("heading", { level: 3, name: "New connection" })).not.toBeInTheDocument();
  });

  it("allows at most ten connections", async () => {
    const ten = Array.from({ length: 10 }, (_, i) => connection({ connection_id: `conn-${i}`, display_name: `Connection ${i}` }));
    const { user } = await open(settings({ connections: ten }));
    expect(screen.getByText("10 of 10")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add connection" })).toBeDisabled();
    expect(screen.getByText(/maximum of 10 connections is reached/)).toBeInTheDocument();
    await user.click(card("Connection 3").getByRole("button", { name: "Remove Connection 3" }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(screen.getByRole("button", { name: "Add connection" })).toBeEnabled();
  });

  it("refuses a duplicate identifier before sending", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }));
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.type(editor.getByLabelText("Display name"), "Another");
    await user.type(editor.getByLabelText("Identifier"), "corp-llm");
    await user.selectOptions(editor.getByLabelText("Provider type"), "ollama");
    await user.type(editor.getByLabelText("Endpoint"), "http://127.0.0.1:11434");
    await user.click(editor.getByRole("button", { name: "Add model profile" }));
    await user.type(editor.getByLabelText("Profile 1 identifier"), "llama");
    await user.type(editor.getByLabelText("Profile 1 model"), "llama3.1:8b");
    await user.click(saveButton());
    expect(await screen.findByText("Another connection already uses this identifier.")).toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);
  });

  it("discards unsaved edits only after confirmation", async () => {
    const { user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByText("Discard your unsaved changes?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("450");
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    await user.click(screen.getAllByRole("button", { name: "Discard changes" })[0]!);
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("45");
    expect(saveButton()).toBeDisabled();
  });
});

describe("credentials", () => {
  it("has no field for a key, token, password or header, and no password input", async () => {
    const { user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.selectOptions(editor.getByLabelText("Provider type"), "openai_compatible");
    expect(document.querySelector("input[type='password']")).toBeNull();
    for (const label of Array.from(document.querySelectorAll("label")).map((l) => l.textContent ?? "")) {
      expect(label).not.toMatch(/api[ -]?key|secret|token|password|authorization|header|charge/i);
    }
    expect(editor.getByLabelText("Credential reference (variable name)")).toBeInTheDocument();
    expect(editor.getByText(/never typed, sent or shown here/)).toBeInTheDocument();
  });

  it("refuses a value that is not a variable name, never sends it and never repeats it", async () => {
    const { api, user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.type(editor.getByLabelText("Display name"), "Pasted a key");
    await user.type(editor.getByLabelText("Identifier"), "pasted");
    await user.selectOptions(editor.getByLabelText("Provider type"), "openai_compatible");
    await user.selectOptions(editor.getByLabelText("Data locality"), "company_endpoint");
    await user.type(editor.getByLabelText("Endpoint"), "https://llm.corp.example.invalid/v1");
    await user.type(editor.getByLabelText("Credential reference (variable name)"), SECRET_SENTINEL);
    await user.click(editor.getByRole("button", { name: "Add model profile" }));
    await user.type(editor.getByLabelText("Profile 1 identifier"), "p");
    await user.type(editor.getByLabelText("Profile 1 model"), "m");
    await user.click(saveButton());

    expect(await screen.findByText(/Enter only the NAME of a backend environment variable/)).toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);
    const messages = Array.from(document.querySelectorAll(".field-error, [role='alert'], [role='status']")).map((e) => e.textContent ?? "");
    expect(messages.join(" ")).not.toContain(SECRET_SENTINEL);
  });

  it("accepts a well-formed reference and requires one for a non-local OpenAI-compatible endpoint", async () => {
    const { api, user } = await open(settings());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    const editor = within(screen.getByRole("group", { name: /Edit connection/ }));
    await user.type(editor.getByLabelText("Display name"), "Corp");
    await user.type(editor.getByLabelText("Identifier"), "corp");
    await user.selectOptions(editor.getByLabelText("Provider type"), "openai_compatible");
    await user.selectOptions(editor.getByLabelText("Data locality"), "company_endpoint");
    await user.type(editor.getByLabelText("Endpoint"), "https://llm.corp.example.invalid/v1");
    await user.click(editor.getByRole("button", { name: "Add model profile" }));
    await user.type(editor.getByLabelText("Profile 1 identifier"), "p");
    await user.type(editor.getByLabelText("Profile 1 model"), "m");
    await user.click(saveButton());
    expect(await screen.findByText("This is required.")).toBeInTheDocument();
    expect(puts(api)).toHaveLength(0);

    await user.type(editor.getByLabelText("Credential reference (variable name)"), "AP_AI_CRED_CORP");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect((putBody(api).connections as Array<{ credential_ref: string }>)[0]!.credential_ref).toBe("AP_AI_CRED_CORP");
  });

  it("drops any field the contract does not list, so a rogue secret in an answer never reaches the screen or a request", async () => {
    const rogue = { ...withSelection(), api_key: SECRET_SENTINEL, settings: { ...withSelection().settings, secret: SECRET_SENTINEL }, connections: [{ ...connection(), api_key: SECRET_SENTINEL, token: SECRET_SENTINEL }] };
    const server = settingsServer(withSelection());
    const api = installFakeApi(signedIn((call) => (call.method === "GET" ? json(200, rogue) : server.handler(call)), administrator()));
    const user = userEvent.setup();
    renderApp("/settings/ai");
    await screen.findByRole("group", { name: "Extraction mode" });
    expect(document.body.textContent).not.toContain(SECRET_SENTINEL);
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await editCard(user, "Company model server");
    await user.type(screen.getByLabelText("Display name"), "!");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    expect(JSON.stringify(puts(api)[0]!.body)).not.toContain(SECRET_SENTINEL);
    expect(document.body.textContent).not.toContain(SECRET_SENTINEL);
  });

  it("writes nothing to browser storage and logs nothing while loading, editing and saving", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const logs = (["log", "info", "warn", "error", "debug"] as const).map((level) => vi.spyOn(console, level).mockImplementation(() => undefined));
    const { user } = await open(withSelection());
    await editCard(user, "Company model server");
    await user.type(screen.getByLabelText("Credential reference (variable name)"), "X");
    await user.click(saveButton());
    await screen.findByText("Worker restart required.");
    expect(setItem).not.toHaveBeenCalled();
    for (const log of logs) expect(log).not.toHaveBeenCalled();
  });

  it("sends only the fields of the contract", async () => {
    const { api, user } = await open(withSelection());
    await editCard(user, "Company model server");
    await user.type(screen.getByLabelText("Display name"), "!");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    const body = putBody(api);
    expect(Object.keys(body).sort()).toEqual(["connection_id", "connections", "expected_revision", "extraction_mode", "policy_mode", "profile_id", "provider_id", "timeout_seconds"]);
    for (const c of body.connections as Array<Record<string, unknown>>) {
      expect(Object.keys(c).sort()).toEqual(["connection_id", "credential_ref", "display_name", "enabled", "endpoint", "locality", "profiles", "provider_type"]);
    }
  });
});

describe("saving, CSRF and the worker restart", () => {
  it("sends the PUT with the CSRF token, JSON and the session cookie", async () => {
    const { api, user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    await waitFor(() => expect(puts(api)).toHaveLength(1));
    const put = puts(api)[0]!;
    expect(put.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(put.headers["content-type"]).toBe("application/json");
    expect(put.credentials).toBe("same-origin");
  });

  it("refreshes a stale CSRF token and retries the save exactly once", async () => {
    let first = true;
    const { api, user } = await open(withSelection(), {
      onPut: (call, current) => {
        if (first) {
          first = false;
          return apiError(403, "csrf_failed");
        }
        const state = { current };
        return acceptPut(state, call);
      },
    });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    await screen.findByText("Worker restart required.");
    expect(puts(api)).toHaveLength(2);
    expect(api.calls.filter((c) => c.path === "/api/auth/csrf/")).toHaveLength(2);
  });

  it("says that a worker restart is required after saving, and does not claim the settings are active", async () => {
    const { api, user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    const notice = (await screen.findByText("Worker restart required.")).closest(".notice")!;
    expect(notice).toHaveTextContent("Revision 4 is saved");
    expect(notice).toHaveTextContent("not active until the worker is restarted");
    expect(notice).toHaveTextContent("This page cannot restart it");
    expect(document.body.textContent).not.toMatch(/now active|active immediately|applied immediately/i);
    expect(screen.queryByRole("button", { name: /restart/i })).not.toBeInTheDocument();
    expect(api.calls.every((c) => c.path.startsWith("/api/auth/") || c.path === SETTINGS_PATH)).toBe(true);
  });

  it("shows the saved revision and reads as saved again", async () => {
    const { user } = await open(withSelection());
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    await screen.findByText("Worker restart required.");
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("450");
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText(/Revision 4/, { selector: ".save-meta" })).toBeInTheDocument();
  });

  it("prevents a double submit while a save is in flight", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { api, user } = await open(withSelection(), {
      onPut: async (call, current) => {
        await gate;
        return acceptPut({ current }, call);
      },
    });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.dblClick(saveButton());
    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    release();
    await screen.findByText("Worker restart required.");
    expect(puts(api)).toHaveLength(1);
  });
});

describe("errors", () => {
  it("shows a 400 validation_error beside the field, opens the connection and keeps every edit", async () => {
    const { api, user } = await open(settings({ connections: [connection()] }), {
      onPut: () => apiError(400, "validation_error", { fields: { "connections[0].endpoint": ["endpoint_not_loopback"], timeout_seconds: ["exceeds_worker_lease"] } }),
    });
    const editor = await editCard(user, "Company model server");
    await user.clear(editor.getByLabelText("Display name"));
    await user.type(editor.getByLabelText("Display name"), "My edit");
    await user.click(screen.getByRole("button", { name: "Done editing" }));
    await user.click(saveButton());

    expect(await screen.findByText(/Some values are not valid, so nothing was saved/)).toBeInTheDocument();
    expect(await screen.findByText("A local endpoint must be on this machine (localhost or a loopback address).")).toBeInTheDocument();
    expect(screen.getByLabelText("Endpoint")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Display name")).toHaveValue("My edit");
    expect(puts(api)).toHaveLength(1);
    expect(screen.queryByText("Worker restart required.")).not.toBeInTheDocument();
  });

  it("shows a server timeout refusal beside the timeout and keeps the typed value", async () => {
    const { user } = await open(withSelection(), { onPut: () => apiError(400, "validation_error", { fields: { timeout_seconds: ["exceeds_worker_lease"] } }) });
    await user.clear(screen.getByLabelText("Timeout (seconds)"));
    await user.type(screen.getByLabelText("Timeout (seconds)"), "100");
    await user.click(saveButton());
    expect(await screen.findByText(/longer than the worker's lease allows/)).toBeInTheDocument();
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("100");
  });

  it("shows a refusal it cannot place beside a field, without echoing anything", async () => {
    const { user } = await open(withSelection(), { onPut: () => apiError(400, "validation_error", { fields: { _unknown: ["unknown_field"] } }) });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    expect(await screen.findByText("This field is not recognised.")).toBeInTheDocument();
  });

  it("explains a 403 on save and keeps the edits", async () => {
    const { user } = await open(withSelection(), { onPut: () => apiError(403, "permission_denied") });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    expect(await screen.findByText(/You do not have permission to do this\. Nothing was saved, and your edits are still here\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("450");
    expect(saveButton()).toBeEnabled();
  });

  it("keeps the edits when the network fails, and a second attempt can succeed", async () => {
    let fail = true;
    const { api, user } = await open(withSelection(), {
      onPut: (call, current) => {
        if (fail) return "network-error";
        return acceptPut({ current }, call);
      },
    });
    await user.type(screen.getByLabelText("Timeout (seconds)"), "0");
    await user.click(saveButton());
    expect(await screen.findByText(/The server could not be reached/)).toBeInTheDocument();
    expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("450");
    expect(screen.queryByText("Worker restart required.")).not.toBeInTheDocument();
    fail = false;
    await user.click(saveButton());
    await screen.findByText("Worker restart required.");
    expect(puts(api)).toHaveLength(2);
  });

  describe("a revision conflict (409 stale_settings_revision)", () => {
    function conflictServer() {
      let conflicted = false;
      const holder: { server?: ReturnType<typeof settingsServer> } = {};
      holder.server = settingsServer(withSelection({ revision: 3 }), {
        onPut: (call, current) => {
          if (!conflicted) {
            conflicted = true;
            holder.server!.state.current = {
              ...current,
              revision: 4,
              settings: { ...current.settings, timeout_seconds: 60 },
              connections: [...current.connections, connection({ connection_id: "added-elsewhere", display_name: "Added elsewhere" })],
            };
            return apiError(409, "stale_settings_revision");
          }
          return acceptPut(holder.server!.state, call);
        },
      });
      return holder.server;
    }

    async function conflictedPage() {
      const server = conflictServer();
      const api = installFakeApi(signedIn(server.handler, administrator()));
      const user = userEvent.setup();
      renderApp("/settings/ai");
      await screen.findByRole("group", { name: "Extraction mode" });
      await user.clear(screen.getByLabelText("Timeout (seconds)"));
      await user.type(screen.getByLabelText("Timeout (seconds)"), "20");
      await user.click(saveButton());
      await screen.findByText("The settings were changed by someone else.");
      return { api, user };
    }

    it("changes nothing, keeps the edits and offers an explicit choice", async () => {
      await conflictedPage();
      expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("20");
      expect(screen.getByText(/The latest settings are revision 4/)).toBeInTheDocument();
      expect(screen.queryByText("Worker restart required.")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Keep my edits on top of the latest" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Discard my edits and load the latest" })).toBeInTheDocument();
    });

    it("keeps the edits on the latest revision and does not overwrite another administrator's connection", async () => {
      const { api, user } = await conflictedPage();
      await user.click(screen.getByRole("button", { name: "Keep my edits on top of the latest" }));
      expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("20");
      expect(screen.getByRole("heading", { level: 3, name: "Added elsewhere" })).toBeInTheDocument();
      await user.click(saveButton());
      await screen.findByText("Worker restart required.");
      expect(puts(api)).toHaveLength(2);
      expect(putBody(api, 1)).toMatchObject({ expected_revision: 4, timeout_seconds: 20 });
      expect(putBody(api, 1)).not.toHaveProperty("connections");
    });

    it("lets the user discard their edits and load the latest settings", async () => {
      const { user } = await conflictedPage();
      await user.click(screen.getByRole("button", { name: "Discard my edits and load the latest" }));
      expect(screen.getByLabelText("Timeout (seconds)")).toHaveValue("60");
      expect(screen.getByRole("heading", { level: 3, name: "Added elsewhere" })).toBeInTheDocument();
      expect(screen.queryByText("The settings were changed by someone else.")).not.toBeInTheDocument();
      expect(saveButton()).toBeDisabled();
    });

    it("keeps the user's own connection list when they had edited it", async () => {
      const server = conflictServer();
      const api = installFakeApi(signedIn(server.handler, administrator()));
      const user = userEvent.setup();
      renderApp("/settings/ai");
      await screen.findByRole("group", { name: "Extraction mode" });
      await user.click(card("Ollama on this machine").getByRole("button", { name: "Remove Ollama on this machine" }));
      await user.click(screen.getByRole("button", { name: "Remove connection" }));
      await user.click(saveButton());
      await user.click(await screen.findByRole("button", { name: "Keep my edits on top of the latest" }));
      expect(screen.queryByRole("heading", { level: 3, name: "Added elsewhere" })).not.toBeInTheDocument();
      await user.click(saveButton());
      await screen.findByText("Worker restart required.");
      expect((putBody(api, 1).connections as Array<{ connection_id: string }>).map((c) => c.connection_id)).toEqual(["corp-llm"]);
      expect(putBody(api, 1).expected_revision).toBe(4);
    });
  });
});

describe("existing screens", () => {
  it("still sends the other pages' requests unchanged and does not touch the settings endpoint", async () => {
    const handler: Handler = (call) => (call.path === "/api/requests/" ? json(200, { count: 0, page: 1, page_size: 20, results: [] }) : apiError(404, "not_found"));
    const api = installFakeApi(signedIn(handler, administrator()));
    renderApp("/requests");
    await screen.findByRole("heading", { level: 1, name: "Requests" });
    expect(api.calls.filter((c) => c.path === SETTINGS_PATH)).toHaveLength(0);
  });
});
