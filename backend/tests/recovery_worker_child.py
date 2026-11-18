"""Test-only child process for the process-level queue tests (not a test module).

Started by ``tests.recovery_support.WorkerProcesses``; it runs the REAL ``manage.py runworker``
command (real claim, lease, recovery, signal handling, console output, exit codes) in a separate
operating-system process, with exactly three test-only differences, all applied through parameters the
production code already has (nothing in ``apps.tasks`` is modified):

* ``worker.process_one`` is wrapped so that it receives ``registry=`` (the production allowlist plus
  the test handlers of ``recovery_support``) and ``clock=`` (the real clock plus
  ``--clock-offset-seconds``, so a test can make a lease look expired without waiting);
* ``worker.run_until_stopped`` is wrapped so that it uses that process function and so that the test
  handler can see the worker's stop flag;
* ``worker.install_composition`` is wrapped so that the production composition's REAL kind gate (MOD-1b S4: the legacy kinds, plus the
  kinds of enabled modules) is installed with exactly the three test kinds of ``recovery_support`` added to its answer. Without this the
  production gate holds every ``recovery.*`` task in ``queued`` (they are neither legacy nor a module's kinds), the worker reports "No task
  available" and no process-level test can run. The gate stays in the loop: any other kind is still held, and a gate that fails or answers
  badly still allows nothing (the wrapper raises on a non-collection answer, which the worker treats as a failed gate).

Safety: it refuses to start unless the configured database name starts with ``test_`` (the database
Django creates for tests), it exits by itself after ``--max-seconds`` (a watchdog), and every wait in
it is bounded. Usage (internal): ``python recovery_worker_child.py --name N [--gate] [options] <runworker args>``.

This file is ASCII only.
"""

import argparse
import dataclasses
import os
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
GATE_WAIT_SECONDS = 60
WATCHDOG_EXIT_CODE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test-only runworker child process.", allow_abbrev=False)
    parser.add_argument("--name", required=True, help="Marker-file name for this child (letters, digits, dash).")
    parser.add_argument("--clock-offset-seconds", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=240, help="Watchdog: exit this child after this many seconds.")
    parser.add_argument("--gate", action="store_true", help="Wait for the 'go' marker before starting to work.")
    return parser


def refuse_unless_test_database(name) -> None:
    """Exit with a message unless ``name`` is a Django test database name (``test_*``)."""
    if not str(name).startswith("test_"):
        raise SystemExit("recovery_worker_child refuses to run: the database is not a dedicated test database (test_*).")


def start_watchdog(max_seconds: int) -> threading.Thread:
    """Terminate this process after ``max_seconds`` so an orphaned child can never run on."""

    def expire():
        time.sleep(max_seconds)
        os._exit(WATCHDOG_EXIT_CODE)

    thread = threading.Thread(target=expire, name="watchdog", daemon=True)
    thread.start()
    return thread


def install_patches(clock_offset_seconds: int):
    """Wrap ``worker.process_one`` and ``worker.run_until_stopped`` (see the module docstring)."""
    from django.utils import timezone

    from apps.tasks import worker
    from tests import recovery_support

    offset = timedelta(seconds=clock_offset_seconds)
    registry = recovery_support.registry()
    original_process_one = worker.process_one
    original_run_until_stopped = worker.run_until_stopped

    def clock():
        return timezone.now() + offset

    def process_one(**kwargs):
        return original_process_one(registry=registry, clock=clock, **kwargs)

    def run_until_stopped(**kwargs):
        recovery_support.STOP = kwargs["stop"]
        return original_run_until_stopped(process=process_one, **kwargs)

    original_install_composition = worker.install_composition
    test_kinds = frozenset(recovery_support.TEST_KINDS)

    def install_composition(composition):
        production_gate = composition.kind_gate
        if production_gate is not None:

            def gate_with_test_kinds():
                answer = production_gate()  # the production gate itself: it may raise (a DatabaseError included), which the worker handles as before
                if not isinstance(answer, (set, frozenset, list, tuple)):
                    raise TypeError("the production gate answered with something that is not a collection of kinds")
                return frozenset(answer) | test_kinds

            composition = dataclasses.replace(composition, kind_gate=gate_with_test_kinds)
        original_install_composition(composition)

    worker.process_one = process_one
    worker.run_until_stopped = run_until_stopped
    worker.install_composition = install_composition
    return clock


def wait_at_gate(directory: Path, name: str) -> None:
    """Announce ``ready-<name>`` and wait (bounded) for the parent's ``go`` marker."""
    from tests import recovery_support

    (directory / f"ready-{name}").touch()
    deadline = time.monotonic() + GATE_WAIT_SECONDS
    while not (directory / recovery_support.GO_FILE).exists():
        if time.monotonic() > deadline:
            raise SystemExit("recovery_worker_child: the go marker never appeared.")
        time.sleep(recovery_support.POLL_SECONDS)


def main(argv=None) -> None:
    options, runworker_args = build_parser().parse_known_args(argv)
    sys.path.insert(0, str(BACKEND_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

    import django

    django.setup()
    from django.conf import settings

    refuse_unless_test_database(settings.DATABASES["default"]["NAME"])
    start_watchdog(options.max_seconds)

    from tests import recovery_support

    directory = recovery_support.control_dir()
    (directory / f"pid-{options.name}").write_text(str(os.getpid()))  # the real process id (see recovery_support notes)
    install_patches(options.clock_offset_seconds)
    if options.gate:
        wait_at_gate(directory, options.name)

    from django.core.management import execute_from_command_line

    execute_from_command_line(["manage.py", "runworker", *runworker_args])


if __name__ == "__main__":
    main()
