# Data Model

> **Status: Proposed.** Only a minimal subset exists as Django models:
> `Case`, `WorkflowRun` (identity, case link, current-revision pointer), `ResultRevision`,
> and `Approval` (approved decisions only), in `apps/cases`, plus the `accounts.User`
> skeleton model, plus `RequestSource`, `StepTask` and `ExtractionOutcome` in `apps/intake` and a run
> `state` and `failed_step` on `WorkflowRun` (see
> [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md)), plus
> `RevisionEdit` in `apps/intake`, `Rejection` in `apps/cases`, `AuditEvent` in `apps/audit`, and a stable
> `User.public_id` (see [review-services-slice.md](../development/review-services-slice.md)). The PostgreSQL tests
> of all of these models passed in the owner's local run (tested scenarios only, see the [local validation record](../development/local-validation-record.md)). Everything else here is
> not built. This document defines
> entity responsibilities and relationships. Field lists are illustrative and
> will be refined when the backend is built (Phase 3). Confirmed product
> directions (one company per installation, UUID public identifiers) are
> recorded in [overview.md](overview.md), section 2.

## 1. Principles

- **One installation, one company** (confirmed direction for the initial
  scope). There is a single `CompanyProfile` per database. Tables carry no
  per-row tenant key, and no multi-tenant hosted implementation is in the
  initial scope. Adding multi-tenancy later would be a deliberate redesign, not
  a configuration switch.
- **PostgreSQL stores records, never document bytes.** File content lives in
  managed document storage; the database stores a reference and a hash.
- **Immutable history.** Original documents, result revisions, approvals, and
  audit events are never edited in place.
- **Relational first.** Relationships are expressed with foreign keys. No
  vector or graph structure is required for the first workflow.
- **Provenance everywhere.** Derived data records which workflow version,
  configuration version, and model profile produced it.

## 2. Distinctions that must stay clear

| Distinguish | Explanation |
| --- | --- |
| **Case** vs **TaskAttempt** | A `Case` is a business matter (for example one request). A `TaskAttempt` is one technical attempt to run a piece of work. A case may involve many runs; a run involves many tasks; a task may have many attempts. Failed attempts do not change the business facts of a case. |
| **WorkflowDefinition** vs **WorkflowRun** | A definition is a versioned, reusable description of a process (shipped in a workflow package). A run is one execution of one definition version against one case. |
| **Original document** vs **derived result** | A `Document` is the uploaded file, immutable. A `ResultRevision` is data derived from it by extraction or human editing. Derived data can be superseded; originals cannot be changed. |
| **Current configuration** vs **execution snapshot** | Company configuration may change over time. A run records an immutable snapshot of the configuration, workflow version, and model profile settings in effect when it started, so its results remain explainable later. |

## 3. Entity overview

Primary entities (requested scope):

`CompanyProfile`, `User` and roles, `Case`, `Document`, `WorkflowDefinition`,
`WorkflowRun`, `TaskAttempt`, `Approval`, `ExportArtifact`, `AuditEvent`,
`ModelProfile`.

Supporting entities introduced by this design because the primary entities
cannot express the requirements without them:

- `ResultRevision`: immutable derived result (needed for revision-bound
  approval).
- `ConfigurationVersion`: versioned company configuration.
- `Task`: the queue entry whose execution history is `TaskAttempt`.

Relationship summary (textual):

```text
CompanyProfile 1 --- * ConfigurationVersion
CompanyProfile 1 --- * User            (User * --- * Role via role assignment)
CompanyProfile 1 --- * ModelProfile

Case 1 --- * Document
Case 1 --- * WorkflowRun
WorkflowDefinition 1 --- * WorkflowRun
ConfigurationVersion 1 --- * WorkflowRun     (snapshot reference)
ModelProfile 1 --- * WorkflowRun             (snapshot reference)

WorkflowRun 1 --- * Task
Task 1 --- * TaskAttempt
WorkflowRun 1 --- * ResultRevision
ResultRevision 1 --- * Approval
ResultRevision 1 --- * ExportArtifact
Approval 1 --- * ExportArtifact              (the approval that authorized it)

AuditEvent * --- 0..1 Case / WorkflowRun / other subject
```

## 4. Entities

### CompanyProfile

- **Responsibility:** the identity and installation-level settings of the one
  company served by this installation: display name, the company AI data-policy
  mode (see [ai-policy.md](ai-policy.md)), and a pointer to the active
  `ConfigurationVersion`.
- **Constraint:** exactly one row per installation. Enforced by the domain
  layer and a database constraint.
- **Not responsible for:** per-workflow business settings (those live in the
  configuration).

### ConfigurationVersion (supporting)

- **Responsibility:** one immutable version of the company's validated
  configuration, containing only values that installed workflow packages
  declare as configurable.
- **Identity:** version number, unique and increasing within the installation.
  A content hash is stored alongside the structured values.
- **Rules:** a version is validated before it is saved and never edited after
  being saved. A change creates a new version. Exactly one version is marked
  active.

### User and roles

- **Responsibility:** the people (and, later, service identities) who act in the
  system, and the permissions they hold.
- **Proposed roles (to be confirmed):**
  - `operator`: upload documents, view cases, edit drafts, request export of
    approved results.
  - `approver`: approve or reject revisions.
  - `administrator`: manage users, configuration, model profiles.
  - `auditor`: read-only access, including audit events.
- **Rules:** roles are assigned to users; a user may hold several. Authorization
  is checked in the domain/API layer on every operation. The `system` actor
  (used by the worker in audit events) is not a login user.
- **Uniqueness:** unique login identifier. Credentials handling depends on the
  authentication approach (open question) and is never stored in plain text.
- **Implementation (review-services slice):** `User.public_id` is a random, unique, non-null UUID that
  identifies the user in domain records that store a plain actor UUID (approvals, rejections, audit events);
  the integer primary key is unchanged. The `operator`, `approver` and `auditor` roles exist as Django groups
  (`setup_roles`) with the initial development permissions in
  [review-services-slice.md](../development/review-services-slice.md); the `administrator` role does not. No
  login mechanism exists.

### Case

- **Responsibility:** the business matter being handled, for example one
  incoming request. Groups the documents and workflow runs that belong to it.
- **Identity:** an internal identifier plus a human-readable case reference,
  unique within the installation.
- **Does not contain:** execution state. Technical progress lives in
  `WorkflowRun`, `Task`, and `TaskAttempt`.

### Document

- **Responsibility:** one original uploaded file, immutable after storage.
- **Key attributes:** owning case, uploader, original filename (treated as
  user-provided data), detected media type, size, **SHA-256 hash**, and a
  **storage reference**.
- **Storage reference:** a key relative to the managed storage root, generated
  by the system (not derived from the filename). It is never an absolute path
  and never points into the source tree. The storage root is configured per
  installation ([deployment.md](deployment.md)).
- **Integrity:** the hash is computed at upload and may be re-verified later,
  for example on restore.
- **Uniqueness:** unique `(case, SHA-256)`; uploading identical bytes twice into
  one case is rejected or linked to the existing document (policy to be decided
  in Phase 3). The same bytes may appear in different cases.

### WorkflowDefinition

- **Responsibility:** a versioned description of a business process, supplied
  by a workflow package: result schema, validation rule set, prompt/extraction
  template references, export formats, and the schema of the configuration it
  accepts.
- **Identity:** `(workflow key, version)`, unique. A published version is
  immutable; a change is a new version.
- **Relationship:** referenced by runs. Existing runs keep pointing at the
  version they started with.

### WorkflowRun

- **Responsibility:** one execution of one `WorkflowDefinition` version for
  one `Case`. Holds the canonical `state` (names in
  [first-workflow.md](first-workflow.md)), `failed_step`, and the pointer to the
  current `ResultRevision`.
- **Execution snapshot:** at creation, the run records references to the
  workflow definition version, the `ConfigurationVersion` (and its hash), and
  the `ModelProfile` (together with a copy of the resolved settings that
  affect behavior, such as provider kind, model identifier, and the data-policy
  mode in effect). Because workflow definition versions and configuration
  versions are immutable, references to them are sufficient; the `ModelProfile`
  settings are copied because profiles may be edited. Later changes to current
  configuration do not alter a run's recorded history or its validation rules.
- **Snapshot versus enforcement.** The snapshot records *what was used* and
  makes results explainable. It is **not** a permission. The data-policy mode
  is evaluated against the company's *current* mode at execution time, so a
  tightening (for example to `local_only`) takes effect immediately for tasks
  not yet executed ([ai-policy.md](ai-policy.md)).
- **Concurrency guard:** state changes are made with an optimistic version
  counter so two concurrent requests cannot both apply conflicting transitions.
- **Uniqueness:** run identifiers are unique. Multiple runs per case are
  allowed (for example a correction after export).

### Task (supporting)

- **Responsibility:** a queue entry representing one unit of background work
  for a run (for example `extract` or `export`): kind, state, scheduling time,
  lease information, attempt count, retry limit, cancellation flag.
- **Uniqueness:** an idempotency key unique per logical unit of work, composed
  of the run, the step, and a **retry generation** number. The first enqueue of
  a step uses generation 1, so the same work cannot be enqueued twice; a
  deliberate manual retry after `failed` uses the next generation and therefore
  creates a *new* task without reopening or altering the failed one (see
  [background-execution.md](background-execution.md)).
- **Task versus attempt:** a `Task` is the unit of work and its overall
  outcome; automatic retries of that task are further `TaskAttempt` rows under
  the same task. A manual retry is a new `Task`.

### TaskAttempt

- **Responsibility:** the record of one claim and execution of a task: worker
  identity, start and end times, outcome (`succeeded`, `failed`, `abandoned`,
  `cancelled`), a short error category and message, and a lease token used to
  reject stale results.
- **Provenance:** for AI calls, references the `ModelProfile` and records
  adapter name, reported model identifier, request parameters, and token or
  duration metrics if available. It does not store document text or prompts by
  default.
- **Uniqueness:** `(task, attempt number)`.
- **Not business history:** attempts explain how work ran. They never carry
  business decisions.

### ResultRevision (supporting)

- **Responsibility:** one immutable version of the structured draft for a run.
- **Key attributes:** run, **revision number** (1, 2, ...), parent revision,
  structured payload conforming to a result schema version, **content hash**
  (SHA-256 of a canonical serialization), validation findings with severity,
  origin (`extraction` or `human_edit`), and the author: the editing user, or
  the task attempt (and therefore model provenance) for extraction.
- **Uniqueness:** unique `(run, revision number)`. At most one `extraction`
  revision per run, which also prevents duplicate output if extraction is
  executed twice.
- **Immutability:** never updated after creation. An edit produces a new row.
- **Valid and invalid:** a revision with no blocking findings is *valid*; one
  or more blocking findings make it *invalid*. Invalid revisions are stored
  (so humans can correct them) but can be neither approved nor exported
  ([first-workflow.md](first-workflow.md), approval and export rules).

### Approval

- **Responsibility:** a recorded human decision (`approved` or `rejected`)
  about one specific revision.
- **Key attributes:** the revision id, the **content hash of that revision at
  decision time**, the deciding user, decision time, and an optional reason
  (required for rejection).
- **One approval authorizes one immutable revision.** It never authorizes any
  other revision, including a later one that descends from it.
- **Effective approval:** an approval is *effective* only when its decision is
  `approved`, its revision is the run's current revision, the stored hash
  equals that revision's content hash, and the revision is valid. This is
  **derived**, not stored: editing creates a new revision, so the earlier
  approval simply stops matching the current revision. The consequence is
  recorded as an `approval.invalidated` audit event.
- **Preserved and immutable:** approval rows are never updated or deleted
  through the application. Historical approvals and rejections are kept
  permanently. There is no stored "invalidated" flag to modify.
- **Pure policy:** the identity, hash-binding, and export-eligibility rules above exist as
  database-independent Python (UUID identifiers; each revision and approval is bound to its
  case, workflow run, revision, and content hash, and an approval from another run never
  authorizes an export), with no persistence or authentication (see
  [revision-approval-policy.md](../development/revision-approval-policy.md)). Approvals
  and revisions are now persisted (`apps/cases`): unique `(run, revision number)` in the
  database, a stored current-revision pointer, ORM- and service-level immutability (not
  database-level), and approver identity stored as data. The review-services slice
  ([review-services-slice.md](../development/review-services-slice.md)) adds the `needs_review` state guard, an
  immutable `Rejection` (a separate model: `Approval` stays "approved" only), and an approval service that
  derives the approver from the authenticated user's `public_id` (its PostgreSQL tests passed in the owner's local
  run, tested scenarios only). The
  atomic re-check at export commit remains future work.
- **Creation guard:** an `approved` decision can be created only for the
  run's current revision, only while the run is in `needs_review`, and only if
  the revision is valid.

### ExportArtifact

- **Responsibility:** a file produced from an approved revision.
- **Key attributes:** run, revision, the authorizing approval, format and format
  version, file **SHA-256**, size, **storage reference**, the creating task
  attempt, and creation time.
- **Uniqueness:** unique `(revision, format, format version)`, so repeating the
  same export returns the existing artifact.
- **Rule:** may only be created while the referenced approval is effective,
  checked again at commit time by the worker. If the approval is no longer
  effective at that moment, the result is discarded.

### AuditEvent

- **Responsibility:** append-only record of significant actions for
  accountability.
- **Key attributes:** event id, timestamp, actor (user id or the `system`
  actor), action name from a fixed vocabulary (for example `document.uploaded`,
  `run.state_changed`, `revision.created`, `approval.recorded`,
  `approval.invalidated`, `export.created`, `task.failed`), subject type and
  id, related case and run ids, a request or task correlation id, and a small
  structured `details` object.
- **Content restraint:** `details` holds identifiers, state names, counts,
  hashes, error categories, and version references. It does **not** hold
  document text, extracted values, prompts, model output, credentials, or
  personal data beyond identifiers. The sensitive content stays in
  `Document` and `ResultRevision` where access is controlled.
- **Immutability (proposed):** the application never updates or deletes audit
  rows; the runtime database role is granted insert and select only on this
  table. Tamper-evidence beyond that (for example hash chaining) is an open
  question.
- **Implementation (review-services slice):** `apps/audit` stores events with the vocabulary `request.submitted`,
  `revision.created`, `request.edited`, `run.state_changed`, `approval.recorded`, `approval.invalidated` and
  `rejection.recorded`, written in the transaction of the operation they describe. Append-only at the ORM and
  service boundary only: no trigger, no restricted database role, no hash chain. The `details` values are
  restricted to booleans, integers, null and short tokens. Not recorded yet: `document.uploaded`,
  `export.created`, `task.failed`, and refused attempts.

### ModelProfile

- **Responsibility:** a named, administrator-managed description of how a task
  may call an AI model: adapter kind, **locality** (`local`, `company_endpoint`,
  or `external`), model identifier, declared capabilities, parameters, and a
  reference to credentials held outside the database and source control.
- **Versioning:** profiles are versioned. A run snapshots the profile settings
  it used. Changing a model for a workflow requires an evaluation record (see
  [ai-policy.md](ai-policy.md)).
- **Seed profile:** the first profile is the deterministic test adapter, which
  has locality `local` and no credentials.

## 5. Identifiers and uniqueness

- **Public identifiers:** **UUIDs** (confirmed direction) for every entity
  exposed through the API. Sequential integers must not be exposed as public
  identifiers. Whether internal primary keys are also UUIDs is a Phase 3
  detail.
- **Human-readable references:** cases have a readable reference, unique within
  the installation.
- **Summary of uniqueness constraints (proposed):**

| Entity | Constraint |
| --- | --- |
| CompanyProfile | Single row |
| User | Unique login identifier |
| Case | Unique case reference |
| Document | Unique `(case, SHA-256)` |
| WorkflowDefinition | Unique `(workflow key, version)` |
| ConfigurationVersion | Unique version number |
| ResultRevision | Unique `(run, revision number)`; at most one `extraction` revision per run |
| Task | Unique idempotency key `(run, step, retry generation)` |
| TaskAttempt | Unique `(task, attempt number)` |
| ExportArtifact | Unique `(revision, format, format version)` |

## 6. Result revisions

- Revision numbers start at 1 per run and increase by one.
- A run's `current revision` pointer moves forward only.
- Approval and export always reference a revision id plus its content hash.
- Hashes are computed over a canonical serialization so equal content yields
  equal hashes. The version-1 format for the request workflow is defined and
  tested as a database-independent preparation component (see
  [request-intake-slice.md](../development/request-intake-slice.md)): it hashes the
  requester, items, notes, and source references, and excludes validation findings
  and execution metadata, which stay separate revision attributes. Source references
  must be verified against the source text, so canonicalizing a draft that has any
  requires that text; the text itself is not hashed, and the hash does not identify the
  original document, whose own identity and hash remain separate revision metadata. A
  revision must store the canonical format version with its hash. Nothing is persisted
  yet.

## 7. File hashes and storage references

- Every stored file (`Document`, `ExportArtifact`) records a SHA-256 hash and a
  storage reference relative to the storage root.
- Files are written to a temporary location and moved into place atomically; the
  database row is committed only after the file is durable. An orphaned file
  with no row is possible after a crash and must be detected by a maintenance
  check rather than assumed impossible.
- Backups must capture the database and the storage directory as a consistent
  pair ([deployment.md](deployment.md)).

## 8. Configuration and workflow versioning

- `WorkflowDefinition` versions and `ConfigurationVersion` versions are both
  immutable once saved.
- A `WorkflowRun` stores references to the exact versions it used.
- Upgrading a workflow package adds new definition versions. In-progress runs
  continue on their original version unless an explicit migration is defined.
- Configuration is validated against the schema declared by the workflow
  definition version it applies to.

## 9. Model and provider provenance

For every AI-derived revision the system can answer: which workflow version,
which configuration version, which model profile and adapter, what reported
model identifier, and which task attempt. This lets a later model change be
traced and evaluated. The provenance lives on `TaskAttempt` and the run's
execution snapshot, not in the audit `details`.

## 10. Audit metadata

See the `AuditEvent` entry. In short: record who, what, when, which subject,
which versions, and which correlation id, but not the sensitive content itself.

## 11. Optional capabilities

- **pgvector:** if semantic retrieval is later needed, an extension and
  dedicated tables may be added. The first workflow does not depend on it, and
  no table in this document requires it.
- **Graph database:** none. Relationships are modeled relationally.

## 12. Explicitly not specified yet

Exact column types, indexes, retention periods, soft-delete policy, personal
data deletion handling, and the authentication tables are decided during Phase 3
and in [open-questions.md](open-questions.md).
