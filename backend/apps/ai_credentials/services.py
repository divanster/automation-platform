"""The managed credential services: the administrator's create, replace, delete and status of a secret in the secret store.

AUTHORIZATION. Only an administrator (an active ``is_superuser`` user, reloaded from the database on every call) may use them, like the AI settings: no role grants it. A refusal is
``AuthorizationError(MISSING_PERMISSION)`` and is decided BEFORE the reference or the value is looked at, so a caller who is not allowed learns nothing.

NO SECRET OUT. No function here returns a value: ``get_status`` reads a secret only to learn whether it can be read and discards it. A value is wrapped in a ``SecretValue`` as soon as it is
accepted (its ``repr`` is redacted), it goes only to the secret store, and it is in no error, log line, audit event or response. Errors are codes (``errors.py``).

EXPLICIT OPERATIONS. ``create`` refuses an existing reference, ``replace`` refuses a missing one, ``delete`` removes: there is no upsert. ``delete`` is refused while a stored connection reads the
credential (``credential_in_use``), decided under the settings row's lock so a concurrent settings save cannot add a user meanwhile.

NO FALLBACK. Only the secret store of this process (``store_factory``) is used. A disabled, unavailable, busy or unreadable store is an error code; nothing is read from or written to the
environment, another store or the database.

AUDIT. Each success records ONE append-only event (``ai_credential.created``, ``.replaced``, ``.deleted``) with ``apps.audit.recorder.record_event``: the actor's public id, the time (set by
the database), an opaque subject id derived from the reference, and the store backend's identifier. No value, no reference name, no path. The event is written INSIDE the transaction, BEFORE
the secret store is changed, and the store change is the last step before the commit: a refused or failed operation rolls the event back, and a failed audit write stops the change. The secret
store is not part of the database transaction. The OUTCOME of a failed commit is therefore UNKNOWN to the process (the server may have committed and the acknowledgement been lost), so nothing is ever deleted
or overwritten to guess it: the original exception propagates unchanged and, when the secret store may have changed (the store call returned, or it was interrupted), ONE fixed ERROR line is logged with the
action, the actor's public id and the opaque subject id for the operator (``docs/architecture/secret-store.md``, section 9: reconciliation). A refused store call, an audit failure and every failure before the store
call are definite: nothing changed and nothing is logged as uncertain. A crash between the store change and the commit leaves the same state with no log line (an accepted window).

SERIALIZATION. Every create, replace and delete first takes a PostgreSQL TRANSACTION-level advisory lock keyed on the reference (``_lock_reference``), BEFORE the audit INSERT and released at commit or
rollback: two operations on the same reference run one after the other, so the audit order is the order of the secret store writes. Another reference has another key; no lock is global. A delete then
also locks the settings row, in that order only (a settings save takes only the row lock), so there is no lock cycle.

READS. The list of connections that read a credential is read INSIDE the transaction, before the store is changed, so no database read can fail after the commit and turn a committed success into an error.

RUNTIME. A change does NOT reach a running worker: it reads its credential once, at start-up (``config/intake_extraction.build_extractor_source``). The caller reports ``APPLY_MODE``.

LOGGING. Identifiers only: the actor's public id and the opaque subject id; never a reference, value, path or message of the store.
"""

import logging
from dataclasses import dataclass

from django.db import connection, transaction

from apps.accounts.access import MISSING_PERMISSION
from apps.accounts.authz import load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.ai_settings.services import connections_using_managed_reference
from apps.audit.events import (
    ACTION_AI_CREDENTIAL_CREATED,
    ACTION_AI_CREDENTIAL_DELETED,
    ACTION_AI_CREDENTIAL_REPLACED,
    SUBJECT_AI_SETTINGS,
    user_event,
)
from apps.audit.recorder import record_event
from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_BUSY,
    CODE_DISABLED,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    SecretStoreError,
    SecretValue,
)

from .errors import (
    CredentialAlreadyExistsError,
    CredentialError,
    CredentialInUseError,
    CredentialNotFoundError,
    CredentialStoreError,
    CredentialValidationError,
)
from .rules import MAX_SECRET_LENGTH, credential_lock_key, credential_subject_id, reference_problem

logger = logging.getLogger(__name__)

_BEFORE_STORE, _IN_STORE, _STORED = "before_store", "in_store", "stored"  # how far an operation got: the secret store untouched, being called, or changed

APPLY_MODE = "worker_restart_required"  # what the API tells a client: a changed credential reaches a worker only when it starts
SOURCE = "managed"

STATUS_SET = "set"
STATUS_NOT_SET = "not_set"
STATUS_UNREADABLE = "unreadable"  # a file exists but THIS process cannot decrypt it: written by another Windows identity, damaged, or moved from another machine


@dataclass(frozen=True)
class CredentialStatus:
    reference: str
    status: str  # STATUS_SET, STATUS_NOT_SET or STATUS_UNREADABLE
    store: str  # the secret store backend's identifier
    connections: tuple  # identifiers of the stored connections that read this credential


def _require_administrator(user, action: str):
    actor = load_actor(user)  # raises not_authenticated / inactive
    if not actor.is_superuser:
        log_refusal(action, MISSING_PERMISSION, actor.public_id)
        raise AuthorizationError(MISSING_PERMISSION)
    return actor


def _checked_reference(reference) -> str:
    problem = reference_problem(reference)
    if problem is not None:
        raise CredentialValidationError({"reference": [problem]})
    return reference


def _checked_value(value) -> SecretValue:
    if not isinstance(value, str):
        raise CredentialValidationError({"value": ["invalid_type"]})
    if len(value) > MAX_SECRET_LENGTH:
        raise CredentialValidationError({"value": ["too_long"]})
    try:
        return SecretValue(value)
    except SecretStoreError:
        raise CredentialValidationError({"value": ["invalid_secret"]}) from None


def _store(store_factory):
    try:
        return store_factory()
    except SecretStoreError as error:
        raise _store_error(error) from None


def _store_error(error: SecretStoreError) -> Exception:
    if error.code == CODE_NOT_FOUND:
        return CredentialNotFoundError()
    if error.code == CODE_ALREADY_EXISTS:
        return CredentialAlreadyExistsError()
    return CredentialStoreError(error.code if error.code in (CODE_DISABLED, CODE_UNAVAILABLE, CODE_BUSY, CODE_UNREADABLE) else CODE_UNAVAILABLE)


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except SecretStoreError as error:
        raise _store_error(error) from None


def _backend(store) -> str:
    backend = getattr(store, "backend_id", "custom")
    return backend if isinstance(backend, str) and backend else "custom"


def get_status(*, user, reference, store_factory) -> CredentialStatus:
    """Whether the managed credential ``reference`` is stored and readable by THIS process, and which connections read it. Reads the secret only to test it; never returns it."""
    _require_administrator(user, "ai_credential.status")
    reference = _checked_reference(reference)
    store = _store(store_factory)
    try:
        store.get(reference)  # the value is not kept: only whether it could be read
        status = STATUS_SET
    except SecretStoreError as error:
        if error.code == CODE_NOT_FOUND:
            status = STATUS_NOT_SET
        elif error.code == CODE_UNREADABLE:
            status = STATUS_UNREADABLE
        else:
            raise _store_error(error) from None
    return CredentialStatus(reference, status, _backend(store), connections_using_managed_reference(reference))


def _lock_reference(reference: str) -> None:
    """Serialize every create, replace and delete of ONE reference for the rest of the current transaction (a PostgreSQL transaction-level advisory lock, released at commit or rollback).
    It is taken BEFORE the audit INSERT, so the audit order of two operations on the same reference is the order of their secret store writes. Another reference has another key and is not
    delayed; no other lock is global. It must be called inside ``transaction.atomic()`` (outside one the lock would be released at once)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [credential_lock_key(reference)])


def _log_uncertain(action: str, actor_public_id, subject, store) -> None:
    """Record, for the operator, that the secret store MAY have changed without the database confirming the audit event. A fixed message: the action, the actor's public id, the opaque
    subject id and the backend identifier; never a reference, value, path or exception text. It must never raise, so the original exception still propagates."""
    try:
        logger.error(
            "AI credential %s: the secret store may have changed but the audit commit was not confirmed; reconcile (actor=%s subject=%s store=%s).",
            action, actor_public_id, subject, _backend(store),
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - diagnosis must not replace the failure it reports
        pass


def _write(user, reference, value, store_factory, *, replace: bool) -> CredentialStatus:
    actor = _require_administrator(user, "ai_credential.replace" if replace else "ai_credential.create")
    reference = _checked_reference(reference)
    secret = _checked_value(value)
    store = _store(store_factory)
    subject = credential_subject_id(reference)
    action = ACTION_AI_CREDENTIAL_REPLACED if replace else ACTION_AI_CREDENTIAL_CREATED
    verb = "replaced" if replace else "created"
    phase = _BEFORE_STORE
    try:
        with transaction.atomic():
            _lock_reference(reference)
            users = connections_using_managed_reference(reference)  # read in the transaction, before anything changes: there is no database read after the commit to fail
            record_event(user_event(actor.public_id, action, SUBJECT_AI_SETTINGS, subject, store=_backend(store)))
            phase = _IN_STORE
            _call(store.put, reference, secret, replace=replace)  # last: after the audit write, right before the commit
            phase = _STORED
    except BaseException as error:
        if _outcome_uncertain(phase, error):
            _log_uncertain(verb, actor.public_id, subject, store)
        raise  # the original exception and traceback, unchanged; nothing is deleted or overwritten to guess what the database did
    logger.info("AI credential %s: actor=%s subject=%s store=%s", verb, actor.public_id, subject, _backend(store))
    return CredentialStatus(reference, STATUS_SET, _backend(store), users)


def _outcome_uncertain(phase: str, error: BaseException) -> bool:
    """Whether the secret store MAY have changed although the operation failed: after the store call returned (the failure is the commit's, whose outcome is unknown), or while it ran and the
    failure is not a definite refusal of the store (an interruption). A ``CredentialError`` raised by the store call is a definite refusal: it fails before any change."""
    if phase == _STORED:
        return True
    return phase == _IN_STORE and not isinstance(error, CredentialError)


def create_credential(*, user, reference, value, store_factory) -> CredentialStatus:
    """Store a NEW managed credential. ``credential_already_exists`` when the reference is taken (nothing is overwritten). Returns the status (never the value)."""
    return _write(user, reference, value, store_factory, replace=False)


def replace_credential(*, user, reference, value, store_factory) -> CredentialStatus:
    """Replace the value of an EXISTING managed credential (a rotation). ``credential_not_found`` when there is none (nothing is created). Returns the status (never the value)."""
    return _write(user, reference, value, store_factory, replace=True)


def delete_credential(*, user, reference, store_factory) -> CredentialStatus:
    """Delete a managed credential. ``credential_not_found`` when there is none; ``credential_in_use`` while a stored connection reads it. Returns the status (``not_set``)."""
    actor = _require_administrator(user, "ai_credential.delete")
    reference = _checked_reference(reference)
    store = _store(store_factory)
    subject = credential_subject_id(reference)
    phase = _BEFORE_STORE
    try:
        with transaction.atomic():
            _lock_reference(reference)  # first: the reference's lock, then the settings row's (a settings save takes only the row lock, so the two cannot deadlock)
            users = connections_using_managed_reference(reference, lock=True)  # the settings row stays locked until this transaction ends
            if users:
                raise CredentialInUseError(users)
            record_event(user_event(actor.public_id, ACTION_AI_CREDENTIAL_DELETED, SUBJECT_AI_SETTINGS, subject, store=_backend(store)))
            phase = _IN_STORE
            _call(store.delete, reference)  # last: after the audit write, right before the commit
            phase = _STORED
    except BaseException as error:
        if _outcome_uncertain(phase, error):
            _log_uncertain("deleted", actor.public_id, subject, store)
        raise
    logger.info("AI credential deleted: actor=%s subject=%s store=%s", actor.public_id, subject, _backend(store))
    return CredentialStatus(reference, STATUS_NOT_SET, _backend(store), ())
