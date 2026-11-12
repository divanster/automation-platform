# Implementation Plan

> **Status: Product implementation has not started.** This plan sequences the
> work. It contains **no delivery dates** and no effort estimates; those require
> decisions and measurements that do not exist yet. Phases 0 and 1 produced
> documentation only. Phase 2 has produced a backend skeleton (see "Phase 2
> status") and early persistence for revisions and approvals; no business workflow, worker,
> AI, frontend, or installer code exists.

## How to use this plan

- Phases run in order. A phase starts only when its **stop-and-report gate** in
  the previous phase has been reviewed and accepted by the project owner.
- Each phase states its allowed scope and explicit exclusions. Work outside them
  needs a plan change first.
- "Tests and acceptance" lists what must be demonstrated, not what is already
  done. Nothing below has been performed unless explicitly marked.
- Reference documents: [overview.md](overview.md),
  [first-workflow.md](first-workflow.md), [data-model.md](data-model.md),
  [background-execution.md](background-execution.md), [ai-policy.md](ai-policy.md),
  [deployment.md](deployment.md), [open-questions.md](open-questions.md).

## Phase summary

| Phase | Name | Status |
| --- | --- | --- |
| 0 | Repository preflight | Performed (read-only checks) |
| 1 | Architecture specification | Reviewed and revised; decisions recorded |
| 2 | Backend skeleton | **Implemented; database-backed acceptance criteria supported by local evidence** (PostgreSQL 18.6, 35 PostgreSQL-required tests OK, health endpoints `ok`); open: a clean-checkout reproduction (not yet reported) and the `listen_addresses='*'` hardening item; closure is the owner's decision (see "Phase 2 status" below and the [local validation record](../development/local-validation-record.md)) |
| 3 | Data, documents, and basic authorization | Not started |
| 4 | Persistent worker | Not started |
| (checkpoint) | Installation feasibility experiment | Not started |
| 5 | End-to-end workflow with deterministic test adapter | Not started |
| 6 | Real AI adapters and policy enforcement | Not started |
| 7 | Minimal frontend | Not started |
| 8 | Installer, backup, restore, and controlled updates | Not started |
| 9 | Pilot and repeatable company configuration | Not started |

---

## Phase 0: Repository preflight

- **Goal:** confirm the repository is in a known, safe state before any design or
  build work.
- **Deliverables:** a recorded preflight report: working directory, repository
  root, branch, working tree status, remotes (without credentials), latest local
  commit, and local upstream configuration.
- **Dependencies:** repository initialized with initial files.
- **Allowed scope:** read-only inspection.
- **Exclusions:** any file change, fetch, pull, push, branch switch, or identity
  change. Local upstream configuration must not be treated as proof of a
  successful push.
- **Tests and acceptance:** the Git root equals the expected project root; the
  working tree status is understood; target documents have no unrelated
  uncommitted changes.
- **Stop-and-report gate:** report findings. Stop if the root differs or target
  files have unrelated changes.

## Phase 1: Architecture specification

- **Goal:** a coherent, reviewable architecture specification and plan.
- **Deliverables:** the documents in `docs/architecture/`, the ADRs in
  `docs/decisions/` (including the technology-baseline ADR 0007), and the
  open-questions list. A Phase 1 review pass corrected inconsistencies and
  recorded the owner's confirmed directions.
- **Dependencies:** Phase 0.
- **Allowed scope:** Markdown documentation only.
- **Exclusions:** application code, dependency files, Django or React
  initialization, Docker, services, installers, licenses, dates, invented
  versions or performance figures.
- **Tests and acceptance:** relative links and ADR references resolve; state
  names and domain terms are consistent across documents; every phase has
  acceptance criteria; no planned component is described as implemented; no
  secrets or customer data are present; the project owner reviews and accepts or
  amends the documents.
- **Stop-and-report gate:** report created files, trade-offs, and the decisions
  needed before Phase 2. Do not start Phase 2.

## Phase 2: Backend skeleton

- **Goal:** a minimal, runnable, tested backend structure on the chosen
  toolchain, with module boundaries from [overview.md](overview.md) in place.
- **Deliverables:** project layout with the proposed modules as empty or minimal
  structures; the technology baseline from
  [ADR 0007](../decisions/0007-technology-baseline.md) implemented (Python 3.14
  series with the documented 3.13 fallback, Django 5.2 LTS at 5.2.8 or later,
  Django REST Framework 3.18 series, `venv` and `pip`) with **exact versions
  recorded**; reproducible dependency records following the ADR 0007 policy
  (exact pins including transitive dependencies, development and production
  dependencies separated, no unbounded versions); the Python version recorded
  in the repository; configuration loading that keeps secrets out of source
  control (`.env.example` only); Django built-in users with session
  authentication; a health endpoint; test runner and lint/format configuration;
  a connection to a local PostgreSQL development database; a basic CI definition
  if desired.
- **Dependencies:** Phase 1 reviewed; the owner decided PostgreSQL 18 (native
  Windows, not Docker or WSL) and `accounts.User` extending `AbstractUser`
  (see [open-questions.md](open-questions.md), section 0).
- **Allowed scope:** structure, configuration, tooling, and the health endpoint.
- **Exclusions:** domain models beyond what tooling needs, workflow logic,
  worker, AI code, frontend, installer, Docker unless decided, JWT, custom user
  fields, external identity integration, any second application database.
- **Tests and acceptance:** the test suite runs and passes from a clean checkout
  following written instructions; a fresh virtual environment built from the
  recorded dependency pins reproduces the same package set; the health endpoint
  works; no secrets in the repository; module dependency rules are checked by an
  automated test or lint rule if feasible; the Python 3.14 compatibility of
  every dependency (including the PostgreSQL driver) is verified, or the
  documented 3.13 fallback is applied and recorded.
- **Stop-and-report gate:** report the chosen versions and tools, how to run
  tests, and any deviation from the plan.

### Phase 2 status

Updated after the owner's local validation (see the
[local validation record](../development/local-validation-record.md), which separates pasted
output from owner confirmations without logs). **The database-backed Phase 2 criteria are
supported by evidence for the tested environment; overall Phase 2 closure remains the owner's
decision at the stop-and-report gate, and the gaps below are still open.**

**Division of work.** The Claude Code development VM is used for code development and
database-free checks only; PostgreSQL is **not** installed there and the VM never connects to
a database on another machine. PostgreSQL 18.6 runs on the owner's **local** Windows computer
(CPython 3.14.10, project at `C:\Projects\newnew\automation-platform`), where the
migrations, the PostgreSQL-backed tests, the real readiness probe, and the application smoke
test were run. The VM did not run or observe them.

Done and verified on the VM (DB-free; PostgreSQL was not contacted):

- Backend skeleton: `backend/config` (split base/development/production
  settings), `accounts` (`accounts.User`, no added fields, standard `UserAdmin`,
  initial migration) and `health` (liveness and readiness).
- Exact versions and a hash-locked, runtime/development-separated dependency set
  ([runbook](../development/runbook.md)); a fresh environment built from the lock
  matched `.venv`, and an altered hash was rejected.
- The DB-free tests pass (the skeleton, the request-intake preparation slice, and the
  revision/approval policy and persistence preparation), including settings parsing, the
  production secret and host requirements, and tests that fail if any API view other than
  the two health views becomes public. `manage.py check`, compile checks, and a DB-free
  migration-state test pass.

Done and observed locally (pasted output from the owner):

- The PostgreSQL-required tests: `Ran 35 tests`, `OK`, exit code 0; the dedicated
  `test_automation_dev` database was destroyed afterwards.
- Liveness and readiness both returned status `ok` on port 8010 (readiness checks a real
  database connection).

Confirmed by the owner locally, **without pasted output**: `showmigrations`,
`makemigrations --check --dry-run`, and the local DB-free regression suite all completed
successfully. No local DB-free test count was supplied.

Gaps:

| Gap | Status |
| --- | --- |
| A real PostgreSQL connectivity check (readiness returning 200 against a real server) | **Closed** for the tested environment (pasted output) |
| `migrate` of the migrations against PostgreSQL | **Closed for the test database** (Django applied them to build it; pasted output). The development database's applied state is owner-confirmed via `showmigrations`, no log |
| The `requires_postgres` tests | **Closed:** 35 ran, OK (pasted output) |
| Compatibility of Django 5.2 with PostgreSQL 18 in practice | **Closed for the tested environment** (PostgreSQL 18.6) |
| `runserver` end to end and the application smoke test | **Closed for the smoke test that was run** (both health endpoints `ok` on port 8010); the admin-page and listening-address checks were not reported |
| Hash-locked dependency install on the local computer | **Closed by owner-supplied output** (`pip install --require-hashes --only-binary=:all: -r requirements/development.txt` succeeded, then `pip check` reported no broken requirements); not independently reproduced by Claude |
| Clean-checkout test run following the written instructions | **Partly closed:** DB-free suite passed locally (owner-confirmed, no count); a clean-checkout reproduction has **not** been reported |
| PyCharm project and interpreter | Optional developer tooling; **not a backend or database acceptance criterion** and not a blocker; not configured or confirmed |
| PostgreSQL `listen_addresses='*'` reported on the local server | **Open deployment-hardening item.** It conflicts with the runbook's loopback-only rule; the database must not be described as loopback-only. See [open-questions.md](open-questions.md), item 1.5 |
| CI definition (optional in the plan) | Not requested |

Later phases are not marked complete by this validation.

## Phase 3: Data, documents, and basic authorization

- **Goal:** persistent core entities, safe document storage, and role-based
  access.
- **Deliverables:** models and migrations for the entities in
  [data-model.md](data-model.md) needed so far (`CompanyProfile`,
  `ConfigurationVersion`, `User` and roles, `Case`, `Document`,
  `WorkflowDefinition`, `WorkflowRun`, `ResultRevision`, `Approval`,
  `AuditEvent`, `ModelProfile`; queue entities may wait for Phase 4); managed
  document storage service with hashing and atomic writes; upload with content
  type detection and size limits; authentication per the chosen approach; role
  checks; audit recording; configuration validation and versioning.
- **Dependencies:** Phase 2 accepted, including its PostgreSQL-backed checks
  (a provisioned PostgreSQL 18 on the owner's local computer is required before
  this phase's migrations can be tested); the duplicate-upload policy, upload size limit,
  role model details, and password/account-recovery handling decided
  (open questions 2.1 to 2.4). Authentication approach, input format (plain
  text), and public identifiers (UUID) are already decided.
- **Allowed scope:** backend only; API endpoints needed to exercise the above.
- **Exclusions:** background execution, AI code, export, frontend, real customer
  data.
- **Tests and acceptance:** uniqueness constraints enforced; stored files match
  recorded hashes; files are stored outside the source tree; revisions and audit
  events cannot be modified through the application; unauthorized users are
  refused for each role-restricted action; audit events contain no document or
  extracted content; migrations apply cleanly on an empty database.
- **Stop-and-report gate:** report the schema, authorization matrix, and known
  gaps.

## Phase 4: Persistent worker

- **Goal:** a separate worker process that executes tasks reliably from a
  PostgreSQL-backed queue. The queue's correctness is **unproven** until this
  phase's tests pass ([background-execution.md](background-execution.md),
  section 15); this phase is where it is proven or the design is revised.
- **Deliverables:** the library-versus-custom decision recorded; `Task` and
  `TaskAttempt` entities; atomic claim, lease, heartbeat, fencing, bounded
  retries with backoff, cancellation, concurrency limits, idempotent enqueue;
  worker entry point using the shared domain layer; graceful shutdown; a trivial
  test task kind used only for proving behavior.
- **Dependencies:** Phase 3.
- **Allowed scope:** worker, queue, and their tests.
- **Exclusions:** real extraction logic, AI adapters, installer, frontend. The
  design must not hold transactions open during task work.
- **Tests and acceptance:** two concurrent workers never execute the same task
  simultaneously; a worker killed mid-task is recovered after lease expiry
  without duplicate results; a stale worker's late commit is rejected; retries
  stop at the limit with backoff; cancellation behaves as specified in
  [background-execution.md](background-execution.md); concurrency limits are
  honored; the worker contains no business rules absent from the domain layer.
- **Stop-and-report gate:** report measured behavior and decisions on lease,
  retry, and limit values, and any reliability concern.

## Checkpoint: Installation feasibility experiment (after Phase 4)

- **Goal:** learn early whether database, API, and worker can be installed and
  run as services on a clean, ordinary Windows machine, before investing in UI
  and polish.
- **Deliverables:** a written findings report: what had to be installed,
  administrator rights needed, how services were started, behavior across logoff,
  restart, and sleep, antivirus or policy interference, and open problems. Any
  scripts are experimental and clearly labeled as such.
- **Dependencies:** Phase 4; supported Windows environment decided at least
  provisionally.
- **Allowed scope:** a throwaway or clearly experimental setup on a clean test
  machine without real customer data.
- **Exclusions:** a production installer, signing, update mechanism, UI work, real
  customer environments without explicit agreement.
- **Tests and acceptance:** the experiment is repeatable from written steps on a
  clean machine; findings are recorded honestly, including failures.
- **Stop-and-report gate:** report findings and recommend whether to continue,
  change deployment assumptions, or revise the architecture (for example the
  worker or database choice) before Phase 5.

## Phase 5: End-to-end workflow with deterministic test adapter

> **Status: not started.** A small database-independent *preparation slice*
> already exists outside this phase: the request result schema, pure validation
> rules, a deterministic test extractor, and canonical serialization with SHA-256
> content hashing (an internal component, not an export), as plain Python with DB-free tests
> ([request-intake-slice.md](../development/request-intake-slice.md)). A second
> preparation slice holds the **pure rules** for revision identity, approval bound to a
> revision and content hash, and export eligibility
> ([revision-approval-policy.md](../development/revision-approval-policy.md)). A third,
> persistence slice (`apps/cases`) stores cases, runs, result revisions, and approvals with
> database-unique revision numbers per run, a current-revision pointer, and transactional
> services; it is early Phase 3 work done ahead of order. Its PostgreSQL tests passed in the
> owner's local run (PostgreSQL 18.6, 35 PostgreSQL-required tests) for the tested
> scenarios only; see the
> [local validation record](../development/local-validation-record.md). None of these
> completes or starts Phase 5: there is no authentication or authorization, no export or
> race-safe export re-check, no state machine, no tasks or worker, no storage, no audit
> events, no API, and no UI. Immutability of revisions and approvals is ORM and service level,
> not database tamper protection. Some specification questions they surfaced are open
> (open-questions.md items 2.6 to 2.19).

- **Goal:** the full first vertical slice working end to end without any real AI.
- **Deliverables:** the request-document workflow package (schema, validation
  rules, versioned definition); the deterministic test adapter behind the AI
  gateway interface; extraction task; validation; revision editing; approval
  bound to one immutable revision; an earlier approval no longer applying to a
  new revision after an edit (history preserved); export of the approved
  revision with an execution-time re-check;
  failure, retry, and cancellation paths; API endpoints for all of these.
- **Dependencies:** Phase 4 and the checkpoint report. Input format (plain
  text) and export format (JSON) are already decided.
- **Allowed scope:** the workflow and the test adapter, as specified in
  [first-workflow.md](first-workflow.md).
- **Exclusions:** real AI adapters, network calls to any provider, automatic
  email sending, ERP writes, payment integration, pricing or inventory, PDF
  parsing, CSV export, OCR, frontend.
- **Tests and acceptance:** acceptance criteria AC-1 to AC-22 in
  [first-workflow.md](first-workflow.md) pass in automated tests; every state
  transition is covered; no external network calls occur.
- **Stop-and-report gate:** report test results, deviations from the
  specification, and spec changes required.

## Phase 6: Real AI adapters and policy enforcement

- **Goal:** replace the test adapter with real local and, if approved, external
  adapters under enforced data policy.
- **Deliverables:** adapters for the selected local runtime and, if decided,
  company-endpoint and external providers; policy-mode enforcement in the
  gateway; capability checks; structured-output validation; credential handling
  outside source control; an evaluation set and procedure for the extraction
  workflow; provider failure behavior.
- **Dependencies:** Phase 5; AI runtime and external provider decisions; policy
  modes reviewed; evaluation data without unauthorized customer information.
- **Allowed scope:** gateway, adapters, policy, evaluation tooling.
- **Exclusions:** tool use by models, silent fallback, shipping model weights in
  the repository, expanding the workflow scope, committing real documents or
  credentials.
- **Tests and acceptance:** tests prove `local_only` blocks every non-local
  profile, including embeddings; no cross-locality fallback occurs on provider
  failure; invalid model output becomes an attempt failure; prompt-injection
  samples in test documents do not change behavior beyond the validated output;
  a model change cannot become active without a recorded evaluation; no
  credentials appear in logs, the database, or the frontend payloads.
- **Stop-and-report gate:** report evaluation results (actual, not assumed),
  policy test results, and known limitations.

## Phase 7: Minimal frontend

- **Goal:** a minimal React/TypeScript UI for the first workflow.
- **Deliverables:** screens for upload, case and run status, draft review and
  editing, findings display, approval and rejection, export download, and
  failure/cancel/retry actions; role-appropriate visibility.
- **Dependencies:** Phase 5 API stable; Phase 6 may be partial but policy
  information should be displayable; frontend tooling decided.
- **Allowed scope:** UI for the specified workflow only.
- **Exclusions:** business rules in the frontend, new workflows, design
  system expansion, direct database or provider access, offline sync.
- **Tests and acceptance:** the full slice can be completed through the UI by a
  user with the right roles; unauthorized actions are not offered and are
  refused by the API; the UI shows that approval is tied to a revision and
  becomes invalid after an edit; component and end-to-end tests cover the main
  path and failure paths.
- **Stop-and-report gate:** report usability findings from a review session and
  remaining gaps.

## Phase 8: Installer, backup, restore, and controlled updates

- **Goal:** supportable installation and operation on Windows.
- **Deliverables:** installer and service packaging chosen from the findings of
  the feasibility experiment; database provisioning by installation tooling;
  least-privilege database roles; documented backup and restore procedures
  covering database and storage together with hash verification; an update
  procedure with backup, controlled migration, and rollback by restore;
  operational documentation.
- **Dependencies:** feasibility experiment; Phases 5 to 7 sufficiently stable;
  packaging, backup destination, and update delivery decisions.
- **Allowed scope:** installation, backup, restore, update tooling and docs.
- **Exclusions:** new product features, hub-mode implementation (design only
  unless decided), a mandatory control portal.
- **Tests and acceptance:** clean install, restart, and uninstall on a clean
  machine; a backup restored on another clean machine reproduces the data with
  matching hashes; an update from the previous version succeeds and a failed
  update can be rolled back by restore; services run under a non-administrative
  account where policy allows; the database is reachable only locally in
  standalone mode.
- **Stop-and-report gate:** report test evidence and unresolved environment
  limitations.

## Phase 9: Pilot and repeatable company configuration

- **Goal:** run a controlled pilot with one company and make company setup
  repeatable without code changes.
- **Deliverables:** a validated, documented company configuration procedure;
  pilot success criteria agreed in advance; a feedback and defect process;
  support and licensing boundaries written down; a review of whether the
  product-core and workflow-package split held without a customer fork.
- **Dependencies:** Phases 1 to 8; pilot decisions in
  [open-questions.md](open-questions.md); explicit agreement with the pilot
  company about data handling.
- **Allowed scope:** configuration tooling, pilot support, fixes to the shared
  core and packages.
- **Exclusions:** customer-specific forks or branches, uncontrolled
  customization, new workflows not planned in the pilot agreement.
- **Tests and acceptance:** a second configuration can be applied without code
  changes; pilot success criteria are evaluated against recorded results; any
  deviation that required code changes for one company is recorded as a product
  issue.
- **Stop-and-report gate:** report pilot outcomes, defects, and a recommendation
  on the next workflow package.
