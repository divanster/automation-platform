import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CONNECTIONS_PREFIX,
  SECRET_SENTINEL,
  SETTINGS_PATH,
  administrator,
  connection,
  connectionTestPath,
  ollama,
  settings,
  testResult,
  testingServer,
  withSelection,
} from "../test/aiSettings";
import { apiError, installFakeApi, json, type FakeResponse, type Handler, type RecordedCall } from "../test/fakeApi";
import { operator } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { deferred, signedIn } from "../test/server";
import type { AiSettings } from "../api/types";

afterEach(() => {
  vi.restoreAllMocks();
});

const CORP = "Company model server";
const OLLAMA = "Ollama on this machine";
const TWO_PROFILES = [
  { profile_id: "corp-small", model_id: "corp-model-8b" },
  { profile_id: "corp-large", model_id: "corp-model-70b" },
];

const card = (name: string) => within(screen.getByRole("heading", { level: 3, name }).closest("article")!);
const testButton = (name: string) => card(name).getByRole("button", { name: `Test connection ${name}` });
const tests = (api: ReturnType<typeof installFakeApi>) => api.calls.filter((c) => c.path.startsWith(CONNECTIONS_PREFIX));

async function open(initial: AiSettings, onTest: (call: RecordedCall) => ReturnType<Handler> = () => json(200, testResult())) {
  const { server, handler } = testingServer(initial, onTest);
  const api = installFakeApi(signedIn(handler, administrator()));
  const user = userEvent.setup();
  renderApp("/settings/ai");
  await screen.findByRole("group", { name: "Extraction mode" });
  return { server, api, user };
}

describe("access", () => {
  it("offers nothing to a non-administrator and sends no test request", async () => {
    const { handler } = testingServer(withSelection(), () => json(200, testResult()));
    const api = installFakeApi(signedIn(handler, operator()));
    renderApp("/settings/ai");
    expect(await screen.findByText("Not available for your account")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Test connection/ })).not.toBeInTheDocument();
    expect(tests(api)).toHaveLength(0);
  });

  it("shows a Test connection action on every saved connection for an administrator", async () => {
    await open(withSelection());
    expect(testButton(CORP)).toBeEnabled();
    expect(testButton(OLLAMA)).toBeEnabled();
    expect(screen.getAllByText("Connection test checks the Django web process. The worker may use a different environment or Windows account.")).toHaveLength(2);
  });

  it("treats a 403 permission_denied as authoritative", async () => {
    const { user } = await open(withSelection(), () => apiError(403, "permission_denied"));
    await user.click(testButton(CORP));
    expect(await card(CORP).findByRole("alert")).toHaveTextContent(/permission to test connections/);
  });
});

describe("what can be tested", () => {
  it("does not allow testing a connection that is not saved", async () => {
    const { api, user } = await open(withSelection());
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    expect(screen.getByText(/Save this connection before testing it/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Test connection / })).toHaveLength(2);
    expect(tests(api)).toHaveLength(0);
  });

  it("does not allow testing a connection that is saved as disabled", async () => {
    const { api } = await open(settings({ connections: [connection({ enabled: false })] }));
    expect(testButton(CORP)).toBeDisabled();
    expect(card(CORP).getByText(/saved as disabled/)).toBeInTheDocument();
    expect(tests(api)).toHaveLength(0);
  });

  it("does not allow testing a connection with no saved model profile", async () => {
    await open(settings({ connections: [connection({ profiles: [] })] }));
    expect(testButton(CORP)).toBeDisabled();
    expect(card(CORP).getByText(/no saved model profile/)).toBeInTheDocument();
  });

  it("offers only the SAVED profiles, and tests the saved version, not the unsaved edits", async () => {
    const { api, user } = await open(withSelection(), () => json(200, testResult({ connection_id: "ollama-local", profile_id: "llama", provider_type: "ollama", locality: "local" })));
    await user.click(card(OLLAMA).getByRole("button", { name: `Disable ${OLLAMA}` }));
    await user.click(screen.getByRole("button", { name: "Disable connection" }));
    expect(card(OLLAMA).getByText("Disabled")).toBeInTheDocument();
    expect(card(OLLAMA).getByText(/uses the saved version of this connection, not your unsaved edits/)).toBeInTheDocument();
    expect(testButton(OLLAMA)).toBeEnabled();
    await user.click(testButton(OLLAMA));
    expect(await card(OLLAMA).findByText("Test passed")).toBeInTheDocument();
    expect(tests(api)).toHaveLength(1);
  });
});

describe("running a test", () => {
  it("sends exactly the saved profile id to the connection's own URL, with CSRF, and shows success and the duration", async () => {
    const { api, user } = await open(withSelection());
    await user.click(testButton(CORP));
    const result = await card(CORP).findByText("Test passed");
    expect(result).toBeInTheDocument();
    expect(card(CORP).getByText("412 ms")).toBeInTheDocument();
    expect(card(CORP).getByText(/does not show that the worker can use this connection/)).toBeInTheDocument();
    const post = api.callsTo("POST", connectionTestPath("corp-llm"));
    expect(post).toHaveLength(1);
    expect(post[0]!.body).toEqual({ profile_id: "corp-small" });
    expect(post[0]!.headers["x-csrftoken"]).toBe("csrf-test-token");
    expect(tests(api)).toHaveLength(1);
  });

  it("tests the profile the administrator selected", async () => {
    const { api, user } = await open(settings({ connections: [connection({ profiles: TWO_PROFILES })] }), () => json(200, testResult({ profile_id: "corp-large" })));
    await user.selectOptions(card(CORP).getByLabelText("Model profile to test"), "corp-large");
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))[0]!.body).toEqual({ profile_id: "corp-large" });
    expect(card(CORP).getByText("profile corp-large")).toBeInTheDocument();
  });

  it("shows a testing state and blocks duplicate and parallel tests until the answer arrives", async () => {
    const gate = deferred<FakeResponse>();
    const { api, user } = await open(withSelection(), () => gate.promise);
    await user.click(testButton(CORP));
    expect(await card(CORP).findByRole("button", { name: `Test connection ${CORP}` })).toBeDisabled();
    expect(card(CORP).getByRole("button", { name: `Test connection ${CORP}` })).toHaveTextContent("Testing…");
    expect(card(CORP).getByText(/Testing corp-small…/)).toBeInTheDocument();
    expect(testButton(OLLAMA)).toBeDisabled();
    await user.click(testButton(CORP));
    await user.dblClick(testButton(OLLAMA));
    expect(tests(api)).toHaveLength(1);
    gate.resolve(json(200, testResult()));
    await card(CORP).findByText("Test passed");
    expect(testButton(CORP)).toBeEnabled();
    expect(testButton(OLLAMA)).toBeEnabled();
    expect(tests(api)).toHaveLength(1);
  });

  it("shows a failure with its sanitized code and duration, never retries by itself, and allows a manual retry", async () => {
    let n = 0;
    const { api, user } = await open(withSelection(), () => (++n === 1 ? json(200, testResult({ ok: false, code: "timeout", retryable: true, duration_ms: 20001 })) : json(200, testResult({ duration_ms: 90 }))));
    await user.click(testButton(CORP));
    expect(await card(CORP).findByText("Test failed")).toBeInTheDocument();
    expect(card(CORP).getByText("timeout")).toBeInTheDocument();
    expect(card(CORP).getByText("20,001 ms")).toBeInTheDocument();
    expect(card(CORP).getByText(/did not answer in time/)).toBeInTheDocument();
    expect(card(CORP).getByText(/Nothing is retried automatically/)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(tests(api)).toHaveLength(1);
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    expect(tests(api)).toHaveLength(2);
  });

  it("never shows provider output, request content or any field outside the contract", async () => {
    const { user } = await open(withSelection(), () =>
      json(200, { ...testResult({ ok: false, code: "invalid_structured_output" }), provider_output: SECRET_SENTINEL, message: SECRET_SENTINEL, request: SECRET_SENTINEL, endpoint: "https://leak.invalid" }),
    );
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test failed");
    expect(document.body.textContent).not.toContain(SECRET_SENTINEL);
    expect(document.body.textContent).not.toContain("leak.invalid");
  });

  it("refuses an answer about another connection and shows a fixed message", async () => {
    const { user } = await open(withSelection(), () => json(200, testResult({ connection_id: "somebody-else" })));
    await user.click(testButton(CORP));
    expect(await card(CORP).findByRole("alert")).toHaveTextContent(/does not understand/);
    expect(card(CORP).queryByText("Test passed")).not.toBeInTheDocument();
  });

  it("drops a shown result when the saved connection changes", async () => {
    const { user } = await open(withSelection({ connections: [connection(), ollama()] }));
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    await user.click(card(CORP).getByRole("button", { name: `Edit ${CORP}` }));
    const name = screen.getByLabelText("Display name");
    await user.clear(name);
    await user.type(name, "Renamed server");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("AI settings saved.");
    expect(screen.queryByText("Test passed")).not.toBeInTheDocument();
  });
});

describe("errors", () => {
  const cases: Array<[string, FakeResponse, RegExp]> = [
    ["400 unknown profile", apiError(400, "validation_error", { fields: { profile_id: ["unknown_profile"] } }), /no saved model profile with this name/],
    ["403 csrf_failed", apiError(403, "csrf_failed"), /security check failed/],
    ["404 missing connection", apiError(404, "not_found"), /not among the saved connections/],
    ["409 disabled", apiError(409, "connection_disabled"), /saved as disabled and cannot be tested/],
    ["409 not usable", apiError(409, "connection_not_usable", { issue: "endpoint_not_approved" }), /nothing was sent.*approved list/],
    ["429 throttled", apiError(429, "throttled", { retry_after_seconds: 30 }, { "Retry-After": "30" }), /Try again in 30 seconds/],
    ["429 busy", apiError(429, "connection_test_busy"), /still running on the server/],
    ["500", apiError(500, "internal_error"), /internal error/],
  ];

  it.each(cases)("%s: shows a fixed message, keeps unsaved edits and sends no other request", async (_name, response, message) => {
    const { api, user } = await open(withSelection(), () => response);
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    await user.click(testButton(CORP));
    expect(await card(CORP).findByRole("alert")).toHaveTextContent(message);
    expect(card(CORP).getByRole("alert")).not.toHaveTextContent("fixed text");
    expect(screen.getByText("You have unsaved changes.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
    expect(api.callsTo("PUT", SETTINGS_PATH)).toHaveLength(0);
    expect(testButton(CORP)).toBeEnabled();
    expect(document.body.textContent).not.toContain("Worker restart required");
  });

  it("shows a network failure, says the test may or may not have run, and does not retry", async () => {
    const { api, user } = await open(withSelection(), () => "network-error");
    await user.click(testButton(CORP));
    expect(await card(CORP).findByRole("alert")).toHaveTextContent(/may or may not have run/);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))).toHaveLength(1);
  });

  it("repeats a request only once to renew a rejected CSRF token", async () => {
    let first = true;
    const { api, user } = await open(withSelection(), () => {
      if (first) {
        first = false;
        return apiError(403, "csrf_failed");
      }
      return json(200, testResult());
    });
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))).toHaveLength(2);
  });

  it("keeps edits made while a test runs, and does not save them", async () => {
    const gate = deferred<FakeResponse>();
    const { api, user } = await open(withSelection(), () => gate.promise);
    await user.click(testButton(CORP));
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    gate.resolve(apiError(409, "connection_not_usable", { issue: "missing_api_key" }));
    expect(await card(CORP).findByRole("alert")).toHaveTextContent(/no credential for this connection/);
    expect(screen.getByText("You have unsaved changes.")).toBeInTheDocument();
    expect(screen.getByText("New connection")).toBeInTheDocument();
    expect(api.callsTo("PUT", SETTINGS_PATH)).toHaveLength(0);
  });
});

describe("security", () => {
  it("sends no key, endpoint or prompt from the page, and has no secret input in a test", async () => {
    const { api, user } = await open(withSelection());
    expect(card(CORP).queryByLabelText(/secret|key|password|token/i)).not.toBeInTheDocument();
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    const post = api.callsTo("POST", connectionTestPath("corp-llm"))[0]!;
    expect(Object.keys(post.body as object)).toEqual(["profile_id"]);
    expect(JSON.stringify(post.body)).toBe('{"profile_id":"corp-small"}');
    expect(JSON.stringify(post.headers)).not.toMatch(/authorization|subscription|api-key/i);
  });

  it("writes nothing to browser storage and logs nothing, for a pass and for a failure", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const logs = (["log", "info", "warn", "error", "debug"] as const).map((level) => vi.spyOn(console, level).mockImplementation(() => undefined));
    let n = 0;
    const { user } = await open(withSelection(), () => (++n === 1 ? json(200, testResult()) : apiError(429, "connection_test_busy")));
    await user.click(testButton(CORP));
    await card(CORP).findByText("Test passed");
    await user.click(testButton(OLLAMA));
    await card(OLLAMA).findByRole("alert");
    await waitFor(() => expect(testButton(CORP)).toBeEnabled());
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    for (const log of logs) expect(log).not.toHaveBeenCalled();
  });
});
