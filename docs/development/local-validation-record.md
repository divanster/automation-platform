# Local PostgreSQL validation record

> **Scope.** This records validation that the project owner ran on the **local Windows
> computer** (not in the Claude Code development VM) and reported afterwards. The VM did not
> run, repeat, or observe any of it, and no database credentials were read or set to write
> this record. Evidence is split below into **pasted output** (text the owner supplied) and
> **owner-confirmed without pasted output** (the owner said it completed; no log was
> supplied). Nothing is claimed beyond that.

## Environment (as reported by the owner)

| Item | Reported value |
| --- | --- |
| Python | CPython 3.14.10, Windows AMD64 |
| PostgreSQL | 18.6 |
| Local project path | `C:\Projects\newnew\automation-platform` |
| Port used for the successful application smoke test | **8010** (the runbook's example uses 8000; the port is a local choice) |
| Test database | `test_automation_dev`, created and destroyed by Django |

Observation: the VM's recorded interpreter is CPython 3.14.4 and the runbook's version table
names it. The local run used 3.14.10, a later patch release of the same series. The
hash-locked package set is separate from the interpreter patch version. The local
installation from the hash-locked file is described in the next subsection.

The local package versions (Django, DRF, psycopg) are not recorded here as locally
observed. The pinned versions in the [runbook](runbook.md) are what the repository
specifies.

### Local dependency installation (command and output supplied by the owner)

The owner ran this on the local computer, in the local project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements\development.txt
```

The owner supplied the successful package-installation output, followed by `pip check`
reporting `No broken requirements found.` This is the runbook's hash-locked installation
method (`--require-hashes` with `--only-binary=:all:` against the committed development
lock). Recorded accurately:

- The command was provided to Claude in the conversation, and the successful installation
  and `pip check` output were supplied. The output itself is not reproduced in this
  repository.
- **Claude did not independently reproduce this installation.** The VM's own fresh-environment
  reproduction (2026-11-10) is a separate, earlier check.
- **No clean-checkout reproduction is claimed.** It was not reported whether the local
  checkout was freshly cloned or whether the installed package set was compared with the
  VM's; the installed versions are not recorded here.

## Evidence

### Supported by pasted output

| Result | Detail |
| --- | --- |
| PostgreSQL-required tests | `Ran 35 tests`, `OK`, exit code 0 |
| Test database cleanup | The dedicated `test_automation_dev` database was destroyed afterwards |
| Liveness | `GET /api/health/live/` returned status `ok` on port 8010 |
| Readiness | `GET /api/health/ready/` returned status `ok` on port 8010 (this checks a real database connection) |

The 35 matches the collection expected from the repository at the persistence commit: 3
skeleton tests plus 32 persistence tests (see the [runbook](runbook.md), section 5). The pasted
output did not report any skipped test. Because Django builds the test database by applying
every migration, the pass also exercises the `accounts` and `cases` migrations on PostgreSQL
18.6 (in the throwaway test database).

### Confirmed by the owner, no pasted output

| Result | Note |
| --- | --- |
| `showmigrations` completed successfully | No log supplied; the exact lines (for example whether every migration shows `[X]` in the development database) were not shown |
| `makemigrations --check --dry-run` completed successfully | No log supplied; expected result is "No changes detected" |
| Local DB-free regression suite passed | **No count was supplied.** The VM checkpoint that preceded the persistence commit reported 514 DB-free tests; that is the VM's figure, not a newly observed local count, and is not recorded here as one |

## What this validates (tested environment and scenarios only)

Persistence database acceptance is **satisfied for the tested environment and scenarios**:
CPython 3.14.10 on Windows AMD64, PostgreSQL 18.6, Django's dedicated test database, and the 35
PostgreSQL-required tests. Specifically, those tests ran and passed against a real server:

- the schema constraints (unique `(run, revision_number)`, the number, format-version, and
  hash CHECK constraints), protected deletes, and the foreign keys;
- revision and approval persistence, the stored current-revision pointer, and the persisted
  export-eligibility reads;
- rollback when a write fails after the insert;
- competing writers on one run, each on its own connection (`WriterSerializationTests`);
- the skeleton's user model tests and a real readiness check.

It also settles two items that were open since Phase 2: Django 5.2 with PostgreSQL 18 worked
in these tests and in the application smoke test, and the `psycopg` driver worked on
CPython 3.14.10.

## What this does not validate

- **Concurrency.** Passing concurrency tests show that the tested scenarios (competing
  writers for one revision number; an approval racing a new revision) behaved correctly in the
  runs that were made. They **do not prove that no other race exists**, and they say nothing
  about different timing, load, isolation settings, other servers, or other code paths. The
  export-commit re-check is still not built, and persistence alone does not make an export
  race-safe.
- **Immutability.** Revisions and approvals are immutable at the Django ORM and service
  boundary only. That is **not database tamper protection**: raw SQL, an administrator, or a
  migration can change rows, and a consistent change to the payload and its hash together is
  not detected on load. See [revision-approval-policy.md](revision-approval-policy.md).
- **Authentication and authorization.** `approver_id` remains an unverified domain actor
  identifier.
- **Anything not built:** worker, API, UI, audit events, document storage, export execution,
  the state machine.
- **A different environment:** another PostgreSQL version, server configuration, operating
  system, or Python build.

## Phase 2 acceptance criteria, reviewed individually

Criteria are from the Phase 2 entry in the [implementation plan](../architecture/implementation-plan.md).
"Closed" means the available evidence supports it for the tested environment. Nothing else is
marked closed.

| Criterion | Status | Basis |
| --- | --- | --- |
| The test suite runs and passes from a clean checkout following written instructions | **Partly closed.** DB-free suite passed locally (owner-confirmed, no count). **A clean-checkout reproduction has not been reported**: it is not established that the checkout was freshly cloned or that the written steps were followed as written | Owner confirmation only |
| A fresh virtual environment built from the recorded pins reproduces the same package set | **Closed on the VM** (2026-11-10). **Locally:** the hash-locked install succeeded and `pip check` reported no broken requirements (owner-supplied output, not independently reproduced by Claude). Not established locally: that the installed set equals the VM's | VM record; owner-supplied local output |
| The health endpoint works | **Closed:** liveness and readiness both `ok` against a real PostgreSQL 18.6 | Pasted output |
| A connection to a local PostgreSQL development database | **Closed** for the tested environment | Pasted output (readiness) |
| Migrations apply against PostgreSQL | **Closed for the test database** (Django applies all migrations to build it). **Development database:** `showmigrations` owner-confirmed, no log, so the applied state of each migration there is not shown here | Pasted output and owner confirmation |
| `makemigrations --check --dry-run` against a reachable database | **Owner-confirmed**, no log | Owner confirmation only |
| Django 5.2 compatibility with PostgreSQL 18 in practice | **Closed for the tested environment** | Pasted output |
| Python 3.14 compatibility of every dependency, including the PostgreSQL driver | **Closed for the tested environment:** the driver ran on CPython 3.14.10. Other dependencies are exercised by the passing suite | Pasted output and owner confirmation |
| Application smoke test (`runserver` plus `migrate`, end to end) | **Closed for the smoke test that was run:** both health endpoints returned `ok` on port 8010. The admin login page check and the listening-address check from the runbook were not reported | Pasted output |
| `requires_postgres` tests | **Closed:** 35 ran, `OK`, exit code 0, test database destroyed | Pasted output |
| No secrets in the repository | Checked by secret scans on the VM at each checkpoint (no matches); no local evidence needed or supplied | VM record |
| Module dependency rules checked by an automated test | **Closed** (VM and, per owner confirmation, local DB-free suite) | Tests in the repository |
| Hash-locked install on the local computer | **Closed** by owner-supplied output (successful install with `--require-hashes --only-binary=:all:`, then `pip check` clean). Not independently reproduced by Claude | Owner-supplied output |
| CI definition (optional) | **Not requested** | None |
| PostgreSQL server configuration meets the runbook's loopback-only rule | **Open: not met as reported.** See below | Owner report |

Phase 2's database-backed criteria are supported by the evidence above. Overall Phase 2 closure
is the project owner's decision at its stop-and-report gate. No later phase is marked
complete.

### Keeping the categories separate

| Category | Status |
| --- | --- |
| **Tested backend and persistence behavior** | Supported by the pasted 35-test run and health checks, for the tested environment and scenarios only (see "What this does not validate") |
| **Clean-checkout reproduction** | **Not yet reported.** Local install and DB-free results exist, but a fresh clone followed step by step has not been reported |
| **Production driver and packaging decisions** | Open and separate: binary versus source-built `psycopg` (open question 1.3), the application server, service packaging, installer, TLS, and backups. Nothing here validates them |
| **Network hardening** | Open: `listen_addresses='*'` (below). Not a backend-behavior result either way |

**PyCharm** is optional developer tooling. Its setup is not a Phase 2 backend or database
acceptance criterion and is not a blocker; it is not configured or confirmed, and nothing
in this record depends on it.

**This is not production readiness.** No production server, service, hardening, or
operations work has been done or validated.

## Unresolved deployment-hardening item: `listen_addresses`

The owner reported that PostgreSQL was configured with `listen_addresses='*'`. That setting
makes the server listen on every network interface. It conflicts with the runbook's rule
(sections 0 and 6: PostgreSQL listens on loopback only, and the check in part C stops the
procedure if it does not) and with [deployment.md](../architecture/deployment.md) (local
connections only in standalone mode).

- **The database must not be described as loopback-only.** This record makes no such claim.
- Not known from the report: whether `pg_hba.conf` restricts client addresses, whether the
  Windows firewall blocks inbound connections to the port, and whether the computer is
  reachable from other machines.
- **Unresolved.** It is tracked in [open-questions.md](../architecture/open-questions.md),
  item 1.5. It must be resolved (limit `listen_addresses` to loopback, or document and
  justify restricted exposure with `pg_hba.conf` and firewall rules) before any shared,
  hub, or production use. The validation above used a dedicated development server and a
  throwaway test database only.

## Persistent-worker slice (`apps/tasks`)

Supplied by the owner after running the steps of
[persistent-worker-slice.md](persistent-worker-slice.md), section 9.3, on the local Windows
computer. **These are owner-supplied local results.** The VM did not run or observe any of them, and
the report did not restate the environment (the one above is the previously reported one).

| Step | Reported result |
| --- | --- |
| `migrate` applying `tasks.0001_initial` | OK, exit code 0 |
| Worker PostgreSQL tests (`tests.test_tasks_postgres`) | 95 passed, exit code 0 |
| All PostgreSQL-required tests (`--tag requires_postgres`) | 130 passed, exit code 0 (35 earlier plus 95) |
| Test database | Destroyed afterwards |
| Smoke test: one `test.add_numbers` task with values `[1, 2, 3]`, processed by `runworker --once` | Processed successfully; task state `succeeded`; persisted result `count=3`, `sum=6`; attempt count 1 |
| `runworker --once` on an empty queue | `No task available.` |
| Continuous `runworker --poll-seconds 2`, then Ctrl+C pressed once | Graceful shutdown: `Worker stopped after processing 0 task(s).`; exit code 0 |

The count of 95 equals the number of tests collected in the VM, so no test appears to have been
skipped. Because Django builds the test database by applying
every migration, the pass also exercises `tasks.0001_initial` on PostgreSQL in the throwaway test
database.

**What this supports.** For the scenarios written in `backend/tests/test_tasks_postgres.py`: the
database constraints, enqueue and idempotency (including conflicting reuse and concurrent enqueues of
one key), a single claim among competing threads, row skipping with `SKIP LOCKED`, eligible-task
ordering, lease renewal, recovery of expired leases, fencing of expired and superseded tokens,
bounded retries with backoff, atomic task-and-attempt updates, queued-task cancellation (including
cancel versus claim), terminal-state protection, and the worker running a handler with no open
transaction. Plus one real end-to-end task through the `runworker` command.

**What it does not show.**

- No test kills a real worker **process**: a crash is simulated by claiming a task and never
  finishing it, and the concurrency tests use threads, not separate worker processes.
- The Ctrl+C check ran against an idle worker (0 tasks); finishing a task in progress on shutdown
  is covered only by DB-free tests with mocks.
- Not tested: a heartbeat (none exists), restart or sleep on Windows, several hosts or clock skew,
  load or throughput, and measured lease, retry, or polling values (the defaults are provisional).
- Passing tests do not prove that no other race exists. This is a development database; it is not
  production readiness, and `listen_addresses` (above) is still unresolved.
- The queue is not connected to any workflow, and no endpoint or UI exists.

## Still deferred

Authentication and authorization (the review-services slice adds authorization inside the services and links
approvals to users through `User.public_id`, but a login mechanism does not exist; see the pending section
above), connecting the queue to any workflow, a worker
heartbeat, concurrency limits, cooperative cancellation of running tasks, an API, a UI, audit
events, race-safe export (the atomic re-check at export commit), database-level immutability,
document storage, the state machine, and real AI adapters. The open questions that cover them
are in [open-questions.md](../architecture/open-questions.md).

## Code comments

Two status comments written before the local run were updated to point at this record: the
`STATUS` paragraph in `backend/tests/test_cases_persistence_postgres.py` and the locking note
in `backend/apps/cases/services.py`. Both keep the fact that database tests are not run in
the development VM. Only comments changed; no executable code. The `STATUS` paragraph in
`backend/tests/test_tasks_postgres.py` was likewise updated to point here after the worker run.

## Worker crash-recovery tests (real worker processes; `apps/tasks`)

Supplied by the owner after running `backend/tests/test_tasks_recovery_postgres.py` on the local
Windows computer (commands: [persistent-worker-slice.md](persistent-worker-slice.md), section 9.4).
**These are owner-supplied local results.** The VM never connected to PostgreSQL and did not run or
observe any of them; in the VM only the database-free tests ran (full suite `Ran 719 tests`, OK, at the
time of writing). The report did not restate the environment, and no log was pasted.

| Step | Reported result |
| --- | --- |
| First local run of the module | 15 tests executed, 12 passed, 3 failed (66.471 s, exit code 1). The three ordered race tests (`test_completion_`, `test_failure_`, `test_renewal_arriving_while_recovery_holds_the_row_is_refused`) failed in the test helper `wait_until_a_session_waits_for_a_lock` with "no session ever waited for the row lock" |
| Cause and correction | A test-harness defect: the helper polled `pg_stat_activity` from inside the transaction that held the lock, and PostgreSQL keeps that view's snapshot for the rest of the transaction. The helper now observes the competing session through `pg_locks` and `pg_blocking_pids()`. Test code only; no production code was changed |
| Run after the correction (`tests.test_tasks_recovery_postgres`) | **15 passed**, exit code 0, 37.091 s; the three previously failing lock-wait tests passed |
| Test database | Destroyed successfully afterwards |

The 15 tests are `MultiProcessClaimTests` 2, `KilledWorkerTests` 2, `GracefulShutdownWithActiveTaskTests` 2,
and `RecoveryRaceTests` 9. A combined `--tag requires_postgres` run of all PostgreSQL-required tests
after this module was added (130 earlier plus 15 = 145) was **not reported**.

**What this supports**, for the scenarios written in the module only:

- Two real worker processes held two different tasks at the same moment, and two processes released
  together raced for one task, which ran once (one attempt, one stored result, handler run once).
- A worker process that was forcibly killed while running a task left it `running` until its lease
  expired; a later worker process recovered it (attempt `abandoned`, code `lease_expired`), the retry
  completed once, a stale lease token could not complete, fail, or renew it, and a task whose worker was
  killed on every attempt ended `failed` with `lease_expired` at its attempt limit.
- Graceful shutdown with a task in progress: after the first signal the worker finished the task,
  persisted the result, and exited with code 0; a second signal exited with code 130 and the task was
  recovered after its lease. The signal was `CTRL_BREAK_EVENT` to the child's process group on Windows
  (Ctrl+C cannot be sent to one process there); `SIGINT` elsewhere.
- Completion, failure, and renewal against lease recovery, in both forced orders and as unordered races
  on separate connections, ended in exactly one consistent state.

**What it does not show.**

- Lease expiry was simulated by giving the recovering worker process a shifted clock; leases were not
  waited out in real time. One machine, one operating system, one PostgreSQL server.
- There is no heartbeat, and no Windows service or supervisor that restarts a dead worker.
- Not tested: `SIGTERM`, `taskkill` without `/F`, or a service stop (not known to be graceful on
  Windows); restart or sleep on Windows; power loss; several hosts or clock skew; load or throughput;
  measured lease, retry, or polling values.
- Passing tests do not prove that no other race exists. Handlers still run at least once and may run
  more than once. This is a development database; it is not production readiness.
- Findings of the read-only audit that this tests-only slice does not address: one inconsistent
  attempt row would make recovery (and therefore claiming) fail for every worker; a hung handler
  stops a single worker from recovering anything; a handler that outlives the lease has its result
  discarded and is run again; a hard interrupt does not release the lease. These are for a later,
  separately reviewed hardening slice.
- The queue is not connected to any workflow; there is no real AI, API, or UI.

## Request-intake orchestration slice (`apps/intake`): owner-confirmed, no pasted output

The project owner reported on 2026-11-13 that, on the local Windows computer, the 76 request-intake PostgreSQL
tests passed, the combined run of all 221 PostgreSQL-required tests passed, and a manual end-to-end smoke test
succeeded (request, worker, valid outcome, revision 1, `needs_review`). This is **owner-confirmed without pasted
output**: no log is attached, the VM did not run, repeat or observe any of it, and the exact command lines,
timings and the output of the existing-row preflight (whether `cases_workflowrun` held rows) are not in this
record. It covers the `cases 0002` to `0004` and `intake 0001` migrations as applied in that run and the tested
scenarios only; it does not prove that no other race exists. See the
[slice document](request-intake-orchestration-slice.md).

## Review-services slice: owner-supplied local results

The review-services slice ([slice document](review-services-slice.md)) was written and committed
(`05233d2`); its DB-free tests pass in the development VM (991 tests). **These are owner-supplied local
results.** The VM never connected to PostgreSQL and did not run, repeat or observe any of them. The test,
migration and role-setup results were reported by the owner without a log (owner-confirmed without pasted
output). For the manual smoke test the owner supplied the actual console output; it was run through Django's
interactive shell with PowerShell here-strings (see the
[slice document](review-services-slice.md), section 10.4). None of this output is stored in the repository.
The owner reported:

* The 135 review PostgreSQL tests: OK, exit code 0.
* All 356 PostgreSQL-required tests (221 earlier plus 135): OK, exit code 0.
* The `User.public_id` migration on the development database: 1 existing user, 0 missing ids, 1 distinct id.
* All new migrations applied (`accounts` 0002 to 0004, `audit` 0001, `cases` 0005, `intake` 0002);
  `makemigrations --check --dry-run`: No changes detected; `setup_roles` completed.
* A manual smoke test: an operator submitted a request; the worker processed the extraction task; the run
  reached `needs_review` with revision 1; an approval attempt by the operator was refused (`missing_permission`);
  an approver approved the revision; the final state was `approved`; the audit actions were
  `request.submitted`, `revision.created`, `run.state_changed`, `approval.recorded`, `run.state_changed`.

What this validates: the tested scenarios of the slice on the owner's PostgreSQL 18 development database,
including the three-step user migration with one existing user. What it does not: the migration with many
existing users on PostgreSQL (only one user existed), any race the tests do not cover, database-level
immutability (revisions, approvals, rejections and audit events are protected at the ORM and service boundary
only), a login mechanism, a REST API, a UI, real AI, or production readiness. The exact command lines and timings
of the test runs were not supplied.

## Pending: REST API slice (not yet validated)

The REST API slice ([slice document](rest-api-slice.md)) has been written, and its DB-free tests pass in the development
VM (1201 tests). No SQLite substitute was used. **Owner-supplied local result, no output attached:** the first version of
the PostgreSQL API tests (98) was run locally with 96 passed and 2 failed (exit code 1). Both failures were defects in the
TESTS, not in the application, and are corrected without weakening any behavioral assertion:

1. `test_an_invalid_draft_is_a_400_with_findings_and_never_becomes_a_revision` (named by the owner) expected the message
   "Requester name is required."; the domain says "A value is required." for a requester whose name is null (the first
   wording is only for a missing requester object). The finding code, field path and blocking flag matched. Those three are the
   stable contract (`workflows/request_intake/findings.py`); the message is fixed explanatory text and is not.
2. A second failure, not named in the report and identified by analysis: `test_login_rotates_the_session_and_the_csrf_token`
   assumed Django's `login()` changes the session key on every login. It does so for a pre-existing anonymous session
   (`cycle_key`) and flushes a different user's session, but keeps the key when the same user logs in again. The test
   was replaced by three that test exactly those cases (planted anonymous session replaced, CSRF secret rotated, other
   user's session discarded). **Confirm the identity of the second failure on the rerun.**

The corrected module has 100 tests (a combined collection of 456) and has **not been rerun**; nothing in this record supports
any claim about it until the owner reports that output here. The slice adds no model, migration, or dependency. (See the
later entry "MOD-1b / S1b core authorization refactor" for a 242-test local PostgreSQL run that the owner reported for the
authorization, review and API tests; this section is deliberately left unchanged until the owner confirms which modules it covered.)

## UI-2 request review and approval: owner-supplied browser validation (2026-11-16)

> **Scope.** This records one workflow that the project owner ran in a **real browser on the local Windows computer** against
> the real Django REST API, the local PostgreSQL 18 database and the persistent worker (`runworker`), with the React app from
> `frontend/` (see the [slice document](frontend-slice.md), sections 13.7 and 13.7a). **Evidence type: owner-supplied
> screenshots, described to Claude in text.** The screenshots are not stored in this repository and Claude did not see them; no
> command output was pasted. The development VM has no PostgreSQL and did not run, repeat or observe any of it. Nothing below is
> stronger than the owner's description.

**Subject.** Request `REQ-20261116-B666BD8`; accounts `smoke-operator` (Operator) and `smoke-approver` (Approver), and one account
authorized to view the audit history (its role was not stated).

**Reported observations:**

| Area | Reported result |
| --- | --- |
| Submission and extraction | A request submitted through React was extracted by the worker: the task succeeded on attempt 1, the run reached `needs_review`, revision 1 existed |
| Editing | The Operator changed the requester to "Ivan Test" and a quantity from 2 to 3 (boxes), leaving the description "A4 paper"; revision 2 was created with a new SHA-256 content hash; two `value_not_in_source` warnings (`requester.name`, `items[0].quantity`) were displayed |
| Authorization | The Operator who submitted and edited the request could not approve it; the UI offered only what the backend permitted |
| Approval | `smoke-approver` approved revision 2; the UI showed "Revision 2 approved."; the state became approved; approvals/rejections read 1/0; both warnings stayed visible |
| Audit history | Seven events in the real UI: request submitted; revision 1 created; run state changed to `needs_review`; revision 2 created; draft edited; revision 2 approved; run state changed to `approved`. The edit and approval events showed different actor identifiers; the approval event recorded two warnings |

**What this validates (the reported workflow, this environment only):** the UI-2 editor, approval and audit screens, the REST API
endpoints behind them (submit, detail, edit, approve, audit), the worker's extraction and the persistence in PostgreSQL work
together for the path above. The approval succeeded for a revision with two warnings; the backend requires the acknowledgement
to equal the revision's current warnings exactly and the audit event recorded two, which is consistent with both having been sent,
but the screenshots do not independently show each checkbox click. Control visibility followed the backend, which shows
role-based gating for an Operator; it does **not** exercise the self-approval rule, which concerns a user who holds the approve
permission.

**What it does not validate.** Stated plainly:

- **Not demonstrated in the supplied evidence:** the stale-revision conflict flow; rejection and its confirmation; validation
  findings beside fields after a refused draft; adding and removing items; a first edit of an invalid extraction; the stale-approval
  path; the self-approval blocker text; keyboard-only use; small screens; browser console and storage checks; audit paging; other
  browsers.
- **Not covered by this evidence:** concurrency beyond what the PostgreSQL tests exercise, database-level immutability (ORM and
  service level only), real AI (extraction is the deterministic fixture parser), document upload, a source-text review endpoint,
  a revision diff, a Windows installer, production serving (Django does not serve `frontend/dist`; TLS, HSTS and CSP are
  undecided), and production readiness.
- **Known display limits:** the audit history shows actor identifiers, not names (the API returns none), and self-approval stays
  forbidden by the backend, including for a lone user.
- **No claim that all possible UI behavior was tested.** UI-2 is **development-validated for the tested workflow** only.

This entry does not change the status of anything recorded above, in particular the REST API PostgreSQL tests in the section
"Pending: REST API slice", which this record still shows as not rerun.

## MOD-1b / S1b core authorization refactor: owner-supplied local PostgreSQL results (2026-11-17)

> **Scope.** The generic own / any / superuser authorization algorithm moved from `apps/intake/access.py` into Core
> (`apps/accounts/access.py`) with no change in any authorization outcome (commit `962d6ab`, branch `feat/module-authz-core`; see
> [module-contract.md](../architecture/module-contract.md), section 3.8). The project owner ran the PostgreSQL authorization, review
> and API integration tests **locally on the Windows computer**. **These are owner-supplied results.** The development VM has no
> PostgreSQL, never connected to one, and did not run, repeat or observe any of it. No output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Tests | **242 passed** |
| Runtime | 55.659 seconds |
| Exit code | 0 |
| Test database | `test_automation_dev` destroyed successfully |

**Scenarios the owner reported as included:** Django users and groups; authorization equivalence; the review services; API
permissions; concurrent edits and approvals; the end-to-end API workflow.

**What was not supplied:** the command line, the list of test modules, and the output. For orientation only (this is the VM's
arithmetic, not something the owner stated): 242 equals the 135 tests of `tests/test_review_postgres.py`, the 100 of the corrected
`tests/test_api_postgres.py` and the 7 of the new `tests/test_authz_core_postgres.py`. The section "Pending: REST API slice" above is
**not changed** by this entry, because the owner did not name the modules; if the 242 did include the corrected API module, the owner
can confirm that there.

**What this validates (this environment and these scenarios only):** with the refactor in place, the authorization, review-service and
API behaviours exercised by those tests give the same results on the owner's PostgreSQL database, including real users read by
`load_actor`, groups created by `setup_roles`, inactive and deleted users, and the concurrency and end-to-end scenarios in the tests.
Together with the DB-free evidence in the VM (a 36,864-row truth table recorded from the old code before the change, reproduced
by the old algorithm, by the delegating module and by Core), this is the evidence that the refactor preserved behaviour.

**What it does not validate, and what does not exist.** It does not prove that no other race or authorization path exists. It says
nothing about module-driven permissions: there is **no `ModuleState`, no runtime module permission, no enable/disable and no
capability facade**, Request Intake is not a module, and nothing builds authorization rules from a manifest. It is **not a
production-readiness claim**: a login throttle per process, undecided TLS/HSTS, ORM-level immutability and the other limits recorded
above are unchanged.

## MOD-1b / S2 worker claim filter, maintenance hooks and kind gate: owner-supplied local PostgreSQL results

> **Scope.** S2 added an allowed-kind filter to the atomic claim, ordered maintenance hooks and a fail-closed kind gate seam to `apps/tasks`
> (implementation commit `28483a6`, branch `feat/module-worker-gating`; see [module-contract.md](../architecture/module-contract.md),
> section 3.9, and [persistent-worker-slice.md](persistent-worker-slice.md), section 11). The project owner ran three PostgreSQL test
> modules **locally on the Windows computer**. **These are owner-supplied results.** The development VM has no PostgreSQL, never connected
> to one, and did not run, repeat or observe any of it. No output or log is stored in this repository.

**Modules the owner named:** `tests.test_tasks_gating_postgres`, `tests.test_tasks_postgres`, `tests.test_tasks_recovery_postgres`.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Tests | **149 passed** |
| Runtime | 66.399 seconds |
| Exit code | 0 |
| Test database | The dedicated test database was destroyed successfully |

**What was not supplied:** the exact command line and the output. For orientation only (the VM's arithmetic from counting the test methods in
the files, not something the owner stated): 149 equals the 39 tests of `test_tasks_gating_postgres.py`, the 95 of `test_tasks_postgres.py`
and the 15 of `test_tasks_recovery_postgres.py`.

**What this supports (this environment and these scenarios only):**

- **Filtered claiming (new tests).** On the owner's PostgreSQL database the filtered claim behaved as the tests expect: no filter claims any
  kind as before, an empty filter claims nothing, a non-empty filter is part of the single `FOR UPDATE SKIP LOCKED` statement and keeps the
  order, tasks of other kinds (an unknown kind included) stay queued and untouched, and a worker with an installed gate holds tasks of gated-off
  kinds and runs them once the gate opens.
- **Worker concurrency and recovery regression.** The existing queue suite (95 tests: claim, lease, retry, recovery, cancellation, concurrent
  workers) and the 15 real-process recovery tests (separate `runworker` processes, a forced kill, races against recovery) passed unchanged with
  S2 in place, together with the new threaded tests of workers with disjoint and overlapping filters.
- **Lease and retry behaviour preserved.** Lease tokens, attempt numbering, backoff, retry, recovery of expired leases and fencing gave the
  results the tests expect, in the tested scenarios.

**What it does not validate, and what does not exist.** It does not prove that no other concurrency race exists. **`ModuleState` is not
implemented and nothing can be enabled or disabled at run time**: no gate is installed by the production composition, so every kind is still
claimable and no task is held. The **gate race window remains** (a kind gated off between the gate read and the claim can still be claimed by
that one claim, section 3.9 of the module contract), `while_disabled` is read by nothing, and the gate that reads module state is S4. The tests
run on one machine and operating system. This is **not a production-readiness claim**: the other limits recorded above are unchanged.

## MOD-1b / S3 extensible audit vocabulary: owner-supplied local PostgreSQL results

> **Scope.** S3 lets modules declare audit actions that Core's one recorder accepts (implementation commit `ba4060a`, test-expectation fix
> `5398a1b`, branch `feat/module-audit-vocabulary`; see [module-contract.md](../architecture/module-contract.md), section 3.10). It adds audit
> migration `audit.0002_extensible_audit_vocabulary`: the fixed list of action names is replaced by a bounded, anchored action-**shape** CHECK
> (`audit_event_action_shape`) and the subject-type CHECK gains `module`. The project owner ran the migration and the PostgreSQL tests **locally
> on the Windows computer**. **These are owner-supplied results.** The development VM has no PostgreSQL, never connected to one, and did not run,
> repeat or observe any of it. No output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Migration `audit.0002_extensible_audit_vocabulary` | Completed successfully |
| Existing audit events | **15 existing audit events remained** after the migration |
| New PostgreSQL audit vocabulary tests (`tests/test_audit_vocabulary_postgres.py`) | **24 passed** |
| Corrected schema-constraint test (`tests.test_review_postgres.SchemaTests.test_the_database_has_every_named_constraint`) | Passed |
| Full selected PostgreSQL regression | **342 tests passed, 80.063 seconds, exit code 0** |
| Test database | The dedicated test database was destroyed successfully |

**An earlier run of the same selection** reported 342 tests with 341 passed and 1 failed (exit code 1). The failing test was
`SchemaTests.test_the_database_has_every_named_constraint`, which still expected the old constraint name `audit_event_action_valid` while the
migration intentionally replaces it with `audit_event_action_shape`. The new constraint existed in PostgreSQL; the failure was a **stale test
expectation**, corrected in `5398a1b` (one line, test code only, no model or migration change). The VM's DB-free suite did not show it because
the test needs PostgreSQL.

**What was not supplied:** the command line, the list of test modules, whether the 15 events were on the development database, and any output. For
orientation only (the VM's arithmetic, not something the owner stated): 342 equals the 24 tests of `test_audit_vocabulary_postgres.py`, the
135 of `test_review_postgres.py`, the 100 of `test_api_postgres.py`, the 7 of `test_authz_core_postgres.py` and the 76 of `test_intake_postgres.py`.
The section "Pending: REST API slice" above is **not changed** by this entry, because the owner did not name the modules.

**What this supports (this environment and these scenarios only):**

- **The migration on existing data.** It applied to a database that held audit events and left them in place (15, as reported). The new test also
  compares every column of every pre-existing row, before and after, in the throwaway test database.
- **The new constraints.** `audit_event_action_shape` exists and the extended subject-type CHECK accepts the existing types plus `module`;
  well-formed actions (every Core action and module-shaped ones) are accepted and malformed ones are refused by the database; PostgreSQL's reading of the
  shape pattern agreed with Python's on the test's fixed corpus.
- **Forward and reverse migration.** The tests cover the forward migration with existing rows, the reverse without events that only the new
  vocabulary allows (old constraints restored, every row kept), and the reverse **with** such events, which is refused before any constraint is touched,
  deleting nothing. The owner's report did not itemize the tests, so this rests on the 24 passing.
- **The recorder and Core behaviour.** A declared module action recorded through the one recorder inside a transaction, an unknown but well-formed
  action refused by Python, rollback of the business operation when the audit write fails, and the unchanged review, API and intake audit behaviour
  in the regression.

**What it does not validate, and what does not exist.**

- **The database does not enforce the allowlist.** The CHECK validates syntax only; raw SQL, a privileged migration or an administrator can store any
  well-formed action, and a test pins this. Audit events remain append-only at the ORM and service boundary only: no hash chain, trigger or insert-only
  role. Namespace declarations are naming rules, not a sandbox.
- **No module uses it.** `BUILTIN_MODULES` is empty, nothing installs a module vocabulary in a running process (deferred to MOD-2), and there is no
  `ModuleState`, enable/disable or S4. Request Intake emits Core events only.
- **Rollback limits.** Once a module action or a `module`-subject event is stored, migration `0002` cannot be reversed; events are never deleted to make
  it succeed.
- Not shown: a large audit table (the constraints are added with a table scan under a brief exclusive lock), another PostgreSQL version, operating system
  or machine, or that no other race exists.
- **This is not a production-readiness claim.** The other limits recorded above (`listen_addresses`, per-process login throttling, undecided TLS and
  HSTS, ORM-level immutability) are unchanged.

## MOD-1b / S4 module host (persistent module state, enable/disable, worker gate): owner-supplied local PostgreSQL results

> **Scope.** S4 adds `apps/module_host` (`ModuleState`, the append-only `ModuleNamespaceClaim`, the append-only one-row `ModuleHostMarker`, migration
> `module_host.0001_initial`), a module sync, audited enable and disable administrator commands, a status command, and installs the S2 kind gate in the production
> worker composition (branch `feat/module-runtime-state`: implementation `96efa2f`, test-only corrections `45bd0da`, `4ba709b` and `0f8e44d`; see
> [module-contract.md](../architecture/module-contract.md), section 3.11). The project owner ran the migration and the PostgreSQL tests **locally on the Windows
> computer**. **These are owner-supplied results.** The development VM has no PostgreSQL, never connected to one, and did not run, repeat or observe any of it. No
> output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Module host PostgreSQL tests (`tests/test_module_host_postgres.py`) | **75 passed** |
| Process-level recovery tests (`tests/test_tasks_recovery_postgres.py`) | **15 passed** |
| Audit vocabulary PostgreSQL tests (`tests/test_audit_vocabulary_postgres.py`) | **25 passed** |
| Migration `module_host.0001_initial` | Applied successfully |
| Full selected PostgreSQL regression | **567 tests passed, 153.186 seconds, exit code 0** |
| Test database | The dedicated test database was destroyed successfully |
| DB-free regression in the development VM | 1926 tests passed (198 of them in the five module host files) |

**The path to the passing run (reported by the owner, with tracebacks only where supplied).** Every failure was a test or harness defect; **no production code, model or
migration changed in any of the corrections**.

1. First module host run: 73 tests, 72 passed, 1 error (`DataError`, `StringDataRightTruncation`) in
   `SchemaTests.test_malformed_ids_versions_and_fingerprints_are_refused_by_the_database`, for a 41-character module id. The contract allows 3 to 40 characters and the column is
   `varchar(40)`, so the column width refuses the value before any CHECK runs; Django reports that as `DataError`, while CHECK, unique and NOT NULL violations are
   `IntegrityError`. Corrected in `45bd0da` (the over-length case asserted separately, two boundary tests added: 75 tests). The focused run then passed, 75 tests.
2. First combined regression: 566 tests, 6 failures and 3 errors, exit code 1; only one traceback was supplied
   (`RecoveryRaceTests.test_two_worker_processes_released_together_run_one_task_once`: state `queued`, attempt count 0, both workers "No task available."). Cause: the
   process-level children run the real `runworker`, which installs the production composition with the S4 kind gate; their `recovery.*` kinds are neither legacy kinds nor a
   module's, so the gate correctly held them. Corrected in `4ba709b` (the child adds exactly the three test kinds to the real gate's answer; the gate still fails closed
   and still holds every other kind; eight DB-free tests). The 15 recovery tests then passed. The individual tracebacks of the other reported failures were not supplied; by the
   code, the six process-level recovery tests share that cause.
3. Second combined regression: 567 tests, 3 errors (`IntegrityError`, constraint `audit_event_action_valid`, action `module.enabled`). Cause: three audit migration tests built
   their audit `0001` rows from the live `events.ACTIONS`, to which S4 added two actions the old fixed list refuses. Corrected in `0f8e44d` (a frozen `OLD_ACTIONS` of the
   seven original actions, four DB-free tests pinning it to the migrations, one new PostgreSQL test for the reverse refusal after a `module.enabled` event: 25 audit tests).
4. Final run: the combined selection passed, **567 tests, 153.186 s, exit code 0**.

**What was not supplied:** the command lines and module lists of the runs, the output, which database the migration was applied to and whether a backup was taken, and any
manual run of `module_status`, `module_sync`, `module_enable` or `module_disable` against a database (**not reported; not claimed**). The intended selection is the one in
[runbook.md](runbook.md), section 5.1.

**What this supports (this environment and these scenarios only):**

- **The migration and the constraints.** `module_host.0001_initial` applied; the stored shape checks, the unique module id and namespace, and the one-row marker behave as the tests expect.
- **Initialization.** The first sync writes the marker in its own transaction (an empty registry included); a refused or failed sync writes none; after it, missing module state fails closed.
- **Namespace ownership.** Claims persist and are not reassigned through the services; a changed or released namespace is refused and nothing is written.
- **Enable and disable.** Idempotent (no write, no event), locked, dependency-checked and audited in the change's transaction; concurrent syncs, enables and disables left rows and events consistent.
- **The worker gate.** Tasks of a disabled, version-mismatched or stateless module stayed `queued` untouched and were not failed, ran after an enable in their original order, a running attempt finished, and recovery,
  retry and fencing behaved as before; the real-process recovery tests passed with the production gate in the loop.
- **Legacy Request Intake compatibility.** `request_intake.extract` ran end to end through the production composition with no module state, with every unrelated module disabled and with the state rows gone but the marker present; the unchanged
  `test_intake_postgres` and `test_api_postgres` suites are part of the intended selection (the exact module list of the 567-test run was not supplied, so their pass is inferred from the
  all-passed result, not itemized).

**What it does not validate, and what does not exist.**

- It does not prove that no other concurrency race exists. The S2 gate race window (a kind gated off between the gate's read and the claim can be claimed by that one claim) remains, and the state is read once per iteration.
- Request Intake is **not** registered as a module (`BUILTIN_MODULES` is empty); nothing is gated in practice. MOD-2 must remove its kind from the legacy set when it registers it.
- There is **no API-side write blocking** for a disabled module, no `while_disabled` handling, no capability facade or `ModuleContext` (at S4; S6 added one, see its section below), no module installation and no Update Manager; a version mismatch has no remedy yet.
- Integrity is ORM and service level only (no trigger, no revoked privilege); raw SQL that deletes the marker and every state row reopens the legacy window. The audit table is not tamper-protected.
- If `module_sync` runs before MOD-2, the marker exists and the MOD-2 release's modules stay held until `module_sync` runs with that release (the upgrade routine).
- One machine, one operating system, one PostgreSQL. **This is not a production-readiness claim.**

## MOD-1b / S5 workflow attribution: owner-supplied local PostgreSQL results (2026-11-18)

> **Scope.** S5 gives every `WorkflowRun` a `module_id`, a `workflow_key` and a `workflow_version` (the workflow definition version), written once at creation and refused afterwards at the ORM level, through the additive
> migrations `cases.0006` to `0009` (branch `feat/module-workflow-attribution`: implementation `e2a4c55`; see [module-contract.md](../architecture/module-contract.md), section 3.12, and [runbook.md](runbook.md),
> section 5.2). The project owner ran the migrations and the PostgreSQL tests **locally on the Windows computer**. **These are owner-supplied results.** The development VM has no PostgreSQL, never connected to one,
> and did not run, repeat or observe any of it. No output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| PostgreSQL backup before the S5 migrations | Taken |
| Migrations `cases.0006` through `cases.0009` | Applied successfully to the owner's development database |
| Historical `WorkflowRun` records | **Four** existing runs were backfilled to `module_id = request_intake`, `workflow_key = request_intake`, `workflow_version = 1` |
| Focused PostgreSQL attribution tests (`tests/test_workflow_attribution_postgres.py`) | **37 passed** |
| Full selected PostgreSQL regression | **604 tests passed, 207.855 seconds, exit code 0** |
| Test database | The dedicated test database was destroyed successfully |
| DB-free regression in the development VM | 1998 tests passed |

**What was not supplied:** the command lines and module lists of the runs, the output, how the four runs were checked (a query, the shell or the tests), whether `migrate` stopped at `cases.0008` at any point (the reported
result is that the migrations completed), and any manual smoke test of Request Intake after the migration (**not reported; not claimed**). For orientation only (the VM's arithmetic, not stated by the owner): 604 equals the 37
attribution tests plus the 567 tests of the S4 selection; the intended selection is the one in [runbook.md](runbook.md), section 5.2, which also includes the cases persistence suite.

**What this supports (this environment and these scenarios only):**

- **The migrations.** `cases.0006` to `0009` applied on the owner's development database that already held runs; the four existing runs, all of them Request Intake runs, read as `request_intake` / `request_intake` / 1.
- **The tests.** The 37 PostgreSQL attribution tests passed: the migrations against existing rows (the backfill on evidence, the refusal of a run without a Request Intake source, idempotency, reverse and forward), the NOT NULL
  and CHECK constraints as PostgreSQL stores them, a new run's attribution, the ORM guard on a real database, and concurrent creation.
- **Existing Request Intake behaviour in the tested scenarios.** The selected regression, which includes the edited run-creating suites and the unchanged review, API and worker suites, passed with the attribution in place, so
  submission, replay, extraction, edit, approval and rejection, task claiming and the audit history behaved as before in those scenarios. The canonical hash comparison (a hash recorded from `main` before S5) is part of
  the attribution tests; the exact module list of the 604-test run was not supplied, so the pass of the individual existing suites is inferred from the all-passed result, not itemized.

**What it does not validate, and what does not exist.**

- Request Intake is **not** registered as a module (`BUILTIN_MODULES` is empty), nothing reads the attribution, and no module is activated.
- There is no `ModuleContext` or capability facade (at S5; S6 added one, see the next section), no module installation and no Update Manager, and no real AI integration.
- The attribution is checked for shape and reserved names, not against a registry; protection is ORM and service level only (no trigger, no revoked privilege), so raw SQL can change a value.
- It does not prove that no other race or edge case exists. One machine, one operating system, one PostgreSQL. **This is not a production-readiness claim.**

## MOD-1b / S6 ModuleContext capability facade: owner-supplied local PostgreSQL results (2026-11-18)

> **Scope.** S6 adds the `ModuleContext` (`apps/module_host/context.py`, the pure `context_rules.py` and `config/module_context.py`): an immutable, registry-backed context with four capability facades (`tasks`, `cases`, `audit`
> and `permissions`) that delegate to the existing Core services (branch `feat/module-capability-facade`: implementation `ee4198d`, test-only fix `5b61298`; see [module-contract.md](../architecture/module-contract.md), section 3.13, and
> [runbook.md](runbook.md), section 5.3). S6 adds no migration, model, command or route. The project owner ran the PostgreSQL tests **locally on the Windows computer**. **These are owner-supplied results.** The development VM has no
> PostgreSQL, never connected to one, and did not run, repeat or observe any of it. No output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Focused S6 PostgreSQL tests (`tests/test_module_context_postgres.py`) | **72 passed** |
| Full selected PostgreSQL regression | **676 tests passed, 191.518 seconds, exit code 0** |
| Test database | The dedicated test database was destroyed successfully |
| DB-free regression in the development VM | 2129 tests passed |

**What was not supplied:** the command lines and module list of the runs, the output, and any manual smoke test of Request Intake (**not reported; not claimed**; S6 changes nothing a smoke test would exercise, because nothing in the
application calls the context). Whether the runs used exactly the commit `5b61298` is the owner's stated expectation, not an output stored here. For orientation only (the VM's arithmetic, not stated by the owner): the intended selection is the
one in [runbook.md](runbook.md), section 5.3; that the 676 tests are those modules is inferred from the all-passed result, not itemized.

**What this supports (this environment and these scenarios only):**

- **The ModuleContext.** `ModuleContext` was implemented with four capability facades; the 72 PostgreSQL tests passed against real rows: task enqueue through the unchanged queue, runs with attribution and the state machine, audit
  inside and rolled back with the transaction, permission checks with real users and roles compared with Request Intake's own authorization, the module states (disabled, version mismatch, missing state, before the first sync), the
  shared row lock and the concurrency scenarios (a disable that waits for an in-flight call, and a call that waits for an uncommitted disable).
- **Existing Request Intake behaviour in the tested scenarios.** The selected regression passed with S6 in place, so submission, extraction, review, approval, rejection, task claiming, recovery, workflow attribution and the audit history
  behaved as before in those scenarios (S6 changes none of their code).

**What it does not validate, and what does not exist.**

- Request Intake is **not** registered as a module (`BUILTIN_MODULES` is empty), does not use a context, and nothing in the application calls the context; no module is activated.
- There is no real AI integration. `revision.approval` has no facade.
- **`ModuleContext` is not a security sandbox**: it is a naming and authority boundary inside one process; code admitted to the process can bypass it.
- There is no module installation and no Update Manager, no module endpoint and no API-side write blocking for a disabled module.
- It does not prove that no other race or edge case exists (the S2 gate race window is unchanged). One machine, one operating system, one PostgreSQL. **This is not a production-readiness claim.**

## MOD-2 Request Intake as the first built-in module: owner-supplied local PostgreSQL results (2026-11-19)

> **Scope.** MOD-2 declares Request Intake as a module (`apps/intake/module.py`), registers it in the static registry (`config/modules.py`) and gates its task kind `request_intake.extract` on the stored module state (`config/task_registry.py`; branch `feat/request-intake-module`: implementation `871bf2d`, test-only corrections `8915925` and `05cf1a7`;
> see [module-contract.md](../architecture/module-contract.md), section 3.14, and [runbook.md](runbook.md), section 5.4). MOD-2 adds no migration, model, endpoint, screen, permission or audit action. The project owner ran the PostgreSQL tests and `module_sync` **locally on the Windows computer**. **These are owner-supplied results.**
> The development VM has no PostgreSQL, never connected to one, and did not run, repeat or observe any of it. No output or log is stored in this repository.

**Reported by the owner:**

| Item | Reported result |
| --- | --- |
| Focused MOD-2 PostgreSQL tests (`tests/test_request_intake_module_postgres.py`) | **30 passed** (the first run had one error, a test setup defect corrected in `8915925`) |
| Namespace continuity tests (`tests.test_module_context_postgres.LegacyRequestIntakeTests`) | **4 passed** (the combined regression had one error, a test setup defect corrected in `05cf1a7`) |
| Full selected PostgreSQL regression | **701 tests passed, 220.591 seconds, exit code 0** |
| Test database | The dedicated test database was destroyed successfully |
| DB-free regression in the development VM | 2172 tests passed |
| `module_sync` on the local development database | Executed |
| `module_status` afterwards | Registry `d07104ee7579`; synced: yes; `request_intake`: enabled; registered 1.0.0; installed 1.0.0; **claimable: yes**; task kind `request_intake.extract` |

**The two corrected test defects** (tests only; production code, models and migrations did not change): (1) a missing-state test deleted a `ModuleState` row through the ORM, which refuses it by design; it now uses raw SQL as the S4 tests do and asserts that zero state rows and one initialization marker remain. (2) a context test synced a synthetic registry and then the production registry; namespace
continuity refuses that (`ctx_mod` released, `intake` reassigned), so the test now asserts the refusal, that the stored state and claims are unchanged, that no `request_intake` row appears and that the gate stays closed. The production sync after an earlier empty-registry sync is covered by the MOD-2 tests.

**What was not supplied:** the command lines and module list of the runs, the output, a backup record before `module_sync`, whether the development database held an initialization marker from an earlier empty-registry sync before `module_sync`, and any manual smoke test after the sync (a submission, a worker run, a browser walk-through): **not reported; not claimed**. `claimable: yes` is the status command's report of the gate's decision, not an
observed worker claim. For orientation only (the VM's arithmetic, not stated by the owner): 701 is 25 more than the 676 of the S6 selection; the intended selection is the one in [runbook.md](runbook.md), section 5.4.

**What this supports (this environment and these scenarios only):**

- **Registration and state.** Request Intake is registered as the first built-in module; `module_sync` created its state on the development database; the host reports it enabled, version-matched and claimable.
- **The worker gate.** The tests passed for the production composition: tasks held and never failed while the module is disabled, version-mismatched or without state after initialization; resumed in order after re-enable; a running attempt finishing after a disable; an expired lease recovered and then held; retry unchanged; the reconciler still moving permanently failed runs to failed; `test.add_numbers` unaffected.
- **Preserved contracts in the tested scenarios.** The groups `setup_roles` writes and the profile permissions equal the manifest's derivation; the `ModuleContext` decision equals Request Intake's own authorization for every user, action and owner; the audit vocabulary is Core's and existing events stayed readable; historical run attribution was untouched; the selected regression, which includes the unchanged intake, review, API, queue, worker,
  attribution, module host and context suites, passed, so submission, extraction, edit, approval, rejection and the REST API behaved as before in those scenarios (inferred from the all-passed result, not itemized).

**What it does not validate, and what does not exist.**

- **No API-side write blocking:** the API still accepts submissions while the module is disabled or has no state; the runs and their tasks stay queued.
- **The S2 gate race window remains.** No concurrency guarantee beyond the tested scenarios is claimed.
- Request Intake does **not** use `ModuleContext` internally; `revision.approval` has no facade; the manifest's capability list is a declaration. Nothing is sandboxed.
- There is no real AI integration, no module installation and no Update Manager, no module endpoint, and no licence or signature enforcement.
- It does not prove that no other race or edge case exists. One machine, one operating system, one PostgreSQL. **This is not a production-readiness claim.**

## AI-1b KPMG Workbench: owner-supplied local smoke test with GPT-5 (reported with AI-2a, 2026-11-19)

> **Scope.** One real call through the AI-1b Workbench provider, run by the project owner **locally**, reported in the instructions for AI-2a. The development VM did not run, repeat or observe it, never contacts the Workbench and holds no key. No output or log is
> stored in this repository, and no key, charge code or request text was shared with the VM.

**Reported by the owner:**

| Item | Reported value |
| --- | --- |
| Provider and profile | `kpmg_workbench`, `workbench-gpt5` |
| Deployment | `gpt-5-2025-08-07-dzs-sdc` |
| Reported model | `gpt-5-2025-08-07` |
| Structured JSON | valid |
| Duration | 2.33 seconds |
| Tokens | 107 input, 148 output |

**What this supports (that one call only):** the AI-1b request shape was accepted for the GPT-5 deployment, `choices[0].message.content` held valid JSON, and the answer reported a model and token usage ([ai-gateway.md](../architecture/ai-gateway.md) section 14.10(d)).

**What it does not validate:** the GPT-4.1 deployment, the HTTP status codes for failures, a proxy or TLS inspection, rate limits, Request Intake's prompt and schema (AI-2a), larger inputs, repeatability, retention or logging at the Workbench, or cost. It says nothing about extraction accuracy.
No PostgreSQL was involved. **The AI-2a PostgreSQL tests (`tests/test_intake_ai_postgres.py`, 35 tests) have not been run by anyone; nothing is recorded for them here.**
