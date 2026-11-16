// Session endpoints (backend/apps/api/views_auth.py). The session itself is Django's HttpOnly cookie; the only thing
// this module keeps is the CSRF token, inside http.ts.

import { ApiError, UNEXPECTED_RESPONSE, clearCsrfToken, request, setCsrfToken } from "./http";
import type { LoginResponse, Profile } from "./types";

function isProfile(value: unknown): value is Profile {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.public_id === "string" &&
    typeof v.username === "string" &&
    typeof v.is_superuser === "boolean" &&
    Array.isArray(v.roles) &&
    Array.isArray(v.permissions)
  );
}

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/** Who is signed in? A 403 `not_authenticated` is the normal "nobody" answer, so it does not raise the session alarm. */
export async function fetchMe(signal?: AbortSignal): Promise<Profile> {
  const body = await request<unknown>("/api/auth/me/", { signal, notifyUnauthenticated: false });
  if (!isProfile(body)) throw unexpected();
  return body;
}

/** Sign in. The CSRF token is rotated by the server on login, so the one in the response replaces the old one. */
export async function login(username: string, password: string): Promise<Profile> {
  const body = await request<unknown>("/api/auth/login/", {
    method: "POST",
    body: { username, password },
    notifyUnauthenticated: false,
  });
  const response = body as Partial<LoginResponse> | undefined;
  if (!isProfile(body) || typeof response?.csrf_token !== "string" || response.csrf_token === "") throw unexpected();
  setCsrfToken(response.csrf_token);
  const { csrf_token: _token, ...profile } = body as LoginResponse;
  return profile;
}

/** End the session on the server. The in-memory CSRF token is dropped whatever the outcome, so the next sign-in starts clean. */
export async function logout(): Promise<void> {
  try {
    await request<void>("/api/auth/logout/", { method: "POST", notifyUnauthenticated: false });
  } finally {
    clearCsrfToken();
  }
}
