# Persistent worker slice (generic queue and one test handler)

> **Status: implemented. DB-free tests pass in the VM; the 95 PostgreSQL tests and a manual smoke
> test passed in the owner's local run** ([record](local-validation-record.md); owner-supplied local
> results). That supports claim, lease, recovery, and idempotency behavior **for the tested
> scenarios only**; it does not prove that no other race exists. Real worker processes were
> added later by test-only process-level recovery tests (section 9.4), which also passed in the
> owner's local run ([background-execution.md](../architecture/background-execution.md), section 15,
> "Evidence status"). The VM never ran the PostgreSQL tests. This slice does not connect the
> request-intake workflow, performs **no external effect**, authorizes none, and registers no Windows service.

## 1. Scope

In (`backend/apps/tasks`): a generic PostgreSQL-backed queue: enqueue with an optional idempotency key,
atomic claim, lease fencing, bounded retries with backoff, recovery of expired leases, cancellation
of queued tasks, one deterministic side-effect-free **test** handler, and the `runworker` command.

Out: API, UI, real AI, documents, request-intake orchestration, authentication, export execution,
central monitoring, Windows service packaging, concurrency limits, a heartbeat framework,
`LISTEN/NOTIFY`, and cleanup of finished tasks.

## 2. Design decisions and trade-offs

| Topic | Decision | Why / trade-off |
| --- | --- | --- |
| Where | New Django app `apps.tasks`. `policy.py` and `handlers.py` use only the standard library (tests enforce it). | No `Task` or `TaskAttempt` existed, so nothing was reused or duplicated. Pure modules are testable without a database. The app imports nothing from `apps.cases` or `workflows`. |
| Library or custom | A small custom implementation. | Recorded for this slice only; no dependency was added. A library is not ruled out later. |
| Task states | `queued`, `running`, `succeeded`, `failed`, `cancelled`. | The architecture documents say `ready`; the code uses `queued` and the documents now say so. Task state is **not** `WorkflowRun` state. |
| Business data | A task holds a bounded input payload and a bounded result only. No revision, approval, or case is stored or referenced. | A task failing never changes a revision or an approval. |
| Time source | The application clock (`django.utils.timezone.now`, UTC) is read once per operation and that single instant is used for every availability and lease comparison. The database clock is not used. Every service function takes an optional `now`. | Time rules are testable with fixed instants and no sleeps. Assumes workers and the enqueuer share a host (the standalone installation) or have synchronized clocks. A clock step (sleep, NTP) shifts leases; a late worker is still fenced by its token. |
| Idempotency scope | The key is **global** across the installation, not per kind. The database enforces it with a partial unique index (non-null keys only). Identity of a request is kind + canonical payload (SHA-256 fingerprint). `max_attempts` and `available_at` are scheduling options and are **not** part of identity. | Same key and same request returns the existing task, in any state, including finished ones (never restarted); a repeat with other scheduling options returns the original unchanged. Same key with a different kind or payload raises `IdempotencyConflictError`. Keys live as long as the row (no cleanup). Do not put sensitive data in keys. |
| Claim | One short transaction: `SELECT ... WHERE state='queued' AND available_at <= now AND attempt_count < max_attempts ORDER BY available_at, created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`; then state `running`, a **new** lease token, the lease expiry, `attempt_count + 1`, and a `TaskAttempt` row. Committed before the handler runs. | The worker refuses to run a handler inside an open transaction. A partial unique index allows at most one `running` attempt per task, a second guard. |
| Recovery | A separate `recover_expired_leases` step that the worker runs before every claim. | Keeps claim simple and recovery explicit. Cost: an expired task waits for the next worker iteration. |
| Fencing | Each worker mutation (`renew_lease`, `complete_task`, `fail_task`) locks the one row with `SELECT ... FOR UPDATE` and a WHERE on `state='running'`, the lease token, and `lease_expires_at > now`; no row means `LeaseLostError` and nothing is written. The task and its attempt are changed in one transaction. | An expired or superseded worker cannot write, even before recovery has run. The lease is valid strictly while `now < lease_expires_at`. The refusal reason (`not_running`, `token_mismatch`, `lease_expired`, `not_found`) is advisory. |
| Retry policy | Default 3 attempts (limit 1 to 10); backoff `min(300 s, 5 s * 2^(n-1))`, reduced by up to 20 % jitter. **Provisional values, not measured** (the architecture says values come from measurement). | Bounded. A lease expiry also counts as an attempt, so a handler that kills its worker cannot loop forever. |
| Failure classes | Retryable (`TaskRetryableError`), permanent (`TaskPermanentError`, invalid payload, unknown kind, invalid result), and unexpected exception. An unexpected exception is treated as **retryable within the attempt limit** with code `handler_error`. | Only a closed set of safe codes is stored. Messages, payloads, results, and tracebacks are never stored or logged; only the exception class name is logged. |
| Cancellation | Queued tasks (including ones waiting for a retry) can be cancelled. A running task is refused with `CancellationNotSupportedError`. Cancelling a cancelled task changes nothing. Succeeded or failed tasks are refused with `TaskTerminalError`. Nothing restarts a finished task. | Cancelling cannot undo external effects, and cooperative cancellation is not built. |
| Worker loop | Polls with an integer interval of 1 to 60 s (default 2) and sleeps in slices of at most 1 s. A database error closes the connections and is retried after the interval, up to 5 times in a row, then the worker stops. | No busy loop. The driver's error text is never logged or printed (only the class). |

## 3. Data

`Task`: `id` (UUID), `kind`, `idempotency_key` (nullable), `request_fingerprint`, `payload` (JSON,
at most 4096 bytes canonical, no floats, depth at most 5), `state`, `available_at`, `max_attempts`,
`attempt_count` (claims so far), `lease_token`, `lease_expires_at`, `result` (JSON, at most 4096
bytes, set on success), `failure_code` (set when failed), `created_at`, `finished_at` (terminal
states only). Timestamps carry no `auto_now`, so the injected clock is the only time source.

`TaskAttempt`: `id`, `task`, `attempt_number` (unique per task), `worker_id` (process id plus a random
suffix; no host or user name), `lease_token`, `started_at`, `ended_at`, `outcome` (`running`,
`succeeded`, `failed`, `abandoned`), `failure_code`. `cancelled` is not an attempt outcome because
running tasks cannot be cancelled yet.

Database CHECK constraints tie the lease fields to `running`, `finished_at` to terminal states,
`failure_code` to `failed`, started states to `attempt_count >= 1`, and `attempt_count <= max_attempts <= 10`.
They were rendered to SQL and reviewed, and their logic is tested in Python, but they have **not**
been run against PostgreSQL. The ORM does not stop code that bypasses the services (`Task.save()`,
`QuerySet.update()`, raw SQL); only the constraints above guard that.

Allowed transitions: `queued -> running` (claim), `queued -> cancelled`, `running -> succeeded`,
`running -> failed`, `running -> queued` (retry or recovery requeue, always with a future
`available_at`). Terminal states have no outgoing transition.

Migration: `apps/tasks/migrations/0001_initial.py` (creates two tables; touches no existing table).

## 4. Guarantees and non-guarantees (intended; PostgreSQL behavior NOT proven)

* Two workers should not hold the same active task: row lock with `SKIP LOCKED`, plus one running attempt per task by index.
* Each claim gets a fresh lease token; a stale token cannot change the task.
* Recovery closes an expired attempt as `abandoned` (code `lease_expired`) and either requeues (attempts remain, backoff applies) or marks the task `failed`.
* **Handlers run at least once and may run more than once** (a crash after the effect, or a lease that expires while the handler still runs). Exactly-once execution is **not** provided. Fencing stops a stale worker from *recording* a result; it cannot stop its handler from having run.
* **No external side effects are authorized in this slice.** The only handler is a pure test function. A future handler with external effects needs its own reviewed design (see background-execution.md, section 13).

## 5. Long-running handlers (limitation)

A handler must finish within the lease (default 60 s). There is no heartbeat. A handler that outlives
its lease is fenced: its late result is discarded (`lease_lost`), the task is recovered, and it runs
again. Later, a heartbeat will keep the lease: a separate thread or process with its own connection
calling `renew_lease` about every third of the lease, plus a cooperative stop flag. That design needs
its own review (cancellation races, connection use, behavior when the machine sleeps). `renew_lease`
exists and is tested, but nothing calls it yet.

## 6. Test handler

`test.add_numbers` is a **test handler**, registered in a fixed read-only mapping; nothing is
resolved from a string path, imported by name, evaluated, or executed. Payload:
`{"values": [1 to 20 integers, each |v| <= 1,000,000], "label": optional string, at most 40 characters}`.
Result: `{"count": n, "sum": s}` plus `"label"` when given. It performs no network, filesystem, AI,
clock, randomness, or database access.

## 7. Operation

Run from `backend\` with the project's virtual environment and the database variables set (runbook,
section 6.E):

```powershell
python manage.py runworker --once      # process at most one available task, then exit
python manage.py runworker             # poll until Ctrl+C
python manage.py runworker --poll-seconds 5 --lease-seconds 120
```

Defaults come from `AP_WORKER_POLL_SECONDS` (2) and `AP_WORKER_LEASE_SECONDS` (60); command-line
values override them; out-of-range values are refused before anything runs.

* **Per iteration:** recover expired leases, claim one task (short transaction, committed), run the handler with no transaction open, then record the outcome under the lease checks.
* **Idle:** the worker waits the poll interval; after a processed task it looks again at once.
* **Shutdown:** the first Ctrl+C (or SIGTERM, or Ctrl+Break on Windows) lets the current task finish, then the worker exits with code 0 and closes its connections. A second Ctrl+C interrupts at once (exit code 130); a task being run is then recovered after its lease expires and **runs again**.
* **Crash or kill:** nothing special happens at shutdown; the lease expires, and the next worker's recovery step requeues the task (with backoff) or fails it when attempts are exhausted. Recovery time is bounded by the lease plus the backoff.
* **Output and logs:** task ids, kinds, attempt numbers, statuses, and safe failure codes only. No payloads, results, messages, or tracebacks.
* **Not done:** no Windows service, no scheduler; start it by hand.

## 8. Files

New: `backend/apps/tasks/` (`__init__.py`, `apps.py`, `errors.py`, `policy.py`, `handlers.py`,
`models.py`, `services.py`, `worker.py`, `management/commands/runworker.py`,
`migrations/0001_initial.py`), `backend/tests/test_tasks_policy.py`, `test_tasks_models.py`,
`test_tasks_worker.py`, `test_tasks_postgres.py`, and this document.

Changed: `backend/config/settings/base.py` (`apps.tasks`; `WORKER_LEASE_SECONDS`, `WORKER_POLL_SECONDS`),
`.env.example`, `backend/tests/test_settings.py` and `test_request_intake_django.py` (the installed
project apps list now includes `apps.tasks`), and status text in `CLAUDE.md`, `README.md`, the
runbook, and the architecture documents (README, overview, implementation plan, background execution).

## 9. Validation

### 9.1 In the VM (no database), recorded at the time of writing

Run from `backend\` with `..\.venv\Scripts\python.exe`:

| Command | Result |
| --- | --- |
| `manage.py test` | `Ran 684 tests`, OK, exit code 0 (514 before this slice plus 170 new) |
| `manage.py test tests.test_tasks_policy tests.test_tasks_models tests.test_tasks_worker` | `Ran 170 tests`, OK |
| `python -m unittest` on the pure-domain modules and `tests.test_tasks_policy` (no Django) | `Ran 466 tests`, OK, exit code 0 |
| `manage.py check` | no issues |
| `manage.py check --deploy` (production settings, placeholder values) | only `security.W004` and `W008`, as before |
| `python -m compileall -q apps config tests manage.py workflows` | exit code 0 |
| `makemigrations --check --dry-run` (history check patched out; no connection is opened) | `No changes detected` |

The migration-state test (`tests/test_tasks_models.py`, `MigrationTests`) also runs in the suite.
`migrate`, `sqlmigrate`, and `showmigrations` need a database and were **not** run.

### 9.2 PostgreSQL tests: run locally by the owner

`backend/tests/test_tasks_postgres.py`, 95 tests, all tagged `requires_postgres`: `SchemaTests` 15,
`EnqueueTests` 12, `ClaimTests` 9, `LeaseTests` 21, `RecoveryTests` 10, `CancellationTests` 7,
`TerminalStateProtectionTests` 3, `ClaimConcurrencyTests` 8, `WorkerProcessTests` 10. In the VM they
were only collected and reported as skipped (95 skipped, no connection opened). The owner then ran
them locally: **95 passed, exit code 0**, and all **130** PostgreSQL-required tests (35 earlier plus
95) **passed, exit code 0**, with the test database destroyed afterwards, as supplied by the owner. The manual smoke test and the shutdown check of section 9.3 were also run; the
results are in the [local validation record](local-validation-record.md).

### 9.3 Local validation steps (owner; on the local Windows computer only)

Use the setup of runbook section 6 (the `$LocalProject`, `$Py`, and `AP_DB_*` variables) and the same
rules: dedicated development server, no `--keepdb`, no `--noinput`, flag removed afterwards.

```powershell
Set-Location "$LocalProject\backend"
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set. Remove-Item Env:AP_RUN_DB_TESTS and repeat." }

# 1. Update the checkout to the reviewed commit of this slice, then apply the new migration to the development database.
& $Py manage.py showmigrations tasks
& $Py manage.py migrate                            # applies tasks 0001_initial
& $Py manage.py makemigrations --check --dry-run   # expect: No changes detected

# 2. DB-free tests.
& $Py manage.py test                               # expect: Ran 684 tests, OK

# 3. The new PostgreSQL tests only. Creates and drops test_<AP_DB_NAME>.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_tasks_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 95 tests, OK, none skipped, exit code 0
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 4. All PostgreSQL tests (35 earlier + 95 new).
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 130 tests, OK
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 5. Manual smoke test against the development database (writes one task row; use a new key to repeat).
& $Py manage.py shell -c "from apps.tasks import services; r = services.enqueue_task(kind='test.add_numbers', payload={'values': [1, 2, 3]}, idempotency_key='smoke-1'); print(r.task.pk, r.created)"
& $Py manage.py runworker --once                   # expect: Task <id> succeeded.
& $Py manage.py runworker --once                   # expect: No task available.
& $Py manage.py shell -c "from apps.tasks.models import Task; t = Task.objects.get(idempotency_key='smoke-1'); print(t.state, t.result, t.attempts.count())"   # expect: succeeded {'count': 3, 'sum': 6} 1

# 6. Shutdown: start the continuous worker, then press Ctrl+C once.
& $Py manage.py runworker --poll-seconds 2         # expect: "Worker ... started", then after Ctrl+C: "Worker stopped after processing 0 task(s)."
"exit code: $LASTEXITCODE"                         # expect 0
```

Report the exact `Ran N tests` lines, any failure, and the output of steps 5 and 6. If a concurrency
test fails or hangs, report it as it is; do not loosen it. Hangs are bounded by lock and join
timeouts (10 s and 30 s), so a stuck lock shows up as a failure, not a frozen run.

### 9.4 Real-process recovery tests (test-only follow-up; run locally by the owner)

`backend/tests/test_tasks_recovery_postgres.py`, 15 tests tagged `requires_postgres`
(`MultiProcessClaimTests` 2, `KilledWorkerTests` 2, `GracefulShutdownWithActiveTaskTests` 2,
`RecoveryRaceTests` 9), with the test-only support in `tests/recovery_support.py` and
`tests/recovery_worker_child.py` and DB-free tests in `tests/test_tasks_recovery_support.py`. They start
real `manage.py runworker` child processes (test handlers are injected through the existing `registry`
and `clock` parameters of `process_one`; the production allowlist and production code are unchanged),
kill one forcibly, and race worker mutations against recovery. In the VM they are only collected and
skipped (15 skipped); the DB-free suite passes. The owner ran them locally: **15 passed, exit code 0,
37.091 s, test database destroyed** (an earlier local run failed three tests because of a defect in the
test helper, fixed in test code; see the [record](local-validation-record.md)). Windows signal
limits and the design are described in the module docstrings. Run from `backend\` with the setup of
section 9.3 (no `--keepdb`, no `--noinput`, flag removed afterwards):

```powershell
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_tasks_recovery_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect: Ran 15 tests, OK, none skipped, exit code 0
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }
```

## 10. Remaining limitations

* The database-dependent behavior is supported only for the scenarios in `test_tasks_postgres.py`, `test_tasks_recovery_postgres.py`, and the manual smoke test. The tests do not prove the absence of other races. The recovery tests kill a real worker process, but lease expiry in them is simulated with a shifted clock, and only one machine and operating system were used. `SIGTERM`, `taskkill` without `/F`, a service stop, restart, and sleep on Windows were not tested.
* Time comes from the application clock; skewed clocks across hosts are not handled.
* No heartbeat, no supervisor that restarts a dead worker, no concurrency limits, no cooperative cancellation of running tasks, no cleanup of finished tasks, no monitoring, no Windows service.
* Lease, retry, and polling values are provisional guesses, not measurements.
* The queue is not connected to cases, runs, or the request-intake workflow, and no endpoint or UI exists.
* Task rows hold business payloads in plain JSON in the database; nothing here encrypts or access-controls them, and there is no authorization on enqueueing. Do not enqueue sensitive data until that is designed.
