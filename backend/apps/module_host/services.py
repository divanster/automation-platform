"""Module sync, enable, disable and status: the only code that changes module state (MOD-1b S4).

Each operation is ONE transaction. The module sync also takes a PostgreSQL transaction-level advisory lock, so two syncs run one after the other;
every operation then locks ALL ``ModuleState`` rows (a handful, always in module-id order, so there is no deadlock) before it reads them, which
serializes a concurrent enable and disable: a dependent cannot be enabled while its dependency is being disabled. The audit event of a change
is recorded inside that transaction by Core's one recorder, so the change and its event commit or roll back together.

WHAT THIS DOES NOT DO. It reads only the approved static registry it is handed (built by the composition root, ``config.modules``): it imports no module, runs no
module migration, downloads and installs nothing, and executes nothing from the registry. Disabling a module touches no task and no data: queued
tasks stay queued (the worker gate holds them), running attempts finish, and failed or completed tasks stay as they are.

THE ACTOR. Every change is made by the administrator command line, recorded as the SYSTEM actor (there is no login user and no public API in S4).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from django.db import connection, transaction
from django.utils import timezone

from apps.audit import events
from apps.audit.recorder import record_event
from apps.modules.continuity import check_namespace_continuity
from apps.modules.errors import RegistryError
from apps.modules.registry import ModuleRegistry

from . import rules
from .errors import ModuleHostError
from .models import ModuleHostMarker, ModuleNamespaceClaim, ModuleState
from .rules import StateFacts

logger = logging.getLogger(__name__)

STATUS_FORMAT = 1
CAUSE_ADMIN_COMMAND = "admin_command"
CAUSE_SYNC = "sync"
_SYNC_LOCK_KEY = 7_340_219_001  # an arbitrary constant for pg_advisory_xact_lock: "module host sync"


def _registry(registry) -> ModuleRegistry:
    if not isinstance(registry, ModuleRegistry):
        raise ModuleHostError("invalid_registry", "the registry must be an apps.modules.registry.ModuleRegistry")
    return registry


def _instant(now):
    if now is None:
        return timezone.now()
    if not isinstance(now, datetime) or not timezone.is_aware(now):
        raise ModuleHostError("invalid_clock", "the time of a change must be timezone-aware")
    return now


def _facts(rows) -> dict:
    return {row.module_id: StateFacts(row.module_id, row.installed_version, row.enabled, row.registry_fingerprint) for row in rows}


@dataclass(frozen=True)
class HostState:
    """What the worker gate and the status read: the state rows and whether the first sync has completed (the durable marker)."""

    facts: dict
    initialized: bool


def read_host_state() -> HostState:
    """The state rows, then the marker, each in one query and without a lock. This is what the worker gate reads.

    The rows are read FIRST: a sync that commits between the two reads (rows and marker commit together) can only make this answer more closed (rows
    missing, marker present), never more open.
    """
    facts = _facts(ModuleState.objects.all())
    return HostState(facts, rules.is_initialized(facts, ModuleHostMarker.objects.exists()))


def _lock_rows() -> list:
    """Every ModuleState row, locked ``FOR UPDATE`` in module-id order. Needs an open transaction (the caller's)."""
    return list(ModuleState.objects.select_for_update().order_by("module_id"))


def _record_change(action: str, row: ModuleState, registry: ModuleRegistry, cause: str) -> None:
    record_event(
        events.system_event(
            action,
            events.SUBJECT_MODULE,
            row.pk,
            module_id=row.module_id,
            installed_version=row.installed_version,
            registry_fingerprint=registry.fingerprint,
            cause=cause,
        )
    )


# --- sync ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncReport:
    registry_fingerprint: str
    initial: bool  # the initialization marker did not exist before this sync: this is the first completed sync (or the repair of a missing marker)
    created: tuple  # ((module id, enabled), ...)
    refreshed: tuple
    unchanged: tuple
    version_mismatches: tuple  # rules.VersionMismatch
    unregistered: tuple
    claims_added: tuple  # ((namespace, module id), ...)

    @property
    def ok(self) -> bool:
        """False when a version mismatch or an unregistered state row needs the administrator's attention (nothing is changed for them)."""
        return not self.version_mismatches and not self.unregistered

    def to_dict(self) -> dict:
        return {
            "format": STATUS_FORMAT,
            "ok": self.ok,
            "initial_sync": self.initial,
            "registry_fingerprint": self.registry_fingerprint,
            "created": [{"module_id": module_id, "enabled": enabled} for module_id, enabled in self.created],
            "refreshed": list(self.refreshed),
            "unchanged": list(self.unchanged),
            "version_mismatches": [
                {"module_id": m.module_id, "installed_version": m.installed_version, "registered_version": m.registered_version} for m in self.version_mismatches
            ],
            "unregistered": list(self.unregistered),
            "namespace_claims_added": [{"namespace": namespace, "module_id": module_id} for namespace, module_id in self.claims_added],
        }


def sync_modules(registry, *, now=None) -> SyncReport:
    """Materialize state for the registered modules. Repeatable: a second run with nothing new changes nothing.

    * Reads only ``registry``. Refuses namespace ownership conflicts (a stored claim now belonging to another module id, or released) before
      changing anything (``namespace_ownership_changed``, ``namespace_released``).
    * Creates a ``ModuleState`` row (and records the namespace claims) for a module that has none. The row starts ENABLED, so the modules shipped in a
      release keep working, unless a direct dependency is not enabled, in which case it starts disabled. An existing row is NEVER enabled or
      disabled, and its installed version is never changed: a different registered version is reported (``version_mismatches``) and the module stays
      unavailable. A state row without a registry entry is reported and left alone. Nothing is deleted.
    * Records ``module.enabled`` or ``module.disabled`` (cause ``sync``) for each row it creates, in the same transaction.
    """
    registry = _registry(registry)
    moment = _instant(now)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_SYNC_LOCK_KEY])
        rows = _lock_rows()
        facts = _facts(rows)
        marker_present = ModuleHostMarker.objects.exists()
        claims = dict(ModuleNamespaceClaim.objects.values_list("namespace", "module_id"))
        try:
            check_namespace_continuity(claims, registry)
        except RegistryError as error:
            # The continuity check raises these two codes (or ``invalid_namespace_history``, which a stored mapping cannot produce): refuse as a change.
            code = "namespace_released" if error.code == "namespace_released" else "namespace_ownership_changed"
            raise ModuleHostError(code, str(error)) from None
        plan = rules.plan_sync(registry, facts, claims)

        for namespace, module_id in plan.new_claims:
            ModuleNamespaceClaim.objects.create(namespace=namespace, module_id=module_id, claimed_at=moment)
        for new in plan.create:
            row = ModuleState.objects.create(
                module_id=new.module_id,
                installed_version=new.installed_version,
                enabled=new.enabled,
                registry_fingerprint=registry.fingerprint,
                created_at=moment,
                updated_at=moment,
                enabled_changed_at=moment,
            )
            _record_change(events.ACTION_MODULE_ENABLED if new.enabled else events.ACTION_MODULE_DISABLED, row, registry, CAUSE_SYNC)
        if not marker_present:
            # The durable "first sync completed" record, in this transaction: it exists if and only if the sync commits (an empty registry included).
            ModuleHostMarker.objects.create(initialized_at=moment, initial_registry_fingerprint=registry.fingerprint)
        by_id = {row.module_id: row for row in rows}
        for module_id in plan.refresh:
            row = by_id[module_id]
            row.registry_fingerprint = registry.fingerprint
            row.updated_at = moment
            row.save(update_fields=["registry_fingerprint", "updated_at"])

    report = SyncReport(
        registry_fingerprint=registry.fingerprint,
        initial=not marker_present,
        created=tuple((new.module_id, new.enabled) for new in plan.create),
        refreshed=plan.refresh,
        unchanged=plan.unchanged,
        version_mismatches=plan.mismatched,
        unregistered=plan.unregistered,
        claims_added=plan.new_claims,
    )
    logger.info("module sync: %d created, %d refreshed, %d mismatched", len(report.created), len(report.refreshed), len(report.version_mismatches))
    return report


# --- enable and disable -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeResult:
    module_id: str
    changed: bool  # False for an idempotent no-op: nothing was written and no event was recorded
    enabled: bool


def _change(registry, module_id, *, enable: bool, now) -> ChangeResult:
    registry = _registry(registry)
    manifest = rules.registered(registry, module_id)  # an unknown id is refused before a transaction is opened
    moment = _instant(now)
    with transaction.atomic():
        rows = _lock_rows()
        facts = _facts(rows)
        row = next((r for r in rows if r.module_id == module_id), None)
        # Idempotent: no write and no event. An enabled module whose stored version does not match is NOT a no-op for enable: it is refused below.
        if row is not None and row.enabled is enable and (not enable or row.installed_version == manifest.module_version):
            return ChangeResult(module_id, False, enable)
        (rules.check_can_enable if enable else rules.check_can_disable)(registry, module_id, facts)
        row.enabled = enable
        row.enabled_changed_at = moment
        row.updated_at = moment
        row.save(update_fields=["enabled", "enabled_changed_at", "updated_at"])
        _record_change(events.ACTION_MODULE_ENABLED if enable else events.ACTION_MODULE_DISABLED, row, registry, CAUSE_ADMIN_COMMAND)
    logger.info("module %s %s", module_id, "enabled" if enable else "disabled")
    return ChangeResult(module_id, True, enable)


def enable_module(registry, module_id, *, now=None) -> ChangeResult:
    """Enable ``module_id``. Refused for an unknown module, a missing state row, a version mismatch, or a dependency that is not enabled.

    Enabling an enabled module is a no-op (no write, no audit event). The module's queued tasks become claimable again on the worker's next
    iteration, in their original order; nothing is re-created or re-run.
    """
    return _change(registry, module_id, enable=True, now=now)


def disable_module(registry, module_id, *, now=None) -> ChangeResult:
    """Disable ``module_id``. Refused for an unknown module, a missing state row, or while an enabled module depends on it directly.

    Disabling a disabled module is a no-op. Tasks are not touched: queued tasks are held by the worker gate (never failed), running attempts
    finish, and completed and failed tasks stay as they are. A version mismatch does not prevent disabling.
    """
    return _change(registry, module_id, enable=False, now=now)


# --- status -------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleStatus:
    module_id: str
    module_kind: str
    registered_version: str
    installed_version: str | None
    enabled: bool | None  # None: no state row
    claimable: bool  # whether the worker gate lets this module's task kinds through (the gate uses the same rule)
    reason: str  # one of the rules.* reasons
    dependencies: tuple
    task_kinds: tuple
    configuration_version: int | None
    registry_fingerprint_current: bool | None  # None: no state row
    enabled_changed_at: str | None  # ISO 8601, UTC

    def to_dict(self) -> dict:
        return {
            "module_id": self.module_id,
            "module_kind": self.module_kind,
            "registered_version": self.registered_version,
            "installed_version": self.installed_version,
            "enabled": self.enabled,
            "claimable": self.claimable,
            "reason": self.reason,
            "dependencies": list(self.dependencies),
            "task_kinds": list(self.task_kinds),
            "configuration_version": self.configuration_version,
            "registry_fingerprint_current": self.registry_fingerprint_current,
            "enabled_changed_at": self.enabled_changed_at,
        }


@dataclass(frozen=True)
class HostStatus:
    registry_fingerprint: str
    synced: bool
    modules: tuple = field(default_factory=tuple)  # ModuleStatus, in registration order
    unregistered: tuple = field(default_factory=tuple)  # ids with a state row and no registry entry

    def module(self, module_id: str) -> ModuleStatus:
        for status in self.modules:
            if status.module_id == module_id:
                return status
        raise ModuleHostError("unknown_module", f"module '{module_id}' is not in the approved static registry")

    def to_dict(self) -> dict:
        return {
            "format": STATUS_FORMAT,
            "registry_fingerprint": self.registry_fingerprint,
            "synced": self.synced,
            "modules": [status.to_dict() for status in self.modules],
            "unregistered_state": list(self.unregistered),
        }


def get_module_status(registry) -> HostStatus:
    """The status of every registered module, read without a lock. Reads only; changes nothing."""
    registry = _registry(registry)
    rows = {row.module_id: row for row in ModuleState.objects.all()}
    facts = _facts(rows.values())
    initialized = rules.is_initialized(facts, ModuleHostMarker.objects.exists())
    availability = rules.assess_modules(registry, facts, initialized=initialized)
    statuses = []
    for manifest in registry.modules:
        row = rows.get(manifest.module_id)
        verdict = availability[manifest.module_id]
        statuses.append(
            ModuleStatus(
                module_id=manifest.module_id,
                module_kind=manifest.module_kind,
                registered_version=manifest.module_version,
                installed_version=None if row is None else row.installed_version,
                enabled=None if row is None else row.enabled,
                claimable=verdict.available,
                reason=verdict.reason,
                dependencies=registry.dependencies_of(manifest.module_id),
                task_kinds=tuple(sorted(rules.kinds_of(manifest))),
                configuration_version=None if row is None else row.configuration_version,
                registry_fingerprint_current=None if row is None else row.registry_fingerprint == registry.fingerprint,
                enabled_changed_at=None if row is None else row.enabled_changed_at.isoformat(),
            )
        )
    return HostStatus(registry.fingerprint, initialized, tuple(statuses), tuple(sorted(module_id for module_id in rows if module_id not in registry)))
