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

Authentication and authorization (including linking approvals to authenticated users, required
before any API exposes approval operations), connecting the queue to any workflow, a worker
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

## Pending: request-intake orchestration slice (not yet validated)

The request-intake orchestration slice ([slice document](request-intake-orchestration-slice.md)) has been
written, and its DB-free tests pass in the development VM. **No PostgreSQL result exists for it.** Its 76
PostgreSQL tests, the `cases 0002` to `0004` and `intake 0001` migrations (including their effect on a
database that already holds runs), and the manual smoke test are all waiting for the owner's local run.
Nothing in this record supports any claim about them until the owner pastes that output here.
