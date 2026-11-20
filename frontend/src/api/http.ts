// The one place that talks to the network.
//
// SAME ORIGIN. Every URL is relative ("/api/..."): in development the Vite proxy forwards it to Django, so the
// browser never makes a cross-origin request and no CORS is needed.
//
// CSRF. Django's CSRF cookie is HttpOnly, so JavaScript cannot read it; the token is delivered in the body of
// GET /api/auth/csrf/ and held ONLY in this module's memory (never in browser storage). It is sent as X-CSRFToken on
// every unsafe request. A 403 `csrf_failed` refreshes the token and retries the request exactly once.
//
// ERRORS. Every failure is an ApiError built from the contract's `{"error": {code, message, details?}}` body, or from a
// synthetic code (`network_error`, `unexpected_response`) when there is no such body. The UI decides from `code`, never
// from the message text. Nothing in this module logs.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;
  readonly retryAfterSeconds: number | undefined;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: Record<string, unknown>,
    retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export const NETWORK_ERROR = "network_error";
export const UNEXPECTED_RESPONSE = "unexpected_response";
export const NOT_AUTHENTICATED = "not_authenticated";
export const CSRF_FAILED = "csrf_failed";

const CSRF_URL = "/api/auth/csrf/";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

let csrfToken: string | null = null;
let unauthenticatedHandler: (() => void) | null = null;

/** Remember the token the server just issued (the login response carries a fresh one). */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** Forget the token (logout, session end). The next unsafe request fetches a new one. */
export function clearCsrfToken(): void {
  csrfToken = null;
}

/** Called when a request that needed a session gets 403 `not_authenticated` (an expired or ended session). */
export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  unauthenticatedHandler = handler;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT";
  query?: Record<string, string | number | undefined>;
  body?: unknown;
  signal?: AbortSignal;
  /** False for calls where "not signed in" is an expected answer (the session probe, the login itself). */
  notifyUnauthenticated?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!path.startsWith("/api/")) {
    throw new Error("API paths must start with /api/");
  }
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function errorFrom(response: Response, body: unknown): ApiError {
  if (isRecord(body) && isRecord(body.error) && typeof body.error.code === "string") {
    const { code, message, details } = body.error;
    const retryHeader = Number(response.headers.get("Retry-After"));
    const retryDetail = isRecord(details) ? details.retry_after_seconds : undefined;
    const retry =
      Number.isFinite(retryHeader) && retryHeader > 0 ? retryHeader : typeof retryDetail === "number" ? retryDetail : undefined;
    return new ApiError(
      response.status,
      code,
      typeof message === "string" ? message : "",
      isRecord(details) ? details : undefined,
      retry,
    );
  }
  return new ApiError(response.status, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/** One network round trip with the right headers. Throws ApiError for anything but a 2xx response. */
async function send(method: string, url: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (!SAFE_METHODS.has(method)) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRFToken"] = csrfToken ?? "";
  }
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, NETWORK_ERROR, "The server could not be reached.");
  }
  const parsed = await readBody(response);
  if (!response.ok) throw errorFrom(response, parsed);
  return parsed;
}

async function fetchCsrfToken(signal?: AbortSignal): Promise<void> {
  const body = await send("GET", CSRF_URL, undefined, signal);
  if (isRecord(body) && typeof body.csrf_token === "string" && body.csrf_token !== "") {
    csrfToken = body.csrf_token;
    return;
  }
  throw new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

/** Perform an API call and return the parsed JSON body (undefined for 204). */
export async function request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const url = buildUrl(path, options.query);
  const unsafe = !SAFE_METHODS.has(method);
  const notify = options.notifyUnauthenticated ?? true;

  const attempt = async (): Promise<unknown> => {
    if (unsafe && csrfToken === null) await fetchCsrfToken(options.signal);
    return send(method, url, options.body, options.signal);
  };

  try {
    try {
      return (await attempt()) as T;
    } catch (error) {
      if (unsafe && error instanceof ApiError && error.code === CSRF_FAILED) {
        // The token was stale (rotated by a login in another tab, or the server restarted): fetch a new one, once.
        csrfToken = null;
        return (await attempt()) as T;
      }
      throw error;
    }
  } catch (error) {
    if (notify && error instanceof ApiError && error.code === NOT_AUTHENTICATED) unauthenticatedHandler?.();
    throw error;
  }
}
