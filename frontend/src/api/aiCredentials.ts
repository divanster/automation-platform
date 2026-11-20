// The managed AI credential endpoints (backend/apps/api/views_ai_credentials.py):
//   POST   /api/ai-credentials/              {reference, value}  create (refuses an existing reference)
//   PUT    /api/ai-credentials/<reference>/  {value}             replace (refuses a missing reference)
//   DELETE /api/ai-credentials/<reference>/                      delete (refused while a stored connection reads it)
//   GET    /api/ai-credentials/<reference>/                      status: set, not_set or unreadable
// Administrator only; the server decides and answers 403 `permission_denied` to anyone else.
//
// SECRETS. The value is a parameter of `createAiCredential` and `replaceAiCredential` ONLY: it goes into the JSON body of
// that one request (same origin, session cookie, X-CSRFToken) and nowhere else. It is not stored in this module, not
// returned, not logged, and the answer is copied field by field into a new object that has no field a value could occupy,
// so nothing the server (or a proxy) adds can reach the screen. The server's errors are codes, never a value.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import type { AiCredentialStatus, CredentialStatusValue } from "./types";

const COLLECTION = "/api/ai-credentials/";

const detail = (reference: string) => `${COLLECTION}${encodeURIComponent(reference)}/`;

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

function str(value: unknown): string {
  if (typeof value !== "string") throw unexpected();
  return value;
}

function statusValue(value: unknown): CredentialStatusValue {
  if (value !== "set" && value !== "not_set" && value !== "unreadable") throw unexpected();
  return value;
}

/** Exported for tests. Throws `unexpected_response` when the body is not the contract's shape. */
export function parseCredentialStatus(body: unknown): AiCredentialStatus {
  if (typeof body !== "object" || body === null || Array.isArray(body)) throw unexpected();
  const v = body as Record<string, unknown>;
  if (v.source !== "managed" || !Array.isArray(v.connections)) throw unexpected();
  return {
    reference: str(v.reference),
    source: "managed",
    status: statusValue(v.status),
    store: str(v.store),
    connections: v.connections.map(str),
    apply: str(v.apply),
  };
}

export async function getAiCredentialStatus(reference: string, signal?: AbortSignal): Promise<AiCredentialStatus> {
  return parseCredentialStatus(await request<unknown>(detail(reference), { signal }));
}

export async function createAiCredential(reference: string, value: string, signal?: AbortSignal): Promise<AiCredentialStatus> {
  return parseCredentialStatus(await request<unknown>(COLLECTION, { method: "POST", body: { reference, value }, signal }));
}

export async function replaceAiCredential(reference: string, value: string, signal?: AbortSignal): Promise<AiCredentialStatus> {
  return parseCredentialStatus(await request<unknown>(detail(reference), { method: "PUT", body: { value }, signal }));
}

export async function deleteAiCredential(reference: string, signal?: AbortSignal): Promise<AiCredentialStatus> {
  return parseCredentialStatus(await request<unknown>(detail(reference), { method: "DELETE", signal }));
}
