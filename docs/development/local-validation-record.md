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
hash-locked package set is separate from the interpreter patch version; whether the local
environment was built from `requirements/*.txt` was **not reported**.

The local package versions (Django, DRF, psycopg) were not reported, so none is recorded
here as locally observed. The pinned versions in the [runbook](runbook.md) are what the
repository specifies.

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
| The test suite runs and passes from a clean checkout following written instructions | **Partly closed.** DB-free suite passed locally (owner-confirmed, no count). Not established: that the checkout was clean and the written steps were followed as written | Owner confirmation only |
| A fresh virtual environment built from the recorded pins reproduces the same package set | **Closed on the VM** (2026-11-10). **Open locally:** whether the local environment was built from the locks was not reported | VM record; no local report |
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
| Hash-locked install on the local computer | **Open** (not reported) | None |
| PyCharm project and interpreter | **Open / not confirmed** | None |
| CI definition (optional) | **Not requested** | None |
| PostgreSQL server configuration meets the runbook's loopback-only rule | **Open: not met as reported.** See below | Owner report |

Phase 2's database-backed criteria are supported by the evidence above. Overall Phase 2 closure
is the project owner's decision at its stop-and-report gate; the open rows above and the
deployment item below are the remaining gaps. No later phase is marked complete.

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

## Still deferred

Authentication and authorization (including linking approvals to authenticated users, required
before any API exposes approval operations), the background worker, an API, a UI, audit
events, race-safe export (the atomic re-check at export commit), database-level immutability,
document storage, the state machine, and real AI adapters. The open questions that cover them
are in [open-questions.md](../architecture/open-questions.md).

## Stale code comments (not changed by this record)

The `STATUS` paragraph at the top of `backend/tests/test_cases_persistence_postgres.py` still
says those tests were not run and their guarantees are unverified; that was true when it was
written and is now out of date. The note in `backend/apps/cases/services.py` that the locking
was not exercised by a database test refers to the development VM and is still accurate
there, but it does not mention the local run. This record is documentation-only, so code was
deliberately not modified; correct both comments in the next code change.
