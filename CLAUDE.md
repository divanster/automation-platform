# CLAUDE.md

Working rules for AI-assisted development in this repository.

## Project state

Architecture is **planned, not implemented**, apart from the Phase 2 backend
skeleton (settings, `accounts.User`, health endpoints; see
`docs/development/runbook.md`) and a database-independent request-intake
preparation slice (plain-Python schema, validation, deterministic test extractor,
canonical serialization and content hashing;
see `docs/development/request-intake-slice.md`). Approvals, exports, background
execution, persistence, and real AI adapters are not implemented. See `README.md` and
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
