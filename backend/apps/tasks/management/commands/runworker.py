"""Run the persistent task worker.

    python manage.py runworker --once       # process at most one available task, then exit
    python manage.py runworker              # poll until Ctrl+C

In this slice the only registered handler is the deterministic TEST handler ``test.add_numbers``;
nothing else can run. The first Ctrl+C (or SIGTERM / Ctrl+Break) finishes the current task and
exits; a second Ctrl+C interrupts at once, and an interrupted task is recovered after its lease
expires. Handlers may run more than once. No payloads or results are printed or logged.
"""

import logging
import signal
import sys
import threading
from contextlib import contextmanager

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections

from apps.tasks import worker
from apps.tasks.policy import QueuePolicyError, validate_lease_seconds, validate_poll_seconds

_SIGNAL_NAMES = ("SIGINT", "SIGTERM", "SIGBREAK")  # SIGBREAK exists on Windows only


@contextmanager
def shutdown_on_signals(stop: threading.Event):
    """Set ``stop`` on the first signal; a second one raises ``KeyboardInterrupt`` (hard interrupt).

    The handler only sets a flag, which is safe inside a signal handler. Previous handlers are
    restored on exit. Signals can only be installed from the main thread; elsewhere this is a no-op.
    """

    def handler(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()

    previous = {}
    for name in _SIGNAL_NAMES:
        number = signal.Signals.__members__.get(name)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, handler)
        except (ValueError, OSError):  # not the main thread, or a signal this platform will not accept
            continue
    try:
        yield
    finally:
        for number, old in previous.items():
            signal.signal(number, old)


@contextmanager
def console_logging(stream=None):
    """Send the queue's log lines (task ids, kinds, statuses, safe codes) to the console while the command runs."""
    logger = logging.getLogger("apps.tasks")
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class Command(BaseCommand):
    help = "Run the persistent task worker (the deterministic test handler only, in this slice)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one available task and exit.")
        parser.add_argument("--poll-seconds", type=int, default=None, help="Seconds to wait when idle (1 to 60). Default: AP_WORKER_POLL_SECONDS.")
        parser.add_argument("--lease-seconds", type=int, default=None, help="Lease length per attempt (5 to 3600). Default: AP_WORKER_LEASE_SECONDS.")

    def handle(self, *args, **options):
        try:
            poll_seconds = validate_poll_seconds(settings.WORKER_POLL_SECONDS if options["poll_seconds"] is None else options["poll_seconds"])
            lease_seconds = validate_lease_seconds(settings.WORKER_LEASE_SECONDS if options["lease_seconds"] is None else options["lease_seconds"])
        except QueuePolicyError as error:
            raise CommandError(str(error)) from None

        worker_id = worker.make_worker_id()
        try:
            with console_logging():
                if options["once"]:
                    self._run_once(worker_id, lease_seconds)
                else:
                    self._run_loop(worker_id, lease_seconds, poll_seconds)
        except DatabaseError as error:
            # The driver's message can contain connection details; report the class only.
            raise CommandError(f"Database error ({type(error).__name__}); see the log.") from None
        except KeyboardInterrupt:
            raise CommandError("Interrupted. A task being run is recovered after its lease expires.", returncode=130) from None
        finally:
            connections.close_all()

    def _run_once(self, worker_id, lease_seconds):
        outcome = worker.process_one(worker_id=worker_id, lease_seconds=lease_seconds)
        if outcome.status is worker.ProcessStatus.IDLE:
            self.stdout.write("No task available.")
        else:
            detail = f" ({outcome.failure_code})" if outcome.failure_code else ""
            self.stdout.write(f"Task {outcome.task_id} {outcome.status.value}{detail}.")

    def _run_loop(self, worker_id, lease_seconds, poll_seconds):
        stop = threading.Event()
        self.stdout.write(f"Worker {worker_id} started (lease {lease_seconds}s, poll {poll_seconds}s). Press Ctrl+C to stop after the current task.")
        with shutdown_on_signals(stop):
            processed = worker.run_until_stopped(worker_id=worker_id, lease_seconds=lease_seconds, poll_seconds=poll_seconds, stop=stop)
        self.stdout.write(f"Worker stopped after processing {processed} task(s).")
