# Implementation Plan

> **Status: Product implementation has not started.** This plan sequences the
> work. It contains **no delivery dates** and no effort estimates; those require
> decisions and measurements that do not exist yet. Phases 0 and 1 produced
> documentation only. Phase 2 has produced a backend skeleton (see "Phase 2
> status") and early persistence for revisions and approvals. A first Phase 4 slice (a generic
> queue and test handler) is implemented; its PostgreSQL tests and a manual smoke test passed in the
> owner's local run, for the tested scenarios only. AI-1a and AI-1b (both merged) add a standalone AI gateway foundation (contracts,
> policy enforcement, a registry and a deterministic test provider) and a KPMG Workbench provider adapter (tested with a mocked transport; the owner reports one local GPT-5 smoke test).
> AI-2a (on `feat/ai-request-intake-extraction`, implementation `e2fac8a`; the owner's local PostgreSQL run passed, PR not yet merged) lets Request Intake extract through the gateway **only when an operator explicitly enables `ai` mode**; the default is the
> deterministic fixture parser. It is tested with DB-free tests and a mocked transport in the VM, and with 35 PostgreSQL tests plus a 736-test regression in the owner's local run (owner-supplied, tested scenarios only); it has not been run end to end against the Workbench. No installer code exists, and no AI feature is reachable unless the worker is started in `ai` mode.

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
| 3 | Data, documents, and basic authorization | **Partly started, ahead of order** (review-services slice: `User.public_id`, roles as groups, authorization inside the services, audit events; DB-free tests pass; its 135 PostgreSQL tests, the user migration (1 existing user) and a manual smoke test passed in the owner's local run, owner-supplied and for the tested scenarios only; see [review-services-slice.md](../development/review-services-slice.md)). A REST API slice adds session login and the request endpoints (DB-free tests pass; 96 of the first 98 PostgreSQL tests passed in the owner's local run and 2 failed on test defects that are now corrected, rerun pending; see [rest-api-slice.md](../development/rest-api-slice.md)). Documents, storage, password reset, and a UI are not started |
| 4 | Persistent worker | **First slice implemented; phase not complete** (generic queue, `runworker`, one test handler; DB-free tests pass; the 95 PostgreSQL tests, a manual smoke test, and 15 process-level recovery tests (real worker processes, a forced kill) passed in the owner's local run for the tested scenarios only; see [persistent-worker-slice.md](../development/persistent-worker-slice.md) and the [local validation record](../development/local-validation-record.md)) |
| (checkpoint) | Installation feasibility experiment | Not started |
| 5 | End-to-end workflow with deterministic test adapter | **Not complete; its first orchestration slice is written** (`apps/intake`: submit, queue, extract, validate, canonicalize, record, `needs_review`, failure reconciliation; DB-free tests pass; its 76 PostgreSQL tests and a manual smoke test passed in the owner's local run, tested scenarios only; see [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md)). A review-services slice adds edit, approve and reject services with audit events (its PostgreSQL tests and a smoke test passed in the owner's local run, tested scenarios only; see [review-services-slice.md](../development/review-services-slice.md)). A REST API slice exposes submit, review, approval, rejection and audit (PostgreSQL tests written, **not run**; see [rest-api-slice.md](../development/rest-api-slice.md)). Export, the React UI, and real AI are not built |
| 6 | Real AI adapters and policy enforcement | **Foundation AI-1a merged (PR #19); first real adapter AI-1b (KPMG Workbench) merged (PR #20; mocked transport in the VM; the owner reports one local GPT-5 smoke test); AI-2a (Request Intake extraction through the gateway, explicit opt-in) written, awaiting review**; AI-1a: the provider-neutral gateway contracts, data-locality policy enforcement, a static provider registry and a deterministic test provider, DB-free tests only. No credential store, per-company policy setting, evaluation set or production-readiness claim exists; see [ai-gateway.md](ai-gateway.md) |
| 7 | Minimal frontend | **Partly implemented; phase not complete** (`frontend/`, UI-1 and UI-2: sign-in, request list and submission, request detail with processing status, draft editing, approval with warning acknowledgement, rejection, per-request audit history; one UI-2 workflow (submit, extract, edit, approve by a second user, audit) was run by the owner in a real browser against the local Django, PostgreSQL and worker, as reported with screenshots described to Claude, not stored; development-validated for that workflow only; see [frontend-slice.md](../development/frontend-slice.md) and the [local validation record](../development/local-validation-record.md)) |
| 8 | Installer, backup, restore, and controlled updates | Not started |
| 9 | Pilot and repeatable company configuration | Not started |

### Module architecture track (MOD-1 to MOD-4)

A parallel track that prepares independently versioned business modules, connectors and AI adapters on the shared Core. It does not
replace a phase; it changes how Phases 5, 6 and later deliverables will be packaged. Design: [module-contract.md](module-contract.md).

| Slice | Scope | Status |
| --- | --- | --- |
| MOD-1 | Architecture audit and module contract design | Done as a read-only audit (recorded in this document and module-contract.md) |
| MOD-1a | Manifest v1, SemVer subset, static allowlisted registry, architecture tests | **Implemented in the development VM** (`backend/apps/modules`, `backend/config/modules.py`; DB-free tests only). The built-in tuple is **empty**; nothing activates, no model, no migration, no endpoint. Request Intake is unchanged and not yet a module. |
| MOD-1b | Capability contracts, permission boundaries, module state table | **Split into slices S1a to S6** (design audit done). **S1a merged into `main` (PR #11):** pure contracts only (permission scopes `global`/`own`/`any`, default role grants for the existing Operator/Approver/Auditor, the `permissions.check` Core capability, maintenance hook declarations, read-only direct dependency inspection, a pure role-permission derivation proven equal to `apps/accounts/roles.py` by a golden test with a synthetic Request Intake manifest, a pure namespace-continuity check; DB-free tests only). **No runtime enforcement:** `setup_roles`, `/api/auth/me/`, the worker, audit and every service are unchanged and nothing calls the new code. **S1b merged into `main` (PR #12; commit `962d6ab`):** a behaviour-preserving refactor that moves the generic own/any/superuser algorithm from `apps/intake/access.py` into Core (`apps/accounts/access.py`); Request Intake keeps only its action table and delegates; proven in the VM by a 36,864-row truth table recorded before the change plus glue equivalence tests (DB-free), and **owner-supplied local PostgreSQL validation (2026-11-17): 242 authorization, review and API integration tests passed, 55.659 s, exit code 0, test database destroyed; the VM did not run or observe them and no output is stored**; no permission, role, group, migration, endpoint or outcome changed (see [module-contract.md](module-contract.md) section 3.8 and [local-validation-record.md](../development/local-validation-record.md)). Still absent: `ModuleState`, runtime module permissions, enable/disable and the capability facade; no production-readiness claim. **S2 merged into `main` (PR #13; commit `28483a6`):** generic, optional worker seams in `apps/tasks` only: an allowed-kind filter inside the atomic claim query (`None` = unrestricted as before, empty = claim nothing, non-empty = `kind IN` in the same `FOR UPDATE SKIP LOCKED` statement; filtered-out and unknown-kind tasks stay queued untouched), ordered named maintenance hooks after the legacy `after_iteration` hook (failure isolated, database errors follow the existing connection recovery, duplicate ids refused), and a `kind_gate` seam that fails closed. **Nothing installs a gate or a new hook; the production composition, `runworker` and every existing behaviour are unchanged, and no module can be disabled.** DB-free tests pass in the VM (the focused suite also survived 22 deliberate mutations). **Owner-supplied local PostgreSQL validation: the new filtered-claim and gated-worker tests (39) together with the unchanged queue suite (95) and the real-process recovery tests (15) passed, 149 tests, 66.399 s, exit code 0, test database destroyed; the VM did not run or observe them and no output is stored.** This supports filtered claiming and unchanged lease, retry, attempt-numbering, fencing and recovery behaviour in the tested scenarios only; the gate race window remains, `ModuleState` and run-time enable/disable do not exist, no concurrency guarantee beyond the tested scenarios is claimed, and there is no production-readiness claim (see [module-contract.md](module-contract.md) section 3.9 and the [local validation record](../development/local-validation-record.md)). **S3 merged into `main` (PR #14; commits `ba4060a` and `5398a1b`):** modules can declare audit actions that the one recorder accepts. An immutable, deterministic `AuditVocabulary` (Core's seven actions, detail keys and subject types unchanged, plus validated module declarations; Core namespaces and reserved names refused; installed once per process, idempotent, a conflicting reinstall refused) is composed by `config/audit_vocabulary.py`; audit migration `0002` replaces the fixed action list with a bounded, anchored **shape** CHECK and adds the `module` subject type, touching no existing row. **The database checks syntax only; the Python vocabulary is the allowlist; raw SQL can still store any well-formed action; nothing here is tamper protection.** Reversing the migration is refused once a module action or `module` subject exists (events are never deleted). `BUILTIN_MODULES` is still empty and nothing installs a module vocabulary in the application. DB-free tests pass in the VM (102 new; each of 32 deliberate mutations of the implementation was caught by the focused set). **Owner-supplied local PostgreSQL validation (the VM did not run or observe it; no output is stored): migration `0002` completed, the 15 existing audit events remained, the 24 new PostgreSQL tests passed, and the selected regression passed, 342 tests, 80.063 s, exit code 0, test database destroyed** (an earlier run had one failure, a stale constraint-name expectation in `SchemaTests`, corrected in `5398a1b`). This supports the new action-shape and subject-type constraints, the forward and reverse migration (reverse refused, nothing deleted, when new-vocabulary events exist) and unchanged Core audit behaviour in the tested scenarios only; the database still does not enforce the allowlist, and the audit table is not tamper-protected. No `ModuleState`, enable/disable or production-readiness claim (see [module-contract.md](module-contract.md) section 3.10 and the [local validation record](../development/local-validation-record.md)). **S4 implemented and validated on `feat/module-runtime-state` (implementation `96efa2f`, test-only corrections `45bd0da`, `4ba709b`, `0f8e44d`; pushed, PR not yet merged):** a new Django app `apps/module_host` with `ModuleState` (UUID id, unique `module_id`, `installed_version`, `enabled`, nullable `configuration_version`, `registry_fingerprint`, timestamps), an append-only `ModuleNamespaceClaim` (unique namespace) and an append-only one-row `ModuleHostMarker` (written by the first completed sync, in its transaction) in one additive migration (`0001`: three tables, no existing table or migration touched); a repeatable `module_sync` (reads only the static registry, refuses namespace ownership changes through the S1a continuity check, creates missing state, never enables or disables an existing module, never changes a stored version, reports version mismatches); transactional, idempotent, locked, audited `module_enable` / `module_disable` (refused for unknown modules, version mismatches, disabled dependencies, and while an enabled module depends on the module; tasks and data are never touched); `module_status` with stable JSON; and the S2 `kind_gate` **installed** in the production composition, built from the registry and the stored state (disabled, version-mismatched, stateless-after-sync and unknown kinds are held, never failed; running attempts finish; recovery and retry unchanged). **Request Intake is not a module and its `request_intake.extract` kind is a *legacy ungated* kind, so it works exactly as before; MOD-2 must remove it from that set when it registers Request Intake (a registered module claiming a legacy kind is refused at import).** Before the first sync completes (the durable marker, not the presence of rows) an absent state row means "enabled" (backward compatibility); after it, missing state fails closed, even if no row is left. `module.enabled` / `module.disabled` are two new **Core** audit actions (the `module` namespace is reserved for Core), recorded as the system actor in the change's own transaction. `BUILTIN_MODULES` is still empty, so nothing is gated in practice; no public API, no installer, no update path. DB-free tests pass in the VM (1926 in all, 198 of them in the module host files; each of 25 deliberate mutations was caught). **Owner-supplied local PostgreSQL validation (the VM did not run or observe it; no output is stored): the 75 module host tests, the 15 process-level recovery tests and the 25 audit vocabulary tests passed, `module_host.0001_initial` applied successfully, and the full selected PostgreSQL regression passed, 567 tests, 153.186 s, exit code 0, test database destroyed.** Three test-only corrections preceded that run (a `varchar(40)` overflow is a `DataError`, the recovery children must run the production gate with their test kinds allowed, and the audit migration fixtures must use the frozen original actions); no production code, model or migration changed. This supports the tested scenarios on one machine only: the S2 gate race window remains, Request Intake is not registered as a module (its task kind is a legacy ungated kind, verified end to end in the tested scenarios by the module host regression tests), there is no API-side write blocking for a disabled module, no capability facade or `ModuleContext`, no module installation or Update Manager, and no production-readiness claim (see [module-contract.md](module-contract.md) section 3.11, the [runbook](../development/runbook.md) section 5.1 and the [local validation record](../development/local-validation-record.md)). **S5 (workflow attribution) is merged into `main` (PR #16; implementation commit `e2a4c55`; owner-supplied local PostgreSQL validation, not observed by the VM: the migrations applied, four historical runs were attributed to `request_intake` / `request_intake` / 1, the 37 attribution tests passed and the selected regression passed, 604 tests, 207.855 s, exit code 0, test database destroyed; tested scenarios only, no production-readiness claim):** `WorkflowRun` gains `module_id`, `workflow_key` and `workflow_version` (the workflow definition version, mirroring the manifest's `definition_version`), set once by `create_run` (a required keyword) and refused afterwards at the ORM level; additive migrations `cases.0006` to `0009` (nullable columns, an idempotent evidence-based backfill of Request Intake runs, a validation that **stops with a count at any run without a Request Intake source**, then NOT NULL and CHECK constraints); Request Intake's identity is `request_intake` / `request_intake` / 1, equal to the synthetic manifest; no state, task, lease, approval, audit or canonical-hash change, no REST exposure, no module activation, 1998 DB-free tests in the VM (see [module-contract.md](module-contract.md) section 3.12 and the [local validation record](../development/local-validation-record.md)). **S6 (ModuleContext capability facade) is implemented on `feat/module-capability-facade` (implementation `ee4198d`, test-only fix `5b61298`; pushed, PR not yet opened; owner-supplied local PostgreSQL validation, not observed by the VM: the 72 focused PostgreSQL tests passed and the selected regression passed, 676 tests, 191.518 s, exit code 0, test database destroyed; tested scenarios only, no production-readiness claim):** `apps/module_host/context.py` (with the pure `context_rules.py`, `config/module_context.py` and `ModuleContextError`) gives a registered module an immutable, registry-backed context whose facades `task.enqueue`, `case.run`, `audit.record` and `permissions.check` delegate to the existing `enqueue_task`, `create_case` / `create_run` / `transition_run`, `record_event` and the generic Core access rule, only for capabilities the manifest declares, only for the module's own task kinds, workflows (stamped with an explicit attribution at the declared definition version), audit actions and permissions, and, for state-changing calls, only while the module is enabled, version-matched and present in `ModuleState` (checked under a shared row lock, so a disable waits for an in-flight call); actors are explicit and re-read, never ambient; `revision.approval` has no facade; the queue, worker, run state machine, audit recorder and Request Intake are unchanged and Request Intake still does not use a context; 2129 DB-free tests in the VM; existing Request Intake behaviour was preserved in the tested scenarios; a naming and authority boundary, not a sandbox (see [module-contract.md](module-contract.md) section 3.13 and the [local validation record](../development/local-validation-record.md)). **Not started after S6:** real AI integration. (MOD-2, Request Intake as a registered module, is implemented in its own row; routing Request Intake's own code through the facades was not part of it.) The contract is shaped for a future `ModuleState` and Update Manager (namespace continuity, direct dependents, fingerprint); see [module-contract.md](module-contract.md) sections 3.4 to 3.7, 5.5, 5.6 and 8. |
| MOD-2 | Request Intake as the first registered built-in module | **Implemented on `feat/request-intake-module` (implementation `871bf2d`, test-only corrections `8915925` and `05cf1a7`; pushed, PR not opened); owner-supplied local PostgreSQL validation passed (not observed by the VM; no output stored): 30 focused tests, 4 namespace continuity tests, a 701-test selected regression (220.591 s, exit code 0, test database destroyed), and `module_sync` on the development database after which `module_status` reported `request_intake` enabled, 1.0.0, claimable; tested scenarios only.** `apps/intake/module.py` declares the manifest (module `request_intake` 1.0.0, workflow `request_intake` v1, task kind `request_intake.extract`, the six `intake.*` permissions and the Operator, Approver and Auditor grants, the five required Core capabilities, the `requests` API prefix and the existing screens; no audit action, because every action it emits is Core's; no handler object, so the file imports no model; no maintenance hook, the reconciler stays the `after_iteration` hook); `config/modules.py` registers it statically; `config/task_registry.py` removes `request_intake.extract` from the legacy ungated set (only `test.add_numbers` remains) and verifies at import that the handler allowlist is exactly the legacy kinds plus the registry's task kinds. The worker gates its kind on the stored module state: held (never failed) while disabled, version-mismatched or stateless after initialization, resumed in order on re-enable, a running attempt finishes, recovery and retry unchanged. **`module_sync` must be run (explicitly; nothing runs it) before the worker claims Request Intake tasks on an installation that has already synced.** Roles, `/api/auth/me/`, audit vocabulary, REST routes and React routes are unchanged and proven equal to the manifest by DB-free tests (2172 in the VM) and preserved in the owner's tested PostgreSQL scenarios; no migration; API submissions are not blocked while the module is disabled; the S2 gate race window remains; Request Intake does not use `ModuleContext`; not a sandbox; no real AI, installer or Update Manager; no production-readiness claim (see [module-contract.md](module-contract.md) section 3.14 and the [runbook](../development/runbook.md) section 5.4). |
| MOD-3 | A second independent business module | Not started |
| MOD-4 | Signed installable package lifecycle and update manager | Not started; blocked on a trust-model decision |

### AI Gateway track (AI-1 and later)

A parallel track that builds the provider-neutral AI gateway of Phase 6 in small slices, before any real provider. Design:
[ai-gateway.md](ai-gateway.md); policy: [ai-policy.md](ai-policy.md).

| Slice | Scope | Status |
| --- | --- | --- |
| AI-1a | Gateway foundation and provider contracts | **Implemented and merged into `main` (PR #19, implementation `7df5c68`); DB-free tests only (166 new tests; the full DB-free suite, 2338 tests, passed in the development VM; see [ai-gateway.md](ai-gateway.md) section 13).** `backend/apps/ai_gateway` is a pure standard-library package, not a Django app: immutable `ModelProfile`, `ProviderDescriptor` and `ProviderCapabilities`, `GenerationRequest` and `GenerationResponse`; three policy modes (`local_only`, `company_endpoint`, `approved_cloud`) enforced before any provider code runs on every call, with no fallback; a static, immutable, duplicate-rejecting `ProviderRegistry`; a closed JSON Schema subset with strict output validation; stable error codes with fixed messages; a deterministic **test-only** provider that the production composition never lists. `config/ai_gateway.py` is the composition root and both its tuples are empty. **Nothing calls it**: Request Intake extraction, the worker, modules (`ai.invoke` is still a future capability), the API, settings, the database and the frontend are unchanged. Provider locality is the provider's own declaration, timeouts are cooperative, cloud approval is a call parameter, and there is no real provider, credential handling or production-readiness claim. |
| AI-1b | KPMG Workbench provider adapter | **Merged into `main` (PR #20, implementation `b3cf551`); the owner has since reported one local GPT-5 smoke test (owner-supplied, not observed by the VM; [ai-gateway.md](ai-gateway.md) section 14.10(d)). As written:** aligned with the client-side HTTP contract reported from the FI integration audit (GP-0299-FI-APP); DB-free tests only (147 new tests; the full DB-free suite, 2485 tests, passed in the development VM; see [ai-gateway.md](ai-gateway.md) section 14). The provider `kpmg_workbench` (locality `company_endpoint`; profiles `workbench-gpt5`, preferred, and `workbench-gpt41`, manual) behind a mockable `HttpTransport` seam in the pure package; a separate standard-library HTTPS transport (`config/ai_http_transport.py`: verified TLS with no off switch, explicit proxy only, separate connect and read timeouts, no redirects); an opt-in composition `config/ai_workbench.py` that reads `AP_AI_WORKBENCH_*` from the environment when called and fails closed without a key. The request is the minimal verified one: `POST .../deployments/{deployment}/chat/completions?api-version=2024-10-21`, header `Ocp-Apim-Subscription-Key: <raw key>`, header `x-kpmg-charge-code`, body `messages` plus `response_format` `json_object`, **no `model`, no temperature or other sampling parameter and no token-limit parameter**; the answer is read from `choices[0].message.content`. Only the key is required configuration; the endpoint host is allowlisted so the key cannot be sent elsewhere; retries are at most 2, same-deployment, inside one 90-second deadline; no fallback to another deployment, model or provider. **Never run against the Workbench: the contract is client-side evidence, not proof of live compatibility** (ai-gateway.md section 14.10 lists what still needs live verification). The Workbench is **not** in the production composition (`config/ai_gateway.py` is still empty), nothing calls it, Request Intake extraction is unchanged, and there is no per-company policy setting, credential store or production-readiness claim. |
| AI-2a | Real AI extraction for Request Intake (explicit opt-in) | **Implemented on `feat/ai-request-intake-extraction` (implementation `e2fac8a`; pushed, PR not yet merged); 148 new DB-free tests (2636 in all) pass in the development VM; the owner ran the 35 focused PostgreSQL tests and a 736-test selected regression locally, all passed (736 tests, 238.962 s, exit code 0, test database destroyed; owner-supplied, not run or observed by the VM, no output stored; tested scenarios only, with a scripted provider; existing review, approval, audit and worker behaviour preserved in them); nothing sent to the Workbench by the VM or by the tests.** Two modes, `deterministic` (default; unchanged) and `ai` (`AP_INTAKE_EXTRACTION_MODE`), no automatic fallback; `ai` needs an explicit `AP_AI_POLICY_MODE` (no default) and the Workbench key, and the worker refuses to start otherwise or when its lease cannot cover the call deadline. `apps/intake/ai_extraction.py` calls the gateway with the `workbench-gpt5` profile; the answer is a candidate that goes through the existing validation, canonicalization, hashing, revision, review and approval unchanged (no AI field can approve; a human approves). Failures map onto the queue's existing codes (retryable provider errors retried within the attempt limit; policy, configuration, invalid or refused output permanent; never a silent fallback). No frontend, upload, connector, second provider, migration, model, endpoint, audit action or failure code. **Never run end to end against the Workbench with Request Intake's prompt; no evaluation or accuracy measurement; no manual retry of a failed run.** See [ai-gateway.md](ai-gateway.md) section 15 and the [runbook](../development/runbook.md) section 5.6. |
| AI-2b and later | Credential store, policy storage, module facade (`ai.invoke`), evaluation set, documents and connectors | Not started; scope to be set by the owner |

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
- **Status of the first slice (not the whole phase):** a generic queue and one deterministic test
  handler are implemented ([persistent-worker-slice.md](../development/persistent-worker-slice.md)).
  Its 95 PostgreSQL tests and a manual smoke test passed in the owner's local run, for the tested
  scenarios only ([record](../development/local-validation-record.md)). A test-only follow-up added
  15 process-level recovery tests (two real worker processes, a forcibly killed worker recovered after
  its lease, shutdown with a task in progress, races against recovery); they passed in the owner's
  local run, again for the tested scenarios only. Still open for this phase: heartbeat, a supervisor
  or Windows service, concurrency limits, behavior across restart and sleep on Windows, measured
  lease and retry values, and the hardening items listed in the record. Not production ready.

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
> race-safe export re-check, no review or approval flow, no storage, no audit
> events, no API, and no UI. A later orchestration slice (`apps/intake`; see
> [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md)) adds the run
> state and connects a submitted text to the worker and the deterministic extractor up to `needs_review`; its
> PostgreSQL tests passed in the owner's local run (as reported; tested scenarios only). A review-services slice
> ([review-services-slice.md](../development/review-services-slice.md)) adds authorization inside edit, approve and
> reject services and audit events (its PostgreSQL tests passed in the owner's local run, tested scenarios
> only); the sentence above about "no
> authentication, no review or approval flow, no audit events" describes the earlier slices. It does not
> complete Phase 5. Immutability of revisions and approvals is ORM and service level,
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

> **Implementation status (2026-11-16).** Partly implemented ahead of Phases 5 and 6 being complete; see
> [frontend-slice.md](../development/frontend-slice.md) (UI-1, UI-2). Built: sign-in, request list and text submission, request
> detail with polling, draft editing, approval with warning acknowledgement, rejection with fixed reasons, per-request audit
> history, and stale-revision handling, every control following the server's `allowed_actions` with no business rule in React.
> The owner reports running one UI-2 workflow in a real browser against the real local stack (screenshots described to Claude,
> not stored; [record](../development/local-validation-record.md)): **development-validated for that workflow only, not
> production-ready.** Not built: document upload, export download, failure, cancel and retry actions, a source-text review
> endpoint, revision diffs, serving by Django, an installer, real AI. The stale-revision conflict and rejection were not shown in
> the reported browser run. The stop-and-report gate (a usability review session) has not been held.

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
