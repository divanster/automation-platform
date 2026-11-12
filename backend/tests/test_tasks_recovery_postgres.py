"""Process-level and race tests for worker crash recovery. They REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). Django then creates and later destroys
``test_<AP_DB_NAME>``; ``DedicatedTestDatabaseMixin`` refuses any other name, and every child process
refuses any database name that does not start with ``test_``.

STATUS: written on a development VM that has no PostgreSQL and never run there. Until the project
owner runs them locally they are NOT RUN. Their DB-free support code is tested in
test_tasks_recovery_support.py.

What is real here: separate operating-system processes running ``manage.py runworker`` (through
tests/recovery_worker_child.py), a real forced kill, and real concurrent PostgreSQL sessions. What is
controlled: a lease is made to look expired by giving a child a clock offset (no waiting for 60
seconds), and the blocking handlers are test-only functions in tests/recovery_support.py that are
not part of the production allowlist and are reached only through the existing ``registry``
parameter. No production code is changed.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_tasks_recovery_postgres --tag requires_postgres

WINDOWS LIMITATIONS (explicit): Ctrl+C cannot be sent to one child process on Windows, so the
shutdown tests send CTRL_BREAK_EVENT to the child's process group, which ``runworker`` treats like
Ctrl+C (SIGBREAK); on POSIX they send SIGINT. SIGTERM, ``taskkill`` without ``/F``, and a Windows
service stop are not tested and are not known to be graceful. If the signal never reaches the worker,
the graceful-shutdown test fails with a message saying so (it does not hang). The race tests below
that hold a row lock use ``pg_stat_activity`` to see that a session is waiting; that needs a role
that may see its own sessions (the normal case).

This file is ASCII only.
"""

import tempfile
import threading
import time
from pathlib import Path

from django.db import connection, transaction
from django.test import TransactionTestCase, tag

from apps.tasks import errors, services
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.policy import Failure, FailureCode
from tests import recovery_support as support
from tests import test_tasks_postgres as base

requires_postgres = base.requires_postgres

LONG_LEASE = 300  # seconds; long enough that real elapsed time never expires it by accident
STILL_HELD_OFFSET = 30  # a child clock this far ahead: the 300 s lease is still held
EXPIRED_OFFSET = 400  # ... past the first lease
LATER_OFFSET = 1000  # ... past the recovery backoff (at most 5 s) of the first recovery
LATEST_OFFSET = 5000  # ... past the second attempt's lease (1000 + 300)
FINAL_OFFSET = 10000
SIGNAL_WAIT_SECONDS = 20


def attempts_of(task):
    """``[(attempt_number, outcome, failure_code), ...]`` in order."""
    return list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("attempt_number", "outcome", "failure_code"))


# --- real worker processes -----------------------------------------------------------------------------


class WorkerProcessTestCase(base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """Committed data (TransactionTestCase) is what the child processes see."""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="ap-recovery-")
        self.addCleanup(temp.cleanup)  # runs last: after the children are gone and their logs are closed
        self.directory = Path(temp.name)
        self.workers = support.WorkerProcesses(self.directory, connection.settings_dict["NAME"])
        self.addCleanup(self.workers.close)  # kills any child still running, whatever the test did
        self.registry = support.registry()

    def enqueue(self, kind, label="x", **options):
        return services.enqueue_task(kind=kind, payload={"label": label}, registry=self.registry, **options).task

    def run_once(self, name, *, offset=0, lease=60):
        """Run ``runworker --once`` in a new process to completion; return (exit code, output)."""
        child = self.workers.start_worker(name, "--once", "--lease-seconds", str(lease), clock_offset_seconds=offset)
        code = self.workers.wait(child)
        return code, child.output()

    def kill_and_check_gone(self, child):
        self.workers.kill(child)
        real = child.real_pid()
        self.assertIsNotNone(real, "the child never recorded its process id")
        deadline = time.monotonic() + 10  # termination can take a moment to show up in the process list
        while support.process_exists(real) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(support.process_exists(real), "the killed worker process is still running")
        self.assertNotEqual(child.poll(), 0, "a killed worker must not look like a clean exit")


@tag("requires_postgres")
@requires_postgres
class MultiProcessClaimTests(WorkerProcessTestCase):
    def test_two_worker_processes_hold_two_different_tasks_at_the_same_time(self):
        a = self.enqueue(support.RENDEZVOUS_KIND, "a")
        b = self.enqueue(support.RENDEZVOUS_KIND, "b")
        first = self.workers.start_worker("w1", "--once", "--lease-seconds", str(LONG_LEASE))
        second = self.workers.start_worker("w2", "--once", "--lease-seconds", str(LONG_LEASE))
        # Each handler returns only after BOTH have arrived, so success proves two processes held
        # two tasks at the same moment; a task claimed by both would leave one worker with nothing.
        codes = [self.workers.wait(first), self.workers.wait(second)]
        self.assertEqual(codes, [0, 0], self.workers.describe())
        self.assertEqual(len(self.workers.markers("arrived-*")), 2)
        for task, label in ((a, "a"), (b, "b")):
            stored = Task.objects.get(pk=task.pk)
            self.assertEqual((stored.state, stored.attempt_count, stored.result), ("succeeded", 1, {"ok": 1}), self.workers.describe())
            self.assertEqual(attempts_of(task), [(1, "succeeded", "")])
            self.assertEqual(len(self.workers.markers(f"exec-{label}-*")), 1)  # the handler ran once
        worker_ids = set(TaskAttempt.objects.values_list("worker_id", flat=True))
        self.assertEqual(len(worker_ids), 2)  # one attempt per process, from two different processes
        self.assertEqual(Task.objects.filter(state="running").count(), 0)
        self.assertEqual(TaskAttempt.objects.filter(outcome="running").count(), 0)

    def test_two_worker_processes_released_together_run_one_task_once(self):
        task = self.enqueue(support.WORK_KIND, "r")
        first = self.workers.start_worker("w1", "--once", "--lease-seconds", str(LONG_LEASE), gated=True)
        second = self.workers.start_worker("w2", "--once", "--lease-seconds", str(LONG_LEASE), gated=True)
        self.workers.wait_for_markers("ready-*", count=2, watching=[first, second])
        self.workers.touch(support.GO_FILE)  # both start claiming at about the same moment
        codes = [self.workers.wait(first), self.workers.wait(second)]
        self.assertEqual(codes, [0, 0], self.workers.describe())
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.result), ("succeeded", 1, {"ok": 1}), self.workers.describe())
        self.assertEqual(attempts_of(task), [(1, "succeeded", "")])  # numbering consistent, one attempt, one result
        self.assertEqual(len(self.workers.markers("exec-r-*")), 1, "the handler ran more than once")
        outputs = [first.output(), second.output()]
        self.assertEqual(sum("succeeded." in text for text in outputs), 1, outputs)  # exactly one process ran it
        self.assertEqual(sum("No task available." in text for text in outputs), 1, outputs)  # the other found nothing


@tag("requires_postgres")
@requires_postgres
class KilledWorkerTests(WorkerProcessTestCase):
    def test_a_killed_worker_is_recovered_after_its_lease_and_the_task_completes_once(self):
        task = self.enqueue(support.BLOCK_KIND, max_attempts=3)
        crashing = self.workers.start_worker("crash", "--once", "--lease-seconds", str(LONG_LEASE))
        self.workers.wait_for_markers("started-*", watching=[crashing])  # claimed, and now blocked inside the handler
        claimed = Task.objects.get(pk=task.pk)
        self.assertEqual((claimed.state, claimed.attempt_count), ("running", 1))
        old_token = claimed.lease_token
        self.assertIsNotNone(old_token)

        self.kill_and_check_gone(crashing)

        # Immediately after the kill nothing has changed: the task is still leased to the dead worker.
        after_kill = Task.objects.get(pk=task.pk)
        self.assertEqual((after_kill.state, after_kill.lease_token, after_kill.attempt_count), ("running", old_token, 1))
        self.assertEqual(attempts_of(task), [(1, "running", "")])

        # The lease has not expired yet (a worker whose clock is 30 s ahead of a 300 s lease): nothing is taken over.
        code, output = self.run_once("early", offset=STILL_HELD_OFFSET)
        self.assertEqual(code, 0, output)
        self.assertIn("No task available.", output)
        still = Task.objects.get(pk=task.pk)
        self.assertEqual((still.state, still.lease_token), ("running", old_token))

        # After the lease: a second worker process recovers it. The attempt is abandoned and the task
        # waits out its backoff (so this iteration finds nothing to claim).
        code, output = self.run_once("recover", offset=EXPIRED_OFFSET)
        self.assertEqual(code, 0, output)
        recovered = Task.objects.get(pk=task.pk)
        self.assertEqual((recovered.state, recovered.lease_token, recovered.lease_expires_at, recovered.attempt_count), ("queued", None, None, 1))
        self.assertEqual(attempts_of(task), [(1, "abandoned", "lease_expired")])
        self.assertIsNone(Task.objects.get(pk=task.pk).result)

        # A stale token cannot commit anything, before the retry ...
        self.assert_stale_token_refused(task, old_token)

        # ... then a third worker runs the retry to completion (the handler is released this time).
        self.workers.touch(support.RELEASE_FILE)
        code, output = self.run_once("retry", offset=LATER_OFFSET)
        self.assertEqual(code, 0, output)
        self.assertIn("succeeded.", output)
        final = Task.objects.get(pk=task.pk)
        self.assertEqual((final.state, final.result, final.attempt_count), ("succeeded", {"ok": 1}, 2))
        self.assertEqual((final.lease_token, final.lease_expires_at), (None, None))
        self.assertEqual(attempts_of(task), [(1, "abandoned", "lease_expired"), (2, "succeeded", "")])
        self.assertEqual(TaskAttempt.objects.filter(task=task, outcome="succeeded").count(), 1)  # one stored result
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(len(self.workers.markers("started-*")), 2)  # the handler ran twice: at-least-once, not exactly-once
        self.assertEqual(len(set(TaskAttempt.objects.values_list("worker_id", flat=True))), 2)

        # ... and after it: the dead worker's token still cannot change the finished task.
        self.assert_stale_token_refused(task, old_token)

    def assert_stale_token_refused(self, task, token):
        before = base.snapshot(task)
        with self.assertRaises(errors.LeaseLostError) as caught:
            services.complete_task(task_id=task.pk, lease_token=token, result={"ok": 2})
        self.assertEqual(caught.exception.reason, "not_running")
        with self.assertRaises(errors.LeaseLostError):
            services.fail_task(task_id=task.pk, lease_token=token, failure=Failure(FailureCode.TEMPORARY_FAILURE, retryable=True))
        with self.assertRaises(errors.LeaseLostError):
            services.renew_lease(task_id=task.pk, lease_token=token, lease_seconds=60)
        self.assertEqual(base.snapshot(task), before)  # nothing was written

    def test_a_task_whose_worker_is_killed_on_every_attempt_fails_at_its_attempt_limit(self):
        task = self.enqueue(support.BLOCK_KIND, max_attempts=2)
        first = self.workers.start_worker("crash1", "--once", "--lease-seconds", str(LONG_LEASE))
        self.workers.wait_for_markers("started-*", count=1, watching=[first])
        self.kill_and_check_gone(first)

        code, output = self.run_once("recover1", offset=EXPIRED_OFFSET)
        self.assertEqual(code, 0, output)
        self.assertEqual(Task.objects.get(pk=task.pk).state, "queued")  # one attempt left

        second = self.workers.start_worker("crash2", "--once", "--lease-seconds", str(LONG_LEASE), clock_offset_seconds=LATER_OFFSET)
        self.workers.wait_for_markers("started-*", count=2, watching=[second])  # attempt 2 is running
        running = Task.objects.get(pk=task.pk)
        self.assertEqual((running.state, running.attempt_count), ("running", 2))
        self.kill_and_check_gone(second)

        code, output = self.run_once("recover2", offset=LATEST_OFFSET)
        self.assertEqual(code, 0, output)
        failed = Task.objects.get(pk=task.pk)
        self.assertEqual((failed.state, failed.failure_code, failed.attempt_count), ("failed", "lease_expired", 2))
        self.assertIsNotNone(failed.finished_at)
        self.assertEqual((failed.lease_token, failed.lease_expires_at), (None, None))
        self.assertEqual(attempts_of(task), [(1, "abandoned", "lease_expired"), (2, "abandoned", "lease_expired")])

        # Bounded: a later worker finds nothing, and no third attempt is ever made.
        code, output = self.run_once("after", offset=FINAL_OFFSET)
        self.assertEqual(code, 0, output)
        self.assertIn("No task available.", output)
        self.assertEqual(Task.objects.get(pk=task.pk).attempt_count, 2)
        self.assertEqual(len(self.workers.markers("started-*")), 2)


@tag("requires_postgres")
@requires_postgres
class GracefulShutdownWithActiveTaskTests(WorkerProcessTestCase):
    def start_busy_loop_worker(self, name, task):
        child = self.workers.start_worker(name, "--poll-seconds", "1", "--lease-seconds", str(LONG_LEASE))
        self.workers.wait_for_markers("started-*", watching=[child])  # the continuous worker is inside the task
        running = Task.objects.get(pk=task.pk)
        self.assertEqual((running.state, running.attempt_count), ("running", 1))
        return child

    def deliver_first_signal(self, child):
        self.workers.interrupt(child)
        try:
            self.workers.wait_for_markers("stop-seen-*", watching=[child], timeout=SIGNAL_WAIT_SECONDS)
        except AssertionError as error:
            raise AssertionError(
                "the shutdown signal did not reach the worker while its task was running (on Windows this depends on "
                "CTRL_BREAK_EVENT reaching the child's process group; see the module notes): " + str(error)
            ) from None

    def test_the_first_signal_lets_the_active_task_finish_and_the_worker_exits_successfully(self):
        task = self.enqueue(support.BLOCK_KIND)
        child = self.start_busy_loop_worker("loop", task)

        self.deliver_first_signal(child)
        # The worker has seen the request but must not stop mid-task: it is still alive and the task still running.
        self.assertIsNone(child.poll(), "the worker exited while its task was still running")
        self.assertEqual(Task.objects.get(pk=task.pk).state, "running")

        self.workers.touch(support.RELEASE_FILE)  # let the handler finish
        code = self.workers.wait(child)
        self.assertEqual(code, 0, child.output())
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.result, stored.attempt_count), ("succeeded", {"ok": 1}, 1))
        self.assertEqual(attempts_of(task), [(1, "succeeded", "")])
        self.assertIn("Worker stopped after processing 1 task(s).", child.output())

    def test_a_second_signal_interrupts_at_once_and_the_task_is_recovered_after_its_lease(self):
        task = self.enqueue(support.BLOCK_KIND)
        child = self.start_busy_loop_worker("hard", task)
        old_token = Task.objects.get(pk=task.pk).lease_token

        self.deliver_first_signal(child)
        self.workers.interrupt(child)  # the second signal is a hard interrupt
        code = self.workers.wait(child)
        self.assertEqual(code, 130, child.output())

        # The interrupted task was not recorded: it is still leased, and only the lease expiry frees it.
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.lease_token), ("running", old_token))
        self.assertEqual(attempts_of(task), [(1, "running", "")])

        code, output = self.run_once("recover", offset=EXPIRED_OFFSET)
        self.assertEqual(code, 0, output)
        self.assertEqual(Task.objects.get(pk=task.pk).state, "queued")
        self.assertEqual(attempts_of(task), [(1, "abandoned", "lease_expired")])


# --- recovery races (separate PostgreSQL connections) ----------------------------------------------------


class Background:
    """Run a callable on its own thread and connection with a lock timeout; collect ``("ok", value)`` or ``("error", error)``."""

    def __init__(self, target):
        self.outcome = None
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()

    def _run(self, target):
        try:
            with connection.cursor() as cursor:  # this thread's own connection
                cursor.execute(f"SET lock_timeout = '{base.LOCK_TIMEOUT_SECONDS}s'")
            self.outcome = ("ok", target())
        except Exception as error:  # recorded and asserted on by the test
            self.outcome = ("error", error)
        finally:
            connection.close()

    def result(self):
        self._thread.join(timeout=base.JOIN_TIMEOUT)
        if self._thread.is_alive():
            raise AssertionError("a background thread did not finish within the bound (possible deadlock)")
        return self.outcome


@tag("requires_postgres")
@requires_postgres
class RecoveryRaceTests(base.ThreadedMixin, base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """Worker mutation versus recovery on one expired task.

    The first six tests are ORDERED: the main connection keeps one operation uncommitted (it holds
    the row lock) while a second connection runs the competing operation, so each order is forced
    rather than hoped for. The last three let both operations race on a barrier with no ordering and
    accept either winner, but check that exactly one won and that the stored state is consistent.
    The task is claimed at T0 with a 60 s lease, so it expires at T0+60; a worker acts at T0+59
    (lease held) and recovery at T0+61 (lease expired), which is only possible when they run in
    different instants, as they do in production.
    """

    def setUp(self):
        super().setUp()
        self.task = base.enqueue()
        self.claimed = base.claim(now=base.T0, lease=60)
        self.token = self.claimed.lease_token

    # helpers

    def recover(self):
        return services.recover_expired_leases(now=base.at(61), rng=base.zero)

    def complete(self):
        return services.complete_task(task_id=self.task.pk, lease_token=self.token, result={"count": 1, "sum": 1}, now=base.at(59))

    def fail_with_retry(self):
        return services.fail_task(task_id=self.task.pk, lease_token=self.token, failure=base.RETRYABLE, now=base.at(59), rng=base.zero)

    def renew(self):
        return services.renew_lease(task_id=self.task.pk, lease_token=self.token, lease_seconds=60, now=base.at(59))

    def stored(self):
        return Task.objects.get(pk=self.task.pk)

    def wait_until_a_session_waits_for_a_lock(self):
        deadline = time.monotonic() + base.LOCK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")
                if cursor.fetchone()[0] >= 1:
                    return
            time.sleep(0.05)
        self.fail("no session ever waited for the row lock")

    def assert_lease_lost(self, outcome, reason="not_running"):
        kind, value = outcome
        self.assertEqual(kind, "error", outcome)
        self.assertIsInstance(value, errors.LeaseLostError)
        self.assertEqual(value.reason, reason)

    def assert_recovered(self, available_at):
        stored = self.stored()
        self.assertEqual((stored.state, stored.lease_token, stored.lease_expires_at, stored.attempt_count), ("queued", None, None, 1))
        self.assertEqual(stored.available_at, available_at)
        self.assertIsNone(stored.result)
        self.assertEqual(attempts_of(self.task), [(1, "abandoned", "lease_expired")])

    # ordered: recovery first, worker blocked then refused

    def test_completion_arriving_while_recovery_holds_the_row_is_refused(self):
        with transaction.atomic():
            self.assertEqual(self.recover(), (1, 0))  # requeued, not yet committed: this connection holds the row lock
            blocked = Background(self.complete)
            self.wait_until_a_session_waits_for_a_lock()
        self.assert_lease_lost(blocked.result())
        self.assert_recovered(base.at(66))  # 61 s + 5 s backoff

    def test_failure_arriving_while_recovery_holds_the_row_is_refused(self):
        with transaction.atomic():
            self.assertEqual(self.recover(), (1, 0))
            blocked = Background(self.fail_with_retry)
            self.wait_until_a_session_waits_for_a_lock()
        self.assert_lease_lost(blocked.result())
        self.assert_recovered(base.at(66))  # the failed-attempt path would have given at(64) and a "temporary_failure" attempt

    def test_renewal_arriving_while_recovery_holds_the_row_is_refused(self):
        with transaction.atomic():
            self.assertEqual(self.recover(), (1, 0))
            blocked = Background(self.renew)
            self.wait_until_a_session_waits_for_a_lock()
        self.assert_lease_lost(blocked.result())
        self.assert_recovered(base.at(66))

    # ordered: worker first, recovery skips the locked row

    def test_recovery_skips_a_row_whose_completion_is_not_yet_committed(self):
        with transaction.atomic():
            self.complete()  # uncommitted: holds the row lock
            self.assertEqual(Background(self.recover).result(), ("ok", (0, 0)))  # skipped, not blocked, not requeued
        stored = self.stored()
        self.assertEqual((stored.state, stored.result, stored.attempt_count, stored.finished_at), ("succeeded", {"count": 1, "sum": 1}, 1, base.at(59)))
        self.assertEqual(attempts_of(self.task), [(1, "succeeded", "")])
        self.assertEqual(self.recover(), (0, 0))  # and nothing is left for a later recovery

    def test_recovery_skips_a_row_whose_failure_is_not_yet_committed(self):
        with transaction.atomic():
            self.fail_with_retry()  # uncommitted: the task is requeued by the failure path
            self.assertEqual(Background(self.recover).result(), ("ok", (0, 0)))
        stored = self.stored()
        self.assertEqual((stored.state, stored.lease_token, stored.attempt_count, stored.available_at), ("queued", None, 1, base.at(64)))
        self.assertEqual(attempts_of(self.task), [(1, "failed", "temporary_failure")])  # the failure, not an abandonment
        self.assertEqual(self.recover(), (0, 0))

    def test_recovery_skips_a_row_whose_renewal_is_not_yet_committed_and_the_renewed_lease_holds(self):
        with transaction.atomic():
            self.renew()  # uncommitted: the new expiry is T0+119
            self.assertEqual(Background(self.recover).result(), ("ok", (0, 0)))
        stored = self.stored()
        self.assertEqual((stored.state, stored.lease_token, stored.lease_expires_at), ("running", self.token, base.at(119)))
        self.assertEqual(self.recover(), (0, 0))  # still not expired at T0+61, after the commit
        self.assertEqual(attempts_of(self.task), [(1, "running", "")])
        services.complete_task(task_id=self.task.pk, lease_token=self.token, result={"count": 1, "sum": 1}, now=base.at(100))
        self.assertEqual(self.stored().state, "succeeded")

    # unordered: a real race, either winner, one consistent result

    def race(self, worker_call, check_worker_won, check_recovery_won, rounds=8):
        """Repeat a barrier-started race on fresh tasks; each round must end in exactly one of two consistent states."""
        wins = {"worker": 0, "recovery": 0}
        for round_number in range(rounds):
            task = self.task if round_number == 0 else base.enqueue(payload={"values": [round_number]})
            if round_number:
                claimed = base.claim(now=base.T0, lease=60, worker_id=f"w-{round_number}")
                self.assertEqual(claimed.task_id, task.pk)
                self.task, self.token = task, claimed.lease_token
            results = self.run_threads([worker_call, self.recover])
            worker_result, recovery_result = results
            self.assertEqual(recovery_result[0], "ok", recovery_result)
            if worker_result[0] == "ok":
                wins["worker"] += 1
                self.assertEqual(recovery_result[1], (0, 0), results)
                check_worker_won()
            else:
                wins["recovery"] += 1
                self.assert_lease_lost(worker_result)
                self.assertEqual(recovery_result[1], (1, 0), results)
                check_recovery_won()
            self.assertEqual(TaskAttempt.objects.filter(task=task).count(), 1)  # one attempt, one outcome
        return wins

    def test_completion_racing_recovery_has_exactly_one_winner(self):
        def worker_won():
            self.assertEqual(self.stored().state, "succeeded")
            self.assertEqual(attempts_of(self.task), [(1, "succeeded", "")])

        def recovery_won():
            self.assert_recovered(base.at(66))

        self.race(self.complete, worker_won, recovery_won)

    def test_failure_racing_recovery_has_exactly_one_winner(self):
        def worker_won():
            self.assertEqual((self.stored().state, self.stored().available_at), ("queued", base.at(64)))
            self.assertEqual(attempts_of(self.task), [(1, "failed", "temporary_failure")])

        def recovery_won():
            self.assert_recovered(base.at(66))

        self.race(self.fail_with_retry, worker_won, recovery_won)

    def test_renewal_racing_recovery_has_exactly_one_winner(self):
        def worker_won():
            stored = self.stored()
            self.assertEqual((stored.state, stored.lease_token, stored.lease_expires_at), ("running", self.token, base.at(119)))
            self.assertEqual(attempts_of(self.task), [(1, "running", "")])

        def recovery_won():
            self.assert_recovered(base.at(66))

        self.race(self.renew, worker_won, recovery_won)
