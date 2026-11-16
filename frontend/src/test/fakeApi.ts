// TEST ONLY. A stand-in for `fetch` so the client and the screens can be tested without a server. It records every call.
// The running application never imports this file; there is no mock backend in the product.

import { vi } from "vitest";

export interface RecordedCall {
  method: string;
  url: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
  credentials: RequestCredentials | undefined;
}

export interface FakeResponse {
  status: number;
  body?: unknown;
  rawBody?: string;
  headers?: Record<string, string>;
}

export type Handler = (call: RecordedCall) => FakeResponse | Promise<FakeResponse> | "network-error";

export function json(status: number, body?: unknown, headers?: Record<string, string>): FakeResponse {
  return { status, body, headers };
}

export function apiError(status: number, code: string, details?: Record<string, unknown>, headers?: Record<string, string>): FakeResponse {
  return json(status, { error: { code, message: "fixed text", ...(details ? { details } : {}) } }, headers);
}

export function installFakeApi(handler: Handler) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const parsed = new URL(url, "http://localhost");
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) headers[key.toLowerCase()] = value;
    const call: RecordedCall = {
      method: (init?.method ?? "GET").toUpperCase(),
      url,
      path: parsed.pathname,
      query: parsed.searchParams,
      headers,
      body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined,
      credentials: init?.credentials,
    };
    calls.push(call);
    const result = await handler(call);
    if (result === "network-error") throw new TypeError("Failed to fetch");
    const text = result.rawBody ?? (result.body === undefined ? "" : JSON.stringify(result.body));
    return new Response(result.status === 204 ? null : text, { status: result.status, headers: result.headers });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    fetchMock,
    callsTo: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path),
  };
}
