// TEST ONLY: composes a handler for a signed-in user on top of the fake fetch.

import type { Profile } from "../api/types";
import { apiError, json, type Handler } from "./fakeApi";
import { operator } from "./fixtures";

/** `GET /api/auth/me/` answers with `profile`; everything else goes to `rest`. */
export function signedIn(rest: Handler, profile: Profile = operator()): Handler {
  return (call) => {
    if (call.path === "/api/auth/me/") return json(200, profile);
    if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "csrf-test-token" });
    return rest(call);
  };
}

/** `GET /api/auth/me/` answers "not signed in". */
export function signedOut(rest: Handler = () => apiError(403, "not_authenticated")): Handler {
  return (call) => {
    if (call.path === "/api/auth/me/") return apiError(403, "not_authenticated");
    if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "csrf-test-token" });
    return rest(call);
  };
}

/** A promise that is settled by the test, to control the order in which responses arrive. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
