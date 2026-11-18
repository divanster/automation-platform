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
worker kind gate installed in the production composition; see `docs/architecture/module-contract.md` section 3.11; merged into `main` (PR #15): its 1926 DB-free
tests pass in the development VM, and the owner ran its 75 PostgreSQL tests, the migration and a 567-test PostgreSQL regression locally (all passed; owner-supplied, tested
scenarios only; see `docs/development/local-validation-record.md`); the registry is still empty so nothing is gated, Request Intake is not a module (its task kind is a legacy
ungated kind), there is no enable/disable API and no API-side write blocking, no capability facade, no installer or update manager, and the gate has the known S2 race window), plus MOD-1b / S5 (merged, PR #16; workflow attribution: `WorkflowRun.module_id`, `workflow_key` and `workflow_version`, set once at creation and refused afterwards at the ORM level, with additive migrations
`cases.0006` to `0009` whose backfill attributes only runs that have a Request Intake source and stops with a count at any other; see `docs/architecture/module-contract.md` section 3.12; merged into `main` (PR #16, implementation `e2a4c55`): its 1998 DB-free tests passed in the
development VM, and the owner ran the migrations, saw four historical runs attributed, and ran its 37 PostgreSQL tests and a 604-test PostgreSQL regression locally, all passed; owner-supplied, tested scenarios only, see
`docs/development/local-validation-record.md`; Request Intake is still not a module and nothing reads the attribution), plus MOD-1b / S6 (the `ModuleContext` capability facade: `apps/module_host/context.py`, the pure `context_rules.py` and `config/module_context.py`; a registered module gets an immutable, registry-backed context whose
`tasks`, `cases`, `audit` and `permissions` facades delegate to the existing `enqueue_task`, case services, `record_event` and the generic Core access rule, only for declared capabilities and for the module's own kinds, workflows, audit actions and permissions, and, for
state-changing calls, only while the module is enabled and version-matched; actors are explicit; see `docs/architecture/module-contract.md` section 3.13; on `feat/module-capability-facade` (committed locally, not yet pushed, PR not opened): its 2129 DB-free tests pass in the development VM and its 72
PostgreSQL tests have NOT been run; no module is registered, Request Intake does not use it, nothing in the application calls it, `revision.approval` has no facade, and it is a naming and authority boundary, not a sandbox). The source text in the UI, revision diffs, document upload, exports,
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
