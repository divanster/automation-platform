"""Test-only support for the process-level queue tests (not a test module; no production code changes).

Two parts:

1. Controlled TEST handlers (``recovery.block``, ``recovery.rendezvous``, ``recovery.work``). They are
   NOT in the production allowlist ``apps.tasks.handlers.HANDLERS``. They exist only in the registry
   built by ``registry()``, which the tests pass to ``enqueue_task(registry=...)`` and which the child
   script (``recovery_worker_child.py``) patches into the worker through the existing ``registry``
   parameter of ``process_one``. Nothing is imported by name, evaluated, or resolved from a string:
   the handlers are plain functions in a fixed mapping, as in ``apps.tasks.handlers``.
   They coordinate with the parent test only through empty marker files in one control directory
   (the path comes from ``AP_RECOVERY_TEST_DIR``). Payloads are synthetic: ``{"label": "<letters>"}``.

2. ``WorkerProcesses``: starts real ``manage.py runworker`` child processes (through the child
   script), waits for them with bounds, delivers or simulates signals, kills them, and guarantees that
   none outlives its test (explicit ``close()``, plus an ``atexit`` hook, plus a watchdog inside every
   child that exits it after ``--max-seconds``).

WINDOWS NOTES. (a) A virtual environment's ``python.exe`` is a launcher that starts the real
interpreter as a child process, so killing only ``Popen.pid`` could leave the real worker running.
The child therefore records its real process id in a marker file, and killing uses
``taskkill /T /F`` on the launcher and on the recorded id. (b) Ctrl+C cannot be sent to one child
process on Windows. ``interrupt()`` sends CTRL_BREAK_EVENT to the child's own process group
(``runworker`` treats SIGBREAK like Ctrl+C); on POSIX it sends SIGINT. SIGTERM, ``taskkill`` without
``/F``, and a Windows service stop are NOT exercised and are not known to be graceful on Windows.

This file is ASCII only.
"""

import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from apps.tasks import handlers
from apps.tasks.policy import InvalidPayloadError, TaskPermanentError

CONTROL_DIR_ENV = "AP_RECOVERY_TEST_DIR"

BLOCK_KIND = "recovery.block"  # blocks until the parent creates RELEASE_FILE
RENDEZVOUS_KIND = "recovery.rendezvous"  # returns only once two workers are inside it at the same time
WORK_KIND = "recovery.work"  # records that it ran, then returns

RELEASE_FILE = "release"
GO_FILE = "go"

POLL_SECONDS = 0.05
HANDLER_MAX_WAIT_SECONDS = 120  # a handler never blocks forever, even if the parent disappears
RENDEZVOUS_MAX_WAIT_SECONDS = 30

MARKER_WAIT_SECONDS = 60
CHILD_WAIT_SECONDS = 90
CHILD_MAX_SECONDS = 240  # the watchdog inside every child

MAX_LABEL_LENGTH = 20

# Set by the child script to the worker's stop flag (a threading.Event); None in a --once worker.
STOP = None


# --- the test handlers -------------------------------------------------------------------------------


def control_dir() -> Path:
    value = os.environ.get(CONTROL_DIR_ENV, "")
    if not value:
        raise TaskPermanentError()  # a safe code only; never reached in a correctly started child
    return Path(value)


def _touch(path: Path) -> None:
    path.touch(exist_ok=True)


def validate_label_payload(payload) -> dict:
    """``{"label": 1 to 20 ASCII letters or digits}`` (the label becomes part of a marker file name)."""
    if not isinstance(payload, dict) or set(payload) - {"label"}:
        raise InvalidPayloadError("payload must be an object with an optional label")
    label = payload.get("label", "x")
    if not isinstance(label, str) or not 1 <= len(label) <= MAX_LABEL_LENGTH or not (label.isascii() and label.isalnum()):
        raise InvalidPayloadError(f"label must be 1 to {MAX_LABEL_LENGTH} ASCII letters or digits")
    return {"label": label}


def run_block(payload: dict) -> dict:
    """Record ``started-<label>-<pid>``, then wait for the release file (noting a shutdown request)."""
    directory = control_dir()
    _touch(directory / f"started-{payload['label']}-{os.getpid()}")
    deadline = time.monotonic() + HANDLER_MAX_WAIT_SECONDS
    stop_noted = False
    while not (directory / RELEASE_FILE).exists():
        if STOP is not None and STOP.is_set() and not stop_noted:
            _touch(directory / f"stop-seen-{os.getpid()}")  # the signal reached the worker while this task ran
            stop_noted = True
        if time.monotonic() > deadline:
            raise TaskPermanentError()
        time.sleep(POLL_SECONDS)
    return {"ok": 1}


def run_rendezvous(payload: dict) -> dict:
    """Return only after two ``arrived-*`` markers exist: proves two workers hold two tasks at once."""
    directory = control_dir()
    _touch(directory / f"arrived-{payload['label']}-{os.getpid()}")
    deadline = time.monotonic() + RENDEZVOUS_MAX_WAIT_SECONDS
    while len(list(directory.glob("arrived-*"))) < 2:
        if time.monotonic() > deadline:
            raise TaskPermanentError()
        time.sleep(POLL_SECONDS)
    _touch(directory / f"exec-{payload['label']}-{os.getpid()}")
    return {"ok": 1}


def run_work(payload: dict) -> dict:
    _touch(control_dir() / f"exec-{payload['label']}-{os.getpid()}")
    return {"ok": 1}


def registry() -> dict:
    """The real production allowlist plus the three test handlers (the production mapping is untouched)."""
    extra = {
        kind: handlers.TaskHandler(kind=kind, validate=validate_label_payload, run=run, is_test_handler=True)
        for kind, run in ((BLOCK_KIND, run_block), (RENDEZVOUS_KIND, run_rendezvous), (WORK_KIND, run_work))
    }
    return {**handlers.HANDLERS, **extra}


# --- observing a blocked session ---------------------------------------------------------------------------

# Live lock-manager state, not a statistics snapshot: ``pg_stat_activity`` is cached for the rest of
# the transaction that first reads it, so polling it from inside the transaction that holds the lock
# never shows a session that blocks later. ``pg_locks`` and ``pg_blocking_pids`` read the lock manager
# at call time. This matches one backend (``pid = %s``) that has an ungranted lock request and is blocked
# by THIS connection's backend, so a lock wait of any other session cannot satisfy it.
BLOCKED_BY_THIS_CONNECTION_SQL = (
    "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid = %s AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
)


def wait_until_blocked_by_this_connection(connection, session, timeout, *, clock=time.monotonic, sleep=time.sleep) -> None:
    """Return once ``session``'s backend is blocked by a lock held by ``connection``; raise ``AssertionError`` otherwise.

    ``session`` needs ``backend_pid`` (None until it has connected), ``connected`` (an event set once
    it has connected or failed to) and ``outcome`` (None until its operation has finished). One
    ``timeout`` bounds the whole wait. It fails at once, instead of waiting out the bound, when the
    session never connected or has already finished (a finished session is not blocked).
    """
    deadline = clock() + timeout
    session.connected.wait(timeout=max(0.0, deadline - clock()))
    if session.backend_pid is None:
        raise AssertionError(f"the background session never connected (outcome: {session.outcome!r})")
    while True:
        with connection.cursor() as cursor:
            cursor.execute(BLOCKED_BY_THIS_CONNECTION_SQL, [session.backend_pid])
            if cursor.fetchone()[0] >= 1:
                return
        if session.outcome is not None:
            raise AssertionError(f"the background session finished without waiting for the lock (outcome: {session.outcome!r})")
        if clock() >= deadline:
            raise AssertionError("no session ever waited for the row lock")
        sleep(POLL_SECONDS)


# --- child processes ------------------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent
CHILD_SCRIPT = Path(__file__).resolve().with_name("recovery_worker_child.py")


def process_exists(pid: int) -> bool:
    """Whether a process with this id is still running. It never signals the process (``os.kill`` would terminate it on Windows)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30)
        return
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Child:
    """One started child process, its log file, and its marker-file name."""

    def __init__(self, name, process, log_path, log_file, directory):
        self.name = name
        self.process = process
        self.log_path = Path(log_path)
        self._log_file = log_file
        self._directory = Path(directory)

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def real_pid(self) -> int | None:
        """The worker's own process id as it recorded it (differs from ``pid`` behind a Windows venv launcher)."""
        marker = self._directory / f"pid-{self.name}"
        try:
            return int(marker.read_text().strip())
        except (OSError, ValueError):
            return None

    def output(self, limit: int = 3000) -> str:
        """The tail of the child's combined stdout and stderr (ids, kinds, statuses, safe codes; never payloads)."""
        try:
            self._log_file.flush()
        except (OSError, ValueError):
            pass
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def close_log(self) -> None:
        try:
            self._log_file.close()
        except OSError:
            pass


_ALL = []


@atexit.register
def _close_everything_at_exit():
    for workers in list(_ALL):
        workers.close()


class WorkerProcesses:
    """Starts, observes, and always cleans up child worker processes for one test."""

    def __init__(self, directory, database_name):
        self.directory = Path(directory)
        self.database_name = database_name
        self.children = []
        _ALL.append(self)

    # -- starting --

    def _environment(self):
        env = dict(os.environ)
        env["AP_DB_NAME"] = self.database_name  # the dedicated test database; the child refuses any other name
        env[CONTROL_DIR_ENV] = str(self.directory)
        return env

    def start_command(self, name, argv) -> Child:
        log_path = self.directory / f"child-{name}.log"
        log_file = open(log_path, "wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # own group: CTRL_BREAK reaches only this child
        process = subprocess.Popen(
            argv,
            cwd=str(BACKEND_DIR),
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        child = Child(name, process, log_path, log_file, self.directory)
        self.children.append(child)
        return child

    def start_worker(self, name, *runworker_args, clock_offset_seconds=0, gated=False) -> Child:
        """Start ``manage.py runworker <runworker_args>`` in a child, with the test registry and an optional clock offset.

        ``clock_offset_seconds`` shifts only that child's clock, so a lease can be made to look
        expired without waiting. ``gated`` makes the child wait for ``GO_FILE`` before it starts working.
        """
        argv = [sys.executable, "-u", str(CHILD_SCRIPT), "--name", name, "--max-seconds", str(CHILD_MAX_SECONDS)]
        argv += ["--clock-offset-seconds", str(int(clock_offset_seconds))]
        if gated:
            argv.append("--gate")
        argv += list(runworker_args)
        return self.start_command(name, argv)

    # -- observing --

    def describe(self, children=None) -> str:
        parts = []
        for child in self.children if children is None else children:
            parts.append(f"--- {child.name} (exit code {child.poll()}) ---\n{child.output()}")
        return "\n".join(parts)

    def markers(self, pattern):
        return sorted(path.name for path in self.directory.glob(pattern))

    def touch(self, name) -> None:
        (self.directory / name).touch()

    def wait_for_markers(self, pattern, *, count=1, watching=(), timeout=MARKER_WAIT_SECONDS) -> list:
        """Wait until ``count`` marker files match ``pattern``; fail (with the children's output) on timeout or if a watched child exits."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.markers(pattern)
            if len(found) >= count:
                return found
            exited = [child for child in watching if child.poll() is not None]
            if exited:
                raise AssertionError(f"a child exited while waiting for {pattern!r}:\n{self.describe(exited)}")
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {count} marker(s) {pattern!r}; found {found}:\n{self.describe()}")
            time.sleep(POLL_SECONDS)

    def wait(self, child, timeout=CHILD_WAIT_SECONDS) -> int:
        """The child's exit code. A child that does not exit within the bound is killed and the wait fails."""
        try:
            return child.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            output = child.output()
            self.kill(child)
            raise AssertionError(f"child {child.name!r} did not exit within {timeout}s:\n{output}") from None

    def interrupt(self, child) -> None:
        """Send the child what Ctrl+C is for ``runworker``: CTRL_BREAK_EVENT on Windows (see the module notes), SIGINT elsewhere."""
        if os.name == "nt":
            child.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.process.send_signal(signal.SIGINT)

    # -- ending --

    def kill(self, child) -> None:
        """Forcibly terminate the child and the real worker process behind it (no graceful shutdown), and reap it."""
        real = child.real_pid()
        if child.poll() is None:  # only while the launcher is alive: its process tree is ours, a recycled id is not
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=30)
            else:
                child.process.kill()
        try:
            child.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.process.kill()
            child.process.wait(timeout=30)
        if real is not None and process_exists(real):  # belt and braces, right after the tree kill
            _kill_pid(real)

    def close(self) -> None:
        """Kill every child that is still running and close the log files. Safe to call more than once."""
        for child in self.children:
            if child.poll() is None:
                try:
                    self.kill(child)
                except Exception:  # cleanup must not stop at the first problem
                    pass
            child.close_log()
        if self in _ALL:
            _ALL.remove(self)
