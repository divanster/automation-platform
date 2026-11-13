"""DB-free tests for the worker loop, the ``runworker`` command, and service input validation.

``SimpleTestCase`` refuses every database query, so a test that passes here never touched a
database. The service calls the worker makes are replaced by mocks; what those services do against
PostgreSQL (claim, lease, recovery) is in test_tasks_postgres.py and is NOT run in the VM.
This file is ASCII only.
"""

import io
import logging
import signal
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, override_settings

from apps.tasks import errors, handlers, services, worker
from apps.tasks.errors import LeaseLostError
from apps.tasks.management.commands import runworker
from apps.tasks.policy import (
    Failure,
    FailureCode,
    QueuePolicyError,
    TaskPermanentError,
    TaskRetryableError,
    TaskState,
)
from apps.tasks.services import ClaimedTask, FailureResult
from tests.settings_loader import load_settings

T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TASK_ID = uuid.UUID(int=0x71)
TOKEN = uuid.UUID(int=0x72)
SECRET = "SECRET-PAYLOAD-VALUE-9f3"


def claimed(kind="test.add_numbers", payload=None, attempt=1, maximum=3):
    return ClaimedTask(
        task_id=TASK_ID,
        kind=kind,
        payload={"values": [1, 2, 3]} if payload is None else payload,
        attempt_id=uuid.UUID(int=0x73),
        attempt_number=attempt,
        max_attempts=maximum,
        lease_token=TOKEN,
        lease_expires_at=T0 + timedelta(seconds=60),
    )


def handler_registry(run, kind="fake.kind", validate=None):
    return {kind: handlers.TaskHandler(kind=kind, validate=validate or (lambda p: p), run=run, is_test_handler=True)}


def silence_worker_log(test):
    """Keep expected warnings out of the test output (assertLogs still sees them)."""
    handler = logging.NullHandler()
    worker.logger.addHandler(handler)
    test.addCleanup(worker.logger.removeHandler, handler)


class ProcessOneTests(SimpleTestCase):
    """process_one with the four service calls replaced by mocks."""

    def setUp(self):
        silence_worker_log(self)
        patches = {name: mock.patch.object(worker, name) for name in ("recover_expired_leases", "claim_next_task", "complete_task", "fail_task")}
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.mocks["fail_task"].return_value = FailureResult(TaskState.FAILED, None)

    def process(self, registry=None, clock=lambda: T0):
        return worker.process_one(worker_id="w-1", lease_seconds=60, clock=clock, registry=registry)

    def test_nothing_to_do_is_idle_and_runs_no_handler(self):
        self.mocks["claim_next_task"].return_value = None
        outcome = self.process()
        self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
        self.mocks["complete_task"].assert_not_called()
        self.mocks["fail_task"].assert_not_called()

    def test_expired_leases_are_recovered_before_claiming(self):
        calls = []
        self.mocks["recover_expired_leases"].side_effect = lambda **kw: calls.append("recover")
        self.mocks["claim_next_task"].side_effect = lambda **kw: calls.append("claim")
        self.process()
        self.assertEqual(calls, ["recover", "claim"])

    def test_the_test_handler_result_is_completed_under_the_claim_token(self):
        self.mocks["claim_next_task"].return_value = claimed(payload={"values": [3, -1, 10], "label": "demo"})
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.task_id, outcome.failure_code), (worker.ProcessStatus.SUCCEEDED, TASK_ID, None))
        self.mocks["complete_task"].assert_called_once_with(task_id=TASK_ID, lease_token=TOKEN, result={"count": 3, "sum": 12, "label": "demo"}, now=T0)
        self.mocks["fail_task"].assert_not_called()

    def test_the_claim_uses_the_given_worker_id_and_lease(self):
        self.mocks["claim_next_task"].return_value = None
        self.process()
        _, kwargs = self.mocks["claim_next_task"].call_args
        self.assertEqual((kwargs["worker_id"], kwargs["lease_seconds"], kwargs["now"]), ("w-1", 60, T0))

    def test_a_permanent_handler_failure_fails_the_task_without_a_retry(self):
        def run(payload):
            raise TaskPermanentError()

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
        outcome = self.process(handler_registry(run))
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "rejected"))
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.REJECTED, False))
        self.mocks["complete_task"].assert_not_called()

    def test_a_retryable_failure_reports_requeued_when_the_service_schedules_another_attempt(self):
        def run(payload):
            raise TaskRetryableError()

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
        self.mocks["fail_task"].return_value = FailureResult(TaskState.QUEUED, T0 + timedelta(seconds=5))
        outcome = self.process(handler_registry(run))
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "temporary_failure"))
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.TEMPORARY_FAILURE, True))

    def test_a_retryable_failure_on_the_last_attempt_reports_failed(self):
        def run(payload):
            raise TaskRetryableError()

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind", attempt=3)
        outcome = self.process(handler_registry(run))  # the service decides; here it says FAILED
        self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)

    def test_an_unexpected_exception_is_a_retryable_handler_error_and_its_message_is_not_kept(self):
        def run(payload):
            raise RuntimeError(f"connection string password={SECRET}")

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind", payload={"token": SECRET})
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            outcome = self.process(handler_registry(run))
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.HANDLER_ERROR, True))
        self.assertEqual(outcome.failure_code, "handler_error")
        everything = repr(self.mocks["fail_task"].call_args) + repr(outcome) + "\n".join(logs.output)
        self.assertNotIn(SECRET, everything)
        self.assertNotIn("Traceback", everything)
        self.assertIn("RuntimeError", "\n".join(logs.output))  # the class name is logged, nothing else

    def test_an_unknown_kind_fails_permanently_without_running_anything(self):
        self.mocks["claim_next_task"].return_value = claimed(kind="os.system")
        outcome = self.process()
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.UNKNOWN_KIND, False))
        self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)

    def test_a_stored_payload_that_no_longer_validates_fails_permanently(self):
        self.mocks["claim_next_task"].return_value = claimed(payload={"values": "not a list", "note": SECRET})
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            outcome = self.process()
        self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.INVALID_PAYLOAD, False))
        self.assertEqual(outcome.failure_code, "invalid_payload")
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_a_result_that_is_not_a_small_json_object_fails_permanently(self):
        for bad in ("text", None, [1], {"x": 1.5}, {"x": "y" * 5000}):
            with self.subTest(result=repr(bad)[:30]):
                self.mocks["fail_task"].reset_mock()
                self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
                outcome = self.process(handler_registry(lambda payload, bad=bad: bad))
                self.assertEqual(self.mocks["fail_task"].call_args.kwargs["failure"], Failure(FailureCode.INVALID_RESULT, False))
                self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)
                self.mocks["complete_task"].assert_not_called()

    def test_losing_the_lease_discards_the_result_and_does_not_retry_the_write(self):
        self.mocks["claim_next_task"].return_value = claimed(payload={"values": [1], "label": SECRET})
        self.mocks["complete_task"].side_effect = LeaseLostError("lease_expired")
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            outcome = self.process()
        self.assertEqual(outcome.status, worker.ProcessStatus.LEASE_LOST)
        self.assertEqual(self.mocks["complete_task"].call_count, 1)
        self.mocks["fail_task"].assert_not_called()  # a stale worker must not mark the task failed either
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("lease_expired", "\n".join(logs.output))

    def test_losing_the_lease_while_recording_a_failure_is_also_discarded(self):
        def run(payload):
            raise TaskPermanentError()

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
        self.mocks["fail_task"].side_effect = LeaseLostError("token_mismatch")
        outcome = self.process(handler_registry(run))
        self.assertEqual(outcome.status, worker.ProcessStatus.LEASE_LOST)

    def test_the_clock_is_read_afresh_for_each_step_so_the_lease_is_judged_when_recording(self):
        instants = iter([T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=100)])
        self.mocks["claim_next_task"].return_value = claimed()
        self.process(clock=lambda: next(instants))
        self.assertEqual(self.mocks["recover_expired_leases"].call_args.kwargs["now"], T0)
        self.assertEqual(self.mocks["claim_next_task"].call_args.kwargs["now"], T0 + timedelta(seconds=1))
        self.assertEqual(self.mocks["complete_task"].call_args.kwargs["now"], T0 + timedelta(seconds=100))

    def test_the_handler_runs_after_the_claim_and_before_the_completion(self):
        order = []
        self.mocks["claim_next_task"].side_effect = lambda **kw: order.append("claim") or claimed(kind="fake.kind")
        self.mocks["complete_task"].side_effect = lambda **kw: order.append("complete")

        def run(payload):
            order.append("handler")
            return {"ok": 1}

        self.process(handler_registry(run))
        self.assertEqual(order, ["claim", "handler", "complete"])

    def test_a_handler_is_refused_inside_a_database_transaction(self):
        ran = []
        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
        with mock.patch.object(worker, "connection") as fake_connection:
            fake_connection.in_atomic_block = True
            with self.assertRaises(RuntimeError):
                self.process(handler_registry(lambda payload: ran.append(1) or {}))
        self.assertEqual(ran, [])
        self.mocks["complete_task"].assert_not_called()

    def test_the_handler_sees_no_open_transaction_in_the_normal_path(self):
        seen = []
        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")

        def run(payload):
            seen.append(worker.connection.in_atomic_block)
            return {}

        self.process(handler_registry(run))
        self.assertEqual(seen, [False])

    def test_interrupts_are_not_swallowed_by_the_handler_guard(self):
        def run(payload):
            raise KeyboardInterrupt

        self.mocks["claim_next_task"].return_value = claimed(kind="fake.kind")
        with self.assertRaises(KeyboardInterrupt):
            self.process(handler_registry(run))
        self.mocks["fail_task"].assert_not_called()
        self.mocks["complete_task"].assert_not_called()

    def test_success_is_logged_without_payload_or_result(self):
        self.mocks["claim_next_task"].return_value = claimed(payload={"values": [1], "label": SECRET})
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            self.process()
        text = "\n".join(logs.output)
        self.assertIn(str(TASK_ID), text)
        self.assertNotIn(SECRET, text)


class WaitTests(SimpleTestCase):
    class FakeStop:
        def __init__(self, set_after=None):
            self.waits = []
            self.set_after = set_after

        def is_set(self):
            return self.set_after is not None and len(self.waits) >= self.set_after

        def wait(self, seconds):
            self.waits.append(seconds)

    def test_a_long_wait_is_taken_in_slices_of_at_most_one_second(self):
        stop = self.FakeStop()
        worker.wait_for(stop, 2.5)
        self.assertEqual(stop.waits, [1.0, 1.0, 0.5])

    def test_a_wait_ends_as_soon_as_stop_is_set(self):
        stop = self.FakeStop(set_after=1)
        worker.wait_for(stop, 60)
        self.assertEqual(stop.waits, [1.0])

    def test_no_wait_happens_when_already_stopped(self):
        stop = self.FakeStop(set_after=0)
        worker.wait_for(stop, 60)
        self.assertEqual(stop.waits, [])


class RunLoopTests(SimpleTestCase):
    def setUp(self):
        silence_worker_log(self)
        for name in ("close_old_connections", "connections"):
            patcher = mock.patch.object(worker, name)
            self.addCleanup(patcher.stop)
            setattr(self, name, patcher.start())

    def stopper(self):
        class Stop:
            def __init__(self):
                self.flag = False

            def is_set(self):
                return self.flag

        return Stop()

    def run_loop(self, outcomes, stop=None, poll=7):
        stop = stop or self.stopper()
        waits = []
        sequence = iter(outcomes)

        def process(**kwargs):
            try:
                item = next(sequence)
            except StopIteration:
                stop.flag = True
                return worker.ProcessOutcome(worker.ProcessStatus.IDLE)
            if isinstance(item, Exception):
                raise item
            return item

        count = worker.run_until_stopped(worker_id="w", lease_seconds=60, poll_seconds=poll, stop=stop, process=process, wait=lambda s, seconds: waits.append(seconds))
        return count, waits

    def test_idle_iterations_wait_for_the_poll_interval_instead_of_spinning(self):
        idle = worker.ProcessOutcome(worker.ProcessStatus.IDLE)
        count, waits = self.run_loop([idle, idle, idle])
        self.assertEqual(count, 0)
        self.assertEqual(waits, [7, 7, 7, 7])  # three idle outcomes plus the final one that sets stop

    def test_processed_tasks_are_counted_and_followed_at_once_by_the_next_claim(self):
        done = worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED, TASK_ID)
        count, waits = self.run_loop([done, done])
        self.assertEqual(count, 2)
        self.assertEqual(waits, [7])  # only the final idle iteration waits

    def test_the_loop_checks_the_stop_flag_before_each_iteration(self):
        stop = self.stopper()
        stop.flag = True
        count, waits = self.run_loop([worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED)], stop=stop)
        self.assertEqual((count, waits), (0, []))

    def test_connections_are_checked_every_iteration(self):
        idle = worker.ProcessOutcome(worker.ProcessStatus.IDLE)
        self.run_loop([idle, idle])
        self.assertEqual(self.close_old_connections.call_count, 3)

    def test_a_database_error_closes_the_connections_waits_and_retries(self):
        done = worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED)
        count, waits = self.run_loop([OperationalError("host=db.internal password=hunter2"), done])
        self.assertEqual(count, 1)
        self.assertEqual(waits, [7, 7])
        self.connections.close_all.assert_called_once()

    def test_the_database_error_text_is_not_logged(self):
        with self.assertLogs("apps.tasks.worker", level="DEBUG") as logs:
            self.run_loop([OperationalError("host=db.internal password=hunter2")])
        text = "\n".join(logs.output)
        self.assertIn("OperationalError", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("db.internal", text)

    def test_repeated_database_errors_stop_the_worker_instead_of_looping_forever(self):
        errors_in_a_row = [OperationalError("down")] * worker.MAX_CONSECUTIVE_DATABASE_ERRORS
        with self.assertRaises(OperationalError):
            self.run_loop(errors_in_a_row)
        self.assertEqual(self.connections.close_all.call_count, worker.MAX_CONSECUTIVE_DATABASE_ERRORS)

    def test_a_success_resets_the_consecutive_error_count(self):
        done = worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED)
        pattern = [OperationalError("x")] * (worker.MAX_CONSECUTIVE_DATABASE_ERRORS - 1) + [done] + [OperationalError("x")] * (worker.MAX_CONSECUTIVE_DATABASE_ERRORS - 1)
        count, _ = self.run_loop(pattern)
        self.assertEqual(count, 1)

    def test_other_exceptions_are_not_swallowed(self):
        with self.assertRaises(ZeroDivisionError):
            self.run_loop([ZeroDivisionError()])

    def test_the_real_wait_returns_promptly_for_a_stopped_event(self):
        import threading

        stop = threading.Event()
        stop.set()
        worker.wait_for(stop, 60)  # returns at once; a hang would fail the suite by timeout


class WorkerIdTests(SimpleTestCase):
    def test_worker_ids_are_short_unique_and_carry_no_host_name(self):
        first, second = worker.make_worker_id(), worker.make_worker_id()
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), services.MAX_WORKER_ID_LENGTH)
        self.assertRegex(first, r"^\d+-[0-9a-f]{8}$")


class RunWorkerCommandTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(runworker, "connections")
        self.connections = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, worker, "_composition", worker._composition)  # the command installs a composition

    def run_command(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        call_command("runworker", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_once_processes_at_most_one_task_and_exits(self):
        outcome = worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED, TASK_ID, "test.add_numbers")
        with mock.patch.object(worker, "process_one", return_value=outcome) as process:
            out, _ = self.run_command("--once")
        process.assert_called_once()
        self.assertEqual(out.strip(), f"Task {TASK_ID} succeeded.")

    def test_once_reports_when_there_is_nothing_to_do(self):
        with mock.patch.object(worker, "process_one", return_value=worker.ProcessOutcome(worker.ProcessStatus.IDLE)):
            out, _ = self.run_command("--once")
        self.assertEqual(out.strip(), "No task available.")

    def test_once_reports_the_safe_failure_code_only(self):
        outcome = worker.ProcessOutcome(worker.ProcessStatus.REQUEUED, TASK_ID, "k", "handler_error")
        with mock.patch.object(worker, "process_one", return_value=outcome):
            out, _ = self.run_command("--once")
        self.assertIn("requeued (handler_error)", out)

    def test_once_passes_the_configured_lease(self):
        with mock.patch.object(worker, "process_one", return_value=worker.ProcessOutcome(worker.ProcessStatus.IDLE)) as process:
            self.run_command("--once", "--lease-seconds", "30")
        self.assertEqual(process.call_args.kwargs["lease_seconds"], 30)
        self.assertRegex(process.call_args.kwargs["worker_id"], r"^\d+-[0-9a-f]{8}$")

    @override_settings(WORKER_LEASE_SECONDS=45, WORKER_POLL_SECONDS=9)
    def test_the_defaults_come_from_settings(self):
        with mock.patch.object(worker, "run_until_stopped", return_value=0) as loop:
            self.run_command()
        self.assertEqual((loop.call_args.kwargs["lease_seconds"], loop.call_args.kwargs["poll_seconds"]), (45, 9))

    def test_command_line_values_override_settings(self):
        with mock.patch.object(worker, "run_until_stopped", return_value=0) as loop:
            self.run_command("--poll-seconds", "3", "--lease-seconds", "120")
        self.assertEqual((loop.call_args.kwargs["lease_seconds"], loop.call_args.kwargs["poll_seconds"]), (120, 3))

    def test_out_of_range_values_are_refused_before_anything_runs(self):
        for args in (["--poll-seconds", "0"], ["--poll-seconds", "61"], ["--lease-seconds", "4"], ["--lease-seconds", "3601"], ["--poll-seconds", "-1"]):
            with self.subTest(args=args), mock.patch.object(worker, "process_one") as process, mock.patch.object(worker, "run_until_stopped") as loop:
                with self.assertRaises(CommandError):
                    self.run_command("--once", *args)
                process.assert_not_called()
                loop.assert_not_called()

    def test_the_continuous_mode_runs_the_loop_with_a_stop_event_and_reports_the_count(self):
        with mock.patch.object(worker, "run_until_stopped", return_value=4) as loop:
            out, _ = self.run_command()
        self.assertTrue(hasattr(loop.call_args.kwargs["stop"], "is_set"))
        self.assertIn("started", out)
        self.assertIn("stopped after processing 4 task(s)", out)

    def test_the_continuous_mode_does_not_run_a_single_task_directly(self):
        with mock.patch.object(worker, "run_until_stopped", return_value=0), mock.patch.object(worker, "process_one") as process:
            self.run_command()
        process.assert_not_called()

    def test_connections_are_closed_after_success_and_after_failure(self):
        with mock.patch.object(worker, "process_one", return_value=worker.ProcessOutcome(worker.ProcessStatus.IDLE)):
            self.run_command("--once")
        self.assertEqual(self.connections.close_all.call_count, 1)
        with mock.patch.object(worker, "process_one", side_effect=ZeroDivisionError):
            with self.assertRaises(ZeroDivisionError):
                self.run_command("--once")
        self.assertEqual(self.connections.close_all.call_count, 2)

    def test_a_database_error_becomes_a_command_error_without_the_driver_message(self):
        with mock.patch.object(worker, "process_one", side_effect=OperationalError("host=db.internal user=svc password=hunter2")):
            with self.assertRaises(CommandError) as caught:
                self.run_command("--once")
        self.assertIn("OperationalError", str(caught.exception))
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertNotIn("db.internal", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.connections.close_all.assert_called_once()

    def test_a_hard_interrupt_exits_with_a_message_and_a_non_zero_code(self):
        with mock.patch.object(worker, "run_until_stopped", side_effect=KeyboardInterrupt):
            with self.assertRaises(CommandError) as caught:
                self.run_command()
        self.assertEqual(caught.exception.returncode, 130)
        self.assertIn("recovered after its lease expires", str(caught.exception))
        self.connections.close_all.assert_called_once()

    def test_payloads_never_reach_the_command_output(self):
        outcome = worker.ProcessOutcome(worker.ProcessStatus.SUCCEEDED, TASK_ID, "test.add_numbers")
        with mock.patch.object(worker, "process_one", return_value=outcome):
            out, err = self.run_command("--once")
        self.assertNotIn("values", out + err)
        self.assertNotIn("sum", out + err)

    def test_the_command_registers_no_service_and_needs_no_extra_arguments(self):
        self.assertIn("test handler", runworker.Command.help.lower())


class ConsoleLoggingTests(SimpleTestCase):
    def test_log_lines_reach_the_stream_only_while_the_command_runs(self):
        stream = io.StringIO()
        logger = logging.getLogger("apps.tasks.worker")
        before = list(logging.getLogger("apps.tasks").handlers)
        with runworker.console_logging(stream):
            logger.info("task %s succeeded", TASK_ID)
            logger.debug("not shown at INFO")
        logger.info("after")
        self.assertIn(f"task {TASK_ID} succeeded", stream.getvalue())
        self.assertNotIn("not shown", stream.getvalue())
        self.assertNotIn("after", stream.getvalue())
        self.assertEqual(logging.getLogger("apps.tasks").handlers, before)


class ShutdownSignalTests(SimpleTestCase):
    def install(self):
        """Enter the context manager with signal.signal replaced; return (handlers, previous, stop, context)."""
        import threading

        installed = {}
        restored = {}
        previous_marker = object()

        def fake_signal(number, handler):
            if handler is previous_marker:
                restored[number] = handler
            else:
                installed[number] = handler
            return previous_marker

        patcher = mock.patch.object(runworker.signal, "signal", side_effect=fake_signal)
        patcher.start()
        self.addCleanup(patcher.stop)
        stop = threading.Event()
        return installed, restored, stop, runworker.shutdown_on_signals(stop)

    def test_sigint_and_sigterm_are_handled_and_restored(self):
        installed, restored, stop, context = self.install()
        with context:
            self.assertIn(signal.SIGINT, installed)
            self.assertIn(signal.SIGTERM, installed)
            if "SIGBREAK" in signal.Signals.__members__:
                self.assertIn(signal.SIGBREAK, installed)
        self.assertEqual(set(restored), set(installed))

    def test_the_first_signal_only_sets_the_flag(self):
        installed, _, stop, context = self.install()
        with context:
            installed[signal.SIGINT](signal.SIGINT, None)
            self.assertTrue(stop.is_set())

    def test_the_second_signal_is_a_hard_interrupt(self):
        installed, _, stop, context = self.install()
        with context:
            installed[signal.SIGINT](signal.SIGINT, None)
            with self.assertRaises(KeyboardInterrupt):
                installed[signal.SIGINT](signal.SIGINT, None)

    def test_handlers_are_restored_even_when_the_body_raises(self):
        installed, restored, stop, context = self.install()
        with self.assertRaises(ZeroDivisionError):
            with context:
                raise ZeroDivisionError
        self.assertEqual(set(restored), set(installed))

    def test_installing_outside_the_main_thread_is_a_quiet_no_op(self):
        import threading

        with mock.patch.object(runworker.signal, "signal", side_effect=ValueError("signal only works in main thread")):
            with runworker.shutdown_on_signals(threading.Event()):
                pass  # no exception


class ServiceValidationTests(SimpleTestCase):
    """Rules enforced before any database access (SimpleTestCase would refuse a query)."""

    GOOD = {"values": [1, 2]}

    def test_an_unknown_kind_is_refused(self):
        for kind in ("nope", "os.system", "", None, 5):
            with self.subTest(kind=kind), self.assertRaises(errors.UnknownTaskKindError):
                services.enqueue_task(kind=kind, payload=self.GOOD)

    def test_an_invalid_payload_is_refused(self):
        for payload in (None, {}, {"values": []}, {"values": [1.5]}, {"values": [1], "x": 1}):
            with self.subTest(payload=repr(payload)), self.assertRaises(QueuePolicyError):
                services.enqueue_task(kind="test.add_numbers", payload=payload)

    def test_bad_keys_attempt_limits_and_times_are_refused(self):
        for changes in (
            {"idempotency_key": "has space"},
            {"idempotency_key": ""},
            {"idempotency_key": "x" * 201},
            {"max_attempts": 0},
            {"max_attempts": 11},
            {"max_attempts": True},
            {"available_at": datetime(2030, 1, 1)},
            {"now": datetime(2030, 1, 1)},
        ):
            with self.subTest(changes=changes), self.assertRaises(QueuePolicyError):
                services.enqueue_task(kind="test.add_numbers", payload=self.GOOD, **changes)

    def test_the_payload_size_bound_is_enforced_with_an_injected_handler(self):
        registry = handler_registry(lambda p: {}, kind="fake.kind")
        with self.assertRaises(QueuePolicyError):
            services.enqueue_task(kind="fake.kind", payload={"x": "y" * 5000}, registry=registry)

    def test_an_error_does_not_echo_the_payload_or_key(self):
        with self.assertRaises(QueuePolicyError) as caught:
            services.enqueue_task(kind="test.add_numbers", payload={"values": [1], "label": SECRET * 5})
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(QueuePolicyError) as caught:
            services.enqueue_task(kind="test.add_numbers", payload=self.GOOD, idempotency_key=SECRET + " bad")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_claim_validates_the_worker_id_and_the_lease_before_any_query(self):
        for worker_id in ("", "x" * 101, None, 5):
            with self.subTest(worker_id=worker_id), self.assertRaises(QueuePolicyError):
                services.claim_next_task(worker_id=worker_id, lease_seconds=60)
        for lease in (0, 4, 3601, None, 1.5):
            with self.subTest(lease=lease), self.assertRaises(QueuePolicyError):
                services.claim_next_task(worker_id="w", lease_seconds=lease)

    def test_worker_mutations_require_a_uuid_token(self):
        failure = Failure(FailureCode.REJECTED, False)
        for token in ("a-string", None, 5, TOKEN.hex):
            with self.subTest(token=repr(token)):
                with self.assertRaises(QueuePolicyError):
                    services.renew_lease(task_id=TASK_ID, lease_token=token, lease_seconds=60)
                with self.assertRaises(QueuePolicyError):
                    services.complete_task(task_id=TASK_ID, lease_token=token, result={})
                with self.assertRaises(QueuePolicyError):
                    services.fail_task(task_id=TASK_ID, lease_token=token, failure=failure)

    def test_a_result_must_be_a_small_json_object(self):
        for result in ("text", None, [1], {"x": 1.5}, {"x": "y" * 5000}):
            with self.subTest(result=repr(result)[:30]), self.assertRaises(QueuePolicyError):
                services.complete_task(task_id=TASK_ID, lease_token=TOKEN, result=result)

    def test_fail_requires_a_classified_failure(self):
        with self.assertRaises(TypeError):
            services.fail_task(task_id=TASK_ID, lease_token=TOKEN, failure=RuntimeError("raw exception"))
        with self.assertRaises(TypeError):
            services.fail_task(task_id=TASK_ID, lease_token=TOKEN, failure="handler_error")

    def test_renew_validates_the_lease_before_any_query(self):
        with self.assertRaises(QueuePolicyError):
            services.renew_lease(task_id=TASK_ID, lease_token=TOKEN, lease_seconds=0)

    def test_recovery_limit_must_be_positive(self):
        for limit in (0, -1, True, 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(QueuePolicyError):
                services.recover_expired_leases(limit=limit)

    def test_the_lease_error_carries_only_a_reason(self):
        error = LeaseLostError("token_mismatch")
        self.assertEqual(error.reason, "token_mismatch")
        self.assertNotIn(str(TOKEN), str(error))


class LoggingConfigurationTests(SimpleTestCase):
    def test_the_worker_logger_is_namespaced_under_the_app(self):
        self.assertEqual(worker.logger.name, "apps.tasks.worker")
        self.assertIsInstance(worker.logger, logging.Logger)


class WorkerSettingsTests(SimpleTestCase):
    def test_defaults(self):
        from django.conf import settings as active

        base = load_settings("config.settings.base")
        self.assertEqual((base.WORKER_LEASE_SECONDS, base.WORKER_POLL_SECONDS), (60, 2))
        self.assertIn("apps.tasks", active.INSTALLED_APPS)

    def test_values_come_from_the_environment(self):
        base = load_settings("config.settings.base", {"AP_WORKER_LEASE_SECONDS": "30", "AP_WORKER_POLL_SECONDS": "5"})
        self.assertEqual((base.WORKER_LEASE_SECONDS, base.WORKER_POLL_SECONDS), (30, 5))

    def test_invalid_values_are_refused_at_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        for env in ({"AP_WORKER_LEASE_SECONDS": "soon"}, {"AP_WORKER_LEASE_SECONDS": "4"}, {"AP_WORKER_POLL_SECONDS": "0"}, {"AP_WORKER_POLL_SECONDS": "x"}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                load_settings("config.settings.base", env)
