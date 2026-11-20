// The AI settings endpoint (backend/apps/api/views_ai_settings.py): GET and PUT /api/ai-settings/extraction/.
// Administrator only; the server decides, and answers 403 `permission_denied` to anyone else.
//
// SECRETS. The API has no credential field and this client never adds one. The answer is also copied field by field
// into new objects (`parse*` below): a field the contract does not list is dropped here and can never reach the screen,
// whatever a server (or a proxy in between) sent.

import { ApiError, UNEXPECTED_RESPONSE, request } from "./http";
import type {
  AiCatalog,
  AiCatalogProfile,
  AiConnection,
  AiProfileRef,
  AiProvider,
  AiProviderStatus,
  AiProviderType,
  AiSettings,
  AiSettingsUpdate,
  AiSettingsValues,
} from "./types";

const PATH = "/api/ai-settings/extraction/";

function unexpected(): ApiError {
  return new ApiError(200, UNEXPECTED_RESPONSE, "The server sent a response the application does not understand.");
}

type Rec = Record<string, unknown>;

function isRec(value: unknown): value is Rec {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function str(value: unknown): string {
  if (typeof value !== "string") throw unexpected();
  return value;
}
function strOrNull(value: unknown): string | null {
  if (value === null) return null;
  return str(value);
}
function bool(value: unknown): boolean {
  if (typeof value !== "boolean") throw unexpected();
  return value;
}
function int(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value)) throw unexpected();
  return value;
}
function intOrNull(value: unknown): number | null {
  return value === null ? null : int(value);
}
function rec(value: unknown): Rec {
  if (!isRec(value)) throw unexpected();
  return value;
}
function list<T>(value: unknown, item: (entry: unknown) => T): T[] {
  if (!Array.isArray(value)) throw unexpected();
  return value.map(item);
}
function strings(value: unknown): string[] {
  return list(value, str);
}

function parseStatus(value: unknown): AiProviderStatus {
  const v = rec(value);
  return { configured: bool(v.configured), issue: strOrNull(v.issue) };
}

function parseProfileRef(value: unknown): AiProfileRef {
  const v = rec(value);
  return { profile_id: str(v.profile_id), model_id: str(v.model_id) };
}

function parseCatalogProfile(value: unknown): AiCatalogProfile {
  const v = rec(value);
  return { profile_id: str(v.profile_id), model_id: str(v.model_id), permitted_policy_modes: strings(v.permitted_policy_modes) };
}

function parseProvider(value: unknown): AiProvider {
  const v = rec(value);
  return {
    provider_id: str(v.provider_id),
    label: str(v.label),
    locality: str(v.locality),
    status: parseStatus(v.status),
    profiles: list(v.profiles, parseCatalogProfile),
  };
}

function parseProviderType(value: unknown): AiProviderType {
  const v = rec(value);
  return {
    provider_type: str(v.provider_type),
    label: str(v.label),
    localities: strings(v.localities),
    needs_endpoint: bool(v.needs_endpoint),
    credential: str(v.credential),
    profile_source: str(v.profile_source),
    profiles: list(v.profiles, parseCatalogProfile),
  };
}

function parseConnection(value: unknown): AiConnection {
  const v = rec(value);
  return {
    connection_id: str(v.connection_id),
    display_name: str(v.display_name),
    provider_type: str(v.provider_type),
    locality: str(v.locality),
    endpoint: strOrNull(v.endpoint),
    credential_ref: strOrNull(v.credential_ref),
    enabled: bool(v.enabled),
    profiles: list(v.profiles, parseProfileRef),
    status: parseStatus(v.status),
  };
}

function parseValues(value: unknown): AiSettingsValues {
  const v = rec(value);
  return {
    extraction_mode: str(v.extraction_mode),
    provider_id: strOrNull(v.provider_id),
    connection_id: strOrNull(v.connection_id),
    profile_id: strOrNull(v.profile_id),
    policy_mode: strOrNull(v.policy_mode),
    timeout_seconds: intOrNull(v.timeout_seconds),
  };
}

function parseCatalog(value: unknown): AiCatalog {
  const v = rec(value);
  const timeout = rec(v.timeout);
  return {
    providers: list(v.providers, parseProvider),
    provider_types: list(v.provider_types, parseProviderType),
    locality_policies: list(v.locality_policies, (entry) => {
      const e = rec(entry);
      return { locality: str(e.locality), policy_modes: strings(e.policy_modes) };
    }),
    credential_ref_prefix: str(v.credential_ref_prefix),
    policy_modes: strings(v.policy_modes),
    timeout: { min: int(timeout.min), max: int(timeout.max), default: int(timeout.default) },
  };
}

/** Exported for tests. Throws `unexpected_response` when the body is not the contract's shape. */
export function parseAiSettings(body: unknown): AiSettings {
  const v = rec(body);
  return {
    settings: parseValues(v.settings),
    connections: list(v.connections, parseConnection),
    unreadable_connections: int(v.unreadable_connections),
    stored: bool(v.stored),
    revision: int(v.revision),
    updated_at: strOrNull(v.updated_at),
    updated_by: strOrNull(v.updated_by),
    apply: str(v.apply),
    catalog: parseCatalog(v.catalog),
  };
}

export async function getAiSettings(signal?: AbortSignal): Promise<AiSettings> {
  return parseAiSettings(await request<unknown>(PATH, { signal }));
}

/** Save the settings (and, when `update.connections` is present, the complete connection list). Same-origin, session cookie, CSRF header. */
export async function saveAiSettings(update: AiSettingsUpdate, signal?: AbortSignal): Promise<AiSettings> {
  return parseAiSettings(await request<unknown>(PATH, { method: "PUT", body: update, signal }));
}
