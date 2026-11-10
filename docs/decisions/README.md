# Architecture Decision Records

Architecture Decision Records (ADRs) capture significant decisions, the context
and alternatives behind them, and the conditions under which they should be
revisited.

An **Accepted** ADR records a decision confirmed by the project owner. It does
**not** mean the design is implemented or proven: implementation has not
started. Parts of an Accepted ADR that remain untested are listed explicitly
under its Status as qualifications or as "Proposed, unproven".

## Status values

- **Proposed:** drafted for review.
- **Accepted:** reviewed and adopted by the project owner.
- **Superseded:** replaced by a later ADR (which must be linked).
- **Rejected:** considered and declined.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-modular-monolith.md) | Modular monolith | Accepted |
| [0002](0002-postgresql-primary-storage.md) | PostgreSQL as primary storage | Accepted (PostgreSQL on client Windows machines unproven) |
| [0003](0003-persistent-background-worker.md) | Persistent background worker | Accepted for the separate worker; **PostgreSQL-backed queue Proposed and unproven** |
| [0004](0004-provider-neutral-ai-gateway.md) | Provider-neutral AI gateway | Accepted for the gateway, deterministic adapter first, and no silent cloud fallback; **policy-mode details Proposed** |
| [0005](0005-company-configuration-not-forks.md) | Company configuration, not forks | Accepted |
| [0006](0006-local-execution-independent-of-control-plane.md) | Local execution independent of a control plane | Accepted |
| [0007](0007-technology-baseline.md) | Technology baseline for the backend skeleton | Accepted; exact versions pinned and PostgreSQL 18 selected in Phase 2 (see its implementation record); PostgreSQL not yet provisioned |

## Conventions

- File name: `NNNN-short-title.md`, numbered sequentially, never reused.
- Sections: Status, Context, Decision, Alternatives, Consequences and
  trade-offs, Conditions for revisiting. ADR 0007 adds verification and
  open-items sections.
- Do not edit an accepted ADR's decision in place; add a new ADR that supersedes
  it and update the index.

Related: [architecture documents](../architecture/README.md).
