import { describe, expect, it } from "vitest";

import { SECRET_SENTINEL, catalog, connection, ollama, settings, withSelection } from "../test/aiSettings";
import {
  buildUpdate,
  connectionSource,
  connectionsChanged,
  describeSettingsCode,
  draftConnection,
  draftFromSettings,
  isCredentialRef,
  isDirty,
  parseConnectionPath,
  rebaseDraft,
  validateDraft,
} from "./aiSettingsForm";

describe("isCredentialRef", () => {
  it.each(["AP_AI_CRED_CORP", "AP_AI_CRED_A1_B2"])("accepts the variable name %s", (value) => expect(isCredentialRef(value, "AP_AI_CRED_")).toBe(true));

  it.each(["", "AP_AI_CRED_", "ap_ai_cred_corp", "DJANGO_SECRET_KEY", "AP_AI_CRED_lower", "AP_AI_CRED_ SPACE", SECRET_SENTINEL, `AP_AI_CRED_${"X".repeat(49)}`])(
    "refuses %j",
    (value) => expect(isCredentialRef(value, "AP_AI_CRED_")).toBe(false),
  );
});

describe("the draft and the update body", () => {
  it("round-trips the stored settings with nothing changed", () => {
    const data = withSelection();
    const draft = draftFromSettings(data);
    expect(draft.source).toBe(connectionSource("corp-llm"));
    expect(isDirty(draft, data)).toBe(false);
    expect(connectionsChanged(draft, data)).toBe(false);
  });

  it("builds a body without a connection list unless the list changed", () => {
    const data = withSelection();
    const draft = draftFromSettings(data);
    expect(buildUpdate(draft, data)).not.toHaveProperty("connections");
    const changed = { ...draft, connections: draft.connections.map((c, i) => (i === 0 ? { ...c, display_name: "Other" } : c)) };
    expect(buildUpdate(changed, data).connections?.[0]?.display_name).toBe("Other");
  });

  it("does not count the hidden AI fields of deterministic mode as a change", () => {
    const data = settings();
    const draft = { ...draftFromSettings(data), policy_mode: "approved_cloud", timeout: "99" };
    expect(isDirty(draft, data)).toBe(false);
  });

  it("trims what is typed and sends null for what a provider type does not take", () => {
    const data = settings();
    const draft = draftFromSettings(data);
    const added = { ...draftConnection(ollama()), display_name: "  Spaced  ", endpoint: " http://127.0.0.1:11434 ", credential_ref: "AP_AI_CRED_X" };
    const body = buildUpdate({ ...draft, connections: [added] }, data);
    expect(body.connections?.[0]).toMatchObject({ display_name: "Spaced", endpoint: "http://127.0.0.1:11434", credential_ref: null });
  });

  it("sends the timeout as an integer", () => {
    const data = withSelection();
    expect(buildUpdate({ ...draftFromSettings(data), timeout: " 30 " }, data).timeout_seconds).toBe(30);
  });

  it("keeps an untouched connection list in step with the latest after a conflict, and the user's own list otherwise", () => {
    const previous = withSelection();
    const latest = withSelection({ revision: 4, connections: [connection(), ollama(), connection({ connection_id: "extra", display_name: "Extra" })] });
    const untouched = rebaseDraft(draftFromSettings(previous), previous, latest);
    expect(untouched.connections.map((c) => c.connection_id)).toEqual(["corp-llm", "ollama-local", "extra"]);

    const mine = draftFromSettings(previous);
    const edited = { ...mine, connections: mine.connections.slice(0, 1) };
    expect(rebaseDraft(edited, previous, latest).connections.map((c) => c.connection_id)).toEqual(["corp-llm"]);
  });
});

describe("validateDraft", () => {
  const data = withSelection();
  const base = () => draftFromSettings(data);

  it("accepts the stored settings", () => {
    expect(validateDraft(base(), data.catalog)).toEqual({});
  });

  it("requires every AI field in AI mode, and none in deterministic mode", () => {
    const empty = { ...base(), source: "", profile_id: "", policy_mode: "", timeout: "" };
    expect(Object.keys(validateDraft(empty, data.catalog)).sort()).toEqual(["connection_id", "policy_mode", "timeout_seconds"]);
    expect(validateDraft({ ...empty, mode: "deterministic" }, data.catalog)).toEqual({});
  });

  it("refuses a selected connection that is disabled or does not exist", () => {
    const disabled = { ...base(), connections: base().connections.map((c) => ({ ...c, enabled: false })) };
    expect(validateDraft(disabled, data.catalog).connection_id).toEqual(["connection_disabled"]);
    expect(validateDraft({ ...base(), source: connectionSource("gone") }, data.catalog).connection_id).toEqual(["unknown_connection"]);
  });

  it("refuses a profile the selected connection does not offer", () => {
    expect(validateDraft({ ...base(), profile_id: "other" }, data.catalog).profile_id).toEqual(["unknown_profile"]);
  });

  it("checks the policy against the catalog's locality table, not a rule of its own", () => {
    expect(validateDraft({ ...base(), policy_mode: "local_only" }, data.catalog).policy_mode).toEqual(["policy_forbids_provider"]);
    const stricter = catalog({ locality_policies: [{ locality: "company_endpoint", policy_modes: ["approved_cloud"] }, { locality: "local", policy_modes: ["local_only"] }, { locality: "cloud", policy_modes: [] }] });
    expect(validateDraft({ ...base(), policy_mode: "company_endpoint" }, stricter).policy_mode).toEqual(["policy_forbids_provider"]);
    expect(validateDraft({ ...base(), policy_mode: "bogus" }, data.catalog).policy_mode).toEqual(["invalid_choice"]);
  });

  it("checks the connection's fields with the server's field paths", () => {
    const draft = base();
    const broken = { ...draft, connections: [{ ...draft.connections[0]!, connection_id: "Bad Id", endpoint: "", profiles: [{ profile_id: "p", model_id: "has space" }] }, draft.connections[1]!] };
    const problems = validateDraft(broken, data.catalog);
    expect(problems["connections[0].connection_id"]).toEqual(["invalid_value"]);
    expect(problems["connections[0].endpoint"]).toEqual(["required"]);
    expect(problems["connections[0].profiles[0].model_id"]).toEqual(["invalid_value"]);
    expect(problems["connections[1].endpoint"]).toBeUndefined();
  });

  it("refuses more than ten connections", () => {
    const draft = base();
    const many = Array.from({ length: 11 }, (_, i) => ({ ...draft.connections[0]!, key: `k${i}`, connection_id: `c${i}` }));
    expect(validateDraft({ ...draft, connections: many }, data.catalog).connections).toEqual(["too_many"]);
  });
});

describe("text", () => {
  it("never repeats a value, only a fixed sentence for a code", () => {
    expect(describeSettingsCode("policy_forbids_provider")).toBe("This data policy does not permit the selected connection's locality.");
    expect(describeSettingsCode("something_new")).toBe("This value is not accepted (something_new).");
  });

  it("reads the connection a server path belongs to", () => {
    expect(parseConnectionPath("connections[2].profiles[0].model_id")).toEqual({ index: 2, field: "profiles[0].model_id" });
    expect(parseConnectionPath("connections[0]")).toEqual({ index: 0, field: "" });
    expect(parseConnectionPath("timeout_seconds")).toBeNull();
  });
});
