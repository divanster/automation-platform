"""The AI settings services: the administrator's read and update, and the worker's read at start-up.

AUTHORIZATION. Only an administrator (an active ``is_superuser`` user, reloaded from the database on every call: ``apps.accounts.authz.load_actor``) may read or
change the settings. No role (Operator, Approver, Auditor) and no Django model permission grants it: the settings decide where request text may be sent, so
they are not delegable by group membership. A refusal is ``AuthorizationError(MISSING_PERMISSION)`` and is decided BEFORE anything is validated or read, so a
caller who is not allowed learns nothing about the settings or the catalog.

NO SECRET. Nothing here reads or returns a credential. The only environment-derived datum is a provider's ``ProviderStatus`` (configured yes or no, one of two
fixed issue codes), produced by the composition root's probe for the PROCESS THAT ANSWERS the request; the worker reads its own environment.

NO FALLBACK. A save is accepted or refused as a whole. ``load_stored_settings`` returns the stored row or ``None``: it never substitutes a default for a
row it cannot read (a database error propagates, and the worker refuses to start).

RUNTIME. A saved change does NOT affect a running worker: the worker reads the row once, at start-up (``config/task_registry.prepare_worker``). The caller
reports that to the client (``APPLY_MODE``).

CONCURRENCY. ``expected_revision`` (0 when no row exists) must equal the stored revision, checked under a row lock, so two administrators cannot overwrite each
other unknowingly (``AISettingsConflictError``).

AUDIT. Every successful save records one append-only ``ai_settings.updated`` event (actor, time, revision, mode, provider, connection, profile, policy, timeout, connection
count; identifiers and numbers only) with ``apps.audit.recorder.record_event`` INSIDE the save's transaction, after the row is written: the event commits with the change or is rolled back with
it, and a failed audit write rolls the change back. A save that creates, changes or removes a connection also records one ``ai_settings.connection_changed`` event per
connection (identifier, type, locality, enabled flag, whether a credential reference is set, number of profiles; never an endpoint, a reference name or a display name), in the same
transaction. A refusal, a validation error and a stale-revision conflict write no event (they raise before the write or roll it back).

CONNECTIONS. ``connections`` in a save is the complete list and replaces the stored one; absent, the stored list is kept. The list, the selection and the revision are one row: they
change together under one lock. A connection holds no credential, only the name of a backend environment variable.

LOGGING. A save logs identifiers only (the actor's public id, the revision, the mode, the profile and the policy): never a key, header, endpoint or text.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.accounts.access import MISSING_PERMISSION
from apps.accounts.authz import load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.audit.events import ACTION_AI_SETTINGS_CONNECTION_CHANGED, ACTION_AI_SETTINGS_UPDATED, SUBJECT_AI_SETTINGS, user_event
from apps.audit.recorder import record_event

from .errors import AISettingsConflictError
from .models import SETTINGS_SUBJECT_ID, SINGLETON_ID, AIExtractionSettings, connection_subject_id
from .rules import (
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_MANAGED,
    DEFAULT_VALUES,
    ISSUE_INVALID_CONFIGURATION,
    PROVIDER_ISSUES,
    ConnectionValues,
    ExtractionCatalog,
    ExtractionValues,
    ProviderStatus,
    parse_stored_connection,
    validate_connections,
    validate_values,
)

logger = logging.getLogger(__name__)

APPLY_MODE = "worker_restart_required"  # what the API tells a client: a saved change reaches a worker only when it starts

_UNSET = object()  # ``connections`` absent from a save: the stored list is left as it is


@dataclass(frozen=True)
class SettingsRecord:
    values: ExtractionValues
    stored: bool  # False: no row exists; the values are the deterministic default and each worker uses its own environment until the first save
    revision: int  # 0 when not stored
    updated_at: datetime | None = None
    updated_by_public_id: UUID | None = None
    connections: tuple = ()  # the stored connections that have a readable structure (``ConnectionValues``)
    unreadable_connections: int = 0  # stored items that do not: only a hand-edited row has any; they are neither shown nor used


@dataclass(frozen=True)
class ProviderView:
    provider_id: str
    label: str
    locality: str
    status: ProviderStatus
    profiles: tuple


@dataclass(frozen=True)
class ConnectionView:
    values: ConnectionValues
    status: ProviderStatus  # whether THIS process can use it (a fixed code, never a value)


@dataclass(frozen=True)
class ProviderTypeView:
    provider_type: str
    label: str
    localities: tuple
    needs_endpoint: bool
    credential: str
    profile_source: str
    profiles: tuple  # the fixed profiles (``ProfileEntry``) of a type whose profiles are the catalog's; empty for a type whose profiles the administrator declares


@dataclass(frozen=True)
class SettingsView:
    record: SettingsRecord
    providers: tuple
    policy_modes: tuple
    timeout_min: int
    timeout_max: int
    timeout_default: int
    apply_mode: str = APPLY_MODE
    provider_types: tuple = ()
    locality_policies: tuple = ()
    connections: tuple = ()  # ``ConnectionView``


@dataclass(frozen=True)
class StoredSettings:
    """What the worker reads: the row as stored (NOT validated here: ``config/intake_extraction.py`` validates it before use) and its revision.

    ``connections`` are the stored connections with a readable structure; the worker re-checks the SELECTED one against its catalog before using it."""

    values: ExtractionValues
    revision: int
    connections: tuple = ()


def _require_administrator(user, action: str):
    actor = load_actor(user)  # raises not_authenticated / inactive
    if not actor.is_superuser:
        log_refusal(action, MISSING_PERMISSION, actor.public_id)
        raise AuthorizationError(MISSING_PERMISSION)
    return actor


def _values_of(row) -> ExtractionValues:
    return ExtractionValues(row.extraction_mode, row.provider_id, row.profile_id, row.policy_mode, row.timeout_seconds, row.connection_id)


def _stored_connections(row) -> tuple:
    """``(connections, unreadable)``: the stored connections whose structure is readable, and how many items are not. Never raises on a damaged row."""
    raw = None if row is None else row.connections
    if raw is None:
        return (), 0
    if not isinstance(raw, list):
        return (), 1
    parsed = [parse_stored_connection(item) for item in raw]
    return tuple(c for c in parsed if c is not None), sum(1 for c in parsed if c is None)


def _record_of(row) -> SettingsRecord:
    if row is None:
        return SettingsRecord(DEFAULT_VALUES, stored=False, revision=0)
    connections, unreadable = _stored_connections(row)
    return SettingsRecord(_values_of(row), True, row.revision, row.updated_at, row.updated_by_public_id, connections, unreadable)


def _checked_status(status) -> ProviderStatus:
    if not isinstance(status, ProviderStatus) or (status.issue is not None and status.issue not in PROVIDER_ISSUES):
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    return ProviderStatus(bool(status.configured), status.issue)


def _status_of(entry) -> ProviderStatus:
    try:
        status = entry.status()
    except Exception as error:  # a probe must never break the read; the class name only, the text could hold a value
        logger.error("AI provider status probe failed (%s).", type(error).__name__)
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    return _checked_status(status)


def _connection_status(connection: ConnectionValues, catalog: ExtractionCatalog) -> ProviderStatus:
    entry = catalog.provider_type(connection.provider_type)
    if entry is None:
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    try:
        status = entry.status(connection)
    except Exception as error:  # the class name only, the text could hold a value
        logger.error("AI connection status probe failed (%s).", type(error).__name__)
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    return _checked_status(status)


def _view(row, catalog: ExtractionCatalog) -> SettingsView:
    providers = tuple(ProviderView(p.provider_id, p.label, p.locality, _status_of(p), catalog.profiles_of(p.provider_id)) for p in catalog.providers)
    record = _record_of(row)
    provider_types = tuple(
        ProviderTypeView(t.provider_type, t.label, t.localities, t.needs_endpoint, t.credential, t.profile_source, catalog.profiles_of(t.provider_type)) for t in catalog.provider_types
    )
    connections = tuple(ConnectionView(c, _connection_status(c, catalog)) for c in record.connections)
    return SettingsView(
        record, providers, catalog.policy_modes, catalog.timeout_min, catalog.timeout_max, catalog.timeout_default,
        provider_types=provider_types, locality_policies=catalog.locality_policies, connections=connections,
    )  # fmt: skip


def get_extraction_settings(*, user, catalog: ExtractionCatalog) -> SettingsView:
    _require_administrator(user, "ai_settings.read")
    return _view(AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first(), catalog)


def _connection_changes(old: tuple, new: tuple) -> list:
    """``[(change, connection)]``: what a save created, changed or removed, comparing by identifier. Pure."""
    old_by = {c.connection_id: c for c in old}
    new_ids = {c.connection_id for c in new}
    changes = []
    for connection in new:
        before = old_by.get(connection.connection_id)
        if before is None:
            changes.append(("created", connection))
        elif before != connection:
            changes.append(("updated", connection))
    changes.extend(("removed", c) for c in old if c.connection_id not in new_ids)
    return changes


def _with_stored_sources(supplied, stored: tuple):
    """``supplied`` (the submitted list) with ``credential_source`` filled in from the stored connection of the same identifier for each item that omits it. Pure."""
    if not isinstance(supplied, (list, tuple)):
        return supplied
    by_id = {c.connection_id: c for c in stored}
    merged = []
    for item in supplied:
        if isinstance(item, Mapping) and "credential_source" not in item and isinstance(item.get("connection_id"), str):
            previous = by_id.get(item["connection_id"])
            if previous is not None and previous.credential_source != CREDENTIAL_SOURCE_ENVIRONMENT:
                item = {**item, "credential_source": previous.credential_source}
        merged.append(item)
    return merged


def connections_using_managed_reference(reference: str, *, lock: bool = False) -> tuple:
    """The identifiers of the stored connections that read the managed credential ``reference`` (``credential_source`` managed). With ``lock`` the settings row is locked until the
    caller's transaction ends, so a settings save cannot add a user of the reference while the caller decides (the caller must be inside ``transaction.atomic``). Identifiers only."""
    manager = AIExtractionSettings.objects
    row = (manager.select_for_update() if lock else manager).filter(pk=SINGLETON_ID).first()
    connections, _ = _stored_connections(row)
    return tuple(c.connection_id for c in connections if c.credential_source == CREDENTIAL_SOURCE_MANAGED and c.credential_ref == reference)


def update_extraction_settings(*, user, data, expected_revision: int, catalog: ExtractionCatalog, lease_seconds: int | None = None) -> SettingsView:
    """Validate ``data`` against ``catalog`` and store it as the next revision. Raises ``AuthorizationError``, ``AISettingsValidationError`` or ``AISettingsConflictError``.

    ``data`` may carry ``connections``: the COMPLETE list of connections, which replaces the stored one (absent: the stored list is kept). The selection and the
    list are validated together and stored in one transaction under one revision."""
    actor = _require_administrator(user, "ai_settings.update")
    supplied = _UNSET
    if isinstance(data, Mapping) and "connections" in data:
        data = dict(data)
        supplied = data.pop("connections")
    effective: tuple = ()
    if supplied is not _UNSET:
        # An item that omits ``credential_source`` keeps the stored one of the same connection: a client that does not know the field can never switch a managed credential back to the
        # environment by leaving it out. Switching is explicit (``"credential_source": "environment"``). The read is unlocked; a concurrent change fails the revision check below.
        stored_now, _ = _stored_connections(AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first())
        effective = validate_connections(_with_stored_sources(supplied, stored_now), catalog)
    elif isinstance(data, Mapping) and data.get("connection_id") is not None:
        # A selection by connection needs the stored list. This read is unlocked: if the row changes before the lock below, its revision no longer equals
        # ``expected_revision`` and the save is refused as a conflict, so a selection is never accepted against a list that was not the one the client saw.
        effective, _ = _stored_connections(AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first())
    values = validate_values(data, catalog, lease_seconds=lease_seconds, connections=effective)
    with transaction.atomic():
        row = AIExtractionSettings.objects.select_for_update().filter(pk=SINGLETON_ID).first()
        current = 0 if row is None else row.revision
        if expected_revision != current:
            raise AISettingsConflictError()
        previous, _ = _stored_connections(row)
        fields = {**values.as_mapping(), "updated_by_public_id": actor.public_id}
        stored_list = [c.as_mapping() for c in effective] if supplied is not _UNSET else None
        if row is None:
            try:
                with transaction.atomic():
                    row = AIExtractionSettings.objects.create(id=SINGLETON_ID, revision=1, connections=stored_list or [], **fields)
            except IntegrityError:  # another administrator created the row between our read and our insert
                raise AISettingsConflictError() from None
        else:
            for name, value in fields.items():
                setattr(row, name, value)
            if stored_list is not None:
                row.connections = stored_list
            row.revision = current + 1
            row.save(update_fields=[*fields, *(("connections",) if stored_list is not None else ()), "revision", "updated_at"])
        now_stored, _ = _stored_connections(row)
        changes = _connection_changes(previous, now_stored) if stored_list is not None else []
        record_event(
            user_event(
                actor.public_id, ACTION_AI_SETTINGS_UPDATED, SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID,
                revision=row.revision, extraction_mode=row.extraction_mode, provider_id=row.provider_id,
                profile_id=row.profile_id, policy_mode=row.policy_mode, timeout_seconds=row.timeout_seconds,
                connection_id=row.connection_id, connection_count=len(now_stored), connections_changed=bool(changes),
            )
        )  # fmt: skip
        for change, connection in changes:
            record_event(
                user_event(
                    actor.public_id, ACTION_AI_SETTINGS_CONNECTION_CHANGED, SUBJECT_AI_SETTINGS, connection_subject_id(connection.connection_id),
                    revision=row.revision, change=change, connection_id=connection.connection_id, provider_type=connection.provider_type,
                    locality=connection.locality, enabled=connection.enabled, has_credential_ref=connection.credential_ref is not None,
                    credential_source=connection.credential_source, profile_count=len(connection.profiles),
                )
            )  # fmt: skip
    logger.info(
        "AI extraction settings saved: actor=%s revision=%s mode=%s connection=%s profile=%s policy=%s connections_changed=%s",
        actor.public_id, row.revision, row.extraction_mode, row.connection_id, row.profile_id, row.policy_mode, len(changes),
    )  # fmt: skip
    return _view(row, catalog)


def load_stored_settings() -> StoredSettings | None:
    """The stored row for the worker, or ``None`` when no row exists. A database error propagates: nothing is guessed."""
    row = AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first()
    if row is None:
        return None
    connections, _ = _stored_connections(row)
    return StoredSettings(_values_of(row), row.revision, connections)
