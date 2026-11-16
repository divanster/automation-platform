"""The module registry: an immutable, deterministic, validated set of module manifests. Pure Python, standard library only.

WHAT IT IS. ``ModuleRegistry.build(manifests)`` takes manifest OBJECTS, checks that they fit together and with this Core, and
returns a read-only registry. It imports nothing, scans nothing and executes nothing: it cannot find a module that was not handed
to it. In MOD-1a nothing activates, enables or runs a module; the registry only answers "which approved modules exist, in what
order, and do they fit".

GUARANTEES (each pinned by tests/test_module_registry.py):

* IMMUTABLE. A registry cannot be changed after ``build`` (no attribute can be set or deleted, collections are tuples or read-only
  mappings, and mutating the list passed to ``build`` afterwards changes nothing).
* DETERMINISTIC. The result depends only on the manifests' content, never on the order they were passed in, on dictionary or set
  ordering, or on the process (``PYTHONHASHSEED``). Modules are ordered topologically (a dependency before its dependents); among
  modules that are ready at the same time, by module id.
* NO SILENT OVERWRITING. Two modules claiming the same module id, task kind, permission, audit action, workflow key, API route,
  UI contribution id, UI route path, namespace or provided capability is an error, never a "last one wins".
* FAIL CLOSED. Any problem raises ``RegistryError`` with a stable code and builds nothing. There is no partial registry.
* STABLE FINGERPRINT. ``fingerprint`` is the SHA-256 of the canonical form of the ordered manifests (``registry_format`` 1). It is
  meant to let the API process and the worker confirm they were built from the same modules (a skew between them is a risk).

CHECK ORDER (the first problem found is raised, so the error for a given input is deterministic): object types; duplicate module
ids; compatibility with this Core API and update protocol; dependencies exist and are version-compatible; no dependency cycle;
clashes between identifiers (most specific code first); namespace ownership; capability requirements; UI permission references.
Namespace uniqueness comes after the identifier clashes so that a clash names the identifier that collided.

WHAT IT IS NOT. It is not a loader, an installer, an activator or a license check, and it is not a security boundary: a module that
is allowed into a process runs with that process's privileges (docs/architecture/module-contract.md, "Trust boundaries").
"""

import hashlib
import heapq
import json
from collections.abc import Iterable, Iterator
from types import MappingProxyType

from .errors import RegistryError
from .manifest import ModuleManifest
from .versions import CORE_API_VERSION, UPDATER_PROTOCOL_VERSION, Version, is_core_api_compatible

REGISTRY_FORMAT = 1

# Capabilities Core provides today, each backed by existing code (apps.tasks, apps.audit, apps.cases and the revision and approval
# services). A module may require these. Their namespaces are reserved, so no module can provide or redefine one.
CORE_CAPABILITIES = frozenset({"task.enqueue", "audit.record", "case.run", "revision.approval"})

# Capabilities the architecture names but that do not exist yet. Requiring one is refused with its own code, not a generic
# "missing", so the reason is clear: the capability is planned, not built.
FUTURE_CAPABILITIES = frozenset({"documents.read", "ai.invoke", "secrets.resolve"})

# Permissions Core defines (a UI contribution may name one).
CORE_PERMISSIONS = frozenset({"audit.view_auditevent"})

_BUILD = object()


def _claim(owners: dict, key: str, owner: str, code: str, what: str) -> None:
    if key in owners:
        raise RegistryError(code, f"{what} '{key}' is declared by both '{owners[key]}' and '{owner}'")
    owners[key] = owner


def _read_only(mapping: dict) -> MappingProxyType:
    return MappingProxyType(dict(sorted(mapping.items())))


class ModuleRegistry:
    """An immutable, ordered set of validated manifests. Create it only with ``ModuleRegistry.build``."""

    __slots__ = (
        "_modules", "_by_id", "_fingerprint", "_core_api_version", "_task_kinds", "_permissions", "_audit_actions", "_workflows",
        "_api_routes", "_ui_contributions", "_capability_providers", "_namespace_owners",
    )  # fmt: skip

    def __init__(self, *, _token=None, **state):
        if _token is not _BUILD:
            raise TypeError("a ModuleRegistry is created with ModuleRegistry.build()")
        for name, value in state.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("a ModuleRegistry is immutable")

    def __delattr__(self, name):
        raise AttributeError("a ModuleRegistry is immutable")

    # construction ------------------------------------------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        manifests: Iterable[ModuleManifest],
        *,
        core_api_version: Version = CORE_API_VERSION,
        updater_protocol_version: int = UPDATER_PROTOCOL_VERSION,
        core_capabilities: frozenset = CORE_CAPABILITIES,
        future_capabilities: frozenset = FUTURE_CAPABILITIES,
        core_permissions: frozenset = CORE_PERMISSIONS,
    ) -> "ModuleRegistry":
        """Validate ``manifests`` against each other and this Core and return the registry, or raise ``RegistryError``.

        The keyword arguments describe the Core the registry is built for and default to this Core's values; tests pass others.
        """
        if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Iterable):
            raise RegistryError("invalid_manifest_object", "manifests must be an iterable of ModuleManifest objects")
        items = list(manifests)  # a private copy: later changes to the caller's list cannot reach the registry
        if not all(isinstance(item, ModuleManifest) for item in items):
            raise RegistryError("invalid_manifest_object", "every registry entry must be a ModuleManifest object, never a path, name or class")

        ordered_input = sorted(items, key=lambda m: m.module_id)
        by_id: dict[str, ModuleManifest] = {}
        for manifest in ordered_input:
            if manifest.module_id in by_id:
                raise RegistryError("duplicate_module_id", f"module id '{manifest.module_id}' is registered twice")
            by_id[manifest.module_id] = manifest

        cls._check_core_compatibility(ordered_input, core_api_version, updater_protocol_version)
        cls._check_dependencies(ordered_input, by_id)
        order = cls._topological_order(by_id)
        modules = tuple(by_id[module_id] for module_id in order)

        claims = cls._check_clashes(ordered_input)
        cls._check_capabilities(ordered_input, by_id, claims["providers"], core_capabilities, future_capabilities)
        cls._check_ui_permissions(ordered_input, core_permissions)

        document = {"registry_format": REGISTRY_FORMAT, "modules": [m.to_dict() for m in modules]}
        fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        return cls(
            _token=_BUILD,
            _modules=modules,
            _by_id=MappingProxyType({m.module_id: m for m in modules}),
            _fingerprint=fingerprint,
            _core_api_version=core_api_version,
            _task_kinds=_read_only(claims["task_kinds"]),
            _permissions=_read_only(claims["permissions"]),
            _audit_actions=_read_only(claims["audit_actions"]),
            _workflows=_read_only(claims["workflows"]),
            _api_routes=_read_only(claims["api_routes"]),
            _ui_contributions=_read_only(claims["ui_ids"]),
            _capability_providers=_read_only(claims["providers"]),
            _namespace_owners=_read_only(claims["namespaces"]),
        )

    # checks ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _check_core_compatibility(ordered, core_api_version, updater_protocol_version) -> None:
        for manifest in ordered:
            if not is_core_api_compatible(manifest.core_api_range, core_api_version):
                raise RegistryError(
                    "incompatible_core_api",
                    f"module '{manifest.module_id}' supports Core API {manifest.core_api_compatibility}, this Core is {core_api_version}",
                )
            needed = manifest.update.minimum_updater_protocol
            if needed is not None and needed > updater_protocol_version:
                raise RegistryError(
                    "unsupported_updater_protocol",
                    f"module '{manifest.module_id}' needs update protocol {needed}, this Core supports {updater_protocol_version}",
                )

    @staticmethod
    def _check_dependencies(ordered, by_id) -> None:
        for manifest in ordered:
            for dependency in sorted(manifest.dependencies, key=lambda d: d.module_id):
                target = by_id.get(dependency.module_id)
                if target is None:
                    raise RegistryError("missing_dependency", f"module '{manifest.module_id}' depends on '{dependency.module_id}', which is not registered")
                if not dependency.range.contains(target.version):
                    raise RegistryError(
                        "incompatible_dependency_version",
                        f"module '{manifest.module_id}' needs '{dependency.module_id}' {dependency.version_range}, registered is {target.module_version}",
                    )

    @staticmethod
    def _topological_order(by_id) -> list:
        """Kahn's algorithm with a min-heap on module id: a dependency always precedes its dependents, ties break by id."""
        unmet = {module_id: {d.module_id for d in manifest.dependencies} for module_id, manifest in by_id.items()}
        dependents: dict[str, list] = {module_id: [] for module_id in by_id}
        for module_id, deps in unmet.items():
            for dep in deps:
                dependents[dep].append(module_id)
        ready = [module_id for module_id, deps in unmet.items() if not deps]
        heapq.heapify(ready)
        order = []
        while ready:
            current = heapq.heappop(ready)
            order.append(current)
            for dependent in dependents[current]:
                unmet[dependent].discard(current)
                if not unmet[dependent]:
                    heapq.heappush(ready, dependent)
        if len(order) != len(by_id):
            stuck = sorted(set(by_id) - set(order))
            raise RegistryError("dependency_cycle", "modules in or behind a dependency cycle: " + ", ".join(stuck))
        return order

    @staticmethod
    def _check_clashes(ordered) -> dict:
        task_kinds: dict = {}
        permissions: dict = {}
        audit_actions: dict = {}
        workflows: dict = {}
        api_routes: dict = {}
        ui_ids: dict = {}
        ui_paths: dict = {}
        providers: dict = {}
        namespaces: dict = {}
        for manifest in ordered:
            owner = manifest.module_id
            for handler in manifest.task_handlers:
                _claim(task_kinds, handler.kind, owner, "duplicate_task_kind", "task kind")
            for permission in manifest.permissions:
                _claim(permissions, permission.permission_id, owner, "duplicate_permission", "permission")
            for action in manifest.audit_actions:
                _claim(audit_actions, action.action, owner, "duplicate_audit_action", "audit action")
            for workflow in manifest.workflows:
                _claim(workflows, workflow.workflow_key, owner, "duplicate_workflow", "workflow key")
            for route in manifest.api_contributions:
                _claim(api_routes, route.route_prefix, owner, "duplicate_api_route", "api route")
            for contribution in manifest.ui_contributions:
                _claim(ui_ids, contribution.contribution_id, owner, "duplicate_ui_contribution", "ui contribution")
                if contribution.kind == "route":
                    _claim(ui_paths, contribution.path, owner, "duplicate_ui_route", "ui route path")
            for capability in manifest.capabilities_provided:
                _claim(providers, capability, owner, "duplicate_capability", "provided capability")
        for manifest in ordered:
            for namespace in manifest.namespaces:
                _claim(namespaces, namespace, manifest.module_id, "duplicate_namespace", "namespace")
        return {
            "task_kinds": task_kinds,
            "permissions": permissions,
            "audit_actions": audit_actions,
            "workflows": workflows,
            "api_routes": api_routes,
            "ui_ids": ui_ids,
            "providers": providers,
            "namespaces": namespaces,
        }

    @staticmethod
    def _check_capabilities(ordered, by_id, providers, core_capabilities, future_capabilities) -> None:
        for manifest in ordered:
            direct = {d.module_id for d in manifest.dependencies}
            for capability in sorted(manifest.capabilities_required):
                if capability in core_capabilities:
                    continue
                provider = providers.get(capability)
                if provider is None:
                    if capability in future_capabilities:
                        raise RegistryError("capability_not_yet_available", f"module '{manifest.module_id}' requires '{capability}', which is planned but not built")
                    raise RegistryError("missing_capability", f"module '{manifest.module_id}' requires '{capability}', which nothing provides")
                if provider not in direct:
                    raise RegistryError(
                        "capability_provider_not_dependency",
                        f"module '{manifest.module_id}' requires '{capability}' from '{provider}' but does not depend on it",
                    )

    @staticmethod
    def _check_ui_permissions(ordered, core_permissions) -> None:
        for manifest in ordered:
            own = {p.permission_id for p in manifest.permissions}
            for contribution in sorted(manifest.ui_contributions, key=lambda u: u.contribution_id):
                permission = contribution.permission
                if permission is not None and permission not in own and permission not in core_permissions:
                    raise RegistryError(
                        "undeclared_ui_permission",
                        f"ui contribution '{contribution.contribution_id}' names a permission neither module '{manifest.module_id}' nor Core defines",
                    )

    # read access -------------------------------------------------------------------------------------------------------

    @property
    def modules(self) -> tuple:
        """The manifests in registration order (dependencies first, ties by module id)."""
        return self._modules

    @property
    def module_ids(self) -> tuple:
        return tuple(m.module_id for m in self._modules)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def core_api_version(self) -> Version:
        return self._core_api_version

    @property
    def task_kinds(self) -> MappingProxyType:
        """task kind -> owning module id."""
        return self._task_kinds

    @property
    def permissions(self) -> MappingProxyType:
        return self._permissions

    @property
    def audit_actions(self) -> MappingProxyType:
        return self._audit_actions

    @property
    def workflows(self) -> MappingProxyType:
        return self._workflows

    @property
    def api_routes(self) -> MappingProxyType:
        return self._api_routes

    @property
    def ui_contributions(self) -> MappingProxyType:
        return self._ui_contributions

    @property
    def capability_providers(self) -> MappingProxyType:
        """capability -> the module that provides it (Core capabilities are not listed)."""
        return self._capability_providers

    @property
    def namespace_owners(self) -> MappingProxyType:
        """namespace -> the one module that owns it (its id and its declared ``extra_namespaces``). Ownership is explicit, never inherited:
        a dependency, a dependent and a similarly named namespace own nothing of each other."""
        return self._namespace_owners

    def get(self, module_id: str) -> ModuleManifest | None:
        return self._by_id.get(module_id)

    def __contains__(self, module_id) -> bool:
        return module_id in self._by_id

    def __len__(self) -> int:
        return len(self._modules)

    def __iter__(self) -> Iterator[ModuleManifest]:
        return iter(self._modules)

    def __repr__(self) -> str:
        return f"<ModuleRegistry {len(self)} module(s) {self._fingerprint[:12]}>"
