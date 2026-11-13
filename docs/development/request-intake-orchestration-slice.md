# Request-intake orchestration slice (text request to `needs_review`)

> **Status: implemented; DB-free tests pass in the VM. The project owner has reported (2026-11-13, no
> output attached to the repository) that the 76 new PostgreSQL tests and the combined run of all 221
> PostgreSQL-required tests passed locally, and that a manual smoke test succeeded: request, worker, valid
> outcome, revision 1, `needs_review`.** That covers the tested scenarios only. The development VM has no
> PostgreSQL and never connects to one; it did not observe the run (see the
> [local validation record](local-validation-record.md)). As merged, this slice has no authentication, no
> authorization, no API, no UI, and no real AI; it extracts with the deterministic fixture parser only.
>
> **Update.** The [review-services slice](review-services-slice.md) was built on top of this one and adds
> authorization, edit, approval, rejection and audit events. The "out of scope" and "limitations" statements
> below, and the PostgreSQL-status wording in sections 10 to 12, describe this slice as it was merged.

## 1. Scope

In: the path

```text
text -> submit_request -> Case + WorkflowRun(queued) + RequestSource + Task + StepTask   (one transaction)
     -> worker -> request_intake.extract handler -> deterministic extraction -> validation -> canonicalization
     -> ExtractionOutcome (+ ResultRevision number 1 when valid) -> WorkflowRun needs_review
     -> (task failed for good) reconciler -> WorkflowRun failed, failed_step "extract"
```

The new app is `apps/intake`. It reuses `apps.cases` (case, run, `record_revision`), `apps.tasks`
(`enqueue_task`, the worker, the failure classes), `workflows.request_intake` (extractor, validation,
canonicalization) and `workflows.revision_approval` (`create_request_intake_revision`). It adds no
second queue, no second workflow engine, and no validation, canonicalization, or hashing of revision
content (tests fail if the app names those functions).

Out (deferred): authentication and authorization (who may submit is not checked; `submitted_by` is only
recorded), any API or UI, review, edit, approval, rejection, export, cancellation of a run, manual retry
of a failed run (the state table allows `failed -> queued`; no code does it), audit events, document
upload and managed storage, company configuration (no validation limits are configured), real AI, a
heartbeat for long handlers.

## 2. Design decisions

| Topic | Decision | Why / trade-off |
| --- | --- | --- |
| Where | New app `apps.intake`. `apps.tasks` and `apps.cases` stay generic: `apps.tasks` imports no business code (a test checks it), `apps.cases` imports no request-intake code. | Both are guarded by existing static tests. The workflow wiring needs the queue and the domain, so it lives above both. |
| Task payload | `{"run_id": "<uuid>"}` and nothing else; the stored payload is validated again by the worker. | No request text in `Task.payload`, in the idempotency key, in the fingerprint, or in the task result (identifiers and a status only). This removes the queue's "do not enqueue sensitive data" limitation for this workflow. |
| Source text | New immutable `RequestSource` table, one per run. Exact text, SHA-256 of its UTF-8 bytes, byte length. Limit **65,536 bytes** (provisional, not measured; open question 2.2). | The managed `Document` storage of the data model does not exist yet. Plaintext business content in PostgreSQL, like revision bytes. NUL and lone surrogates are refused; the text is never trimmed or normalized. |
| Invalid drafts | `ExtractionOutcome` with status `valid` or `invalid`. A valid outcome points at revision 1; an invalid one has no revision and keeps the draft, ambiguities and all findings as plain JSON. | A `ResultRevision` cannot hold blocking findings and canonicalization refuses them (open question 2.17). The run still goes to `needs_review`: an invalid draft is a normal outcome, not a failure. |
| Run to task link | New `StepTask(run, step, generation, task)`, unique on `(run, step, generation)`. | `Task` must stay unaware of runs. The idempotency key is `request_intake.extract:<run uuid>:<generation>`. Only generation 1 is created today. |
| Run state | `WorkflowRun.state` and `failed_step`; changed only by `apps.cases.services.transition_run` under the run row lock. | Row locking is the pattern `record_revision` already uses. The data model's "optimistic version counter" is not used. |
| Handler allowlist | `apps.tasks.handlers.HANDLERS` is unchanged (still only `test.add_numbers`). `apps.intake.registry.PRODUCTION_REGISTRY` is a read-only mapping of that plus `request_intake.extract`. `config/task_registry.py` is the composition root; `runworker` installs it. | Fixed names, no dotted paths, no dynamic import, nothing registered at run time. `test.add_numbers` stays in the composed allowlist, as before; dropping it from production is an open decision. |
| Failure reconciliation | `reconcile_runs` runs after every worker iteration (a hook installed by the composition). It moves a run to `failed` only when the newest extraction task is `failed`. | A task failing is recorded by the generic queue, which cannot know runs. The reconciler is idempotent, so a crash between the task failing and the run moving is repaired by the next iteration. |
| Case reference | `REQ-YYYYMMDD-XXXXXXXX` (UTC date, eight random hex digits); a collision is retried up to three times. | Human-readable and unique by the database constraint. A sequential number would need a counter row that serializes every submission. |
| Client request id | Optional UUID per user: the same id and text returns the original run; the same id with other text is `ClientRequestConflictError`. | Makes a repeated submit safe. Enforced by a partial unique index; the loser of a race reads the winner's row. |

## 3. Models and migrations

All new tables use UUID primary keys and `on_delete=PROTECT` foreign keys.

| Model (table) | Columns | Database constraints |
| --- | --- | --- |
| `cases.WorkflowRun` (existing; extended) | `state` (default `queued`), `failed_step` (default empty) | `cases_run_state_valid` (the eight states), `cases_run_failed_step_valid`, `cases_run_failed_step_matches_state` (a failed step is set exactly when the state is `failed`) |
| `intake.RequestSource` (`intake_requestsource`), immutable | `run` (one-to-one), `submitted_by` (FK `AUTH_USER_MODEL`), `text`, `text_sha256`, `byte_length`, `client_request_id` (nullable), `created_at` | unique `run`; unique `(submitted_by, client_request_id)` where the id is set; hash is 64 lowercase hex; `1 <= byte_length <= 65536` |
| `intake.StepTask` (`intake_steptask`) | `run`, `step` (`extract`), `generation`, `task` (one-to-one to `tasks.Task`), `created_at` | unique `task`; unique `(run, step, generation)`; step in the vocabulary; `generation >= 1` |
| `intake.ExtractionOutcome` (`intake_extractionoutcome`), immutable | `run` (one-to-one), `status`, `revision` (nullable one-to-one to `cases.ResultRevision`), `extractor_name`, `extractor_version`, `draft` (JSON), `ambiguities` (JSON), `findings` (JSON), `created_at` | unique `run` (one outcome per run); unique `revision`; status in (`valid`, `invalid`); `valid` exactly when a revision is set |

Immutability of `RequestSource` and `ExtractionOutcome` is at the ORM and service boundary only, like
`ResultRevision` and `Approval` (see the docstring of `apps/cases/models.py`); raw SQL, an
administrator, and a migration are not stopped. `apps.tasks` gained no column and no migration.

Migrations (all hand-checked with `makemigrations --check --dry-run`, "No changes detected"; none has been
applied to any database):

| Migration | Operations | Purpose |
| --- | --- | --- |
| `cases/0002_workflowrun_state` | `AddField` x2 (with defaults) | Adds the columns. Existing rows become `queued` with no failed step. Nothing dropped or altered. |
| `cases/0003_backfill_run_state` | `RunPython` (reversible, a no-op in reverse) | Sets `state = needs_review` only where `state = 'queued'` and the run already has a current revision. |
| `cases/0004_workflowrun_state_constraints` | `AddConstraint` x3 | The CHECK constraints, after the data is consistent. |
| `intake/0001_initial` | `CreateModel` x3 | The new tables. Depends on `cases 0004`, `tasks 0001` and the user model. |

Why three `cases` migrations: each migration is its own PostgreSQL transaction, so the data update never
shares a transaction with a table alteration (PostgreSQL can refuse an `ALTER TABLE` after row updates on
a table with deferred foreign-key triggers in the same transaction).

The `intake` DDL was previewed without a database connection (Django's `collect_sql`): `CREATE TABLE`
statements with the CHECK, UNIQUE and partial-unique definitions above, `jsonb` columns, deferrable
foreign keys, and one extra index per foreign key. The `cases 0004` DDL is three
`ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)` statements. `cases 0002` is Django's usual
`ADD COLUMN ... DEFAULT ... NOT NULL` followed by `DROP DEFAULT` (not previewed: it needs a connection).

### Existing-row safety, and what is not known

The VM cannot see the owner's local database, so it is **not known** whether `cases_workflowrun` already
holds rows there. The migrations are written so that either answer is safe:

* A run without a revision becomes `queued`. It has no `StepTask`, so the reconciler (which starts from
  `StepTask` rows) never touches it, and it is never extracted (no task exists for it).
* A run with a current revision becomes `needs_review`, the only consistent state for it. No approval or
  export state is inferred; a run that someone approved by hand through `record_approval` is still shown as
  `needs_review` until a later slice defines what to do with it.
* No row is deleted, no column is dropped or altered, and every new column has a default.

**Local preflight before applying `cases 0002` to the development database** (read-only; from `backend\`
with the runbook's `$Py` and `AP_DB_*` variables; do not run it against a database you do not own):

```powershell
& $Py manage.py showmigrations cases intake tasks
& $Py manage.py shell -c "from apps.cases.models import Approval, WorkflowRun; print('runs', WorkflowRun.objects.count(), 'with_revision', WorkflowRun.objects.filter(current_revision__isnull=False).count(), 'approvals', Approval.objects.count())"
```

Expected on a development database that only ever held test data: small numbers. If `with_revision` is
greater than zero, those runs become `needs_review` (see above); if `approvals` is greater than zero, note
it in the report. Take a dump first if the data matters (`pg_dump`, per the runbook). Reversal:
`migrate cases 0001` (after `migrate intake zero`) drops the new columns and tables; reversing `0003` is a
no-op because the columns are dropped by reversing `0002`.

## 4. Transaction boundaries

| Operation | Transactions | Locks |
| --- | --- | --- |
| `submit_request` | One `atomic()`: case (reference retried in a savepoint), run, source, `enqueue_task` (a savepoint inside it), `StepTask`. All or nothing; the worker cannot see the task before commit. | None beyond the inserts. A repeated client request id is resolved by the unique index. |
| Claim, complete, fail, recover | Unchanged (`apps.tasks.services`). | Task row only. |
| `run_extraction`, T1 | One short `atomic()`: lock the run; return the existing outcome if there is one; otherwise `queued/extracting -> extracting`, read the text. | Run row. |
| `run_extraction`, extraction | **No transaction.** Extractor, validation, canonicalization. | None (a test probes the run row from a second connection while the extractor runs). |
| `run_extraction`, T2 | One `atomic()`: lock the run again; return the existing outcome if there is one; discard if the run is no longer `extracting`; `record_revision` (valid only); insert the outcome; `extracting -> needs_review`. | Run row. `record_revision` and `transition_run` lock the same row again, which is harmless. |
| `reconcile_runs` | One short `atomic()` per run, conditions re-checked under the lock. | Run row, then a plain read of the task. |

Lock order everywhere: run row first; the task row is locked only by the queue's own services, and never
while a run lock is held for write by this app.

## 5. State transitions

The vocabulary and table are in `apps/cases/run_state.py` (pure, standard library only) and follow
`first-workflow.md` section 11 with two documented differences. The transitions this slice performs are
marked.

| From | To | Performed by this slice | Notes |
| --- | --- | --- | --- |
| (new) | `queued` | `submit_request` (the column default) | |
| `queued` | `extracting` | handler T1 | |
| `extracting` | `extracting` | handler T1 on a retry or after a crash | **Difference 1.** The queue retries invisibly to the run, so the run is not moved back to `queued` when an attempt fails; the specification's `extracting -> queued` is not implemented. |
| `extracting` | `needs_review` | handler T2 (valid or invalid draft) | |
| `queued` or `extracting` | `failed` (`failed_step = extract`) | reconciler | **Difference 2.** `queued -> failed` is allowed because a task can fail for good before its handler ever ran (an unknown kind or an invalid payload is refused by the worker). |
| `needs_review`, `approved`, `exporting`, `exported`, `cancelled`, and `failed -> queued` / `approved` | | not performed | In the table so the database constraint need not change; a failed run leaves `failed` only by retrying the step that failed. |

Every change goes through `transition_run`, which applies `transition_problem` to the stored state while the
run is locked and raises `InvalidTransitionError` (with a stable reason) otherwise. State is business state:
a task attempt failing never changes it.

## 6. Idempotency and retries

* One `ExtractionOutcome` per run: a unique one-to-one in the database, plus a check under the run lock in
  both T1 and T2.
* A repeated call returns the **existing** outcome (`created=False`) and writes nothing, so the task result
  is identical for the first call and every repeat. It never creates a second revision and never overwrites
  the first committed result (a test compares every column before and after, with a different extractor on
  the second call).
* Crash after T2 but before the queue records success: the lease expires, recovery requeues the task, the
  next attempt finds the outcome and completes the task with the same result. A late stale worker either
  loses to the outcome or finds the run no longer `extracting`.
* Two concurrent calls: the second waits on the run lock in T2, then returns the first one's outcome and
  discards its own computation.
* **Fencing limit.** The queue's lease token protects the queue's own bookkeeping, not this handler's database
  writes: a stale worker can still reach T2. With the deterministic extractor its result would be identical;
  with a non-deterministic extractor (a later slice) the first committed outcome stands and the later one is
  discarded.
* Enqueue idempotency: key `request_intake.extract:<run>:1` through the queue's existing mechanism.

## 7. Failure behavior

| Situation | What happens |
| --- | --- |
| Text refused at submit (blank, NUL, lone surrogate, over 65,536 bytes) | `RequestTextError(reason)`; nothing stored. The message never echoes the text. |
| Extractor or database error in the handler | Propagates; the queue records a retryable `handler_error`, requeues with backoff while attempts remain (3 by default). The run stays `extracting`; **not** failed. |
| Run or stored text missing, a value that validates but cannot be serialized safely (`SerializationSafetyError`), a draft value the JSON form cannot store | `TaskPermanentError(rejected)`: the task fails at once; the reconciler then fails the run. |
| Attempts exhausted | Task `failed` (`handler_error`); the reconciler sets `state = failed`, `failed_step = extract` in the same worker iteration. |
| Run cancelled before or during extraction | The call returns `discarded` (a succeeded task whose result names only the state) and writes nothing. No code cancels a run yet; this guards the future one. |
| Worker not running | The run stays `queued`; nothing detects it (no supervisor, no heartbeat). |

Only safe codes are stored (`rejected`, `handler_error`, ...) and only the exception class name is logged,
exactly as in the queue slice. A test asserts that an exception message does not appear in any stored row.

## 8. Worker integration

`apps/tasks/worker.py` gained `Composition(registry, after_iteration)`, `install_composition`, and a hook
call after every `process_one`; with no composition installed, behavior is unchanged. `runworker` imports
`config.task_registry.COMPOSITION` and installs it before working. That is the whole change to `apps.tasks`:
no new file, no business import (a test allows exactly one project import outside `apps.tasks` there,
`config.task_registry`). An explicit `registry=` argument still wins, which keeps the process-level recovery
tests (they wrap `process_one` with their own registry) unchanged. A failing hook is logged by class and
does not stop the worker; a database error from it reaches the worker loop's existing handling.

## 9. Files

New: `backend/apps/intake/` (`__init__.py`, `apps.py`, `policy.py`, `errors.py`, `draft_json.py`,
`extraction.py`, `models.py`, `submission.py`, `services.py`, `handlers.py`, `registry.py`,
`migrations/0001_initial.py`); `backend/apps/cases/run_state.py`; `backend/apps/cases/migrations/`
`0002_workflowrun_state.py`, `0003_backfill_run_state.py`, `0004_workflowrun_state_constraints.py`;
`backend/config/task_registry.py`; `backend/tests/test_intake_policy.py`, `test_intake_extraction.py`,
`test_intake_composition.py`, `test_intake_postgres.py`; this document.

Changed: `apps/cases/models.py` (state fields and constraints), `apps/cases/services.py`
(`lock_run`, `transition_run`), `apps/cases/errors.py` (`InvalidTransitionError`), `apps/tasks/worker.py`
and `apps/tasks/management/commands/runworker.py` (the composition point), `config/settings/base.py`
(`apps.intake`), `tests/test_settings.py` and `tests/test_request_intake_django.py` (the project-apps list),
`tests/test_tasks_worker.py` (restores the installed composition after the command tests), and status text
in `CLAUDE.md`, `README.md` and the architecture and development documents.

## 10. Validation

### 10.1 In the VM (no database), run from `backend\` with `..\.venv\Scripts\python.exe`

| Command | Result |
| --- | --- |
| `manage.py test` | `Ran 834 tests`, OK (719 before this slice plus 115 new; the 76 PostgreSQL tests are excluded by the project runner) |
| `manage.py test tests.test_intake_policy tests.test_intake_extraction tests.test_intake_composition` | 115 tests: policy and run-state table 31, draft JSON and extraction 26, composition, handler boundaries, models, migrations and static source rules 58 |
| `python -m unittest tests.test_intake_policy tests.test_intake_extraction` (no Django) | OK |
| `manage.py check` | no issues |
| `makemigrations --check --dry-run` (history check patched out; no connection is opened) | `No changes detected` |
| `python -m compileall -q apps config tests manage.py workflows` | exit code 0 |
| The PostgreSQL test module, collected without `AP_RUN_DB_TESTS` | 76 tests, all skipped, no connection |

### 10.2 What a throwaway in-memory SQLite harness showed (NOT evidence about PostgreSQL)

While writing the tests, the ORM-level logic was also exercised once against a **throwaway in-memory SQLite
database, configured outside the repository** (no PostgreSQL, no SQLite in the project, nothing committed).
SQLite has no row locks and a different constraint and migration engine, so a pass there proves only that the
Python and ORM logic is internally consistent. 68 of the 76 PostgreSQL tests could run that way and passed
(schema, transitions, submission, extraction, worker, crash-after-persistence, reconciliation, the
existing-row migration). It found one real defect that the DB-free tests had missed: a task failing before the
handler ran leaves the run `queued`, and the table had no `queued -> failed` (fixed; see section 5). The
**8 tests it could not run** are the ones that need real concurrency or the real command: all 6
`ConcurrencyTests`, `ExtractionHoldsNoTransactionTests`, and the `runworker --once` test. Those, and the
locking they cover, have never run anywhere.

### 10.3 PostgreSQL tests (written, not run): `backend/tests/test_intake_postgres.py`, 76 tests, tagged `requires_postgres`

`SchemaTests` 9, `TransitionServiceTests` 5, `SubmissionTests` 15, `ExtractionServiceTests` 17,
`ExtractionHoldsNoTransactionTests` 1, `WorkerEndToEndTests` 7, `CrashAfterPersistenceTests` 3,
`ReconciliationTests` 11, `ConcurrencyTests` 6, `RunStateMigrationTests` 2. They cover atomic submission and
rollback, task/run linkage, a valid and an invalid outcome, idempotent retry after persistence, no second
revision, a stale worker, concurrent submissions, concurrent extraction of one run, concurrent reconcilers,
reconciliation after task failure (and not for a task waiting for a retry), and migrating a database that
already holds runs. Each class refuses to run unless the database name starts with `test_`.

## 11. Local validation steps (owner; on the local Windows computer only)

Use the runbook's setup (`$LocalProject`, `$Py`, `AP_DB_*`), a dedicated development server, no `--keepdb`,
no `--noinput`, and remove the flag afterwards.

```powershell
Set-Location "$LocalProject\backend"
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set. Remove-Item Env:AP_RUN_DB_TESTS and repeat." }

# 0. Preflight (section 3): count existing runs and approvals, take a dump if the data matters.

# 1. Update the checkout to the reviewed commit, then apply the migrations to the development database.
& $Py manage.py showmigrations cases intake
& $Py manage.py migrate                            # applies cases 0002-0004 and intake 0001
& $Py manage.py makemigrations --check --dry-run   # expect: No changes detected

# 2. DB-free tests.
& $Py manage.py test                               # expect: Ran 834 tests, OK

# 3. The new PostgreSQL tests only. Creates and drops test_<AP_DB_NAME>.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_intake_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 76 tests, OK, none skipped, exit code 0
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 4. Everything that needs PostgreSQL (130 earlier + 15 recovery + 76 new = 221), to prove nothing regressed,
#    in particular the process-level recovery tests, which start the real runworker.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 221 tests, OK
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 5. Manual smoke test against the development database (writes one user "smoke", case, run, source, task, outcome, revision).
& $Py manage.py shell -c "from django.contrib.auth import get_user_model; from apps.intake.submission import submit_request; u, _ = get_user_model().objects.get_or_create(username='smoke'); r = submit_request(text='Requester: Jane\nItem: 2 | boxes | A4 paper\n', submitted_by=u); print(r.run.pk, r.case.reference, r.created)"
& $Py manage.py runworker --once                   # expect: Task <id> succeeded.
& $Py manage.py shell -c "from apps.cases.models import WorkflowRun; r = WorkflowRun.objects.latest('created_at'); print(r.state, r.extraction_outcome.status, r.current_revision.revision_number)"   # expect: needs_review valid 1
```

Report the exact `Ran N tests` lines, any failure, and the output of steps 5 and the preflight. If a
concurrency test fails or hangs, report it as it is; do not loosen it. Hangs are bounded by lock and join
timeouts (15 s and 60 s). The 221 in step 4 assumes the earlier counts still hold (35 + 95 + 15 = 145);
report the number you get if it differs.

## 12. Remaining limitations

* **All PostgreSQL behavior of this slice is unproven.** Row locking, the new constraints, the three
  migrations on a database with existing rows, concurrent submission and extraction, and the worker hook have
  never run against PostgreSQL.
* No authentication or authorization: anyone with the Python API can submit. No API, no UI, no audit events.
* No review, edit, approval, rejection, export, run cancellation, or manual retry. A failed run stays failed.
* The 65,536-byte limit, the 3 attempts, and the lease and backoff values are provisional, not measured.
  A handler must finish within the 60-second lease (no heartbeat): fine for the fixture parser, not for a model.
* The extractor is a fixture parser for one labelled-line template; text in any other form is extracted as an
  invalid draft (nothing is invented). It is not an AI model.
* The source text, drafts and findings are plaintext in PostgreSQL; nothing here encrypts or access-controls
  them. `test.add_numbers` is still in the production allowlist.
* A worker that is not running is not detected; a run waits `queued`.
* Reconciliation runs inside the worker loop. If no worker runs, a failed task is not reflected on its run.
