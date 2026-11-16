import { describe, expect, it } from "vitest";

import { apiError, installFakeApi, json } from "../test/fakeApi";
import { HASH, RUN_ID } from "../test/fixtures";
import { REV1, auditPage, warning } from "../test/review";
import { REJECTION_REASONS } from "./types";
import { approveRequest, editRequest, listAudit, rejectRequest } from "./review";

const draft = {
  requester: { name: "n", contact: null },
  items: [{ description: "d", quantity: "2.50", unit: null, notes: null }],
  notes: null,
  source_refs: [{ field_path: "requester.name", excerpt: "x", line: 1 }],
};

const csrf = (call: { path: string }) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "tok" }) : undefined);

describe("editRequest", () => {
  it("posts base_revision_id and the draft, with the CSRF token, and nothing else", async () => {
    const api = installFakeApi((call) => csrf(call) ?? json(200, { revision_id: "r", revision_number: 2, content_hash: HASH, run_state: "needs_review", warnings: [], approval_invalidated: false }));
    const result = await editRequest(RUN_ID, REV1, draft);
    const post = api.callsTo("POST", `/api/requests/${RUN_ID}/edit/`)[0]!;
    expect(post.headers["x-csrftoken"]).toBe("tok");
    expect(Object.keys(post.body as object).sort()).toEqual(["base_revision_id", "draft"]);
    expect(post.body).toEqual({ base_revision_id: REV1, draft });
    expect((post.body as { draft: typeof draft }).draft.items[0]?.quantity).toBe("2.50");
    expect(result.revision_number).toBe(2);
  });

  it("sends an explicit null base for the first edit of an invalid extraction", async () => {
    const api = installFakeApi((call) => csrf(call) ?? json(200, { revision_id: "r", revision_number: 1, content_hash: HASH, run_state: "needs_review", warnings: [], approval_invalidated: false }));
    await editRequest(RUN_ID, null, draft);
    expect(api.callsTo("POST", `/api/requests/${RUN_ID}/edit/`)[0]!.body).toHaveProperty("base_revision_id", null);
  });

  it("surfaces the 409 stale_base_revision error code", async () => {
    installFakeApi((call) => csrf(call) ?? apiError(409, "stale_base_revision"));
    await expect(editRequest(RUN_ID, REV1, draft)).rejects.toMatchObject({ status: 409, code: "stale_base_revision" });
  });

  it("rejects a malformed success body", async () => {
    installFakeApi((call) => csrf(call) ?? json(200, { nope: true }));
    await expect(editRequest(RUN_ID, REV1, draft)).rejects.toMatchObject({ code: "unexpected_response" });
  });
});

describe("approveRequest", () => {
  it("posts exactly revision_id, content_hash and the acknowledged warnings as {code, field_path}", async () => {
    const api = installFakeApi((call) => csrf(call) ?? json(200, { approval_id: "a", revision_id: REV1, content_hash: HASH, run_state: "approved", acknowledged_warnings: [] }));
    await approveRequest(RUN_ID, REV1, HASH, [warning("a", "x"), warning("b", "y")]);
    const post = api.callsTo("POST", `/api/requests/${RUN_ID}/approve/`)[0]!;
    expect(post.body).toEqual({
      revision_id: REV1,
      content_hash: HASH,
      acknowledged_warnings: [
        { code: "a", field_path: "x" },
        { code: "b", field_path: "y" },
      ],
    });
    expect(post.body).not.toHaveProperty("approver_id");
  });

  it("surfaces self_approval_forbidden and warnings_not_acknowledged codes", async () => {
    installFakeApi((call) => csrf(call) ?? apiError(403, "self_approval_forbidden"));
    await expect(approveRequest(RUN_ID, REV1, HASH, [])).rejects.toMatchObject({ status: 403, code: "self_approval_forbidden" });
    installFakeApi((call) => csrf(call) ?? apiError(400, "warnings_not_acknowledged", { missing: [{ code: "a", field_path: "x" }], unexpected: [] }));
    await expect(approveRequest(RUN_ID, REV1, HASH, [])).rejects.toMatchObject({ status: 400, code: "warnings_not_acknowledged" });
  });
});

describe("rejectRequest", () => {
  it("posts revision_id, content_hash and a reason code only", async () => {
    const api = installFakeApi((call) => csrf(call) ?? json(200, { rejection_id: "j", revision_id: REV1, reason_code: "other", run_state: "needs_review" }));
    await rejectRequest(RUN_ID, REV1, HASH, "other");
    expect(api.callsTo("POST", `/api/requests/${RUN_ID}/reject/`)[0]!.body).toEqual({ revision_id: REV1, content_hash: HASH, reason_code: "other" });
  });

  it("offers exactly the API's four reason codes", () => {
    expect([...REJECTION_REASONS]).toEqual(["incorrect_content", "incomplete_content", "not_supported_by_source", "other"]);
  });
});

describe("listAudit", () => {
  it("reads one page of the per-request audit history", async () => {
    const api = installFakeApi(() => json(200, auditPage([])));
    await listAudit(RUN_ID, 2, 20);
    const call = api.calls[0]!;
    expect(call.method).toBe("GET");
    expect(call.path).toBe(`/api/requests/${RUN_ID}/audit/`);
    expect(Object.fromEntries(call.query)).toEqual({ page: "2", page_size: "20" });
  });

  it("surfaces 403 permission_denied", async () => {
    installFakeApi(() => apiError(403, "permission_denied"));
    await expect(listAudit(RUN_ID, 1, 20)).rejects.toMatchObject({ code: "permission_denied" });
  });
});
