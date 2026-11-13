# Review services slice (identity, roles, edit, approve, reject, audit)

> **Status: implemented; DB-free tests pass in the VM; the project owner reported passing local PostgreSQL
> results** (section 10.4: the 135 new tests and the combined 356, the user migration on a database with an
> existing user, and a manual smoke test). Those are **owner-supplied local results**: the development VM has no
> PostgreSQL, never connects to one, and did not run or observe them. They cover the tested scenarios only and
> are not a claim that no other race exists or that the system is production-ready. This slice has no REST API,
> no React UI, no product login mechanism, no export, and no real AI. The services are Python functions that
> take an already-authenticated user object.

## 1. Scope

In: the service layer that lets an authenticated person act on the request-intake workflow, and the
records that make those actions accountable.

```text
accounts.User.public_id                      stable public identity (UUID), safe migration
Operator / Approver / Auditor                Django groups + permissions, `manage.py setup_roles`
submit_request_as(user, text)                authorized submit               -> request.submitted
edit_request(user, run, base, draft)         next immutable revision         -> revision.created, request.edited
approve_request(user, run, rev, hash, acks)  Approval + run -> approved      -> approval.recorded, run.state_changed
reject_request(user, run, rev, hash, code)   Rejection, run stays needs_review -> rejection.recorded
get_review_summary(user, run)                what a reviewer needs to act
list_audit_events(user, run)                 the run's audit history
```

Every write is one database transaction that includes its audit events. Authorization is enforced inside
the services, not in a future view. Validation, canonicalization, hashing, the approval rules, the revision
write rules and the run state table are reused from `workflows` and `apps.cases` (section 5, "reuse"); this
slice adds no second implementation of any of them (static tests fail if it does).

Out (deferred; section 12): any API or UI, a login mechanism and session handling, per-case or per-team
assignment, export, run cancellation and retry, document upload, real AI, database-level immutability,
audit tamper-evidence, personal-data handling beyond keeping it out of logs and audit events.

## 2. Initial policy decisions (explicit; the owner can change them)

These are decisions of THIS slice, made so the services can be built. The architecture's role model is still
"to be confirmed" ([data-model.md](../architecture/data-model.md), open question 2.3); each decision below is
one place in the code and one set of tests, so changing it is small.

| # | Decision | Where it lives |
| --- | --- | --- |
| P1 | **Self-approval is forbidden.** A user may not approve a run if they submitted the request or authored any revision of it (an edit). | `review_rules.is_self_approval` |
| P2 | **A superuser bypasses role membership and the own-request scope, and nothing else.** Self-approval (P1), state guards, stale-revision and content-hash checks, warning acknowledgement and revision integrity apply to a superuser exactly as to anyone. An inactive superuser is refused. | `access.py`, `review_rules.py` |
| P3 | **Operators act on their own requests.** `edit` and `view` through the Operator role reach only requests the operator submitted. | `access.py`, `roles.py` |
| P4 | **Approver and auditor pools are installation-wide.** Any approver may approve or reject any request waiting for review (subject to P1); any auditor may view any request. There is no per-case or per-team assignment yet. | `roles.py` |
| P5 | **Approval and rejection only in `needs_review`.** | `review_rules.py` |
| P6 | **Edit only in `needs_review` and `approved`.** `exporting` is refused: export does not exist, and its cancellation protocol is not built. Every other state is refused. | `review_rules.py` |
| P7 | **Rejection leaves the run in `needs_review`** (the normative table in [first-workflow.md](../architecture/first-workflow.md) section 11). A rejection is a recorded decision, not a state. | `review.reject_request` |
| P8 | **A rejected revision cannot be approved.** The way forward is an edit, which creates a new revision. Several rejections of one revision are all kept. | `review_rules.approval_problem` |
| P9 | **An invalid edit is refused, not saved.** A draft with blocking findings (or one that cannot be serialized safely) creates no revision and changes nothing; the caller gets the findings back. This differs from the sentence "the new revision may itself be invalid; it is saved anyway" in first-workflow.md section 5, because a `ResultRevision` cannot hold blocking findings (open question 2.17). | `editing.py` |
| P10 | **An edit that does not change the content hash is refused** (`unchanged_content`): an identical revision would invalidate an approval for nothing. | `review.edit_request` |
| P11 | **Extractor ambiguities are not carried into an edit.** A human edit replaces the extractor's reading; the new revision's findings are exactly what validation reports for the edited draft. Every warning validation reports is stored and must be acknowledged at approval. | `editing.py` |
| P12 | **Warnings must be acknowledged exactly.** The acknowledgement must equal the current revision's warnings, as `(finding code, field path)` pairs: a missing warning and an unknown one are both refused. | `review_rules.approval_problem` |
| P13 | **Rejection reasons are a fixed list** (`incorrect_content`, `incomplete_content`, `not_supported_by_source`, `other`). No free text is accepted, stored, logged, or audited anywhere. | `intake.policy.REJECTION_REASONS` |
| P14 | **`submit_request` stays the unauthorized lower layer** (the 76 orchestration tests call it). `submit_request_as` is the authorized entry point. | `submission.py` |
| P15 | **Refused attempts are logged by code, not audited.** Their transaction rolls back, so an event written in it would be too. | `accounts/authz.py` |

## 3. User identity and the migration

`accounts.User` gains `public_id`, a random UUID (`uuid4`, unique, not null, not editable). The integer
primary key is unchanged and remains the foreign-key target. `public_id` is what domain records that store a
plain UUID use: `Approval.approver_id`, `Rejection.rejected_by`, audit events. It is generated by the model;
no caller supplies it. It is an identifier, not a credential. The admin class is unchanged.

Three migrations, each its own PostgreSQL transaction, so data and DDL never share one:

| Migration | Operation | What PostgreSQL does |
| --- | --- | --- |
| `accounts/0002_user_public_id` | `AddField`, nullable, no default | `ALTER TABLE ... ADD COLUMN "public_id" uuid NULL`. Metadata only; existing rows get NULL. |
| `accounts/0003_backfill_user_public_id` | `RunPython` (reverse is a no-op) | One `UPDATE ... WHERE id = <pk> AND public_id IS NULL` per user, each with its own `uuid4()`. Touches only rows still NULL, so it can be re-run. |
| `accounts/0004_user_public_id_constraints` | `AlterField` to unique, not null, default | `SET DEFAULT`, an `UPDATE ... WHERE public_id IS NULL` that touches **no row** after step 2, `SET NOT NULL`, `ADD CONSTRAINT ... UNIQUE`, `DROP DEFAULT`. |

Why not one migration with `default=uuid.uuid4`: Django would evaluate the callable once and give every
existing user the same value. The previewed SQL of step 3 shows the single-default `UPDATE`; it is harmless
only because step 2 already filled every row. If a NULL somehow remained, the unique constraint would refuse
the duplicate instead of accepting it. The DDL was previewed without a connection (`collect_sql`; the
parameterized `UPDATE` was rendered with a stand-in formatter), and `makemigrations --check --dry-run` reports
"No changes detected".

**Run these migrations with the application stopped.** Between steps 1 and 3 a user created by code that does
not know the column would get NULL, and step 3 would then fail (it would not damage anything).

New users created afterwards get a distinct `public_id` from the model default (`create_user`,
`create_superuser`, the admin, fixtures without the field).

### Local preflight before `migrate` (owner)

The VM cannot see the owner's database. (The owner later reported that it held 1 user; section 10.4.) Whatever the
number, the migration is written to be safe:

```powershell
& $Py manage.py showmigrations accounts audit cases intake
& $Py manage.py shell -c "from django.contrib.auth import get_user_model as g; U = g(); print('users', U.objects.count(), 'superusers', U.objects.filter(is_superuser=True).count())"
```

Expected on a development database: a handful of users. Take a dump first if the data matters (`pg_dump`, per
the runbook). Afterwards, verify distinctness (section 11, step 1). Reversal: `migrate accounts 0001` drops the column (the reverse of step 2 is a no-op, the reverse of step 3
relaxes the constraints). Nothing else depends on the column, and the other new tables reference the user by
its integer key, so no other migration has to be reversed first.

## 4. Roles, permissions, and scope

Roles are Django **groups**; `python manage.py setup_roles` (repeatable) creates them and sets each group's
permissions to **exactly** the set below. It needs a migrated database (`migrate` creates the permissions),
never creates a permission, never touches users, memberships or other groups, and prints only role names and
counts. A missing permission is a clear `CommandError` that changes nothing.

| Permission | Meaning | Operator | Approver | Auditor |
| --- | --- | :---: | :---: | :---: |
| `intake.submit_request` | submit a request | yes | | |
| `intake.view_own_request` | view requests the user submitted | yes | | |
| `intake.edit_own_request` | edit requests the user submitted | yes | | |
| `intake.view_any_request` | view any request | | yes | yes |
| `intake.approve_request` | approve a request waiting for review | | yes | |
| `intake.reject_request` | reject a request waiting for review | | yes | |
| `audit.view_auditevent` | read audit events | | | yes |

The Auditor holds only `view` permissions. A user may hold several roles; permissions are the union.

**A Django permission is global, so the permission alone never authorizes an action.** The name carries the
scope (`*_own_request`), and the service checks the stored record: `edit_request` loads the run's
`RequestSource.submitted_by` under the run lock and refuses with `out_of_scope` when an own-scope user is not
the submitter. The rules are pure functions (`apps/intake/access.py`), tested over every role, action and
ownership combination without a database. The permission codenames are declared on the unmanaged model
`intake.RequestSource` (Django creates a permission only for a model; the model's default
add/change/delete/view permissions are switched off) and a test compares them with `apps/accounts/roles.py`.
An unmanaged, table-less model was tried first and dropped: it made `manage.py dumpdata` fail. A database
that already ran the orchestration slice keeps the four unused default `requestsource` permission rows; no role
holds them and nothing checks them.

How a service authorizes (`apps/intake/authorization.py`, `apps/accounts/authz.py`):

1. `require_permission(user, action)` BEFORE anything is looked up or locked: reload the user **fresh** from
   the database (the object passed in is never trusted: not its `is_active`, `is_superuser`, cached
   permissions or `public_id`), require authenticated and active, require some permission for the action.
2. After the run row is locked: `authorize(user, action, submitter_pk=...)` reloads the actor again (a role or
   activation change committed while this call waited for the lock is honoured) and applies the scope.

Refusals raise `AuthorizationError(reason)` with `not_authenticated`, `inactive`, `missing_permission` or
`out_of_scope`. They are logged as `action`, `reason` and the actor's `public_id` only (never a username,
name, e-mail or request content; a test checks the log text). An unknown run is `RecordNotFoundError`, raised
after the permission check, so a user with no permission learns nothing about run ids; a user who holds the
permission can tell an unknown run from a request they are not scoped to (UUIDs are unguessable; accepted).

## 5. The services (`apps/intake/review.py`, `apps/intake/submission.py`)

All arguments are keyword-only; the acting user is `user`. There is **no parameter** through which a caller can
supply an approver, editor or rejecter identity (a test inspects the signatures): the identity is the freshly
loaded user's `public_id`, or the user row for `RevisionEdit.edited_by`.

### `edit_request(user, run_id, base_revision_id, draft, limits=None)`

One transaction: lock the run, load the source text, authorize with scope, check the state and the base
revision, build the next revision, write it, and write the audit events.

* `base_revision_id` is the revision the caller edited. It must be the run's **current** revision. It is
  `None` exactly for the **first edit of an invalid extraction**: the extraction produced an
  `ExtractionOutcome(status=invalid)` and no revision, the run is in `needs_review`, and an edit with
  `base_revision_id=None` creates **revision 1** (a `RevisionEdit` with no base; the extraction outcome stays
  as immutable history). `None` once a revision exists, or a base when none exists, is a stale base.
* The draft goes through `build_edit_revision` -> `create_request_intake_revision`: the same validation (with
  the stored source text, so source references are verified exactly as for an extraction), canonicalization
  and hashing as an extraction. Blocking findings or an unserializable value: `DraftInvalidError`
  (`blocking_findings` with every finding, or `not_serializable` with the path). Nothing is stored.
* The new revision is written by `record_revision` (number = highest + 1, pointer moved, under the lock).
  Earlier revisions and every approval and rejection are untouched.
* Run state: `needs_review` stays `needs_review`; `approved` returns to `needs_review` through
  `transition_run`, and an `approval.invalidated` event records that the earlier approval no longer matches
  the current revision. The approval row itself is not modified; "effective approval" stays derived.
* Result: `EditResult(revision_id, revision_number, content_hash, run_state, warnings, approval_invalidated)`.

### `approve_request(user, run_id, revision_id, content_hash, acknowledged_warnings=())`

Checks, after authentication, permission and scope, in this fixed order (the first failing one is reported):

| # | Refusal `reason` | Meaning |
| --- | --- | --- |
| 1 | `run_not_in_review` | The run is not in `needs_review`. |
| 2 | `no_current_revision` | The run has no revision (an invalid extraction no one has edited). |
| 3 | `self_approval_forbidden` (`SelfApprovalError`) | The user submitted the request or authored a revision of it. Superusers included. |
| 4 | `stale_revision` (`StaleRevisionError`) | `revision_id` is not the current revision. |
| 5 | `content_hash_mismatch` | `content_hash` is not the current revision's stored hash. |
| 6 | `revision_rejected` | The revision has a recorded rejection (P8). |
| 7 | `warnings_not_acknowledged` / `unknown_warning_acknowledged` (`WarningsNotAcknowledgedError`, carries the pairs) | The acknowledgement is not exactly the current warnings (P12). |

Then, in the same transaction: `approve_revision(current, approver_id=<actor.public_id>,
warnings_reviewed=True)` (the `True` is passed only after step 7 passed, so it is a fact about this call),
`record_approval` (which re-checks the binding to case, run, revision, hash and currency, and that the record
equals what the policy produces), `transition_run(needs_review -> approved)`, and the audit events. Earlier
approvals are never touched. A corrupted stored revision fails the domain's re-verification and is never
approved.

### `reject_request(user, run_id, revision_id, content_hash, reason_code)`

Refusals in order: `run_not_in_review`, `no_current_revision`, `stale_revision`, `content_hash_mismatch`,
`unknown_reason_code`. Then `record_rejection` (new, in `apps/cases/services.py`, mirroring `record_approval`)
inserts an immutable `cases.Rejection` bound to the revision (and through it to run and case) and to its
content hash, with `rejected_by = actor.public_id` and the stable `reason_code`. **The run stays in
`needs_review`.** The revision, its findings, and earlier approvals and rejections are untouched. A rejection
by the submitter of their own request is allowed (it is not an approval).

### `submit_request_as(user, text, client_request_id=None, now=None)`

Requires `intake.submit_request` and calls `submit_request` with the freshly loaded user as the submitter.
`submit_request` itself now also writes `request.submitted` in its transaction (a replayed submit writes none).

### Reads

`get_review_summary(user, run_id)` returns identifiers, state, the current revision's number, hash, findings
and warnings (or the invalid outcome's findings while there is no revision), and decision counts for the
current revision. It is a plain read: no lock, no snapshot across its queries. `list_audit_events(user,
run_id)` requires `audit.view_auditevent` (or a superuser).

### What is reused, not repeated

| Concern | Reused | Where this slice calls it |
| --- | --- | --- |
| Validation, source-reference checks, canonicalization, hashing | `create_request_intake_revision` | `editing.build_edit_revision` |
| Revision numbering, pointer, write rules | `apps.cases.services.record_revision` | `edit_request` |
| Approval policy (blocking findings, warnings, consistency) | `approve_revision` | `approve_request` |
| Approval binding and persistence | `apps.cases.services.record_approval` | `approve_request` |
| Run state table and transitions | `apps.cases.services.transition_run`, `lock_run` | all writes |
| Draft JSON, findings JSON | `draft_json`, `cases.adapters` | read service |

`tests/test_review_boundaries.py` reads the source with `ast`: the new modules name no canonicalization,
validation, hashing or JSON function and import no `hashlib`; `approve_revision` is imported from the domain
and never redefined (the orchestration slice's static ban on that name is narrowed to every module except
`review.py`); no service assigns `state`, `failed_step` or `current_revision`; `apps.cases` imports nothing
from `intake`, `audit` or `accounts`.

## 6. Transaction boundaries

| Operation | Transactions | Locks and order |
| --- | --- | --- |
| `edit_request` | One `atomic()`: lock the run; load source; authorize with scope; state and base checks; build; `record_revision`; `RevisionEdit`; (`transition_run`); audit events. | Run row first. `record_revision` and `transition_run` lock the same row again (harmless). Nothing else is locked for write. |
| `approve_request` | One `atomic()`: lock the run; authorize; checks; `approve_revision`; `record_approval`; `transition_run`; audit events. | Same. |
| `reject_request` | One `atomic()`: lock the run; authorize; checks; `record_rejection`; audit event. | Same. `record_rejection` read the revision before locking when called alone; here the run is already locked. |
| `submit_request` / `submit_request_as` | The existing single `atomic()`, now also containing the `request.submitted` event. | Unchanged. |
| `run_extraction`, T2 | The existing `atomic()`, now also containing `revision.created` (valid draft) and `run.state_changed`, by the `system` actor. | Unchanged. |
| `get_review_summary`, `list_audit_events` | No transaction, no lock. | None. |

Two operations on one run run one after the other and the second sees the first one's result. An edit racing
an approval has exactly two outcomes, both consistent: approved then edited (the approval stays on the old
revision, the run is back in `needs_review`, `approval.invalidated` recorded), or edited then a stale
approval refused. Either way no approval is bound to the current revision of a run the system calls
approved. The permission check before the lock means an unauthorized caller never queues on a run's lock.

An audit write that fails raises and rolls the whole operation back, by design: there is no operation without
its audit event. The permission and group state is read at the start and again right after the lock; a change
committed in between those two reads and the end of the operation is not seen by it.

## 7. Audit events (`apps/audit`)

Append-only records of significant actions, written by `apps.audit.recorder.record_event`, which **refuses to
run outside `transaction.atomic()`** (`AuditTransactionError`), so an event commits with its operation or is
rolled back with it.

| Action | Actor | Written by | `details` (the only keys allowed) |
| --- | --- | --- | --- |
| `request.submitted` | user | `submit_request` | `byte_length`, `client_request_id_used` |
| `revision.created` | system (extraction) or user (edit) | `run_extraction` T2, `edit_request` | `revision_number`, `content_hash`, `origin` (`extraction` or `edit`), `blocking_count`, `warning_count` |
| `request.edited` | user | `edit_request` | `base_revision_id` (null for a first edit), `first_edit` |
| `run.state_changed` | system or user | T2 (`extracting` -> `needs_review`), `approve_request` (`needs_review` -> `approved`), `edit_request` (`approved` -> `needs_review`) | `from_state`, `to_state`, `cause` |
| `approval.recorded` | user | `approve_request` | `revision_id`, `content_hash`, `warning_count` |
| `approval.invalidated` | user | `edit_request` on an approved run | `revision_id`, `by_revision_id`, `approval_count` |
| `rejection.recorded` | user | `reject_request` | `revision_id`, `content_hash`, `reason_code` |

Each row: an increasing id, `occurred_at`, `actor_kind` (`user` or `system`), `actor_public_id` (the user's
`public_id`; null for the system), `action`, `subject_type` and `subject_id`, `case_id`, `run_id`, `details`.
Not recorded: queue-level events (`task.failed` and similar), run states other than the review-relevant
transitions above (`queued -> extracting`, `failed`), and refused attempts (P15).

**Content restraint.** `details` is a flat object; a value is a boolean, an integer, null, or a short token of
letters, digits and `_ . : -`. A value with a space, a quote or any other character is refused, so request
text, extracted values, names, e-mail addresses and exception messages cannot be stored by accident. Keys are
fixed per action; a key name that could hold content (`text`, `name`, `email`, `message`, `excerpt`, ...) does
not exist. A PostgreSQL test scans a full flow's events for the request's content and the users' names.

**What "append-only" means here, and does not.** The ORM and service boundary only: `AuditEvent` is an
`ImmutableRecord`, so `save()` on an existing row, `delete()`, `update()`, `bulk_update()` and upserts raise
`ImmutableRecordError`. There is **no database-level protection**: no trigger and no revoked UPDATE or DELETE
privilege. Raw SQL, `QuerySet.raw()`, private ORM internals, a database administrator and a migration can
change or remove events, and nothing detects it (no hash chain). A PostgreSQL test pins this by changing a
row with raw SQL. `audit.view_auditevent` is the only permission Django creates for the model (no add,
change or delete permission exists). Open questions 2.5 and 2.18.

## 8. Models and migrations

| Migration | Operations | Purpose |
| --- | --- | --- |
| `accounts/0002_user_public_id` | `AddField` (nullable) | Section 3. |
| `accounts/0003_backfill_user_public_id` | `RunPython` | Section 3. |
| `accounts/0004_user_public_id_constraints` | `AlterField` | Section 3. |
| `audit/0001_initial` | `CreateModel` `AuditEvent` | Table with CHECK constraints on action, subject type, actor kind, and "a user actor has a public id, the system has none"; index on `(run_id, id)`. No dependency. |
| `cases/0005_rejection` | `CreateModel` `Rejection` | Immutable; FK to `ResultRevision` (PROTECT); CHECK: hash is 64 lowercase hex, reason code shape `^[a-z][a-z0-9_]{0,39}$`. |
| `intake/0002_revisionedit_request_permissions` | `AlterModelOptions`, `CreateModel` | `RequestSource` gets the six request permissions and loses its default permissions (options only, no DDL); `RevisionEdit` is new (immutable; one per revision; nullable base; author FK to the user, PROTECT; CHECK: a revision is not its own base). |

No existing table other than `accounts_user` is altered. The DDL of all six was previewed without a
connection (section 10).

## 9. Files

New: `backend/apps/accounts/` (`roles.py`, `authz.py`, `errors.py`, `management/commands/setup_roles.py`,
`migrations/0002..0004`); `backend/apps/audit/` (`apps.py`, `events.py`, `models.py`, `recorder.py`,
`migrations/0001_initial.py`); `backend/apps/cases/migrations/0005_rejection.py`;
`backend/apps/intake/` (`access.py`, `authorization.py`, `review_rules.py`, `editing.py`, `review.py`,
`migrations/0002_revisionedit_request_permissions.py`); `backend/tests/` (`test_review_roles.py`,
`test_review_rules.py`, `test_review_audit.py`, `test_review_identity.py`, `test_review_models.py`,
`test_review_boundaries.py`, `test_review_postgres.py`); this document.

Changed: `apps/accounts/models.py` (`public_id`), `apps/cases/models.py` (`Rejection`; the docstrings),
`apps/cases/services.py` (`record_rejection`), `apps/cases/errors.py` (`InvalidReasonCodeError`),
`apps/intake/models.py` (`RevisionEdit`, the permissions on `RequestSource`), `apps/intake/policy.py`
(`REJECTION_REASONS`), `apps/intake/errors.py` (review errors), `apps/intake/submission.py`
(`submit_request_as`, the audit event), `apps/intake/services.py` (T2 audit events),
`config/settings/base.py` (`apps.audit`); tests that pin deliberate changes: `test_settings.py` and
`test_request_intake_django.py` (the project-apps list), `test_user_model.py` (`public_id` is the one added
field), `test_intake_composition.py` (the source-file list, the intake migration list, and the narrowed
`approve_revision` ban), `test_cases_persistence.py` (the `Approval` docstring now points at the service);
status text in `CLAUDE.md`, `README.md`, the architecture documents and the runbook.

## 10. Validation

### 10.1 In the VM (no database), run from `backend\` with `..\.venv\Scripts\python.exe`

| Command | Result |
| --- | --- |
| `manage.py test` | `Ran 991 tests`, OK (834 before this slice plus 157 new; the 135 PostgreSQL tests are excluded by the project runner) |
| `manage.py test tests.test_review_roles tests.test_review_rules tests.test_review_audit tests.test_review_identity tests.test_review_models tests.test_review_boundaries` | 157 tests: roles, permission matrix and scope 24, review rules and edit builder 51, audit events and recorder 23, identity and user migrations 17, models and migrations 19, static boundaries 23 |
| `python -m unittest` on the pure-domain modules (no Django) | `Ran 523 tests`, OK |
| `manage.py check` | no issues |
| `makemigrations --check --dry-run` (history check patched out; no connection is opened) | `No changes detected` |
| `python -m compileall -q apps config tests manage.py workflows` | exit code 0 |
| `git diff --check`, and a scan of the new files for CRLF, trailing whitespace, missing final newline | clean |
| Link check over the changed documents | every relative link resolves |
| The PostgreSQL test module, collected without `AP_RUN_DB_TESTS` | 0 tests run, no connection |

The migration DDL was previewed with Django's `collect_sql` and no connection (section 3, section 8).

### 10.2 What a throwaway in-memory SQLite harness showed (NOT evidence about PostgreSQL)

As in the orchestration slice, the ORM-level logic of the PostgreSQL tests was run once against a throwaway
SQLite database configured **outside the repository** (nothing committed). SQLite has no row locks and a
different constraint and migration engine, so a pass proves only that the Python and ORM logic and the test
fixtures are internally consistent. 128 of the 135 tests could run that way and passed (after one fixture
defect in a test was fixed). **Mutation check:** with each of six safeguards switched off in turn (the
self-approval rule, the scope check, the content-hash check, the stale-base check, the warning
acknowledgement, and fresh reloading of the actor), the same tests failed in every case, so they do detect the
defects they name. The **7 tests it could not run** are the `ConcurrencyTests` (their helper issues the
PostgreSQL-only `SET lock_timeout`). They could not run in the VM; they are among the 135 tests the owner
reported passing against PostgreSQL (section 10.4).

### 10.3 PostgreSQL tests (owner-run locally, section 10.4): `backend/tests/test_review_postgres.py`, 135 tests, tagged `requires_postgres`

`SchemaTests` 7, `RolesCommandTests` 7, `PublicIdTests` 5, `UserMigrationTests` 6, `AuthorizationTests` 8,
`SubmitAsTests` 7, `EditServiceTests` 27, `ApprovalServiceTests` 27, `RejectionServiceTests` 15,
`ReadServiceTests` 6, `ExtractionAuditTests` 4, `AuditRecordTests` 7, `RecorderTransactionTests` 2,
`ConcurrencyTests` 7. They cover: the user migration applied to a database that already holds users (every
user gets a distinct id, nothing else changes, step by step, a kept value, duplicates refused by step 3,
reversal); `setup_roles` (exact permissions, repeatable, drift repair, other groups and memberships untouched);
every role against every service on a real request, including unauthenticated, deleted, inactive,
deactivated-after-loading, role-removed-after-loading (despite a cached permission) and demoted callers;
authorized and unauthorized edits; stale base revisions; the first edit of an invalid extraction; invalid,
unchanged and unserializable drafts; source references and warnings preserved; approval bound to revision and
hash; self-approval by submitter, by an author, by a superuser; exact warning acknowledgement; historical
revisions and approvals preserved (and an old approval not authorizing a newer revision, through the existing
export-eligibility check); rejection persistence and its resulting state; audit events of every operation,
their order, their content restraint, and their rollback together with the operation (an audit failure at
each position, and a failed state change); and, in `ConcurrencyTests`: two edits of one revision, five edits
of one revision, an edit racing an approval, two approvals, a rejection racing an approval, and independent
runs not blocking each other. Each class refuses to run unless the database name starts with `test_`.

### 10.4 Local PostgreSQL results (owner-supplied; NOT run in the VM)

The project owner ran the steps of section 11 on the local Windows computer and reported:

| Item | Reported result |
| --- | --- |
| The 135 review PostgreSQL tests (`tests.test_review_postgres`) | OK, exit code 0 |
| All PostgreSQL-required tests together (221 earlier + 135 new) | 356 tests, OK, exit code 0 |
| `User.public_id` migration on the development database | 1 existing user, 0 missing public ids, 1 distinct public id |
| New migrations (`accounts` 0002 to 0004, `audit` 0001, `cases` 0005, `intake` 0002) | All applied |
| `makemigrations --check --dry-run` | No changes detected |
| `manage.py setup_roles` | Completed |

Manual smoke test, as reported:

1. An operator submitted a request.
2. The worker processed the extraction task successfully; the run reached `needs_review` with revision 1.
3. An approval attempt by the operator was refused with `missing_permission`.
4. An approver approved the revision; the final run state was `approved`.
5. The audit actions of the run, in order: `request.submitted`, `revision.created`, `run.state_changed`,
   `approval.recorded`, `run.state_changed`.

How the smoke test was run: through Django's interactive shell (`manage.py shell`), with the Python code passed
in PowerShell here-strings, as in the section 11 scripts. The owner supplied the actual console output, and the
steps above are the owner's summary of it. The test, migration and role-setup results were reported without a log. None of the
output is stored in the repository, and the VM did not run, repeat or observe any of it (see the
[local validation record](local-validation-record.md)). What it does and does not show:

* It supports the tested scenarios on the owner's PostgreSQL 18 development database: the three-step user
  migration with one existing user, the constraints, `setup_roles`, the service rules, the transaction and
  rollback behavior, and the competing-connection tests, as the tests define them.
* It does **not** show how the user migration behaves with many existing users on PostgreSQL (only 1 user
  existed; distinctness for several users is covered by DB-free and SQLite-harness tests only), that no other
  race exists, anything about a production database, or production readiness.
* The smoke test was a manual, single-request run in the interactive shell. It shows the operator-refused and approver-approved path and the five audit actions listed
  above. It did not exercise edit, rejection, stale revisions, warning acknowledgement or self-approval (the
  PostgreSQL tests cover those), and no REST API, login, or UI was involved: the caller was whichever user the
  shell session acted as.

## 11. Local validation steps (owner; on the local Windows computer only)

These steps were run by the owner; the reported results are in section 10.4. They remain the procedure for
repeating the validation.

Use the runbook's setup (`$LocalProject`, `$Py`, `AP_DB_*`), a dedicated development server, no `--keepdb`,
no `--noinput`, and remove the flag afterwards. Stop anything that uses the development database first
(section 3: run the user migration with the application stopped).

```powershell
Set-Location "$LocalProject\backend"
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set. Remove-Item Env:AP_RUN_DB_TESTS and repeat." }

# 0. Preflight (section 3): count existing users; take a dump if the data matters.
& $Py manage.py showmigrations accounts audit cases intake
& $Py manage.py shell -c "from django.contrib.auth import get_user_model as g; U = g(); print('users', U.objects.count())"

# 1. Apply the migrations to the development database, then verify the identities.
& $Py manage.py migrate                            # applies accounts 0002-0004, audit 0001, cases 0005, intake 0002
& $Py manage.py makemigrations --check --dry-run   # expect: No changes detected
& $Py manage.py shell -c "from django.contrib.auth import get_user_model as g; U = g(); ids = list(U.objects.values_list('public_id', flat=True)); print('users', len(ids), 'distinct', len(set(ids)), 'null', ids.count(None))"   # expect: distinct == users, null 0
& $Py manage.py setup_roles                        # expect: Operator/Approver/Auditor: created, 3/3/2 permissions
& $Py manage.py setup_roles                        # run it again: expect "updated", same counts

# 2. DB-free tests.
& $Py manage.py test                               # expect: Ran 991 tests, OK

# 3. The new PostgreSQL tests only. Creates and drops test_<AP_DB_NAME>.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_review_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 135 tests, OK, none skipped, exit code 0
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 4. Everything that needs PostgreSQL (221 earlier + 135 new = 356), to prove nothing regressed.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 356 tests, OK
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }
```

Manual smoke test (writes two users, one case, run, source, task, outcome, revisions, an approval and audit
events to the development database; the users get no password):

```powershell
# 5a. Two users with roles, and a submission by the operator. Prints the run id.
@'
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from apps.intake.submission import submit_request_as
U = get_user_model()
op, _ = U.objects.get_or_create(username="smoke-operator")
ap, _ = U.objects.get_or_create(username="smoke-approver")
op.groups.add(Group.objects.get(name="Operator"))
ap.groups.add(Group.objects.get(name="Approver"))
result = submit_request_as(user=op, text="Requester: Jane\nItem: 2 | boxes | A4 paper\n")
print("RUN", result.run.pk)
'@ | & $Py manage.py shell
& $Py manage.py runworker --once                   # expect: Task <id> succeeded.

# 5b. Review: the operator may not approve; the approver may. Put the run id from 5a below.
$RunId = "<run id from 5a>"
@"
import uuid
from django.contrib.auth import get_user_model
from apps.accounts.errors import AuthorizationError
from apps.intake import review
U = get_user_model()
op, ap = U.objects.get(username="smoke-operator"), U.objects.get(username="smoke-approver")
run_id = uuid.UUID("$RunId")
s = review.get_review_summary(user=ap, run_id=run_id)
print("STATE", s.state, "revision", s.current_revision_number, "warnings", s.warnings)
try:
    review.approve_request(user=op, run_id=run_id, revision_id=s.current_revision_id, content_hash=s.content_hash, acknowledged_warnings=s.warnings)
except AuthorizationError as error:
    print("OPERATOR REFUSED", error.reason)
a = review.approve_request(user=ap, run_id=run_id, revision_id=s.current_revision_id, content_hash=s.content_hash, acknowledged_warnings=s.warnings)
print("APPROVED", a.run_state)
from apps.audit.recorder import list_run_events
print([e.action for e in list_run_events(run_id)])
"@ | & $Py manage.py shell
# expect: STATE needs_review revision 1 ...; OPERATOR REFUSED missing_permission; APPROVED approved;
#         ['request.submitted', 'revision.created', 'run.state_changed', 'approval.recorded', 'run.state_changed']
```

Report the exact `Ran N tests` lines, any failure, and the output of the preflight and the smoke test. If a
concurrency test fails or hangs, report it as it is; do not loosen it (hangs are bounded by lock and join
timeouts of 15 s and 60 s). The 356 in step 4 assumes the earlier 221 still hold; report the number you get if
it differs.

## 12. Deferred

* **API and UI.** No endpoint, serializer, view, template or screen. A future API must call the services in
  `apps.intake.review` and must not repeat or skip their checks; the DRF defaults (authenticated, session
  authentication, JSON) already apply to any new view, and the public-endpoint inventory test still lists
  only the health views.
* **A login mechanism** (login view, session lifecycle, password reset, lockout, multi-factor): not built.
  Django's admin login exists for staff. The services take a user the caller has authenticated.
* Per-case or per-team assignment, an administrator role, a configurable policy for self-approval, and
  delegation (open question 2.3).
* Export (`approved -> exporting -> exported`), run cancellation, and manual retry of a failed run.
* Document upload, real AI extraction, company configuration, the `exporting` edit path.
* Audit retention, hash chaining, an insert-only database role, database triggers (open questions 2.5, 2.18).
* A per-warning record of who reviewed what and when (open question 2.15): the approval records the warning
  codes and paths the approver acknowledged, and who approved, and nothing about how long or how carefully.

## 13. Remaining security limitations

* **No authentication exists in the product yet** (section 12). `load_actor` rejects a non-user, an
  unauthenticated, a deleted and an inactive user, but it cannot know that the caller really is that user.
* **PostgreSQL behavior is supported for the tested scenarios only** (owner-supplied local results, section
  10.4): locking, constraints and concurrency as the 135 tests exercise them, and the user migration on a
  database with ONE existing user. It is not proven for many users, other races, or a production database.
* **No production-readiness claim.** This is a development-database result for a development slice.
* **ORM-level immutability only** for revisions, approvals, rejections, authorship records, extraction
  outcomes, sources and audit events. Raw SQL, a database administrator, and a migration can change or delete
  them; the hash check detects a change to the bytes or the hash alone, not both; there is no audit hash chain
  and no restricted database role.
* Permission and group changes committed after a service has re-read the actor, and before it commits, are not
  seen by that operation (a window of one transaction).
* `submit_request` and the services of `apps.cases` are unauthorized lower layers; only code that calls the
  `apps.intake` services is authorized. Nothing stops other Python code in the process from calling them.
* The approver and auditor pools are installation-wide (P4): any approver may approve any other user's
  request. There is no segregation by team, company or case, and no second approver.
* A user who holds a superuser flag can edit and view anything and approve anything they did not author (P2).
  Superuser membership is not audited.
* Authorization refusals are logged but not audited, so a probing user leaves a log line, not an audit event.
* The source text, drafts, revisions and findings are plaintext in PostgreSQL; `get_review_summary` returns
  findings that may quote the source text to anyone who may view the request.
* `public_id` is in every audit event and approval; it is an identifier with no personal data, but anyone who
  can read the user table can map it to a person.
* The rejection reason list and the self-approval rule are provisional (section 2).
