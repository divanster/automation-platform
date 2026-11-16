// TEST ONLY: synthetic data. Every value is made up for the tests and exists nowhere in the running application.

import type { Profile, RequestDetail, RequestListItem, Page } from "../api/types";

export const RUN_ID = "11111111-1111-4111-8111-111111111111";
export const OTHER_ID = "22222222-2222-4222-8222-222222222222";
export const USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

export function operator(overrides: Partial<Profile> = {}): Profile {
  return {
    public_id: USER_ID,
    username: "synthetic.operator",
    is_superuser: false,
    roles: ["Operator"],
    permissions: ["intake.submit_request", "intake.view_own_request", "intake.edit_own_request"],
    ...overrides,
  };
}

export function listItem(overrides: Partial<RequestListItem> = {}): RequestListItem {
  return {
    run_id: RUN_ID,
    case_id: "33333333-3333-4333-8333-333333333333",
    reference: "REQ-20260101-AAAA0001",
    state: "needs_review",
    failed_step: "",
    created_at: "2026-01-01T10:00:00Z",
    current_revision_number: 1,
    outcome_status: "valid",
    submitted_by: USER_ID,
    ...overrides,
  };
}

export function page(results: RequestListItem[], overrides: Partial<Page<RequestListItem>> = {}): Page<RequestListItem> {
  return { count: results.length, page: 1, page_size: 20, results, ...overrides };
}

export function detail(overrides: Partial<RequestDetail> = {}): RequestDetail {
  return {
    run_id: RUN_ID,
    case_id: "33333333-3333-4333-8333-333333333333",
    reference: "REQ-20260101-AAAA0001",
    state: "needs_review",
    failed_step: "",
    created_at: "2026-01-01T10:00:00Z",
    submitted_by: USER_ID,
    processing: { step: "extract", task_state: "succeeded", attempt_count: 1, failure_code: null },
    outcome_status: "valid",
    current_revision: { id: "44444444-4444-4444-8444-444444444444", number: 1, content_hash: HASH, created_at: "2026-01-01T10:00:05Z" },
    draft: {
      requester: { name: "Synthetic Requester", contact: null },
      items: [{ description: "Synthetic widget", quantity: "2.50", unit: "pcs", notes: null }],
      notes: null,
      source_refs: [{ field_path: "requester.name", excerpt: "Requester: Synthetic Requester", line: 1 }],
    },
    draft_source: "revision",
    findings: [],
    warnings: [],
    approval_count: 0,
    rejection_count: 0,
    allowed_actions: [],
    action_blockers: {},
    ...overrides,
  };
}
