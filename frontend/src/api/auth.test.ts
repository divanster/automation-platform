import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json } from "../test/fakeApi";
import { operator } from "../test/fixtures";
import { fetchMe, login, logout } from "./auth";
import { request } from "./http";

describe("login()", () => {
  it("bootstraps CSRF, posts the credentials with the token, and returns the profile without the token", async () => {
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "before-login" });
      return json(200, { ...operator(), csrf_token: "after-login" });
    });
    const profile = await login("synthetic.operator", "synthetic-password");

    expect(api.calls.map((c) => `${c.method} ${c.path}`)).toEqual(["GET /api/auth/csrf/", "POST /api/auth/login/"]);
    expect(api.calls[1]?.headers["x-csrftoken"]).toBe("before-login");
    expect(api.calls[1]?.body).toEqual({ username: "synthetic.operator", password: "synthetic-password" });
    expect(profile).toEqual(operator());
    expect(profile).not.toHaveProperty("csrf_token");
  });

  it("replaces the CSRF token with the fresh one from the response and uses it afterwards", async () => {
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "before-login" });
      if (call.path === "/api/auth/login/") return json(200, { ...operator(), csrf_token: "after-login" });
      return json(200, {});
    });
    await login("u", "p");
    await request("/api/requests/", { method: "POST", body: { text: "t" } });

    expect(api.callsTo("GET", "/api/auth/csrf/")).toHaveLength(1); // no second bootstrap
    expect(api.callsTo("POST", "/api/requests/")[0]?.headers["x-csrftoken"]).toBe("after-login");
  });

  it("surfaces invalid_credentials without treating it as an expired session", async () => {
    installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(403, "invalid_credentials")));
    await expect(login("u", "wrong")).rejects.toMatchObject({ code: "invalid_credentials", status: 403 });
  });

  it("rejects a success response that has no profile or no token", async () => {
    installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : json(200, { username: "only" })));
    await expect(login("u", "p")).rejects.toMatchObject({ code: "unexpected_response" });
  });
});

describe("fetchMe()", () => {
  it("returns the profile", async () => {
    installFakeApi(() => json(200, operator()));
    await expect(fetchMe()).resolves.toEqual(operator());
  });

  it("raises not_authenticated when nobody is signed in", async () => {
    installFakeApi(() => apiError(403, "not_authenticated"));
    await expect(fetchMe()).rejects.toMatchObject({ code: "not_authenticated" });
  });
});

describe("logout()", () => {
  it("posts with the CSRF token and then forgets it, so the next unsafe request bootstraps again", async () => {
    let tokens = 0;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: `token-${(tokens += 1)}` });
      if (call.path === "/api/auth/logout/") return { status: 204 };
      return json(200, {});
    });
    await request("/api/x/", { method: "POST", body: {} }); // token-1
    await logout();
    await request("/api/x/", { method: "POST", body: {} }); // must fetch token-2

    expect(api.callsTo("POST", "/api/auth/logout/")[0]?.headers["x-csrftoken"]).toBe("token-1");
    expect(api.callsTo("POST", "/api/x/")[1]?.headers["x-csrftoken"]).toBe("token-2");
  });

  it("forgets the token even when the server could not be reached", async () => {
    let tokens = 0;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: `token-${(tokens += 1)}` });
      if (call.path === "/api/auth/logout/") return "network-error";
      return json(200, {});
    });
    await expect(logout()).rejects.toMatchObject({ code: "network_error" });
    await request("/api/x/", { method: "POST", body: {} });
    expect(api.callsTo("GET", "/api/auth/csrf/")).toHaveLength(2);
  });
});
