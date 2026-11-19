"""The decisions of the module host as pure functions over plain data. Standard library only: no Django, no database, no ``apps.modules`` import.

A registry is read through its public attributes only (``modules``, ``get``, ``dependencies_of``, ``dependents_of``, ``namespace_owners``,
``task_kinds``, ``fingerprint``), so these rules cannot reach anything a registry does not expose. State comes in as ``StateFacts``
(what a ``ModuleState`` row says), read and locked by ``apps.module_host.services``; nothing here reads or writes a database.

THE AVAILABILITY RULE. Whether a registered module's task kinds may be claimed (the worker gate, and the ``claimable`` column of the status):

1. no state row, and the host is not initialized (no completed sync): AVAILABLE (``legacy_unsynced``). This is the backward-compatibility
   transition: an installation that predates module state keeps working until an administrator runs the module sync.
2. no state row, and the host is initialized: NOT AVAILABLE (``missing_state``). After the first sync missing state fails closed.
3. a row whose installed version differs from the registered one: NOT AVAILABLE (``version_mismatch``).
4. a row that is not enabled: NOT AVAILABLE (``disabled``).
5. enabled and version-matched, but a direct dependency is not available: NOT AVAILABLE (``dependency_unavailable``), whatever the dependency's
   own reason. The services keep that situation from arising; this makes a hand-edited database fail closed.

INITIALIZED means the durable ``ModuleHostMarker`` exists (written by the first completed sync, in its transaction), NOT that some row exists: deleting
state rows cannot reopen the window. One conservative exception: ``ModuleState`` rows with no marker is an inconsistent state (only a hand edit or a
partial restore can make it) and counts as initialized, so it fails closed. A sync, even with an empty registry, writes the marker; the next sync repairs
a missing one. A registry with no module never needs a row, so for it the question does not matter.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import ModuleHostError

# Reasons a module is, or is not, available (stable strings: the status output and the tests use them).
ENABLED = "enabled"
LEGACY_UNSYNCED = "legacy_unsynced"
MISSING_STATE = "missing_state"
VERSION_MISMATCH = "version_mismatch"
DISABLED = "disabled"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"

MODULE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"
_MODULE_ID = re.compile(MODULE_ID_PATTERN)
_KIND = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


@dataclass(frozen=True)
class StateFacts:
    """What one ``ModuleState`` row says, as plain data."""

    module_id: str
    installed_version: str
    enabled: bool
    registry_fingerprint: str = ""


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str


@dataclass(frozen=True)
class NewState:
    module_id: str
    installed_version: str
    enabled: bool


@dataclass(frozen=True)
class VersionMismatch:
    module_id: str
    installed_version: str
    registered_version: str


@dataclass(frozen=True)
class SyncPlan:
    """What a sync would do. Nothing in it enables or disables an existing module, deletes anything, or changes a stored version."""

    create: tuple  # NewState, in registration order (a dependency before its dependents)
    refresh: tuple  # module ids whose stored registry fingerprint is out of date (version matches)
    mismatched: tuple  # VersionMismatch: reported, never changed
    unregistered: tuple  # ids with a row but no registry entry: reported, never changed or deleted
    unchanged: tuple  # module ids that need nothing
    new_claims: tuple  # (namespace, module_id) pairs not claimed yet, sorted by namespace

    @property
    def clean(self) -> bool:
        """True when nothing needs the administrator's attention (no version mismatch, no unregistered state)."""
        return not self.mismatched and not self.unregistered


def is_initialized(facts: Mapping, marker_present: bool) -> bool:
    """The first module sync has completed: the marker exists. Rows without a marker (an inconsistent database) also count, so the answer is never more open."""
    return bool(marker_present) or bool(facts)


def check_module_id(module_id) -> str:
    if not isinstance(module_id, str) or _MODULE_ID.fullmatch(module_id) is None:
        raise ModuleHostError("invalid_module_id", "a module id is lower-case letters, digits and underscores, 3 to 40 characters, starting with a letter")
    return module_id


def registered(registry, module_id):
    """The manifest of ``module_id``; ``ModuleHostError('unknown_module')`` when the approved static registry does not contain it."""
    check_module_id(module_id)
    manifest = registry.get(module_id)
    if manifest is None:
        raise ModuleHostError("unknown_module", f"module '{module_id}' is not in the approved static registry")
    return manifest


def kinds_of(manifest) -> frozenset:
    return frozenset(handler.kind for handler in manifest.task_handlers)


# --- availability and the worker gate -----------------------------------------------------------------------------------


def assess_modules(registry, facts: Mapping, *, initialized: bool) -> dict:
    """``module id -> Availability`` for every registered module, by the rule in the module docstring. A dependency is judged before its dependents.

    ``initialized`` is required (keyword-only) so a caller can never fall back to "unsynced" by omission."""
    if not isinstance(initialized, bool):
        raise TypeError("initialized must be a bool")
    synced = initialized
    result: dict = {}
    for manifest in registry.modules:  # topological: dependencies first
        fact = facts.get(manifest.module_id)
        if fact is None:
            own = Availability(not synced, MISSING_STATE if synced else LEGACY_UNSYNCED)
        elif fact.installed_version != manifest.module_version:
            own = Availability(False, VERSION_MISMATCH)
        elif not fact.enabled:
            own = Availability(False, DISABLED)
        else:
            own = Availability(True, ENABLED)
        if own.available and not all(result[dependency].available for dependency in registry.dependencies_of(manifest.module_id)):
            own = Availability(False, DEPENDENCY_UNAVAILABLE)
        result[manifest.module_id] = own
    return result


def check_legacy_kinds(registry, legacy_kinds) -> frozenset:
    """Validate the set of task kinds that predate modules and stay claimable without any module state.

    A kind in it must not also be declared by a registered module: the day a module takes over such a kind (MOD-2 for Request Intake) the
    composition root has to drop it from the legacy set on purpose, so gating cannot start, or fail to start, by accident.
    """
    if isinstance(legacy_kinds, (str, bytes)) or not isinstance(legacy_kinds, Iterable):
        raise ModuleHostError("invalid_legacy_kinds", "the legacy kinds must be a collection of task kind texts")
    kinds = frozenset(legacy_kinds)
    if not all(isinstance(kind, str) and _KIND.fullmatch(kind) for kind in kinds):
        raise ModuleHostError("invalid_legacy_kinds", "every legacy kind must be a task kind text of 1 to 64 characters")
    claimed = sorted(kinds & set(registry.task_kinds))
    if claimed:
        raise ModuleHostError("legacy_kind_claimed", "these legacy task kinds are declared by a registered module: " + ", ".join(claimed))
    return kinds


def check_handler_composition(registry, handler_kinds, legacy_kinds) -> None:
    """The worker's handler allowlist must be exactly the legacy kinds plus the task kinds of the registered modules (MOD-2).

    Every task kind a registered module declares needs a handler in the allowlist (otherwise the gate would answer a kind the worker cannot run), and every
    handler must be owned: legacy, or declared by a module. A new production kind therefore forces a decision (a module's, or legacy) instead of being silently
    gated or silently allowed. ``legacy_kinds`` must already have passed ``check_legacy_kinds``.
    """
    handlers = frozenset(handler_kinds)
    declared = frozenset(registry.task_kinds)
    missing = sorted(declared - handlers)
    if missing:
        raise ModuleHostError("handler_composition", "these task kinds are declared by a registered module but have no handler: " + ", ".join(missing))
    unowned = sorted(handlers - declared - frozenset(legacy_kinds))
    if unowned:
        raise ModuleHostError("handler_composition", "these handlers belong to no registered module and are not legacy kinds: " + ", ".join(unowned))


def allowed_kinds(registry, availability: Mapping, legacy_kinds: frozenset) -> frozenset:
    """The task kinds a worker may claim: the legacy kinds plus the kinds of every available module. Every other kind, including one nobody
    knows, is absent (the S2 claim filter then leaves such a task queued)."""
    allowed = set(legacy_kinds)
    for manifest in registry.modules:
        if availability[manifest.module_id].available:
            allowed |= kinds_of(manifest)
    return frozenset(allowed)


# --- synchronization ----------------------------------------------------------------------------------------------------


def plan_sync(registry, facts: Mapping, claims: Mapping) -> SyncPlan:
    """What the sync must do, given the stored state rows and namespace claims (the continuity check has already passed).

    * a registered module without a row gets one, ENABLED only when all of its direct dependencies are enabled and version-matched (a new
      module never enables itself on top of a disabled dependency);
    * an existing row is never enabled or disabled and its installed version is never changed: a different version is reported;
    * a row whose registry fingerprint is out of date, with the version matching, is refreshed;
    * a row without a registry entry is reported and left alone.
    """
    effective: dict = {}  # module id -> enabled and version-matched, as it will be after the sync
    create, refresh, mismatched, unchanged = [], [], [], []
    for manifest in registry.modules:
        fact = facts.get(manifest.module_id)
        if fact is None:
            enabled = all(effective[dependency] for dependency in registry.dependencies_of(manifest.module_id))
            create.append(NewState(manifest.module_id, manifest.module_version, enabled))
            effective[manifest.module_id] = enabled
        elif fact.installed_version != manifest.module_version:
            mismatched.append(VersionMismatch(manifest.module_id, fact.installed_version, manifest.module_version))
            effective[manifest.module_id] = False
        else:
            effective[manifest.module_id] = fact.enabled
            if fact.registry_fingerprint != registry.fingerprint:
                refresh.append(manifest.module_id)
            else:
                unchanged.append(manifest.module_id)
    unregistered = tuple(sorted(module_id for module_id in facts if module_id not in registry))
    new_claims = tuple((namespace, owner) for namespace, owner in sorted(registry.namespace_owners.items()) if namespace not in claims)
    return SyncPlan(tuple(create), tuple(refresh), tuple(mismatched), unregistered, tuple(unchanged), new_claims)


# --- enable and disable -------------------------------------------------------------------------------------------------


def check_can_enable(registry, module_id: str, facts: Mapping) -> None:
    """Raise ``ModuleHostError`` unless ``module_id`` may be enabled: registered, has state, version-matched, every direct dependency enabled."""
    manifest = registered(registry, module_id)
    fact = facts.get(module_id)
    if fact is None:
        raise ModuleHostError("state_missing", f"module '{module_id}' has no stored state: run the module sync first")
    if fact.installed_version != manifest.module_version:
        raise ModuleHostError(
            "version_mismatch", f"module '{module_id}' is installed as {fact.installed_version} but the registry has {manifest.module_version}: it cannot be enabled"
        )
    blocked = []
    for dependency in registry.dependencies_of(module_id):
        dependency_fact = facts.get(dependency)
        if dependency_fact is None or not dependency_fact.enabled or dependency_fact.installed_version != registry.get(dependency).module_version:
            blocked.append(dependency)
    if blocked:
        raise ModuleHostError("dependency_not_enabled", f"module '{module_id}' needs these modules enabled first: " + ", ".join(sorted(blocked)))


def check_can_disable(registry, module_id: str, facts: Mapping) -> None:
    """Raise ``ModuleHostError`` unless ``module_id`` may be disabled: registered, has state, and no enabled module depends on it directly."""
    registered(registry, module_id)
    if facts.get(module_id) is None:
        raise ModuleHostError("state_missing", f"module '{module_id}' has no stored state: run the module sync first")
    blocking = [dependent for dependent in registry.dependents_of(module_id) if (facts.get(dependent) is not None and facts[dependent].enabled)]
    if blocking:
        raise ModuleHostError("dependents_enabled", f"module '{module_id}' is needed by enabled modules; disable them first: " + ", ".join(sorted(blocking)))
