import { describe, expect, it } from "vitest";

import { CONNECTIONS_PREFIX, SECRET_SENTINEL, connectionTestPath, testResult } from "../test/aiSettings";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { parseConnectionTest, testAiConnection } from "./aiConnectionTest";
import { ApiError } from "./http";

function serve(status = 200, body: unknown = testResult()) {
  return installFakeApi((call) => {
    if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "token-1" });
    return json(status, body);
  });
}

describe("parseConnectionTest", () => {
  it("returns the contract's fields", () => {
    expect(parseConnectionTest(JSON.parse(JSON.stringify(testResult())))).toEqual(testResult());
    expect(parseConnectionTest(testResult({ ok: false, code: "timeout", retryable: true, duration_ms: 20000 })).code).toBe("timeout");
  });

  it("copies only listed fields, so provider output or request content is dropped", () => {
    const parsed = parseConnectionTest({ ...testResult(), output: SECRET_SENTINEL, message: SECRET_SENTINEL, endpoint: "https://x.invalid" });
    expect(JSON.stringify(parsed)).not.toContain(SECRET_SENTINEL);
    expect(JSON.stringify(parsed)).not.toContain("x.invalid");
    expect(Object.keys(parsed).sort()).toEqual(["code", "connection_id", "duration_ms", "locality", "ok", "profile_id", "provider_type", "retryable"]);
  });

  it.each([
    ["not an object", "x"],
    ["a list", []],
    ["ok not a boolean", { ...testResult(), ok: "yes" }],
    ["a negative duration", { ...testResult(), duration_ms: -1 }],
    ["a fractional duration", { ...testResult(), duration_ms: 1.5 }],
    ["a duration that is text", { ...testResult(), duration_ms: "412" }],
    ["a success that carries a code", { ...testResult(), code: "timeout" }],
    ["a failure without a code", { ...testResult(), ok: false, code: null }],
    ["a code that is not a code (free text)", { ...testResult(), ok: false, code: "The provider said: secret" }],
    ["retryable not a boolean", { ...testResult(), retryable: 1 }],
    ["no connection id", { ...testResult(), connection_id: undefined }],
  ])("refuses a malformed answer: %s", (_name, body) => {
    expect(() => parseConnectionTest(body)).toThrow(ApiError);
  });
});

describe("testAiConnection", () => {
  it("sends one POST with exactly {profile_id}, the CSRF header, the session cookie and nothing else", async () => {
    const api = serve();
    const result = await testAiConnection("corp-llm", "corp-small");
    expect(result).toEqual(testResult());
    const post = api.callsTo("POST", connectionTestPath("corp-llm"));
    expect(post).toHaveLength(1);
    expect(post[0]!.body).toEqual({ profile_id: "corp-small" });
    expect(post[0]!.headers["x-csrftoken"]).toBe("token-1");
    expect(post[0]!.credentials).toBe("same-origin");
    expect(api.calls.filter((c) => c.path.startsWith(CONNECTIONS_PREFIX))).toHaveLength(1);
  });

  it("encodes the identifier in the path", async () => {
    const api = serve(200, testResult({ connection_id: "a/b?c" }));
    await testAiConnection("a/b?c", "corp-small");
    expect(api.calls.some((c) => c.url === `${CONNECTIONS_PREFIX}a%2Fb%3Fc/test/`)).toBe(true);
  });

  it("refuses an answer about another connection or profile", async () => {
    serve(200, testResult({ connection_id: "other" }));
    await expect(testAiConnection("corp-llm", "corp-small")).rejects.toMatchObject({ code: "unexpected_response" });
    serve(200, testResult({ profile_id: "other" }));
    await expect(testAiConnection("corp-llm", "corp-small")).rejects.toMatchObject({ code: "unexpected_response" });
  });

  it("throws the server's error code and sends nothing again", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(409, "connection_disabled")));
    await expect(testAiConnection("corp-llm", "corp-small")).rejects.toMatchObject({ status: 409, code: "connection_disabled" });
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))).toHaveLength(1);
  });

  it("repeats a request only to refresh a rejected CSRF token, once, and not otherwise", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(403, "csrf_failed")));
    await expect(testAiConnection("corp-llm", "corp-small")).rejects.toMatchObject({ code: "csrf_failed" });
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))).toHaveLength(2);
  });

  it("turns a network failure into a network_error and does not retry", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : "network-error"));
    await expect(testAiConnection("corp-llm", "corp-small")).rejects.toMatchObject({ code: "network_error" });
    expect(api.callsTo("POST", connectionTestPath("corp-llm"))).toHaveLength(1);
  });
});
