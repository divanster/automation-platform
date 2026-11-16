// User-facing text for the API's stable error codes (docs/development/rest-api-slice.md, section 4).
//
// The decision is made from `code` (and `details` codes), never from the server's message text, and the text shown is
// written here: nothing the user typed and nothing from a response body is echoed into an error message.

import { ApiError, NETWORK_ERROR, UNEXPECTED_RESPONSE } from "../api/http";

const REQUEST_TEXT_REASONS: Record<string, string> = {
  blank: "Enter the request text.",
  too_large: "The request text is too large.",
  contains_nul: "The request text contains a character that cannot be stored.",
  unencodable: "The request text contains a character that cannot be stored.",
  not_text: "The request text is not valid text.",
};

const MESSAGES: Record<string, string> = {
  [NETWORK_ERROR]: "The server could not be reached. Check that the backend is running, then try again.",
  [UNEXPECTED_RESPONSE]: "The server sent a response this application does not understand.",
  not_authenticated: "You are not signed in.",
  csrf_failed: "The security check failed. Please try again.",
  permission_denied: "You do not have permission to do this.",
  invalid_credentials: "The username or password is incorrect.",
  not_found: "This request does not exist, or you are not allowed to see it.",
  idempotency_conflict: "This submission conflicts with an earlier one. Please submit again.",
  malformed_json: "The request could not be understood by the server.",
  validation_error: "Some of the entered values are not valid.",
  payload_too_large: "The request is too large.",
  method_not_allowed: "The server does not support this operation.",
  unsupported_media_type: "The server does not support this operation.",
  not_acceptable: "The server does not support this operation.",
  internal_error: "The server reported an internal error. Try again later.",
  // review (edit, approve, reject)
  stale_base_revision: "This request was changed after you opened it, so your edit was not saved.",
  stale_revision: "This request was changed after you opened it, so nothing was recorded. The latest version has been loaded.",
  content_hash_mismatch: "This request was changed after you opened it, so nothing was recorded. The latest version has been loaded.",
  run_not_in_review: "The request is no longer waiting for review.",
  run_state_not_editable: "The request cannot be edited in its current state.",
  no_current_revision: "The request has no revision yet.",
  nothing_to_edit: "There is nothing to edit.",
  revision_rejected: "This revision was rejected and cannot be approved. Edit it to create a new revision.",
  unchanged_content: "The values are identical to the current revision, so nothing was saved.",
  self_approval_forbidden: "You cannot approve a request that you submitted or edited.",
  warnings_not_acknowledged: "The warnings changed. Review the current warnings and acknowledge them again.",
  unknown_warning_acknowledged: "The warnings changed. Review the current warnings and acknowledge them again.",
  draft_invalid: "The draft has blocking findings and was not saved. Nothing was changed.",
  draft_not_serializable: "A value in the draft cannot be saved.",
};

/** The answers that mean "what you were looking at is no longer current": the page reloads the request after them. */
const OUT_OF_DATE_CODES: ReadonlySet<string> = new Set([
  "stale_base_revision",
  "stale_revision",
  "content_hash_mismatch",
  "run_not_in_review",
  "run_state_not_editable",
  "no_current_revision",
  "nothing_to_edit",
  "revision_rejected",
  "warnings_not_acknowledged",
  "unknown_warning_acknowledged",
  "self_approval_forbidden",
]);

export function isOutOfDate(error: unknown): boolean {
  return error instanceof ApiError && OUT_OF_DATE_CODES.has(error.code);
}

export function isStaleRevision(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "stale_base_revision" || error.code === "stale_revision" || error.code === "content_hash_mismatch");
}

export interface ErrorFinding {
  code: string;
  field_path: string;
  blocking: boolean;
  message: string;
}

/** The findings of a 400 `draft_invalid` (the contract's `details.findings`; no excerpts), else null. */
export function findingsFrom(error: unknown): ErrorFinding[] | null {
  if (!(error instanceof ApiError) || error.code !== "draft_invalid") return null;
  const raw = error.details?.findings;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): ErrorFinding[] => {
    if (typeof item !== "object" || item === null) return [];
    const f = item as Record<string, unknown>;
    if (typeof f.code !== "string" || typeof f.field_path !== "string") return [];
    return [{ code: f.code, field_path: f.field_path, blocking: f.blocking === true, message: typeof f.message === "string" ? f.message : "" }];
  });
}

/** The `{path: [error codes]}` of a 400 `validation_error` (codes only), else an empty object. */
export function fieldCodesFrom(error: unknown): Record<string, string[]> {
  if (!(error instanceof ApiError) || error.code !== "validation_error") return {};
  const fields = error.details?.fields;
  if (typeof fields !== "object" || fields === null) return {};
  const result: Record<string, string[]> = {};
  for (const [path, codes] of Object.entries(fields as Record<string, unknown>)) {
    if (Array.isArray(codes)) result[path] = codes.filter((c): c is string => typeof c === "string");
  }
  return result;
}

const FIELD_CODE_TEXT: Record<string, string> = {
  invalid: "This value is not accepted.",
  max_length: "This value is too long.",
  max_value: "This value is too large.",
  min_value: "This value is too small.",
  null: "A value is not allowed here.",
  unknown_field: "This field is not recognised.",
  invalid_choice: "Choose one of the listed options.",
};

export function describeFieldCode(code: string): string {
  return FIELD_CODE_TEXT[code] ?? `This value is not accepted (${code}).`;
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong.";
  if (error.code === "throttled") {
    return error.retryAfterSeconds
      ? `Too many attempts. Try again in ${error.retryAfterSeconds} seconds.`
      : "Too many attempts. Try again in a moment.";
  }
  if (error.code === "invalid_request_text") {
    const reason = error.details?.reason;
    return (typeof reason === "string" && REQUEST_TEXT_REASONS[reason]) || "The request text is not acceptable.";
  }
  const message = MESSAGES[error.code];
  if (message) return message;
  // An unlisted code is shown as the code only: it is a stable identifier, never a value the user sent.
  return `The request could not be completed (${error.code}).`;
}

/** True for answers that mean "this will not work for this user", so retrying is pointless. */
export function isPermissionError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "permission_denied";
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.code === "not_found";
}
