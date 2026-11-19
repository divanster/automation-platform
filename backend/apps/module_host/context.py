"""ModuleContext: the controlled interface through which a module reaches Core (MOD-1b S6).

A ``ModuleContext`` is built for ONE registered module from the approved static registry (``ModuleContext.open``). It is immutable, holds the module's
identity (id, version, registry fingerprint) and exposes up to four facades, each only if the module's manifest lists the capability in
``capabilities_required``: ``tasks`` (``task.enqueue``), ``cases`` (``case.run``), ``audit`` (``audit.record``) and ``permissions`` (``permissions.check``).
Every facade DELEGATES to the existing Core service, unchanged, after the module-specific checks; no business rule, queue rule, state-machine rule, audit rule
or authorization rule is written again here.

WHAT EACH FACADE ADDS TO THE SERVICE IT WRAPS (the rules themselves are ``apps.module_host.context_rules``, pure and tested without a database):

* ``tasks.enqueue`` -> ``apps.tasks.services.enqueue_task``: the task kind must be one the module declares (and not another module's), an idempotency key must
  start with a namespace the module owns, and the module must be available.
* ``cases.create_case`` / ``create_run`` / ``transition_run`` -> ``apps.cases.services``: a run is created with an explicit ``WorkflowAttribution`` built from the
  module's own declaration (module id, workflow key, DECLARED definition version; the caller supplies only the key), and a transition is refused on a run
  created under another module. The state machine is ``apps.cases.run_state``, unchanged.
* ``audit.record`` -> ``apps.audit.recorder.record_event``: only an action the module declares (never one of Core's), only its declared detail keys, only the run,
  revision, approval and rejection subject types, a run that is the module's own, and only inside the caller's ``transaction.atomic()`` (the recorder refuses
  otherwise). The vocabulary's value rules apply unchanged.
* ``permissions.require`` / ``authorize`` -> ``apps.accounts.access`` (the generic own / any / superuser rule) over ``apps.accounts.authz.load_actor``: only the
  module's own declared permissions (by their declared scope) and the Core permissions in ``context_rules.PERMITTED_CORE_PERMISSIONS``.

MODULE STATE. A state-changing call (enqueue, case writes, audit) is refused unless the module is AVAILABLE by the host's one rule
(``rules.assess_modules``: registered, state present once the first sync has completed, enabled, version-matched, dependencies available; before the first sync a
registered module counts as enabled, the documented backward-compatibility transition). The check runs inside a transaction (the facade opens one, which joins the
caller's) AFTER taking a shared row lock on every ``ModuleState`` row (``FOR SHARE``): concurrent calls do not block each other, but ``module_enable``,
``module_disable`` and ``module_sync`` (which lock the rows ``FOR UPDATE``) wait for an in-flight call to finish, and a call that starts after a disable commits
sees it. ``permissions`` only reads, so it is not gated by module state.

EXPLICIT ACTORS. Nothing here reads an ambient user (no request, no thread-local, no setting). Every method that records or decides about a person takes the
user as an argument, and the user is re-read from the database by Core (``load_actor``): a caller can never supply a public id. A system event is recorded only
by passing the explicit ``SYSTEM_ACTOR`` sentinel.

TRUST LIMITS (docs/architecture/module-contract.md, section 3.13). A context is a naming and authority boundary inside one process, not a sandbox: code
admitted to the process can import Core services directly. The context exposes no database connection, no cursor, no registry or manifest mutation and no
filesystem access, and imports nothing by name, but it cannot stop module code from doing those things by other means. Request Intake is a registered module since MOD-2, but it
does not use a context: its submission path and services are unchanged (a test pins that).
"""

import uuid
from collections.abc import Mapping
from types import MappingProxyType

from django.db import connection, transaction
from django.db.transaction import TransactionManagementError

from apps.accounts import access as core_access
from apps.accounts.authz import Actor, load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.audit import events
from apps.audit.recorder import AuditTransactionError, record_event
from apps.cases import services as case_services
from apps.cases.attribution import WorkflowAttribution
from apps.cases.models import WorkflowRun
from apps.modules.registry import ModuleRegistry
from apps.tasks.policy import DEFAULT_MAX_ATTEMPTS
from apps.tasks.services import EnqueueResult, enqueue_task

from . import context_rules, rules
from .errors import ModuleContextError, ModuleHostError
from .models import ModuleState
from .services import read_host_state

_TOKEN = object()


class _SystemActor:
    """The explicit "the system did this" actor for ``audit.record``. A sentinel, never a user: there is exactly one and it carries no identity."""

    __slots__ = ()

    def __repr__(self):
        return "SYSTEM_ACTOR"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


SYSTEM_ACTOR = _SystemActor()


def _share_lock_state_rows() -> None:
    """Share-lock every ``ModuleState`` row until the surrounding transaction ends (``FOR SHARE``, module-id order, like the host's own ``FOR UPDATE``)."""
    if not connection.in_atomic_block:
        raise TransactionManagementError("The module state must be read inside a transaction.")
    table = connection.ops.quote_name(ModuleState._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT module_id FROM {table} ORDER BY module_id FOR SHARE")  # a fixed statement: the table name is the model's, no value is interpolated


def _check_own_run(manifest, run_id) -> None:
    """Refuse unless ``run_id`` is a run created under ``manifest``'s module. A run that does not exist, a malformed id and another module's run are
    refused IDENTICALLY (``run_not_owned``, one message), so a module with no authority over a run cannot tell whether it exists.

    It reads the run's module id WITHOUT a lock: the attribution is immutable (``apps.cases.models.WorkflowRun``), so there is nothing to protect, and a module
    never takes a row lock on a run that is not its own (it could otherwise hold up another module's run until its transaction ends).
    """
    owner = None
    if isinstance(run_id, uuid.UUID):
        owner = WorkflowRun.objects.filter(pk=run_id).values_list("module_id", flat=True).first()
    context_rules.check_run_owner(manifest, owner)


class _Frozen:
    """Immutable, and final: a class outside this module cannot subclass it to change how identity or a capability is read."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs):
        if cls.__module__ != __name__:
            raise TypeError(f"{cls.__qualname__}: the ModuleContext and its facades cannot be subclassed")
        super().__init_subclass__(**kwargs)

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name):
        raise AttributeError(f"{type(self).__name__} is immutable")


class ModuleContext(_Frozen):
    """The identity of one registered module and the Core capabilities its manifest declares. Immutable; build it with ``ModuleContext.open``."""

    __slots__ = ("_registry", "_manifest", "_handler_registry")

    def __init__(self, token, registry, manifest, handler_registry):
        if token is not _TOKEN:
            raise TypeError("build a ModuleContext with ModuleContext.open(registry, module_id)")
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "_handler_registry", handler_registry)

    @classmethod
    def open(cls, registry, module_id, *, handler_registry=None) -> "ModuleContext":
        """The context of ``module_id`` in ``registry`` (the approved static registry). Reads no database: identity comes from the registry alone.

        Raises ``ModuleContextError`` for something that is not a registry (``invalid_registry``), a malformed id (``invalid_module_id``) or a module the registry
        does not contain (``unknown_module``). ``handler_registry`` is the task handler allowlist ``enqueue`` validates against
        (``apps.tasks.services.enqueue_task``'s ``registry``); ``None`` means the queue's own default.
        """
        if not isinstance(registry, ModuleRegistry):
            raise ModuleContextError("invalid_registry", "the registry must be an apps.modules.registry.ModuleRegistry")
        if handler_registry is not None and not isinstance(handler_registry, Mapping):
            raise ModuleContextError("invalid_registry", "the handler registry must be a mapping of task kind to handler")
        try:
            manifest = rules.registered(registry, module_id)
        except ModuleHostError as error:
            raise ModuleContextError(error.code, str(error)) from None
        return cls(_TOKEN, registry, manifest, None if handler_registry is None else MappingProxyType(dict(handler_registry)))

    # --- identity (immutable metadata) ---------------------------------------------------------------------------------

    @property
    def module_id(self) -> str:
        return self._manifest.module_id

    @property
    def module_version(self) -> str:
        return self._manifest.module_version

    @property
    def registry_fingerprint(self) -> str:
        return self._registry.fingerprint

    @property
    def capabilities(self) -> frozenset:
        """The Core capabilities this module declares AND Core has a facade for."""
        return frozenset(self._manifest.capabilities_required) & context_rules.FACADE_CAPABILITIES

    @property
    def manifest(self):
        """The module's own manifest (a frozen object)."""
        return self._manifest

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def __repr__(self):
        return f"<ModuleContext {self.module_id} {self.module_version} {self.registry_fingerprint[:12]}>"

    # --- the facades ---------------------------------------------------------------------------------------------------

    @property
    def tasks(self) -> "TaskCapability":
        context_rules.check_capability(self._manifest, context_rules.CAP_TASK_ENQUEUE)
        return TaskCapability(self)

    @property
    def cases(self) -> "CaseCapability":
        context_rules.check_capability(self._manifest, context_rules.CAP_CASE_RUN)
        return CaseCapability(self)

    @property
    def audit(self) -> "AuditCapability":
        context_rules.check_capability(self._manifest, context_rules.CAP_AUDIT_RECORD)
        return AuditCapability(self)

    @property
    def permissions(self) -> "PermissionCapability":
        context_rules.check_capability(self._manifest, context_rules.CAP_PERMISSIONS_CHECK)
        return PermissionCapability(self)

    # --- module state --------------------------------------------------------------------------------------------------

    def _require_available(self) -> None:
        """Refuse unless the module is available now. Call inside a transaction: it takes the shared state lock first, then reads the state."""
        _share_lock_state_rows()
        state = read_host_state()
        verdict = rules.assess_modules(self._registry, state.facts, initialized=state.initialized)[self.module_id]
        context_rules.check_available(self.module_id, verdict)


class _Facade(_Frozen):
    __slots__ = ("_context",)

    def __init__(self, context):
        object.__setattr__(self, "_context", context)

    def __repr__(self):
        return f"<{type(self).__name__} of {self._context.module_id}>"


# --- task.enqueue ---------------------------------------------------------------------------------------------------------------


class TaskCapability(_Facade):
    __slots__ = ()

    def enqueue(self, *, kind, payload, idempotency_key=None, max_attempts=DEFAULT_MAX_ATTEMPTS, available_at=None, now=None) -> EnqueueResult:
        """``apps.tasks.services.enqueue_task`` for a task kind this module declares. Same result, same idempotency, same errors.

        The kind must be declared by the module (``task_kind_not_declared``, or ``task_kind_not_owned`` when another module owns it), a given idempotency key must
        start with a namespace the module owns (``idempotency_key_not_namespaced``), and the module must be available. The payload is validated by the kind's handler
        exactly as before. Claiming, leases, retries and recovery are not touched.
        """
        context = self._context
        context_rules.check_task_kind(context._registry, context._manifest, kind)
        context_rules.check_idempotency_key(context._manifest, idempotency_key)
        with transaction.atomic():  # joins the caller's transaction (a savepoint); holds the shared state lock until it ends
            context._require_available()
            return enqueue_task(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                available_at=available_at,
                now=now,
                registry=context._handler_registry,
            )


# --- case.run -------------------------------------------------------------------------------------------------------------------


class CaseCapability(_Facade):
    __slots__ = ()

    def create_case(self, *, reference, case_id=None):
        """``apps.cases.services.create_case``."""
        with transaction.atomic():
            self._context._require_available()
            return case_services.create_case(reference=reference, case_id=case_id)

    def create_run(self, *, case_id, workflow_key, run_id=None):
        """``apps.cases.services.create_run`` for a workflow this module declares, with an explicit ``WorkflowAttribution``.

        The attribution is the module's id, the workflow key and the workflow's DECLARED definition version; the caller cannot choose another module or version.
        """
        manifest = self._context._manifest
        declaration = context_rules.declared_workflow(manifest, workflow_key)
        attribution = WorkflowAttribution(manifest.module_id, declaration.workflow_key, declaration.definition_version)
        with transaction.atomic():
            self._context._require_available()
            return case_services.create_run(case_id=case_id, attribution=attribution, run_id=run_id)

    def transition_run(self, run_id, *, to, failed_step="", allowed_from=None):
        """``apps.cases.services.transition_run`` on a run created under this module. The transition table is Core's, unchanged.

        A run that is not this module's, one that does not exist and a malformed id are all ``run_not_owned`` with the same message: no existence is disclosed.
        """
        with transaction.atomic():
            self._context._require_available()  # first: a disabled module learns nothing about any run
            _check_own_run(self._context._manifest, run_id)  # then ownership, unlocked and without disclosing existence
            return case_services.transition_run(run_id, to=to, failed_step=failed_step, allowed_from=allowed_from)  # the service takes the run lock itself


# --- audit.record ---------------------------------------------------------------------------------------------------------------


def _resolve_actor(actor):
    """``None`` for the explicit system actor, else the freshly loaded ``Actor``. No ambient identity: ``None`` and anything else that is not a user is refused."""
    if actor is SYSTEM_ACTOR:
        return None
    if actor is None:
        raise ModuleContextError("actor_required", "name the actor: a user, or SYSTEM_ACTOR for the system")
    return load_actor(actor)  # AuthorizationError for something that is not an authenticated, active user; the stored row is judged, not the object


class AuditCapability(_Facade):
    __slots__ = ()

    def record(self, *, actor, action, subject_type, subject_id, case_id=None, run_id=None, **details):
        """``apps.audit.recorder.record_event`` for an action this module declares. Must be called inside the caller's ``transaction.atomic()``.

        ``actor`` is a user (re-read from the database; their public id becomes the event's actor) or ``SYSTEM_ACTOR``. ``details`` may hold only the keys the
        declaration lists, with the audit vocabulary's value rules (identifiers, numbers, booleans, short tokens; never content). When ``run_id`` is given, or the
        subject is a run, that run must have been created under this module.
        """
        context = self._context
        context_rules.check_audit_event(context._manifest, events.ACTIONS, action, subject_type, details)
        if not connection.in_atomic_block:
            raise AuditTransactionError("An audit event must be recorded inside the transaction of the operation it describes.")
        context._require_available()  # inside the caller's transaction; a disabled module records nothing, whatever else is wrong with the call
        resolved = _resolve_actor(actor)
        if resolved is None:
            event = events.system_event(action, subject_type, subject_id, case_id=case_id, run_id=run_id, **details)
        else:
            event = events.user_event(resolved.public_id, action, subject_type, subject_id, case_id=case_id, run_id=run_id, **details)
        for run in {run_id, subject_id if subject_type == events.SUBJECT_RUN else None} - {None}:
            _check_own_run(context._manifest, run)  # after the actor: an actor that is refused learns nothing about any run
        return record_event(event)


# --- permissions.check ----------------------------------------------------------------------------------------------------------


class PermissionCapability(_Facade):
    __slots__ = ()

    def _rule(self, permission_ids):
        any_set, own_set = context_rules.access_sets(self._context._manifest, permission_ids)
        return core_access.AccessRule(any_permissions=any_set, own_permissions=own_set)

    def _label(self, permission_ids) -> str:
        return f"{self._context.module_id}:" + "+".join(sorted(permission_ids))

    def require(self, user, *permission_ids) -> Actor:
        """The permission part only: the freshly loaded actor if they hold ANY of the named permissions (any scope), else ``AuthorizationError``.

        Call it BEFORE looking the record up, so a user with no permission learns nothing about whether the record exists (the order Request Intake's services use).
        """
        rule = self._rule(permission_ids)
        actor = load_actor(user)
        reason = core_access.permission_problem(rule, is_superuser=actor.is_superuser, permissions=actor.permissions)
        if reason is not None:
            log_refusal(self._label(permission_ids), reason, actor.public_id)
            raise AuthorizationError(reason)
        return actor

    def authorize(self, user, *permission_ids, owner_pk=None) -> Actor:
        """Both parts, permission first: the actor, freshly read, if they may act on a record whose owner is ``owner_pk`` (``None``: no known owner).

        Core's own / any / superuser rule, unchanged: a superuser passes; holding an ``any`` (or ``global``) permission passes; holding only an ``own`` permission
        passes only for the actor's own record (``out_of_scope`` otherwise); holding none is ``missing_permission``, whoever the owner is.
        """
        rule = self._rule(permission_ids)
        actor = load_actor(user)
        reason = core_access.access_problem(rule, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=owner_pk)
        if reason is not None:
            log_refusal(self._label(permission_ids), reason, actor.public_id)
            raise AuthorizationError(reason)
        return actor
