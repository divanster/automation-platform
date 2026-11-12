# Architecture

> **Status: Architecture is planned, not implemented, except for a minimal
> backend skeleton** (Django project, `accounts.User`, health endpoints; see the
> [development runbook](../development/runbook.md)) and early slices listed in
> [overview.md](overview.md) (including a generic queue with a test handler whose
> PostgreSQL tests have **not been run**). The business workflow, any real
> background work, AI gateway, frontend, and installer do not exist. Everything below is a design unless an Accepted ADR says otherwise.

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
6. [deployment.md](deployment.md): development, standalone, and future hub
   deployment; operations.
7. [implementation-plan.md](implementation-plan.md): phases 0 to 9, each with
   scope, tests, and a stop-and-report gate.
8. [open-questions.md](open-questions.md): unresolved decisions, grouped by when
   they are needed.

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
| Managed document storage | Controlled file storage outside the source tree, referenced by key and hash. |
| AI gateway | Provider-neutral entry point for model calls; enforces the company data policy with no silent cloud fallback. |
| Approvals and audit | Human approvals tied to specific result revisions, and an append-only event history. |
| Optional future control portal | Possible later portal; never required for local operation. |

## First vertical slice

**document upload -> extraction -> validation -> human review and approval -> export**

Specified in [first-workflow.md](first-workflow.md). It uses a deterministic test
AI adapter first and performs no consequential external actions.
