import { describe, expect, it, vi } from "vitest";

import { apiError, installFakeApi, json } from "../test/fakeApi";
import { HASH, RUN_ID, detail } from "../test/fixtures";
import { ApiError, NETWORK_ERROR, UNEXPECTED_RESPONSE, request, setCsrfToken, setUnauthenticatedHandler } from "./http";
import { getRequest, listRequests } from "./requests";

describe("request(): transport rules", () => {
  it("sends a GET with same-origin credentials, no CSRF header and no body", async () => {
    const api = installFakeApi(() => json(200, { ok: true }));
    await request("/api/ping/");
    const [call] = api.calls;
    expect(call?.method).toBe("GET");
    expect(call?.credentials).toBe("same-origin");
    expect(call?.headers["x-csrftoken"]).toBeUndefined();
    expect(call?.headers.accept).toBe("application/json");
    expect(call?.body).toBeUndefined();
  });

  it("refuses any path outside /api/ so a caller cannot reach another origin", async () => {
    const api = installFakeApi(() => json(200, {}));
    await expect(request("https://elsewhere.example/api/x/")).rejects.toThrow();
    await expect(request("/other/")).rejects.toThrow();
    expect(api.calls).toHaveLength(0);
  });

  it("builds the query string and leaves out undefined values", async () => {
    const api = installFakeApi(() => json(200, {}));
    await request("/api/ping/", { query: { page: 2, state: undefined, q: "a b" } });
    expect(api.calls[0]?.url).toBe("/api/ping/?page=2&q=a+b");
  });

  it("returns undefined for 204", async () => {
    installFakeApi(() => ({ status: 204 }));
    setCsrfToken("t");
    await expect(request("/api/x/", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("request(): CSRF handling", () => {
  it("fetches the token once before the first unsafe request and sends it as X-CSRFToken", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "tok-1" }) : json(200, { done: true })));
    await request("/api/thing/", { method: "POST", body: { a: 1 } });
    await request("/api/thing/", { method: "POST", body: { a: 2 } });

    expect(api.calls.map((c) => `${c.method} ${c.path}`)).toEqual(["GET /api/auth/csrf/", "POST /api/thing/", "POST /api/thing/"]);
    expect(api.calls[1]?.headers["x-csrftoken"]).toBe("tok-1");
    expect(api.calls[2]?.headers["x-csrftoken"]).toBe("tok-1");
    expect(api.calls[1]?.headers["content-type"]).toBe("application/json");
    expect(api.calls[1]?.body).toEqual({ a: 1 });
  });

  it("does not fetch the token for safe requests", async () => {
    const api = installFakeApi(() => json(200, {}));
    await request("/api/thing/");
    expect(api.callsTo("GET", "/api/auth/csrf/")).toHaveLength(0);
  });

  it("on 403 csrf_failed fetches a fresh token and retries the request exactly once", async () => {
    let posts = 0;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "fresh" });
      posts += 1;
      return call.headers["x-csrftoken"] === "stale" ? apiError(403, "csrf_failed") : json(200, { done: true });
    });
    setCsrfToken("stale");
    await expect(request("/api/thing/", { method: "POST", body: {} })).resolves.toEqual({ done: true });
    expect(posts).toBe(2);
    expect(api.callsTo("POST", "/api/thing/").map((c) => c.headers["x-csrftoken"])).toEqual(["stale", "fresh"]);
  });

  it("gives up after one retry when the CSRF check keeps failing", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(403, "csrf_failed")));
    await expect(request("/api/thing/", { method: "POST", body: {} })).rejects.toMatchObject({ code: "csrf_failed", status: 403 });
    expect(api.callsTo("POST", "/api/thing/")).toHaveLength(2);
  });

  it("does not retry other 403 answers", async () => {
    const api = installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(403, "permission_denied")));
    await expect(request("/api/thing/", { method: "POST", body: {} })).rejects.toMatchObject({ code: "permission_denied" });
    expect(api.callsTo("POST", "/api/thing/")).toHaveLength(1);
  });

  it("fails clearly when the CSRF bootstrap itself fails", async () => {
    installFakeApi((call) => (call.path === "/api/auth/csrf/" ? { status: 200, body: { nope: 1 } } : json(200, {})));
    await expect(request("/api/thing/", { method: "POST", body: {} })).rejects.toMatchObject({ code: UNEXPECTED_RESPONSE });
  });
});

describe("request(): error contract", () => {
  it("turns the contract body into an ApiError with code, status and details", async () => {
    installFakeApi(() => apiError(400, "validation_error", { fields: { "draft.items[0].quantity": ["invalid"] } }));
    const error = (await request("/api/x/").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.code).toBe("validation_error");
    expect(error.details).toEqual({ fields: { "draft.items[0].quantity": ["invalid"] } });
  });

  it("reads Retry-After from the header for 429 throttled", async () => {
    installFakeApi(() => apiError(429, "throttled", { retry_after_seconds: 7 }, { "Retry-After": "12" }));
    await expect(request("/api/x/")).rejects.toMatchObject({ code: "throttled", status: 429, retryAfterSeconds: 12 });
  });

  it("falls back to details.retry_after_seconds when the header is missing", async () => {
    installFakeApi(() => apiError(429, "throttled", { retry_after_seconds: 7 }));
    await expect(request("/api/x/")).rejects.toMatchObject({ retryAfterSeconds: 7 });
  });

  it("maps a non-JSON error page (for example a proxy 502) to unexpected_response", async () => {
    installFakeApi(() => ({ status: 502, rawBody: "<html>Bad gateway</html>" }));
    await expect(request("/api/x/")).rejects.toMatchObject({ code: UNEXPECTED_RESPONSE, status: 502 });
  });

  it("maps a JSON body without an error object to unexpected_response", async () => {
    installFakeApi(() => json(500, { detail: "boom" }));
    await expect(request("/api/x/")).rejects.toMatchObject({ code: UNEXPECTED_RESPONSE, status: 500 });
  });

  it("maps a failed fetch to network_error", async () => {
    installFakeApi(() => "network-error");
    await expect(request("/api/x/")).rejects.toMatchObject({ code: NETWORK_ERROR, status: 0 });
  });

  it("re-throws an abort as an AbortError, not as an API error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("aborted", "AbortError");
      }),
    );
    await expect(request("/api/x/")).rejects.toMatchObject({ name: "AbortError" });
  });

  it("never puts the response message into the error unless the contract supplied it", async () => {
    installFakeApi(() => ({ status: 500, rawBody: "SENSITIVE-DETAIL" }));
    const error = (await request("/api/x/").catch((e: unknown) => e)) as ApiError;
    expect(error.message).not.toContain("SENSITIVE-DETAIL");
  });
});

describe("request(): expired sessions", () => {
  it("notifies the session handler on 403 not_authenticated", async () => {
    installFakeApi(() => apiError(403, "not_authenticated"));
    const handler = vi.fn();
    setUnauthenticatedHandler(handler);
    await expect(request("/api/requests/")).rejects.toMatchObject({ code: "not_authenticated" });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("stays silent when the caller expects that answer (session probe, login)", async () => {
    installFakeApi(() => apiError(403, "not_authenticated"));
    const handler = vi.fn();
    setUnauthenticatedHandler(handler);
    await expect(request("/api/auth/me/", { notifyUnauthenticated: false })).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it("does not treat other 403 answers as an expired session", async () => {
    installFakeApi(() => apiError(403, "permission_denied"));
    const handler = vi.fn();
    setUnauthenticatedHandler(handler);
    await expect(request("/api/requests/")).rejects.toMatchObject({ code: "permission_denied" });
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("decimal quantities stay text", () => {
  it("keeps every digit and the scale of a decimal quantity exactly as sent", async () => {
    const quantities = ["2.50", "0.1000000000000000000001", "12345678901234567890.123456789", "1E+3", "007"];
    for (const quantity of quantities) {
      const draft = detail().draft!;
      installFakeApi(() =>
        json(200, detail({ draft: { ...draft, items: [{ description: "x", quantity, unit: null, notes: null }] } })),
      );
      const result = await getRequest(RUN_ID);
      expect(result.draft?.items[0]?.quantity).toBe(quantity);
      expect(typeof result.draft?.items[0]?.quantity).toBe("string");
    }
  });

  it("refuses a quantity that arrives as a JSON number instead of showing a rounded value", async () => {
    installFakeApi(() => ({
      status: 200,
      rawBody: JSON.stringify(detail()).replace('"quantity":"2.50"', '"quantity":2.5'),
    }));
    await expect(getRequest(RUN_ID)).rejects.toMatchObject({ code: UNEXPECTED_RESPONSE });
  });

  it("accepts a null quantity", async () => {
    const draft = detail().draft!;
    installFakeApi(() => json(200, detail({ draft: { ...draft, items: [{ description: "x", quantity: null, unit: null, notes: null }] } })));
    await expect(getRequest(RUN_ID)).resolves.toMatchObject({ run_id: RUN_ID });
  });
});

describe("request endpoints use the contract's paths and parameters", () => {
  it("lists with page, page_size and the optional state", async () => {
    const api = installFakeApi(() => json(200, { count: 0, page: 1, page_size: 20, results: [] }));
    await listRequests({ page: 3, page_size: 20, state: "failed" });
    expect(api.calls[0]?.path).toBe("/api/requests/");
    expect(Object.fromEntries(api.calls[0]!.query)).toEqual({ page: "3", page_size: "20", state: "failed" });
  });

  it("encodes the run id and rejects a malformed detail body", async () => {
    const api = installFakeApi(() => json(200, { unexpected: true }));
    await expect(getRequest(RUN_ID)).rejects.toMatchObject({ code: UNEXPECTED_RESPONSE });
    expect(api.calls[0]?.path).toBe(`/api/requests/${RUN_ID}/`);
  });

  it("exposes the content hash and revision exactly as sent", async () => {
    installFakeApi(() => json(200, detail()));
    const result = await getRequest(RUN_ID);
    expect(result.current_revision?.content_hash).toBe(HASH);
  });
});
