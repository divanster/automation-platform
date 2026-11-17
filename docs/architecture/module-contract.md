# Module contract (MOD-1a): manifests and the static registry

> **Status: implemented as a foundation; nothing uses it yet.** MOD-1a adds a pure-Python package, `backend/apps/modules`, and a
> composition root, `backend/config/modules.py`, whose built-in module tuple is **empty**. No module is registered, activated,
> enabled or run, no application code calls the registry, and Request Intake is **not** a module yet (that is MOD-2). It was
> written and tested in the development VM with DB-free tests only. The design in sections 8 and 9 (lifecycle, update manager)
> is intent, not code.
>
> **MOD-1b / S1a (pure capability and permission contracts)** extends the same package, still with no runtime: permission scopes,
> default role grants, the `permissions.check` Core capability, maintenance hook **declarations**, read-only direct dependency
> inspection, a pure derivation of the default role permission sets, and a pure namespace-continuity check (sections 3.4 to 3.7,
> 5.5, 5.6). **Nothing here is enforced or executed**: `setup_roles`, `/api/auth/me/`, the worker and every authorization service
> are unchanged, there is no `apps/module_host` yet, and Request Intake is still not a module. S1a is merged into `main`.
>
> **MOD-1b / S1b (generic authorization refactor)** moves the own / any / superuser algorithm out of `apps/intake/access.py` into Core
> (`apps/accounts/access.py`) with no change in any authorization outcome (section 3.8). Request Intake delegates to it. It is a
> refactor, not a contract change: no module uses Core's rule, no manifest is turned into rules, and the behaviour-preservation
> evidence is DB-free in the development VM, plus an owner-supplied local PostgreSQL run (242 tests, exit code 0; section 3.8 and
> [local-validation-record.md](../development/local-validation-record.md)). Still absent: `ModuleState`, runtime module permissions,
> enable/disable and a capability facade. No production-readiness claim. S1b is merged into `main` (PR #12).
>
> **MOD-1b / S2 (worker claim filter, ordered maintenance hooks, kind gate seam)** was implemented in commit `28483a6` and is merged into `main` (PR #13).
> It gives `apps/tasks` three optional, generic seams (section 3.9): `claim_next_task` accepts an allowed-kind
> filter that is part of the atomic claim query; the worker `Composition` accepts further named maintenance hooks that run after the legacy
> `after_iteration` hook; and it accepts a `kind_gate` callable that answers which kinds may be claimed, failing closed. **Nothing installs a
> gate or a new hook:** the production composition is unchanged, so the worker behaves exactly as before. `apps/tasks` still imports neither
> `apps.modules` nor `apps.intake`, and there is no module enable/disable, `ModuleState` or `apps/module_host` (S4). The DB-free tests pass in
> the development VM. The PostgreSQL tests (`tests/test_tasks_gating_postgres.py`, with the existing queue and process-level recovery suites)
> were run by the project owner locally: **149 passed, 66.399 s, exit code 0, test database destroyed** (owner-supplied; the VM did not run or
> observe them; section 3.9 and [local-validation-record.md](../development/local-validation-record.md)). Supported for the tested scenarios
> only; no production-readiness claim.
>
> **MOD-1b / S3 (extensible audit vocabulary)** was implemented in commits `ba4060a` and `5398a1b` and is merged into `main` (PR #14). A module can
> declare audit actions and Core's one recorder accepts them, without a migration per action (section 3.10): an immutable `AuditVocabulary`
> (Core's seven actions, their detail keys and subject types unchanged, plus validated module declarations) is checked in Python whenever an event
> is built, and audit migration `0002` replaces the fixed list of action names with a bounded, anchored **shape** check and adds the `module`
> subject type. **The database checks syntax only; the Python vocabulary is the allowlist.** Raw SQL can still store any well-formed action, and
> nothing here is tamper protection or a sandbox. `BUILTIN_MODULES` is still empty, nothing in the application installs a module vocabulary yet,
> and there is no `ModuleState`, enable/disable or S4. The DB-free tests pass in the development VM. The owner reports, from a local run that the VM
> did not observe (no output is stored): migration `0002` completed, the 15 audit events that existed remained after it, the 24 new PostgreSQL
> tests passed, the corrected schema-constraint test passed, and the selected PostgreSQL regression ran **342 tests, all passed, 80.063 s, exit
> code 0, dedicated test database destroyed** (section 3.10 and [local-validation-record.md](../development/local-validation-record.md)).
> Supported for the tested scenarios only. No production-readiness claim.
>
> S3 review (owner decisions): the runtime installation of a module vocabulary is deferred to MOD-2; `request` is now a reserved Core namespace in
> the manifest; and the manifest and the audit vocabulary apply the same detail-key restrictions (sections 3.2 and 3.10).
>
> **MOD-1b / S4 (persistent module state, enable/disable, worker gate)** is implemented on `feat/module-runtime-state` (**committed locally, not
> pushed; PostgreSQL tests not run**). A new Django app, `apps/module_host`, stores a `ModuleState` row per registered module and an append-only `ModuleNamespaceClaim` per
> namespace (additive migration `0001`), syncs them from the approved static registry (`module_sync`), enables and disables a module transactionally with
> an audit event (`module_enable`, `module_disable`), and reports status (`module_status`) (section 3.11). The existing S2 `kind_gate` is now installed in the
> production worker composition: it holds the queued tasks of a disabled, version-mismatched or stateless (after the first sync) module and lets
> **legacy kinds, Request Intake's `request_intake.extract` included, through unchanged**. **`BUILTIN_MODULES` is still EMPTY, Request Intake is not a
> module (that is MOD-2), no module is gated in practice, and there is no public enable/disable API.** The DB-free tests pass in the development VM
> (198 new tests; 1914 in all). The 73 new PostgreSQL tests were written and **NOT RUN** (the VM has no PostgreSQL); the owner has not run them or the migration.
> Two Core audit actions were added (`module.enabled`, `module.disabled`), which changes the S3 "seven Core actions" pin (section 3.11, "Audit").
> No production-readiness claim.

## 1. Purpose and scope

The product is one shared Core plus business modules, connectors and AI adapters that can be versioned independently
([ADR 0001](../decisions/0001-modular-monolith.md), [ADR 0005](../decisions/0005-company-configuration-not-forks.md),
[overview.md](overview.md) section 5, rule 3: "the core does not import workflow packages by name; packages register themselves
through a defined registration interface"). Before any module exists, MOD-1a fixes the **contract**: what a module declares
about itself, how declarations are validated, and how a fixed, reviewed set of them becomes an immutable registry.

**Built in MOD-1a:** manifest v1 (immutable, self-validating dataclasses), a documented SemVer subset with ranges, Core API
compatibility checks, dependency checks with cycle detection, capability declarations, a deterministic registry with a stable
fingerprint, and architecture tests that keep all of it isolated.

**Not built in MOD-1a** (later slices built some of it: the audit vocabulary in S3, section 3.10; the module state table, sync, enable/disable and the
worker gate in S4, section 3.11): loading or activating modules, permissions seeding, task-handler registration with the worker, API or UI mounting,
configuration storage, licensing, signatures, packages, an update manager, a marketplace (section 10).

## 2. Terms

| Term | Meaning |
| --- | --- |
| **Core** | The shared platform: `apps.accounts`, `apps.audit`, `apps.cases`, `apps.tasks`, the transport parts of `apps.api`, `config`, and now `apps.modules`. Core packages never import a module. |
| **Module** | A versioned unit described by a manifest. Four kinds (below). |
| **Manifest** | Declarative, immutable data describing one module. It is not a loader: nothing in it is imported or executed. |
| **Registry** | The immutable, ordered, validated set of manifests built from a static list. |
| **Composition root** | `config/modules.py`: the one place that may name built-in modules. |

| Module kind | What it is | May declare workflows |
| --- | --- | --- |
| `business` | Workflows, task handlers, permissions, UI, audit actions | yes |
| `connector` | Moves documents or records to or from an external system; no business rules | no |
| `ai_adapter` | Implements the provider-neutral AI gateway interface ([ADR 0004](../decisions/0004-provider-neutral-ai-gateway.md)); the gateway is not built | no |
| `service` | An external, process-isolated module reached over a typed local protocol (not built) | no |

## 3. Manifest v1

`apps.modules.manifest.ModuleManifest`. Every dataclass validates itself when constructed (`__post_init__`), so an invalid
manifest cannot exist; it is frozen, and every collection is copied into a tuple.

### 3.1 Fields

| Field | Required | Rule |
| --- | --- | --- |
| `manifest_version` | yes | Integer; only `1` is supported (`unsupported_manifest_version` otherwise). A different shape needs a new manifest version. |
| `module_id` | yes | `[a-z][a-z0-9_]{2,39}`; stable forever, never renamed or reused; not a reserved namespace |
| `module_kind` | yes | `business`, `connector`, `ai_adapter` or `service` |
| `module_version` | yes | `MAJOR.MINOR.PATCH` (section 4) |
| `core_api_compatibility` | yes | A version range of the Core API the module supports; **must have an upper bound** |
| `license_feature` | yes | Dotted identifier of at least two segments (for example `mod.request_intake`). Format only: nothing enforces licenses. Several modules may share one. |
| `display_name` | no | Printable text, at most 80 characters |
| `extra_namespaces` | no | Further identifier prefixes the module owns (section 3.2) |
| `dependencies` | no | `ModuleDependency(module_id, version_range)`: hard dependencies on other modules |
| `capabilities_required` / `capabilities_provided` | no | Dotted capability ids of at least two segments (section 3.3) |
| `workflows` | no | `WorkflowDeclaration(workflow_key, definition_version, task_kinds, description)`; `business` modules only; the task kinds must be declared by the same module |
| `task_handlers` | no | `TaskHandlerDeclaration(kind, handler=None, description)`; kind at most 64 characters (the `Task.kind` column) |
| `permissions` | no | `PermissionDeclaration(permission_id, scope, description)`; `<namespace>.<codename>`; scope `global`, `own` or `any` (section 3.5) |
| `role_grants` | no | `RoleGrant(role, permissions)`: default grants to `Operator`, `Approver` or `Auditor`, of permissions the module itself declares (section 3.6) |
| `maintenance_hooks` | no | `MaintenanceHookDeclaration(hook_id, while_disabled, description)`: declarations only, `hook_id` exactly `<namespace>.<name>` (section 3.7) |
| `audit_actions` | no | `AuditActionDeclaration(action, detail_keys)`; at most 32 characters (the `AuditEvent.action` column). Since S3 the audit vocabulary is composed from these (section 3.10), which also refuses a detail key whose name suggests free content |
| `configuration_schema` | no | `ConfigSchema(schema_version, fields)` of typed `ConfigField`s (`bool`, `int`, `string`, `enum`, `secret_ref`): no code, no expressions; a `secret_ref` names a secret held elsewhere and can have no default |
| `ui_contributions` | no | `UiContribution(contribution_id, kind, path, label, permission)`; `kind` is `route` or `navigation`; `permission` is a UI hint only |
| `api_contributions` | no | `ApiContribution(route_prefix)`: one path segment under `/api/`; `auth`, `health`, `modules` and `admin` are Core's |
| `update` | no | `UpdateMetadata(data_schema_version, minimum_updater_protocol)`, both optional positive integers (section 4.4) |

Collections are bounded (at most 100 entries each; 50 config fields) as a sanity limit, not a business rule.

### 3.2 Identifiers and namespaces

A module **owns** its `module_id` and any `extra_namespaces` as identifier prefixes. Every task kind, permission id, audit action,
workflow key, provided capability and UI contribution id must start with an owned namespace followed by a dot. Namespaces are
unique across modules, and a fixed set is reserved for Core and cannot be owned:

`core, test, platform, module, auth, admin, health, run, revision, approval, rejection, request, audit, task, case, document, documents, ai, secrets, permissions`

so one module cannot define, shadow or collide with another module's or Core's identifiers (the existing `test.add_numbers`
handler and the existing `run.state_changed` audit action stay Core's). `request` was added to the list in S3 by owner decision, because
`request.submitted` and `request.edited` are Core audit actions; a module's own ids such as `request_intake` are different namespaces and unaffected. `extra_namespaces` exists so a built-in module can keep
identifiers that predate the contract: Request Intake's permissions are `intake.*` and its task kind is
`request_intake.extract`, so MOD-2 will declare `extra_namespaces=("intake",)` rather than rename anything. Workflow keys,
capabilities and UI ids are namespaced the same way. API route prefixes are not namespaced (Request Intake serves `requests`)
and are protected by uniqueness instead.

**Owner decision: `extra_namespaces` is approved, under these rules** (each is enforced by code and tests):

1. **Explicit declaration.** A module owns its `module_id` and exactly the `extra_namespaces` it lists, nothing else. There is no
   default beyond the id, no wildcard and no "owns whatever it uses". The module id is always owned and cannot be listed again.
2. **Global uniqueness.** A namespace has one owner in the whole registry. The module id of one module is a namespace too, so a
   module cannot list another module's id as an extra namespace (`duplicate_namespace`, raised as soon as the real owner is
   registered). Core's reserved namespaces can never be claimed.
3. **Registry validation.** The registry checks the claim, and exposes the result read-only as `namespace_owners`
   (namespace -> owning module id), so ownership can be inspected.
4. **Immutable ownership.** Within a registry, ownership cannot change: manifests are frozen, `extra_namespaces` is a sorted tuple,
   and `namespace_owners` is a read-only mapping. **What this does not cover:** one registry cannot know about the next release. A
   namespace a module has used must never later pass to a different module id; keeping that promise across versions is a
   requirement on the future update manager (section 9), not something MOD-1a can enforce.
5. **No implicit inheritance.** Ownership matches the first dot-separated segment of an identifier **exactly**: owning `intake`
   does not grant `intake_extra`, `in` or `request`, and owning `demo_module` does not grant `demo`. A dependency grants nothing
   from the namespaces of the module it depends on, a dependent gets nothing from its dependency (not even through a chain of
   dependencies), and a dependent cannot claim its dependency's namespace as its own. Extra namespaces are part of the manifest and
   so of its fingerprint.

### 3.3 No executable paths

There is **no field** that holds a dotted import path, a file path, a command, a URL to code or an entry point (a test inspects the
dataclass fields and the package source for this). The only executable thing a manifest can carry is a task-handler **object**
that someone already imported and passed in, checked structurally (`kind` equal to the declared kind, callable `validate` and
`run`); text or bytes, including a string subclass dressed up with those attributes, is refused (`invalid_handler`). Handler
objects are excluded from the canonical form and the fingerprint (only whether one is present counts). In MOD-1a a handler is
metadata; registering handlers with the worker is MOD-2.

### 3.4 Capabilities

A capability is a named thing a module needs from Core or from another module. **Core provides today** (each backed by existing
code, or in the case of `permissions.check` the authorization services): `task.enqueue`, `case.run`, `audit.record`,
`revision.approval`, `permissions.check`. **Planned, not built** (requiring one is refused with
`capability_not_yet_available`): `documents.read`, `ai.invoke`, `secrets.resolve`. A module may provide capabilities in its own
namespaces.

**Owner decision: strict direct capability dependencies.** A module that requires a capability provided by another module must
declare that provider **itself, as a direct dependency** (`capability_provider_not_dependency` otherwise). The registry never
accepts a transitive dependency in its place, in any shape: depending on a module that depends on the provider, depending on
something the provider depends on, or depending on an unrelated module all fail, and the rule is checked for every required
capability, not just the first. It does not apply to capabilities Core provides. The reasons are that registration order stays well
defined and that a module cannot start to "work" because an unrelated dependency happens to pull a provider in; if that dependency
later changed, the failure would otherwise appear in the wrong place.

Capabilities are declarations used for validation and ordering; MOD-1a enforces nothing at run time.

**The five Core capabilities and what each will mean** (the contract names them; a later slice, S6, adds a thin facade that delegates
to the existing services unchanged; no capability can be called today):

| Capability | Meaning for a module | Existing service it will delegate to |
| --- | --- | --- |
| `task.enqueue` | Enqueue tasks of kinds the module declares, with idempotency keys in its own namespace | `apps.tasks.services.enqueue_task` |
| `case.run` | Create cases and runs for workflows the module declares, and move them through the Core run states | `apps.cases.services` (`create_case`, `create_run`, `transition_run`) |
| `audit.record` | Record events whose actions the module declares, inside the operation's transaction | `apps.audit.recorder.record_event` |
| `revision.approval` | Create revisions and record approvals and rejections through the Core policy | `apps.cases.services` and the pure `workflows.revision_approval` policy |
| `permissions.check` | Ask whether a user may act, using permissions the module itself declares (or Core's listed ones), with the own/any/superuser rules | `apps.accounts.authz.load_actor` and the Core access rule `apps.accounts.access` (section 3.8; Request Intake's action table is `apps.intake.access`) |

`permissions.check` belongs to Core: the `permissions` namespace is reserved, so no module can provide it, and a module cannot
provide any of the five either (it would need to own a reserved namespace). Requiring an unknown capability is `missing_capability`;
requiring a planned one is `capability_not_yet_available`. **Capability execution does not exist**: a test checks that the package
defines no function or class with the names of the services it will one day delegate to.

### 3.5 Permissions and scopes

A permission id is `<namespace>.<codename>` and is also the Django `app_label.codename`, so a built-in module's permissions are
backed by a model's `Meta.permissions` (as `RequestSource` does for the existing `intake.*` permissions; Django creates a permission
only for a model). The module must own the namespace; Request Intake keeps its existing ids through `extra_namespaces=("intake",)`.
The `scope` says what the permission reaches:

| Scope | Reaches | Example (Request Intake today) |
| --- | --- | --- |
| `global` | No particular record | `intake.submit_request` |
| `own` | Only records the user owns | `intake.view_own_request`, `intake.edit_own_request` |
| `any` | Any record | `intake.view_any_request`, `intake.approve_request`, `intake.reject_request` |

A scope is a **declaration**. Nothing in the contract applies it: the authorization services check scope against the stored record,
as they do now. The default scope is `any` (unchanged from MOD-1a), so a declaration that wants a narrower reach states it. A test
checks that the synthetic Request Intake declarations use scopes consistent with how `apps.intake.access` uses each permission.

### 3.6 Default role grants

A `RoleGrant(role, permissions)` seeds a **default** grant to one of the three existing groups, `Operator`, `Approver` or `Auditor`
(`KNOWN_ROLES`, kept equal to `apps.accounts.roles.ROLES` by a test). Owner decision: no new roles in this slice; a module cannot
define a role. Rules, each enforced and tested:

1. A module may grant **only permissions it declares itself** (`role_grant_undeclared_permission`). Because a declared permission
   must be in an owned namespace, this also means **no cross-module grants** and no grant of a Core permission by a module.
2. An unknown role is refused (`unknown_role`); a role granted twice by one module, or a permission twice in one grant, is refused
   (`duplicate_role_grant`); a grant must name at least one permission (`empty_role_grant`).
3. **No implicit permissions.** A declared permission that no grant lists belongs to no role, whatever its scope; a scope never
   grants anything.
4. Grants are immutable, sorted, and part of the canonical form, so they are part of the fingerprint. A manifest that uses neither
   `role_grants` nor `maintenance_hooks` keeps exactly the canonical form, and fingerprint, it had before they existed.

**Derivation.** `derive_role_permissions(registry)` returns `role -> frozenset(permission ids)` for the three roles, in a fixed order:
Core's own grants (`CORE_ROLE_GRANTS`: the Auditor reads the audit history, a Core permission no module declares) united with every
registered module's grants. It does not depend on registration order, ignores whether a module is enabled (so disabling never silently
revokes access), and is **not** wired to `setup_roles` or to `/api/auth/me/`: `apps/accounts/roles.py` remains the source of truth.
`derive_all_permissions(registry)` returns every declared permission plus Core's (the set `/me` may report).

**Golden equivalence (tested).** A synthetic Request Intake manifest (`tests/module_support.py`, built without importing
`apps.intake` into `apps.modules`) derives **exactly** the existing `apps.accounts.roles.ROLE_PERMISSIONS` and `ALL_PERMISSIONS`, role
by role, including the Auditor's Core permission; a guard test changes one grant and checks that the equivalence then fails. A
Django-runner test also checks that the synthetic permission ids and descriptions equal the real `RequestSource` model's
`Meta.permissions`.

### 3.7 Maintenance hook declarations

A `MaintenanceHookDeclaration(hook_id, while_disabled, description)` declares that a module will have bookkeeping work the worker runs
after every iteration (today Request Intake's reconciler is such a thing). **Only the declaration exists.** There is no callable, no
import path and nothing that executes; `hook_id` is an **opaque name**, exactly `<owned namespace>.<name>` (two segments, so it cannot
be a deeper, import-path-shaped string), unique across the registry, and nothing may ever resolve it as a path: a later slice
attaches the real callable through the composition root, by id. `while_disabled` says whether the worker keeps running the hook while
the module is disabled (true for bookkeeping that must not stall). The registry lists hooks as `RegisteredHook(module_id, hook_id,
while_disabled)`, deterministically: modules in registration order, then hook id. S1a did not change `worker.py`. S2 adds the worker's side of this
(section 3.9) without reading the declarations: the worker keeps its own, smaller id check and knows nothing about manifests.

### 3.8 The Core authorization rule (MOD-1b / S1b)

**What moved.** The generic own / any / superuser algorithm, which was inside `apps/intake/access.py`, is now `apps/accounts/access.py`:
pure Python, standard library only (a test pins that its only import is `dataclasses`), no Django, no database, no logging, and no
word about requests. This is a **behaviour-preserving refactor**: no permission, role, group, migration, endpoint or outcome changed.

**The Core interface.** `AccessRule(any_permissions, own_permissions)` is a frozen description of one action: the permissions that allow
it on any record and those that allow it only on the actor's own records (each normalized to a `frozenset` of text; anything else is a
`TypeError`). Three functions take a rule and facts the caller already read (`load_actor` reads them; Core reads nothing), all with
keyword-only arguments: `permission_problem(rule, *, is_superuser, permissions)`, `scope_problem(rule, *, is_superuser, permissions,
actor_pk, owner_pk)` and `access_problem(rule, *, is_superuser, permissions, actor_pk, owner_pk=None)`. They return a stable code or
`None`: `missing_permission` and `out_of_scope` (constants `MISSING_PERMISSION`, `OUT_OF_SCOPE`).

**The exact algorithm** (moved unchanged):

1. A superuser passes both parts: it bypasses role membership and the own-record scope. Core has no other rule for it to bypass.
2. Permission part: not a superuser and holding none of the rule's any or own permissions is `missing_permission`.
3. Scope part: not a superuser; holding an *any* permission passes; otherwise holding an *own* permission passes only when `owner_pk` is
   not `None` and equals `actor_pk` (pk `0` is a real owner, an unknown owner never matches); holding an own permission for another
   user's record is `out_of_scope`; holding none is `missing_permission`, so the scope part alone never lets anyone through.
4. `access_problem` is the permission part first, then the scope part. A user with no permission therefore gets the same answer for
   their own record, somebody else's and an unknown one.

**What stays in Request Intake.** `apps/intake/access.py` keeps the `Action` vocabulary and the table that says which permission each
action needs (`_ANY`, `_OWN`, unchanged), builds one `AccessRule` per action from it, and its three functions keep their names,
signatures, `submitter_pk` argument and `ValueError` for an unknown action; each is now a single call into Core (a test pins that
they hold no logic of their own, and that the algorithm exists once in the application). `apps/intake/authorization.py`
(`require_permission` before any lookup, `authorize` after the run lock, with the single refusal log line), `review.py`,
`submission.py`, `queries.py`, `load_actor`, `log_refusal` and `AuthorizationError` are **unchanged**, so error codes, refusal
precedence, inactive and deleted user handling, logging and transaction boundaries are those of the code before. **Business rules did not
move:** self-approval ban, state guards, stale revision and content-hash checks, warning acknowledgement and workflow transitions remain
in `apps/intake/review_rules.py` and the services; none of them takes a superuser flag.

**Behaviour-preservation evidence** (all DB-free, run in the development VM):

- `tests/authz_oracle.py` is a frozen copy of the old algorithm (it imports nothing from `apps`). The truth table of the old code over every
  combination of 6 actions x 512 permission subsets (the seven known permissions plus two they must not match) x 2 superuser flags x 6
  actor/submitter scenarios (same user, other user, no submitter, pk 0, nothing known, a value of another type) = **36 864 rows, three
  functions each**, was recorded **before the refactor** as a SHA-256 digest (`tests/authz_truth_table.py`,
  `BASELINE_DIGEST` in `tests/test_authz_core_access.py`). The oracle, the refactored `apps.intake.access` and Core called directly with
  rules built from the oracle's tables all reproduce the digest, and every row is compared one by one.
- Every role, a user with two roles, and a superuser, on own, other and unknown records; the exceptions (`ValueError` for an unknown action
  even for a superuser, `TypeError` for a bad permission collection); keyword-only arguments; the public signatures.
- `tests/test_authz_glue_equivalence.py` runs the unchanged glue with a fabricated actor, over all 512 permission subsets, both superuser
  flags and every scenario, and compares the decision, the stable code, the single log line (action, reason, public id) and the returned
  actor with the oracle; it also pins the precedence (an unauthenticated or inactive user is refused before the action is even looked at) and, by AST, that
  the services still require permission before any lookup or lock and re-check scope after the lock, inside the transaction.
- 15 deliberate mutations of Core and of the Request Intake table (for example removing the superuser bypass, inverting the owner
  comparison, swapping the order of the two parts, swapping any and own) were each caught by these tests.

**What the VM cannot show, and the PostgreSQL tests that cover it.** The VM has no PostgreSQL and ran none of these. The new
`tests/test_authz_core_postgres.py` compares the real `load_actor`, real groups made by `setup_roles` and real users
(every role, role pairs, superuser, inactive, deleted, stale objects) with the oracle. The existing suites cover the rest and must
still pass unchanged: `tests/test_review_postgres.py` (`AuthorizationTests`, `SubmitAsTests`, `EditServiceTests`, `ApprovalServiceTests`,
`RejectionServiceTests`, `ReadServiceTests`, `ConcurrencyTests`) and `tests/test_api_postgres.py` (`IsolationApiTests`, the approval,
rejection, edit and audit API classes). Intended command, from `backend\`, with the runbook's database set up:
`python manage.py test tests.test_authz_core_postgres tests.test_review_postgres tests.test_api_postgres --tag requires_postgres -v 2`.

**Owner-supplied local PostgreSQL result (2026-11-17; not run, repeated or observed by the VM).** The project owner ran the PostgreSQL
authorization, review and API integration tests on the local Windows computer for the S1b commit `962d6ab` and reported: **242 tests
passed, runtime 55.659 seconds, exit code 0, and the test database `test_automation_dev` destroyed successfully.** The reported
scenarios include Django users and groups, authorization equivalence, the review services, API permissions, concurrent edits and
approvals, and the end-to-end API workflow. The output is not stored in the repository, and the exact command line and module list were
not supplied. This validates the tested scenarios on the owner's local PostgreSQL only: the refactor changed no outcome in them. It does
not prove that no other race or authorization path exists, it says nothing about module-driven permissions (none exist yet), and it is
not a production-readiness claim. Record: [local-validation-record.md](../development/local-validation-record.md).

**Not done here, by design.** Core does not know which module owns a permission, does not read `ModuleState` (there is none), and
nothing derives `AccessRule` objects from manifests: the permission scopes of section 3.5 are still declarations, and the table of
Request Intake is still written in code. A later slice will build rules from a registered module's declarations (scope `own` to
`own_permissions`, `any` and `global` to `any_permissions`) and put the generic read-actor-then-decide-then-log sequence behind the
`permissions.check` facade; a golden test will then have to show that rules built from the manifest equal the table above. A disabled
module's actions are blocked by a separate gate, never by changing who holds a permission. `apps/modules` does not import Core's
rule (a test pins it), and `setup_roles`, `/api/auth/me/` and `profile.py` are untouched.

### 3.9 The worker seams: claim filter, maintenance hooks, kind gate (MOD-1b / S2)

S2 changes only `apps/tasks` (`policy.py`, `services.py`, `worker.py`). The queue stays one queue and one worker; the seams are optional and
generic, they know nothing about modules, and nothing in the repository uses them yet except the tests.

**Claim filter.** `claim_next_task(*, worker_id, lease_seconds, now=None, allowed_kinds=None)`:

| `allowed_kinds` | Meaning |
| --- | --- |
| `None` (default) | No restriction. The statement is the one every existing caller has always run; callers that omit the argument are unchanged. |
| empty set, frozenset, list or tuple | Claim nothing. The arguments are validated, then `None` is returned **without any database query**. |
| non-empty set, frozenset, list or tuple of strings | `kind IN (...)` is added to the **same** locking statement (`SELECT ... FOR UPDATE SKIP LOCKED ... ORDER BY available_at, created_at, id LIMIT 1`). |

Anything else is refused with `QueuePolicyError` before any query: a bare string (it would be read as characters), a mapping (its `False`
entries would read as allowed), a generator, `bytes`, a number, a member that is not a string of 1 to 64 characters. The rule is that a caller's
mistake can narrow a claim and never widen it. The filter is a `WHERE` condition, so a task of another kind is **never locked, claimed, counted
or changed** and stays queued; this includes a task whose kind no allowlist knows. Nothing is claimed and then put back. Order, the lease token,
`attempt_count`, the `TaskAttempt` row, retries with backoff, recovery of expired leases, cancellation and fencing are the code they were; none of
them reads the filter (a static test checks that recovery, renewal, completion, failure and cancellation never mention it).

**Unrestricted legacy behaviour is unchanged.** Without a filter, an unknown kind is still claimed and failed permanently (`unknown_kind`), as
before; a test pins it. A filtered claim does not judge kinds: a filter that names an unknown kind can claim it (the worker's narrowing, below,
is what keeps such a task from being claimed by a gated worker).

**Maintenance hooks.** `Composition(registry, after_iteration, maintenance_hooks=(), kind_gate=None)`; the two new fields are last, so positional
use of the first two is unchanged. `maintenance_hooks` is a **tuple** of `MaintenanceHook(hook_id, run)`:

- `hook_id` is an opaque name, exactly `<namespace>.<name>` in lower case (the same two-segment shape as the manifest's hook id, so it cannot be an
  import path). `run` is a callable the composition root already imported and takes no arguments. A string, a path or a list of hooks is refused.
- **Order.** After every iteration, idle or not, after the outcome is recorded: first the legacy `after_iteration` hook (its fixed id is
  `core.after_iteration`; `core` is a reserved namespace, so no module hook can carry it, and the id is reserved even when no legacy hook is
  set), then `maintenance_hooks` **in the order they were written in the composition root** (not sorted, not by id). One composition is used for
  the whole iteration.
- **Duplicate ids** are refused when the composition is installed (`ValueError`, naming the id); a refused composition changes nothing.
- **Failure isolation.** An exception from a hook other than a database error is logged and the next hook still runs; the log line is the hook id
  and the exception **class** only (the legacy hook keeps its original line, `after-iteration hook raised <Class>`). Messages, tracebacks and task
  payloads are never logged, and a hook failure does not change a task's recorded outcome.
- **Database errors** follow the existing recovery unchanged: a `DatabaseError` from any hook is re-raised, the worker loop closes the connections
  and retries after the poll interval, and after five in a row the worker stops. **Consequence:** the hooks after the failing one do not run in that
  iteration, and a hook that fails persistently with a database error therefore starves the hooks after it and eventually stops the worker, exactly
  as the single hook could before.
- No dynamic imports: the hook callables are objects, and a test pins that `apps/tasks` imports nothing dynamic and no module or business code.
- `while_disabled` (section 3.7) is **not read** by anything yet. Every installed hook runs every iteration; honouring `while_disabled` belongs to
  the S4 gate.

**Kind gate.** `Composition.kind_gate` is a callable with no arguments returning the kinds that may be claimed (a set, frozenset, list or tuple of
strings). Per iteration the worker does: recover expired leases, **ask the gate**, claim with the answer, run, record, run the hooks.

- **No gate:** nothing changes; the claim is unrestricted (`allowed_kinds=None`).
- **Narrowing.** The answer is intersected with the kinds the worker has a handler for (the registry in use, or the default allowlist). A kind the
  gate allows but this worker cannot run therefore stays queued instead of being claimed and failed. This is deliberate (a worker that is older
  than the process that enqueued the task must not destroy it) and matches the owner decision in section 8 that a claim takes only kinds that are
  enabled **and** known to the worker. It applies only when a gate is installed.
- **Empty answer:** no task can be claimed. Recovery of expired leases and the hooks still run.
- **Fail closed.** A gate that raises, answers `None`, or answers anything that is not a set, frozenset, list or tuple of valid kind strings allows
  **nothing**, logs one line with the exception class (or "invalid answer") and never the answer, and the worker goes on to its hooks. `None` is not
  "unrestricted". A `KeyboardInterrupt` is not swallowed.
- **A database error from the gate** (the S4 gate will read `ModuleState`) is re-raised before any claim and follows the worker loop's connection
  recovery, like the hooks; nothing is claimed.
- **Held, not failed.** Tasks of a gated-off kind are not touched: still `queued`, `attempt_count` unchanged, same position. When the gate opens they
  are claimed as attempt 1 (or the next attempt, for a retry). A running attempt whose kind is gated off is allowed to finish.
- **Race window.** The gate is read once per iteration and the claim follows immediately; a kind gated off between the two can still be claimed by
  that one claim. The S4 design accepts this (attempts already in flight finish), so it is a property to know, not a bug to fix here.
- **Layering.** `apps/tasks` receives a plain callable from the composition root (`config/task_registry.py`; since S4 it installs the module gate,
  section 3.11). It imports neither `apps.modules` nor `apps.intake` (tests pin it). The `ModuleState`-backed gate is S4 (built); the use of
  `while_disabled` was **not** built in S4 (no module hook exists to gate).

**Compatibility with existing callers.** The production `COMPOSITION` has no gate and no extra hook (a test pins it); `runworker`,
`process_one`'s and `run_until_stopped`'s signatures (the real-process recovery tests wrap them), the existing hook semantics and every existing
test are unchanged. The one visible difference without a gate is that `process_one` now passes `allowed_kinds=None` to `claim_next_task`.

**Tests.** DB-free, run in the development VM: `tests/test_tasks_gating.py` (the filter value, the service with a mocked query chain and the
no-query empty filter, no-gate compatibility, gate narrowing and fail-closed cases, hook order, isolation, database errors and installation
rules, static guarantees; 22 deliberate mutations of the implementation were each caught). PostgreSQL:
`tests/test_tasks_gating_postgres.py` (39 tests: the filtered claim as one `FOR UPDATE SKIP LOCKED` statement with the kind condition,
untouched filtered-out and unknown-kind tasks, ordering, lease, attempt numbering, retry and recovery unchanged, threaded workers with disjoint
and overlapping filters, and `process_one` with an installed gate against committed data), together with the unchanged
`tests/test_tasks_postgres.py` (95) and `tests/test_tasks_recovery_postgres.py` (15; the real-process crash recovery).

**Owner-supplied local PostgreSQL result (the VM did not run, repeat or observe it; no output is stored).** The project owner ran those three
modules locally on the Windows computer and reported: **149 passed, runtime 66.399 seconds, exit code 0, and the dedicated test database
destroyed successfully** (the exact command line was not supplied; 39 + 95 + 15 = 149 is the VM's own count of the test methods). This
supports, **for the tested scenarios only**: filtered claiming as specified; the existing worker concurrency and process-level recovery
behaviour passing unchanged with S2 in place; and lease, retry, attempt numbering and fencing behaviour preserved. It does not prove that no
other concurrency race exists, it is one machine and one operating system, it says nothing about module state (there is none), and it is not a
production-readiness claim. The gate race window described above remains. Record:
[local-validation-record.md](../development/local-validation-record.md).

### 3.10 The audit vocabulary (MOD-1b / S3)

**Goal.** A module may declare audit actions (`AuditActionDeclaration`, section 3.1) and have them recorded by Core's **one** recorder
(`apps.audit.recorder.record_event`, unchanged), without a Core migration for every new action. Nothing existing changed: the seven Core
actions and their detail keys, the four Core subject types, the value rules (booleans, integers, null, short tokens of letters, digits and
`_ . : -`), the content restraint, and the rule that an event is recorded only inside the transaction of the operation it describes (an audit
failure rolls the operation back). Existing audit rows are not touched.

**Core versus module ownership.**

| Rule | Enforced by | Code |
| --- | --- | --- |
| The seven Core actions, their detail keys and the subject types `run`, `revision`, `approval`, `rejection` stay exactly as they were; `module` is added as a Core subject type (S4 uses it for `module.enabled` and `module.disabled`, section 3.11) | `apps/audit/events.py` constants, a frozen golden copy in `tests/test_audit_vocabulary.py` | - |
| A module cannot declare a Core action | the manifest (a Core action's namespace is reserved) and, again, the vocabulary | `core_action_claimed` |
| A module cannot declare an action in a namespace Core uses: the first segment of a Core action (`request`, `run`, `revision`, `approval`, `rejection`) or any reserved namespace | the manifest (all five Core prefixes, `request` included since the S3 owner decision, are reserved namespaces, so no registry can give them to a module) and, as defence in depth, the vocabulary itself | `core_namespace_claimed` |
| A module may declare an action only in a namespace it owns, as the registry knows it (`namespace_owners`); a dependency, a dependent and a prefix own nothing of each other | the vocabulary, fed the registry's owners | `namespace_not_owned` |
| An action is declared once (never "last one wins") | the registry (`duplicate_audit_action`) and the vocabulary | `duplicate_action` |
| An action has the shape (below) and at most 32 characters | the manifest and the vocabulary | `invalid_action_shape` |
| Detail keys: lower-case identifiers, at most 20, no duplicates, and no key whose name suggests free content (`text`, `name`, `email`, `contact`, `notes`, `description`, `message`, `excerpt`, `reason`, `error`, `password`, `username`); Core's own keys already avoid those names (a test) | **both**, by one rule: the manifest (`FORBIDDEN_AUDIT_DETAIL_KEYS`, `MAX_AUDIT_DETAIL_KEYS`; code `forbidden_audit_detail_key`) and the vocabulary apply the same names, the same character set and length (`[a-z][a-z0-9_]{0,39}`) and the same count limit, so a manifest that registers can always be composed. The two packages may not import each other, so the rule is written twice and tests pin the constants and compare behaviour on a generated sample | `invalid_detail_key`, `forbidden_detail_key` |
| Values keep Core's rules for every action alike | `apps/audit/events.py` | `AuditEventError` |

A module may **emit Core events through Core services** (for example `run.state_changed`); it cannot redefine them or their detail keys. A
module's declared action is checked with the keys its own declaration lists, never Core's, and a Core action is checked with Core's keys, never a
module's.

**The design.**

- `apps/audit/vocabulary.py` (pure, standard library only; no Django, no `apps.modules`): `ModuleAuditAction(module_id, action, detail_keys)`
  (plain data, validated on construction) and `AuditVocabulary`, an immutable value (`__slots__`, read-only mappings, tuples, frozen sets, no
  public constructor). `AuditVocabulary.core(...)` builds Core's part; `core_vocabulary.with_modules(declarations, namespace_owners,
  reserved_namespaces)` returns a **new** vocabulary or raises `AuditVocabularyError` (stable `code`). Declarations are applied in a fixed order
  (by action), so the result and the first error depend only on content, not on input order or the process (checked across `PYTHONHASHSEED`
  values). Equal vocabularies have equal `fingerprint`s (SHA-256 of a canonical form); `core_fingerprint` identifies the Core part.
- `apps/audit/events.py`: Core's constants stay where they were, plus `CORE_VOCABULARY`, `current_vocabulary()` and `install_vocabulary()`.
  `NewAuditEvent` checks the action, the subject type and the detail keys against the **installed** vocabulary, which is Core's alone until one is
  installed. An action nobody declared, an undeclared detail key and a non-text action are `AuditEventError`, raised before anything reaches the
  database.
- `config/audit_vocabulary.py`, the explicit composition root: `build_audit_vocabulary(registry=None)` (pure) turns a validated `ModuleRegistry`
  into declarations and hands over the registry's `namespace_owners` and the manifest's reserved namespaces; `install_audit_vocabulary(registry=None)`
  builds and installs. `registry=None` means `config.modules.BUILTIN_MODULES`, which is **empty**, so the built-in result equals Core's vocabulary
  (a test). Importing it needs no Django and reads no database or file (a subprocess test); it names no module and imports no business code.
  Core packages never import it (tests pin that), and `apps.audit` imports neither `apps.modules` nor `config`.

**Initialization.** `install_vocabulary` is idempotent and never a silent replacement: installing a vocabulary equal to the installed one (same
fingerprint) changes nothing; installing a different one, **including Core-only after a module one**, is refused (`vocabulary_conflict`), and
so is one built on a different Core part. It runs under a lock, so concurrent installations leave exactly one winner. **Nothing in the application
calls it yet** (no settings module, `AppConfig.ready()`, entry point or command; a test pins that): with no module registered, Core's own
vocabulary is already in force. The first module (MOD-2) decides where the call belongs; the proposal is next to the worker's composition
(`runworker`) and the API's start-up, **before any event is built**, in every process that records module events (the API and the worker are
separate processes and must install the same vocabulary; compare `fingerprint`s, as for the registry). A process that never installs one refuses
module actions: that fails loudly, not silently. Tests restore state with `mock.patch.object(events, "_installed", ...)`
(`tests/audit_support.py`: `installed(...)`, and a mixin that fails any test that starts with something installed), so nothing leaks between tests.

**Database shape check versus Python allowlist.**

| Question | Answered by |
| --- | --- |
| Is the action text well formed (two or more lower-case dotted segments, at most 32 characters)? | **Both**: the database CHECK `audit_event_action_shape` and `action_shape_ok` use one pattern, `ACTION_SHAPE_PATTERN` (`^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39}){1,15}$`; the migration repeats the text because a migration must not import application code, and a test keeps them equal) |
| Is the action declared by Core or a module? | **Python only** (`AuditVocabulary`) |
| Are the detail keys the declared ones? Are the values safe? | **Python only** |
| Is the subject type known? | **Both**: a fixed list in the CHECK (`run`, `revision`, `approval`, `rejection`, `module`) and in Python |

The pattern accepts every action a manifest may declare (a test compares it with the manifest on a large generated sample) and every Core action.
Its repetition is bounded (the column holds 32 characters and a segment plus its dot takes at least two, so at most 16 segments). It uses only
anchors, character classes and one bounded group, so PostgreSQL and Python should read it alike; **the PostgreSQL test that compares them on a fixed
corpus passed in the owner's local run (below), for that corpus only**.

**What this does not protect.** Because the database checks syntax only, raw SQL, a privileged migration or a database administrator can store any
well-formed action, declared or not, and nothing detects it (a PostgreSQL test pins this so nobody claims more). Namespace ownership and the
vocabulary are naming rules inside one process: code admitted to the process can import Core internals, build events, or write the table. Neither
the ORM's immutability nor a namespace declaration is database-level tamper protection or a sandbox. There is still no hash chain, trigger or
insert-only role (data-model open questions 2.5 and 2.18).

**The migration** (`apps/audit/migrations/0002_extensible_audit_vocabulary.py`; depends only on `audit.0001_initial`; additive in intent; nothing
is read, rewritten or deleted):

1. remove `audit_event_action_valid` (the fixed list of seven names) and add `audit_event_action_shape` (a regular-expression CHECK);
2. replace `audit_event_subject_type_valid` with the same name and the list extended by `module`;
3. a `RunPython` that does nothing going forward, and **on a reverse runs first as a guard** (below).

PostgreSQL validates the existing rows against both new constraints while adding them (a table scan under a brief exclusive lock; fine for today's
volume, worth knowing if the table grows large). Field lengths are unchanged (`action` 32, `subject_type` 16): no contract mismatch required a
change. Model state and migration agree: a DB-free test runs Django's autodetector against the migration graph without a database and finds no
change, and compares the migration's constraints with the model's.

**Rollback limits.** Reversing restores the old fixed lists, which is possible only while every stored event satisfies them. If an event with a
module action or the `module` subject type exists, the reverse is **refused with `IrreversibleError` before any constraint is touched**, naming
counts only. **Events are never deleted or rewritten to make a reverse succeed.** Past such events the way back is a restore from backup or a
reviewed, owner-run data decision outside this migration. Without such events the reverse restores the old constraints and keeps every row. Also
note that after a rollback of the *code* (not the migration), Python would again refuse module actions while their rows stay readable.

**Not done in S3** (S4 added `ModuleState` and enable/disable, section 3.11; the S4 audit actions are Core's, not declared by a module). The capability facade (`audit.record` is still only a name a manifest may require), a
module-owned subject type, storing the vocabulary, a database-level allowlist or protection, and wiring any entry point. Request Intake is not a
module and emits only Core events. The API's audit serializer passes `action` through as text and the frontend shows an unknown action as its raw
name, so a module action displays without a change there.

**Tests.** DB-free (`tests/test_audit_vocabulary.py`, 102 tests, pass in the VM; each of 32 deliberate mutations of the
implementation was caught by the focused set): Core unchanged against a frozen copy, module actions accepted only when declared, refusals and their codes, namespace and
reserved-name rules, detail keys and values, immutability, determinism (input order, processes), idempotent and conflicting installation, state
isolation, the composition root (and that it needs no Django), shape agreement with the manifest, the migration and the model, the transaction
requirement, "one recorder", and the import boundaries. Pinned older tests changed narrowly because the design changed: `test_review_audit.py`
(the action constraint is now the shape, by a new name), `test_review_boundaries.py` (`events.py` may import `threading` for the install lock) and
`test_module_boundaries.py` (`config/audit_vocabulary.py` is the second, and only other, importer of `apps.modules` and of `config.modules`).
PostgreSQL (`tests/test_audit_vocabulary_postgres.py`, **24 tests; passed in the owner's local run, see below**; the VM never ran them): the forward migration with existing rows (unchanged),
the constraints as PostgreSQL stores them, PostgreSQL's reading of the pattern against Python's, a valid module action and the `module` subject
through the recorder, an unknown well-formed action refused by Python but accepted by the database, malformed actions refused by the database,
rollback of the business operation when the audit write fails (database-level, Python-level and a Core service), Core's history unchanged and the
audit API unchanged with a module vocabulary installed, and the reverse migration with and without events only the new vocabulary allows.

**Local PostgreSQL validation (owner-run; the commands, and the result recorded after them).** From `backend\`, with the database set up as in the runbook (section 6) and
`AP_RUN_DB_TESTS` unset in the window before and after:

```powershell
$Py = "$LocalProject\.venv\Scripts\python.exe"
Set-Location "$LocalProject\backend"
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set in this window. Run: Remove-Item Env:AP_RUN_DB_TESTS   then repeat." }

# 1. Preflight against the development database (reads it; applies nothing)
& $Py manage.py showmigrations audit                                          # expect: 0001 [X], 0002 [ ]
& $Py manage.py makemigrations --check --dry-run                              # expect: No changes detected
& $Py manage.py sqlmigrate audit 0002_extensible_audit_vocabulary             # read the DDL: two DROP CONSTRAINT, two ADD CONSTRAINT
& $Py manage.py sqlmigrate audit 0002_extensible_audit_vocabulary --backwards

# 2. DB-free tests
& $Py manage.py test                                                          # expect OK

# 3. PostgreSQL tests: Django creates and destroys test_<AP_DB_NAME>; no --keepdb, no --noinput
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_audit_vocabulary_postgres tests.test_authz_core_postgres tests.test_review_postgres tests.test_api_postgres tests.test_intake_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                                                # expect 0
} finally {
    Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue
}
```

Report the exact `Ran N tests` line (the new module contributes exactly 24) and any failure; the existing review, API and intake suites must pass
unchanged. Applying the migration to a development database that already holds audit events (`python manage.py migrate audit`) is a separate,
optional step; take a backup first and do not run it against anything that is not disposable until the PostgreSQL tests have passed. This
validates the tested scenarios on one machine only and would not be a production-readiness claim.

**Owner-supplied local PostgreSQL result (the VM did not run, repeat or observe it; no output is stored):** audit migration `0002` completed, the 15 audit events that existed remained after it, the 24 new PostgreSQL tests passed, the corrected schema-constraint test passed, and the selected PostgreSQL regression ran **342 tests, all passed, 80.063 s, exit code 0, dedicated test database destroyed**. The first run of the selected modules reported 342 tests with 341 passed and 1 failed (exit code 1): `SchemaTests.test_the_database_has_every_named_constraint` in
`tests/test_review_postgres.py` still expected the old constraint name `audit_event_action_valid`; the new constraint existed, the failure was a
stale test expectation, and one line in that test was corrected (commit `5398a1b`, test code only). The command line, the module list and the
output were not supplied; for orientation only (the VM's arithmetic, not stated by the owner) 342 equals the 24 tests of
`test_audit_vocabulary_postgres.py` plus the earlier 242 authorization, review and API tests plus the 76 intake tests.

**What this supports (this environment and these scenarios only).** The migration applied to a database that already held audit events and left
them in place (15 events, as reported; the PostgreSQL test additionally compares every column of every pre-existing row before and after in the
test database); the new action-shape CHECK (`audit_event_action_shape`) and the extended subject-type CHECK exist and behave as the tests expect
(well-formed actions and the `module` subject accepted, malformed ones refused by the database, PostgreSQL and Python agreeing on the fixed
corpus); the forward and the reverse migration with and without events that only the new vocabulary allows (the reverse refused, nothing
deleted, when such events exist); a module action recorded through the one recorder; rollback of the business operation when the audit write
fails; and the unchanged review, API and intake audit behaviour in the regression. **What it does not show:** that the database enforces the
allowlist (it does not: raw SQL can still store any well-formed action, a test pins this); tamper protection of audit events (ORM-level
immutability only, no hash chain or trigger); any module-driven behaviour (no module is registered, nothing installs a module vocabulary, and
there is no `ModuleState` or enable/disable); behaviour with many or large audit tables (the constraint is added with a table scan under a brief
exclusive lock); another PostgreSQL version or machine; or that no other race exists. Not a production-readiness claim. Record:
[local-validation-record.md](../development/local-validation-record.md).

### 3.11 The module host: persistent state, sync, enable/disable and the worker gate (MOD-1b / S4)

**Status.** Implemented on `feat/module-runtime-state` (committed locally, not pushed). DB-free tests pass in the development VM; the PostgreSQL tests and the
migration have **not been run by anybody**. `apps/modules` stays pure and gains no model; the runtime state is a separate Django app, `apps/module_host`
(label `module_host`), installed in `INSTALLED_APPS`. **No module is registered** (`BUILTIN_MODULES` is empty), Request Intake is **not** a module, there is **no
public API** (no route, view or serializer), and nothing is installed, downloaded, imported by name or migrated by the host.

**Schema (migration `module_host.0001_initial`: three `CREATE TABLE`s and their indexes, nothing else; no existing migration was modified).**

| `ModuleState` column | Type and rule |
| --- | --- |
| `id` | UUID primary key |
| `module_id` | `varchar(40)`, unique; CHECK `^[a-z][a-z0-9_]{2,39}$` (the manifest's own shape) |
| `installed_version` | `varchar(32)`; CHECK for the documented SemVer subset (`MAJOR.MINOR.PATCH`, no pre-release); the version the stored data belongs to |
| `enabled` | boolean NOT NULL, **no default**: whoever creates a row says so |
| `configuration_version` | nullable integer (>= 0); reserved, always null (no configuration is stored yet) |
| `registry_fingerprint` | `varchar(64)`, CHECK lower-case hex; the static registry's fingerprint when the row was last confirmed by a sync |
| `created_at`, `updated_at`, `enabled_changed_at` | timestamps with time zone; the last equals `created_at` until `enabled` changes |

| `ModuleNamespaceClaim` column | Type and rule |
| --- | --- |
| `id` | UUID primary key |
| `namespace` | `varchar(40)`, **unique**; CHECK `^[a-z][a-z0-9_]{1,39}$` |
| `module_id` | `varchar(40)`, indexed; CHECK as above |
| `claimed_at` | timestamp with time zone |

| `ModuleHostMarker` column | Type and rule |
| --- | --- |
| `id` | smallint primary key, CHECK `id = 1`: the database accepts exactly one row |
| `initialized_at` | timestamp with time zone: when the first sync completed |
| `initial_registry_fingerprint` | `varchar(64)`, CHECK lower-case hex: the registry that first sync ran against (an empty registry has one too) |

There is no foreign key and no Django permission for any of the three tables. **Enforced where:** the database enforces uniqueness, the one-row marker and the shape
checks only. A claim and the marker are append-only through the ORM (`ImmutableRecord`: no update, no reassignment, no reset, no delete; a namespace can be
claimed once; the services only ever insert them, a test pins it), and a `ModuleState` row cannot be
deleted through the ORM (a disabled module keeps its row and its history); updates go through the services under a row lock. Raw SQL, a migration, a
database administrator and private ORM internals can still change or remove these rows and nothing detects it (no trigger, no hash chain), exactly as for the
audit and case records. Code in the process is not sandboxed (section 7).

**Sync (`sync_modules`, command `module_sync`).** Repeatable; one transaction, serialized by a PostgreSQL advisory lock and the row lock below.

1. Reads **only** the static registry it is handed (which has already passed every compatibility, dependency, capability and namespace check of section 5);
   it imports nothing, runs no module migration and downloads or installs nothing.
2. **Namespace ownership.** The stored claims go through `check_namespace_continuity` (section 5.6) before anything is written: a namespace now owned by another
   module id is refused (`namespace_ownership_changed`), and one nobody claims any more is refused (`namespace_released`). Nothing is written in either case.
   Missing claims are inserted (a module may add namespaces); an existing claim is never touched.
3. A registered module **without a row** gets one, with `installed_version` = the registered version and `registry_fingerprint` = the current one. It starts
   **enabled** (so a release's modules keep working) **unless a direct dependency is not enabled and version-matched**, in which case it starts disabled. This is
   decided in registration order, so a chain starts consistently. One `module.enabled` or `module.disabled` event (cause `sync`) is recorded per created row.
4. An **existing row is never enabled or disabled**, and its `installed_version` is **never changed**. A registered version that differs from the stored one is a
   **version mismatch**: it is reported (and the command exits non-zero), the module stays unavailable, and nothing about it is written.
5. A row whose stored fingerprint is out of date, with a matching version, gets its fingerprint and `updated_at` refreshed (no event; this is not a state
   change). A row with no registry entry is reported (`unregistered`) and left alone; nothing is ever deleted.
6. With an empty registry a sync creates no state, but it **does write the initialization marker** (below).
7. **The initialization marker.** In the same transaction, and only if the whole sync succeeds, the first completed sync inserts the single `ModuleHostMarker` row.
   A refused sync (a namespace conflict, an audit failure) writes no marker: the transaction rolls back. The marker is never rewritten; a later sync leaves it alone
   (the one exception: rows with no marker, an inconsistent database, are repaired by inserting it).

**The backward-compatibility transition (explicit).** The installation existed before module state did. The rule, in `apps/module_host/rules.py` and used by the
gate and by `module_status`, is:

| Situation | Treatment | Reason shown |
| --- | --- | --- |
| The initialization marker does not exist (no sync has completed) and no state row exists | a registered module is treated as **enabled** | `legacy_unsynced` |
| The marker exists (the first sync completed), and this registered module has no row, **even if no row is left at all** | **fails closed**: its task kinds are held | `missing_state` |
| Row present, installed version differs from the registered one | unavailable | `version_mismatch` |
| Row present, `enabled` is false | unavailable | `disabled` |
| Row enabled and version-matched, but a direct dependency is unavailable | unavailable (defence for a hand-edited database) | `dependency_unavailable` |
| Row enabled, version-matched, dependencies available | available | `enabled` |

"Initialized" is defined by the **durable marker, not by the presence of rows** (the first S4 draft inferred it from "some row exists", which let deleted rows, or
a sync that ran before any module existed, reopen the window; that was corrected in the S4 review). The gate reads the rows and then the marker (rows first, so a sync
committing in between can only make the answer more closed); `module_status` reads both. **One conservative exception:** rows with no marker (only a hand edit or a
partial restore can produce that) count as initialized, so the answer is never more open than the facts; the next `module_sync` repairs the marker. Consequences,
stated plainly: (a) with no registered module (today) the gate never reads the database and nothing is held; (b) an installation that **never** ran `module_sync` has no
marker, so when the first release that registers a module (MOD-2: Request Intake) arrives its modules run under `legacy_unsynced` until the administrator runs
`module_sync` once, which is the compatibility window; (c) **an installation that already ran `module_sync` before that release (the sync with an empty registry writes
the marker) has no window: the new release's modules have no row and are HELD (Request Intake's extraction included, once MOD-2 makes it a module) until `module_sync` is
run**, which is the upgrade routine (stop the worker, migrate, `module_sync`, start the worker); (d) **run `module_sync` after every upgrade, before starting the
worker**; (e) deleting every state row by hand no longer reopens the window (the marker stays); deleting the marker as well, by raw SQL, still would, and is not
protected against. Legacy kinds are unaffected in every case.

**Enable and disable (`enable_module`, `disable_module`; commands `module_enable`, `module_disable`).** Each is one `transaction.atomic()` block that locks
**every** `ModuleState` row `FOR UPDATE` in module-id order (a handful of rows; one fixed order, so no deadlock; this is also what makes "enable a dependent"
and "disable its dependency" mutually exclusive), reads the state, decides with the pure rules, writes, and records the audit event, all before the commit.

* **Unknown module** (not in the static registry) and a malformed id are refused before a transaction is opened. A missing state row is refused
  (`state_missing`: run the sync).
* **Idempotent.** Enabling an enabled module, or disabling a disabled one, writes nothing and records no event (the result says `changed: false`). Enabling an
  enabled module whose stored version does not match is **not** a no-op: it is refused.
* **Enable** is refused for a version mismatch (`version_mismatch`) and while any direct dependency is not enabled and version-matched (`dependency_not_enabled`).
* **Disable** is refused while any module that directly depends on it is enabled (`dependents_enabled`); disable the dependents first. A version mismatch does not
  prevent disabling.
* **Tasks and data are never touched.** The host imports no task model. Disabling holds the module's queued tasks (below); a running attempt finishes; failed
  and completed tasks, runs, revisions, approvals and audit events stay exactly as they are. Enabling re-creates and re-runs nothing: held tasks become claimable
  again, in their original order, on the worker's next iteration (no restart).
* **Actor.** Every change is made by the administrator command line and is audited as the **system** actor (no login user, no public id). There is no public
  API and no UI for enable or disable in S4.

**The worker gate (`apps/module_host/gate.py`, installed in `config/task_registry.py`).** `make_kind_gate(registry, legacy_kinds)` returns the plain callable S2's
`Composition.kind_gate` expects. Its answer is **the legacy kinds plus the kinds of every available module** (table above); everything else is absent, so a
disabled module's kinds, a version-mismatched module's kinds, a module's kinds with missing state after the sync, and every unknown kind are excluded. The
worker (unchanged code, section 3.9) narrows the answer to the kinds it has a handler for, fails closed if the gate raises or answers badly, re-raises a
`DatabaseError` so the existing connection recovery runs (nothing is claimed), keeps recovering expired leases and running its hooks, and leaves every task of
an excluded kind **queued, untouched and unfailed**. A running attempt of a kind that is gated off finishes and is recorded; retry, backoff and lease recovery are
unchanged. The state is read once per iteration; the S2 race window (a kind gated off between that read and the claim can be claimed by that one claim) remains.

* **Legacy kinds and Request Intake.** `config/task_registry.py` defines `LEGACY_UNGATED_KINDS = {test.add_numbers, request_intake.extract}`, taken from the two
  handlers' own constants; a test pins it equal to the production allowlist, so a new production kind forces a decision. Legacy kinds are claimable with no module
  state at all: **Request Intake's `request_intake.extract` is not gated, and works as before, with or without any `ModuleState` row, even if every other module is
  disabled.** `make_kind_gate` refuses (`legacy_kind_claimed`, when `config.task_registry` is imported) a legacy kind that a registered module also declares, so
  MOD-2 cannot register Request Intake without removing its kind from the legacy set in the same change; gating cannot start or fail to start by accident.
* **Today** the registry is empty, so the production gate answers the legacy set **without reading the database**. Its first read happens with the first
  registered module.
* **One visible difference from before S4.** The production composition now carries a gate, so the worker's claim is filtered to the allowed, known kinds. A task
  whose kind this worker has no handler for (a hand-inserted row, or a task from a newer release) is therefore left queued instead of being claimed and failed as
  `unknown_kind`; the claim statement gains one `kind IN (...)`. This is the S2 narrowing (section 3.9), now active in production. Without a gate (tests) the old
  behaviour is unchanged.
* `while_disabled` of a maintenance hook is still **not** consulted: no module hook exists and the production composition installs none.

**Audit.** `module.enabled` and `module.disabled`, subject type `module`, `subject_id` = the `ModuleState` id, system actor, details `module_id`,
`installed_version`, `registry_fingerprint` and `cause` (`admin_command` or `sync`): identifiers, a version, a hash and a short token; the value rules and the
no-free-text key rules of section 3.10 apply (a test refuses a `name`, `note` or spaced value). The event is recorded by Core's one recorder inside the
operation's transaction: **a failed audit write rolls the state change back**, and an idempotent no-op records nothing. **Why these are Core's actions and not
"module-declared":** `module` is a namespace the contract reserves for Core, so no manifest can declare an action in it, and S3's `with_modules` refuses it. S4
therefore added both to Core's own vocabulary (`apps/audit/events.py`: `ACTIONS`, `DETAIL_KEYS`). **This changes the S3 pin "Core has seven actions"**: the seven
actions and their detail keys are unchanged, and `tests/test_audit_vocabulary.py` (the frozen copy) and `tests/test_review_audit.py` were updated to expect nine.
Nothing is installed into the process-wide vocabulary (`install_vocabulary` is called by nothing new; Core's own vocabulary is always in force); no audit
migration was needed (the database already accepted the `module` subject type and any well-formed action, section 3.10).

**Administrator commands** (`python manage.py ...`, from `backend\`; each reads the static registry from `config.modules`, validates its input, closes the
database connections, reports a database error by class only, and exits non-zero on a refusal):

| Command | What it does |
| --- | --- |
| `module_sync [--json]` | the sync above; non-zero exit (after printing the report) for a version mismatch or an unregistered state row |
| `module_status [module_id] [--json]` | read only: per module, registered and installed version, `enabled`, `claimable` and why, task kinds, dependencies, fingerprint currency |
| `module_enable <module_id> [--json]` | `enable_module` |
| `module_disable <module_id> [--json]` | `disable_module` |

`--json` prints one document with sorted keys and `"format": 1` (status: `registry_fingerprint`, `synced`, `modules[]`, `unregistered_state`; sync: `ok`,
`initial_sync`, `created`, `refreshed`, `unchanged`, `version_mismatches`, `unregistered`, `namespace_claims_added`; enable and disable: `module_id`, `changed`,
`enabled`). Text output shows a 12-character fingerprint prefix. The commands take no other argument: no path, no shell text, no `--migrate`, no install.

**Future update compatibility (design intent; nothing here is an update manager).** `installed_version` and `registry_fingerprint` are what an update manager
will compare with a new release: the sync never changes `installed_version`, so a release with another version shows up as a mismatch and the module stays
unavailable until an update step (migration preflight, `update.data_schema_version`, backup, then an explicit audited change of the stored version) exists. The
namespace history is persisted and checked at every sync, so a release that hands a namespace to another module id, or drops one, is refused (section 9, "Namespace
continuity"). `configuration_version` is reserved for the versioned company configuration (ADR 0005). A sync run for a new release must come before the worker
of that release starts, or its new modules' kinds stay held.

**Remaining limitations.**

* No module is registered; nothing is gated in practice. The gate, the sync and the enable/disable services have been exercised only by DB-free tests with the
  database replaced, and by **PostgreSQL tests that nobody has run**.
* "Initialized" is the marker (above). Raw SQL, a migration or an administrator that deletes the marker AND every state row reopens the legacy window; nothing detects it.
  An empty-registry `module_sync` run before MOD-2 closes the window for the MOD-2 release (run the sync with that release).
* No public API and no UI to enable or disable, and no API-side use of the state: **a disabled module is not "read-only" beyond held tasks**, because there is no
  module write path yet. Maintenance hooks (`while_disabled`) are not wired. The API process does not read `ModuleState` and does not compare registry
  fingerprints with the worker.
* A version mismatch has no remedy in S4 (no update path): the module stays unavailable until the stored version and the release agree.
* The gate race window (S2) remains; a task is held by kind, not by "who enqueued it", and enqueueing is not blocked for a disabled module (the producer, not
  the host, decides; Request Intake is not a module).
* Integrity is ORM and service level only (no trigger, no revoked privilege); the audit table is not tamper-protected (section 3.10). A module in the process
  is not sandboxed.
* The advisory lock and row locks are PostgreSQL features (the only supported database); their behaviour under load is untested.

**Tests.** DB-free (all pass in the VM, 198 new tests): `tests/test_module_host_rules.py` (56: the availability and legacy-transition rule, allowed kinds, sync plan,
enable/disable checks), `tests/test_module_host_services.py` (52: the services with the models, lock, transaction and recorder replaced by recording stand-ins;
order of lock, save, event and commit; idempotency; refusals; rollback on an audit failure; status; the Core audit actions), `tests/test_module_host_gate.py` (26:
the production gate and Request Intake's legacy kind, the gate with modules, the S2 worker with the gate), `tests/test_module_host_cli.py` (28: input validation,
output, stable JSON, safe errors) and `tests/test_module_host_boundaries.py` (36: imports, no dynamic import or process, no migration run, no delete or raw
SQL beyond the one advisory lock, one atomic block per change, model and migration contract, model/migration agreement without a database). Narrowly updated
older pins: `test_audit_vocabulary.py`, `test_review_audit.py`, `test_module_boundaries.py`, `test_tasks_gating.py`, `test_settings.py` and
`test_request_intake_django.py` (installed apps, callers of `config.modules`, the production gate, the nine Core actions). Each of 25 deliberate mutations of the
implementation (8 of them aimed at the initialization marker) (fail-open missing state, version check removed, legacy kinds dropped, no idempotency, no audit event, dependents ignored, new modules always
enabled, an empty-registry gate that answers nothing, a legacy overlap accepted, continuity skipped, a sync that enables, no gate in the composition, no row lock,
and others) was caught by the focused set. PostgreSQL (`tests/test_module_host_postgres.py`, **73 tests, NOT RUN**): the stored constraints, the migration
(applied, reversed and re-applied without touching other tables or rows), sync, namespace ownership that persists and cannot change, enable and disable
against real rows, audit consistency, rollback when the audit write fails, concurrent syncs, enables and disables (including the dependent/dependency race), and
the worker on committed data (tasks held, resumed, a running attempt finishing, recovery, retry, version mismatch, missing state, Request Intake end to end with
the production composition). The unchanged `test_intake_postgres`, `test_api_postgres`, `test_tasks_*_postgres` and `test_audit_vocabulary_postgres` suites are
the regression and must still pass. **Commands for the owner: [runbook.md](../development/runbook.md), section 5.1.**

## 4. Versioning

### 4.1 The SemVer subset

`apps.modules.versions`. Deliberately small, not a package manager.

- A version is exactly `MAJOR.MINOR.PATCH`: non-negative integers, each `0` or without leading zero, at most nine digits.
  `1.2.3` is valid; `1.2`, `01.2.3`, `v1.2.3`, a trailing space or newline are not.
- **No pre-release tags and no build metadata** (`-beta`, `+5` are refused, not ignored). If they are ever needed the contract gets
  a new manifest version.
- Versions compare numerically by (major, minor, patch): `1.10.0 > 1.9.0`.
- A **range** is one to four comparators, all of which must hold, joined by commas: `>=1.2.0,<2.0.0`. Operators: `>=`, `>`, `<=`,
  `<`, `==`. There is no `^`, `~`, wildcard, `||` or hyphen form.
- A range is normalized to its tightest interval and printed canonically (equal ranges have equal text, and equal fingerprints). A
  range nothing can satisfy is refused (`empty_range`). At most 64 characters.

### 4.2 Core API compatibility

`CORE_API_VERSION` is `1.0.0`: the version of the module-facing Core interface. A manifest states the versions it supports in
`core_api_compatibility` (for example `>=1.0.0,<2.0.0`) and the registry refuses a module whose range does not contain the Core's
version (`incompatible_core_api`). A range without an upper bound is refused at the manifest level, so a module cannot claim to
work with every future Core. Rule of thumb for Core releases: a breaking change to the module-facing interface raises the major
version. What belongs to "Core API v1" has not been enumerated yet (open decision, section 11).

### 4.3 Module versions and dependencies

Each module has its own version, independent of the Core's and of other modules'. A dependency names another module and a range
of its versions; the registry checks that the dependency is registered (`missing_dependency`) and its version is inside the range
(`incompatible_dependency_version`), and rejects cycles (`dependency_cycle`; a self-dependency is refused earlier, by the manifest).

### 4.4 Update metadata (for a future update manager)

| Field | Meaning | Checked now |
| --- | --- | --- |
| `core_api_compatibility` | Which Core API versions the module runs on | yes: the registry |
| `module_version` | The module's own version | yes: format and dependency ranges |
| `dependencies[].version_range` | Which versions of other modules it accepts | yes |
| `update.data_schema_version` | Version of the module's own stored data, for a migration preflight | format only |
| `workflows[].definition_version` | Version of a workflow definition, so runs can stay pinned to the version they started on | format only |
| `update.minimum_updater_protocol` | Lowest update-manager protocol that can install or upgrade the module | yes: refused if above `UPDATER_PROTOCOL_VERSION` |

`UPDATER_PROTOCOL_VERSION` is `0`: **no update manager exists**, so a manifest that demands protocol 1 or higher is refused
(`unsupported_updater_protocol`) rather than silently assumed to be satisfiable.

## 5. The registry

`apps.modules.registry.ModuleRegistry.build(manifests)` takes manifest **objects** (a path, a name, a class or a dict is refused,
`invalid_manifest_object`), validates them against each other and this Core, and returns a read-only registry. It imports,
scans and executes nothing: it cannot find a module that was not handed to it.

### 5.1 Guarantees (each is tested)

- **Immutable.** No attribute can be set or deleted, there is no instance dictionary, collections are tuples or read-only
  mappings, and changing the list passed to `build` afterwards changes nothing.
- **Deterministic.** The result depends only on the manifests' content: not on input order, dict or set ordering, or the process
  (tested across five `PYTHONHASHSEED` values in separate processes). Order is topological, a dependency before its dependents;
  modules that are ready together go in module-id order.
- **No silent overwriting.** Two modules claiming the same module id, task kind, permission, audit action, workflow key, API route,
  UI contribution id, UI route path, provided capability or namespace is an error naming both modules, never "last one wins".
- **Explicit, exclusive namespace ownership.** Each namespace has exactly one owner, ownership is never inherited, and the result is
  exposed read-only as `namespace_owners` (section 3.2).
- **Strict direct capability dependencies.** A required capability must come from Core or from a direct dependency (section 3.4).
- **Fail closed.** Any problem raises `RegistryError` with a stable code and builds nothing.
- **Stable fingerprint.** `fingerprint` is the SHA-256 of the canonical JSON of the ordered manifests (`registry_format` 1;
  collections sorted, ranges normalized, handler objects reduced to a flag). It is meant to let the API process and the worker confirm
  they run the same modules, because the two are separate processes. It does not include the Core API version.

### 5.2 Check order

The first problem found is raised, so the error for a given input is deterministic: input types; duplicate module ids;
compatibility with the Core API and updater protocol; dependencies exist and are in range; no cycle; clashes between identifiers
(most specific code first); namespace uniqueness; capability requirements; UI permission references. Namespace uniqueness comes
after the identifier clashes so a clash names the identifier that collided.

### 5.3 Error codes

Every error has a stable `code` (listed in `apps/modules/errors.py`; a test checks that every declared code is raised somewhere and
every raised code is declared) and a message that never echoes a submitted value. Groups: version (`invalid_version`,
`invalid_range`, `empty_range`, `too_many_comparators`); manifest (identifier, namespace, handler, configuration, update-metadata,
`duplicate_*` within one module); registry (`duplicate_module_id`, `duplicate_namespace`, `duplicate_ui_route`,
`incompatible_core_api`, `unsupported_updater_protocol`, `missing_dependency`, `incompatible_dependency_version`,
`dependency_cycle`, `missing_capability`, `capability_not_yet_available`, `capability_provider_not_dependency`,
`undeclared_ui_permission`, `invalid_manifest_object`, `unknown_module`, `invalid_core_grants`, `invalid_namespace_history`,
`namespace_released`, `namespace_ownership_changed`). S1a added the manifest codes `unknown_role`, `duplicate_role_grant`,
`empty_role_grant`, `role_grant_undeclared_permission` and `duplicate_maintenance_hook`.

### 5.4 The composition root

`config/modules.py` holds `BUILTIN_MODULES`, a static tuple of manifest objects approved to be part of a Core release, and
`build_registry()`. A module is built in only if its manifest is written there in code, in a reviewed change: no directory scanning,
no setting with dotted paths, no entry points, no import by name. In MOD-1a the tuple is empty. Because a future module's manifest
will live in the module's own package, which imports Django models, the exact loading mechanism (a function that imports lazily,
after Django's app registry is ready) is decided in MOD-2.

Since S3 there is a second, separate composition root, `config/audit_vocabulary.py` (section 3.10). It reads a registry (the built-in one by
default) and builds the audit vocabulary; it names no module itself, and nothing in the application calls it yet.

Since S4 `config/modules.py` is read by exactly two kinds of caller: the worker's composition root (`config/task_registry.py`, which builds the kind gate from
it) and the four administrator commands of `apps/module_host` (section 3.11). No view, serializer, settings module or other entry point calls
`build_registry` (`tests/test_module_boundaries.py` lists the callers).

### 5.5 Direct dependency inspection

`registry.dependencies` and `registry.dependents` are read-only mappings (`module id -> sorted tuple of ids`) of the **direct** edges,
and `dependencies_of(id)` and `dependents_of(id)` answer for one module; an unknown module is `unknown_module`, never an empty answer.
There is deliberately **no transitive query**, and no capability is inherited through a chain (the strict direct-provider rule,
section 3.4). A future disable operation uses `dependents` to refuse disabling a module that an enabled module depends on.

### 5.6 Namespace continuity across releases

`apps/modules/continuity.py` holds the rule as pure code (owner decision: ownership is preserved across releases). Given the earlier
claims as a plain mapping `namespace -> module id`, `check_namespace_continuity(history, registry)` refuses a namespace that now
belongs to a different module (`namespace_ownership_changed`) and one that nobody claims any more (`namespace_released`, because its
identifiers may still be stored); a module may add namespaces. `next_namespace_history(history, registry)` returns the history to
keep. Since S4 the history is stored (`ModuleNamespaceClaim`, append-only) and `check_namespace_continuity` runs inside every `module_sync`, before anything
is written (section 3.11); `next_namespace_history` is not called (the sync inserts only the claims that are missing). What is still missing is the
update path that would run the check before a NEW release is activated.

## 6. Compatibility rules (summary)

A set of modules is accepted only if **all** of these hold: every manifest is valid; module ids are unique; every module's
`core_api_compatibility` contains the Core API version; no module needs an update protocol the Core lacks; every dependency is
registered and inside its version range; there is no dependency cycle; no identifier is claimed twice; namespaces are unique and none
is reserved; every required capability is Core-provided or provided by a **direct** dependency; every UI permission reference is
defined by the same module or by Core.

## 7. Trust boundaries

**A module admitted to a Python process is not sandboxed.** In-process code can read the process environment (including the database
password), open database connections, import any Core internals and read files. Nothing in this contract changes that. Therefore:

| What MOD-1a does | What it does not do |
| --- | --- |
| Refuses manifests that name import paths or commands; only objects handed in by the composition root count | Prevent code that is already in the process from doing anything |
| Keeps identifier namespaces disjoint and Core's reserved | Enforce permissions, capabilities, document access or secret access at run time |
| Detects registry differences between processes (fingerprint) | Verify who wrote a module, or that it was not tampered with (no signatures) |
| Makes the set of built-in modules a reviewed, static tuple | Isolate a module's failures or resources from the worker or API |

Declared permissions and capabilities are **declarations** for validation, UI hints and review; they are enforcement only where Core
services check them (as the existing review services do for Request Intake). Real isolation needs a separate process (kind
`service`) and is future work. Customer documents, secrets, local-only AI policy and license enforcement are not touched by this
slice; the product rules still apply to anything built later (`CLAUDE.md`: local-only mode never silently falls back to a cloud
provider; significant external actions need human approval; AI output is validated).

## 8. Future module lifecycle (design intent, not implemented)

| Stage | Built-in module (ships in the Core release) | Signed installable package (future) | External service module (future) |
| --- | --- | --- | --- |
| Discover | The static tuple | A managed install directory only; never `PYTHONPATH` or arbitrary folders | A declared endpoint |
| Install | Implicit: it is part of the release; `migrate` creates its tables | Verify, then extract into the managed directory | Register an endpoint and a credential |
| Configure | Validated against `configuration_schema`; saved as an immutable, versioned configuration ([ADR 0005](../decisions/0005-company-configuration-not-forks.md)) | Same | Same |
| Enable | Compatibility checks, seed permissions, write an audit event | Same plus signature and license checks | Same plus a health check |
| Disable | Stop new work for the module; keep data and in-flight runs readable | Same | Same |
| Upgrade | New version beside the old; runs stay pinned to their workflow definition version; migrations forward-only | Same, with rollback metadata | Version handshake |
| Uninstall | Not possible for built-ins (disable only) | Disable, then data kept by default; purging is an explicit audited action | Remove the endpoint |

**Owner decisions (MOD-1b audit), still design intent, none built:**

- **Disabling is read-only mode.** New writes through the module are blocked; queued tasks of the module are **held** (not failed);
  attempts already running are allowed to finish; historical records stay readable. A hold needs the worker's claim step to take
  only kinds that are enabled, version-matched and known to the worker, and maintenance hooks declared `while_disabled=True` keep
  running. S2 built the worker's side of this as generic seams (a claim filter, a kind gate and ordered maintenance hooks; section 3.9); S1a did
  not touch the worker. **S4 built the gate that reads module state and installed it (section 3.11)**; the use of `while_disabled` is still not built
  (no module declares a hook that the worker runs), and "new writes through the module are blocked" is not enforced anywhere yet (there is no module
  write path; the held-tasks part is).
- **Runtime state lives in a separate `apps/module_host` Django app** (built in S4: `ModuleState`, namespace claims, sync/enable/disable
  commands). `apps/modules` stays pure and gains no models. S1a only prepares the pure inputs that app will use: the registry,
  `dependents_of` (refuse to disable a module an enabled one needs), `derive_role_permissions` and the namespace continuity check.
- **Roles.** The existing Operator, Approver and Auditor groups stay; modules supply default grants to them and add no role.
- **Updates.** A module release may change its permissions, hooks and capabilities, but never the owner of a namespace
  (section 5.6). A Core release that adds a capability only adds to `CORE_CAPABILITIES`; a manifest that needs one the running Core
  lacks fails with `missing_capability`, which is how a module update can be refused cleanly.

How API and worker agree on the enabled set remains open (the registry fingerprint is the proposed comparison).

## 9. Future Update Manager requirements

An update manager does not exist and is **not** part of MOD-1a. When it is designed, it must at least:

- **Namespace continuity.** Refuse any release in which a namespace (a module id or an `extra_namespace`) that an earlier release of
  another module id owned is claimed by a different module id, and refuse a release that drops a namespace whose identifiers are
  still stored (permissions, audit actions, task kinds, workflow keys). A single registry cannot see across releases, so this check
  belongs to the update path (owner decision on `extra_namespaces`, requirement 4). The pure rule exists since S1a
  (`apps/modules/continuity.py`, section 5.6); storing the history and calling the check remain to be built.
- **Permission and capability changes.** Compare a release's role grants and required capabilities with the installed ones, show the
  difference to the administrator before activation, and never apply a changed default grant to existing groups silently: grants
  are seeds, and an administrator's later changes to a group are not overwritten.

- **Signed packages.** Verify a vendor signature, against a pinned key, before reading anything else in a package; refuse downgrades
  and revoked versions; protect against archive path traversal; never execute package content during verification. The signature
  scheme and any cryptography dependency are undecided (the standard library has no Ed25519).
- **Online updates.** Fetch only from a configured, authenticated source; verify before storing; never auto-apply; respect
  `local_only` and company network policy; work with no vendor portal (the product runs without one,
  [ADR 0006](../decisions/0006-local-execution-independent-of-control-plane.md)).
- **Offline updates.** Accept a package from removable media or a file share with the same verification; record where it came from.
- **Migration preflight.** Before activation: compare `update.data_schema_version`, list the migrations that would run, check free
  disk space and the database server version, confirm no run is mid-flight in a way the migration cannot handle, and refuse if the
  package's `core_api_compatibility` or `minimum_updater_protocol` do not fit.
- **Backup and recovery.** Take or require a verified backup (database and managed storage) before any migration; document restore;
  migrations are forward-only, so rollback means restore.
- **Safe activation and rollback.** Stage the new version beside the old; run the registry compatibility checks and a health check;
  activate atomically for the API and the worker together (check the fingerprint); keep the previous version until the new one has
  been confirmed; on failure, stop and restore rather than run mixed versions.
- **Audit.** Every install, enable, disable, upgrade and rollback is an audit event with the actor and the versions involved.

## 10. Limitations and deferred functionality

- No module is registered; `BUILTIN_MODULES` is empty. Since S4 the registry is read by the worker's composition root (the kind gate) and by the four
  `apps/module_host` commands only (section 5.4); `apps/modules` itself still has no model and no migration.
- Module state exists since S4 (section 3.11: `apps/module_host`, `ModuleState`, namespace claims, sync, enable and disable commands), but no module is activated
  by it: there is nothing to activate until MOD-2.
- No handler registration with the worker, no permission seeding, no API or UI mounting, no endpoint listing modules.
- Run states and permission constants in Core are still shaped by Request Intake (`apps/cases/run_state.py`, `apps/accounts/roles.py`); the
  contract does not change them. The audit vocabulary is extensible since S3 (section 3.10): Core's seven actions stay Request-Intake-shaped
  and fixed, and modules add their own.
- No configuration storage or value validation (only schema declarations), no secrets store, no document storage, no AI gateway.
- No license enforcement, no signatures, no package format, no installer, no update manager, no marketplace.
- No sandbox; see section 7.
- Capability and permission declarations are not enforced at run time. Permission scopes are declarations; the default scope is `any`.
- Default role grants are not wired: `setup_roles`, `/api/auth/me/` and `apps/accounts/profile.py` still use `apps/accounts/roles.py`,
  and nothing calls `derive_role_permissions`. Roles are fixed to the existing three.
- Core's access rule (`apps/accounts/access.py`, section 3.8) is used only by Request Intake's own table. Nothing builds `AccessRule`
  objects from manifest permission scopes, no module can use it, and the generic "read actor, decide, log, refuse" sequence is still
  Request Intake's glue (`apps/intake/authorization.py`), to be generalized behind the `permissions.check` facade (S6). One characteristic
  carried over unchanged: `access_problem` evaluates `permissions` twice, so a one-shot iterator (a generator) would be exhausted by the
  first part; every caller passes a `frozenset` and the tests use re-iterable collections only.
- The behaviour-preservation tests for S1b are DB-free in the VM. The PostgreSQL comparison (`tests/test_authz_core_postgres.py`) and the
  existing review and API suites passed in the owner's local run (242 tests, owner-supplied, section 3.8); the VM never ran them.
- Still absent after S1b: `ModuleState`, runtime module permissions (no module permission is checked at run time; no manifest becomes
  an `AccessRule`), enable/disable, and the capability facade. Request Intake is not a module.
- Maintenance hooks in a manifest are declarations: nothing reads them. Since S2 `apps/tasks/worker.py` can run further named hooks after
  the legacy `after_iteration` hook (section 3.9), but the production composition installs none, `while_disabled` is not consulted (S4 did not
  change that), and a manifest hook id is not resolved to code anywhere.
- The worker's claim filter and kind gate (S2, section 3.9) are **installed since S4** (section 3.11), but no module is registered, so the gate answers
  the legacy kinds and holds nothing today. The S2 PostgreSQL tests passed in the owner's local run (section 3.9) for the tested scenarios
  only; the gate race window remains.
- S4 (section 3.11) adds `apps/module_host`, `ModuleState`, the sync and enable/disable administrator commands and the persisted namespace history
  (the continuity check runs in every sync). **Its PostgreSQL tests and migration have NOT been run by anybody**; the limitations of S4 are listed in
  section 3.11, "Remaining limitations".
- No capability facade exists (`task.enqueue` and the others are names a manifest may require, nothing a module can call).
- Request Intake has no manifest in the application; the synthetic one in `tests/module_support.py` exists only for the golden test.
- Only manifest version 1; no pre-release versions.
- **S3 (audit vocabulary), merged into `main` (PR #14):** no module is registered and nothing installs a module vocabulary in the application (no entry point,
  `ready()` or command calls `install_audit_vocabulary`), so no module action can be recorded in any running process yet. The database checks the
  shape of an action only; membership is Python's rule, bypassable by raw SQL, a migration or a database administrator. A module action stored in
  the table prevents reversing migration `0002` (by design: events are never deleted). The migration and its PostgreSQL behaviour passed the
  24 PostgreSQL tests and a 342-test regression in the owner's local run (owner-supplied, section 3.10), for the tested scenarios on one machine
  only. Owner decision: installing a module vocabulary in a running
  process is deferred to MOD-2. No production-readiness claim.

## 11. Open decisions (for later slices)

1. **Loading mechanism** for module manifests that live in packages importing Django models (lazy import after app loading).
2. **Audit vocabulary:** decided and implemented in S3 (section 3.10, merged, PR #14): the database CHECK is a
   bounded shape pattern, the allowlist is the Python vocabulary composed from the registry. Owner decision: the process-start installation
   call is deferred to MOD-2 (where it belongs is still open), and any database-level protection of the table.
3. **Run-state vocabulary:** keep the Core set fixed with modules mapping onto it, or make it extensible; the name `extracting` is
   intake-shaped.
4. **Run snapshots:** when `WorkflowRun` gains `workflow_key` and `workflow_version`
   ([data-model.md](data-model.md), `WorkflowDefinition`).
5. **Result schema versioning and the content hash** ([open-questions.md](open-questions.md), 2.14), before a second module
   produces revisions.
6. **Permission model for installable modules:** namespaced permissions and the existing three roles are decided (owner,
   MOD-1b); still open is how an installable package without models gets Django permission rows (Django creates them only for a
   model), versus a Core permission registry table. Built-ins keep `Meta.permissions`.
7. **Disabling a module:** decided as read-only mode (section 8); the worker claim filter and kind gate seam exist since S2 (section 3.9; its PostgreSQL
   tests passed in the owner's local run); S4 built the state storage, the gate that reads it and the enable/disable commands (section 3.11; its
   PostgreSQL tests are NOT RUN). Still open: how the API process learns the enabled set (there is no API use yet; the proposal is the same
   `ModuleState` rows plus a registry-fingerprint comparison), blocking new writes through a disabled module, and honouring `while_disabled`.
8. **Trust model:** first-party modules only, third-party in-process Python, or external isolated services.
9. **What "Core API v1" contains** and how it is versioned.
10. **Frontend contributions:** a build-time static registry (proposed), declarative screens, or remote code (not proposed).
11. **The `test.add_numbers` handler** in the production allowlist ([ADR 0003](../decisions/0003-persistent-background-worker.md) context).

## 12. Tests and files

Pure-domain tests (plain `unittest`, no Django, no database): `tests/test_module_manifest.py`, `tests/test_module_registry.py`, and
from S1a `tests/test_module_permissions.py` (scopes, role grants, derivation, golden equivalence with `apps.accounts.roles`),
`tests/test_module_capabilities_hooks.py` (capability contracts, hook declarations, ordering, direct dependency inspection) and
`tests/test_module_continuity.py`. Django-runner static tests: `tests/test_module_boundaries.py` (imports, "not a Django app",
nothing activated, existing worker composition and routes unchanged) and, from S1a, `tests/test_module_golden_django.py` (the
synthetic Request Intake permissions equal the real model's `Meta.permissions`; `setup_roles`, the profile and the authz code do not
reference `apps.modules`). Shared helpers: `tests/module_support.py`. Source:
`backend/apps/modules/{__init__,manifest,versions,registry,errors,roles,continuity}.py` and `backend/config/modules.py`. Existing
Request Intake, worker, API and frontend code is unchanged. All of these were run in the development VM without a database; the
PostgreSQL-backed suite was not run there.

From S1b (section 3.8): pure `tests/test_authz_core_access.py` (36 864-row truth table against the recorded baseline digest, Core
generic behaviour, structure and delegation); Django-runner `tests/test_authz_glue_equivalence.py` (the unchanged glue with a
fabricated actor, refusal precedence, service order); PostgreSQL, `tests/test_authz_core_postgres.py` (owner-run locally, section 3.8; the VM did not run it); helpers
`tests/authz_oracle.py` (the frozen old algorithm) and `tests/authz_truth_table.py`. Source: `backend/apps/accounts/access.py` (new),
`backend/apps/intake/access.py` and a docstring in `backend/apps/accounts/authz.py` (changed).

From S2 (section 3.9): pure and Django-runner `tests/test_tasks_gating.py` (DB-free, passes in the VM); PostgreSQL
`tests/test_tasks_gating_postgres.py` (owner-run locally, section 3.9; the VM did not run it). Source: `backend/apps/tasks/policy.py` (`normalize_allowed_kinds`),
`backend/apps/tasks/services.py` (`claim_next_task`) and `backend/apps/tasks/worker.py` (`MaintenanceHook`, `Composition`, the kind gate) (changed).
`config/task_registry.py` and `runworker` are unchanged.

From S3 (section 3.10): DB-free `tests/test_audit_vocabulary.py` (pure and Django-runner classes; passes in the VM) with the helper
`tests/audit_support.py`; PostgreSQL `tests/test_audit_vocabulary_postgres.py` (24 tests; owner-run locally, section 3.10; the VM did not run them). Source:
`backend/apps/audit/vocabulary.py` (new), `backend/apps/audit/events.py` and `backend/apps/audit/models.py` (changed),
`backend/apps/audit/migrations/0002_extensible_audit_vocabulary.py` (new) and `backend/config/audit_vocabulary.py` (new). `apps/modules`,
`config/modules.py`, the recorder, `apps/tasks` and Request Intake are unchanged.

From S4 (section 3.11): DB-free `tests/test_module_host_rules.py` (pure), `tests/test_module_host_services.py`, `tests/test_module_host_gate.py`,
`tests/test_module_host_cli.py` and `tests/test_module_host_boundaries.py` (198 tests; pass in the VM); PostgreSQL `tests/test_module_host_postgres.py` (73 tests;
**NOT RUN**). Source: `backend/apps/module_host/{apps,models,errors,rules,services,gate,cli}.py`, `backend/apps/module_host/management/commands/module_{sync,status,enable,disable}.py`
and `backend/apps/module_host/migrations/0001_initial.py` (all new), `backend/apps/audit/events.py` (two Core actions added), `backend/config/task_registry.py`
(the gate and `LEGACY_UNGATED_KINDS`) and `backend/config/settings/base.py` (the app). `apps/modules`, `config/modules.py`, `apps/tasks`, the recorder, the existing
migrations and Request Intake are unchanged.
