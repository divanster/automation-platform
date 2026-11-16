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
};

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
