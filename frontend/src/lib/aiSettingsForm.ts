// The AI settings form as plain data and functions (no React): the draft an administrator edits, how it becomes the PUT
// body, and the checks that mirror the server's so a mistake is shown beside its field before anything is sent.
//
// NOT AUTHORITATIVE. Every rule here is a hint derived from what the server told us (`catalog`) or a shape the server
// documents; the backend validates again and decides. A server answer for a field is shown the same way as a local one,
// because both use the server's field paths (`connections[0].profiles[1].model_id`) and its stable error codes.
//
// SECRETS. A connection holds `credential_ref`, the NAME of a credential (a backend environment variable, or a managed credential,
// as `credential_source` says). This module never builds a field for a key, token, password or header, and refuses a reference
// that is not shaped like a name, so a pasted key is not sent. A secret VALUE is never part of the draft or of the PUT body: it is
// entered in the separate credentials panel and sent only to /api/ai-credentials/. A refused value is never echoed into a message.

import type { AiCatalog, AiConnectionFields, AiProfileRef, AiProviderStatus, AiProviderType, AiSettings, AiSettingsUpdate, CredentialSource } from "../api/types";
import { describeFieldCode } from "./errors";

/** Mirrors the backend's MAX_CONNECTIONS and MAX_PROFILES_PER_CONNECTION (apps/ai_settings/rules.py). */
export const MAX_CONNECTIONS = 10;
export const MAX_PROFILES = 10;
const MAX_ENDPOINT_LENGTH = 512;

const CONNECTION_ID = /^[a-z][a-z0-9_-]{0,47}$/;
const PROFILE_ID = /^[a-z][a-z0-9_-]{0,63}$/;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x1f\x7f]/;

export type Mode = "deterministic" | "ai";
export const SOURCE_NONE = "";
const LEGACY = "legacy:";
const CONNECTION = "connection:";

export const legacySource = (providerId: string) => `${LEGACY}${providerId}`;
export const connectionSource = (connectionId: string) => `${CONNECTION}${connectionId}`;

export interface ConnectionDraft {
  /** A stable identity for React and for "which card is open"; not sent. */
  key: string;
  /** True until the first save: the identifier can still be chosen. */
  isNew: boolean;
  connection_id: string;
  display_name: string;
  provider_type: string;
  locality: string;
  endpoint: string;
  credential_ref: string;
  credential_source: CredentialSource;
  enabled: boolean;
  profiles: AiProfileRef[];
}

export interface SettingsDraft {
  mode: Mode;
  /** "" (none), `legacy:<provider_id>` (the environment-configured provider) or `connection:<connection_id>`. */
  source: string;
  profile_id: string;
  policy_mode: string;
  /** Text as typed; it becomes an integer only in the payload. */
  timeout: string;
  connections: ConnectionDraft[];
}

/** `{field path: [error codes]}`, in the server's own path convention. */
export type Problems = Record<string, string[]>;

let keyCounter = 0;
const nextKey = () => `draft-${(keyCounter += 1)}`;

export function draftConnection(connection: AiConnectionFields): ConnectionDraft {
  return {
    key: nextKey(),
    isNew: false,
    connection_id: connection.connection_id,
    display_name: connection.display_name,
    provider_type: connection.provider_type,
    locality: connection.locality,
    endpoint: connection.endpoint ?? "",
    credential_ref: connection.credential_ref ?? "",
    credential_source: connection.credential_source,
    enabled: connection.enabled,
    profiles: connection.profiles.map((p) => ({ profile_id: p.profile_id, model_id: p.model_id })),
  };
}

export function emptyConnection(catalog: AiCatalog): ConnectionDraft {
  const type = catalog.provider_types[0];
  return {
    key: nextKey(),
    isNew: true,
    connection_id: "",
    display_name: "",
    provider_type: type?.provider_type ?? "",
    locality: type?.localities[0] ?? "",
    endpoint: "",
    credential_ref: "",
    credential_source: "environment",
    enabled: true,
    profiles: [],
  };
}

/** Changing the provider type invalidates everything that depends on it, so those fields start again. */
export function withProviderType(draft: ConnectionDraft, type: AiProviderType): ConnectionDraft {
  return { ...draft, provider_type: type.provider_type, locality: type.localities[0] ?? "", endpoint: "", credential_ref: "", credential_source: "environment", profiles: [] };
}

export function draftFromSettings(data: AiSettings): SettingsDraft {
  const { settings, connections, catalog } = data;
  const source = settings.extraction_mode !== "ai" ? SOURCE_NONE : settings.connection_id ? connectionSource(settings.connection_id) : settings.provider_id ? legacySource(settings.provider_id) : SOURCE_NONE;
  return {
    mode: settings.extraction_mode === "ai" ? "ai" : "deterministic",
    source,
    profile_id: settings.profile_id ?? "",
    policy_mode: settings.policy_mode ?? "",
    timeout: String(settings.timeout_seconds ?? catalog.timeout.default),
    connections: connections.map(draftConnection),
  };
}

// --- reading the draft ---

export function selectedConnection(draft: SettingsDraft): ConnectionDraft | null {
  if (!draft.source.startsWith(CONNECTION)) return null;
  const id = draft.source.slice(CONNECTION.length);
  return draft.connections.find((c) => c.connection_id === id) ?? null;
}

export function selectedLegacyId(draft: SettingsDraft): string | null {
  return draft.source.startsWith(LEGACY) ? draft.source.slice(LEGACY.length) : null;
}

export function typeOf(catalog: AiCatalog, providerType: string): AiProviderType | undefined {
  return catalog.provider_types.find((t) => t.provider_type === providerType);
}

/** The model profiles the selected source offers (a connection's own, or the environment provider's). */
export function profileOptions(draft: SettingsDraft, catalog: AiCatalog): AiProfileRef[] {
  const connection = selectedConnection(draft);
  if (connection) return connection.profiles;
  const legacy = selectedLegacyId(draft);
  return legacy ? (catalog.providers.find((p) => p.provider_id === legacy)?.profiles ?? []) : [];
}

/** The data policies the selected source may run under, or null when nothing is selected yet. Taken from the catalog, never from a local rule. */
export function permittedPolicyModes(draft: SettingsDraft, catalog: AiCatalog): string[] | null {
  const connection = selectedConnection(draft);
  if (connection) return catalog.locality_policies.find((p) => p.locality === connection.locality)?.policy_modes ?? [];
  const legacy = selectedLegacyId(draft);
  if (legacy === null) return null;
  const profile = catalog.providers.find((p) => p.provider_id === legacy)?.profiles.find((p) => p.profile_id === draft.profile_id);
  return profile ? profile.permitted_policy_modes : catalog.policy_modes;
}

export function selectedLocality(draft: SettingsDraft, catalog: AiCatalog): string | null {
  const connection = selectedConnection(draft);
  if (connection) return connection.locality;
  const legacy = selectedLegacyId(draft);
  return legacy ? (catalog.providers.find((p) => p.provider_id === legacy)?.locality ?? null) : null;
}

// --- draft <-> payload ---

export function connectionFields(draft: ConnectionDraft, catalog: AiCatalog): AiConnectionFields {
  const type = typeOf(catalog, draft.provider_type);
  const endpoint = draft.endpoint.trim();
  const ref = draft.credential_ref.trim();
  return {
    connection_id: draft.connection_id.trim(),
    display_name: draft.display_name.trim(),
    provider_type: draft.provider_type,
    locality: draft.locality,
    endpoint: type?.needs_endpoint && endpoint !== "" ? endpoint : null,
    credential_ref: type && type.credential !== "none" && ref !== "" ? ref : null,
    credential_source: type && type.credential !== "none" ? draft.credential_source : "environment",
    enabled: draft.enabled,
    profiles: draft.profiles.map((p) => ({ profile_id: p.profile_id.trim(), model_id: p.model_id.trim() })),
  };
}

function fieldsOfStored(connection: AiConnectionFields): AiConnectionFields {
  return {
    connection_id: connection.connection_id,
    display_name: connection.display_name,
    provider_type: connection.provider_type,
    locality: connection.locality,
    endpoint: connection.endpoint,
    credential_ref: connection.credential_ref,
    credential_source: connection.credential_source,
    enabled: connection.enabled,
    profiles: connection.profiles.map((p) => ({ profile_id: p.profile_id, model_id: p.model_id })),
  };
}

export function connectionsChanged(draft: SettingsDraft, baseline: AiSettings): boolean {
  const now = draft.connections.map((c) => connectionFields(c, baseline.catalog));
  const before = baseline.connections.map(fieldsOfStored);
  return JSON.stringify(now) !== JSON.stringify(before);
}

function normalizedValues(draft: SettingsDraft) {
  if (draft.mode === "deterministic") return { mode: draft.mode };
  return { mode: draft.mode, source: draft.source, profile_id: draft.profile_id, policy_mode: draft.policy_mode, timeout: draft.timeout.trim() };
}

/** Whether anything differs from what the server last sent. In deterministic mode the hidden AI fields do not count. */
export function isDirty(draft: SettingsDraft, baseline: AiSettings): boolean {
  const base = draftFromSettings(baseline);
  return JSON.stringify(normalizedValues(draft)) !== JSON.stringify(normalizedValues(base)) || connectionsChanged(draft, baseline);
}

/**
 * The PUT body. The connection list is included ONLY when it changed: a save of other fields then leaves the stored list
 * (including any item this page could not read) exactly as it is.
 */
export function buildUpdate(draft: SettingsDraft, baseline: AiSettings): AiSettingsUpdate {
  const catalog = baseline.catalog;
  const base = { expected_revision: baseline.revision, ...(connectionsChanged(draft, baseline) ? { connections: draft.connections.map((c) => connectionFields(c, catalog)) } : {}) };
  if (draft.mode === "deterministic") {
    return { extraction_mode: "deterministic", provider_id: null, connection_id: null, profile_id: null, policy_mode: null, timeout_seconds: null, ...base };
  }
  const connection = selectedConnection(draft);
  return {
    extraction_mode: "ai",
    provider_id: connection ? connection.provider_type : selectedLegacyId(draft),
    connection_id: connection ? connection.connection_id.trim() : null,
    profile_id: draft.profile_id,
    policy_mode: draft.policy_mode,
    timeout_seconds: Number(draft.timeout.trim()),
    ...base,
  };
}

/** After a 409 the user may keep their edits on top of the latest settings. An untouched connection list takes the latest list, so nobody else's change is overwritten by a stale copy. */
export function rebaseDraft(draft: SettingsDraft, previous: AiSettings, latest: AiSettings): SettingsDraft {
  const connections = connectionsChanged(draft, previous) ? draft.connections : latest.connections.map(draftConnection);
  return { ...draft, connections };
}

// --- checks that mirror the server's ---

function add(problems: Problems, path: string, code: string) {
  (problems[path] ??= []).push(code);
}

function validateConnection(problems: Problems, draft: ConnectionDraft, index: number, catalog: AiCatalog, earlierIds: Set<string>) {
  const at = (field: string) => `connections[${index}].${field}`;
  const fields = connectionFields(draft, catalog);
  const type = typeOf(catalog, draft.provider_type);

  if (fields.connection_id === "") add(problems, at("connection_id"), "required");
  else if (!CONNECTION_ID.test(fields.connection_id)) add(problems, at("connection_id"), "invalid_value");
  else if (earlierIds.has(fields.connection_id)) add(problems, at("connection_id"), "duplicate_connection_id");

  if (fields.display_name === "") add(problems, at("display_name"), "required");
  else if (fields.display_name.length > 80 || CONTROL.test(fields.display_name)) add(problems, at("display_name"), "invalid_value");

  if (!type) {
    add(problems, at("provider_type"), "unknown_provider_type");
    return;
  }
  if (!type.localities.includes(draft.locality)) add(problems, at("locality"), "locality_not_allowed_for_provider");

  if (type.needs_endpoint) {
    if (fields.endpoint === null) add(problems, at("endpoint"), "required");
    else if (fields.endpoint.length > MAX_ENDPOINT_LENGTH) add(problems, at("endpoint"), "invalid_value");
  }

  const ref = draft.credential_ref.trim();
  if (type.credential !== "none") {
    if (ref !== "" && !isCredentialRef(ref, catalog.credential_ref_prefix)) add(problems, at("credential_ref"), "invalid_credential_reference");
    else if (ref === "" && (draft.credential_source === "managed" || (type.credential === "required_unless_local" && draft.locality !== "local"))) add(problems, at("credential_ref"), "required");
  }

  if (fields.profiles.length === 0) add(problems, at("profiles"), "required");
  else if (fields.profiles.length > MAX_PROFILES) add(problems, at("profiles"), "too_many");
  const seen = new Set<string>();
  fields.profiles.forEach((profile, i) => {
    const profileAt = (name: string) => `connections[${index}].profiles[${i}].${name}`;
    if (profile.profile_id === "") add(problems, profileAt("profile_id"), "required");
    else if (!PROFILE_ID.test(profile.profile_id)) add(problems, profileAt("profile_id"), "invalid_value");
    else if (seen.has(profile.profile_id)) add(problems, profileAt("profile_id"), "duplicate_profile_id");
    seen.add(profile.profile_id);
    if (profile.model_id === "") add(problems, profileAt("model_id"), "required");
    else if (!MODEL_ID.test(profile.model_id)) add(problems, profileAt("model_id"), "invalid_value");
  });
}

/** True for `AP_AI_CRED_` followed by 1 to 48 upper-case letters, digits or underscores: the shape of a variable NAME, never of a key. */
export function isCredentialRef(value: string, prefix: string): boolean {
  return value.startsWith(prefix) && /^[A-Z0-9_]{1,48}$/.test(value.slice(prefix.length));
}

export function validateDraft(draft: SettingsDraft, catalog: AiCatalog): Problems {
  const problems: Problems = {};
  if (draft.connections.length > MAX_CONNECTIONS) add(problems, "connections", "too_many");
  const earlier = new Set<string>();
  draft.connections.forEach((connection, index) => {
    validateConnection(problems, connection, index, catalog, earlier);
    earlier.add(connection.connection_id.trim());
  });

  if (draft.mode === "deterministic") return problems;

  const connection = selectedConnection(draft);
  const legacy = selectedLegacyId(draft);
  if (draft.source === SOURCE_NONE) add(problems, "connection_id", "required");
  else if (legacy !== null) {
    if (!catalog.providers.some((p) => p.provider_id === legacy)) add(problems, "provider_id", "unknown_provider");
  } else if (!connection) add(problems, "connection_id", "unknown_connection");
  else if (!connection.enabled) add(problems, "connection_id", "connection_disabled");
  else if (Object.keys(problems).some((path) => path.startsWith(`connections[${draft.connections.indexOf(connection)}]`))) add(problems, "connection_id", "invalid_connection");

  if (draft.source !== SOURCE_NONE) {
    if (draft.profile_id === "") add(problems, "profile_id", "required");
    else if (!profileOptions(draft, catalog).some((p) => p.profile_id === draft.profile_id)) add(problems, "profile_id", "unknown_profile");
  }

  if (draft.policy_mode === "") add(problems, "policy_mode", "required");
  else if (!catalog.policy_modes.includes(draft.policy_mode)) add(problems, "policy_mode", "invalid_choice");
  else if (!(permittedPolicyModes(draft, catalog) ?? catalog.policy_modes).includes(draft.policy_mode)) add(problems, "policy_mode", "policy_forbids_provider");

  const timeout = draft.timeout.trim();
  if (timeout === "") add(problems, "timeout_seconds", "required");
  else if (!/^\d{1,6}$/.test(timeout)) add(problems, "timeout_seconds", "invalid_value");
  else if (Number(timeout) < catalog.timeout.min || Number(timeout) > catalog.timeout.max) add(problems, "timeout_seconds", "out_of_range");
  return problems;
}

// --- text ---

const CODE_TEXT: Record<string, string> = {
  required: "This is required.",
  invalid_value: "This value is not valid.",
  invalid_type: "This value is not valid.",
  invalid_choice: "Choose one of the listed options.",
  invalid_credential_reference: "Enter only the NAME of a credential, starting with the prefix shown. Never enter a key or password here.",
  duplicate_connection_id: "Another connection already uses this identifier.",
  duplicate_profile_id: "Another profile of this connection already uses this identifier.",
  too_many: "There are too many.",
  out_of_range: "This value is outside the allowed range.",
  exceeds_worker_lease: "This timeout is longer than the worker's lease allows. Choose a shorter timeout.",
  unknown_provider_type: "This provider type is not available.",
  unknown_provider: "This provider is not available.",
  unknown_profile: "This model profile is not offered by the selected connection.",
  model_not_of_profile: "This model is not the one the profile stands for.",
  profile_not_of_provider: "This profile does not belong to the selected provider.",
  provider_not_of_connection: "The provider does not match the selected connection.",
  unknown_connection: "The selected connection does not exist.",
  connection_disabled: "The selected connection is disabled. Enable it or choose another.",
  invalid_connection: "The selected connection has problems. Fix them first.",
  locality_not_allowed_for_provider: "This provider type cannot be used with this locality.",
  policy_forbids_provider: "This data policy does not permit the selected connection's locality.",
  not_allowed: "This is not allowed for this provider type.",
  not_allowed_in_deterministic_mode: "This is not allowed in deterministic mode.",
  endpoint_locality_mismatch: "The endpoint does not match the declared locality.",
  endpoint_not_loopback: "A local endpoint must be on this machine (localhost or a loopback address).",
  plain_http_not_allowed: "Only a local endpoint may use plain http; use https.",
  invalid_endpoint: "This endpoint is not acceptable.",
  unknown_field: "This field is not recognised.",
};

export function describeSettingsCode(code: string): string {
  return CODE_TEXT[code] ?? describeFieldCode(code);
}

export function describeCodes(codes: string[] | undefined): string | null {
  return codes && codes.length > 0 ? codes.map(describeSettingsCode).join(" ") : null;
}

const LOCALITY_LABELS: Record<string, string> = {
  local: "Local (this machine)",
  company_endpoint: "Company endpoint",
  cloud: "Cloud",
};
export const localityLabel = (locality: string) => LOCALITY_LABELS[locality] ?? locality;

const POLICY_LABELS: Record<string, { label: string; help: string }> = {
  local_only: { label: "Local only", help: "Request text may go only to a model running on this machine." },
  company_endpoint: { label: "Company endpoint", help: "Request text may go to a local model or a company-approved server." },
  approved_cloud: { label: "Approved cloud", help: "Request text may go to a cloud provider. Selecting a cloud connection under this policy is the explicit approval." },
};
export const policyLabel = (mode: string) => POLICY_LABELS[mode]?.label ?? mode;
export const policyHelp = (mode: string) => POLICY_LABELS[mode]?.help ?? null;

const ISSUE_TEXT: Record<string, string> = {
  missing_api_key: "The credential variable is not set in the backend environment.",
  invalid_configuration: "The configuration is not valid in the backend environment.",
  endpoint_not_approved: "The endpoint is not in the operator's approved list.",
};

const MANAGED_ISSUE_TEXT: Record<string, string> = {
  missing_api_key: "The managed credential is not stored, or the secret store cannot supply it. Store it under Managed credentials below.",
};

/** `source` matters only for `missing_api_key`: the sentence differs for an environment variable and a managed credential. */
export function describeStatus(status: AiProviderStatus, source: CredentialSource = "environment"): { ok: boolean; label: string; detail: string | null } {
  if (status.configured) return { ok: true, label: "Configured", detail: null };
  const table = source === "managed" ? { ...ISSUE_TEXT, ...MANAGED_ISSUE_TEXT } : ISSUE_TEXT;
  return { ok: false, label: "Not configured", detail: status.issue ? (table[status.issue] ?? `The configuration is not usable (${status.issue}).`) : "The configuration is not usable." };
}

export function providerTypeLabel(catalog: AiCatalog, providerType: string): string {
  return typeOf(catalog, providerType)?.label ?? providerType;
}

/** The connection a path such as `connections[2].endpoint` belongs to: its index and the field, else null. */
export function parseConnectionPath(path: string): { index: number; field: string } | null {
  const match = /^connections\[(\d+)\](?:\.(.+))?$/.exec(path);
  return match ? { index: Number(match[1]), field: match[2] ?? "" } : null;
}
