import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { SECRET_SENTINEL, connection, managedConnection } from "../test/aiSettings";
import {
  MAX_SECRET_LENGTH,
  checkSecret,
  connectionsFromInUse,
  connectionsReading,
  describeCredentialError,
  describeCredentialField,
  describeCredentialStatus,
  managedReferences,
  referenceProblem,
} from "./aiCredentials";

const PREFIX = "AP_AI_CRED_";

describe("managedReferences", () => {
  it("lists each reference read as a managed credential once, in order, and ignores environment connections", () => {
    const list = [
      connection({ connection_id: "env", credential_ref: "AP_AI_CRED_ENV" }),
      managedConnection({ connection_id: "a", credential_ref: "AP_AI_CRED_A" }),
      managedConnection({ connection_id: "b", credential_ref: "AP_AI_CRED_A" }),
      managedConnection({ connection_id: "c", credential_ref: null }),
      managedConnection({ connection_id: "d", credential_ref: "AP_AI_CRED_D" }),
    ];
    expect(managedReferences(list)).toEqual(["AP_AI_CRED_A", "AP_AI_CRED_D"]);
    expect(connectionsReading(list, "AP_AI_CRED_A")).toEqual(["a", "b"]);
    expect(connectionsReading(list, "AP_AI_CRED_ENV")).toEqual([]);
  });
});

describe("local checks", () => {
  it.each([
    ["AP_AI_CRED_OK", null],
    ["  AP_AI_CRED_OK  ", null],
    ["", "required"],
    ["MY_KEY", "invalid_reference"],
    ["AP_AI_CRED_lower", "invalid_reference"],
    ["AP_AI_CRED_", "invalid_reference"],
    [`AP_AI_CRED_${"A".repeat(49)}`, "invalid_reference"],
  ])("reference %j -> %s", (value, code) => {
    expect(referenceProblem(value, PREFIX)).toBe(code);
  });

  it.each([
    ["a-key", null],
    ["", "required"],
    [" leading", "invalid_secret"],
    ["trailing ", "invalid_secret"],
    ["line\n", "invalid_secret"],
    ["x".repeat(MAX_SECRET_LENGTH), null],
    ["x".repeat(MAX_SECRET_LENGTH + 1), "too_long"],
  ])("secret %j -> %s", (value, code) => {
    expect(checkSecret(value)).toBe(code);
  });

  it("never puts the checked value into a message", () => {
    const texts = [describeCredentialField([checkSecret(` ${SECRET_SENTINEL}`) ?? ""]), describeCredentialField([referenceProblem(SECRET_SENTINEL, PREFIX) ?? ""])];
    expect(texts.join(" ")).not.toContain(SECRET_SENTINEL);
  });
});

describe("describeCredentialStatus", () => {
  const status = (value: "set" | "not_set" | "unreadable") => ({ reference: "AP_AI_CRED_X", source: "managed" as const, status: value, store: "s", connections: [], apply: "worker_restart_required" });
  it("labels the three states", () => {
    expect(describeCredentialStatus(status("set")).label).toBe("Stored");
    expect(describeCredentialStatus(status("not_set")).label).toBe("Not stored");
    expect(describeCredentialStatus(status("unreadable")).tone).toBe("danger");
    expect(describeCredentialStatus(status("unreadable")).detail).toMatch(/cannot decrypt/);
    expect(describeCredentialStatus(status("set")).detail).toBeNull();
  });
});

describe("describeCredentialError", () => {
  const err = (status: number, code: string, details?: Record<string, unknown>) => new ApiError(status, code, SECRET_SENTINEL, details);

  it.each([
    ["permission_denied", /do not have permission/],
    ["credential_not_found", /No credential is stored/],
    ["credential_already_exists", /not overwritten/],
    ["secret_store_disabled", /disabled on the server/],
    ["secret_store_unavailable", /unavailable/],
    ["secret_store_busy", /busy/],
    ["secret_unreadable", /cannot be read/],
    ["validation_error", /not accepted/],
    ["network_error", /could not be reached/],
  ])("describes %s with a fixed sentence and never the server's message", (code, pattern) => {
    const text = describeCredentialError(err(400, code), "create");
    expect(text).toMatch(pattern);
    expect(text).not.toContain(SECRET_SENTINEL);
  });

  it("names the connections of a credential_in_use answer, keeping only well-formed identifiers", () => {
    const error = err(409, "credential_in_use", { connections: ["corp-llm", "Bad Id!", 5, SECRET_SENTINEL] });
    expect(connectionsFromInUse(error)).toEqual(["corp-llm"]);
    expect(describeCredentialError(error, "delete")).toContain("(corp-llm)");
    expect(describeCredentialError(error, "delete")).not.toContain(SECRET_SENTINEL);
  });

  it("says the outcome of a failed change is unknown, but that a status check is only unknown", () => {
    expect(describeCredentialError(new ApiError(0, "network_error", ""), "replace")).toMatch(/may or may not have been applied/);
    expect(describeCredentialError(new ApiError(0, "network_error", ""), "status")).toMatch(/status is unknown/);
  });

  it("falls back to the code only for an unlisted answer", () => {
    expect(describeCredentialError(err(418, "teapot"), "create")).toBe("The request could not be completed (teapot).");
  });
});
