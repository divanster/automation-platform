# Background Execution

> **Status: Proposed design; only a first slice is implemented, and the design as a whole is
> not proven.** Apart from that slice (see below), nothing here has been built, benchmarked, or
> tested. The mechanisms described are intended designs to be validated in Phase 4 and by the
> installation experiment that follows it.
>
> **Correctness of the PostgreSQL-backed queue is proven only for the scenarios tested.**
> Statements such as "two workers cannot hold the same task" or "recovery is bounded by the
> lease" describe the *intended* behavior of a design. The first slice's PostgreSQL tests passed
> in the owner's local run, which supports them for the tested scenarios; it does not support them
> beyond those (there is no heartbeat, restart and sleep on Windows were not tested, and nothing was
> measured; a later test-only slice did kill real worker processes, see the table in section 15). What is otherwise confirmed is the *direction*: a separate persistent
> worker sharing the backend domain code ([ADR 0003](../decisions/0003-persistent-background-worker.md)).
> ADR 0003 still records the queue mechanism as Proposed; this document does not change an ADR's status.
>
> **A first slice now exists in code** ([persistent-worker-slice.md](../development/persistent-worker-slice.md)):
> a generic queue and one deterministic test handler. Its database-free tests pass, and its 95
> PostgreSQL tests (including concurrency, lease, and recovery tests) and a manual smoke test passed
> in the owner's local run ([record](../development/local-validation-record.md)). That is evidence
> for the tested scenarios only; see "Evidence status" after the table in section 15 for what
> remains unproven. A follow-up test-only slice added process-level recovery tests (real worker
> processes, a forced kill, shutdown with a task in progress, and races against recovery); its 15
> PostgreSQL tests passed in the owner's local run (same record).

## Implementation status of the first slice (differences from this proposal)

- The state `ready` is named **`queued`** in code. Everything below that says `ready` means `queued`.
- There is no `cancel_requested` flag. Queued tasks can be cancelled; cancelling a **running** task
  is refused with a clear error (section 8's cooperative cancellation is not built).
- Claim takes only `queued` tasks. Expired leases are handled by a separate recovery step the worker
  runs before each claim (sections 4 and 5), not by the claim query.
- There is no heartbeat: lease renewal exists as a service function, but no process calls it.
  Handlers must finish within the lease.
- Idempotency keys are caller-supplied strings, unique across the installation; the
  `(run, step, retry generation)` composition of section 11 and the manual-retry task are not built.
- No concurrency limits (section 9), no `LISTEN/NOTIFY`, no cleanup of finished tasks, no Windows service.
- **The request-intake extraction handler** ([slice document](../development/request-intake-orchestration-slice.md);
  PostgreSQL tests written, **not run**) is the first handler that touches business tables. The allowlist
  `apps.tasks.handlers.HANDLERS` is unchanged; the worker entry point installs a composed, read-only allowlist
  built outside `apps.tasks` (`config/task_registry.py`) plus an after-iteration hook that reconciles runs whose
  task failed for good. A run is linked to its task by a table in `apps/intake`, not by a column on `Task`; the
  idempotency key is `request_intake.extract:<run>:<generation>`; only generation 1 exists.
- **Section 10 differs for this handler.** There is no single transaction that records the result and marks the
  task `succeeded`: the handler commits the outcome (revision, outcome row, run state) in its own short
  transaction, and the worker then records the task result under the lease check. A crash between the two is made
  safe by idempotence (a repeat returns the existing outcome), not by atomicity. The lease token does not fence the
  handler's domain write, only the queue's bookkeeping.

## 1. Proposal

Run background work with a **separate, persistent worker process** that uses a
**PostgreSQL-backed task queue**. The API process inserts task rows; the worker
claims and executes them. No separate message broker is introduced.
See [ADR 0003](../decisions/0003-persistent-background-worker.md).

Why a separate process rather than threads in the API process:

- Work such as AI calls may take long and must survive API restarts and
  request timeouts.
- Interactive requests must not compete with model calls for the same process.
- The worker can be restarted, limited, or stopped independently of the UI and
  API.

## 2. Why PostgreSQL as the queue

- **Fewer local services.** A standalone Windows installation already needs
  PostgreSQL. Using it as the queue avoids installing, securing, monitoring, and
  updating a second stateful service (such as a message broker) on every client
  machine.
- **Transactional enqueue.** A task is created in the same transaction as the
  business change that requires it, so there is no state where the record exists
  but its task was lost, or the reverse.
- **One backup and restore path.** Queue state is part of the database backup.

### Complexity and trade-offs

This is not free. The team takes on:

- designing claim, lease, retry, and recovery logic correctly,
- polling or `LISTEN/NOTIFY` for wake-ups (polling is simpler and sufficient to
  start),
- table growth and cleanup of finished tasks,
- throughput limits well below a dedicated broker (acceptable for the expected
  workload, which has not been measured).

### Alternatives

| Alternative | Notes |
| --- | --- |
| Broker-based queue (for example Celery with Redis or RabbitMQ) | Mature, but adds a stateful service to every installation and a harder Windows story. Not rejected forever; see revisit conditions in ADR 0003. |
| An existing PostgreSQL-backed task library for Python | Could reduce custom code. Candidates must be evaluated for maintenance status, Windows support, and Django integration before use. **Not verified here.** |
| Django's own task interface, if present in the selected Django version | Availability and worker capabilities must be checked against the chosen version. **Not verified here.** |
| Threads or in-process background jobs | Work is lost when the API process restarts; competes with requests. Rejected for AI work. |
| Windows Task Scheduler or cron-style jobs | Suitable for periodic maintenance, not for event-driven, retryable work. |

Phase 4 begins by deciding between a small custom implementation and an
evaluated library, using the requirements in this document.

## 3. Task model (summary)

Entities are defined in [data-model.md](data-model.md): `Task` is the queue
entry; `TaskAttempt` records each execution attempt.

Proposed task states:

| State | Meaning |
| --- | --- |
| `ready` | Eligible to run once its `run_after` time has passed |
| `running` | Claimed by a worker under an unexpired lease |
| `succeeded` | Completed and its result committed |
| `failed` | Retries exhausted |
| `cancelled` | Cancelled before completing |

A task also carries a `cancel_requested` flag, an attempt counter, a retry
limit, `run_after`, lease owner, lease token, and `lease_expires_at`. Task
states are internal to execution; business state is `WorkflowRun.state`
([first-workflow.md](first-workflow.md)).

### Task versus task attempt

- A **`Task`** is one unit of work for a run step (for example `extract` for
  run R). It has one overall outcome.
- A **`TaskAttempt`** is one claim and execution of that task. Automatic
  retries after a retryable failure or an expired lease are additional
  attempts under the **same** task.
- A **manual retry** by a user after the run reached `failed` creates a **new
  task** with the next *retry generation*; the failed task and its attempts are
  kept unchanged as history.
- Task state describes execution only. A task failing never changes a result
  revision or an approval.

## 4. Atomic claim

A worker claims work in one short transaction:

1. Select one eligible task (`ready` with `run_after` reached, or `running` with
   an expired lease) using row locking that skips rows locked by other workers
   (PostgreSQL `FOR UPDATE SKIP LOCKED`).
2. In the same transaction: set state `running`, record the lease owner and a
   new lease token, set `lease_expires_at`, increment the attempt counter, and
   insert a `TaskAttempt` row.
3. Commit.

Because claim is a single locked transaction, two workers cannot hold the same
task. This must be verified by a concurrent test in Phase 4 rather than
assumed.

## 5. Lease and expiry

- A claim grants a **lease** for a bounded time.
- A worker running a long task **renews** the lease periodically (heartbeat).
- If the lease expires, the task becomes claimable again. The earlier attempt is
  marked `abandoned`.
- Lease durations and heartbeat intervals are configuration; **no values are
  proposed here** and they will be chosen from measurement.

### Fencing stale workers

A worker that was paused (for example the machine slept) may resume after its
lease expired and another attempt took over. To stop it from committing stale
results, every result commit is conditional: it applies only if the task is
still `running` under **the same lease token**. A stale worker's commit affects
zero rows and its result is discarded.

## 6. Bounded retries and backoff

- Each task kind has a **maximum number of attempts**.
- A failed attempt that is retryable returns the task to `ready` with a later
  `run_after` (increasing delay, with some randomization to avoid synchronized
  retries).
- Non-retryable failures (for example invalid input, policy violation) fail the
  task immediately.
- After the last attempt the task becomes `failed` and the domain layer moves the
  run to `failed` with `failed_step`.
- Specific limits and delays are decided in Phase 4.

## 7. Recovery after worker restart

- On startup the worker holds no assumptions about earlier work. It simply claims
  eligible tasks.
- Tasks left `running` by a crashed worker become claimable after their lease
  expires, so recovery time is bounded by the lease duration.
- Recovery relies on state in the database only. The worker keeps nothing
  important in memory.
- This behavior is an acceptance test: kill the worker mid-task and verify that
  the task completes once, without a duplicate result
  (see AC-12 in [first-workflow.md](first-workflow.md)).

## 8. Cancellation semantics

- Cancelling a run sets `cancel_requested` on its non-finished tasks.
- A `ready` task is cancelled immediately.
- A `running` task is cancelled **cooperatively**. The worker checks the flag
  between steps and before committing. A model call already in flight may not be
  abortable, so its result is discarded rather than interrupted.
- The final commit is conditional on the task not being cancelled and the run
  still being in the expected state.
- If cancellation and completion race, the first transition to commit wins; the
  other finds the state changed and does nothing.
- A file written to storage by a discarded export result is an orphan and is
  removed by a maintenance check, not trusted to be absent
  ([data-model.md](data-model.md), section 7).
- The same discard rule applies when a human edit supersedes the revision an
  export was started from: the edit requests cancellation of the export task,
  and a late result is discarded.
- Cancellation never deletes documents, revisions, approvals, or audit history.

## 9. Concurrency limits

Limits are needed so a standalone machine is not overwhelmed and external
provider limits are respected:

- total tasks running per worker,
- tasks running per model profile (provider limit),
- optionally per task kind.

All limits are configuration. **No numbers are proposed**; Phase 4 and the
feasibility experiment must measure on representative hardware.

## 10. Transaction boundaries

The core rule: **no database transaction is held open during an AI call or any
other slow operation.**

Execution of one task:

1. **Transaction 1 (short):** claim the task (section 4). Commit.
2. **No transaction:** read the input document and configuration snapshot, call
   the AI gateway, run deterministic validation.
3. **Transaction 2 (short):** conditionally commit the result (new
   `ResultRevision`, run state change, audit event, mark task `succeeded`) if
   the lease token is still current and the task is not cancelled.

Holding a transaction (and its row locks and connection) across a model call
would block other work, exhaust connections, and make failure recovery
unpredictable.

## 11. Duplicate prevention and idempotency

The system aims for **at-least-once execution with idempotent effects**:

- **Enqueue idempotency:** each logical unit of work has an idempotency key
  made of the run id, the step name, and a **retry generation** number; a unique
  constraint prevents enqueueing the same generation twice. A deliberate manual
  retry increments the generation, so it is a distinct, allowed task rather
  than a duplicate.
- **Result idempotency:** database uniqueness constraints stop a repeated
  execution from creating a second result (for example at most one `extraction`
  revision per run; one export artifact per revision and format).
- **File output idempotency:** files are written to a temporary name and moved
  into place atomically under a deterministic storage key.
- **Conditional commits:** the lease-token check in section 5.

## 12. Exactly-once execution is not promised

General exactly-once execution cannot be guaranteed. A worker can fail after
doing work but before recording that it did so, and a lease can expire while the
worker is still running. Therefore:

- a task can run more than once,
- an AI call may be repeated (cost and latency, but no duplicated business
  result because of the constraints above),
- documentation and user interfaces must not promise exactly-once behavior.

Effects inside the database can be made effectively exactly-once through
transactions and constraints. Effects outside the database cannot, which is why
the first slice has none.

## 13. Ambiguous external outcomes (future)

The first slice performs no external actions. If a later workflow performs one
(for example sending a message or calling another system), a worker crash or
timeout can leave the outcome **unknown**. The proposed approach for that future
work:

1. Record the **intent** (with an idempotency key) before attempting the action.
2. Send the idempotency key to the external system when it supports one.
3. If the outcome is unknown, record a distinct `outcome_unknown` condition. Do
   **not** retry automatically.
4. Require a human to reconcile (check the external system) and record the
   result before further action.
5. Such actions require human approval first and are out of scope until a
   dedicated design is reviewed.

## 14. What is not decided

Library versus custom implementation, polling interval versus `LISTEN/NOTIFY`,
lease and retry values, cleanup policy for finished tasks, and how the worker
runs as a Windows service ([deployment.md](deployment.md)).

## 15. What must be proven before the design is considered correct

None of the following has been done. Each is a required test or experiment in
Phase 4 ([implementation-plan.md](implementation-plan.md)); until all pass, the
queue design stays **unproven**.

| Claim to be proven | Required evidence |
| --- | --- |
| Two workers never run the same task at the same time | Concurrent claim test with multiple worker processes against a real PostgreSQL |
| A crashed worker's task is recovered after lease expiry | Test that kills a worker mid-task and observes recovery without a duplicate result |
| A stale worker cannot commit | Test that expires a lease, lets another attempt win, then lets the first worker try to commit |
| Retries are bounded and backed off | Test that fails a task repeatedly and checks the limit and delays |
| No transaction is held open during task work | Test or inspection showing no open transaction while the test adapter or a simulated slow call runs |
| Cancellation behaves as specified, including races | Tests for each cancellable state and for cancel-versus-complete races |
| Manual retry creates a new task and keeps history | Test following the retry-generation rule |
| Behavior across restart and sleep on Windows | Part of the installation feasibility experiment, not only unit tests |

**Evidence status after the first slice and the process-level recovery tests** (owner-reported local
runs; the tests are in `backend/tests/test_tasks_postgres.py` and
`backend/tests/test_tasks_recovery_postgres.py`):

| Claim | Status |
| --- | --- |
| Two workers never run the same task at the same time | Supported for the tested scenarios: competing **threads**, each on its own connection, and two real worker **processes** (two tasks held at once; one task raced for by two processes ran once). Not tested: more than two processes, several hosts, load |
| A crashed worker's task is recovered after lease expiry | Supported for the tested scenarios: a worker **process was killed** mid-task, the task stayed `running` until its lease expired, a later worker process recovered it, and it completed once; repeated kills ended at the attempt limit. Lease expiry was simulated with a clock offset, not waited out. Not tested: power loss, restart or sleep on Windows; nothing restarts a dead worker (no supervisor) |
| A stale worker cannot commit | Supported: tested before recovery, after recovery, after reclaim, and after a real kill |
| Retries are bounded and backed off | Supported for the tested limits and values (provisional, not measured) |
| No transaction is held open during task work | Supported by an in-handler check and a read from a second connection |
| Cancellation behaves as specified, including races | Supported for queued tasks and for cancel versus claim; running-task cancellation is deliberately unsupported |
| Manual retry creates a new task and keeps history | Not built |
| Graceful shutdown with a task in progress | Supported for the tested scenarios: the first signal (`CTRL_BREAK_EVENT` on Windows, `SIGINT` elsewhere) lets the active task finish and the worker exits 0; a second signal exits 130 and the task is recovered after its lease. `SIGTERM`, `taskkill` without `/F`, and a service stop are not tested |
| Completion, failure, and renewal against lease recovery | Supported for the tested scenarios: both forced orders and unordered races on separate connections ended in one consistent state. Not every interleaving is covered |
| Behavior across restart and sleep on Windows | Not tested |
