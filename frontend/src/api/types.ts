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

export interface ListQuery {
  page?: number;
  page_size?: number;
  state?: RunState;
}
