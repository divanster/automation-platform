# Revision, approval, and export-eligibility policy, and its persistence

> **Status.** The policy rules (`workflows/revision_approval`) are pure Python and fully
> DB-free. Their persistence (`apps/cases`, described in "Persistence" below) is implemented
> but **its PostgreSQL behavior has not been run**: only DB-free tests ran, in the development
> VM. There is no endpoint, worker, file export, authentication, authorization, audit trail,
> or workflow state machine, and nothing here is a completed approval phase. Phase 2 database
> acceptance is still pending the owner's local PostgreSQL validation.

## What exists

Package `backend/workflows/revision_approval/` (not a Django app).

| Module | Depends on | Contents |
| --- | --- | --- |
| `errors.py` | standard library | `PolicyInputError` family, `ApprovalRefusal`, `ApprovalRefusedError` |
| `identifiers.py` | standard library | `uuid.UUID` validation, explicit `parse_uuid`, revision-number rules |
| `findings.py` | generic modules | `RevisionFinding`: a workflow-independent finding |
| `revision.py` | generic modules | `ResultRevision` (immutable) and `RevisionProblem` consistency codes |
| `approval.py` | generic modules | `ApprovalRecord` (immutable) and `approve_revision(...)` |
| `export_eligibility.py` | generic modules | `check_export_eligibility(...)`, `ExportDecision`, `ExportDenialReason` |
| `request_intake_revision.py` | generic modules **and `workflows.request_intake`** | `create_request_intake_revision(...)` and `findings_from_validation(...)` |

The terminology follows [data-model.md](../architecture/data-model.md): a case has runs, a
run has result revisions, and an approval is bound to a revision and its content hash.

### Module dependency boundaries

- The **generic** modules (`errors`, `identifiers`, `findings`, `revision`, `approval`,
  `export_eligibility`, and the package `__init__`) import only the standard library and each
  other. They import **no** workflow package and name no request-intake type
  (`ValidationResult`, `StructuredRequest`, `Finding`, `canonicalize_request`, ...).
- `request_intake_revision.py` is the **only** module that imports `workflows.request_intake`.
  The package `__init__` deliberately does not import it, so `import workflows.revision_approval`
  does not load request intake. Import the factory explicitly:
  `from workflows.revision_approval.request_intake_revision import create_request_intake_revision`.
- The request-intake package never imports `revision_approval`.

Static tests check all three rules on the source, and a clean-interpreter test runs the whole
generic flow (revision, approval, eligibility) on hand-made canonical bytes with
`workflows.request_intake` made unimportable.

## Identifier contract

- The canonical internal type for the **case, workflow run, revision, and approver**
  identifiers is `uuid.UUID`. Only `uuid.UUID` instances are accepted.
- **Strings are not accepted, not even well-formed UUID text.** The explicit conversion is
  `parse_uuid(text, label)`, which accepts only the canonical 8-4-4-4-12 hexadecimal form and
  raises `InvalidIdentifierError` for anything else (braces, `urn:uuid:` prefixes, missing
  hyphens, surrounding whitespace, non-ASCII digits, other types). Error messages name the
  field and never echo the value.
- **The policy never generates identifiers,** UUIDs, timestamps, or sequence values; the
  caller supplies them. The UUID version and whether the identified thing exists are not
  checked. **Revision numbers** are integers of at least 1 (never a bool).

## Result revision

A `ResultRevision` has `case_id`, `run_id`, `revision_id` (UUIDs), `revision_number`,
`canonical_format_version`, `canonical_bytes`, `content_hash`, and `findings` (a tuple of
`RevisionFinding`, kept for review but **not** part of the hashed content; a list is copied
into a tuple). Each revision belongs to exactly one case and one workflow run.

- **Consistency is enforced on construction** and can be re-checked with
  `consistency_problems()`: `content_hash` must be the SHA-256 of exactly
  `canonical_bytes`; the format version must equal the `canonical_format_version` written
  inside the bytes; the bytes must be a JSON object that carries it; and there must be no
  blocking finding. Problem codes: `invalid_case_id`, `invalid_run_id`,
  `invalid_revision_id`, `invalid_revision_number`, `invalid_format_version`,
  `invalid_bytes`, `format_version_mismatch`, `invalid_hash`, `hash_mismatch`,
  `invalid_findings`, `blocking_findings`. A revision of another (historical) format version
  is accepted if it agrees with itself; the policy never recomputes or reinterprets content.
- **Creation for request intake goes through the existing canonicalization operation.**
  `create_request_intake_revision(request, *, case_id, run_id, revision_id, revision_number,
  limits=None, source_text=None, ambiguities=())` calls `canonicalize_request` and wraps its
  output, so blocking findings (`DraftNotValidError`), missing source text for source
  references (`SourceTextRequiredError`), and serialization safety refusals
  (`SerializationSafetyError`) cannot be skipped. It converts the validation findings into
  `RevisionFinding` records with `findings_from_validation`. It does not format JSON or
  Decimals and does not define another hash format (a test checks this on the code).
- **Warnings** stay on `revision.warnings`; they are not in the hashed bytes.

**What this module does not enforce:** database uniqueness (for example one revision number
per run), durable immutability, ordering, or access control. A frozen dataclass is not
tamper-proof, so the export check re-verifies consistency.

## Approval record

An `ApprovalRecord` holds `case_id`, `run_id`, `revision_id`, `content_hash`, `approver_id`
(all identifiers `uuid.UUID`), and `acknowledged_warnings` (a tuple of `(finding code,
field path)` pairs; no business content). `approve_revision(revision, *, approver_id,
warnings_reviewed=False)`:

- binds to the exact revision: its case, its run, its id, **and** its content hash;
- refuses with a stable `ApprovalRefusal` reason: `blocking_findings`,
  `revision_inconsistent`, or `warnings_not_reviewed`;
- never removes warnings: if the revision has any, `warnings_reviewed` must be **exactly
  `True`** (other truthy values do not count), and the acknowledged warnings are then
  recorded; the refusal carries the warnings for review.

**Limitation of the confirmation.** `warnings_reviewed=True` is only a declaration by the
caller. It is **not** proof that a real human acted, and the policy cannot tell who the
approver is. A future authenticated application service must establish the approver's
identity and authorization and that the warnings were actually reviewed.

**Historical approvals are preserved.** Records are frozen and never modified. Creating a
new revision does not touch an existing approval; the old approval simply does not match the
new revision. A new revision needs its own approval **even when its content hash equals** the
old one. Tests verify that equality and identity are unchanged.

## Export eligibility

`check_export_eligibility(*, expected_case_id, expected_run_id, requested_revision,
current_revision, approval)` returns an `ExportDecision(allowed, reasons, case_id, run_id,
revision_id)`. `allowed` is true exactly when there are no reasons; all applicable reasons are
returned in this fixed order (stable codes):

| Reason | Meaning |
| --- | --- |
| `case_mismatch` | The requested or current revision is not in the expected case |
| `run_mismatch` | The requested or current revision is not in the expected workflow run |
| `revision_inconsistent` | The requested revision fails its consistency re-check |
| `not_current_revision` | The requested revision is not the current revision the caller supplied |
| `no_approval` | No approval was supplied |
| `approval_case_mismatch` | The approval belongs to another case than the requested revision |
| `approval_run_mismatch` | The approval belongs to another workflow run than the requested revision |
| `approval_revision_mismatch` | The approval's revision id is not the requested revision's |
| `approval_hash_mismatch` | The approval's content hash is not the revision's |
| `warnings_not_acknowledged` | The revision has warnings that the approval did not record |

It writes no file, produces no export artifact, and has no side effects (tested).

**Run scope.** The caller states which case and which run the export is for. **An approval
from another run never authorizes an export,** even when the case, the payload, the revision
id, and the content hash are all equal: the run id is part of the approval and is compared
(`approval_run_mismatch`). A revision from another run is denied (`run_mismatch`) even with
its own valid approval. How a run's current revision is determined is the caller's job (see
below).

**An approval for revision A never authorizes revision B,** even if their content hashes
are equal: the revision ids must match. Editing creates a new revision, so approval A stays
unchanged but does not authorize B; B needs its own approval. Requesting a superseded
revision A while B is current is denied (`not_current_revision`) even with A's own valid
approval.

### Concurrency boundary

The check works on the snapshot passed in. **Alone, it cannot prevent an edit from racing
with an export:** a newer revision may be created after the caller read the "current
revision". A test demonstrates this: a stale snapshot is still "allowed". The future
persistence and application layer must **re-check the current revision and the approval
atomically at execution and commit time** (for example inside one database transaction with
suitable locking) and discard the result if they no longer match, as the
[data model](../architecture/data-model.md) already requires. Nothing here provides
transactional behavior or promises exactly-once export.

## Persistence (`apps/cases`)

A Django app (not part of `workflows/`) that stores the above and reads it back through the
pure policy. **The generic `revision_approval` modules import no Django or database code**
(a test checks this); only `apps/cases` depends on them, and it never imports
`workflows.request_intake` (it accepts any generic `ResultRevision`).

### Models and migration

`apps/cases/migrations/0001_initial.py` (schema operations only; no raw SQL):

| Model | Holds | Notes |
| --- | --- | --- |
| `Case` | UUID id, unique `reference`, `created_at` | Minimal: the entity from [data-model.md](../architecture/data-model.md), not a parallel one |
| `WorkflowRun` | UUID id, `case` (PROTECT), `current_revision` (nullable, PROTECT), `created_at` | Minimal: state, definition and configuration snapshots are added by later migrations to this table |
| `ResultRevision` | UUID id (caller-supplied), `run` (PROTECT), `revision_number`, `canonical_format_version`, `canonical_bytes`, `content_hash`, `findings` (JSON), `created_at` | Immutable. Case comes from `run.case` |
| `Approval` | UUID id, `revision` (PROTECT), `content_hash`, `approver_id` (UUID), `acknowledged_warnings` (JSON), `created_at` | Immutable. Decision "approved" only. Case and run come from the revision |

Database-enforced: primary keys, foreign keys, **unique `(run, revision_number)`**
(`cases_revision_number_unique_per_run`), `revision_number >= 1`,
`canonical_format_version >= 1`, and `content_hash` being 64 lowercase hex characters on both
revisions and approvals. The unique constraint is per run, so the same number in two runs is
allowed.

### Write operations (`apps/cases/services.py`)

- `record_revision(domain_revision)`: **one transaction**. Locks the run row
  (`SELECT ... FOR UPDATE`), checks that the run belongs to the revision's case, that the
  number is exactly the run's highest plus one (starting at 1), and that the stored pointer is
  consistent, inserts the immutable row, and moves `current_revision` to it. The pointer
  therefore only moves forward. A duplicate or skipped number raises `RevisionNumberError`.
- `record_approval(domain_approval)`: **one transaction**. Locks the run row, loads and
  re-verifies the stored revision through the domain, checks case, run, revision id, and
  content hash against the stored rows, requires that the revision is **still the run's
  current revision** (`NotCurrentRevisionError`), and requires the record to equal what
  `approve_revision` produces for the stored revision (so skipped warning acknowledgements are
  refused). Earlier approvals are never touched.
- `create_case`, `create_run`: minimal creation (ids are generated here, in the persistence
  layer, unless supplied; the pure functions never generate ids).
- Reads: `load_revision`, `get_current_revision`, `list_revisions`, `load_approval`,
  `list_approvals`. They return domain objects; building them re-runs the domain's hash and
  consistency checks, so a corrupted row raises `StoredDataError` instead of being trusted.
- `check_persisted_export_eligibility(expected_case_id, expected_run_id,
  requested_revision_id, approval_id=None)`: loads the stored revision, the expected run's
  current revision, and the approval (by id, from any run), then calls the pure
  `check_export_eligibility`. **The decision, the denial codes, and their fixed order come
  unchanged from the pure policy.** An approval from another run therefore reaches the policy
  and is denied there (`approval_run_mismatch`). A run with no current revision raises
  `RecordNotFoundError`.

Identifiers are `uuid.UUID` throughout (strings are refused with `InvalidIdentifierError`).
Content hashing is the domain's: the persistence code computes no hash, and a test checks
that it imports no hashing module.

### Where immutability is enforced, and the bypasses

Revisions and approvals are immutable **at the Django ORM and service boundary only**.

Guarded (refused with `ImmutableRecordError`):

| Mutation path | Guard |
| --- | --- |
| `save()` / `save_base()` (including `loaddata`'s `save_base(raw=True)`) on an existing row, or with `update_fields` / `force_update` | `ImmutableRecord.save` and `save_base`; a new row is always inserted, so an existing primary key raises `IntegrityError` instead of being overwritten |
| `delete()` on an instance | `ImmutableRecord.delete` |
| `QuerySet.update()`, `bulk_update()`, `delete()`, `bulk_create(update_conflicts=True)` | `ImmutableQuerySet`, on the default manager `objects` and on related managers such as `run.revisions` |
| `update_or_create` | ends in `save()` or `update()` |
| Deleting a run or case that has revisions | `on_delete=PROTECT` |
| Service writes | `record_revision` and `record_approval` only insert; they never update or delete a revision or approval row |

**Not guarded (actual bypasses):**

- Raw SQL, `connection.cursor()`, and `QuerySet.raw()`.
- Private ORM internals: `QuerySet._raw_delete`, `QuerySet._update`, `Model._do_update`.
- `Model._base_manager`, which is a plain manager and not the guarded `objects`.
- A database administrator, or a migration.
- `WorkflowRun.current_revision` is an ordinary updatable column: `run.save()` or a queryset
  update on `WorkflowRun` can point it at any revision, including another run's. Only
  `record_revision` moves it correctly, and the service re-checks the pointer
  (`StoredDataError`) before the next write and on reads.

**There is no database-level immutability** (no trigger, no revoked privilege). The database
itself enforces only primary keys, foreign keys, uniqueness, and the CHECK constraints listed
above. See open question 2.18.

**Hash checking is not tamper protection.** On every load the domain recomputes the SHA-256
of the stored bytes and compares it with the stored hash. That detects accidental corruption
and a change to the bytes or to the hash alone. Anyone who can write the table can change the
payload and its hash together; the row stays internally consistent and is accepted (a
PostgreSQL test demonstrates this). Detecting that would need something outside the row, such
as a signature, an audit chain, or restricted database privileges.

### What persistence does not give you

- **Persistence alone does not make an export race-safe.** Writes to a run are serialized by
  the run-row lock, but `check_persisted_export_eligibility` is a plain read with no lock and
  no snapshot. The re-check at export commit is future work (open question 2.16).
- **No authentication or authorization.** `approver_id` is an **unverified domain actor
  identifier**, stored as data. It is not a foreign key to `accounts.User`
  (`accounts.User` is unchanged and no user mapping is invented), and a supplied UUID is not
  evidence of who approved. There is no `human_approved` flag, and `warnings_reviewed=True`
  remains a declaration. **Linking approvals to authenticated users, and authorizing the
  approver, is required before any API exposes approval operations** (open question 2.15).
- **Only valid revisions are stored** (open question 2.17). Rejections, the `needs_review`
  state guard, audit events, and the other deferred attributes are open question 2.19.

### Test status

DB-free (`tests/test_cases_persistence.py`, run in the VM): metadata and constraint
declarations, migration state, the immutability guards (which raise before any SQL), the pure
write rules (`check_revision_write`, `check_approval_write`), the row-to-domain adapters, and
the persisted eligibility adapter with its reads replaced by in-memory rows (policy decisions
and denial ordering preserved).

PostgreSQL (`tests/test_cases_persistence_postgres.py`, 32 tests): schema constraints, duplicate
numbers, per-run numbering, current-revision storage, approval binding, rollback on failure,
protected deletes, the raw-SQL bypasses (a hash-only change is detected on load; a consistent
payload-and-hash change is not), and **competing writers on one run on separate connections**.
Every class refuses to run unless the connection's database name starts with `test_` (Django's
dedicated test database). The writer tests bound every wait: a barrier timeout, a per-connection
`lock_timeout`, daemon threads with join timeouts, and a check that no thread is still alive,
so wrong locking surfaces as a failure instead of a hang; they assert the final stored rows and
the current-revision pointer. **NOT RUN:** there is no PostgreSQL on the development VM, so
database uniqueness, rollback, and the locking behavior are **unverified** until the owner runs
`python manage.py test --tag requires_postgres` locally with `AP_RUN_DB_TESTS=1`
(runbook, section 6, part F).

## Caller responsibilities (future application service)

- Authenticate users and authorize who may edit and approve (roles; whether one person may
  do both is open question 2.3).
- Generate the UUIDs and revision numbers and call the persistence services (uniqueness is
  enforced by the database; immutability only at the ORM and service boundary, see above).
- Decide which revision is current for a run and when approval is allowed (for example the
  run being in `needs_review`), and supply the current revision and the expected case and run
  when checking.
- Pass the source text whenever the draft has source references.
- Establish that warnings were actually reviewed before passing `warnings_reviewed=True`.
- Re-check eligibility atomically when the export is committed.
- Record audit events; none exist here.

## Not implemented

Endpoints, workers, the workflow state machine, rejection records, authentication and
authorization, audit events, export artifacts and file export, the atomic export-commit
re-check, database-level immutability, and approval invalidation events. The persistence of
the rules above is implemented but untested against a real database.

## Running the tests

From `backend\`, with the project's virtual environment. The policy tests are
standard-library `unittest`, no Django and no settings:

```powershell
python -m unittest tests.test_revision_approval_policy
```

The persistence tests need Django settings: `python manage.py test tests.test_cases_persistence`
(DB-free). `python manage.py test` runs everything that needs no PostgreSQL. Counts are only valid for the commit they were run
on; re-run before quoting them.
