"""Database-free tests of the support code behind tests/test_tasks_recovery_postgres.py.

They run in the default ``manage.py test`` (no PostgreSQL, no worker is started). They check the
parts of the process-level tests that can be checked without a database: the test-only handlers and
their registry (and that the production allowlist is untouched), the child script's refusal and
patching logic, and the process helper's bounded waits, kills, and cleanup, using throw-away
``python -c`` children. This file is ASCII only.
"""

import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone

from apps.tasks import handlers, worker
from apps.tasks.policy import InvalidPayloadError, TaskPermanentError
from tests import recovery_support as support
from tests import recovery_worker_child as child_script


class ControlDirectoryMixin:
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="ap-recovery-unit-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {support.CONTROL_DIR_ENV: str(self.directory)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self, pattern):
        return sorted(path.name for path in self.directory.glob(pattern))


class RegistryTests(SimpleTestCase):
    def test_the_production_allowlist_does_not_contain_the_test_handlers(self):
        for kind in (support.BLOCK_KIND, support.RENDEZVOUS_KIND, support.WORK_KIND):
            self.assertNotIn(kind, handlers.HANDLERS)
        self.assertEqual(set(handlers.HANDLERS), {"test.add_numbers"})

    def test_the_test_registry_is_the_production_allowlist_plus_exactly_the_three_test_handlers(self):
        registry = support.registry()
        self.assertEqual(set(registry) - set(handlers.HANDLERS), {support.BLOCK_KIND, support.RENDEZVOUS_KIND, support.WORK_KIND})
        for kind in handlers.HANDLERS:
            self.assertIs(registry[kind], handlers.HANDLERS[kind])  # the production entries are reused as they are
        for kind in (support.BLOCK_KIND, support.RENDEZVOUS_KIND, support.WORK_KIND):
            self.assertTrue(registry[kind].is_test_handler)
            self.assertEqual(registry[kind].kind, kind)

    def test_labels_are_short_ascii_alphanumerics(self):
        self.assertEqual(support.validate_label_payload({}), {"label": "x"})
        self.assertEqual(support.validate_label_payload({"label": "a1"}), {"label": "a1"})
        for bad in ("", "a" * 21, "../x", "a-b", "a b", "\u00e9", 5, None):
            with self.assertRaises(InvalidPayloadError, msg=repr(bad)):
                support.validate_label_payload({"label": bad})
        for bad in ("text", None, [], {"label": "a", "other": 1}):
            with self.assertRaises(InvalidPayloadError):
                support.validate_label_payload(bad)


class HandlerTests(ControlDirectoryMixin, SimpleTestCase):
    def test_the_block_handler_waits_for_the_release_file(self):
        outcome = {}

        def run():
            outcome["result"] = support.run_block({"label": "b"})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not self.names("started-b-*") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.names("started-b-*"), [f"started-b-{os.getpid()}"])
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())  # still blocked: nothing has released it
        (self.directory / support.RELEASE_FILE).touch()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"], {"ok": 1})

    def test_the_block_handler_notes_a_shutdown_request_while_it_runs(self):
        stop = threading.Event()
        thread = threading.Thread(target=lambda: support.run_block({"label": "b"}), daemon=True)
        with mock.patch.object(support, "STOP", stop):
            thread.start()
            time.sleep(0.2)
            self.assertEqual(self.names("stop-seen-*"), [])
            stop.set()
            deadline = time.monotonic() + 10
            while not self.names("stop-seen-*") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.names("stop-seen-*"), [f"stop-seen-{os.getpid()}"])
            self.assertTrue(thread.is_alive())  # noting the request does not end the task
            (self.directory / support.RELEASE_FILE).touch()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_a_blocked_handler_gives_up_after_its_bound_with_a_safe_code(self):
        with mock.patch.object(support, "HANDLER_MAX_WAIT_SECONDS", 0.2):
            with self.assertRaises(TaskPermanentError):
                support.run_block({"label": "b"})

    def test_the_rendezvous_returns_only_when_two_handlers_have_arrived(self):
        results = {}

        def run(label):
            results[label] = support.run_rendezvous({"label": label})

        first = threading.Thread(target=run, args=("a",), daemon=True)
        first.start()
        time.sleep(0.3)
        self.assertTrue(first.is_alive())  # alone, it keeps waiting
        second = threading.Thread(target=run, args=("b",), daemon=True)
        second.start()
        for thread in (first, second):
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, {"a": {"ok": 1}, "b": {"ok": 1}})
        self.assertEqual(len(self.names("exec-*")), 2)

    def test_a_lone_rendezvous_gives_up_after_its_bound(self):
        with mock.patch.object(support, "RENDEZVOUS_MAX_WAIT_SECONDS", 0.2):
            with self.assertRaises(TaskPermanentError):
                support.run_rendezvous({"label": "a"})

    def test_the_work_handler_records_that_it_ran(self):
        self.assertEqual(support.run_work({"label": "w"}), {"ok": 1})
        self.assertEqual(self.names("exec-w-*"), [f"exec-w-{os.getpid()}"])

    def test_without_a_control_directory_a_handler_fails_with_a_safe_code(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(support.CONTROL_DIR_ENV)
            with self.assertRaises(TaskPermanentError):
                support.run_work({"label": "w"})


class ChildScriptTests(ControlDirectoryMixin, SimpleTestCase):
    def test_only_a_test_database_is_accepted(self):
        child_script.refuse_unless_test_database("test_automation")
        for name in ("automation", "", None, "prod_test_", "Test_x"):
            with self.assertRaises(SystemExit, msg=repr(name)):
                child_script.refuse_unless_test_database(name)

    def test_main_refuses_a_non_test_database_before_doing_anything(self):
        with mock.patch.dict(settings.DATABASES["default"], {"NAME": "automation"}):
            with mock.patch("django.setup"), mock.patch.object(child_script, "start_watchdog") as watchdog:
                with mock.patch.object(child_script, "install_patches") as patches:
                    with self.assertRaises(SystemExit):
                        child_script.main(["--name", "x", "--once"])
        watchdog.assert_not_called()
        patches.assert_not_called()
        self.assertEqual(self.names("pid-*"), [])

    def test_unknown_arguments_pass_through_to_runworker(self):
        options, rest = child_script.build_parser().parse_known_args(
            ["--name", "n", "--clock-offset-seconds", "400", "--gate", "--once", "--lease-seconds", "300", "--poll-seconds", "1"]
        )
        self.assertEqual((options.name, options.clock_offset_seconds, options.gate), ("n", 400, True))
        self.assertEqual(rest, ["--once", "--lease-seconds", "300", "--poll-seconds", "1"])

    def test_the_watchdog_ends_the_process_after_its_bound(self):
        with mock.patch.object(child_script.os, "_exit") as exit_now, mock.patch.object(child_script.time, "sleep") as sleep:
            child_script.start_watchdog(7).join(timeout=10)
        sleep.assert_called_once_with(7)
        exit_now.assert_called_once_with(child_script.WATCHDOG_EXIT_CODE)

    def test_the_patches_inject_the_registry_and_a_shifted_clock_into_the_existing_worker_parameters(self):
        with mock.patch.object(worker, "process_one") as process_one, mock.patch.object(worker, "run_until_stopped") as run_until_stopped:
            with mock.patch.object(support, "STOP", None):
                clock = child_script.install_patches(400)
                shifted = clock()
                self.assertTrue(timezone.now() + timedelta(seconds=399) < shifted < timezone.now() + timedelta(seconds=401))

                worker.process_one(worker_id="w", lease_seconds=60)
                kwargs = process_one.call_args.kwargs
                self.assertEqual((kwargs["worker_id"], kwargs["lease_seconds"]), ("w", 60))
                self.assertEqual(set(kwargs["registry"]) - set(handlers.HANDLERS), {support.BLOCK_KIND, support.RENDEZVOUS_KIND, support.WORK_KIND})
                self.assertGreater(kwargs["clock"](), timezone.now() + timedelta(seconds=399))

                stop = threading.Event()
                worker.run_until_stopped(worker_id="w", lease_seconds=60, poll_seconds=1, stop=stop)
                self.assertIs(support.STOP, stop)  # the test handler can see the worker's stop flag
                call = run_until_stopped.call_args.kwargs
                self.assertEqual((call["worker_id"], call["poll_seconds"], call["stop"]), ("w", 1, stop))
                call["process"](worker_id="w", lease_seconds=60)  # the loop's process function is the patched one
                self.assertEqual(process_one.call_count, 2)

    def test_the_gate_announces_itself_and_waits_for_go(self):
        thread = threading.Thread(target=child_script.wait_at_gate, args=(self.directory, "g"), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not self.names("ready-g") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.names("ready-g"), ["ready-g"])
        self.assertTrue(thread.is_alive())
        (self.directory / support.GO_FILE).touch()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_a_gate_that_is_never_opened_gives_up(self):
        with mock.patch.object(child_script, "GATE_WAIT_SECONDS", 0.2):
            with self.assertRaises(SystemExit):
                child_script.wait_at_gate(self.directory, "g")

    def test_the_child_script_exists_next_to_the_support_module_and_is_not_collected_as_a_test(self):
        self.assertTrue(support.CHILD_SCRIPT.is_file())
        self.assertFalse(support.CHILD_SCRIPT.name.startswith("test"))
        self.assertFalse(Path(support.__file__).name.startswith("test"))


class WorkerProcessesTests(SimpleTestCase):
    """Uses throw-away ``python -c`` children: no worker, no database."""

    SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="ap-recovery-unit-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.workers = support.WorkerProcesses(self.directory, "test_unit")
        self.addCleanup(self.workers.close)

    def test_the_child_environment_names_the_test_database_and_the_control_directory(self):
        env = self.workers._environment()
        self.assertEqual((env["AP_DB_NAME"], env[support.CONTROL_DIR_ENV]), ("test_unit", str(self.directory)))

    def test_a_kill_terminates_the_child_without_a_clean_exit(self):
        child = self.workers.start_command("s1", self.SLEEPER)
        self.assertIsNone(child.poll())
        self.assertTrue(support.process_exists(child.pid))
        self.workers.kill(child)
        self.assertIsNotNone(child.poll())
        self.assertNotEqual(child.poll(), 0)
        self.assertFalse(support.process_exists(child.pid))

    def test_close_kills_a_child_that_is_still_running_and_can_be_repeated(self):
        child = self.workers.start_command("s2", self.SLEEPER)
        self.workers.close()
        self.assertIsNotNone(child.poll())
        self.workers.close()  # idempotent

    def test_a_child_that_outlives_the_wait_bound_is_killed_and_reported(self):
        child = self.workers.start_command("s3", self.SLEEPER)
        with self.assertRaises(AssertionError) as caught:
            self.workers.wait(child, timeout=0.5)
        self.assertIn("did not exit", str(caught.exception))
        self.assertIsNotNone(child.poll())

    def test_a_finished_child_reports_its_exit_code_and_output(self):
        child = self.workers.start_command("s4", [sys.executable, "-c", "print('hello'); raise SystemExit(7)"])
        self.assertEqual(self.workers.wait(child), 7)
        self.assertIn("hello", child.output())
        self.assertFalse(support.process_exists(child.pid))

    def test_waiting_for_markers_returns_them_and_times_out_with_the_children_output(self):
        child = self.workers.start_command("s5", self.SLEEPER)
        threading.Timer(0.2, lambda: (self.directory / "started-a-1").touch()).start()
        self.assertEqual(self.workers.wait_for_markers("started-*", watching=[child], timeout=10), ["started-a-1"])
        with self.assertRaises(AssertionError) as caught:
            self.workers.wait_for_markers("never-*", timeout=0.3)
        self.assertIn("never-*", str(caught.exception))

    def test_waiting_for_a_marker_fails_at_once_when_a_watched_child_has_exited(self):
        child = self.workers.start_command("s6", [sys.executable, "-c", "raise SystemExit(0)"])
        self.workers.wait(child)
        started = time.monotonic()
        with self.assertRaises(AssertionError) as caught:
            self.workers.wait_for_markers("never-*", watching=[child], timeout=30)
        self.assertIn("exited", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10)

    def test_the_real_process_id_is_read_from_the_childs_marker(self):
        child = self.workers.start_command("s7", self.SLEEPER)
        self.assertIsNone(child.real_pid())
        (self.directory / "pid-s7").write_text("12345")
        self.assertEqual(child.real_pid(), 12345)

    def test_process_exists_is_true_for_this_process_and_false_for_a_finished_one(self):
        self.assertTrue(support.process_exists(os.getpid()))
        child = self.workers.start_command("s8", [sys.executable, "-c", "pass"])
        self.workers.wait(child)
        self.assertFalse(support.process_exists(child.pid))
