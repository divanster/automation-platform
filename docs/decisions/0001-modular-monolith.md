# ADR 0001: Modular monolith

## Status

Accepted. This follows the owner's design constraints (modular Django backend,
no microservices at this stage).

Qualification: that module boundaries can be enforced in practice (by tests or
lint rules) is **unproven** until Phase 2.

## Context

The product is a local-first system installed on Windows machines, initially one
installation per company, possibly on a single standalone machine. The team is
small, the domain is still being learned, and every additional deployable unit
adds installation, monitoring, and update burden on client machines.

## Decision

Build the backend as a **modular monolith**: one codebase and one deployable
backend, organized into modules with explicit, enforced dependency directions
(see [overview.md](../architecture/overview.md)). The API process and the
background worker are two process entry points of the same codebase and share
domain logic. Do not introduce microservices at this stage.

## Alternatives

- **Microservices.** Independent scaling and deployment, but a heavy operational
  and installation cost, distributed failure modes, and no demonstrated need.
- **Unstructured monolith.** Simpler at first, but module boundaries erode and
  later extraction becomes expensive.
- **Separate worker codebase.** Duplicates domain rules and risks divergence.

## Consequences and trade-offs

- Simple installation and one version to update.
- Module boundaries depend on discipline and automated checks, not on process
  isolation.
- A defect in shared code affects both API and worker.
- Scaling is limited to running more processes of the same codebase.

## Conditions for revisiting

- A component has a measured scaling or isolation need that processes of the
  monolith cannot meet.
- A component must be released or operated on a different lifecycle or by a
  different team.
- Module dependency rules prove unenforceable in practice.
