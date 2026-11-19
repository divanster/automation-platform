import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json, type Handler } from "../test/fakeApi";
import { RUN_ID, detail, listItem, operator, page } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { signOut } from "../test/shell";
import { signedIn, signedOut } from "../test/server";

const listOk: Handler = (call) => {
  if (call.method === "GET" && call.path === "/api/requests/") return json(200, page([listItem()]));
  if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) return json(200, detail());
  return apiError(404, "not_found");
};

async function signIn(user: ReturnType<typeof userEvent.setup>, password = "synthetic-password") {
  await user.type(await screen.findByLabelText("Username"), "synthetic.operator");
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("session restoration", () => {
  it("restores a signed-in user after a refresh by asking the server, not browser storage", async () => {
    const api = installFakeApi(signedIn(listOk));
    renderApp("/requests");
    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
    expect(screen.getByText("synthetic.operator")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/auth/me/")).toHaveLength(1);
    expect(api.callsTo("POST", "/api/auth/login/")).toHaveLength(0);
  });

  it("shows the sign-in form when nobody is signed in", async () => {
    installFakeApi(signedOut());
    renderApp("/requests");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("explains a failed session check and lets the user retry", async () => {
    let up = false;
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return up ? json(200, operator()) : "network-error";
      return listOk(call);
    });
    const user = userEvent.setup();
    renderApp("/requests");
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be reached/i);

    up = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
  });
});

describe("protected routes", () => {
  it.each(["/requests", "/requests/new", `/requests/${RUN_ID}`, "/anything-else"])("redirects %s to the login without calling the data API", async (path) => {
    const api = installFakeApi(signedOut());
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(api.calls.filter((c) => c.path.startsWith("/api/requests"))).toHaveLength(0);
  });

  it("sends the user back to the page they asked for after signing in", async () => {
    let authed = false;
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return authed ? json(200, operator()) : apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "t0" });
      if (call.path === "/api/auth/login/") {
        authed = true;
        return json(200, { ...operator(), csrf_token: "t1" });
      }
      return listOk(call);
    });
    const user = userEvent.setup();
    renderApp(`/requests/${RUN_ID}`);
    await signIn(user);
    expect(await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ })).toBeInTheDocument();
  });

  it("redirects a signed-in user away from the login page", async () => {
    installFakeApi(signedIn(listOk));
    renderApp("/login");
    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
  });
});

describe("login", () => {
  it("posts the credentials with the CSRF token and shows the requests", async () => {
    let authed = false;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return authed ? json(200, operator()) : apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "before" });
      if (call.path === "/api/auth/login/") {
        authed = true;
        return json(200, { ...operator(), csrf_token: "after" });
      }
      return listOk(call);
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user);

    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
    const loginCall = api.callsTo("POST", "/api/auth/login/")[0];
    expect(loginCall?.headers["x-csrftoken"]).toBe("before");
    expect(loginCall?.body).toEqual({ username: "synthetic.operator", password: "synthetic-password" });
  });

  it("uses the refreshed CSRF token for the next unsafe request after login", async () => {
    let authed = false;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return authed ? json(200, operator()) : apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "before" });
      if (call.path === "/api/auth/login/") {
        authed = true;
        return json(200, { ...operator(), csrf_token: "after" });
      }
      if (call.path === "/api/auth/logout/") return { status: 204 };
      return listOk(call);
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user);
    await signOut(user);
    expect(api.callsTo("POST", "/api/auth/logout/")[0]?.headers["x-csrftoken"]).toBe("after");
  });

  it("shows a fixed message for wrong credentials, clears the password, and does not echo input", async () => {
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "t" });
      return apiError(403, "invalid_credentials");
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user, "hunter2-wrong");

    expect(await screen.findByRole("alert")).toHaveTextContent("The username or password is incorrect.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(document.body).not.toHaveTextContent("hunter2-wrong");
  });

  it("tells the user how long to wait when throttled", async () => {
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "t" });
      return apiError(429, "throttled", { retry_after_seconds: 42 }, { "Retry-After": "42" });
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 42 seconds.");
  });

  it("recovers from a stale CSRF token on login by refreshing it once", async () => {
    let authed = false;
    let tokens = 0;
    const api = installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return authed ? json(200, operator()) : apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: `t${(tokens += 1)}` });
      if (call.path === "/api/auth/login/") {
        if (call.headers["x-csrftoken"] === "t1") return apiError(403, "csrf_failed");
        authed = true;
        return json(200, { ...operator(), csrf_token: "fresh" });
      }
      return listOk(call);
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user);
    expect(await screen.findByText("REQ-20260101-AAAA0001")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/auth/login/").map((c) => c.headers["x-csrftoken"])).toEqual(["t1", "t2"]);
  });

  it("disables the form while the request is in flight", async () => {
    installFakeApi((call) => {
      if (call.path === "/api/auth/me/") return apiError(403, "not_authenticated");
      if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "t" });
      return new Promise<never>(() => {}); // never answers
    });
    const user = userEvent.setup();
    renderApp("/requests");
    await signIn(user);
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDisabled();
  });

  it("uses autocomplete attributes so password managers work, and a password input", async () => {
    installFakeApi(signedOut());
    renderApp("/login");
    expect(await screen.findByLabelText("Username")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
  });
});

describe("logout", () => {
  it("ends the session on the server and returns to the login", async () => {
    const api = installFakeApi(signedIn((call) => (call.path === "/api/auth/logout/" ? { status: 204 } : listOk(call))));
    const user = userEvent.setup();
    renderApp("/requests");
    await signOut(user);

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/auth/logout/")).toHaveLength(1);
    expect(api.callsTo("POST", "/api/auth/logout/")[0]?.headers["x-csrftoken"]).toBe("csrf-test-token");
  });

  it("keeps the user signed in and says so when the server could not be told", async () => {
    installFakeApi(signedIn((call) => (call.path === "/api/auth/logout/" ? "network-error" : listOk(call))));
    const user = userEvent.setup();
    renderApp("/requests");
    await signOut(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not sign out/i);
    expect(screen.getByText("synthetic.operator")).toBeInTheDocument();
  });

  it("treats an already-ended session as signed out", async () => {
    installFakeApi(signedIn((call) => (call.path === "/api/auth/logout/" ? apiError(403, "not_authenticated") : listOk(call))));
    const user = userEvent.setup();
    renderApp("/requests");
    await signOut(user);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("expired sessions", () => {
  it("returns to the login with a notice when an API call finds the session gone", async () => {
    let expired = false;
    installFakeApi(
      signedIn((call) => {
        if (call.path === "/api/requests/") return expired ? apiError(403, "not_authenticated") : json(200, page([listItem()]));
        return apiError(404, "not_found");
      }),
    );
    const user = userEvent.setup();
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");

    expired = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText(/your session has ended/i)).toBeInTheDocument();
  });
});

describe("permissions are hints only", () => {
  it("hides the New request link for users without the submit permission, and the backend stays authoritative", async () => {
    installFakeApi(signedIn(listOk, operator({ permissions: ["intake.view_any_request"], roles: ["Approver"] })));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    await waitFor(() => expect(screen.queryByRole("link", { name: "New request" })).not.toBeInTheDocument());
    expect(screen.getByText(/Approver/)).toBeInTheDocument();
  });
});
