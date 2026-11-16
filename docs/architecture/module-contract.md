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
> are unchanged, there is no `apps/module_host` yet, and Request Intake is still not a module.

## 1. Purpose and scope

The product is one shared Core plus business modules, connectors and AI adapters that can be versioned independently
([ADR 0001](../decisions/0001-modular-monolith.md), [ADR 0005](../decisions/0005-company-configuration-not-forks.md),
[overview.md](overview.md) section 5, rule 3: "the core does not import workflow packages by name; packages register themselves
through a defined registration interface"). Before any module exists, MOD-1a fixes the **contract**: what a module declares
about itself, how declarations are validated, and how a fixed, reviewed set of them becomes an immutable registry.

**Built in MOD-1a:** manifest v1 (immutable, self-validating dataclasses), a documented SemVer subset with ranges, Core API
compatibility checks, dependency checks with cycle detection, capability declarations, a deterministic registry with a stable
fingerprint, and architecture tests that keep all of it isolated.

**Not built:** loading or activating modules, a module state table, permissions seeding, task-handler registration with the
worker, API or UI mounting, the audit-vocabulary change, configuration storage, licensing, signatures, packages, an update
manager, a marketplace (section 10).

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
| `audit_actions` | no | `AuditActionDeclaration(action, detail_keys)`; at most 32 characters (the `AuditEvent.action` column) |
| `configuration_schema` | no | `ConfigSchema(schema_version, fields)` of typed `ConfigField`s (`bool`, `int`, `string`, `enum`, `secret_ref`): no code, no expressions; a `secret_ref` names a secret held elsewhere and can have no default |
| `ui_contributions` | no | `UiContribution(contribution_id, kind, path, label, permission)`; `kind` is `route` or `navigation`; `permission` is a UI hint only |
| `api_contributions` | no | `ApiContribution(route_prefix)`: one path segment under `/api/`; `auth`, `health`, `modules` and `admin` are Core's |
| `update` | no | `UpdateMetadata(data_schema_version, minimum_updater_protocol)`, both optional positive integers (section 4.4) |

Collections are bounded (at most 100 entries each; 50 config fields) as a sanity limit, not a business rule.

### 3.2 Identifiers and namespaces

A module **owns** its `module_id` and any `extra_namespaces` as identifier prefixes. Every task kind, permission id, audit action,
workflow key, provided capability and UI contribution id must start with an owned namespace followed by a dot. Namespaces are
unique across modules, and a fixed set is reserved for Core and cannot be owned:

`core, test, platform, module, auth, admin, health, run, revision, approval, rejection, audit, task, case, document, documents, ai, secrets, permissions`

so one module cannot define, shadow or collide with another module's or Core's identifiers (the existing `test.add_numbers`
handler and the existing `run.state_changed` audit action stay Core's). `extra_namespaces` exists so a built-in module can keep
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
| `permissions.check` | Ask whether a user may act, using permissions the module itself declares (or Core's listed ones), with the own/any/superuser rules | `apps.accounts.authz.load_actor` and the access rules (today in `apps.intake.access`) |

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
while_disabled)`, deterministically: modules in registration order, then hook id. `worker.py` is not changed.

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
keep. **Nothing stores the history and nothing calls these functions yet**: the later module-state and update slices will keep the
history in the database and run the check before a release is activated.

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
  running. S2 changes the worker for this; S1a does not touch it.
- **Runtime state lives in a separate `apps/module_host` Django app** (a future `ModuleState`, namespace claims, sync/enable/disable
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

- No module is registered; `BUILTIN_MODULES` is empty and nothing calls `build_registry`.
- No activation, enablement state, database table, model or migration for modules.
- No handler registration with the worker, no permission seeding, no API or UI mounting, no endpoint listing modules.
- The audit vocabulary, run states and permission constants in Core are still shaped by Request Intake
  (`apps/audit/events.py`, `apps/cases/run_state.py`, `apps/accounts/roles.py`); the contract does not change them.
- No configuration storage or value validation (only schema declarations), no secrets store, no document storage, no AI gateway.
- No license enforcement, no signatures, no package format, no installer, no update manager, no marketplace.
- No sandbox; see section 7.
- Capability and permission declarations are not enforced at run time. Permission scopes are declarations; the default scope is `any`.
- Default role grants are not wired: `setup_roles`, `/api/auth/me/` and `apps/accounts/profile.py` still use `apps/accounts/roles.py`,
  and nothing calls `derive_role_permissions`. Roles are fixed to the existing three.
- Maintenance hooks are declarations: `apps/tasks/worker.py` still has its single `after_iteration` hook and does not read them.
  A hook id is not resolved to code anywhere.
- No `ModuleState`, no `apps/module_host`, no enable/disable, no persisted namespace history; the continuity check is not called.
- No capability facade exists (`task.enqueue` and the others are names a manifest may require, nothing a module can call).
- Request Intake has no manifest in the application; the synthetic one in `tests/module_support.py` exists only for the golden test.
- Only manifest version 1; no pre-release versions.

## 11. Open decisions (for later slices)

1. **Loading mechanism** for module manifests that live in packages importing Django models (lazy import after app loading).
2. **Audit vocabulary:** relax the database CHECK constraints on audit actions and subject types to a prefix pattern plus a registry
   allowlist, or add a Core migration per new action.
3. **Run-state vocabulary:** keep the Core set fixed with modules mapping onto it, or make it extensible; the name `extracting` is
   intake-shaped.
4. **Run snapshots:** when `WorkflowRun` gains `workflow_key` and `workflow_version`
   ([data-model.md](data-model.md), `WorkflowDefinition`).
5. **Result schema versioning and the content hash** ([open-questions.md](open-questions.md), 2.14), before a second module
   produces revisions.
6. **Permission model for installable modules:** namespaced permissions and the existing three roles are decided (owner,
   MOD-1b); still open is how an installable package without models gets Django permission rows (Django creates them only for a
   model), versus a Core permission registry table. Built-ins keep `Meta.permissions`.
7. **Disabling a module:** decided as read-only mode (section 8); the worker claim filter, the state storage and the API/worker
   agreement on the enabled set are still to be built (S2, S4).
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
