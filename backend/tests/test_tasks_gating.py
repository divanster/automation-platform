"""DB-free tests for MOD-1b S2: the claim filter, the ordered maintenance hooks and the kind gate.

``SimpleTestCase`` refuses every database query, so a test that passes here never touched a database. The
service calls the worker makes are replaced by mocks, except where a test says that it relies on "no query"
(an empty claim filter). What the filtered claim does against PostgreSQL is in test_tasks_gating_postgres.py and is
NOT run in the development VM. This file is ASCII only.
"""

import ast
import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase

from apps.tasks import handlers, policy, services, worker
from apps.tasks.errors import LeaseLostError
from apps.tasks.policy import Failure, FailureCode, QueuePolicyError, TaskState
from apps.tasks.services import ClaimedTask, FailureResult
from config import task_registry

BACKEND = Path(__file__).resolve().parent.parent
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TASK_ID = uuid.UUID(int=0x91)
TOKEN = uuid.UUID(int=0x92)
SECRET = "SECRET-PAYLOAD-VALUE-4d2"
KIND_A = "alpha.work"
KIND_B = "beta.work"


def claimed(kind=KIND_A, payload=None):
    return ClaimedTask(
        task_id=TASK_ID,
        kind=kind,
        payload={"note": SECRET} if payload is None else payload,
        attempt_id=uuid.UUID(int=0x93),
        attempt_number=1,
        max_attempts=3,
        lease_token=TOKEN,
        lease_expires_at=T0 + timedelta(seconds=60),
    )


def fake_handler(kind, run=lambda payload: {"ok": 1}):
    return handlers.TaskHandler(kind=kind, validate=lambda p: p, run=run, is_test_handler=True)


REGISTRY = MappingProxyType({KIND_A: fake_handler(KIND_A), KIND_B: fake_handler(KIND_B)})


def silence_worker_log(test):
    handler = logging.NullHandler()
    worker.logger.addHandler(handler)
    test.addCleanup(worker.logger.removeHandler, handler)


class CompositionTestCase(SimpleTestCase):
    """Restores the installed composition after each test and mocks the queue services."""

    def setUp(self):
        silence_worker_log(self)
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        patches = {name: mock.patch.object(worker, name) for name in ("recover_expired_leases", "claim_next_task", "complete_task", "fail_task")}
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.mocks["claim_next_task"].return_value = None
        self.mocks["fail_task"].return_value = FailureResult(TaskState.FAILED, None)

    def install(self, **fields):
        worker.install_composition(worker.Composition(**fields))

    def process(self, registry=REGISTRY):
        return worker.process_one(worker_id="w-1", lease_seconds=60, clock=lambda: T0, registry=registry)

    def claim_filter(self):
        return self.mocks["claim_next_task"].call_args.kwargs["allowed_kinds"]


# --- the claim filter value (pure) ---------------------------------------------------------------------


class NormalizeAllowedKindsTests(SimpleTestCase):
    def test_none_means_unrestricted(self):
        self.assertIsNone(policy.normalize_allowed_kinds(None))

    def test_empty_collections_mean_claim_nothing(self):
        for empty in (set(), frozenset(), [], ()):
            with self.subTest(empty=repr(empty)):
                self.assertEqual(policy.normalize_allowed_kinds(empty), frozenset())

    def test_sets_lists_and_tuples_become_one_frozenset_without_duplicates(self):
        for value in ({KIND_A, KIND_B}, frozenset({KIND_A, KIND_B}), [KIND_A, KIND_B, KIND_A], (KIND_B, KIND_A)):
            with self.subTest(value=repr(value)):
                self.assertEqual(policy.normalize_allowed_kinds(value), frozenset({KIND_A, KIND_B}))

    def test_a_kind_of_exactly_the_column_length_is_accepted(self):
        self.assertEqual(policy.normalize_allowed_kinds(["x" * policy.MAX_KIND_LENGTH]), frozenset({"x" * 64}))

    def test_anything_else_is_refused_so_a_mistake_can_only_narrow_the_claim(self):
        bad = (
            KIND_A,  # a bare string would be read as its characters
            KIND_A.encode(),
            {KIND_A: True, KIND_B: False},  # a mapping: its False entries would be read as allowed
            {KIND_A: True}.keys(),
            (kind for kind in (KIND_A,)),
            iter([KIND_A]),
            5,
            True,
            0,
            object(),
        )
        for value in bad:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(QueuePolicyError):
                policy.normalize_allowed_kinds(value)

    def test_members_must_be_non_empty_strings_of_at_most_64_characters(self):
        for member in ("", "x" * 65, None, 5, b"alpha.work", ("a.b",)):
            with self.subTest(member=repr(member)[:30]), self.assertRaises(QueuePolicyError):
                policy.normalize_allowed_kinds([KIND_A, member])

    def test_an_error_does_not_echo_a_value(self):
        with self.assertRaises(QueuePolicyError) as caught:
            policy.normalize_allowed_kinds([SECRET * 10])
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(QueuePolicyError) as caught:
            policy.normalize_allowed_kinds(SECRET)
        self.assertNotIn(SECRET, str(caught.exception))


# --- claim_next_task: the filter, without a database ---------------------------------------------------------------


class ClaimFilterServiceTests(SimpleTestCase):
    def test_the_signature_gained_one_optional_keyword_and_nothing_else(self):
        parameters = inspect.signature(services.claim_next_task).parameters
        self.assertEqual(list(parameters), ["worker_id", "lease_seconds", "now", "allowed_kinds"])
        self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()))
        self.assertIsNone(parameters["allowed_kinds"].default)
        self.assertIsNone(parameters["now"].default)

    def test_an_empty_filter_claims_nothing_and_runs_no_query(self):
        # SimpleTestCase raises on any query, so returning None here proves the database was not asked.
        for empty in (set(), frozenset(), [], ()):
            with self.subTest(empty=repr(empty)):
                self.assertIsNone(services.claim_next_task(worker_id="w", lease_seconds=60, now=T0, allowed_kinds=empty))

    def test_the_arguments_are_still_validated_before_an_empty_filter_returns(self):
        with self.assertRaises(QueuePolicyError):
            services.claim_next_task(worker_id="", lease_seconds=60, allowed_kinds=frozenset())
        with self.assertRaises(QueuePolicyError):
            services.claim_next_task(worker_id="w", lease_seconds=4, allowed_kinds=frozenset())
        with self.assertRaises(QueuePolicyError):
            services.claim_next_task(worker_id="w", lease_seconds=60, now=datetime(2030, 1, 1), allowed_kinds=frozenset())

    def test_a_bad_filter_is_refused_before_any_query(self):
        for bad in (KIND_A, {KIND_A: True}, 5, [5], [""], (x for x in [KIND_A])):
            with self.subTest(bad=repr(bad)[:30]), self.assertRaises(QueuePolicyError):
                services.claim_next_task(worker_id="w", lease_seconds=60, now=T0, allowed_kinds=bad)

    def run_claim(self, **kwargs):
        """Run the claim against a mocked ``Task`` and return the mock, so the query chain can be read."""
        task_model = mock.MagicMock()
        locked = task_model.objects.select_for_update.return_value
        locked.filter.return_value.order_by.return_value.first.return_value = None
        locked.filter.return_value.filter.return_value.order_by.return_value.first.return_value = None
        with mock.patch.object(services, "Task", task_model), mock.patch.object(services.transaction, "atomic"):
            self.assertIsNone(services.claim_next_task(worker_id="w", lease_seconds=60, now=T0, **kwargs))
        return task_model, locked

    def test_without_a_filter_the_query_is_the_one_callers_always_had(self):
        task_model, locked = self.run_claim()
        task_model.objects.select_for_update.assert_called_once_with(skip_locked=True)
        self.assertEqual(locked.filter.call_count, 1)
        self.assertEqual(sorted(locked.filter.call_args.kwargs), ["attempt_count__lt", "available_at__lte", "state"])
        locked.filter.return_value.filter.assert_not_called()
        locked.filter.return_value.order_by.assert_called_once_with("available_at", "created_at", "id")

    def test_none_is_the_same_query_as_no_argument(self):
        _, with_none = self.run_claim(allowed_kinds=None)
        self.assertEqual(with_none.filter.call_count, 1)
        with_none.filter.return_value.filter.assert_not_called()

    def test_a_filter_is_a_where_condition_on_the_same_skip_locked_query_and_keeps_the_order(self):
        task_model, locked = self.run_claim(allowed_kinds={KIND_B, KIND_A})
        task_model.objects.select_for_update.assert_called_once_with(skip_locked=True)
        base_filter = locked.filter.return_value
        base_filter.filter.assert_called_once_with(kind__in=[KIND_A, KIND_B])  # sorted: a stable statement
        base_filter.filter.return_value.order_by.assert_called_once_with("available_at", "created_at", "id")
        base_filter.filter.return_value.order_by.return_value.first.assert_called_once_with()

    def test_the_filter_is_applied_before_the_lock_is_taken_so_nothing_is_claimed_and_put_back(self):
        source = inspect.getsource(services.claim_next_task)
        self.assertEqual(source.count("select_for_update"), 1)
        self.assertEqual(source.count("kind__in"), 1)
        self.assertLess(source.index("kind__in"), source.index(".first()"))
        self.assertLess(source.index(".first()"), source.index("task.state = "))  # the state change comes after the one query
        self.assertNotIn("kind not in", source.lower())
        self.assertNotIn("allowed_kinds", source.split(".first()")[1])  # nothing after the claim looks at the filter


# --- the worker: kind gate ----------------------------------------------------------------------------------------


class NoGateTests(CompositionTestCase):
    def test_without_a_gate_the_claim_is_unrestricted(self):
        self.process()
        self.assertIsNone(self.claim_filter())

    def test_without_a_composition_at_all_the_claim_is_unrestricted_and_the_default_allowlist_applies(self):
        self.install()
        self.process(registry=None)
        self.assertIsNone(self.claim_filter())

    def test_an_unknown_kind_is_still_claimed_and_failed_for_an_unrestricted_caller(self):
        self.mocks["claim_next_task"].return_value = claimed(kind="os.system")
        outcome = self.process()
        self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.UNKNOWN_KIND, False))

    def test_the_other_claim_arguments_are_unchanged(self):
        self.process()
        kwargs = self.mocks["claim_next_task"].call_args.kwargs
        self.assertEqual((kwargs["worker_id"], kwargs["lease_seconds"], kwargs["now"]), ("w-1", 60, T0))
        self.assertEqual(set(kwargs), {"worker_id", "lease_seconds", "now", "allowed_kinds"})

    def test_the_public_signatures_the_process_level_tests_rely_on_are_unchanged(self):
        self.assertEqual(list(inspect.signature(worker.process_one).parameters), ["worker_id", "lease_seconds", "clock", "registry"])
        self.assertEqual(list(inspect.signature(worker.run_until_stopped).parameters), ["worker_id", "lease_seconds", "poll_seconds", "stop", "process", "wait"])

    def test_the_composition_fields_keep_their_order_so_positional_use_still_works(self):
        names = list(worker.Composition.__dataclass_fields__)
        self.assertEqual(names[:2], ["registry", "after_iteration"])
        self.assertEqual(names[2:], ["maintenance_hooks", "kind_gate"])
        composition = worker.Composition(None, lambda: None)
        self.assertEqual((composition.maintenance_hooks, composition.kind_gate), ((), None))

    def test_the_production_composition_has_the_module_gate_and_no_extra_hook(self):
        # MOD-1b S4 installs the module-state gate (apps.module_host.gate); there is still no extra maintenance hook. Without any registered module the gate
        # answers the legacy kinds, so nothing is held (tests/test_module_host_gate.py).
        self.assertIsNotNone(task_registry.COMPOSITION.kind_gate)
        self.assertEqual(task_registry.COMPOSITION.maintenance_hooks, ())
        self.assertIsNotNone(task_registry.COMPOSITION.after_iteration)


class KindGateTests(CompositionTestCase):
    def test_the_gate_answer_becomes_the_claim_filter(self):
        self.install(kind_gate=lambda: {KIND_A})
        self.process()
        self.assertEqual(self.claim_filter(), frozenset({KIND_A}))

    def test_lists_tuples_and_frozensets_are_accepted(self):
        for answer in ([KIND_A, KIND_B], (KIND_A, KIND_B), frozenset({KIND_A, KIND_B}), {KIND_A, KIND_B}):
            with self.subTest(answer=repr(answer)):
                self.install(kind_gate=lambda answer=answer: answer)
                self.process()
                self.assertEqual(self.claim_filter(), frozenset({KIND_A, KIND_B}))

    def test_an_empty_answer_means_nothing_is_claimed(self):
        self.install(kind_gate=frozenset)
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual(self.claim_filter(), frozenset())

    def test_an_empty_answer_really_claims_nothing_through_the_real_service(self):
        self.mocks["claim_next_task"].side_effect = services.claim_next_task  # the real one: SimpleTestCase refuses a query
        self.install(kind_gate=lambda: set())
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)

    def test_the_gate_is_narrowed_to_the_kinds_the_worker_can_run(self):
        self.install(kind_gate=lambda: {KIND_A, "gamma.unknown", "os.system"})
        self.process()
        self.assertEqual(self.claim_filter(), frozenset({KIND_A}))

    def test_the_default_allowlist_is_used_for_narrowing_when_there_is_no_registry(self):
        self.install(kind_gate=lambda: {"test.add_numbers", KIND_A})
        self.process(registry=None)
        self.assertEqual(self.claim_filter(), frozenset({"test.add_numbers"}))

    def test_the_installed_registry_is_used_for_narrowing(self):
        self.install(registry=MappingProxyType({KIND_B: fake_handler(KIND_B)}), kind_gate=lambda: {KIND_A, KIND_B})
        self.process(registry=None)
        self.assertEqual(self.claim_filter(), frozenset({KIND_B}))

    def test_a_gate_that_allows_only_unknown_kinds_claims_nothing(self):
        self.install(kind_gate=lambda: {"gamma.unknown"})
        self.process()
        self.assertEqual(self.claim_filter(), frozenset())

    def test_the_gate_runs_once_per_iteration_after_recovery_and_before_the_claim(self):
        order = []
        self.mocks["recover_expired_leases"].side_effect = lambda **kw: order.append("recover")
        self.mocks["claim_next_task"].side_effect = lambda **kw: order.append("claim")
        self.install(kind_gate=lambda: order.append("gate") or {KIND_A})
        self.process()
        self.assertEqual(order, ["recover", "gate", "claim"])

    def test_the_gate_is_asked_again_every_iteration(self):
        answers = iter([{KIND_A}, {KIND_B}, set()])
        self.install(kind_gate=lambda: next(answers))
        seen = []
        for _ in range(3):
            self.process()
            seen.append(self.claim_filter())
        self.assertEqual(seen, [frozenset({KIND_A}), frozenset({KIND_B}), frozenset()])

    def test_a_task_claimed_under_a_gate_is_run_and_recorded_as_usual(self):
        self.mocks["claim_next_task"].return_value = claimed(KIND_A)
        self.install(kind_gate=lambda: {KIND_A})
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.task_id, outcome.kind), (worker.ProcessStatus.SUCCEEDED, TASK_ID, KIND_A))
        self.mocks["complete_task"].assert_called_once_with(task_id=TASK_ID, lease_token=TOKEN, result={"ok": 1}, now=T0)

    def test_lease_loss_is_handled_as_before_under_a_gate(self):
        self.mocks["claim_next_task"].return_value = claimed(KIND_A)
        self.mocks["complete_task"].side_effect = LeaseLostError("lease_expired")
        self.install(kind_gate=lambda: {KIND_A})
        self.assertEqual(self.process().status, worker.ProcessStatus.LEASE_LOST)
        self.mocks["fail_task"].assert_not_called()

    def test_install_refuses_a_gate_that_is_not_callable(self):
        before = worker._composition
        for bad in ("apps.modules.gate", {KIND_A}, 5, [KIND_A]):
            with self.subTest(bad=repr(bad)), self.assertRaises(TypeError):
                self.install(kind_gate=bad)
        self.assertIs(worker._composition, before)


class KindGateFailsClosedTests(CompositionTestCase):
    def assert_nothing_allowed(self):
        self.assertEqual(self.claim_filter(), frozenset())
        self.mocks["recover_expired_leases"].assert_called_once()  # recovery is not a claim and still runs

    def test_a_gate_that_raises_allows_nothing_and_the_worker_goes_on(self):
        def gate():
            raise RuntimeError(f"connection string password={SECRET}")

        self.install(kind_gate=gate)
        with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
            outcome = self.process()
        self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
        self.assert_nothing_allowed()
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Traceback", text)

    def test_every_unusable_answer_allows_nothing(self):
        answers = (None, KIND_A, b"x", {KIND_A: True}, 5, True, (k for k in (KIND_A,)), iter([KIND_A]), [KIND_A, 5], [""], ["x" * 65], object())
        for answer in answers:
            with self.subTest(answer=repr(answer)[:30]):
                self.mocks["recover_expired_leases"].reset_mock()
                self.install(kind_gate=lambda answer=answer: answer)
                with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
                    outcome = self.process()
                self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
                self.assert_nothing_allowed()
                self.assertIn("invalid answer", "\n".join(logs.output))

    def test_a_none_answer_is_not_read_as_unrestricted(self):
        self.install(kind_gate=lambda: None)
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            self.process()
        self.assertIsNotNone(self.claim_filter())

    def test_the_answer_is_not_echoed_in_the_log(self):
        self.install(kind_gate=lambda: [SECRET, 5])
        with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
            self.process()
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_a_failing_gate_does_not_stop_the_hooks(self):
        ran = []

        def gate():
            raise RuntimeError

        self.install(
            kind_gate=gate,
            after_iteration=lambda: ran.append("legacy"),
            maintenance_hooks=(worker.MaintenanceHook("alpha.sweep", lambda: ran.append("alpha.sweep")),),
        )
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            self.process()
        self.assertEqual(ran, ["legacy", "alpha.sweep"])

    def test_a_database_error_from_the_gate_reaches_the_worker_loop_and_nothing_is_claimed(self):
        def gate():
            raise OperationalError("host=db.internal password=hunter2")

        self.install(kind_gate=gate)
        with self.assertRaises(OperationalError):
            self.process()
        self.mocks["claim_next_task"].assert_not_called()

    def test_a_keyboard_interrupt_in_the_gate_is_not_swallowed(self):
        def gate():
            raise KeyboardInterrupt

        self.install(kind_gate=gate)
        with self.assertRaises(KeyboardInterrupt):
            self.process()
        self.mocks["claim_next_task"].assert_not_called()

    def test_the_gate_failure_log_carries_no_task_data(self):
        self.mocks["claim_next_task"].return_value = None
        self.install(kind_gate=lambda: 1 / 0)
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            self.process()
        self.assertIn("ZeroDivisionError", "\n".join(logs.output))


# --- the worker: ordered maintenance hooks -------------------------------------------------------------------------------


class HookOrderTests(CompositionTestCase):
    def test_the_legacy_hook_runs_first_then_the_others_in_the_order_given(self):
        ran = []
        self.install(
            after_iteration=lambda: ran.append("legacy"),
            maintenance_hooks=(
                worker.MaintenanceHook("zeta.last_by_name", lambda: ran.append("zeta")),
                worker.MaintenanceHook("alpha.first_by_name", lambda: ran.append("alpha")),
                worker.MaintenanceHook("mid.second", lambda: ran.append("mid")),
            ),
        )
        self.process()
        self.assertEqual(ran, ["legacy", "zeta", "alpha", "mid"])  # the order the composition root wrote, not sorted

    def test_the_order_is_the_same_every_iteration_idle_or_busy(self):
        ran = []
        self.install(
            after_iteration=lambda: ran.append("legacy"),
            maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: ran.append("a")), worker.MaintenanceHook("one.b", lambda: ran.append("b"))),
        )
        self.process()
        self.mocks["claim_next_task"].return_value = claimed(KIND_A)
        self.process()
        self.assertEqual(ran, ["legacy", "a", "b"] * 2)

    def test_the_hooks_run_after_the_outcome_is_recorded(self):
        order = []
        self.mocks["claim_next_task"].return_value = claimed(KIND_A)
        self.mocks["complete_task"].side_effect = lambda **kw: order.append("complete")
        self.install(after_iteration=lambda: order.append("legacy"), maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: order.append("a")),))
        self.process()
        self.assertEqual(order, ["complete", "legacy", "a"])

    def test_without_a_legacy_hook_the_new_ones_still_run(self):
        ran = []
        self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: ran.append("a")),))
        self.process()
        self.assertEqual(ran, ["a"])

    def test_the_hooks_are_called_with_no_arguments(self):
        hook, legacy = mock.Mock(), mock.Mock()
        self.install(after_iteration=legacy, maintenance_hooks=(worker.MaintenanceHook("one.a", hook),))
        self.process()
        hook.assert_called_once_with()
        legacy.assert_called_once_with()

    def test_the_hooks_of_one_iteration_come_from_one_composition(self):
        ran = []

        def swap():
            self.install(maintenance_hooks=(worker.MaintenanceHook("two.x", lambda: ran.append("replacement")),))

        self.install(after_iteration=swap, maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: ran.append("original")),))
        self.process()
        self.assertEqual(ran, ["original"])


class HookFailureTests(CompositionTestCase):
    def hooks(self, ran, failing=()):
        def make(name):
            def run():
                ran.append(name)
                if name in failing:
                    raise RuntimeError(f"{SECRET} in {name}")

            return worker.MaintenanceHook(f"one.{name}", run)

        return tuple(make(name) for name in ("a", "b", "c"))

    def test_a_failing_hook_does_not_stop_the_hooks_after_it(self):
        ran = []
        self.install(maintenance_hooks=self.hooks(ran, failing={"a"}))
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            outcome = self.process()
        self.assertEqual(ran, ["a", "b", "c"])
        self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)

    def test_a_failing_legacy_hook_does_not_stop_the_new_hooks(self):
        ran = []

        def legacy():
            ran.append("legacy")
            raise RuntimeError(SECRET)

        self.install(after_iteration=legacy, maintenance_hooks=self.hooks(ran))
        with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
            self.process()
        self.assertEqual(ran, ["legacy", "a", "b", "c"])
        self.assertEqual(logs.output, ["ERROR:apps.tasks.worker:after-iteration hook raised RuntimeError"])  # the legacy message, unchanged

    def test_every_hook_may_fail_and_each_failure_is_logged_with_its_id_and_class_only(self):
        ran = []
        self.install(after_iteration=lambda: 1 / 0, maintenance_hooks=self.hooks(ran, failing={"a", "b", "c"}))
        with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
            self.process()
        self.assertEqual(ran, ["a", "b", "c"])
        self.assertEqual(
            logs.output,
            [
                "ERROR:apps.tasks.worker:after-iteration hook raised ZeroDivisionError",
                "ERROR:apps.tasks.worker:maintenance hook one.a raised RuntimeError",
                "ERROR:apps.tasks.worker:maintenance hook one.b raised RuntimeError",
                "ERROR:apps.tasks.worker:maintenance hook one.c raised RuntimeError",
            ],
        )

    def test_a_hook_failure_never_logs_the_exception_message_or_the_task_payload(self):
        self.mocks["claim_next_task"].return_value = claimed(KIND_A, payload={"note": SECRET})
        self.install(maintenance_hooks=self.hooks([], failing={"a"}))
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            self.process()
        everything = "\n".join(logs.output)
        self.assertNotIn(SECRET, everything)
        self.assertNotIn("Traceback", everything)
        self.assertIn("RuntimeError", everything)

    def test_a_hook_failure_does_not_change_the_task_outcome(self):
        self.mocks["claim_next_task"].return_value = claimed(KIND_A)
        self.install(maintenance_hooks=self.hooks([], failing={"a", "b", "c"}))
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            outcome = self.process()
        self.assertEqual((outcome.status, outcome.task_id), (worker.ProcessStatus.SUCCEEDED, TASK_ID))
        self.mocks["complete_task"].assert_called_once()

    def test_a_database_error_in_a_hook_reaches_the_worker_loop_and_stops_the_later_hooks_for_that_iteration(self):
        ran = []

        def broken():
            ran.append("b")
            raise OperationalError("host=db.internal password=hunter2")

        self.install(
            after_iteration=lambda: ran.append("legacy"),
            maintenance_hooks=(
                worker.MaintenanceHook("one.a", lambda: ran.append("a")),
                worker.MaintenanceHook("one.b", broken),
                worker.MaintenanceHook("one.c", lambda: ran.append("c")),
            ),
        )
        with self.assertRaises(OperationalError):
            self.process()
        self.assertEqual(ran, ["legacy", "a", "b"])

    def test_a_database_error_in_a_new_hook_follows_the_existing_connection_recovery(self):
        calls = []

        def broken():
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("host=db.internal password=hunter2")

        self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", broken),))
        stop = mock.Mock()
        stop.is_set.side_effect = [False, False, True]
        waits = []
        with mock.patch.object(worker, "close_old_connections"), mock.patch.object(worker, "connections") as connections:
            with self.assertLogs("apps.tasks.worker", level="ERROR") as logs:
                worker.run_until_stopped(
                    worker_id="w",
                    lease_seconds=60,
                    poll_seconds=3,
                    stop=stop,
                    process=lambda **kw: self.process(),
                    wait=lambda s, seconds: waits.append(seconds),
                )
        connections.close_all.assert_called_once()  # closed and retried after the poll interval, as for the legacy hook
        self.assertEqual((len(calls), waits), (2, [3, 3]))
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.assertNotIn("hunter2", "\n".join(logs.output))

    def test_a_keyboard_interrupt_in_a_hook_is_not_swallowed(self):
        def hook():
            raise KeyboardInterrupt

        self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", hook),))
        with self.assertRaises(KeyboardInterrupt):
            self.process()


class HookInstallationTests(CompositionTestCase):
    def refused(self, error, **fields):
        before = worker._composition
        with self.assertRaises(error):
            self.install(**fields)
        self.assertIs(worker._composition, before)  # a refused composition changes nothing

    def test_duplicate_ids_are_refused(self):
        self.refused(
            ValueError,
            maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: None), worker.MaintenanceHook("two.b", lambda: None), worker.MaintenanceHook("one.a", lambda: None)),
        )

    def test_the_same_callable_under_two_ids_is_two_hooks_and_allowed(self):
        run = lambda: None  # noqa: E731
        self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", run), worker.MaintenanceHook("one.b", run)))

    def test_the_legacy_hook_id_is_reserved_even_without_a_legacy_hook(self):
        self.assertEqual(worker.LEGACY_HOOK_ID, "core.after_iteration")
        self.refused(ValueError, maintenance_hooks=(worker.MaintenanceHook(worker.LEGACY_HOOK_ID, lambda: None),))
        self.refused(ValueError, after_iteration=lambda: None, maintenance_hooks=(worker.MaintenanceHook(worker.LEGACY_HOOK_ID, lambda: None),))

    def test_an_id_must_be_namespace_dot_name_in_lower_case(self):
        for bad in ("", "a", "one", "one.", ".a", "One.a", "one.A", "one.a.b", "one:a", "one a.b", "apps.intake.services.reconcile", "apps/intake.x", "one.a\n", "1one.a", "one.a" + "x" * 40, "x" * 41 + ".a", "one-x.a", "one.a ", " one.a", "one..a"):
            with self.subTest(hook_id=bad):
                self.refused(ValueError, maintenance_hooks=(worker.MaintenanceHook(bad, lambda: None),))

    def test_an_id_that_is_not_text_is_refused(self):
        for bad in (None, 5, b"one.a", ("one", "a")):
            with self.subTest(hook_id=repr(bad)):
                self.refused(ValueError, maintenance_hooks=(worker.MaintenanceHook(bad, lambda: None),))

    def test_a_good_id_is_accepted(self):
        for good in ("one.a", "o.a", "request_intake.reconcile", "ab.c1", "x1.y_2", "x" * 40 + "." + "y" * 40):
            with self.subTest(hook_id=good):
                self.install(maintenance_hooks=(worker.MaintenanceHook(good, lambda: None),))

    def test_a_hook_must_be_a_callable_never_a_name_or_a_path(self):
        for bad in ("apps.intake.services.reconcile_after_iteration", "apps.intake.services:reconcile_after_iteration", "C:\\hooks\\run.py", None, 5, ["x"]):
            with self.subTest(run=repr(bad)), self.assertRaises(TypeError):
                self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", bad),))

    def test_only_a_tuple_of_hook_objects_is_accepted(self):
        good = worker.MaintenanceHook("one.a", lambda: None)
        for bad in ([good], {good}, iter([good]), good, "one.a", (lambda: None,), ((("one.a"), lambda: None),), (good, None), (good, ("two.b", lambda: None))):
            with self.subTest(maintenance_hooks=repr(bad)[:40]), self.assertRaises(TypeError):
                self.install(maintenance_hooks=bad)

    def test_a_refused_composition_does_not_start_to_run_a_hook(self):
        ran = []
        before = worker._composition
        with self.assertRaises(ValueError):
            self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: ran.append(1)), worker.MaintenanceHook("one.a", lambda: ran.append(2))))
        self.process()
        self.assertIs(worker._composition, before)
        self.assertEqual(ran, [])

    def test_the_error_names_a_duplicate_id_and_no_other_value(self):
        with self.assertRaises(ValueError) as caught:
            self.install(maintenance_hooks=(worker.MaintenanceHook("one.a", lambda: None), worker.MaintenanceHook("one.a", lambda: None)))
        self.assertIn("one.a", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.install(maintenance_hooks=(worker.MaintenanceHook(SECRET, lambda: None),))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_a_hook_object_is_immutable(self):
        hook = worker.MaintenanceHook("one.a", lambda: None)
        with self.assertRaises(Exception):
            hook.hook_id = "one.b"  # type: ignore[misc]


# --- static guarantees --------------------------------------------------------------------------------------------------------


class StaticGuaranteeTests(SimpleTestCase):
    FILES = ("worker.py", "services.py", "policy.py")

    def source(self, name):
        return (BACKEND / "apps" / "tasks" / name).read_text(encoding="utf-8")

    def test_the_queue_imports_nothing_dynamic_and_no_module_or_business_code(self):
        for name in self.FILES:
            tree = ast.parse(self.source(name))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            with self.subTest(file=name):
                self.assertFalse({m for m in imported if m.split(".")[0] in {"importlib", "pkgutil", "runpy", "imp"}}, imported)
                self.assertFalse({m for m in imported if m.startswith(("apps.modules", "apps.intake", "config.modules", "workflows"))}, imported)

    def test_the_queue_never_evaluates_or_imports_by_name(self):
        for name in self.FILES:
            tree = ast.parse(self.source(name))
            called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            attributes = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            with self.subTest(file=name):
                self.assertFalse(called & {"eval", "exec", "__import__", "compile", "getattr", "setattr"}, called)
                self.assertFalse(attributes & {"import_module", "load_module", "spec_from_file_location"}, attributes)

    def test_the_worker_has_one_queue_and_one_claim_path(self):
        tree = ast.parse(self.source("worker.py"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "claim_next_task"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(k.arg for k in calls[0].keywords), ["allowed_kinds", "lease_seconds", "now", "worker_id"])
        names = [node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
        self.assertFalse([n for n in names if "queue" in n.lower()], names)

    def test_the_gate_is_not_read_by_recovery_or_by_the_lease_fenced_writes(self):
        source = self.source("services.py")
        for function in ("recover_expired_leases", "renew_lease", "complete_task", "fail_task", "_lock_held_task", "cancel_task", "enqueue_task"):
            body = ast.get_source_segment(source, next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == function))
            with self.subTest(function=function):
                self.assertNotIn("allowed_kinds", body)
                self.assertNotIn("kind__in", body)
