# ADR 0006: Local execution independent of a control plane

## Status

Accepted. This follows the owner's constraints: local execution does not
depend on a central control portal, and independent desktop databases are not
synchronized.

Qualification: licensing and update delivery without a control portal are
unresolved ([open-questions.md](../architecture/open-questions.md)).

## Context

The product is local-first: companies expect it to work on their own machines or
servers, possibly offline or behind restrictive networks. A vendor-operated
control portal (for licensing, update information, or fleet management) might be
useful later, but a dependency on it would make local operation fragile and would
weaken the data-control promise.

## Decision

An installation must **run fully without any central control portal**. Document
processing, workflows, approvals, auditing, and the AI data policy operate using
only local components (and any AI endpoints the company's policy permits). A
control portal, if ever built, is optional and limited to non-essential
functions; its absence or unavailability must never stop local execution or
change data-handling policy. Independent desktop databases are **not**
synchronized with each other; shared use is addressed by a company hub
([deployment.md](../architecture/deployment.md)).

## Alternatives

- **Mandatory cloud control plane.** Simplifies licensing and updates but breaks
  offline use and the local-first promise.
- **Periodic mandatory check-in.** Softer, but still introduces a hard dependency
  and failure mode.
- **Synchronized local databases.** Offers shared data without a hub, but
  introduces conflict resolution complexity that is out of scope.

## Consequences and trade-offs

- Licensing, updates, and support must work without constant connectivity, which
  may be less convenient and less enforceable.
- Update delivery and entitlement checks are open questions.
- Fleet-wide visibility for the vendor is limited unless an optional portal is
  added later.

## Conditions for revisiting

- A client or regulatory need requires centrally managed policy that cannot be
  delivered offline.
- Licensing requirements cannot be met without some connectivity; any such
  connectivity must then be optional or fail open for existing data and
  workflows, and be recorded in a new ADR.
