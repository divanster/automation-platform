// Pure helpers of the managed credential panel (no React): which references to list, the checks that mirror the server's, and
// the fixed text for every answer.
//
// NOT AUTHORITATIVE. The checks are hints so an obvious mistake is shown before anything is sent; the backend validates again.
//
// SECRETS. Nothing here receives, keeps or returns a secret VALUE except `checkSecret`, which looks at one and returns only a fixed
// code. No message is built from a value, and an error answer is described by its stable code only.

import { ApiError, NETWORK_ERROR } from "../api/http";
import type { AiConnection, AiCredentialStatus, CredentialStatusValue } from "../api/types";
import { describeError } from "./errors";
import { isCredentialRef } from "./aiSettingsForm";

/** Mirrors the backend's MAX_SECRET_LENGTH (apps/secret_store/values.py). */
export const MAX_SECRET_LENGTH = 4096;

export type CredentialAction = "create" | "replace" | "delete";

/** The distinct references of the SAVED connections that read a managed credential, in list order. */
export function managedReferences(connections: AiConnection[]): string[] {
  const seen: string[] = [];
  for (const connection of connections) {
    if (connection.credential_source === "managed" && connection.credential_ref && !seen.includes(connection.credential_ref)) seen.push(connection.credential_ref);
  }
  return seen;
}

/** The identifiers of the saved connections that read `reference` as a managed credential. */
export function connectionsReading(connections: AiConnection[], reference: string): string[] {
  return connections.filter((c) => c.credential_source === "managed" && c.credential_ref === reference).map((c) => c.connection_id);
}

export function referenceProblem(reference: string, prefix: string): string | null {
  const value = reference.trim();
  if (value === "") return "required";
  return isCredentialRef(value, prefix) ? null : "invalid_reference";
}

/** A fixed code when `value` would be refused, else null. Never repeats the value. */
export function checkSecret(value: string): string | null {
  if (value === "") return "required";
  if (value.length > MAX_SECRET_LENGTH) return "too_long";
  if (value !== value.trim()) return "invalid_secret";
  return null;
}

const FIELD_TEXT: Record<string, string> = {
  required: "This is required.",
  blank: "This is required.",
  null: "This is required.",
  invalid_reference: "The reference must be the prefix shown followed by 1 to 48 capital letters, digits or underscores.",
  invalid_type: "This value is not valid.",
  invalid_secret: "This secret cannot be stored. It must be printable text with no space or line break at the start or end.",
  too_long: `The secret is too long (at most ${MAX_SECRET_LENGTH} characters).`,
  max_length: "This value is too long.",
  unknown_field: "This field is not recognised.",
};

export function describeCredentialField(codes: string[] | undefined): string | null {
  if (!codes || codes.length === 0) return null;
  return codes.map((code) => FIELD_TEXT[code] ?? "This value is not accepted.").join(" ");
}

const STATUS_LABEL: Record<CredentialStatusValue, { label: string; tone: "ok" | "warn" | "danger" }> = {
  set: { label: "Stored", tone: "ok" },
  not_set: { label: "Not stored", tone: "warn" },
  unreadable: { label: "Stored, but unreadable by the server", tone: "danger" },
};

export function describeCredentialStatus(status: AiCredentialStatus): { label: string; tone: "ok" | "warn" | "danger"; detail: string | null } {
  const entry = STATUS_LABEL[status.status];
  const detail =
    status.status === "unreadable"
      ? "A value exists, but this server process cannot decrypt it (for example it was written by another Windows account). Replace it to store a new one."
      : null;
  return { ...entry, detail };
}

const DISPLAYED_ID = /^[a-z][a-z0-9_-]{0,47}$/;

/** The connection identifiers a 409 `credential_in_use` carries (only well-formed identifiers are kept). */
export function connectionsFromInUse(error: ApiError): string[] {
  const raw = error.details?.connections;
  return Array.isArray(raw) ? raw.filter((c): c is string => typeof c === "string" && DISPLAYED_ID.test(c)) : [];
}

/** User-facing text for a failed credential operation: fixed sentences chosen by the stable code, never the value or the server's message. */
export function describeCredentialError(error: unknown, action: CredentialAction | "status"): string {
  if (!(error instanceof ApiError)) return "Something went wrong. The credential was not changed.";
  switch (error.code) {
    case NETWORK_ERROR:
      return action === "status"
        ? "The server could not be reached, so the status is unknown."
        : "The server could not be reached. The change may or may not have been applied: check the status before trying again.";
    case "permission_denied":
      return "You do not have permission to manage credentials. Nothing was changed.";
    case "validation_error":
      return "The reference or the secret was not accepted, so nothing was changed. The problems are shown beside the fields.";
    case "credential_not_found":
      return "No credential is stored under this reference.";
    case "credential_already_exists":
      return "A credential is already stored under this reference. It was not overwritten; use Replace to change it.";
    case "credential_in_use": {
      const names = connectionsFromInUse(error);
      return `A saved connection still reads this credential${names.length ? ` (${names.join(", ")})` : ""}, so it was not deleted. Switch that connection to another credential or to the environment, save the settings, then delete.`;
    }
    case "secret_store_disabled":
      return "The secret store is disabled on the server, so credentials cannot be managed. The operator must configure it (runbook section 5.11).";
    case "secret_store_unavailable":
      return action === "status" ? "The secret store is unavailable, so the status is unknown." : "The secret store is unavailable. Nothing was changed. Try again later or ask the operator.";
    case "secret_store_busy":
      return "The secret store is busy. Nothing was changed. Wait a moment, then try again.";
    case "secret_unreadable":
      return "The stored secret cannot be read by this server process. Replace it with a new value.";
    default:
      return describeError(error);
  }
}
