# Revision, approval, and export-eligibility policy (database-independent)

> **Status: pure-Python policy rules only.** There is no persistence, endpoint, worker,
> file export, authentication, authorization, audit trail, or workflow state machine, and
> nothing here is a completed approval phase. Phase 2 database acceptance is still pending
> the owner's local PostgreSQL validation. Everything below was run only in the development
> VM, DB-free.

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

## Caller responsibilities (future application service)

- Authenticate users and authorize who may edit and approve (roles; whether one person may
  do both is open question 2.3).
- Generate the UUIDs and revision numbers, persist revisions and approvals, and enforce
  uniqueness and immutability in the database.
- Decide which revision is current for a run and when approval is allowed (for example the
  run being in `needs_review`), and supply the current revision and the expected case and run
  when checking.
- Pass the source text whenever the draft has source references.
- Establish that warnings were actually reviewed before passing `warnings_reviewed=True`.
- Re-check eligibility atomically when the export is committed.
- Record audit events; none exist here.

## Not implemented

Persistence, Django models and migrations, endpoints, workers, the workflow state machine,
rejection records, authentication and authorization, audit events, export artifacts and file
export, and approval invalidation events. These rules are untested against a real database.

## Running the tests

From `backend\`, with the project's virtual environment. Standard-library `unittest`, no
Django and no settings:

```powershell
python -m unittest tests.test_revision_approval_policy
```

`python manage.py test` also runs them. Counts are only valid for the commit they were run
on; re-run before quoting them.
