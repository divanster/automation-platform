// User-facing text for the AI connection test. Fixed sentences chosen by the stable code: the server's message text, the provider's words, an endpoint, a credential
// reference and anything the user typed are never echoed.

import { ApiError, CSRF_FAILED, NETWORK_ERROR } from "../api/http";
import type { AiConnectionTestResult } from "../api/types";
import { describeError, fieldCodesFrom } from "./errors";

/** Said beside every test, and again with every result. A passed test is about the web process only. */
export const WEB_PROCESS_NOTE = "Connection test checks the Django web process. The worker may use a different environment or Windows account.";
export const NOT_THE_WORKER = "This result is for the Django web process only. It does not show that the worker can use this connection.";

const GATEWAY_CODE_TEXT: Record<string, string> = {
  provider_unavailable: "The provider could not be reached or is not available.",
  policy_violation: "The saved data policy does not allow this connection, so nothing was sent.",
  timeout: "The provider did not answer in time.",
  invalid_structured_output: "The provider answered, but not with the expected reply.",
  rate_limit: "The provider is limiting requests.",
  authentication_failure: "The provider did not accept the credential.",
  unsupported_model: "The provider does not offer this model.",
  configuration_error: "The connection is not configured correctly for this process.",
  invalid_request: "The provider did not accept the request.",
  capability_missing: "The provider or model lacks a capability the test needs.",
  provider_error: "The provider reported an error.",
};

export interface TestOutcomeView {
  ok: boolean;
  title: string;
  /** The stable code, shown as text, or null on success. */
  code: string | null;
  detail: string | null;
  duration: string;
  retryable: boolean;
}

/** "412 ms" (the contract's unit is milliseconds, so it is shown as such). */
export function formatDuration(ms: number): string {
  return `${ms.toLocaleString("en-GB")} ms`;
}

export function describeTestResult(result: AiConnectionTestResult): TestOutcomeView {
  if (result.ok) {
    return { ok: true, title: "Test passed", code: null, detail: "The connection answered the fixed test request with a valid reply.", duration: formatDuration(result.duration_ms), retryable: false };
  }
  const code = result.code ?? "unknown";
  return {
    ok: false,
    title: "Test failed",
    code,
    detail: GATEWAY_CODE_TEXT[code] ?? "The test did not succeed.",
    duration: formatDuration(result.duration_ms),
    retryable: result.retryable,
  };
}

const NOT_USABLE_ISSUE: Record<string, string> = {
  missing_api_key: "The web process has no credential for this connection (the environment variable is not set, or the managed credential is not stored).",
  endpoint_not_approved: "The endpoint is not in the operator's approved list for this provider type.",
  invalid_configuration: "The connection's saved configuration cannot be used by the web process.",
};

/** Fixed text for a failed test request. Whatever the answer, nothing was changed and the unsaved edits on the page are untouched. */
export function describeTestError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. The test did not complete.";
  switch (error.code) {
    case NETWORK_ERROR:
      return "The server could not be reached. The test may or may not have run. Try again yourself when the server is back.";
    case "validation_error": {
      const codes = fieldCodesFrom(error).profile_id ?? [];
      return codes.includes("unknown_profile")
        ? "The connection has no saved model profile with this name. Save your changes, then choose one of its saved profiles."
        : "The test request was not accepted, so nothing was sent.";
    }
    case "permission_denied":
      return "You do not have permission to test connections. Nothing was sent.";
    case CSRF_FAILED:
      return "The security check failed. Reload the page and try again.";
    case "not_found":
      return "This connection is not among the saved connections (it may have been removed). Save the settings or reload the page.";
    case "connection_disabled":
      return "This connection is saved as disabled and cannot be tested. Enable it and save the settings first.";
    case "connection_not_usable": {
      const issue = error.details?.issue;
      const reason = typeof issue === "string" ? NOT_USABLE_ISSUE[issue] : undefined;
      return `The web process cannot use this connection, so nothing was sent. ${reason ?? NOT_USABLE_ISSUE.invalid_configuration}`;
    }
    case "connection_test_busy":
      return "Other connection tests are still running on the server, so nothing was sent. Wait a moment, then try again.";
    case "throttled":
      return error.retryAfterSeconds
        ? `Too many connection tests. Nothing was sent. Try again in ${error.retryAfterSeconds} seconds.`
        : "Too many connection tests. Nothing was sent. Try again in a moment.";
    default:
      return describeError(error);
  }
}
