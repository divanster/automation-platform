# Architecture Overview

> **Status: Apart from the Phase 2 backend skeleton (section 9), nothing
> described here is implemented.** This document describes intended structure,
> not existing code. Confirmed directions are listed in
> section 2; other statements are proposals unless an Accepted ADR says
> otherwise.

## 1. Product scope

The Automation Platform is a **local-first business process automation**
product. It turns incoming business documents into reviewed, approved,
structured results through predefined workflows, while keeping company data
under the company's control by default.

Core product properties:

- **One shared product core.** A single codebase serves every company.
- **Predefined workflow packages.** Business processes ship as versioned
  packages built on the shared core.
- **Limited, validated company configuration.** Differences between companies
  are expressed as configuration that a workflow package explicitly allows and
  validates. They are never expressed as code changes.
- **Human approval and audit history** for results and for any consequential
  action.
- **Interchangeable AI providers** behind one gateway, governed by a per-company
  data policy (see [ai-policy.md](ai-policy.md)).

### Explicit non-goals

The product is **not**:

- an ERP system,
- an accounting or bookkeeping system,
- a warehouse or inventory system,
- a general-purpose workflow or low-code builder,
- a system that makes pricing, stock, or payment promises.

It also does **not** (at this stage) provide customer-specific code forks,
synchronization between independent desktop databases, microservices, a
required central control portal, or a vector or graph database as a
prerequisite.

## 2. Initial deployment assumptions

- Windows is the development and first deployment target.
- **One installation serves one company.** There is exactly one company profile
  per installation and database. The data model carries no per-row tenant key.
  No multi-tenant hosted implementation is in the initial scope.
- The first deployment is **standalone**: one machine runs the whole system.
- Boundaries are drawn so that a **company hub** (a company-operated shared
  installation used by several of that company's users) remains possible later,
  without redesign. See [deployment.md](deployment.md).
- **Local execution never depends on a central control portal.** See
  [ADR 0006](../decisions/0006-local-execution-independent-of-control-plane.md).

### Confirmed product directions

The project owner has confirmed the following. They are decisions, not
proposals, though none is implemented yet.

| Direction | Detail |
| --- | --- |
| Single company per installation | One installation serves one company initially. No multi-tenant hosted implementation in the initial scope. |
| Public identifiers | UUIDs for entities exposed through the API. |
| First slice input | Plain text (UTF-8) only. **PDF parsing is deferred.** |
| First slice export | JSON only. **CSV export is deferred.** |
| AI in the first slice | A deterministic test AI adapter, before any real provider. |
| External actions | No automatic email sending and no ERP writes. |
| Technology baseline | See [ADR 0007](../decisions/0007-technology-baseline.md). |

Items that are still proposals (for example the PostgreSQL-backed queue and the
AI policy modes) are labeled as such in their own documents and in the
[decision index](../decisions/README.md).

## 3. Components and responsibilities

| Component | Responsibility | Status |
| --- | --- | --- |
| Frontend (React/TypeScript) | User interface for upload, review, editing, approval, export download. Holds no business rules beyond presentation and input convenience. | Proposed; built late (Phase 7) |
| Django API | Authentication, authorization, request validation, orchestration of use cases, and the public HTTP interface. Owns all business rules through the shared domain layer. | Proposed |
| Shared domain layer | Business logic (cases, revisions, validation, approval rules, state transitions). Used by both the API process and the worker process. | Proposed |
| Persistent background worker | Separate long-running process that claims and executes tasks (extraction, export) outside request handling. Runs the same domain code as the API. | Proposed |
| PostgreSQL | Primary store for business records, workflow state, tasks, approvals, configuration, and audit events. | Proposed |
| Managed document storage | File storage for original documents, derived files, and export artifacts, located outside the source tree and referenced from the database. | Proposed |
| AI gateway | Single entry point for model calls; provider-neutral interface, adapters, data-policy enforcement, structured-output validation. | Proposed |
| Approvals and audit | Records of human decisions tied to specific result revisions, and an append-only history of significant events. | Proposed |
| Optional future control portal | Possible vendor-side portal (for example licensing or update information). Not required for local operation and not in scope now. | Deferred |

## 4. Component map

Textual map of the proposed standalone installation. Arrows show the direction
of calls ("A -> B" means A calls B).

```text
                    +----------------------------+
                    |  Browser / React frontend  |   (untrusted client)
                    +-------------+--------------+
                                  | HTTP (authenticated)
                                  v
  +--------------------------------------------------------------+
  |                  Backend codebase (one repository)           |
  |                                                              |
  |   +-----------------+                +--------------------+  |
  |   |  Django API     |                | Background worker  |  |
  |   |  process        |                | process            |  |
  |   +--------+--------+                +----------+---------+  |
  |            |                                    |            |
  |            +------------+      +----------------+            |
  |                         v      v                             |
  |                +--------------------------+                  |
  |                |   Shared domain layer    |                  |
  |                |  (rules, state changes,  |                  |
  |                |   validation, approvals) |                  |
  |                +----+---------+--------+--+                  |
  |                     |         |        |                     |
  |        +------------+   +-----+----+   +-----------+         |
  |        v                v          v               v         |
  |  +-----------+   +------------+  +---------+  +----------+   |
  |  | Workflow  |   | AI gateway |  | Document|  | Audit    |   |
  |  | packages  |   | (adapters) |  | storage |  | recorder |   |
  |  +-----------+   +-----+------+  | service |  +----------+   |
  |                        |         +----+----+                 |
  +------------------------|--------------|----------------------+
                           |              |
                           v              v
                 +------------------+  +------------------+
                 | AI provider      |  | Document files   |
                 | local / company  |  | on disk (outside |
                 | / external       |  | the source tree) |
                 | (policy-governed)|  +------------------+
                 +------------------+

  Domain layer, audit recorder, and task queue also read and write:

                 +----------------------------+
                 | PostgreSQL (primary store) |
                 +----------------------------+
```

The API process and the worker process both talk to PostgreSQL through the
shared domain layer. They do not call each other. The database is the
coordination point (see [background-execution.md](background-execution.md)).

## 5. Boundaries and allowed dependencies

The backend is a **modular monolith** ([ADR 0001](../decisions/0001-modular-monolith.md)):
one deployable codebase, divided into modules with explicit dependency
directions. Module names below are conceptual, not final package names.

Allowed dependency directions (a module may depend only on modules listed to
its right):

| Module | May depend on |
| --- | --- |
| API layer (HTTP views, serializers) | domain layer, authorization |
| Worker entry point | domain layer, task queue |
| Domain layer (cases, documents, revisions, approvals, exports) | audit recorder, document storage service, AI gateway interface, workflow-package registry |
| Workflow packages | domain layer public interfaces only |
| AI gateway | its own adapters, data-policy checks; **not** the domain layer |
| Document storage service | filesystem abstraction only |
| Audit recorder | database only |
| Task queue | database only |

Rules:

1. **No upward or sideways shortcuts.** The API layer does not reach into
   storage, the queue, or the AI gateway directly; it goes through the domain
   layer.
2. **The worker duplicates no logic.** It calls the same domain functions the
   API calls. A rule that exists only in worker code is a defect.
3. **The core does not import workflow packages by name.** Packages register
   themselves with the core through a defined registration interface; the core
   discovers them through the registry.
4. **The AI gateway knows nothing about business workflows.** It receives a
   request and a policy context and returns validated structured output.
5. **The frontend talks only to the Django API.** It never reaches PostgreSQL,
   storage, or AI providers.
6. **No microservices at this stage.** Splitting a module into a service
   requires a demonstrated need (see ADR 0001, revisit conditions).

## 6. Request processing versus background execution

| Aspect | Request processing (API process) | Background execution (worker process) |
| --- | --- | --- |
| Trigger | A user HTTP request | A task row in the database becoming ready |
| Lifetime | Short; bounded by request timeout | May be long (model calls) |
| Typical work | Authenticate, authorize, validate input, store an uploaded file, create a case/run/task, save a human edit as a new revision, record an approval, serve downloads | Run extraction through the AI gateway, run deterministic validation, produce export artifacts |
| AI calls | Never | Yes, only here |
| Database transactions | Short, one per request where practical | Short transactions around claim and result commit; none held open during an AI call |
| Failure handling | Return an error to the caller | Bounded retries, then mark failed |

The API **enqueues** work by inserting a task row in the same transaction that
records the user's intent, then returns. The worker picks the task up
asynchronously. Details and the failure model are in
[background-execution.md](background-execution.md).

## 7. Shared core, workflow package, and company configuration

Three layers must stay separate.

| Layer | Contains | Changes through | Per company? |
| --- | --- | --- | --- |
| **Shared core** | Case management, document handling, revisions, approval and audit mechanics, task queue, AI gateway, authorization, configuration validation | Product releases | No. Identical for every company |
| **Workflow package** | A versioned definition of one business process: input expectations, result schema, validation rules, prompt/extraction templates, export formats, the states it uses, and the schema of the configuration it accepts | Product releases | No. Identical for every company |
| **Company configuration** | Values a workflow package explicitly exposes and validates (for example field limits or which optional fields are required), the company's AI data-policy mode, and which model profiles are allowed | Validated configuration changes, versioned | Yes |

Rules:

- A company-specific behavior difference is acceptable only if it fits the
  configuration schema a package declares. Otherwise it is a product change or
  a new workflow package version, not a fork
  ([ADR 0005](../decisions/0005-company-configuration-not-forks.md)).
- Configuration never contains executable code.
- Configuration is validated on save and versioned. A running workflow uses an
  immutable **execution snapshot** of the configuration in effect when the run
  started (see [data-model.md](data-model.md)).

## 8. Trust boundaries

| Boundary | Trusted side | Untrusted or less-trusted side | Controls (proposed) |
| --- | --- | --- | --- |
| Browser to API | API process | Browser, user input | Authentication, authorization on every endpoint, server-side validation, CSRF/session protections appropriate to the chosen auth approach |
| Uploaded documents | Stored hash and metadata | File content | Content-based type detection, size limits, treated as data only, never executed or interpreted as instructions |
| Document content to AI model | Prompt built by the workflow package | Document text embedded in the prompt | Documents are data, not instructions; structured output only; no tool access for the model ([ai-policy.md](ai-policy.md)) |
| AI gateway to provider | Gateway | Provider (locality depends on policy mode) | Data-policy enforcement per request, no silent fallback, credentials held outside source control and never sent to the frontend |
| Model output to domain | Domain layer | Model-produced text and values | Schema validation and deterministic rule validation before output becomes a result revision; human review before approval |
| Application to storage and database | Service accounts | Other local users and processes | Directory and database access restricted to the service account; database listens only for local connections in standalone mode ([deployment.md](deployment.md)) |
| Installation to vendor control portal | Local installation | Any external portal | No inbound dependency: the installation must run fully without it |

## 9. Planned versus implemented status

| Item | Status |
| --- | --- |
| Repository, README, CLAUDE.md, `.gitignore`, `.gitattributes` | Implemented (documentation and configuration only) |
| These architecture documents and ADRs | Reviewed and revised in the Phase 1 review; accepted decisions are marked in the [decision index](../decisions/README.md); everything else is proposed |
| Django project skeleton (settings, `accounts`, `health`) | Implemented in Phase 2; DB-free tests pass; PostgreSQL-backed tests and health endpoints passed in the owner's local run ([record](../development/local-validation-record.md)) |
| `accounts.User` model and its migrations (`public_id`, a stable public UUID, was added by the review-services slice) | Implemented; the initial migration was applied in the owner's local runs; the three `public_id` migrations have not been applied to any database |
| Request-intake preparation slice (`workflows/request_intake`: result schema, pure validation, deterministic test extractor, canonical serialization and content hashing; plain Python, DB-free tests) | Implemented as preparation only; no persistence, stored revisions, approvals, export, or state machine |
| Revision identity and run scope (UUID identifiers), approval bound to a revision and content hash, and export eligibility (`workflows/revision_approval`: pure rules; plain Python, DB-free tests) | Implemented as pure policy only; no persistence, stored approvals, authentication or authorization, export, or state machine |
| Persistence of cases, workflow runs, result revisions, and approvals (`apps/cases`: minimal `Case` and `WorkflowRun`, immutable `ResultRevision` and `Approval`, unique revision numbers per run, a stored current-revision pointer, transactional services) | Implemented; ORM/service-level immutability only; PostgreSQL tests passed in the owner's local run for the tested scenarios only ([record](../development/local-validation-record.md)); not database tamper protection |
| Other domain models (documents, exports, configuration) and their migrations | Not started |
| Generic persistent task queue and worker (`apps/tasks`: enqueue with idempotency key, atomic claim, lease fencing, bounded retries, recovery, queued-task cancellation, `runworker`, one deterministic test handler; see [persistent-worker-slice.md](../development/persistent-worker-slice.md)) | Implemented; DB-free tests pass; the 95 PostgreSQL tests and a manual smoke test passed in the owner's local run for the tested scenarios only ([record](../development/local-validation-record.md)); not connected to any workflow |
| Request-intake orchestration (`apps/intake`, plus a run state in `apps/cases`: `submit_request` creates a case, a run, an immutable source text, one queued extraction task and its link in one transaction; the worker handler extracts with the deterministic fixture parser, validates, canonicalizes, and stores an `ExtractionOutcome` and, when valid, revision 1; failed tasks are reconciled into failed runs; see [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md)) | Implemented; DB-free tests pass; its 76 PostgreSQL tests and a manual smoke test passed in the owner's local run (as reported; tested scenarios only); no API, UI, or real AI |
| Review services and audit (`apps/intake/review.py`: authorized `edit_request`, `approve_request`, `reject_request`, and read services; roles Operator, Approver and Auditor as Django groups set up by `manage.py setup_roles`; `User.public_id`; `cases.Rejection`; `intake.RevisionEdit`; append-only `apps/audit` events written in the same transaction; see [review-services-slice.md](../development/review-services-slice.md)) | Implemented; DB-free tests pass; its 135 PostgreSQL tests are **written but have not been run**; no login mechanism, API, UI, export, or real AI; immutability and audit append-only are ORM-level only |
| AI gateway, adapters | Not started |
| Frontend | Not started |
| Installer, backup/restore tooling | Not started |
| PostgreSQL 18 | Selected; installed on the owner's **local** computer (reported 18.6), never in the development VM. Validated for the tested scenarios; the reported `listen_addresses='*'` is an unresolved hardening item |
| CI | None |

This table must be updated whenever a component is actually built.

## 10. Related documents

- [first-workflow.md](first-workflow.md): the first vertical slice
- [data-model.md](data-model.md): entities and relationships
- [background-execution.md](background-execution.md): queue and worker design
- [ai-policy.md](ai-policy.md): AI gateway and data policy
- [deployment.md](deployment.md): deployment topologies and operations
- [implementation-plan.md](implementation-plan.md): phased plan
- [open-questions.md](open-questions.md): unresolved decisions
- [Decision records](../decisions/README.md), including the
  [technology baseline](../decisions/0007-technology-baseline.md)
