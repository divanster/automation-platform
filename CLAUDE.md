# CLAUDE.md

Working rules for AI-assisted development in this repository.

## Project state

Architecture is **planned, not implemented**, apart from the Phase 2 backend
skeleton (settings, `accounts.User`, health endpoints; see
`docs/development/runbook.md`) and a database-independent request-intake
preparation slice (plain-Python schema, validation, deterministic test extractor,
canonical serialization and content hashing, and pure revision, approval, and export-eligibility rules;
see `docs/development/request-intake-slice.md`), plus persistence for cases, runs, result
revisions, and approvals (`apps/cases`; see `docs/development/revision-approval-policy.md`;
its PostgreSQL tests passed in the owner's local run, see
`docs/development/local-validation-record.md`), plus a first persistent-worker slice (`apps/tasks`:
a generic PostgreSQL-backed queue, a `runworker` command, and one deterministic test handler; see
`docs/development/persistent-worker-slice.md`; its DB-free tests pass, and its PostgreSQL tests and a
manual smoke test passed in the owner's local run for the tested scenarios only, see
`docs/development/local-validation-record.md`), plus a first orchestration slice (`apps/intake`: a
text request becomes a case, a run, a stored source, and a queued task; the worker extracts with the
deterministic fixture parser, validates, canonicalizes, and records an `ExtractionOutcome` and, when
valid, revision 1, moving the run to `needs_review`, or to `failed` when the task fails for good; see
`docs/development/request-intake-orchestration-slice.md`; its DB-free tests pass, and its PostgreSQL tests and
a manual smoke test passed in the owner's local run for the tested scenarios only), plus a review-services slice
(`accounts.User.public_id`, Operator, Approver and Auditor groups set up by `manage.py setup_roles`, authorized
service functions in `apps.intake.review` to edit, approve and reject a request, an immutable `Rejection`, and
append-only audit events in `apps.audit`; see `docs/development/review-services-slice.md`; its DB-free tests
pass, and its 135 PostgreSQL tests, the user migration and a manual smoke test passed in the owner's local run for
the tested scenarios only; audit and record immutability are ORM-level only), plus a REST API slice (`apps/api`:
Django session login and logout with CSRF protection and bounded login throttling, `/api/auth/me/`, and endpoints
to submit, list, read, edit, approve, reject and audit requests, every one delegating to the services above; see
`docs/development/rest-api-slice.md`; its DB-free tests pass; in the owner's local run 96 of its first 98 PostgreSQL tests
passed and 2 failed because of test defects, since corrected (the 100 corrected tests have NOT been rerun); throttling is per process and TLS, HSTS and the HTTPS redirect are undecided), plus a first frontend slice
(`frontend/`: React, TypeScript and Vite, same-origin through a Vite dev proxy, session-cookie login with CSRF, a
request list, a new-request form, and a request detail page that polls while processing; UI-1 was run by the owner in a real
browser against the local stack, as reported, without recorded output), plus UI-2 review actions in the same frontend (draft
editing with findings beside fields and stale-revision handling, approval with warning acknowledgement, rejection with the
API's fixed reasons, and the per-request audit history; every control follows the server's `allowed_actions`, no business
rule is in React; see `docs/development/frontend-slice.md`, section 13; its 264 frontend tests, typecheck, production build and
the 1246 backend DB-free tests pass in the development VM against a stubbed `fetch`; the owner reports one UI-2 workflow
(submit, worker extraction, edit, approval by a second user, audit history) passing in a real browser against the local Django,
PostgreSQL and worker, with screenshots described to Claude and not stored, see `docs/development/local-validation-record.md`;
the stale-revision conflict and rejection were not shown there, so UI-2 is development-validated for that workflow only and not
production-ready; Django does not serve the built app), plus a module-contract foundation (MOD-1a: `backend/apps/modules`, a pure
standard-library package with manifest v1, a documented SemVer subset and an immutable, deterministic static registry, and
`backend/config/modules.py`, the composition root whose built-in tuple is EMPTY; see `docs/architecture/module-contract.md`;
its DB-free tests pass in the development VM; NO module is registered or activated, Request Intake is NOT yet a module, and there
is no module endpoint, signature, license enforcement or update manager; modules that are admitted to a process are not sandboxed),
plus MOD-1b slices S1a to S3 (pure permission and capability contracts, a generic authorization core, worker claim-filter and kind-gate
seams, and an extensible audit vocabulary; merged), plus MOD-1b / S4 (`apps/module_host`: `ModuleState`, an initialization marker and append-only namespace
claims, a repeatable `module_sync`, audited `module_enable`, `module_disable` and `module_status` administrator commands, and the
worker kind gate installed in the production composition; see `docs/architecture/module-contract.md` section 3.11; **committed locally on `feat/module-runtime-state`, not pushed,
awaiting the owner's PostgreSQL run: its DB-free tests pass in the development VM, its 73 PostgreSQL tests and its migration have NOT been run by
anybody**; the registry is still empty so nothing is gated, Request Intake's task kind is a legacy ungated kind, and there is no
enable/disable API). The source text in the UI, revision diffs, document upload, exports,
password reset, multi-factor authentication, an installer, and real AI adapters are not implemented. See `README.md` and
`docs/architecture/README.md`. Do not describe planned components as existing.

## Working rules

- Keep changes small and explicitly scoped.
- Inspect repository status (`git status`) before making changes.
- Never read or expose secrets (tokens, passwords, `.env` files, credential
  stores). Do not put secrets in prompts, commands, remote URLs, files, logs,
  or Git configuration.
- Do not install dependencies or create infrastructure unless requested.
- Do not force-push, reset destructively, or delete existing work.

## Product rules

- No customer-specific forks of the product. Differences between companies are
  handled by company configuration and workflow packages on the shared core.
- AI outputs require validation before they are trusted or acted on.
- Local-only mode must never silently fall back to a cloud provider.
- Significant external actions require human approval.

## Reporting

At the end of a task, report: changed files, checks run, the commit, and the
push status.
