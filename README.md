# Automation Platform

A local-first business process automation platform.

> **Status: implementation of the product has not started.** The repository
> contains documentation, repository configuration, and a minimal Django backend
> skeleton (project settings, a user model, and health endpoints), database-independent
> workflow rules, and minimal persistence for cases, runs, revisions, and approvals (its
> PostgreSQL tests passed in the owner's local run, for the tested scenarios only). The business workflow, authentication,
> background worker, AI adapters, frontend, and installer are **not** built. Everything else described below is planned, not built. See the
> [development runbook](docs/development/runbook.md) for exactly what exists.

## Purpose

Automate repeatable business processes (for example document handling) while
keeping company data under the company's control by default.

## Approach

- **One shared core.** A single product codebase serves every company.
- **Predefined workflow packages.** Common processes ship as reusable packages
  built on the shared core.
- **Company configuration.** Differences between companies are expressed as
  configuration, not as customer-specific forks.
- **Configurable AI providers.** Local or external AI providers can be selected
  per company. A local-only setting is respected and never silently falls back
  to a cloud provider.
- **Human approval.** Significant external actions require explicit human
  approval, and are recorded in an audit trail.

## Planned stack

- Django 5.2 LTS with Django REST Framework (API), on Python 3.14
- PostgreSQL 18 (native Windows install on the owner's local computer, not in the
  development VM; validated locally by the owner for the tested scenarios, see the
  [local validation record](docs/development/local-validation-record.md))
- Persistent background worker
- React / TypeScript (frontend)

Exact versions in use are recorded in the
[development runbook](docs/development/runbook.md); the reasoning is in the
[technology baseline](docs/decisions/0007-technology-baseline.md).

## Documentation

- [Architecture index](docs/architecture/README.md): proposed component
  boundaries and links to the detailed specification.
- [Architecture overview](docs/architecture/overview.md)
- [First workflow](docs/architecture/first-workflow.md)
- [Implementation plan](docs/architecture/implementation-plan.md)
- [Open questions](docs/architecture/open-questions.md)
- [Decision records](docs/decisions/README.md)
- [Development runbook](docs/development/runbook.md): setup, tests, and current status
- [Request-intake preparation slice](docs/development/request-intake-slice.md):
  the database-independent schema, validation, test extractor, and canonical content hashing
- [Local PostgreSQL validation record](docs/development/local-validation-record.md):
  what the owner ran locally, what is pasted output, and what remains open
- [Revision, approval, and export-eligibility policy](docs/development/revision-approval-policy.md):
  pure rules plus their persistence; no authentication or export

## Repository conventions

See [CLAUDE.md](CLAUDE.md) for working rules for AI-assisted development.
