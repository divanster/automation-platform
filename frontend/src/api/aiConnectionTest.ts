// The AI connection test endpoint (backend/apps/api/views_ai_connection_test.py):
//   POST /api/ai-settings/connections/<connection_id>/test/   {profile_id}
// Administrator only; the server decides, and answers 403 `permission_denied` to anyone else.
//
// WHAT IS SENT. The body is exactly `{profile_id}`: the connection is named in the path and the server tests it AS SAVED. There is no field for a key, endpoint, prompt or limit,
// and this module never adds one. Same origin, session cookie, X-CSRFToken (see http.ts).
//
// WHAT COMES BACK. A fixed outcome. The answer is copied field by field into a new object (`parseConnectionTest`): a field the contract does not list is dropped here and can
// never reach the screen, whatever a server (or a proxy in between) sent. This module does not retry, log or store anything.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import type { AiConnectionTestResult } from "./types";

const testPath = (connectionId: string) => `/api/ai-settings/connections/${encodeURIComponent(connectionId)}/test/`;

// A stable error code is lower-case words joined by underscores. Anything else is not a code and is never shown.
const CODE_SHAPE = /^[a-z][a-z_]{0,39}$/;

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

function text(value: unknown): string {
  if (typeof value !== "string") throw unexpected();
  return value;
}

/** Exported for tests. Throws `unexpected_response` when the body is not the contract's shape. */
export function parseConnectionTest(body: unknown): AiConnectionTestResult {
  if (typeof body !== "object" || body === null || Array.isArray(body)) throw unexpected();
  const v = body as Record<string, unknown>;
  if (typeof v.ok !== "boolean" || typeof v.retryable !== "boolean") throw unexpected();
  if (typeof v.duration_ms !== "number" || !Number.isInteger(v.duration_ms) || v.duration_ms < 0) throw unexpected();
  const code = v.code === null ? null : text(v.code);
  if (v.ok ? code !== null : code === null || !CODE_SHAPE.test(code)) throw unexpected();
  return {
    connection_id: text(v.connection_id),
    profile_id: text(v.profile_id),
    provider_type: text(v.provider_type),
    locality: text(v.locality),
    ok: v.ok,
    code,
    retryable: v.retryable,
    duration_ms: v.duration_ms,
  };
}

/**
 * Test one SAVED connection with one of its SAVED profiles. One request, never repeated by this function. An answer about another
 * connection or profile than the one asked about is refused, so a result can never be shown beside the wrong connection.
 */
export async function testAiConnection(connectionId: string, profileId: string, signal?: AbortSignal): Promise<AiConnectionTestResult> {
  const result = parseConnectionTest(await request<unknown>(testPath(connectionId), { method: "POST", body: { profile_id: profileId }, signal }));
  if (result.connection_id !== connectionId || result.profile_id !== profileId) throw unexpected();
  return result;
}
