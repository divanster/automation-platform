# Architecture

> **Status: Architecture is planned, not implemented, except for a minimal
> backend skeleton** (Django project, `accounts.User`, health endpoints; see the
> [development runbook](../development/runbook.md)) and early slices listed in
> [overview.md](overview.md) (including a generic queue with a test handler; its PostgreSQL tests passed in the
> owner's local run, for the tested scenarios only) and a first orchestration slice that runs a text request
> through the worker and the deterministic extractor to `needs_review` (its PostgreSQL tests and a smoke test
> passed in the owner's local run, tested scenarios only;
> [slice document](../development/request-intake-orchestration-slice.md)) and a review-services slice (user identity,
> roles, authorized edit, approve and reject services, audit events; PostgreSQL tests and a smoke test passed in the
> owner's local run, tested scenarios only; [slice document](../development/review-services-slice.md)) and a REST API slice (session
> login with CSRF protection and throttling, request endpoints over those services; DB-free tests pass, 96 of the first 98 PostgreSQL
> tests passed in the owner's local run and 2 failed on test defects now corrected, rerun pending; [slice document](../development/rest-api-slice.md)). Export, a React UI, password reset,
> the installer does not exist; the AI gateway is a standalone foundation with one provider adapter (KPMG Workbench; the owner reports one local GPT-5 smoke test), and Request Intake can extract through it only when an operator explicitly enables `ai` mode (AI-2a: off by default, no automatic fallback, DB-free tests and a mocked transport in the VM, 35 PostgreSQL tests and a 736-test regression passed in the owner's local run, tested scenarios only, never run end to end against the Workbench; [ai-gateway.md](ai-gateway.md), section 15). Everything below is a design unless an Accepted ADR says otherwise.

This directory contains the initial architecture specification and
implementation plan for the Automation Platform.

## Reading order

1. [overview.md](overview.md): scope, non-goals, components, boundaries, trust
   boundaries, planned versus implemented status.
2. [first-workflow.md](first-workflow.md): the first vertical slice (request
   document to approved structured draft), its state machine, and acceptance
   criteria.
3. [data-model.md](data-model.md): entities, relationships, revisions, hashes,
   versioning, provenance.
4. [background-execution.md](background-execution.md): PostgreSQL-backed queue
   and persistent worker design.
5. [ai-policy.md](ai-policy.md): AI gateway, data-policy modes, and safeguards.
   [ai-gateway.md](ai-gateway.md): the AI-1a gateway foundation and the AI-1b KPMG Workbench provider, as implemented.
6. [deployment.md](deployment.md): development, standalone, and future hub
   deployment; operations.
7. [implementation-plan.md](implementation-plan.md): phases 0 to 9, each with
   scope, tests, and a stop-and-report gate.
8. [open-questions.md](open-questions.md): unresolved decisions, grouped by when
   they are needed.
9. [module-contract.md](module-contract.md): the module manifest, versioning, the static
   registry (MOD-1a, a foundation with no registered module), the persistent module state, sync and
   enable/disable and the worker gate (MOD-1b / S4, pushed; owner-run PostgreSQL validation passed for the tested scenarios), trust boundaries, and the
   future lifecycle and update-manager requirements.
10. [document-storage.md](document-storage.md): DOC-1, secure PDF upload, managed storage, the document API, its authorization and audit
    (implemented on a branch, DB-free validated in the VM only; PostgreSQL tests written and not run; no AI, OCR, extraction or UI).

What currently exists and how to run it: [../development/runbook.md](../development/runbook.md).

Decision records are in [../decisions/README.md](../decisions/README.md),
including the [technology baseline](../decisions/0007-technology-baseline.md).
Accepted ADRs record owner-confirmed decisions; they do not mean anything is
implemented.

## Summary of proposed boundaries

| Component | Responsibility |
| --- | --- |
| Frontend | React/TypeScript user interface for uploads, review, and approvals. |
| Django API | Authentication, authorization, use-case orchestration, and the public API over the shared domain layer. |
| Persistent background worker | Separate process that runs retryable tasks (extraction, export) using the same domain code. |
| PostgreSQL | System of record for business records, workflow state, tasks, approvals, configuration, and audit events. |
| Managed document storage | Controlled file storage outside the source tree, referenced by key and hash (DOC-1 implements the backend: [document-storage.md](document-storage.md)). |
| AI gateway | Provider-neutral entry point for model calls; enforces the company data policy with no silent cloud fallback. |
| Approvals and audit | Human approvals tied to specific result revisions, and an append-only event history. |
| Optional future control portal | Possible later portal; never required for local operation. |

## First vertical slice

**document upload -> extraction -> validation -> human review and approval -> export**

Specified in [first-workflow.md](first-workflow.md). It uses a deterministic test
AI adapter first and performs no consequential external actions.
