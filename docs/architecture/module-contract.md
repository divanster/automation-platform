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
> **MOD-1b / S4 (persistent module state, enable/disable, worker gate)** is implemented, validated and **merged into `main` (PR #15, merge commit `97cdc98`)** (implementation commit `96efa2f`;
> three test-only corrections `45bd0da`, `4ba709b` and `0f8e44d`). A new Django app, `apps/module_host`, stores a `ModuleState` row per registered module and an append-only `ModuleNamespaceClaim` per
> namespace (additive migration `0001`), syncs them from the approved static registry (`module_sync`), enables and disables a module transactionally with
> an audit event (`module_enable`, `module_disable`), and reports status (`module_status`) (section 3.11). The existing S2 `kind_gate` is now installed in the
> production worker composition: it holds the queued tasks of a disabled, version-mismatched or stateless (after the first sync) module and lets
> **legacy kinds, Request Intake's `request_intake.extract` included, through unchanged**. **`BUILTIN_MODULES` is still EMPTY, Request Intake is not a
> module (that is MOD-2), no module is gated in practice, and there is no public enable/disable API.** The DB-free tests pass in the development VM
> (1926 in all; 198 of them in the five module host files). **Owner-supplied local PostgreSQL validation (the VM has no PostgreSQL and did not run, repeat or observe it;
> no output is stored): the 75 module host PostgreSQL tests, the 15 process-level recovery tests and the 25 audit vocabulary tests passed, migration
> `module_host.0001_initial` applied successfully, and the full selected PostgreSQL regression ran 567 tests, all passed, 153.186 s, exit code 0, test database
> destroyed** (section 3.11, "Owner-supplied local PostgreSQL result"). That supports the tested scenarios on one machine only. Two Core audit actions were added
> (`module.enabled`, `module.disabled`), which changes the S3 "seven Core actions" pin (section 3.11, "Audit"). No production-readiness claim.
>
> **MOD-1b / S5 (workflow attribution)** is implemented on `feat/module-workflow-attribution` (implementation commit `e2a4c55`; **owner-supplied local PostgreSQL validation: the migrations applied, four historical runs were attributed, the 37 attribution tests and a 604-test regression passed; the VM did not run or observe it**). Every `WorkflowRun` now records a `module_id`, a
> `workflow_key` and a `workflow_version` (the workflow definition version), written once by `create_run` and refused afterwards at the ORM level; existing Request Intake runs are attributed to
> `request_intake` / `request_intake` / 1 by an evidence-based, idempotent backfill that **stops, with a count, at any run it cannot attribute** (section 3.12). It changes no state transition, task, lease, approval,
> audit event or canonical byte, exposes nothing through the REST API, and activates no module. Request Intake is still not a module. No production-readiness claim.
>
> **MOD-1b / S6 (the ModuleContext capability facade)** is implemented on `feat/module-capability-facade` (committed locally, not yet pushed; **PostgreSQL tests NOT RUN**). `apps/module_host/context.py` gives a registered module one controlled door to Core
> (`ModuleContext.open(registry, module_id)`): immutable identity, and `tasks.enqueue`, `cases.create_case` / `create_run` / `transition_run`, `audit.record` and `permissions.require` / `authorize`, each only if the manifest declares the capability,
> each delegating to the existing Core service unchanged after checking the module's own declarations, ownership and (for the state-changing calls) its `ModuleState` under a shared row lock (section 3.13). No queue, worker, state-machine,
> audit or authorization code changed, nothing uses it outside tests, Request Intake is still legacy, and it is a naming and authority boundary, not a sandbox. No production-readiness claim.

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
| `workflows` | no | `WorkflowDeclaration(workflow_key, definition_version, task_kinds, description)`; `definition_version` an integer from 1 to 2147483647 (a positive 32-bit integer, since S5); `business` modules only; the task kinds must be declared by the same module |
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

Capabilities are declarations used for validation and ordering. MOD-1a enforced nothing at run time; since S6 four of the five Core capabilities have a facade, the `ModuleContext` (section 3.13), which lets a module use a capability only if its manifest declares it.

**The five Core capabilities and what each means** (the contract names them; S6 added a thin facade for four of them, `ModuleContext`, section 3.13, that delegates
to the existing services unchanged; `revision.approval` still has no facade; nothing outside tests calls a facade yet, because no module is registered):

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
`permissions.check` facade (built in S6, section 3.13: the rule is built per call from the declared scope; the golden comparison with Request Intake's table is a PostgreSQL test over every user and owner, not run in the VM); a disabled
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

**Status.** Implemented and validated, merged into `main` (PR #15, merge commit `97cdc98`). DB-free tests pass in the development VM; the PostgreSQL tests
and the migration were run **by the project owner locally** (section "Owner-supplied local PostgreSQL result" below), not by the VM. `apps/modules` stays pure and gains no model; the runtime state is a separate Django app, `apps/module_host`
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
migration was needed (the database already accepted the `module` subject type and any well-formed action, section 3.10). **Rollback consequence:** `module.enabled` and `module.disabled` are not in the old fixed action list, so once the sync or an enable/disable has stored one, reversing audit migration `0002` is refused (section 3.10, "Rollback limits"); events are never deleted to allow it. The PostgreSQL migration tests therefore build their old-schema rows from a frozen list of the seven original actions, not from the live `events.ACTIONS`.

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

* No module is registered; nothing is gated in practice. The gate, the sync and the enable/disable services have been exercised by DB-free tests with the
  database replaced and by the PostgreSQL tests the owner ran locally (tested scenarios only; no real module has ever been registered).
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
and others) was caught by the focused set. PostgreSQL (`tests/test_module_host_postgres.py`, **75 tests; passed in the owner's local run, below**): the stored constraints, the migration
(applied, reversed and re-applied without touching other tables or rows), sync, namespace ownership that persists and cannot change, enable and disable
against real rows, audit consistency, rollback when the audit write fails, concurrent syncs, enables and disables (including the dependent/dependency race), and
the worker on committed data (tasks held, resumed, a running attempt finishing, recovery, retry, version mismatch, missing state, Request Intake end to end with
the production composition). The unchanged `test_intake_postgres`, `test_api_postgres`, `test_tasks_*_postgres` and `test_audit_vocabulary_postgres` suites are
the regression. **Commands: [runbook.md](../development/runbook.md), section 5.1.**

Three test-only corrections followed the owner's first runs, each in a test or its harness and none in production code, a model or a migration: `45bd0da` (a 41-character
id overflows `varchar(40)` and PostgreSQL raises `DataError`, not the `IntegrityError` of a CHECK; the over-length case is asserted separately and two boundary tests were
added, 73 to 75 tests), `4ba709b` (the process-level recovery children run the real `runworker`, so they run the production kind gate, which correctly held their
`recovery.*` test kinds; the child now adds exactly those three kinds to the real gate's answer, and eight DB-free tests pin that it still fails closed and still holds
every other kind) and `0f8e44d` (the audit migration tests built their audit `0001` rows from the live `events.ACTIONS`, which S4 extended with two actions the old schema
refuses; they now use a frozen list of the seven original actions, four DB-free tests pin it to the migrations, and a new PostgreSQL test covers the reverse refusal
after a `module.enabled` event is stored, 24 to 25 audit tests).

**Owner-supplied local PostgreSQL result (2026-11-18; the VM did not run, repeat or observe it; no output is stored).** The project owner ran, on the local Windows
computer, the 75 module host tests, the 15 process-level recovery tests and the 25 audit vocabulary tests, applied `module_host.0001_initial`, and ran the full
selected PostgreSQL regression: **567 tests passed, 153.186 s, exit code 0, and the test database was destroyed successfully.** The command line, the module list and the
output were not supplied (the intended list is runbook section 5.1). This supports, **for the tested scenarios only**:

* the migration applying, and the stored constraints and the one-row marker behaving as the tests expect;
* the initial sync, the marker written in the sync's transaction (an empty registry included), missing state failing closed once the marker exists, and namespace
  ownership persisting and not being reassigned through the services;
* idempotent, locked, audited enable and disable, including the concurrent sync, enable and disable scenarios and the rollback when the audit write fails;
* the production worker composition with its gate: tasks of a disabled, version-mismatched or stateless module held in `queued` and not failed, resumed after an enable, a
  running attempt finishing, and recovery and retry unchanged;
* **Request Intake's legacy task kind staying ungated**: the extraction runs end to end through the production composition with no module state, with unrelated modules
  disabled and with every state row gone but the marker present, and the unchanged `test_intake_postgres` and `test_api_postgres` suites are part of the intended selection (the exact module list of the 567-test run was not supplied);
* the process-level recovery tests (real worker processes, a forced kill, shutdown with a task in progress) passing with the production gate in the loop.

**What it does not show, and what remains.** It does not prove that no other race exists (the S2 gate race window remains; the threaded tests cover the scenarios written),
it is one machine and one PostgreSQL, and the owner did not report a manual run of `module_sync`, `module_status`, `module_enable` or `module_disable` against a database
(they are exercised by the tests through the services and, DB-free, through the commands). Request Intake is **not** registered as a module; there is **no** API-side
write blocking for a disabled module; there is **no** capability facade and no `ModuleContext`; there is **no** module installation and no Update Manager; the gate reads
state once per iteration; and nothing here is a production-readiness claim. Record: [local-validation-record.md](../development/local-validation-record.md).

### 3.12 Workflow attribution: which module, workflow and definition version a run belongs to (MOD-1b / S5)

**Status.** Implemented and validated on `feat/module-workflow-attribution` (implementation commit `e2a4c55`, pushed; PR not yet merged). DB-free tests pass in the development VM (1998 in all). **The PostgreSQL tests and the four
migrations were run by the project owner locally** (section "Owner-supplied local PostgreSQL result" below); the VM has no PostgreSQL and never connects to one. The owner's commands are in the [runbook](../development/runbook.md), section 5.2. **No module is registered, Request Intake is still not a module, and nothing
is enabled or activated by this slice.** Nothing about a run's behaviour reads the new columns.

**Goal.** Every `WorkflowRun` records, for good, the stable module identity, the workflow key and the workflow definition version it was created under, so that an update or an audit can later say which definition a run
started on (the "runs stay pinned to their workflow definition version" intent of sections 4.4 and 8, and open decision 4 of section 11).

**Schema (`cases.WorkflowRun`, three columns; migrations `cases.0006` to `0009`).**

| Column | Type and rule | Manifest field it mirrors |
| --- | --- | --- |
| `module_id` | `varchar(40)` NOT NULL; CHECK `^[a-z][a-z0-9_]{2,39}$` (`cases_run_module_id_shape`) | `module_id` |
| `workflow_key` | `varchar(64)` NOT NULL; CHECK `^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})*$` (`cases_run_workflow_key_shape`) | `WorkflowDeclaration.workflow_key` |
| `workflow_version` | `integer` NOT NULL; CHECK `1 <= value <= 2147483647` (`cases_run_workflow_version_positive`; Django's own `>= 0` check stays) | `WorkflowDeclaration.definition_version` |

The column is called `workflow_version` (the name the contract and the data model already used) and holds the manifest's integer `definition_version`. It is the **workflow definition's** version, not the module's SemVer
`module_version`: a run does not record which module release created it. There is no foreign key and no index: `module_id` is plain text (a module keeps its `ModuleState` row for ever, but nothing ties the two), and no
query reads these columns yet.

**Attribution rules (`apps/cases/attribution.py`, pure, standard library only).** `WorkflowAttribution(module_id, workflow_key, workflow_version)` is a frozen value that validates itself on construction and raises
`WorkflowAttributionError` with a stable `code` (`invalid_type`, `invalid_module_id`, `invalid_workflow_key`, `reserved_namespace`, `invalid_workflow_version`); a message names the field and never repeats the value.

* The **manifest's rules, written twice.** `apps.cases` is Core and may not import `apps.modules` (a boundary test pins it), so the module id pattern and length, the workflow key pattern and length (64) and the reserved
  namespaces are repeated, as the audit vocabulary repeats the detail-key rule. `tests/test_workflow_attribution_pure.py` keeps the copies equal: the reserved set equals `RESERVED_NAMESPACES`, and on a seeded corpus of
  about 2 500 generated and edge strings a module id is accepted exactly when a real `ModuleManifest` accepts it, and a workflow key exactly when a `WorkflowDeclaration` accepts it and its first segment is not reserved
  (no module can own a reserved namespace, so such a key can never be declared).
* **One version limit everywhere (owner decision: a positive 32-bit integer).** `WorkflowDeclaration.definition_version` must be an integer from 1 to 2 147 483 647 (`MAX_WORKFLOW_DEFINITION_VERSION` in
  `apps/modules/manifest.py`; a larger one is refused with code `invalid_type`, like any other bad version; no existing manifest or fingerprint is affected). `WorkflowAttribution` (`invalid_workflow_version`), the `integer`
  column and the CHECK `cases_run_workflow_version_positive` use the same range, and tests compare the four constants (manifest, attribution, model constraint, migrations 0008 and 0009). The first S5 draft left the
  manifest unbounded; the review closed that gap, so a manifest that registers can always create a run.
* **No arbitrary values.** The values come from a typed, validated object. `create_run` accepts only exactly a `WorkflowAttribution` (a subclass is refused: it could change how its values are read) and validates it
  again (so one altered after construction through `object.__setattr__` is refused); the database checks the shape; the one creator passes a constant (below). No request, command line, file or environment variable
  reaches these values, and there is no import by name and no discovery.
* **What Core cannot check.** Whether a module is registered or enabled, and whether a module really declares a workflow key or that definition version, is the registry's knowledge. Core does not have it (Request
  Intake is not a module yet), so the database and `create_run` check **shape and reserved names only**. A well-formed but unregistered pair is accepted (a PostgreSQL test pins that, so nobody claims more). When MOD-2
  registers Request Intake, a golden test must show that its manifest declares exactly the constants below (the DB-free test already does so against the synthetic manifest).

**Request Intake's identity.** `apps/intake/policy.py`: `MODULE_ID = "request_intake"`, `WORKFLOW_KEY = "request_intake"`, `WORKFLOW_VERSION = 1`, and `apps/intake/submission.py` builds the constant
`REQUEST_INTAKE_ATTRIBUTION` from them. They are the identifiers the synthetic Request Intake manifest (`tests/module_support.py`) already declares (module id, workflow key, definition version 1) and the prefix of the task
kind `request_intake.extract`, so MOD-2 can register the module without changing a stored value (a test keeps them equal and checks that the registry gives the module its own workflow key namespace). The workflow
version is the workflow's own and is unrelated to the canonical format version of its revisions (still 1).

**Every creation path (found by reading the code, and pinned by an AST test).** A run is created in exactly one production place: `apps.intake.submission.submit_request` calls `apps.cases.services.create_run`, which is
the only call of `WorkflowRun.objects.create`. `submit_request_as`, the REST API and the UI all go through it. A replayed submission (same client request id) returns the existing run and creates nothing, so its
attribution is the original one; a lost race reads the winner's row. No management command, signal, fixture, admin class or other service creates runs; every other call in the repository is a test. `create_run` now
takes **`attribution` as a required keyword with no default** (a default would have attributed unknown workflows to Request Intake silently), so a future creation path cannot forget it. The existing test callers pass
`REQUEST_INTAKE_ATTRIBUTION` (Request Intake tests) or a neutral `DEMO_ATTRIBUTION` (`tests/attribution_support.py`, Core persistence tests).

**Immutability (ORM and service level only, like the immutable records of `apps/cases/models.py`).** The attribution is written once, by `create_run`, and then refused: `save()` and `save_base()` on an existing row refuse
a changed value, and refuse any `update_fields` that names an attribution column (even with an unchanged value); a new object is always INSERTed, so an object that reuses an existing id cannot overwrite the row
(`loaddata` included); `QuerySet.update()`, `bulk_update()` and `bulk_create(update_conflicts=True)` on the default manager refuse the three columns. The stored value is remembered when a row is loaded or inserted, and a
partial `refresh_from_db(fields=...)` does not launder a value changed in memory. A run loaded without the attribution columns (`.only("state")`) can still be saved: Django then writes only the loaded columns. The error is
`ImmutableAttributionError`, a subclass of `ImmutableRecordError`. Everything else on a run stays updatable exactly as before (`transition_run`, `record_revision`). **Not guarded:** raw SQL and `connection.cursor()`,
`QuerySet.raw()`, private ORM internals, the `_base_manager`, a database administrator and a migration. **There is no trigger and no revoked privilege**: a PostgreSQL test pins that a raw `UPDATE` changes a value.

**Migrations (additive, in four steps; data and schema never share a migration).**

| Migration | Kind | What it does |
| --- | --- | --- |
| `0006_workflowrun_attribution_columns` | schema | adds the three columns, NULLABLE, no default; nothing else changes |
| `0007_backfill_run_attribution` | data | attributes a run to Request Intake **only if it has a `RequestSource` row** and all three columns are still NULL |
| `0008_validate_run_attribution` | read only | counts runs with a missing or malformed attribution and **raises** `UnattributedRunsError` (counts only, no identifier) if there is one |
| `0009_workflowrun_attribution_constraints` | schema | `ALTER COLUMN ... SET NOT NULL` on the three columns and the three CHECK constraints |

* **The evidence.** Before S5 the only creation path was `submit_request`, which creates the run and its one `RequestSource` row in one transaction. A run with a source row is a Request Intake run. A run **without**
  one (a run created by the persistence slice's tests or by hand, as the note in migration `0003` says) is **not guessed at**: it stays unattributed and `0008` stops. This is the only way the migration can refuse; it never
  deletes, never defaults and never attributes an unknown workflow to Request Intake. Migration `0007` depends on `intake.0001_initial` (the table that is the evidence); a migration cannot import application code, so
  its three values are written in the file and a test keeps them equal to `apps/intake/policy.py`.
* **Idempotent.** Only a run whose three columns are all NULL is touched, so running `0007` again, or after a manual fix, overwrites nothing (a PostgreSQL test pins it, including a run that already carries another
  attribution). A partly filled row is neither completed nor overwritten: `0008` reports it.
* **Preserved.** Run ids, states, failed steps, timestamps, cases and current-revision pointers; every revision (canonical bytes, hash, findings), approval, rejection, source, step task, queue task and audit event.
  No canonical bytes or hash is recomputed (a DB-free golden test records the hash of the shared valid text, computed on `main` before this slice, 1113 bytes, and the PostgreSQL end-to-end test compares against it).
* **If a run cannot be attributed.** `migrate` stops at `0008` with the count; steps `0006` and `0007` have committed on their own (they are harmless and repeatable), `0008` and `0009` are not applied, nothing
  is deleted and no constraint exists yet. The owner decides what each such run is (runbook, "Workflow attribution", gives a read-only query to list them), attributes it explicitly, and runs `migrate` again.
* **Reverse.** Reversing `0009` removes the constraints and makes the columns nullable; `0008` and `0007` do nothing in reverse; reversing `0006` drops the three columns (their values are the only thing lost, and they
  can be derived again by `0007` for Request Intake runs). No existing migration was modified, and no run, revision or other row is touched in either direction.
* **Lock.** `SET NOT NULL` and the CHECKs scan `cases_workflowrun` under an exclusive lock, once; fine for today's volume. Stop the worker and API during the upgrade as for any migration.
* The DDL was previewed in the VM without a database (`schema_editor(collect_sql=True)`): three `ADD COLUMN`s, then three `SET NOT NULL` and three `ADD CONSTRAINT ... CHECK`. A DB-free test runs Django's autodetector against
  the migration graph and finds no change.

**Historical compatibility.** Existing Request Intake runs keep their meaning: they read back as module `request_intake`, workflow `request_intake`, version 1, with the same state, history and links. The REST API,
`apps/intake/queries.py` and the frontend **do not expose** the new fields (a test fails if the words appear there), and Request Intake's own queries still select its runs by the existence of a source row, not by
attribution. State transitions, the run-state table, task claiming, leases and recovery, approval rules, the audit vocabulary and the canonical format are untouched (an AST test fails if the worker, the audit app, the
workflows, the transition and approval services or the run-state module mention the attribution).

**Update Manager implications (design intent; nothing here is an update manager).**

* A release may add a new `workflow_version` for the same key; runs already created keep the version they started on, and new ones get the new one. The columns are what a preflight needs for "which runs are mid-flight
  on module X, workflow W, version N" (join with `state`); an index is not added until that query exists and its need is shown.
* A stored `module_id` and `workflow_key` are now persisted history, as namespaces already are (section 9, "Namespace continuity"): a release must not hand a stored workflow key to another module id, nor drop a workflow key
  or definition version that non-terminal runs still carry without an explicit, audited decision. The continuity check does not read runs yet.
* The module's own version at creation is deliberately not recorded (owner decision); if an audit ever needs "which release created this run", that is a later decision.

**Owner-supplied local PostgreSQL result (2026-11-18; the VM did not run, repeat or observe it; no output is stored).** The project owner took a PostgreSQL backup, applied `cases.0006` through `cases.0009` to the development
database, and reported that **four historical `WorkflowRun` records were backfilled to `request_intake` / `request_intake` / 1**, that the **37 focused PostgreSQL attribution tests passed**, and that the full selected PostgreSQL
regression ran **604 tests, all passed, 207.855 s, exit code 0, test database destroyed** (the command line, the module list and the output were not supplied; for orientation only, 604 is the 37 plus the 567 of the S4
selection, the VM's arithmetic). This supports, **for the tested scenarios only**: the migrations applying to a database that held runs; the evidence-based backfill attributing the four existing Request Intake runs; the
constraints, the ORM guard, a new run's attribution and concurrent creation as the tests expect; and Request Intake submission, extraction, review, approval, rejection, the worker and the audit history behaving as before with
the attribution in place (the suites that create runs were edited only to pass an attribution). It does not show how the four runs were inspected, whether the migration ever stopped at `0008` (it completed), any manual
smoke test, behaviour with many runs, another machine or PostgreSQL version, or that no other race exists. Request Intake is **not** registered as a module, there is no `ModuleContext` or capability facade, no module
installation or Update Manager, and no real AI integration. Not a production-readiness claim. Record: [local-validation-record.md](../development/local-validation-record.md).

**Remaining limitations.**

* Shape is checked, registration is not (above). Nothing verifies that a stored pair belongs to a registered module.
* ORM-level protection only; raw SQL, a migration or an administrator can change a value and nothing detects it.
* An installation holding a run that Request Intake did not create cannot complete the migration until its owner attributes that run (by design).
* The PostgreSQL validation is the owner's, on one machine (below); it covers the tested scenarios only.
* No module activation, and Request Intake is not a module. (`case.run` has a facade since S6, section 3.13; it is what stamps a module's runs.)

**Owner decisions (S5 review).** (1) Request Intake's identity is `request_intake` / `request_intake` / `1`. (2) The historical backfill is strict: a run is attributed only on evidence and unknown runs are never guessed at.
(3) Immutability stays ORM-level: no database trigger in S5. (4) `workflow_version` is a positive 32-bit integer, in the manifest, the attribution and the database. (5) No index without a demonstrated need. (6) No
`module_version` on `WorkflowRun`. (7) Canonical payload bytes and content hashes are preserved. One design choice remains the implementer's, open to review: the shape and reserved-namespace rules are repeated in Core
rather than injected from the registry, because Core cannot import it (kept equal by tests).

**Tests.** DB-free: `tests/test_workflow_attribution_pure.py` (14, runs with `python -m unittest`, no Django), `tests/test_workflow_attribution.py` (58: Request Intake's identity and the synthetic manifest, the model
and queryset guard with the SQL method replaced, `create_run`, the migration structure, the backfill and validation functions with fake models, the one-creator AST test, and the boundaries including the unchanged canonical
hash). Narrowly updated older pins: `test_intake_composition.py` (the run's columns and constraints), `test_review_models.py` (the cases migration names), `test_cases_persistence.py` (`create_run` needs an attribution).
PostgreSQL, **owner-run locally, passed** (section 3.12): `tests/test_workflow_attribution_postgres.py` (37 tests: the migrations against existing rows, the refusal, the idempotency, the reverse, the constraints and PostgreSQL's reading of the patterns,
what the database cannot know, the ORM guard on a real database, Request Intake end to end with the canonical hash, and concurrent creation). The existing PostgreSQL suites that create runs were edited only to pass an
attribution (`test_cases_persistence_postgres.py`, `test_intake_postgres.py`), and `RunStateMigrationTests` now stops at `cases.0005` because the runs it inserts have no source (it empties the cases tables before the
cleanup returns the test database to the latest migrations).

### 3.13 The ModuleContext: the controlled capability facade (MOD-1b / S6)

**Status.** Implemented on `feat/module-capability-facade` (committed locally, not yet pushed, no PR). DB-free tests pass in the development VM (131 new ones; 2129 in all). **The PostgreSQL tests (72) have NOT been run**
(the VM has no PostgreSQL and never connects to one); the owner's commands are in the [runbook](../development/runbook.md), section 5.3. **No module is registered, Request Intake is still not a module (MOD-2) and does not
use a context, nothing calls the composition root, and no real AI integration exists.** No migration, no model and no existing service changed.

**Goal.** One controlled interface through which a future module reaches Core: `ModuleContext`, built for one registered module from the approved static registry, exposing up to four facades, each only if the manifest
lists the capability in `capabilities_required`. Every facade **delegates to the existing Core service unchanged**, after the checks that belong to the module (its declarations, its state). No queue, state-machine,
audit or authorization rule is written again.

**Where it lives.** `apps/modules` stays pure and unchanged. The Django runtime part is `apps/module_host/context.py` (the context and the four facades) over `apps/module_host/context_rules.py` (the decisions as pure
functions over a manifest and a registry: standard library plus the host's own rules, tested without Django). `ModuleContextError` and its codes are in `apps/module_host/errors.py`. The composition root is
`config/module_context.py`: `module_context(module_id)` opens a context over `config.modules` and the production task handler allowlist. Nothing imports it yet (a test pins that).

**The API.**

| Call | Delegates to | What the context adds |
| --- | --- | --- |
| `ModuleContext.open(registry, module_id, *, handler_registry=None)` | none (reads no database) | identity from the registry only; `invalid_registry`, `invalid_module_id`, `unknown_module` |
| `.module_id`, `.module_version`, `.registry_fingerprint`, `.capabilities`, `.manifest`, `.has_capability(c)` | none | immutable metadata; `capabilities` is the declared capabilities that have a facade |
| `.tasks.enqueue(*, kind, payload, idempotency_key=None, max_attempts=..., available_at=None, now=None)` | `apps.tasks.services.enqueue_task` | the kind must be the module's own declared kind; a given key must start with an owned namespace; the module must be available |
| `.cases.create_case(*, reference, case_id=None)` | `apps.cases.services.create_case` | the module must be available |
| `.cases.create_run(*, case_id, workflow_key, run_id=None)` | `apps.cases.services.create_run` | the workflow must be declared; the attribution is built here from the module's id, the key and the DECLARED definition version |
| `.cases.transition_run(run_id, *, to, failed_step="", allowed_from=None)` | `apps.cases.services.transition_run` | the module must be available; the run must have been created under this module (checked by an unlocked read of its immutable attribution) |
| `.audit.record(*, actor, action, subject_type, subject_id, case_id=None, run_id=None, **details)` | `apps.audit.recorder.record_event` | declared action only, declared detail keys only, restricted subject types, own runs only, the caller's transaction required |
| `.permissions.require(user, *permission_ids)` and `.authorize(user, *permission_ids, owner_pk=None)` | `apps.accounts.access` (`permission_problem`, `access_problem`) over `apps.accounts.authz.load_actor` | only declared permissions (by their declared scope) and the permitted Core permissions |

The context and its facades are immutable (`__slots__`, assignment and deletion refused), cannot be constructed directly (a private token; `ModuleContext.open` is the only way in), and expose no connection, cursor, registry,
model or filesystem handle. `revision.approval` stays a capability name with **no facade**: declaring it gives nothing (`capability_not_available`), as does a capability another module provides.

**Capability rules (each enforced by code and tests).**

* **Declared only.** Reaching `tasks`, `cases`, `audit` or `permissions` without the capability in the manifest is `capability_not_declared`.
* **Namespace ownership.** A task kind belongs to the module only if its manifest declares it (a kind another module owns is `task_kind_not_owned`, any other is `task_kind_not_declared`: `test.add_numbers` and
  `request_intake.extract` included). An idempotency key, when given, must start with a namespace the module owns followed by a dot (`idempotency_key_not_namespaced`); no key is allowed, as in the queue. A workflow must
  be declared (`workflow_not_declared`). Ownership is exact, never a prefix.
* **Attribution is the module's.** `create_run` accepts no module id, version or attribution from the caller (a test pins that passing one is a `TypeError`); the stored attribution is the manifest's declaration.
* **Other modules' runs, and no existence disclosure.** A transition, and an audit event naming a run, are refused on a run created under another module. **A run that does not exist, a malformed id and another module's run are
  refused identically** (`run_not_owned`, one message), so a module with no authority over a run cannot tell whether it exists; an actor that is refused (`AuthorizationError`) and a module that is disabled are told nothing about any run either
  (the state check and the actor check come before the run is read). The ownership check is an **unlocked** read of the run's module id (the attribution is immutable): the context never takes a row lock on a run that is not its
  own, so a module cannot hold up another module's run. For an authorized module the Core services behave exactly as before (an own run with an illegal transition is the service's own `InvalidTransitionError`). Request Intake's runs
  belong to `request_intake`, so no context can move them. Historical runs are never touched. **Cases are not module-owned**, so `create_run` with an unknown case is still the service's `RecordNotFoundError`: the existence of a case id is
  not hidden.
* **Audit.** Only an action the module declares (a Core action is `audit_action_core_owned`, an unknown one `audit_action_not_declared`), only the declaration's detail keys (`audit_detail_key_not_declared`), only the run, revision,
  approval and rejection subject types (`audit_subject_type_not_allowed`: `module` is the host's own). The audit vocabulary's value rules and the content restraint apply unchanged. `record` **opens no transaction**: it needs the caller's
  (`AuditTransactionError` otherwise, the recorder's own error), so the event commits or rolls back with the operation. The facade logs nothing.
* **Permissions.** Only the module's declared permissions and `audit.view_auditevent` (`context_rules.PERMITTED_CORE_PERMISSIONS`); anything else, another module's permission and every other Core permission included, is
  `permission_not_declared`, even for a superuser. Scopes map as section 3.8 said they would: `own` to the rule's own permissions, `any` and `global` to its any permissions. The decision is Core's rule, unchanged: superuser
  passes, `any` passes, `own` passes only for the actor's own record (`out_of_scope`), none is `missing_permission`. **Refusal precedence and no existence leak:** `require` is the permission part alone, to be called before the record
  is looked up (the order Request Intake's services use); `authorize` runs both parts, permission first, so a user with no permission gets the same answer whoever owns the record or whether it exists. The caller supplies
  `owner_pk`; the context never reads a record. A refusal is logged by Core's `log_refusal` (the label `<module>:<permissions>`, the code and the actor's public id; never a name or a value).

**Explicit actors, no ambient identity.** Nothing reads a request, a thread-local or a setting. Every method that records or decides about a person takes the user as an argument, and Core re-reads the user from the database
(`load_actor`): a caller can never supply a public id, a stale object is judged by what is stored now, and an inactive or deleted user is refused (`AuthorizationError`). `audit.record` needs `actor=`: a user or the
explicit sentinel `SYSTEM_ACTOR` (one instance, copies are itself); `None` is `actor_required`, and a string or a made-up object that is not a user is refused by `load_actor`.

**Module state enforcement.**

| Call | Gated by module state? |
| --- | --- |
| `tasks.enqueue`, `cases.create_case`, `cases.create_run`, `cases.transition_run`, `audit.record` | **yes** (state-changing) |
| `permissions.require`, `permissions.authorize` | **no** (they only decide; they read no state and take no lock) |

The rule is the host's one rule (`rules.assess_modules`, section 3.11): registered, state present once the first sync has completed (the durable marker), enabled, version-matched, direct dependencies available. The refusals are
`unknown_module` (at `open`), `module_disabled`, `module_version_mismatch`, `module_state_missing` and `module_dependency_unavailable`. **Before the first sync a registered module counts as enabled** (the documented
backward-compatibility transition, reason `legacy_unsynced`). Each state-changing call does, in this order: the pure checks (declarations, ownership: no database), then inside a transaction (the facade opens one, which joins the
caller's; `audit.record` uses the caller's) it takes a **shared row lock on every `ModuleState` row** (`SELECT module_id FROM module_host_modulestate ORDER BY module_id FOR SHARE`, the context's one statement of raw SQL), reads the
state, and only then delegates. Consequences: concurrent capability calls never block each other (shared locks); `module_enable`, `module_disable` and `module_sync` (which lock the rows `FOR UPDATE`, in the same order, so there is no
deadlock) **wait for an in-flight call to commit**; a call that starts after a disable has committed is refused. So no capability write by a disabled module can commit after `module_disable` returns, except through a transaction
that took its lock before. The lock is held until the outermost transaction ends, which is the caller's: a long caller transaction delays an administrator's disable. Held tasks, queued work and every record stay exactly as the host
(section 3.11) leaves them; the worker's kind gate, claim, leases, retries and recovery are not touched (a test pins that no file of the queue or the worker mentions the context).

**Error codes.** `ModuleContextError.code` (stable; the message names the rule and identifiers that already passed validation, never a payload, a detail value or business content; a test checks it does not echo values):
`invalid_registry`, `invalid_module_id`, `unknown_module`, `capability_not_declared`, `capability_not_available`, `module_disabled`, `module_version_mismatch`, `module_state_missing`, `module_dependency_unavailable`,
`task_kind_not_owned`, `task_kind_not_declared`, `idempotency_key_not_namespaced`, `workflow_not_declared`, `run_not_owned`, `audit_action_core_owned`, `audit_action_not_declared`, `audit_detail_key_not_declared`,
`audit_subject_type_not_allowed`, `permission_not_declared`, `no_permission_requested`, `actor_required`. Everything the Core services raise (`IdempotencyConflictError`, `InvalidPayloadError`, `InvalidTransitionError`,
`RecordNotFoundError`, `AuditEventError`, `AuditTransactionError`, `AuthorizationError`) passes through unchanged.

**Request Intake stays legacy.** Its registry is empty, so `module_context("request_intake")` is `unknown_module`; its submission path, the API and the worker composition do not mention the context (a test scans them), its task kind
stays a legacy ungated kind, and a submission takes no state lock and reads no module state (a PostgreSQL test captures the SQL). The synthetic Request Intake manifest of `tests/module_support.py` opens a context in a test, which is what MOD-2
will do for real: MOD-2 must remove the kind from the legacy set, register the manifest, route its code through the facades and install the audit vocabulary in each process (nothing in S6 installs it; `audit.record` then fails
loudly with `AuditEventError` for a declared action, which is the S3 design).

**Trust limits (read with section 7).** A context is a **naming and authority boundary inside one process, not a sandbox.** Code admitted to the process can import the Core services directly, open its own database connection,
read files or forge anything Python allows; the context cannot stop that, it only gives a well-behaved module one door that checks its manifest and its state. What the facades do guarantee is mechanical: the checks above run
before any delegation, in the order above, and a refusal changes nothing. Module authors are first-party (section 11, decision 8). Other limits: the context trusts the registry it is given (the composition root's static tuple); the
registry fingerprint is carried, not compared with the worker's; `audit.record` cannot check that a revision or approval subject belongs to the module (only runs are checked); the state is read per call, not cached, and the S2 gate
race window for the worker's claim is unchanged.

**Review and hardening (S6 review).** Two defects were confirmed and fixed, and the rest of the review found nothing to change.

* *Existence disclosure and a cross-module lock (fixed).* `cases.transition_run` took the service's `lock_run` (`SELECT ... FOR UPDATE`) on the run first and compared its module afterwards, so (a) a missing run raised the service's
  `RecordNotFoundError` while another module's run raised `run_not_owned`, which let a module with no authority probe whether a run id exists, and (b) a module took a **row lock on another module's run** until its transaction ended. The
  ownership check is now an unlocked read of the immutable attribution (`_check_own_run`), after the state check; a missing run, a malformed id and a foreign run raise the same error with the same message; the service's own lock is taken
  only for an own run, inside `transition_run`. The audit path already answered both cases alike; a test now pins it and pins that a refused actor never reaches the run table.
* *Identity could be re-read by a subclass (fixed).* `ModuleContext` and the facades were immutable but not final: a subclass could override `module_id` or a capability property. They now refuse subclassing from outside the module
  (`TypeError`); this is accident-proofing, not a sandbox (code in the process can still call `object.__setattr__`).
* *Transactions and locks (reviewed, no defect).* Every state-changing facade call checks the state **inside a transaction after taking the shared lock**, and delegates only afterwards; `enqueue` and the case calls open their own
  `transaction.atomic()` (a savepoint inside the caller's), so the lock is held exactly until the outermost transaction ends, which is the end of the operation when the caller has none; `audit.record` uses the caller's and opens none. The
  host's `FOR UPDATE` over all rows in module-id order and the context's `FOR SHARE` in the same order cannot deadlock, and a disable cannot slip between a call's state check and its write: it waits for every in-flight kind of call
  (`enqueue`, `create_case`, `create_run`, `transition_run`, `audit.record`: PostgreSQL tests) and a call that meets an uncommitted disable waits and is then refused. This relies on PostgreSQL's default READ COMMITTED isolation (the
  project sets none; under REPEATABLE READ a row changed since the snapshot would make `FOR SHARE` fail with a serialization error, which also fails closed). A `DatabaseError` from the lock, the state read or a service propagates unchanged and
  nothing is delegated (DB-free tests, and a lock-timeout test on PostgreSQL); it is never turned into a module refusal. The context introduces no long-running transaction of its own, but a caller's long transaction delays an
  administrator's `module_enable`, `module_disable` or `module_sync`, and a call waits behind a disable in progress for as long as `lock_timeout` allows (none is set by default).
* *Audit and permissions (reviewed, no defect).* An action must be declared and namespaced by the module (a Core action is refused first), only its detail keys and the vocabulary's value rules apply, a transaction is required, no identity is
  inferred (`actor` is required; `None` is refused; a made-up object goes to `load_actor`), the permission part runs before the scope part, and the only Core permission a module may check is the explicit allowlist
  `{audit.view_auditevent}`. Known and accepted: `case_id` on an audit event is not checked against the run's case, and revision, approval and rejection subjects are not checked for ownership.

**Remaining limitations.**

* No module is registered and Request Intake is not a module, so nothing uses the context outside tests; the four facades are validated by tests with a synthetic module only.
* `revision.approval` has no facade (a module cannot create revisions or approvals through Core yet), there is no configuration or secret access, no document access and no AI access.
* The PostgreSQL behaviour (the shared lock, the transaction joining, real users and roles, concurrency) is **not run**: until the owner runs the 72 tests nothing about it is proven.
* `permissions.authorize` is a Core decision only; Request Intake's own `apps/intake/authorization.py` is unchanged and still its own glue (they agree, by a PostgreSQL test over every user and owner).
* No public API, no UI and no administrator command use the context; no sandbox, installer or Update Manager.

**Design choices open to owner review.** (1) The context lives in `apps/module_host` with one `FOR SHARE` statement of raw SQL, which narrowed the S4 boundary pins (the host may reach the queue and the authorization core from
`context.py` only; `services.py`, `gate.py` and `rules.py` still may not). (2) `permissions.*` is not gated by module state. (3) `revision.approval` has no facade. (4) `permissions.authorize(user, *ids, owner_pk=)` with a separate `require`
instead of one call. (5) Audit subject types are limited to run, revision, approval and rejection, and only run ownership is verified. (6) *Resolved in the review:* a missing run and a foreign run are now indistinguishable (see "Other modules' runs"). (7) An idempotency key is optional, as in the queue. (8) The explicit `SYSTEM_ACTOR` sentinel rather than a `None` default. (9) `config/module_context.py`
as the composition root, importing `config.modules` (the pinned importer lists were extended). (10) The S5 one-creator test now lists the context as the second creator of runs.

**Tests.** DB-free: `tests/test_module_context_rules.py` (33, pure: they run with `python -m unittest` and no Django), `tests/test_module_context.py` (98: construction and identity, immutability, capability gating, state enforcement and
its order, delegation arguments, explicit actors, permission scopes against Core's rule, the Request Intake compatibility and the architecture boundaries), with the helper `tests/module_context_support.py`. Each of 19 deliberate
mutations of the implementation was caught by the focused set. Narrowly updated older pins: `test_module_boundaries.py` (the importers of `apps.modules` and of `config.modules`), `test_module_host_boundaries.py` (what `context.py` may import,
the one extra statement of raw SQL, the importers of the host) and `test_workflow_attribution.py` (the creators of runs). PostgreSQL, **not run**: `tests/test_module_context_postgres.py` (72 tests: real task enqueue and the unchanged queue,
real runs with attribution and the state machine, audit inside and rolled back with the transaction and the ORM immutability of events, permission checks with real users and roles against Request Intake's own authorization,
disable, enable, version mismatch, missing state and the pre-sync transition against real rows, concurrency including a disable that waits for an in-flight call, and the legacy Request Intake path). Commands: runbook section 5.3.

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
| `workflows[].definition_version` | Version of a workflow definition, so runs can stay pinned to the version they started on | format (the manifest); since S5 a run stores it as `workflow_version` (section 3.12) |
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
- Capability and permission declarations are enforced only through the `ModuleContext` (S6, section 3.13), and only for code that uses it; no module is registered, so nothing outside tests does. Permission scopes are declarations that the context turns into Core's rule when a module asks; the default scope is `any`.
- Default role grants are not wired: `setup_roles`, `/api/auth/me/` and `apps/accounts/profile.py` still use `apps/accounts/roles.py`,
  and nothing calls `derive_role_permissions`. Roles are fixed to the existing three.
- Core's access rule (`apps/accounts/access.py`, section 3.8) is used only by Request Intake's own table. Nothing builds `AccessRule`
  objects from manifest permission scopes, no module can use it, and the generic "read actor, decide, log, refuse" sequence is still
  Request Intake's glue (`apps/intake/authorization.py`); since S6 the same read-actor, decide, log, refuse sequence is available to modules behind `permissions.check` (section 3.13), while Request Intake keeps its own glue. One characteristic
  carried over unchanged: `access_problem` evaluates `permissions` twice, so a one-shot iterator (a generator) would be exhausted by the
  first part; every caller passes a `frozenset` and the tests use re-iterable collections only.
- The behaviour-preservation tests for S1b are DB-free in the VM. The PostgreSQL comparison (`tests/test_authz_core_postgres.py`) and the
  existing review and API suites passed in the owner's local run (242 tests, owner-supplied, section 3.8); the VM never ran them.
- Still absent after S1b: `ModuleState`, runtime module permissions (no module permission is checked at run time; no manifest becomes
  an `AccessRule`), enable/disable, and the capability facade (the first three were built in S4, the facade in S6, section 3.13). Request Intake is not a module.
- Maintenance hooks in a manifest are declarations: nothing reads them. Since S2 `apps/tasks/worker.py` can run further named hooks after
  the legacy `after_iteration` hook (section 3.9), but the production composition installs none, `while_disabled` is not consulted (S4 did not
  change that), and a manifest hook id is not resolved to code anywhere.
- The worker's claim filter and kind gate (S2, section 3.9) are **installed since S4** (section 3.11), but no module is registered, so the gate answers
  the legacy kinds and holds nothing today. The S2 PostgreSQL tests passed in the owner's local run (section 3.9) for the tested scenarios
  only; the gate race window remains.
- S4 (section 3.11) adds `apps/module_host`, `ModuleState`, the sync and enable/disable administrator commands and the persisted namespace history
  (the continuity check runs in every sync). Its PostgreSQL tests and migration passed in the owner's local run (section 3.11, tested scenarios only); the limitations
  of S4 are listed in section 3.11, "Remaining limitations".
- Since S6 a capability facade exists for `task.enqueue`, `case.run`, `audit.record` and `permissions.check` (the `ModuleContext`, section 3.13); `revision.approval` has none. Nothing outside tests calls it (no module is registered; Request Intake is legacy), its PostgreSQL tests are not run, and it is a naming and authority boundary, not a sandbox.
- S5 (section 3.12) stamps runs with their module, workflow and definition version, but nothing reads the stamp: no query, preflight or continuity check uses it yet, the stored pair is checked for shape and reserved
  names and not against a registry, and protection is ORM-level. Its PostgreSQL tests and migrations passed in the owner's local run (section 3.12, tested scenarios only).
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
4. **Run snapshots:** `WorkflowRun` gained `module_id`, `workflow_key` and `workflow_version` in S5 (section 3.12; owner-validated on PostgreSQL). Still open: the configuration and model-profile snapshots
   ([data-model.md](data-model.md)); by owner decision S5 adds neither the module's own version at creation nor an index (an index needs a demonstrated need), and `definition_version` is bounded to a positive 32-bit integer in the manifest.
5. **Result schema versioning and the content hash** ([open-questions.md](open-questions.md), 2.14), before a second module
   produces revisions.
6. **Permission model for installable modules:** namespaced permissions and the existing three roles are decided (owner,
   MOD-1b); still open is how an installable package without models gets Django permission rows (Django creates them only for a
   model), versus a Core permission registry table. Built-ins keep `Meta.permissions`.
7. **Disabling a module:** decided as read-only mode (section 8); the worker claim filter and kind gate seam exist since S2 (section 3.9; its PostgreSQL
   tests passed in the owner's local run); S4 built the state storage, the gate that reads it and the enable/disable commands (section 3.11; its
   PostgreSQL tests passed in the owner's local run). Still open: how the API process learns the enabled set (there is no API use yet; the proposal is the same
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
`tests/test_module_host_cli.py` and `tests/test_module_host_boundaries.py` (198 tests; pass in the VM); PostgreSQL `tests/test_module_host_postgres.py` (75 tests;
owner-run locally, section 3.11); the recovery harness (`tests/recovery_support.py`, `tests/recovery_worker_child.py`, `tests/test_tasks_recovery_support.py`, 8 new DB-free
tests) and the audit migration fixtures (`tests/test_audit_vocabulary.py`, 4 new DB-free tests, and `tests/test_audit_vocabulary_postgres.py`, 25 tests) were corrected
(test-only). Source: `backend/apps/module_host/{apps,models,errors,rules,services,gate,cli}.py`, `backend/apps/module_host/management/commands/module_{sync,status,enable,disable}.py`
and `backend/apps/module_host/migrations/0001_initial.py` (all new), `backend/apps/audit/events.py` (two Core actions added), `backend/config/task_registry.py`
(the gate and `LEGACY_UNGATED_KINDS`) and `backend/config/settings/base.py` (the app). `apps/modules`, `config/modules.py`, `apps/tasks`, the recorder, the existing
migrations and Request Intake are unchanged.

From S5 (section 3.12): DB-free `tests/test_workflow_attribution_pure.py` and `tests/test_workflow_attribution.py`; PostgreSQL `tests/test_workflow_attribution_postgres.py` (37 tests; owner-run locally, passed). Source: `backend/apps/cases/attribution.py`,
`backend/apps/cases/migrations/0006` to `0009` (new), `backend/apps/cases/{models,services,errors}.py`, `backend/apps/intake/{policy,submission}.py` (changed), `backend/apps/modules/manifest.py` (changed: only `MAX_WORKFLOW_DEFINITION_VERSION` and its check) and
`backend/tests/attribution_support.py` (new). The rest of `apps/modules`, `config/`, `apps/tasks`,
`apps/audit`, the REST API and the frontend are unchanged.
