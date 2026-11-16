import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { describeError, isNotFound, isPermissionError } from "./errors";

const err = (code: string, details?: Record<string, unknown>, retry?: number, message = "server text") =>
  new ApiError(400, code, message, details, retry);

describe("describeError: the stable codes of the API error contract", () => {
  const known: Array<[string, RegExp]> = [
    ["network_error", /could not be reached/i],
    ["unexpected_response", /does not understand/i],
    ["not_authenticated", /not signed in/i],
    ["csrf_failed", /security check/i],
    ["permission_denied", /do not have permission/i],
    ["invalid_credentials", /username or password/i],
    ["not_found", /does not exist|not allowed/i],
    ["idempotency_conflict", /conflicts/i],
    ["validation_error", /not valid/i],
    ["payload_too_large", /too large/i],
    ["internal_error", /internal error/i],
  ];
  it.each(known)("%s has its own message", (code, pattern) => {
    expect(describeError(err(code))).toMatch(pattern);
  });

  it("reports the wait for throttled answers, with and without a number", () => {
    expect(describeError(err("throttled", undefined, 30))).toBe("Too many attempts. Try again in 30 seconds.");
    expect(describeError(err("throttled"))).toMatch(/too many attempts/i);
  });

  it.each([
    ["blank", /enter the request text/i],
    ["too_large", /too large/i],
    ["contains_nul", /cannot be stored/i],
    ["unencodable", /cannot be stored/i],
    ["not_text", /not valid text/i],
  ])("invalid_request_text with reason %s", (reason, pattern) => {
    expect(describeError(err("invalid_request_text", { reason }))).toMatch(pattern);
  });

  it("has a generic message for invalid_request_text with an unknown reason", () => {
    expect(describeError(err("invalid_request_text", { reason: "new_reason" }))).toMatch(/not acceptable/i);
  });

  it("shows only the code of an unlisted error, never the server's message", () => {
    const text = describeError(err("some_new_code", { value: "user typed this" }, undefined, "echoes user typed this"));
    expect(text).toContain("some_new_code");
    expect(text).not.toContain("user typed this");
  });

  it("does not echo the server message for listed codes either", () => {
    expect(describeError(err("permission_denied", undefined, undefined, "SERVER WORDING"))).not.toContain("SERVER WORDING");
  });

  it("handles errors that are not ApiErrors", () => {
    expect(describeError(new Error("secret internals"))).toBe("Something went wrong.");
    expect(describeError("x")).toBe("Something went wrong.");
  });
});

describe("error predicates", () => {
  it("recognise permission and not-found answers only", () => {
    expect(isPermissionError(err("permission_denied"))).toBe(true);
    expect(isPermissionError(err("not_found"))).toBe(false);
    expect(isNotFound(err("not_found"))).toBe(true);
    expect(isNotFound(new Error("x"))).toBe(false);
  });
});
