"""DB-free tests for the ModuleContext (MOD-1b S6): construction, identity, capability gating, module state enforcement, delegation, actors and boundaries.

Nothing here opens a database connection. The one place the context reads state (``read_host_state``), takes its shared row lock, opens a transaction and
reaches a Core service is replaced by a recording stand-in, so the tests check ORDER and ARGUMENTS: what is refused before anything is delegated, and exactly what
is passed to the existing service when nothing is refused. What needs a real PostgreSQL (the lock, the transaction, the services themselves, real users,
concurrency) is in tests/test_module_context_postgres.py and is NOT run in the development VM.

This file is ASCII only.
"""

import ast
import contextlib
import copy
import inspect
import uuid
from pathlib import Path
from unittest import mock

from django.db.transaction import TransactionManagementError
from django.test import SimpleTestCase

from apps.accounts.authz import Actor
from apps.accounts.errors import AuthorizationError
from apps.audit.recorder import AuditTransactionError
from apps.cases.attribution import WorkflowAttribution
from apps.cases.run_state import RunState
from apps.module_host import context as ctx
from apps.module_host import context_rules, rules
from apps.module_host.context import SYSTEM_ACTOR, ModuleContext
from apps.module_host.errors import ModuleContextError
from apps.module_host.rules import StateFacts
from apps.module_host.services import HostState
from tests import module_context_support as support

BACKEND = Path(__file__).resolve().parent.parent
RUN_ID, CASE_ID, SUBJECT_ID = uuid.UUID(int=0xA1), uuid.UUID(int=0xC1), uuid.UUID(int=0xB1)
USER_PUBLIC_ID = uuid.UUID(int=0xE1)
REAL_LOCK = ctx._share_lock_state_rows  # captured before any test replaces it


def facts_for(registry, **changes):
    """Enabled, version-matched state for every module; ``changes`` maps a module id to replacement keyword arguments."""
    facts = {}
    for manifest in registry.modules:
        values = {"installed_version": manifest.module_version, "enabled": True, "registry_fingerprint": registry.fingerprint}
        values.update(changes.get(manifest.module_id, {}))
        facts[manifest.module_id] = StateFacts(manifest.module_id, **values)
    return facts


def actor(pk=1, *, permissions=(), superuser=False, public_id=USER_PUBLIC_ID):
    return Actor(user=mock.Mock(), pk=pk, public_id=public_id, is_superuser=superuser, permissions=frozenset(permissions))


class ContextCase(SimpleTestCase):
    """A registry of two synthetic modules and the stand-ins for everything that would reach the database."""

    def setUp(self):
        self.registry = support.registry()
        self.facts = facts_for(self.registry)
        self.initialized = True
        self.calls = []
        patches = [
            mock.patch.object(ctx, "_share_lock_state_rows", side_effect=lambda: self.calls.append("lock")),
            mock.patch.object(ctx, "read_host_state", side_effect=lambda: HostState(self.facts, self.initialized)),
            mock.patch.object(ctx.transaction, "atomic", side_effect=lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(ctx, "connection", mock.Mock(in_atomic_block=True)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.context = ModuleContext.open(self.registry, support.CTX)

    def refused(self, code, function, *args, **kwargs):
        with self.assertRaises(ModuleContextError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception


# --- construction and identity -------------------------------------------------------------------------------------------------


class ConstructionTests(ContextCase):
    def test_the_context_carries_the_registered_identity(self):
        manifest = self.registry.get(support.CTX)
        self.assertEqual(self.context.module_id, support.CTX)
        self.assertEqual(self.context.module_version, manifest.module_version)
        self.assertEqual(self.context.registry_fingerprint, self.registry.fingerprint)
        self.assertIs(self.context.manifest, manifest)
        self.assertEqual(self.context.capabilities, set(support.ALL_CAPABILITIES))
        self.assertIn(support.CTX, repr(self.context))

    def test_opening_a_context_reads_no_state(self):
        with mock.patch.object(ctx, "read_host_state") as read, mock.patch.object(ctx, "_share_lock_state_rows") as lock:
            ModuleContext.open(self.registry, support.OTHER)
        read.assert_not_called()
        lock.assert_not_called()

    def test_an_unknown_module_is_refused_including_request_intake(self):
        for module_id in ("request_intake", "nobody_mod", "audit"):
            with self.subTest(module_id=module_id):
                self.refused("unknown_module", ModuleContext.open, self.registry, module_id)

    def test_a_malformed_module_id_is_refused(self):
        for module_id in ("Bad", "ab", "x" * 41, "has space", "", None, 5, b"ctx_mod"):
            with self.subTest(module_id=module_id):
                self.refused("invalid_module_id", ModuleContext.open, self.registry, module_id)

    def test_only_a_real_registry_is_accepted(self):
        for registry in (None, {}, [support.ctx_module()], "registry", object()):
            with self.subTest(registry=registry):
                self.refused("invalid_registry", ModuleContext.open, registry, support.CTX)

    def test_the_handler_registry_must_be_a_mapping(self):
        self.refused("invalid_registry", ModuleContext.open, self.registry, support.CTX, handler_registry=["ctx_mod.work"])
        self.assertIsNotNone(ModuleContext.open(self.registry, support.CTX, handler_registry={}))

    def test_a_context_cannot_be_constructed_directly(self):
        manifest = self.registry.get(support.CTX)
        with self.assertRaises(TypeError):
            ModuleContext(object(), self.registry, manifest, None)
        with self.assertRaises(TypeError):
            ModuleContext(None, self.registry, manifest, None)

    def test_the_context_and_its_facades_are_immutable(self):
        for target in (self.context, self.context.tasks, self.context.cases, self.context.audit, self.context.permissions):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(AttributeError):
                    target.module_id = "other_mod"
                with self.assertRaises(AttributeError):
                    target.new_attribute = 1
                with self.assertRaises(AttributeError):
                    delattr(target, "_context" if hasattr(target, "_context") else "_registry")
        with self.assertRaises(AttributeError):
            self.context._manifest = self.registry.get(support.OTHER)

    def test_the_handler_registry_is_copied_read_only(self):
        handlers = {support.KIND: object()}
        context = ModuleContext.open(self.registry, support.CTX, handler_registry=handlers)
        handlers["x.y"] = object()
        self.assertNotIn("x.y", context._handler_registry)
        with self.assertRaises(TypeError):
            context._handler_registry["x.y"] = object()

    def test_two_modules_get_two_different_contexts(self):
        other = ModuleContext.open(self.registry, support.OTHER)
        self.assertEqual((self.context.module_id, other.module_id), (support.CTX, support.OTHER))
        self.assertIsNot(self.context.manifest, other.manifest)

    def test_the_context_exposes_no_connection_cursor_or_way_out(self):
        public = {name for name in dir(self.context) if not name.startswith("_")}
        self.assertEqual(public, {"module_id", "module_version", "registry_fingerprint", "capabilities", "manifest", "has_capability", "tasks", "cases", "audit", "permissions", "open"})
        for facade in (self.context.tasks, self.context.cases, self.context.audit, self.context.permissions):
            exposed = {name for name in dir(facade) if not name.startswith("_")}
            for banned in ("connection", "cursor", "registry", "execute", "raw", "objects", "module_id", "manifest", "context"):
                self.assertNotIn(banned, exposed, f"{type(facade).__name__}.{banned}")


# --- capability declaration ---------------------------------------------------------------------------------------------------


class CapabilityGatingTests(ContextCase):
    def open(self, **overrides):
        registry = support.registry(support.ctx_module(**overrides), support.other_module())
        return ModuleContext.open(registry, support.CTX)

    def test_an_undeclared_capability_cannot_be_reached(self):
        context = self.open(capabilities_required=("task.enqueue",))
        self.assertEqual(context.capabilities, {"task.enqueue"})
        self.assertIsNotNone(context.tasks)
        for name, capability in (("cases", "case.run"), ("audit", "audit.record"), ("permissions", "permissions.check")):
            with self.subTest(capability=capability):
                self.refused("capability_not_declared", getattr, context, name)
                self.assertFalse(context.has_capability(capability))

    def test_a_module_that_declares_nothing_reaches_nothing(self):
        context = self.open(capabilities_required=())
        self.assertEqual(context.capabilities, frozenset())
        for name in ("tasks", "cases", "audit", "permissions"):
            self.refused("capability_not_declared", getattr, context, name)

    def test_revision_approval_has_no_facade_even_when_declared(self):
        context = self.open(capabilities_required=(*support.ALL_CAPABILITIES, "revision.approval"))
        self.assertNotIn("revision.approval", context.capabilities)
        self.assertEqual(context.capabilities, set(support.ALL_CAPABILITIES))
        self.assertFalse(hasattr(context, "revisions"))
        self.assertFalse(hasattr(context, "approvals"))

    def test_the_facade_names_are_the_four_capabilities_and_nothing_else(self):
        facade_properties = {n for n, v in inspect.getmembers(ModuleContext, lambda x: isinstance(x, property)) if n in ("tasks", "cases", "audit", "permissions")}
        self.assertEqual(facade_properties, {"tasks", "cases", "audit", "permissions"})


# --- module state enforcement ---------------------------------------------------------------------------------------------------


class StateEnforcementTests(ContextCase):
    """Every state-changing method is refused for an unavailable module, and nothing is delegated."""

    def state_changing_calls(self):
        return {
            "tasks.enqueue": lambda c: c.tasks.enqueue(kind=support.KIND, payload={}),
            "cases.create_case": lambda c: c.cases.create_case(reference="R"),
            "cases.create_run": lambda c: c.cases.create_run(case_id=CASE_ID, workflow_key=support.FLOW),
            "cases.transition_run": lambda c: c.cases.transition_run(RUN_ID, to=RunState.EXTRACTING),
            "audit.record": lambda c: c.audit.record(actor=SYSTEM_ACTOR, action=support.ACTION, subject_type="revision", subject_id=SUBJECT_ID),
        }

    def patched_services(self):
        stack = contextlib.ExitStack()
        self.delegates = {
            "enqueue": stack.enter_context(mock.patch.object(ctx, "enqueue_task")),
            "create_case": stack.enter_context(mock.patch.object(ctx.case_services, "create_case")),
            "create_run": stack.enter_context(mock.patch.object(ctx.case_services, "create_run")),
            "lock_run": stack.enter_context(mock.patch.object(ctx.case_services, "lock_run")),
            "transition_run": stack.enter_context(mock.patch.object(ctx.case_services, "transition_run")),
            "record_event": stack.enter_context(mock.patch.object(ctx, "record_event")),
        }
        self.addCleanup(stack.close)

    def assert_nothing_delegated(self):
        for name, delegate in self.delegates.items():
            delegate.assert_not_called()

    def test_a_disabled_module_is_refused_everywhere(self):
        self.patched_services()
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        for name, call in self.state_changing_calls().items():
            with self.subTest(call=name):
                self.refused("module_disabled", call, self.context)
        self.assert_nothing_delegated()

    def test_a_version_mismatch_is_refused_everywhere(self):
        self.patched_services()
        self.facts = facts_for(self.registry, **{support.CTX: {"installed_version": "0.0.1"}})
        for name, call in self.state_changing_calls().items():
            with self.subTest(call=name):
                self.refused("module_version_mismatch", call, self.context)
        self.assert_nothing_delegated()

    def test_missing_state_after_the_first_sync_is_refused_everywhere(self):
        self.patched_services()
        self.facts = {module_id: fact for module_id, fact in self.facts.items() if module_id != support.CTX}
        self.initialized = True
        for name, call in self.state_changing_calls().items():
            with self.subTest(call=name):
                self.refused("module_state_missing", call, self.context)
        self.assert_nothing_delegated()

    def test_before_the_first_sync_a_registered_module_counts_as_enabled(self):
        """The documented backward-compatibility transition (rules.assess_modules): no state and no marker means available."""
        self.patched_services()
        self.facts, self.initialized = {}, False
        self.context.tasks.enqueue(kind=support.KIND, payload={})
        self.delegates["enqueue"].assert_called_once()

    def test_an_unavailable_dependency_is_refused(self):
        registry = support.registry(support.ctx_module(), support.dependent(capabilities_required=("task.enqueue",)))
        self.patched_services()
        self.facts = facts_for(registry, **{support.CTX: {"enabled": False}})
        # the dependent declares no task kind, so exercise the state rule through the host's own assessment
        verdict = rules.assess_modules(registry, self.facts, initialized=True)["dep_mod"]
        self.refused("module_dependency_unavailable", context_rules.check_available, "dep_mod", verdict)

    def test_one_modules_state_does_not_affect_another(self):
        self.patched_services()
        self.facts = facts_for(self.registry, **{support.OTHER: {"enabled": False}})
        self.context.tasks.enqueue(kind=support.KIND, payload={})
        self.delegates["enqueue"].assert_called_once()
        other = ModuleContext.open(self.registry, support.OTHER)
        self.refused("module_disabled", other.tasks.enqueue, kind=support.OTHER_KIND, payload={})

    def test_the_lock_is_taken_before_the_state_is_read(self):
        self.patched_services()
        order = []
        with mock.patch.object(ctx, "_share_lock_state_rows", side_effect=lambda: order.append("lock")), mock.patch.object(
            ctx, "read_host_state", side_effect=lambda: order.append("read") or HostState(self.facts, True)
        ):
            self.context.tasks.enqueue(kind=support.KIND, payload={})
        self.assertEqual(order, ["lock", "read"])

    def test_the_state_is_checked_inside_the_transaction_before_the_service_runs(self):
        self.patched_services()
        events = []
        real_atomic = lambda *a, **k: _Recorder(events)  # noqa: E731
        with mock.patch.object(ctx.transaction, "atomic", side_effect=real_atomic), mock.patch.object(
            ctx, "_share_lock_state_rows", side_effect=lambda: events.append("lock")
        ):
            self.delegates["enqueue"].side_effect = lambda **k: events.append("enqueue")
            self.context.tasks.enqueue(kind=support.KIND, payload={})
        self.assertEqual(events, ["enter", "lock", "enqueue", "exit"])

    def test_a_state_refusal_leaves_the_transaction_to_roll_back(self):
        self.patched_services()
        events = []
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        with mock.patch.object(ctx.transaction, "atomic", side_effect=lambda *a, **k: _Recorder(events)):
            self.refused("module_disabled", self.context.tasks.enqueue, kind=support.KIND, payload={})
        self.assertEqual(events, ["enter", "exit-error"])

    def test_the_lock_helper_refuses_to_run_outside_a_transaction(self):
        with mock.patch.object(ctx, "connection", mock.Mock(in_atomic_block=False)):
            with self.assertRaises(TransactionManagementError):
                REAL_LOCK()

    def test_the_lock_statement_is_one_fixed_shared_lock_over_the_state_table(self):
        cursor = mock.MagicMock()
        fake_connection = mock.MagicMock()
        fake_connection.in_atomic_block = True
        fake_connection.cursor.return_value.__enter__.return_value = cursor
        fake_connection.ops.quote_name.side_effect = lambda name: f'"{name}"'
        with mock.patch.object(ctx, "connection", fake_connection):
            REAL_LOCK()
        (statement,), kwargs = cursor.execute.call_args
        self.assertEqual(statement, 'SELECT module_id FROM "module_host_modulestate" ORDER BY module_id FOR SHARE')


class _Recorder:
    """A transaction stand-in that records entering and leaving, and whether it left with an error."""

    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("enter")

    def __exit__(self, exc_type, exc, tb):
        self.events.append("exit-error" if exc_type else "exit")
        return False


# --- task.enqueue -------------------------------------------------------------------------------------------------------------


class TaskCapabilityTests(ContextCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ctx, "enqueue_task")
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_declared_kind_is_delegated_to_enqueue_task_with_the_same_arguments(self):
        handlers = {support.KIND: object()}
        context = ModuleContext.open(self.registry, support.CTX, handler_registry=handlers)
        when = mock.sentinel.when
        result = context.tasks.enqueue(kind=support.KIND, payload={"a": 1}, idempotency_key="ctx_mod.work:1", max_attempts=2, available_at=when, now=when)
        self.assertIs(result, self.enqueue.return_value)
        (), kwargs = self.enqueue.call_args
        self.assertEqual({k: v for k, v in kwargs.items() if k != "registry"}, {"kind": support.KIND, "payload": {"a": 1}, "idempotency_key": "ctx_mod.work:1", "max_attempts": 2, "available_at": when, "now": when})
        self.assertEqual(dict(kwargs["registry"]), handlers)

    def test_defaults_are_the_queues_own(self):
        from apps.tasks.policy import DEFAULT_MAX_ATTEMPTS

        self.context.tasks.enqueue(kind=support.KIND, payload={})
        kwargs = self.enqueue.call_args.kwargs
        self.assertEqual((kwargs["idempotency_key"], kwargs["max_attempts"], kwargs["available_at"], kwargs["now"], kwargs["registry"]), (None, DEFAULT_MAX_ATTEMPTS, None, None, None))

    def test_the_service_errors_pass_through_unchanged(self):
        error = RuntimeError("from the queue")
        self.enqueue.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.context.tasks.enqueue(kind=support.KIND, payload={})
        self.assertIs(caught.exception, error)

    def test_another_modules_kind_a_legacy_kind_and_an_undeclared_kind_are_refused_before_any_state_read(self):
        for kind, code in (
            (support.OTHER_KIND, "task_kind_not_owned"),
            ("test.add_numbers", "task_kind_not_declared"),
            ("request_intake.extract", "task_kind_not_declared"),
            ("ctx_mod.unknown", "task_kind_not_declared"),
        ):
            with self.subTest(kind=kind):
                self.refused(code, self.context.tasks.enqueue, kind=kind, payload={})
        self.enqueue.assert_not_called()
        self.assertEqual(self.calls, [])  # no lock was taken: the pure rules refuse first

    def test_an_idempotency_key_outside_the_namespace_is_refused_before_any_state_read(self):
        for key in ("other_mod.work:1", "request_intake.extract:1", "plain", ""):
            with self.subTest(key=key):
                self.refused("idempotency_key_not_namespaced", self.context.tasks.enqueue, kind=support.KIND, payload={}, idempotency_key=key)
        self.enqueue.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_the_module_never_changes_the_kind_or_the_key_it_passes(self):
        self.context.tasks.enqueue(kind=support.KIND, payload={}, idempotency_key="intake.extract:9")  # an owned extra namespace is fine
        self.assertEqual(self.enqueue.call_args.kwargs["idempotency_key"], "intake.extract:9")


# --- case.run -----------------------------------------------------------------------------------------------------------------


class CaseCapabilityTests(ContextCase):
    def setUp(self):
        super().setUp()
        for name in ("create_case", "create_run", "lock_run", "transition_run"):
            patcher = mock.patch.object(ctx.case_services, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ctx.WorkflowRun, "objects")
        self.runs = patcher.start()
        self.addCleanup(patcher.stop)
        self.set_run_owner(support.CTX)

    def set_run_owner(self, owner):
        """What the unlocked ownership read finds: a module id, or None when there is no such run."""
        self.runs.filter.return_value.values_list.return_value.first.return_value = owner

    def test_create_case_is_delegated(self):
        result = self.context.cases.create_case(reference="REF-1", case_id=CASE_ID)
        self.assertIs(result, self.create_case.return_value)
        self.create_case.assert_called_once_with(reference="REF-1", case_id=CASE_ID)

    def test_a_run_is_created_with_an_explicit_attribution_at_the_declared_version(self):
        result = self.context.cases.create_run(case_id=CASE_ID, workflow_key=support.FLOW, run_id=RUN_ID)
        self.assertIs(result, self.create_run.return_value)
        self.create_run.assert_called_once()
        kwargs = self.create_run.call_args.kwargs
        self.assertEqual(kwargs["case_id"], CASE_ID)
        self.assertEqual(kwargs["run_id"], RUN_ID)
        self.assertEqual(kwargs["attribution"], WorkflowAttribution(support.CTX, support.FLOW, support.FLOW_VERSION))
        self.assertEqual(support.FLOW_VERSION, 3)

    def test_the_caller_cannot_choose_the_module_or_the_version(self):
        for extra in ({"module_id": "other_mod"}, {"workflow_version": 1}, {"attribution": WorkflowAttribution("other_mod", "other_mod.flow", 1)}):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                self.context.cases.create_run(case_id=CASE_ID, workflow_key=support.FLOW, **extra)
        self.create_run.assert_not_called()

    def test_an_undeclared_workflow_is_refused_before_any_state_read(self):
        for key in ("other_mod.flow", "ctx_mod.unknown", "request_intake", "ctx_mod"):
            with self.subTest(key=key):
                self.refused("workflow_not_declared", self.context.cases.create_run, case_id=CASE_ID, workflow_key=key)
        self.create_run.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_an_own_run_is_transitioned_through_the_service_after_state_and_owner_checks(self):
        order = []
        with mock.patch.object(ctx, "_share_lock_state_rows", side_effect=lambda: order.append("state-lock")):
            self.runs.filter.side_effect = lambda **k: order.append("owner-read") or mock.DEFAULT
            self.transition_run.side_effect = lambda *a, **k: order.append("transition")
            self.context.cases.transition_run(RUN_ID, to=RunState.EXTRACTING, failed_step="", allowed_from=(RunState.QUEUED,))
        self.assertEqual(order, ["state-lock", "owner-read", "transition"])
        self.transition_run.assert_called_once_with(RUN_ID, to=RunState.EXTRACTING, failed_step="", allowed_from=(RunState.QUEUED,))
        self.runs.filter.assert_called_once_with(pk=RUN_ID)

    def test_a_transition_on_another_modules_run_is_refused_and_nothing_changes(self):
        for owner in ("other_mod", "request_intake"):
            self.set_run_owner(owner)
            with self.subTest(owner=owner):
                self.refused("run_not_owned", self.context.cases.transition_run, RUN_ID, to=RunState.EXTRACTING)
        self.transition_run.assert_not_called()

    def test_a_missing_run_a_foreign_run_and_a_malformed_id_are_indistinguishable(self):
        """No existence disclosure: the same code AND the same message, whatever the reason the module has no authority over the run."""
        outcomes = {}
        for label, owner, run_id in (("foreign", "other_mod", RUN_ID), ("missing", None, RUN_ID), ("malformed", None, str(RUN_ID)), ("none", None, None), ("number", None, 7)):
            self.set_run_owner(owner)
            with self.assertRaises(ModuleContextError) as caught:
                self.context.cases.transition_run(run_id, to=RunState.EXTRACTING)
            outcomes[label] = (caught.exception.code, str(caught.exception), type(caught.exception))
        self.assertEqual(len(set(outcomes.values())), 1, outcomes)
        self.assertEqual(outcomes["missing"][0], "run_not_owned")
        self.transition_run.assert_not_called()

    def test_a_malformed_id_never_reaches_the_database(self):
        for run_id in (str(RUN_ID), None, 7, b"x"):
            with self.subTest(run_id=run_id):
                self.refused("run_not_owned", self.context.cases.transition_run, run_id, to=RunState.EXTRACTING)
        self.runs.filter.assert_not_called()

    def test_a_disabled_module_learns_nothing_about_any_run(self):
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        for owner in (support.CTX, "other_mod", None):
            self.set_run_owner(owner)
            with self.subTest(owner=owner):
                self.refused("module_disabled", self.context.cases.transition_run, RUN_ID, to=RunState.EXTRACTING)
        self.runs.filter.assert_not_called()  # the state check comes first: the run table is not even read

    def test_a_foreign_run_is_never_locked_by_the_context(self):
        self.set_run_owner("other_mod")
        self.refused("run_not_owned", self.context.cases.transition_run, RUN_ID, to=RunState.EXTRACTING)
        self.runs.select_for_update.assert_not_called()
        self.runs.filter.return_value.select_for_update.assert_not_called()
        self.lock_run.assert_not_called()  # the service's own run lock is taken only for an own run, inside transition_run

    def test_the_owner_check_is_the_actual_owner_of_the_run_row(self):
        self.set_run_owner(support.CTX)
        self.context.cases.transition_run(RUN_ID, to=RunState.EXTRACTING)
        self.runs.filter.return_value.values_list.assert_called_once_with("module_id", flat=True)

    def test_the_state_machines_refusals_pass_through_unchanged(self):
        from apps.cases.errors import InvalidTransitionError

        self.transition_run.side_effect = InvalidTransitionError("not_allowed")
        with self.assertRaises(InvalidTransitionError):
            self.context.cases.transition_run(RUN_ID, to=RunState.APPROVED)



# --- audit.record -------------------------------------------------------------------------------------------------------------


class AuditCapabilityTests(ContextCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ctx, "record_event")
        self.record = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ctx, "load_actor", return_value=actor())
        self.load = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ctx.WorkflowRun, "objects")
        self.runs = patcher.start()
        self.addCleanup(patcher.stop)
        self.runs.filter.return_value.values_list.return_value.first.return_value = support.CTX
        self.vocabulary = mock.patch.object(ctx.events, "current_vocabulary", return_value=_AllowAll())
        self.vocabulary.start()
        self.addCleanup(self.vocabulary.stop)

    def record_call(self, **overrides):
        values = dict(actor=SYSTEM_ACTOR, action=support.ACTION, subject_type="revision", subject_id=SUBJECT_ID, count=2, reason_code="ok")
        values.update(overrides)
        return self.context.audit.record(**values)

    def test_a_system_event_is_recorded_through_the_one_recorder(self):
        result = self.record_call()
        self.assertIs(result, self.record.return_value)
        (event,), _ = self.record.call_args
        self.assertEqual((event.actor_kind, event.actor_public_id, event.action, event.subject_type, event.subject_id), ("system", None, support.ACTION, "revision", SUBJECT_ID))
        self.assertEqual(event.details, {"count": 2, "reason_code": "ok"})
        self.load.assert_not_called()

    def test_a_user_event_uses_the_freshly_loaded_actors_public_id_and_never_a_supplied_one(self):
        user = mock.Mock()
        self.record_call(actor=user, case_id=CASE_ID)
        self.load.assert_called_once_with(user)
        (event,), _ = self.record.call_args
        self.assertEqual((event.actor_kind, event.actor_public_id, event.case_id), ("user", USER_PUBLIC_ID, CASE_ID))

    def test_there_is_no_ambient_actor(self):
        for missing in (None,):
            self.refused("actor_required", self.record_call, actor=missing)
        with self.assertRaises(TypeError):
            self.context.audit.record(action=support.ACTION, subject_type="revision", subject_id=SUBJECT_ID)  # actor is a required keyword
        self.record.assert_not_called()

    def test_something_that_is_not_a_user_is_refused_by_the_authorization_core(self):
        self.load.side_effect = AuthorizationError("not_authenticated")
        with self.assertRaises(AuthorizationError):
            self.record_call(actor="alice")
        self.record.assert_not_called()

    def test_a_system_actor_cannot_be_forged_or_copied(self):
        self.assertIs(copy.copy(SYSTEM_ACTOR), SYSTEM_ACTOR)
        self.assertIs(copy.deepcopy(SYSTEM_ACTOR), SYSTEM_ACTOR)
        self.assertEqual(repr(SYSTEM_ACTOR), "SYSTEM_ACTOR")
        self.load.side_effect = AuthorizationError("not_authenticated")
        for forged in ("SYSTEM_ACTOR", type(SYSTEM_ACTOR)(), {"kind": "system"}):
            with self.subTest(forged=forged), self.assertRaises(AuthorizationError):
                self.record_call(actor=forged)

    def test_core_actions_and_undeclared_actions_are_refused(self):
        for action in ("request.submitted", "run.state_changed", "module.enabled"):
            self.refused("audit_action_core_owned", self.record_call, action=action)
        for action in (support.OTHER_ACTION, "ctx_mod.other", "unknown.thing"):
            self.refused("audit_action_not_declared", self.record_call, action=action)
        self.record.assert_not_called()

    def test_undeclared_detail_keys_and_forbidden_subjects_are_refused(self):
        self.refused("audit_detail_key_not_declared", self.record_call, note="x")
        self.refused("audit_subject_type_not_allowed", self.record_call, subject_type="module")
        self.record.assert_not_called()

    def test_the_vocabulary_value_rules_still_apply(self):
        from apps.audit.events import AuditEventError

        self.vocabulary.stop()  # use the real vocabulary: Core's alone, because nothing installs a module vocabulary in a test
        self.addCleanup(self.vocabulary.start)  # (runs before the cleanup that stops the stand-in, so start and stop stay balanced)
        with self.assertRaises(AuditEventError):
            self.record_call()  # Core's vocabulary does not know the module's action: nothing installed it, so the call fails loudly, not silently
        self.record.assert_not_called()

    def test_it_requires_the_callers_transaction(self):
        with mock.patch.object(ctx, "connection", mock.Mock(in_atomic_block=False)):
            with self.assertRaises(AuditTransactionError):
                self.record_call()
        self.record.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_it_opens_no_transaction_of_its_own(self):
        with mock.patch.object(ctx.transaction, "atomic") as atomic:
            self.record_call()
        atomic.assert_not_called()

    def test_a_disabled_module_records_nothing(self):
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        self.refused("module_disabled", self.record_call)
        self.record.assert_not_called()

    def test_a_run_of_another_module_or_a_missing_run_is_refused(self):
        for owner in ("other_mod", "request_intake", None):
            self.runs.filter.return_value.values_list.return_value.first.return_value = owner
            with self.subTest(owner=owner):
                self.refused("run_not_owned", self.record_call, run_id=RUN_ID)
                self.refused("run_not_owned", self.record_call, subject_type="run", subject_id=RUN_ID)
        self.record.assert_not_called()

    def test_a_missing_run_and_a_foreign_run_are_indistinguishable_for_audit_too(self):
        outcomes = set()
        for owner in ("other_mod", None):
            self.runs.filter.return_value.values_list.return_value.first.return_value = owner
            for kwargs in ({"run_id": RUN_ID}, {"subject_type": "run", "subject_id": RUN_ID}):
                with self.assertRaises(ModuleContextError) as caught:
                    self.record_call(**kwargs)
                outcomes.add((caught.exception.code, str(caught.exception)))
        self.assertEqual(len(outcomes), 1, outcomes)
        self.record.assert_not_called()

    def test_a_refused_actor_learns_nothing_about_any_run(self):
        """The actor is judged before the run table is read: an unauthorized actor gets the authorization refusal, never run_not_owned."""
        for reason in ("not_authenticated", "inactive"):
            self.load.side_effect = AuthorizationError(reason)
            for owner in ("other_mod", None, support.CTX):
                self.runs.filter.return_value.values_list.return_value.first.return_value = owner
                with self.subTest(reason=reason, owner=owner):
                    with self.assertRaises(AuthorizationError) as caught:
                        self.record_call(actor=mock.Mock(), run_id=RUN_ID)
                    self.assertEqual(caught.exception.reason, reason)
        self.runs.filter.assert_not_called()
        self.record.assert_not_called()

    def test_a_disabled_module_is_refused_before_the_actor_or_any_run_is_read(self):
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        self.refused("module_disabled", self.record_call, actor=mock.Mock(), run_id=RUN_ID)
        self.load.assert_not_called()
        self.runs.filter.assert_not_called()

    def test_an_own_run_is_accepted_as_run_and_as_subject(self):
        self.record_call(run_id=RUN_ID)
        self.record_call(subject_type="run", subject_id=RUN_ID)
        self.assertEqual(self.record.call_count, 2)

    def test_the_service_errors_pass_through(self):
        error = RuntimeError("insert failed")
        self.record.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.record_call()
        self.assertIs(caught.exception, error)

    def test_nothing_is_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            self.record_call()


class _AllowAll:
    """A vocabulary stand-in that allows the module's action (the real one is installed by the composition root; none is installed in these tests)."""

    def allows_action(self, action):
        return True

    def allows_subject_type(self, subject_type):
        return True

    def detail_keys_of(self, action):
        return frozenset(support.ACTION_KEYS)


# --- permissions.check --------------------------------------------------------------------------------------------------------


class PermissionCapabilityTests(ContextCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ctx, "load_actor")
        self.load = patcher.start()
        self.addCleanup(patcher.stop)
        self.permissions = self.context.permissions

    def holder(self, *permissions, pk=10, superuser=False):
        self.load.return_value = actor(pk, permissions=permissions, superuser=superuser)
        return mock.Mock()

    def code_of(self, function, *args, **kwargs):
        with self.assertRaises(AuthorizationError) as caught:
            function(*args, **kwargs)
        return caught.exception.reason

    def test_an_any_permission_reaches_any_record(self):
        user = self.holder(support.P_VIEW_ANY)
        self.assertEqual(self.permissions.authorize(user, support.P_VIEW_ANY, owner_pk=99).pk, 10)
        self.assertEqual(self.permissions.authorize(user, support.P_VIEW_ANY, owner_pk=None).pk, 10)

    def test_an_own_permission_reaches_only_the_actors_own_record(self):
        user = self.holder(support.P_VIEW_OWN, pk=10)
        self.assertEqual(self.permissions.authorize(user, support.P_VIEW_OWN, owner_pk=10).pk, 10)
        self.assertEqual(self.code_of(self.permissions.authorize, user, support.P_VIEW_OWN, owner_pk=11), "out_of_scope")
        self.assertEqual(self.code_of(self.permissions.authorize, user, support.P_VIEW_OWN, owner_pk=None), "out_of_scope")  # an unknown owner never matches
        self.assertEqual(self.permissions.authorize(self.holder(support.P_VIEW_OWN, pk=0), support.P_VIEW_OWN, owner_pk=0).pk, 0)  # pk 0 is a real owner

    def test_the_permission_part_comes_first(self):
        user = self.holder()  # holds nothing
        for owner in (None, 10, 99):
            with self.subTest(owner=owner):
                self.assertEqual(self.code_of(self.permissions.authorize, user, support.P_VIEW_OWN, support.P_VIEW_ANY, owner_pk=owner), "missing_permission")
        self.assertEqual(self.code_of(self.permissions.require, user, support.P_VIEW_ANY), "missing_permission")

    def test_require_needs_no_record_and_accepts_any_scope(self):
        self.assertEqual(self.permissions.require(self.holder(support.P_VIEW_OWN), support.P_VIEW_OWN, support.P_VIEW_ANY).pk, 10)

    def test_a_superuser_passes_both_parts(self):
        user = self.holder(superuser=True)
        self.assertEqual(self.permissions.authorize(user, support.P_VIEW_OWN, owner_pk=99).pk, 10)
        self.assertEqual(self.permissions.require(user, support.P_VIEW_ANY).pk, 10)

    def test_a_global_permission_is_an_any_permission(self):
        self.assertEqual(self.permissions.authorize(self.holder(support.P_SUBMIT), support.P_SUBMIT).pk, 10)

    def test_a_permission_of_another_scope_does_not_help(self):
        user = self.holder(support.P_SUBMIT)  # holds a different declared permission
        self.assertEqual(self.code_of(self.permissions.authorize, user, support.P_VIEW_ANY, owner_pk=10), "missing_permission")

    def test_only_declared_and_permitted_core_permissions_can_be_checked(self):
        user = self.holder("intake.approve_request", superuser=True)
        for permission in ("intake.approve_request", "other_mod.do_it", "auth.add_user"):
            with self.subTest(permission=permission):
                self.refused("permission_not_declared", self.permissions.authorize, user, permission)
                self.refused("permission_not_declared", self.permissions.require, user, permission)
        self.load.assert_not_called()  # refused before the actor is even read
        self.assertEqual(self.permissions.authorize(self.holder("audit.view_auditevent"), "audit.view_auditevent").pk, 10)

    def test_naming_no_permission_is_refused(self):
        self.refused("no_permission_requested", self.permissions.authorize, mock.Mock())
        self.refused("no_permission_requested", self.permissions.require, mock.Mock())

    def test_the_actor_is_explicit_and_read_fresh(self):
        user = self.holder(support.P_VIEW_ANY)
        self.permissions.authorize(user, support.P_VIEW_ANY)
        self.load.assert_called_once_with(user)
        with self.assertRaises(TypeError):
            self.permissions.authorize()  # no user: the first argument is the user

    def test_an_unauthenticated_or_inactive_actor_is_refused_by_the_authorization_core(self):
        for reason in ("not_authenticated", "inactive"):
            self.load.side_effect = AuthorizationError(reason)
            with self.subTest(reason=reason):
                self.assertEqual(self.code_of(self.permissions.authorize, mock.Mock(), support.P_VIEW_ANY), reason)

    def test_a_refusal_is_logged_by_code_and_public_id_only(self):
        user = self.holder()
        with self.assertLogs("apps.accounts.authz", level="WARNING") as logs:
            self.code_of(self.permissions.authorize, user, support.P_VIEW_OWN, owner_pk=5)
        line = logs.output[0]
        self.assertIn("missing_permission", line)
        self.assertIn(str(USER_PUBLIC_ID), line)
        self.assertIn("ctx_mod:intake.view_own_request", line)

    def test_it_reads_state_but_is_not_gated_by_it(self):
        self.facts = facts_for(self.registry, **{support.CTX: {"enabled": False}})
        user = self.holder(support.P_VIEW_ANY)
        self.assertEqual(self.permissions.authorize(user, support.P_VIEW_ANY).pk, 10)
        self.assertEqual(self.calls, [])  # no state lock, no state read: it only decides

    def test_the_decision_is_exactly_the_core_rules_decision(self):
        """The same function Request Intake delegates to: the facade adds no rule of its own."""
        from apps.accounts import access

        rule = access.AccessRule(any_permissions={support.P_VIEW_ANY}, own_permissions={support.P_VIEW_OWN})
        for held in ((), (support.P_VIEW_OWN,), (support.P_VIEW_ANY,), (support.P_VIEW_OWN, support.P_VIEW_ANY)):
            for superuser in (False, True):
                for owner in (None, 10, 11):
                    expected = access.access_problem(rule, is_superuser=superuser, permissions=frozenset(held), actor_pk=10, owner_pk=owner)
                    user = self.holder(*held, pk=10, superuser=superuser)
                    with self.subTest(held=held, superuser=superuser, owner=owner):
                        try:
                            self.permissions.authorize(user, support.P_VIEW_OWN, support.P_VIEW_ANY, owner_pk=owner)
                            got = None
                        except AuthorizationError as error:
                            got = error.reason
                        self.assertEqual(got, expected)


# --- errors, final classes ------------------------------------------------------------------------------------------------------


class DatabaseErrorTests(ContextCase):
    """A database error is never turned into a module refusal and never swallowed: it propagates, and nothing is delegated."""

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        self.enqueue = stack.enter_context(mock.patch.object(ctx, "enqueue_task"))
        self.create_case = stack.enter_context(mock.patch.object(ctx.case_services, "create_case"))
        self.record = stack.enter_context(mock.patch.object(ctx, "record_event"))
        stack.enter_context(mock.patch.object(ctx, "load_actor", return_value=actor()))
        stack.enter_context(mock.patch.object(ctx.events, "current_vocabulary", return_value=_AllowAll()))
        self.addCleanup(stack.close)

    def capability_calls(self):
        return {
            "enqueue": lambda: self.context.tasks.enqueue(kind=support.KIND, payload={}),
            "create_case": lambda: self.context.cases.create_case(reference="R"),
            "audit": lambda: self.context.audit.record(actor=SYSTEM_ACTOR, action=support.ACTION, subject_type="revision", subject_id=SUBJECT_ID),
        }

    def test_a_failing_state_lock_propagates_and_nothing_is_delegated(self):
        from django.db import OperationalError

        error = OperationalError("canceling statement due to lock timeout")
        for name, call in self.capability_calls().items():
            with self.subTest(call=name), mock.patch.object(ctx, "_share_lock_state_rows", side_effect=error):
                with self.assertRaises(OperationalError) as caught:
                    call()
                self.assertIs(caught.exception, error)
        for delegate in (self.enqueue, self.create_case, self.record):
            delegate.assert_not_called()

    def test_a_failing_state_read_propagates_and_nothing_is_delegated(self):
        from django.db import DatabaseError

        error = DatabaseError("the state could not be read")
        for name, call in self.capability_calls().items():
            with self.subTest(call=name), mock.patch.object(ctx, "read_host_state", side_effect=error):
                with self.assertRaises(DatabaseError) as caught:
                    call()
                self.assertIs(caught.exception, error)
        for delegate in (self.enqueue, self.create_case, self.record):
            delegate.assert_not_called()

    def test_a_database_error_from_the_service_propagates_unchanged(self):
        from django.db import IntegrityError

        self.create_case.side_effect = IntegrityError("duplicate reference")
        with self.assertRaises(IntegrityError):
            self.context.cases.create_case(reference="R")

    def test_a_database_error_is_not_a_module_context_error(self):
        from django.db import DatabaseError

        self.assertFalse(issubclass(DatabaseError, ModuleContextError))
        self.assertFalse(issubclass(ModuleContextError, DatabaseError))


class FinalClassTests(ContextCase):
    def test_the_context_and_the_facades_cannot_be_subclassed_outside_the_module(self):
        for base in (ModuleContext, type(self.context.tasks), type(self.context.cases), type(self.context.audit), type(self.context.permissions)):
            with self.subTest(base=base.__name__):
                with self.assertRaises(TypeError):
                    type("Sub", (base,), {"module_id": property(lambda self: "other_mod")})

    def test_a_subclass_cannot_open_a_context_that_lies_about_its_identity(self):
        with self.assertRaises(TypeError):
            type("Evil", (ModuleContext,), {"module_id": property(lambda self: "request_intake")})

    def test_the_context_is_a_slotted_class_without_a_dict(self):
        self.assertFalse(hasattr(self.context, "__dict__"))
        self.assertFalse(hasattr(self.context.tasks, "__dict__"))


# --- Request Intake and the application stay as they were ---------------------------------------------------------------------


class RequestIntakeCompatibilityTests(SimpleTestCase):
    def test_the_production_registry_holds_only_request_intake_so_every_other_id_is_refused(self):
        from config import module_context

        self.assertEqual(module_context.REGISTRY.module_ids, ("request_intake",))
        for module_id in ("intake", support.CTX):  # ``intake`` is a legacy NAMESPACE of the module, not a module id
            with self.assertRaises(ModuleContextError) as caught:
                module_context.module_context(module_id)
            self.assertEqual(caught.exception.code, "unknown_module")

    def test_request_intake_can_get_a_context_with_the_four_capabilities_that_have_a_facade(self):
        from apps.intake.module import MANIFEST
        from config import module_context

        context = module_context.module_context("request_intake")
        self.assertEqual(context.module_id, "request_intake")
        self.assertEqual(context.capabilities, {"task.enqueue", "audit.record", "case.run", "permissions.check"})  # revision.approval is declared but has no facade
        self.assertIs(module_context.REGISTRY.get("request_intake"), MANIFEST)

    def test_the_composition_root_uses_the_production_handler_allowlist(self):
        source = (BACKEND / "config" / "module_context.py").read_text(encoding="utf-8")
        self.assertIn("handler_registry=PRODUCTION_REGISTRY", source)
        self.assertIn("from config.modules import build_registry", source)

    def test_request_intakes_submission_path_does_not_use_a_context(self):
        for path in sorted((BACKEND / "apps" / "intake").glob("*.py")) + sorted((BACKEND / "apps" / "api").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for word in ("module_context", "ModuleContext", "module_host", "SYSTEM_ACTOR"):  # MOD-2: registering the manifest does not route Request Intake through a context
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, text)

    def test_the_worker_composition_keeps_the_handlers_and_only_the_test_kind_is_legacy(self):
        from apps.intake.registry import PRODUCTION_REGISTRY
        from config.task_registry import COMPOSITION, LEGACY_UNGATED_KINDS

        self.assertEqual(LEGACY_UNGATED_KINDS, {"test.add_numbers"})  # MOD-2: request_intake.extract is the registered module's kind
        self.assertIs(COMPOSITION.registry, PRODUCTION_REGISTRY)

    def test_the_synthetic_request_intake_manifest_still_works_with_a_context_and_is_not_the_registered_one(self):
        from tests.module_support import intake_like_manifest

        registry = support.registry(intake_like_manifest(capabilities_required=("task.enqueue", "audit.record", "case.run", "permissions.check")))
        context = ModuleContext.open(registry, "request_intake")  # MOD-2 will do this for real; nothing does it now
        self.assertEqual(context.capabilities, {"task.enqueue", "audit.record", "case.run", "permissions.check"})
        registered = __import__("config.modules", fromlist=["BUILTIN_MODULES"]).BUILTIN_MODULES
        self.assertNotIn(intake_like_manifest(), registered)  # the synthetic object is a test fixture; the registered manifest is apps.intake.module.MANIFEST


# --- architecture boundaries --------------------------------------------------------------------------------------------------


def _imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module or "")
    return names


class BoundaryTests(SimpleTestCase):
    def test_apps_modules_stays_pure_and_unchanged_by_the_context(self):
        for path in (BACKEND / "apps" / "modules").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("module_host", text, path.name)
            self.assertNotIn("ModuleContext", text, path.name)

    def test_the_context_names_the_existing_services_and_defines_none_of_their_logic(self):
        tree = ast.parse((BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        # (the facade METHODS are named like the services they wrap; what must not exist is a second implementation of the service or of the rule)
        for forbidden in ("enqueue_task", "claim_next_task", "record_event", "load_actor", "access_problem", "permission_problem", "scope_problem", "validate_idempotency_key", "request_fingerprint"):
            self.assertNotIn(forbidden, defined)
        text = (BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8")
        for call in ("enqueue_task(", "case_services.create_run(", "case_services.create_case(", "case_services.transition_run(", "record_event(", "load_actor(", "core_access.access_problem(", "core_access.permission_problem("):
            self.assertIn(call, text)

    def test_the_context_does_not_import_the_composition_roots_or_business_code(self):
        for name in _imports(BACKEND / "apps" / "module_host" / "context.py"):
            self.assertFalse(name.startswith(("config", "apps.intake", "apps.api", "workflows", "importlib")), name)
        self.assertFalse({n for n in _imports(BACKEND / "apps" / "module_host" / "context_rules.py") if n.startswith(("django", "apps."))})

    def test_the_context_never_locks_a_run_and_never_uses_the_services_run_lock(self):
        text = (BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("select_for_update", names)
        self.assertNotIn("lock_run", names)  # an unlocked read of the immutable attribution decides ownership; the service locks only an own run
        cases = next(c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "CaseCapability")
        transition = ast.unparse(next(f for f in cases.body if isinstance(f, ast.FunctionDef) and f.name == "transition_run"))
        self.assertLess(transition.index("_require_available()"), transition.index("_check_own_run("))
        self.assertLess(transition.index("_check_own_run("), transition.index("case_services.transition_run("))

    def test_the_context_has_no_dynamic_import_filesystem_network_or_raw_connection_use(self):
        text = (BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("importlib", "import_module", "__import__", "eval", "exec", "open", "environ", "getenv", "subprocess", "socket", "pathlib", "getattr", "setattr", "globals", "locals"):
            self.assertNotIn(banned, names, banned)
        # the one use of the connection object is the shared state lock and the "inside a transaction" checks
        uses = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "connection"]
        self.assertEqual(sorted(set(uses)), ["connection.cursor", "connection.in_atomic_block", "connection.ops"])

    def test_only_one_statement_is_raw_sql_and_it_is_the_shared_lock(self):
        text = (BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8")
        self.assertEqual(text.count(".execute("), 1)
        for word in (".raw(", "RunSQL", "TRUNCATE", "DELETE", "UPDATE ", "INSERT ", ".delete(", ".update(", "bulk_"):
            self.assertNotIn(word, text.replace("FOR SHARE", ""), word)

    def test_every_facade_method_that_changes_state_checks_availability_inside_a_transaction(self):
        tree = ast.parse((BACKEND / "apps" / "module_host" / "context.py").read_text(encoding="utf-8"))
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        expected = {
            "TaskCapability": ("enqueue",),
            "CaseCapability": ("create_case", "create_run", "transition_run"),
        }
        for class_name, methods in expected.items():
            functions = {f.name: f for f in classes[class_name].body if isinstance(f, ast.FunctionDef)}
            for method in methods:
                source = ast.unparse(functions[method])
                with self.subTest(method=f"{class_name}.{method}"):
                    self.assertIn("transaction.atomic()", source)
                    self.assertLess(source.index("_require_available()"), min(i for i in (source.find("enqueue_task("), source.find("case_services.create_"), source.find("case_services.transition_run(")) if i >= 0))
        record = {f.name: f for f in classes["AuditCapability"].body if isinstance(f, ast.FunctionDef)}["record"]
        calls = [ast.unparse(n.func) for n in ast.walk(record) if isinstance(n, ast.Call)]
        self.assertNotIn("transaction.atomic", calls)  # the audit facade requires the caller's transaction; it never opens one
        audit = ast.unparse(record)
        self.assertLess(audit.index("_require_available()"), audit.index("record_event("))
        permissions = [ast.unparse(n.func) for n in ast.walk(classes["PermissionCapability"]) if isinstance(n, ast.Call)]
        self.assertNotIn("context._require_available", permissions)
        self.assertFalse([c for c in permissions if "transaction" in c or "require_available" in c])

    def test_the_queue_the_worker_and_the_existing_services_are_untouched(self):
        for relative in ("apps/tasks/services.py", "apps/tasks/worker.py", "apps/tasks/policy.py", "apps/cases/services.py", "apps/cases/run_state.py", "apps/audit/recorder.py", "apps/accounts/access.py", "apps/accounts/authz.py", "apps/intake/access.py", "apps/intake/submission.py"):
            text = (BACKEND / relative).read_text(encoding="utf-8")
            with self.subTest(file=relative):
                self.assertNotIn("module_host", text)
                self.assertNotIn("ModuleContext", text)

    def test_nothing_in_the_application_imports_the_context_composition_root_yet(self):
        importers = []
        for top in ("apps", "config", "workflows"):
            for path in sorted((BACKEND / top).rglob("*.py")):
                if "migrations" in path.parts or "__pycache__" in path.parts or path.name == "module_context.py" and top == "config":
                    continue
                if any(name == "config.module_context" or name.startswith("config.module_context.") for name in _imports(path)):
                    importers.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(importers, [])

    def test_the_host_models_and_migrations_did_not_change(self):
        migrations = sorted(p.name for p in (BACKEND / "apps" / "module_host" / "migrations").glob("0*.py"))
        self.assertEqual(migrations, ["0001_initial.py"])
        names = {n.name for n in ast.walk(ast.parse((BACKEND / "apps" / "module_host" / "models.py").read_text(encoding="utf-8"))) if isinstance(n, ast.ClassDef)}
        self.assertLessEqual({"ModuleState", "ModuleNamespaceClaim", "ModuleHostMarker"}, names)
