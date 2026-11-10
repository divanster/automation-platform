# ADR 0002: PostgreSQL as primary storage

## Status

Accepted: PostgreSQL is the only application database, with relational
modeling and managed file storage for document bytes. pgvector remains an
optional, deferred capability.

Qualification: installing and operating PostgreSQL on client Windows machines
is **unproven** until the installation feasibility experiment. PostgreSQL 18
(native Windows) is selected ([ADR 0007](0007-technology-baseline.md)) and will
be installed on the owner's local Windows computer, not in the development VM.
It is not yet installed, and Django 5.2 with PostgreSQL 18 is unverified until
the PostgreSQL-backed tests have run locally and the owner has reported the
results.

## Context

The system must store business records, workflow state, tasks, approvals,
configuration, and audit events with strong consistency and clear relationships.
It must run on a single Windows machine, and later possibly as a company hub.
Semantic retrieval may become useful but is not required by the first workflow.

## Decision

Use **PostgreSQL** as the primary database for business records, workflow state,
tasks, approvals, configuration, and audit events. Model data **relationally**
with foreign keys and uniqueness constraints. Store document bytes in managed
file storage, referenced by a relative storage key and a content hash. Treat **pgvector as an optional
capability**, added only when semantic retrieval is needed; the first workflow
must not depend on it. Do not introduce a separate graph database without a
demonstrated requirement.

## Alternatives

- **SQLite.** Very easy to ship, but weaker concurrency for a separate worker and
  API, and a harder path to a shared hub. May suit tests or throwaway prototypes.
- **Other relational databases.** Possible, but PostgreSQL fits the planned
  queue approach ([ADR 0003](0003-persistent-background-worker.md)) and optional
  vector support.
- **Dedicated vector store or graph database.** Extra services without a current
  requirement.
- **Document bytes in the database.** Simplifies consistency but bloats the
  database and backups.

## Consequences and trade-offs

- A database server must be installed and managed on each standalone machine,
  with a lifecycle owned by installation tooling
  ([deployment.md](../architecture/deployment.md)).
- The database and file storage must be backed up and restored as a consistent
  pair.
- The same database serves as the task queue, concentrating more responsibility
  in one component.
- Relational modeling may need rework if highly graph-like queries emerge.

## Conditions for revisiting

- Installing and operating PostgreSQL on client machines proves unworkable (see
  the feasibility experiment in the
  [implementation plan](../architecture/implementation-plan.md)).
- Semantic retrieval becomes a core requirement and pgvector is insufficient.
- A concrete query pattern shows relational modeling is inadequate.
