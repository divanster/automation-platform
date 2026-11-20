import { describe, expect, it } from "vitest";

import { SECRET_SENTINEL, SETTINGS_PATH, settings, withSelection } from "../test/aiSettings";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { getAiSettings, parseAiSettings, saveAiSettings } from "./aiSettings";
import { ApiError } from "./http";

describe("parseAiSettings", () => {
  it("returns the contract's fields", () => {
    const data = withSelection();
    expect(parseAiSettings(JSON.parse(JSON.stringify(data)))).toEqual(data);
  });

  it("copies only listed fields, so an unlisted one (a rogue secret) is dropped", () => {
    const data = withSelection();
    const rogue = { ...data, api_key: SECRET_SENTINEL, catalog: { ...data.catalog, token: SECRET_SENTINEL }, connections: data.connections.map((c) => ({ ...c, password: SECRET_SENTINEL })) };
    expect(JSON.stringify(parseAiSettings(rogue))).not.toContain(SECRET_SENTINEL);
  });

  it.each([
    ["not an object", "x"],
    ["no settings", { ...settings(), settings: undefined }],
    ["connections not a list", { ...settings(), connections: {} }],
    ["revision not an integer", { ...settings(), revision: "3" }],
    ["a connection without a status", { ...settings(), connections: [{ connection_id: "x" }] }],
    ["no catalog", { ...settings(), catalog: null }],
    ["a timeout of the wrong type", { ...settings(), catalog: { ...settings().catalog, timeout: { min: "1", max: 2, default: 3 } } }],
  ])("refuses a malformed answer: %s", (_name, body) => {
    expect(() => parseAiSettings(body)).toThrow(ApiError);
  });
});

describe("the requests", () => {
  it("reads with a GET and saves with a PUT that carries the CSRF token", async () => {
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "token-1" });
      if (call.method === "GET") return json(200, settings());
      return json(200, settings({ revision: 4 }));
    });
    expect((await getAiSettings()).revision).toBe(3);
    const saved = await saveAiSettings({ extraction_mode: "deterministic", provider_id: null, connection_id: null, profile_id: null, policy_mode: null, timeout_seconds: null, expected_revision: 3 });
    expect(saved.revision).toBe(4);
    const put = api.callsTo("PUT", SETTINGS_PATH)[0]!;
    expect(put.headers["x-csrftoken"]).toBe("token-1");
    expect(api.callsTo("GET", SETTINGS_PATH)[0]!.headers["x-csrftoken"]).toBeUndefined();
  });

  it("turns the contract's error body into an ApiError with its code and details", async () => {
    installFakeApi(() => apiError(400, "validation_error", { fields: { timeout_seconds: ["out_of_range"] } }));
    await expect(getAiSettings()).rejects.toMatchObject({ status: 400, code: "validation_error", details: { fields: { timeout_seconds: ["out_of_range"] } } });
  });
});
