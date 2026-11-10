# Automation Platform

A local-first business process automation platform.

> **Status: implementation has not started.** This repository currently contains
> only project documentation and repository configuration. Everything described
> below is planned, not built.

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

- Django (API)
- PostgreSQL
- Persistent background worker
- React / TypeScript (frontend)

See [docs/architecture/README.md](docs/architecture/README.md) for the proposed
component boundaries.

## Repository conventions

See [CLAUDE.md](CLAUDE.md) for working rules for AI-assisted development.
