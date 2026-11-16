import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { fetchMe, login as apiLogin, logout as apiLogout } from "../api/auth";
import { ApiError, NOT_AUTHENTICATED, clearCsrfToken, setUnauthenticatedHandler } from "../api/http";
import type { Profile } from "../api/types";
import { describeError } from "../lib/errors";

// Authentication state of the browser tab. The truth lives on the server (Django's HttpOnly session cookie); this only
// remembers what the server last said. Nothing here is written to browser storage: a page refresh asks the server again
// (GET /api/auth/me/), which is how a session is restored.

export type AuthState =
  | { status: "loading" }
  | { status: "anonymous"; notice: string | null }
  | { status: "authenticated"; user: Profile }
  | { status: "error"; error: ApiError };

export const SESSION_EXPIRED_NOTICE = "Your session has ended. Please sign in again.";

interface AuthContextValue {
  state: AuthState;
  /** Resolves on success; rejects with the ApiError (the login page shows `describeError(error)`). */
  login(username: string, password: string): Promise<void>;
  /** Resolves with a message when the server could not be told (the user stays signed in), else with null. */
  logout(): Promise<string | null>;
  /** Ask the server again (after a failed session check). */
  retry(): void;
  /** UI hint only: the backend re-checks every permission on every call. */
  can(permission: string): boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [probe, setProbe] = useState(0);

  // Session restoration: runs on first load and whenever `retry` is called.
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchMe(controller.signal).then(
      (user) => {
        if (!controller.signal.aborted) setState({ status: "authenticated", user });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.code === NOT_AUTHENTICATED) setState({ status: "anonymous", notice: null });
        else setState({ status: "error", error: error instanceof ApiError ? error : new ApiError(0, "unexpected_response", "") });
      },
    );
    return () => controller.abort();
  }, [probe]);

  // An expired or ended session: any API call that gets 403 `not_authenticated` sends the user back to the login.
  useEffect(() => {
    setUnauthenticatedHandler(() => {
      clearCsrfToken();
      setState((current) => (current.status === "authenticated" ? { status: "anonymous", notice: SESSION_EXPIRED_NOTICE } : current));
    });
    return () => setUnauthenticatedHandler(null);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const user = await apiLogin(username, password);
    setState({ status: "authenticated", user });
  }, []);

  const logout = useCallback(async (): Promise<string | null> => {
    try {
      await apiLogout();
    } catch (error) {
      // Already signed out on the server counts as success. Anything else means the session may still be alive, so
      // the user is not shown as signed out.
      if (!(error instanceof ApiError && error.code === NOT_AUTHENTICATED)) {
        return `Could not sign out: ${describeError(error)}`;
      }
    }
    setState({ status: "anonymous", notice: null });
    return null;
  }, []);

  const retry = useCallback(() => setProbe((p) => p + 1), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      login,
      logout,
      retry,
      can: (permission) => state.status === "authenticated" && state.user.permissions.includes(permission),
    }),
    [state, login, logout, retry],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
