import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { testResult } from "../test/aiSettings";
import { NOT_THE_WORKER, WEB_PROCESS_NOTE, describeTestError, describeTestResult, formatDuration } from "./aiConnectionTest";

describe("the fixed notices", () => {
  it("say the test is about the web process and does not prove the worker", () => {
    expect(WEB_PROCESS_NOTE).toBe("Connection test checks the Django web process. The worker may use a different environment or Windows account.");
    expect(NOT_THE_WORKER).toMatch(/does not show that the worker can use/);
  });
});

describe("describeTestResult", () => {
  it("describes a pass with its duration in milliseconds and no code", () => {
    const view = describeTestResult(testResult({ duration_ms: 1234 }));
    expect(view).toMatchObject({ ok: true, title: "Test passed", code: null, duration: "1,234 ms", retryable: false });
  });

  it.each([
    "provider_unavailable",
    "policy_violation",
    "timeout",
    "invalid_structured_output",
    "rate_limit",
    "authentication_failure",
    "unsupported_model",
    "configuration_error",
    "invalid_request",
    "capability_missing",
    "provider_error",
  ])("describes the failure %s with its code and a fixed sentence", (code) => {
    const view = describeTestResult(testResult({ ok: false, code, retryable: true, duration_ms: 7 }));
    expect(view).toMatchObject({ ok: false, title: "Test failed", code, duration: "7 ms", retryable: true });
    expect(view.detail).toBeTruthy();
    expect(view.detail).not.toContain(code);
  });

  it("describes an unrecognised code generically but still shows the sanitized code", () => {
    expect(describeTestResult(testResult({ ok: false, code: "brand_new_code" })).detail).toBe("The test did not succeed.");
  });

  it("formats zero", () => {
    expect(formatDuration(0)).toBe("0 ms");
  });
});

const err = (status: number, code: string, details?: Record<string, unknown>, retry?: number) => new ApiError(status, code, "SERVER MESSAGE MUST NOT SHOW", details, retry);

describe("describeTestError", () => {
  it.each([
    [err(0, "network_error"), /could not be reached.*may or may not have run/],
    [err(400, "validation_error", { fields: { profile_id: ["unknown_profile"] } }), /no saved model profile/],
    [err(400, "validation_error", { fields: { profile_id: ["unknown_field"] } }), /not accepted/],
    [err(403, "permission_denied"), /permission to test connections/],
    [err(403, "csrf_failed"), /security check failed/],
    [err(404, "not_found"), /not among the saved connections/],
    [err(409, "connection_disabled"), /saved as disabled/],
    [err(409, "connection_not_usable", { issue: "missing_api_key" }), /no credential for this connection/],
    [err(409, "connection_not_usable", { issue: "endpoint_not_approved" }), /approved list/],
    [err(409, "connection_not_usable", { issue: "invalid_configuration" }), /cannot use this connection/],
    [err(409, "connection_not_usable", { issue: "surprise" }), /cannot use this connection/],
    [err(409, "connection_not_usable"), /nothing was sent/],
    [err(429, "connection_test_busy"), /still running on the server/],
    [err(429, "throttled", undefined, 30), /Try again in 30 seconds/],
    [err(429, "throttled"), /Try again in a moment/],
    [err(500, "internal_error"), /internal error/],
    [err(418, "something_else"), /something_else/],
  ])("%#: gives a fixed sentence", (error, pattern) => {
    const text = describeTestError(error);
    expect(text).toMatch(pattern);
    expect(text).not.toContain("SERVER MESSAGE");
  });

  it("does not echo an unexpected issue value", () => {
    expect(describeTestError(err(409, "connection_not_usable", { issue: "https://secret.example/key" }))).not.toContain("secret.example");
  });

  it("handles a failure that is not an API error", () => {
    expect(describeTestError(null)).toBe("Something went wrong. The test did not complete.");
  });
});
