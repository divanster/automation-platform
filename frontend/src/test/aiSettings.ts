// TEST ONLY: synthetic AI settings. Every value is made up; no endpoint resolves and no secret exists anywhere in it.

import type { AiCatalog, AiConnection, AiCredentialStatus, AiSettings, Profile } from "../api/types";
import { apiError, json, type Handler, type RecordedCall } from "./fakeApi";
import { operator } from "./fixtures";

export const SECRET_SENTINEL = "SENTINEL-SECRET-VALUE-0451";

export function administrator(overrides: Partial<Profile> = {}): Profile {
  return operator({ username: "synthetic.admin", is_superuser: true, roles: [], permissions: ["intake.view_any_request", "intake.submit_request"], ...overrides });
}

export function catalog(overrides: Partial<AiCatalog> = {}): AiCatalog {
  return {
    providers: [
      {
        provider_id: "kpmg_workbench",
        label: "KPMG Workbench (environment)",
        locality: "company_endpoint",
        status: { configured: true, issue: null },
        profiles: [{ profile_id: "workbench-gpt5", model_id: "gpt-5", permitted_policy_modes: ["company_endpoint", "approved_cloud"] }],
      },
    ],
    provider_types: [
      {
        provider_type: "kpmg_workbench",
        label: "KPMG Workbench",
        localities: ["company_endpoint"],
        needs_endpoint: false,
        credential: "optional",
        profile_source: "fixed",
        profiles: [{ profile_id: "workbench-gpt5", model_id: "gpt-5", permitted_policy_modes: ["company_endpoint", "approved_cloud"] }],
      },
      { provider_type: "openai_compatible", label: "OpenAI-compatible server", localities: ["local", "company_endpoint", "cloud"], needs_endpoint: true, credential: "required_unless_local", profile_source: "declared", profiles: [] },
      { provider_type: "ollama", label: "Ollama (local)", localities: ["local"], needs_endpoint: true, credential: "none", profile_source: "declared", profiles: [] },
    ],
    locality_policies: [
      { locality: "local", policy_modes: ["local_only", "company_endpoint", "approved_cloud"] },
      { locality: "company_endpoint", policy_modes: ["company_endpoint", "approved_cloud"] },
      { locality: "cloud", policy_modes: ["approved_cloud"] },
    ],
    credential_ref_prefix: "AP_AI_CRED_",
    policy_modes: ["local_only", "company_endpoint", "approved_cloud"],
    timeout: { min: 5, max: 600, default: 45 },
    ...overrides,
  };
}

export function connection(overrides: Partial<AiConnection> = {}): AiConnection {
  return {
    connection_id: "corp-llm",
    display_name: "Company model server",
    provider_type: "openai_compatible",
    locality: "company_endpoint",
    endpoint: "https://llm.corp.example.invalid/v1",
    credential_ref: "AP_AI_CRED_CORP_LLM",
    credential_source: "environment",
    enabled: true,
    profiles: [{ profile_id: "corp-small", model_id: "corp-model-8b" }],
    status: { configured: true, issue: null },
    ...overrides,
  };
}

export function ollama(overrides: Partial<AiConnection> = {}): AiConnection {
  return connection({
    connection_id: "ollama-local",
    display_name: "Ollama on this machine",
    provider_type: "ollama",
    locality: "local",
    endpoint: "http://127.0.0.1:11434",
    credential_ref: null,
    profiles: [{ profile_id: "llama", model_id: "llama3.1:8b" }],
    ...overrides,
  });
}

export function settings(overrides: Partial<AiSettings> = {}): AiSettings {
  return {
    settings: { extraction_mode: "deterministic", provider_id: null, connection_id: null, profile_id: null, policy_mode: null, timeout_seconds: null },
    connections: [],
    unreadable_connections: 0,
    stored: true,
    revision: 3,
    updated_at: "2026-11-01T09:30:00Z",
    updated_by: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    apply: "worker_restart_required",
    catalog: catalog(),
    ...overrides,
  };
}

/** The settings of an instance that selects `corp-llm` (with an `ollama-local` connection beside it). */
export function withSelection(overrides: Partial<AiSettings> = {}): AiSettings {
  return settings({
    settings: { extraction_mode: "ai", provider_id: "openai_compatible", connection_id: "corp-llm", profile_id: "corp-small", policy_mode: "company_endpoint", timeout_seconds: 45 },
    connections: [connection(), ollama()],
    ...overrides,
  });
}

export const SETTINGS_PATH = "/api/ai-settings/extraction/";

// --- managed credentials (TEST ONLY: no secret value is ever stored by the stand-in; only whether one "exists") ---

export const CREDENTIALS_PATH = "/api/ai-credentials/";

export function credentialStatus(reference: string, status: AiCredentialStatus["status"], connections: string[] = []): AiCredentialStatus {
  return { reference, source: "managed", status, store: "dpapi_windows", connections, apply: "worker_restart_required" };
}

/** A connection that reads the managed credential `reference`. */
export function managedConnection(overrides: Partial<AiConnection> = {}): AiConnection {
  return connection({ credential_source: "managed", credential_ref: "AP_AI_CRED_CORP_LLM", ...overrides });
}

/**
 * A stateful stand-in for /api/ai-credentials/ that follows the backend's rules: create refuses an existing reference (409), replace and
 * delete refuse a missing one (404), delete is refused while a stored connection reads it (409 `credential_in_use`).
 * `readers(reference)` names the stored connections that read it; `onChange` lets a test react to a stored change.
 */
export function credentialServer(
  initial: Record<string, "set" | "unreadable">,
  options: { readers?: (reference: string) => string[]; onChange?: (reference: string, action: "create" | "replace" | "delete") => void; fail?: (call: RecordedCall) => ReturnType<Handler> | undefined } = {},
) {
  const stored = new Map<string, "set" | "unreadable">(Object.entries(initial));
  const readers = (reference: string) => options.readers?.(reference) ?? [];
  const handler: Handler = (call) => {
    if (!call.path.startsWith(CREDENTIALS_PATH)) return apiError(404, "not_found");
    const injected = options.fail?.(call);
    if (injected !== undefined) return injected;
    if (call.path === CREDENTIALS_PATH) {
      if (call.method !== "POST") return apiError(405, "method_not_allowed");
      const { reference } = call.body as { reference: string };
      if (stored.has(reference)) return apiError(409, "credential_already_exists");
      stored.set(reference, "set");
      options.onChange?.(reference, "create");
      return json(201, credentialStatus(reference, "set", readers(reference)));
    }
    const reference = decodeURIComponent(call.path.slice(CREDENTIALS_PATH.length).replace(/\/$/, ""));
    if (call.method === "GET") return json(200, credentialStatus(reference, stored.get(reference) ?? "not_set", readers(reference)));
    if (!stored.has(reference)) return apiError(404, "credential_not_found");
    if (call.method === "PUT") {
      stored.set(reference, "set");
      options.onChange?.(reference, "replace");
      return json(200, credentialStatus(reference, "set", readers(reference)));
    }
    if (call.method === "DELETE") {
      if (readers(reference).length > 0) return apiError(409, "credential_in_use", { connections: readers(reference) });
      stored.delete(reference);
      options.onChange?.(reference, "delete");
      return json(200, credentialStatus(reference, "not_set"));
    }
    return apiError(405, "method_not_allowed");
  };
  return { stored, handler };
}

/** One handler for both endpoints. */
export function combined(...handlers: Handler[]): Handler {
  return (call) => (call.path.startsWith(CREDENTIALS_PATH) ? handlers[handlers.length - 1]!(call) : handlers[0]!(call));
}

/** A stateful stand-in for the endpoint: GET returns `current`; PUT calls `onPut` (default: accept, bump the revision, store what was sent). */
export function settingsServer(initial: AiSettings, options: { onPut?: (call: RecordedCall, current: AiSettings) => ReturnType<Handler> } = {}) {
  const state = { current: initial };
  const handler: Handler = (call) => {
    if (call.path !== SETTINGS_PATH) return apiError(404, "not_found");
    if (call.method === "GET") return json(200, state.current);
    if (call.method === "PUT") {
      if (options.onPut) return options.onPut(call, state.current);
      return acceptPut(state, call);
    }
    return apiError(405, "method_not_allowed");
  };
  return { state, handler };
}

interface PutBody {
  extraction_mode: string;
  provider_id: string | null;
  connection_id: string | null;
  profile_id: string | null;
  policy_mode: string | null;
  timeout_seconds: number | null;
  connections?: Array<Omit<AiConnection, "status">>;
  expected_revision: number;
}

export function acceptPut(state: { current: AiSettings }, call: RecordedCall) {
  const body = call.body as PutBody;
  if (body.expected_revision !== state.current.revision) return apiError(409, "stale_settings_revision");
  const connections = body.connections ? body.connections.map((c) => ({ ...c, status: { configured: true, issue: null } })) : state.current.connections;
  state.current = {
    ...state.current,
    settings: {
      extraction_mode: body.extraction_mode,
      provider_id: body.provider_id,
      connection_id: body.connection_id,
      profile_id: body.profile_id,
      policy_mode: body.policy_mode,
      timeout_seconds: body.timeout_seconds,
    },
    connections,
    revision: state.current.revision + 1,
    stored: true,
  };
  return json(200, state.current);
}
