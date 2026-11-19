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

AUDIT. Every successful save records one append-only ``ai_settings.updated`` event (actor, time, revision, mode, provider, profile, policy, timeout; identifiers and
numbers only) with ``apps.audit.recorder.record_event`` INSIDE the save's transaction, after the row is written: the event commits with the change or is rolled back with
it, and a failed audit write rolls the change back. A refusal, a validation error and a stale-revision conflict write no event (they raise before the write or roll it back).

LOGGING. A save logs identifiers only (the actor's public id, the revision, the mode, the profile and the policy): never a key, header, endpoint or text.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.accounts.access import MISSING_PERMISSION
from apps.accounts.authz import load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.audit.events import ACTION_AI_SETTINGS_UPDATED, SUBJECT_AI_SETTINGS, user_event
from apps.audit.recorder import record_event

from .errors import AISettingsConflictError
from .models import SETTINGS_SUBJECT_ID, SINGLETON_ID, AIExtractionSettings
from .rules import DEFAULT_VALUES, ISSUE_INVALID_CONFIGURATION, PROVIDER_ISSUES, ExtractionCatalog, ExtractionValues, ProviderStatus, validate_values

logger = logging.getLogger(__name__)

APPLY_MODE = "worker_restart_required"  # what the API tells a client: a saved change reaches a worker only when it starts


@dataclass(frozen=True)
class SettingsRecord:
    values: ExtractionValues
    stored: bool  # False: no row exists; the values are the deterministic default and each worker uses its own environment until the first save
    revision: int  # 0 when not stored
    updated_at: datetime | None = None
    updated_by_public_id: UUID | None = None


@dataclass(frozen=True)
class ProviderView:
    provider_id: str
    label: str
    locality: str
    status: ProviderStatus
    profiles: tuple


@dataclass(frozen=True)
class SettingsView:
    record: SettingsRecord
    providers: tuple
    policy_modes: tuple
    timeout_min: int
    timeout_max: int
    timeout_default: int
    apply_mode: str = APPLY_MODE


@dataclass(frozen=True)
class StoredSettings:
    """What the worker reads: the row as stored (NOT validated here: ``config/intake_extraction.py`` validates it before use) and its revision."""

    values: ExtractionValues
    revision: int


def _require_administrator(user, action: str):
    actor = load_actor(user)  # raises not_authenticated / inactive
    if not actor.is_superuser:
        log_refusal(action, MISSING_PERMISSION, actor.public_id)
        raise AuthorizationError(MISSING_PERMISSION)
    return actor


def _values_of(row) -> ExtractionValues:
    return ExtractionValues(row.extraction_mode, row.provider_id, row.profile_id, row.policy_mode, row.timeout_seconds)


def _record_of(row) -> SettingsRecord:
    if row is None:
        return SettingsRecord(DEFAULT_VALUES, stored=False, revision=0)
    return SettingsRecord(_values_of(row), True, row.revision, row.updated_at, row.updated_by_public_id)


def _status_of(entry) -> ProviderStatus:
    try:
        status = entry.status()
    except Exception as error:  # a probe must never break the read; the class name only, the text could hold a value
        logger.error("AI provider status probe failed (%s).", type(error).__name__)
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    if not isinstance(status, ProviderStatus) or (status.issue is not None and status.issue not in PROVIDER_ISSUES):
        return ProviderStatus(False, ISSUE_INVALID_CONFIGURATION)
    return ProviderStatus(bool(status.configured), status.issue)


def _view(row, catalog: ExtractionCatalog) -> SettingsView:
    providers = tuple(ProviderView(p.provider_id, p.label, p.locality, _status_of(p), catalog.profiles_of(p.provider_id)) for p in catalog.providers)
    return SettingsView(_record_of(row), providers, catalog.policy_modes, catalog.timeout_min, catalog.timeout_max, catalog.timeout_default)


def get_extraction_settings(*, user, catalog: ExtractionCatalog) -> SettingsView:
    _require_administrator(user, "ai_settings.read")
    return _view(AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first(), catalog)


def update_extraction_settings(*, user, data, expected_revision: int, catalog: ExtractionCatalog, lease_seconds: int | None = None) -> SettingsView:
    """Validate ``data`` against ``catalog`` and store it as the next revision. Raises ``AuthorizationError``, ``AISettingsValidationError`` or ``AISettingsConflictError``."""
    actor = _require_administrator(user, "ai_settings.update")
    values = validate_values(data, catalog, lease_seconds=lease_seconds)
    with transaction.atomic():
        row = AIExtractionSettings.objects.select_for_update().filter(pk=SINGLETON_ID).first()
        current = 0 if row is None else row.revision
        if expected_revision != current:
            raise AISettingsConflictError()
        fields = {**values.as_mapping(), "updated_by_public_id": actor.public_id}
        if row is None:
            try:
                with transaction.atomic():
                    row = AIExtractionSettings.objects.create(id=SINGLETON_ID, revision=1, **fields)
            except IntegrityError:  # another administrator created the row between our read and our insert
                raise AISettingsConflictError() from None
        else:
            for name, value in fields.items():
                setattr(row, name, value)
            row.revision = current + 1
            row.save(update_fields=[*fields, "revision", "updated_at"])
        record_event(
            user_event(
                actor.public_id, ACTION_AI_SETTINGS_UPDATED, SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID,
                revision=row.revision, extraction_mode=row.extraction_mode, provider_id=row.provider_id,
                profile_id=row.profile_id, policy_mode=row.policy_mode, timeout_seconds=row.timeout_seconds,
            )
        )  # fmt: skip
    logger.info(
        "AI extraction settings saved: actor=%s revision=%s mode=%s profile=%s policy=%s",
        actor.public_id, row.revision, row.extraction_mode, row.profile_id, row.policy_mode,
    )  # fmt: skip
    return _view(row, catalog)


def load_stored_settings() -> StoredSettings | None:
    """The stored row for the worker, or ``None`` when no row exists. A database error propagates: nothing is guessed."""
    row = AIExtractionSettings.objects.filter(pk=SINGLETON_ID).first()
    return None if row is None else StoredSettings(_values_of(row), row.revision)
