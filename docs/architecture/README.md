# Architecture (proposed)

> **Status: planned, not implemented.** These are initial proposed boundaries
> and may change before implementation.

## Proposed boundaries

| Component | Responsibility |
| --- | --- |
| Frontend | React/TypeScript user interface for uploads, review, and approvals. |
| Django API | Business logic, authentication, workflow orchestration, and the public API. |
| Persistent background worker | Runs long-lived and retryable jobs (extraction, validation, export) outside request handling. |
| PostgreSQL | System of record for configuration, workflow state, and audit data. |
| Managed document storage | Controlled storage for uploaded and generated documents. |
| AI gateway | Single entry point for AI calls; routes to configured local or external providers and enforces local-only mode with no silent cloud fallback. |
| Approvals and audit | Human approval gates before significant external actions, with an auditable record of decisions and AI outputs. |
| Optional future control portal | Possible later portal for managing deployments; not part of the initial scope. |

## First vertical slice

The first end-to-end slice will be:

**document upload -> extraction -> validation -> approval -> export**

It is intended to exercise every core boundary above (except the optional
control portal) with the smallest useful workflow.

## Out of scope for now

No Django project, React app, Docker setup, PostgreSQL services, virtual
environments, or dependency files exist yet.
