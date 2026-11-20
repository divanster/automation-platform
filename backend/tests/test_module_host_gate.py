"""DB-free tests for MOD-1b S4: the worker kind gate built from the static registry and the stored module state.

``SimpleTestCase`` refuses every database query. The production gate (Request Intake only, since MOD-2) reads the state through the same function; the gate of a registry with
modules reads the state through ``apps.module_host.gate.read_host_state``, which these tests replace. What a real claim does against PostgreSQL (tasks held
while disabled, resumed after enabling, a running attempt finishing, recovery and retry) is in tests/test_module_host_postgres.py and is NOT run here.

The Request Intake tests (MOD-2) pin that its kind is gated by the stored state like any module's, while the generic gate tests below use synthetic modules.
"""

import logging
from types import MappingProxyType
from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase

from apps.intake import services
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.module_host import gate, rules
from apps.module_host.errors import ModuleHostError
from apps.module_host.rules import StateFacts
from apps.module_host.services import HostState
from apps.modules.manifest import TaskHandlerDeclaration
from apps.modules.registry import ModuleRegistry
from apps.tasks import handlers, worker
from config import task_registry
from tests.module_support import FakeHandler, dep, named

LEGACY = frozenset({"test.add_numbers", "request_intake.extract"})


def mod(module_id, *, kinds=(), deps=(), version="1.0.0"):
    return named(
        module_id,
        module_version=version,
        task_handlers=tuple(TaskHandlerDeclaration(k, FakeHandler(k), "does work") for k in kinds),
        dependencies=tuple(dep(d) for d in deps),
    )


REGISTRY = ModuleRegistry.build([mod("alpha_mod", kinds=("alpha_mod.work",)), mod("beta_mod", kinds=("beta_mod.work",), deps=("alpha_mod",))])
EMPTY = ModuleRegistry.build([])


def facts(*rows):
    return {module_id: StateFacts(module_id, version, enabled, "f" * 64) for module_id, enabled, version in rows}


def fake_handler(kind):
    return handlers.TaskHandler(kind=kind, validate=lambda p: p, run=lambda p: {"ok": 1}, is_test_handler=True)


WORKER_REGISTRY = MappingProxyType({kind: fake_handler(kind) for kind in ("test.add_numbers", "request_intake.extract", "alpha_mod.work", "beta_mod.work", "gamma.unknown_to_registry")})


PRODUCTION_LEGACY = frozenset({"test.add_numbers"})
INTAKE_KIND = "request_intake.extract"
DOC_KIND = "documents.extract_text"  # DOC-2: Core's own kind, claimable with no module state
PRODUCTION_UNGATED = PRODUCTION_LEGACY | {DOC_KIND}


def production_state(*, enabled=True, version="1.0.0", present=True, initialized=True):
    rows = facts(("request_intake", enabled, version)) if present else {}
    return mock.patch.object(gate, "read_host_state", return_value=HostState(rows, initialized))


class RequestIntakeModuleGateTests(SimpleTestCase):
    """MOD-2: Request Intake is a registered module. Its task kind is gated by the stored module state and is no longer a legacy kind."""

    def test_the_legacy_kinds_are_only_the_queues_test_kind_and_every_other_production_kind_belongs_to_a_module_or_is_core(self):
        self.assertEqual(task_registry.LEGACY_UNGATED_KINDS, PRODUCTION_LEGACY)
        self.assertEqual(task_registry.CORE_UNGATED_KINDS, {DOC_KIND})  # DOC-2: a Core kind, decided on here, not a legacy one
        owned = set(task_registry.MODULE_REGISTRY.task_kinds)
        self.assertEqual(owned, {INTAKE_KIND})
        self.assertEqual(task_registry.UNGATED_KINDS | owned, frozenset(task_registry.WORKER_REGISTRY))  # a new production kind forces a decision
        self.assertEqual(frozenset(PRODUCTION_REGISTRY) | {DOC_KIND}, frozenset(task_registry.WORKER_REGISTRY))
        self.assertFalse(task_registry.UNGATED_KINDS & owned)

    def test_the_gate_reads_the_state_and_allows_request_intake_only_when_it_is_available(self):
        with production_state():
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), PRODUCTION_UNGATED | {INTAKE_KIND})
        with production_state(enabled=False):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), PRODUCTION_UNGATED)  # held: tasks stay queued, test.add_numbers and Core's text extraction are untouched
        with production_state(version="0.9.0"):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), PRODUCTION_UNGATED)

    def test_missing_state_after_initialization_fails_closed_for_request_intake(self):
        # the development database may hold the marker from an earlier empty-registry sync: an absent row is NOT "enabled"
        with production_state(present=False, initialized=True):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), PRODUCTION_UNGATED)

    def test_before_any_sync_the_legacy_window_keeps_request_intake_claimable(self):
        with production_state(present=False, initialized=False):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), PRODUCTION_UNGATED | {INTAKE_KIND})

    def test_the_production_registry_and_reconciler_are_unchanged(self):
        self.assertEqual(dict(task_registry.COMPOSITION.registry), {**PRODUCTION_REGISTRY, DOC_KIND: task_registry.WORKER_REGISTRY[DOC_KIND]})  # Request Intake's registry is untouched; DOC-2 adds one handler
        self.assertEqual([hook.hook_id for hook in task_registry.COMPOSITION.maintenance_hooks], ["documents.reconcile_text"])
        self.assertIs(task_registry.COMPOSITION.after_iteration, services.reconcile_after_iteration)

    def test_the_worker_claims_request_intake_only_through_the_production_gate(self):
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        worker.install_composition(task_registry.COMPOSITION)
        with production_state():
            self.assertEqual(worker._claimable_kinds(task_registry.WORKER_REGISTRY), PRODUCTION_UNGATED | {INTAKE_KIND})
        with production_state(enabled=False):
            self.assertEqual(worker._claimable_kinds(task_registry.WORKER_REGISTRY), PRODUCTION_UNGATED)

    def test_process_one_passes_the_gated_kinds_to_the_claim_with_the_production_composition(self):
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        worker.install_composition(task_registry.COMPOSITION)
        for enabled, expected in ((True, PRODUCTION_UNGATED | {INTAKE_KIND}), (False, PRODUCTION_UNGATED)):
            with self.subTest(enabled=enabled), production_state(enabled=enabled), mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(
                worker, "claim_next_task", return_value=None
            ) as claim, mock.patch.object(worker, "_run_after_iteration"):
                outcome = worker.process_one(worker_id="w-1", lease_seconds=60)
                self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
                self.assertEqual(claim.call_args.kwargs["allowed_kinds"], expected)

    def test_a_task_kind_the_registry_does_not_declare_stays_unknown(self):
        with production_state():
            answer = task_registry.COMPOSITION.kind_gate()
        self.assertNotIn("request_intake.other", answer)
        self.assertNotIn("alpha_mod.work", answer)

    def test_keeping_the_request_intake_kind_in_the_legacy_set_would_fail_loudly(self):
        with self.assertRaises(ModuleHostError) as caught:
            gate.make_kind_gate(task_registry.MODULE_REGISTRY, task_registry.LEGACY_UNGATED_KINDS | {INTAKE_KIND})
        self.assertEqual(caught.exception.code, "legacy_kind_claimed")

    def test_the_built_in_registry_holds_exactly_request_intake(self):
        from apps.intake.module import MANIFEST
        from config.modules import BUILTIN_MODULES, build_registry

        self.assertEqual(BUILTIN_MODULES, (MANIFEST,))
        self.assertEqual(build_registry().module_ids, ("request_intake",))
        self.assertIs(task_registry.MODULE_REGISTRY.get("request_intake"), MANIFEST)


class HandlerCompositionTests(SimpleTestCase):
    """``rules.check_handler_composition``: the allowlist is exactly the legacy kinds plus the registry's task kinds."""

    def test_the_production_composition_fits(self):
        rules.check_handler_composition(task_registry.MODULE_REGISTRY, task_registry.WORKER_REGISTRY, task_registry.UNGATED_KINDS)

    def test_a_declared_kind_without_a_handler_is_refused(self):
        with self.assertRaises(ModuleHostError) as caught:
            rules.check_handler_composition(REGISTRY, {"test.add_numbers", "alpha_mod.work"}, {"test.add_numbers"})
        self.assertEqual(caught.exception.code, "handler_composition")
        self.assertIn("beta_mod.work", str(caught.exception))

    def test_a_handler_nobody_owns_is_refused(self):
        with self.assertRaises(ModuleHostError) as caught:
            rules.check_handler_composition(REGISTRY, {"test.add_numbers", "alpha_mod.work", "beta_mod.work", "gamma.stray"}, {"test.add_numbers"})
        self.assertEqual(caught.exception.code, "handler_composition")
        self.assertIn("gamma.stray", str(caught.exception))

    def test_a_legacy_kind_may_have_a_handler_and_an_empty_registry_needs_only_legacy_handlers(self):
        rules.check_handler_composition(EMPTY, {"test.add_numbers"}, {"test.add_numbers"})


class GateAnswerTests(SimpleTestCase):
    def gate_with(self, state, legacy=LEGACY, initialized=True):
        function = gate.make_kind_gate(REGISTRY, legacy)
        patcher = mock.patch.object(gate, "read_host_state", return_value=HostState(state, initialized))
        patcher.start()
        self.addCleanup(patcher.stop)
        return function

    def test_an_empty_registry_never_reads_the_database(self):
        with mock.patch.object(gate, "read_host_state", side_effect=AssertionError("must not read")):
            self.assertEqual(gate.make_kind_gate(EMPTY, LEGACY)(), LEGACY)

    def test_enabled_modules_kinds_are_allowed_with_the_legacy_kinds(self):
        answer = self.gate_with(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", True, "1.0.0")))()
        self.assertEqual(answer, LEGACY | {"alpha_mod.work", "beta_mod.work"})

    def test_a_disabled_modules_kinds_are_excluded(self):
        answer = self.gate_with(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", False, "1.0.0")))()
        self.assertEqual(answer, LEGACY | {"alpha_mod.work"})

    def test_a_version_mismatched_modules_kinds_are_excluded(self):
        answer = self.gate_with(facts(("alpha_mod", True, "1.0.1"), ("beta_mod", True, "1.0.0")))()
        self.assertEqual(answer, LEGACY)  # beta needs alpha: closed with it

    def test_missing_state_after_the_sync_fails_closed(self):
        answer = self.gate_with(facts(("alpha_mod", True, "1.0.0")))()
        self.assertEqual(answer, LEGACY | {"alpha_mod.work"})

    def test_before_the_sync_the_modules_are_treated_as_enabled(self):
        self.assertEqual(self.gate_with({}, initialized=False)(), LEGACY | {"alpha_mod.work", "beta_mod.work"})

    def test_after_the_sync_with_every_row_gone_the_modules_are_held_and_the_legacy_kinds_still_run(self):
        # the initialization marker exists, the rows do not: the compatibility window must NOT reopen
        self.assertEqual(self.gate_with({}, initialized=True)(), LEGACY)

    def test_the_gate_reads_the_state_afresh_on_every_call(self):
        function = gate.make_kind_gate(REGISTRY, LEGACY)
        answers = iter(
            [
                HostState(facts(("alpha_mod", False, "1.0.0"), ("beta_mod", False, "1.0.0")), True),
                HostState(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", False, "1.0.0")), True),
            ]
        )
        with mock.patch.object(gate, "read_host_state", side_effect=lambda: next(answers)):
            self.assertEqual(function(), LEGACY)
            self.assertEqual(function(), LEGACY | {"alpha_mod.work"})  # an enable takes effect on the next iteration, no restart

    def test_unknown_kinds_are_never_in_the_answer(self):
        answer = self.gate_with(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", True, "1.0.0")))()
        for kind in ("gamma.unknown_to_registry", "os.system", "alpha_mod.other"):
            self.assertNotIn(kind, answer)

    def test_a_bad_registry_or_legacy_set_is_refused_at_build_time(self):
        for bad in (None, (), "registry", object()):
            with self.subTest(bad=bad), self.assertRaises(ModuleHostError) as caught:
                gate.make_kind_gate(bad, LEGACY)
            self.assertEqual(caught.exception.code, "invalid_registry")
        for bad in ("test.add_numbers", None, {1}):
            with self.subTest(legacy=bad), self.assertRaises(ModuleHostError) as caught:
                gate.make_kind_gate(REGISTRY, bad)
            self.assertEqual(caught.exception.code, "invalid_legacy_kinds")
        with self.assertRaises(ModuleHostError):
            gate.make_kind_gate(REGISTRY, {"alpha_mod.work"})


class WorkerIntegrationTests(SimpleTestCase):
    """The gate inside the S2 worker: narrowing, fail-closed behaviour, database errors and held tasks (a held task is never failed)."""

    def setUp(self):
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        worker.logger.addHandler(logging.NullHandler())

    def install(self, state):
        self.read = mock.patch.object(gate, "read_host_state", side_effect=state if callable(state) else (lambda: HostState(state, True)))
        self.read.start()
        self.addCleanup(self.read.stop)
        worker.install_composition(worker.Composition(registry=WORKER_REGISTRY, kind_gate=gate.make_kind_gate(REGISTRY, LEGACY)))

    def test_the_claim_filter_is_the_gate_answer_narrowed_to_the_workers_handlers(self):
        self.install(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", True, "1.0.0")))
        self.assertEqual(worker._claimable_kinds(WORKER_REGISTRY), LEGACY | {"alpha_mod.work", "beta_mod.work"})  # 'gamma...' has a handler but nobody allows it

    def test_a_module_kind_the_worker_has_no_handler_for_is_left_queued(self):
        self.install(facts(("alpha_mod", True, "1.0.0"), ("beta_mod", True, "1.0.0")))
        narrow = MappingProxyType({"test.add_numbers": fake_handler("test.add_numbers")})
        self.assertEqual(worker._claimable_kinds(narrow), frozenset({"test.add_numbers"}))

    def test_disabled_module_tasks_are_held_not_failed(self):
        self.install(facts(("alpha_mod", False, "1.0.0"), ("beta_mod", False, "1.0.0")))
        with mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(worker, "claim_next_task", return_value=None) as claim, mock.patch.object(
            worker, "fail_task"
        ) as fail, mock.patch.object(worker, "complete_task") as complete, mock.patch.object(worker, "_run_after_iteration"):
            outcome = worker.process_one(worker_id="w", lease_seconds=60, registry=WORKER_REGISTRY)
        self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
        self.assertEqual(claim.call_args.kwargs["allowed_kinds"], LEGACY)
        fail.assert_not_called()
        complete.assert_not_called()

    def test_recovery_and_the_hooks_still_run_while_everything_is_held(self):
        self.install(facts(("alpha_mod", False, "1.0.0"), ("beta_mod", False, "1.0.0")))
        with mock.patch.object(worker, "recover_expired_leases") as recover, mock.patch.object(worker, "claim_next_task", return_value=None), mock.patch.object(
            worker, "_run_after_iteration"
        ) as hooks:
            worker.process_one(worker_id="w", lease_seconds=60, registry=WORKER_REGISTRY)
        recover.assert_called_once()
        hooks.assert_called_once()

    def test_a_database_error_from_the_state_read_is_re_raised_and_nothing_is_claimed(self):
        def broken():
            raise OperationalError("connection details must not leak")

        self.install(broken)
        with mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(worker, "claim_next_task") as claim:
            with self.assertRaises(OperationalError):
                worker.process_one(worker_id="w", lease_seconds=60, registry=WORKER_REGISTRY)
        claim.assert_not_called()

    def test_any_other_failure_of_the_gate_allows_nothing(self):
        def broken():
            raise RuntimeError("SECRET-DETAIL")

        self.install(broken)
        with self.assertLogs(worker.logger, level="ERROR") as logs:
            allowed = worker._claimable_kinds(WORKER_REGISTRY)
        self.assertEqual(allowed, frozenset())
        self.assertNotIn("SECRET-DETAIL", "".join(logs.output))

    def test_the_run_loop_recovers_from_a_database_error_in_the_gate_like_any_other(self):
        import threading

        calls = []

        def failing_then_stop(**kwargs):
            calls.append(1)
            raise OperationalError("down")

        stop = threading.Event()
        waits = []

        def wait(event, seconds):
            waits.append(seconds)
            event.set()

        with mock.patch.object(worker, "close_old_connections"), mock.patch.object(worker, "connections"):
            processed = worker.run_until_stopped(worker_id="w", lease_seconds=60, poll_seconds=2, stop=stop, process=failing_then_stop, wait=wait)
        self.assertEqual((processed, len(calls), waits), (0, 1, [2]))


class StaticTests(SimpleTestCase):
    def test_the_gate_rule_is_the_one_the_status_uses(self):
        import inspect

        from apps.module_host import services

        self.assertIn("rules.assess_modules", inspect.getsource(services.get_module_status))
        self.assertIn("rules.assess_modules", inspect.getsource(gate.make_kind_gate))
        self.assertIn("rules.allowed_kinds", inspect.getsource(gate.make_kind_gate))
        self.assertTrue(callable(rules.allowed_kinds))
