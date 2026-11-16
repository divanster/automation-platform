import { RUN_STATES, type RunState } from "../api/types";

export const STATE_LABELS: Record<RunState, string> = {
  queued: "Queued",
  extracting: "Extracting",
  needs_review: "Needs review",
  approved: "Approved",
  exporting: "Exporting",
  exported: "Exported",
  failed: "Failed",
  cancelled: "Cancelled",
};

// States in which the system is still working and the page should keep refreshing. Every other state is stable until
// a person acts, so polling stops there.
const PROCESSING_STATES: ReadonlySet<string> = new Set(["queued", "extracting", "exporting"]);

export function isProcessingState(state: string): boolean {
  return PROCESSING_STATES.has(state);
}

export function isRunState(value: unknown): value is RunState {
  return typeof value === "string" && (RUN_STATES as readonly string[]).includes(value);
}

export function stateLabel(state: string): string {
  return isRunState(state) ? STATE_LABELS[state] : state;
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** The first group of a UUID: enough to tell users apart on screen without showing a whole identifier. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID.test(value);
}

/** A random UUID v4 for `client_request_id`. `crypto.randomUUID` needs a secure context; `getRandomValues` does not. */
export function newClientRequestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
