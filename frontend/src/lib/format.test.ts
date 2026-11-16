import { describe, expect, it, vi } from "vitest";

import { RUN_STATES } from "../api/types";
import { formatDateTime, isProcessingState, isRunState, isUuid, newClientRequestId, shortId, stateLabel } from "./format";

describe("states", () => {
  it("polls only while the system is working", () => {
    const processing = RUN_STATES.filter(isProcessingState);
    expect(processing).toEqual(["queued", "extracting", "exporting"]);
  });

  it("labels every known state and passes an unknown one through", () => {
    for (const state of RUN_STATES) expect(stateLabel(state)).not.toBe(state);
    expect(stateLabel("future_state")).toBe("future_state");
    expect(isRunState("approved")).toBe(true);
    expect(isRunState("nope")).toBe(false);
    expect(isRunState(null)).toBe(false);
  });
});

describe("helpers", () => {
  it("recognises UUIDs and rejects path tricks", () => {
    expect(isUuid("11111111-1111-4111-8111-111111111111")).toBe(true);
    expect(isUuid("11111111-1111-4111-8111-11111111111")).toBe(false);
    expect(isUuid("../admin")).toBe(false);
    expect(isUuid("11111111-1111-4111-8111-111111111111/../x")).toBe(false);
  });

  it("shortens an id to its first group", () => {
    expect(shortId("22222222-2222-4222-8222-222222222222")).toBe("22222222");
  });

  it("returns an unparseable date as it is", () => {
    expect(formatDateTime("not a date")).toBe("not a date");
    expect(formatDateTime("2026-01-01T10:00:00Z")).not.toBe("2026-01-01T10:00:00Z");
  });

  it("makes v4 UUIDs, also where crypto.randomUUID is unavailable (insecure context)", () => {
    const v4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(newClientRequestId()).toMatch(v4);
    const original = crypto.randomUUID;
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    try {
      const a = newClientRequestId();
      expect(a).toMatch(v4);
      expect(newClientRequestId()).not.toBe(a);
    } finally {
      vi.unstubAllGlobals();
      expect(crypto.randomUUID).toBe(original);
    }
  });
});
