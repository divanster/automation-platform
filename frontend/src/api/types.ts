// Types of the REST API contract. Mirrors backend/apps/api/serializers.py and docs/development/rest-api-slice.md
// (sections 4 and 5). Nothing here is a business rule: it is the shape of what the server sends.
//
// DECIMALS. A draft item's `quantity` is decimal TEXT (for example "2.50"). It is typed `string | null` on purpose
// and must never be converted to a JavaScript number: that would lose digits and the exact scale the backend hashes.

export const RUN_STATES = [
  "queued",
  "extracting",
  "needs_review",
  "approved",
  "exporting",
  "exported",
  "failed",
  "cancelled",
] as const;
export type RunState = (typeof RUN_STATES)[number];

export type OutcomeStatus = "valid" | "invalid";
export type DraftSource = "revision" | "extraction_outcome";
export type ReviewAction = "edit" | "approve" | "reject";

export interface Profile {
  public_id: string;
  username: string;
  is_superuser: boolean;
  roles: string[];
  permissions: string[];
}

export interface LoginResponse extends Profile {
  csrf_token: string;
}

export interface SubmitResponse {
  run_id: string;
  case_id: string;
  reference: string;
  state: RunState;
  created: boolean;
}

export interface RequestListItem {
  run_id: string;
  case_id: string;
  reference: string;
  state: RunState;
  failed_step: string;
  created_at: string;
  current_revision_number: number | null;
  outcome_status: OutcomeStatus | null;
  submitted_by: string;
}

export interface Page<T> {
  count: number;
  page: number;
  page_size: number;
  results: T[];
}

// GET /api/dashboard/summary/ (backend DashboardSummaryOutput). Every figure is computed by the server.
export interface DashboardSummary {
  total: number;
  /** queued + extracting + exporting, summed by the server. */
  processing: number;
  needs_review: number;
  approved: number;
  failed: number;
  /** Every run state, zero when there is none. */
  by_state: Record<RunState, number>;
  /** The newest requests the user may view, newest first (the server fixes how many). */
  recent_requests: RequestListItem[];
}

export interface Finding {
  code: string;
  field_path: string;
  blocking: boolean;
  message: string;
  excerpt: string | null;
}

export interface WarningPair {
  code: string;
  field_path: string;
}

export interface Processing {
  step: string;
  task_state: string;
  attempt_count: number;
  failure_code: string | null;
}

export interface CurrentRevision {
  id: string;
  number: number;
  content_hash: string;
  created_at: string;
}

export interface DraftItem {
  description: string | null;
  quantity: string | null; // decimal text, never a number
  unit: string | null;
  notes: string | null;
}

export interface DraftSourceRef {
  field_path: string;
  excerpt: string;
  line: number | null;
}

export interface Draft {
  requester: { name: string | null; contact: string | null };
  items: DraftItem[];
  notes: string | null;
  source_refs: DraftSourceRef[];
}

export interface RequestDetail {
  run_id: string;
  case_id: string;
  reference: string;
  state: RunState;
  failed_step: string;
  created_at: string;
  submitted_by: string;
  processing: Processing | null;
  outcome_status: OutcomeStatus | null;
  current_revision: CurrentRevision | null;
  draft: Draft | null;
  draft_source: DraftSource | null;
  findings: Finding[];
  warnings: WarningPair[];
  approval_count: number;
  rejection_count: number;
  allowed_actions: ReviewAction[];
  action_blockers: Record<string, string>;
}

// --- review: edit, approve, reject, audit (backend/apps/api/serializers.py, EditOutput .. AuditEventOutput) ---

// The reasons a reviewer may give for a rejection. The API has no endpoint that lists them, so they are mirrored here
// from apps.intake.policy.REJECTION_REASONS; backend/tests/test_frontend_contract.py fails if the two lists differ.
// Free text is never accepted: the server answers 400 `validation_error` for any other value.
export const REJECTION_REASONS = ["incorrect_content", "incomplete_content", "not_supported_by_source", "other"] as const;
export type RejectionReason = (typeof REJECTION_REASONS)[number];

export interface EditResponse {
  revision_id: string;
  revision_number: number;
  content_hash: string;
  run_state: RunState;
  warnings: WarningPair[];
  approval_invalidated: boolean;
}

export interface ApproveResponse {
  approval_id: string;
  revision_id: string;
  content_hash: string;
  run_state: RunState;
  acknowledged_warnings: WarningPair[];
}

export interface RejectResponse {
  rejection_id: string;
  revision_id: string;
  reason_code: string;
  run_state: RunState;
}

export type AuditDetailValue = string | number | boolean | null;

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor_kind: "user" | "system";
  actor_public_id: string | null;
  action: string;
  subject_type: string;
  subject_id: string;
  details: Record<string, AuditDetailValue>;
}

// --- AI settings (backend/apps/api/serializers.py, AIExtractionSettingsOutput and AIExtractionSettingsInput) ---
// There is deliberately NO field that holds a credential: `credential_ref` is the NAME of a credential (a backend environment
// variable, or a managed credential in the secret store, as `credential_source` says), never its value.

/** Where the credential named by `credential_ref` is read from. There is no fallback from one to the other. */
export type CredentialSource = "environment" | "managed";

export interface AiProviderStatus {
  configured: boolean;
  /** A fixed code (`missing_api_key`, `invalid_configuration`, `endpoint_not_approved`) or null. Never a value. */
  issue: string | null;
}

export interface AiProfileRef {
  profile_id: string;
  model_id: string;
}

export interface AiCatalogProfile extends AiProfileRef {
  permitted_policy_modes: string[];
}

export interface AiProvider {
  provider_id: string;
  label: string;
  locality: string;
  status: AiProviderStatus;
  profiles: AiCatalogProfile[];
}

export interface AiProviderType {
  provider_type: string;
  label: string;
  localities: string[];
  needs_endpoint: boolean;
  /** `none`, `optional` or `required_unless_local`. */
  credential: string;
  /** `fixed` (pick among `profiles`) or `declared` (name the model identifiers). */
  profile_source: string;
  profiles: AiCatalogProfile[];
}

export interface AiLocalityPolicy {
  locality: string;
  policy_modes: string[];
}

export interface AiConnectionFields {
  connection_id: string;
  display_name: string;
  provider_type: string;
  locality: string;
  endpoint: string | null;
  credential_ref: string | null;
  credential_source: CredentialSource;
  enabled: boolean;
  profiles: AiProfileRef[];
}

export interface AiConnection extends AiConnectionFields {
  status: AiProviderStatus;
}

export interface AiSettingsValues {
  extraction_mode: string;
  provider_id: string | null;
  connection_id: string | null;
  profile_id: string | null;
  policy_mode: string | null;
  timeout_seconds: number | null;
}

export interface AiCatalog {
  providers: AiProvider[];
  provider_types: AiProviderType[];
  locality_policies: AiLocalityPolicy[];
  credential_ref_prefix: string;
  policy_modes: string[];
  timeout: { min: number; max: number; default: number };
}

export interface AiSettings {
  settings: AiSettingsValues;
  connections: AiConnection[];
  unreadable_connections: number;
  stored: boolean;
  revision: number;
  updated_at: string | null;
  updated_by: string | null;
  /** `worker_restart_required`: a saved change reaches a worker only when it starts. */
  apply: string;
  catalog: AiCatalog;
}

/** The body of PUT /api/ai-settings/extraction/. `connections`, when present, is the COMPLETE list and replaces the stored one. */
export interface AiSettingsUpdate extends AiSettingsValues {
  connections?: AiConnectionFields[];
  expected_revision: number;
}

// --- Managed AI credentials (backend/apps/api/views_ai_credentials.py, AICredentialStatusOutput) ---
// The secret value is accepted by POST and PUT only and is never returned: no type here has a field that could hold one.

/** `set`: stored and readable by the web process; `not_set`: nothing stored; `unreadable`: a file exists that this process cannot decrypt. */
export type CredentialStatusValue = "set" | "not_set" | "unreadable";

export interface AiCredentialStatus {
  reference: string;
  source: "managed";
  status: CredentialStatusValue;
  /** The secret store backend's identifier. */
  store: string;
  /** Identifiers of the stored connections that read this credential. */
  connections: string[];
  /** `worker_restart_required`: a change reaches a worker only when it starts. */
  apply: string;
}

export interface ListQuery {
  page?: number;
  page_size?: number;
  state?: RunState;
}
