"""Run one extraction in a separate, short-lived, resource-limited process and bring back a validated result. Standard library only.

WHY A PROCESS. A PDF parser reads hostile input. In the worker's own process a hang, a runaway allocation or a parser bug would hold the worker, its database connection and its lease. In a child process the parent
can stop it (wall-clock timeout, then ``kill``), the child limits its own memory and CPU time first (``child_limits``) and nothing it does can change the parent's state.

WHAT THE CHILD GETS. The PDF bytes on standard input and six integers on the command line; an environment holding only what the interpreter needs (on Windows ``SYSTEMROOT``) plus a fixed ``PYTHONHASHSEED``, so
no database setting, secret or ``AP_*`` variable reaches it; no inherited handle; a neutral working directory; no window. The interpreter is the one that runs the worker (``sys.executable``): never a path
from a setting or from the request. The child's standard error is discarded.

WHAT COMES BACK. At most ``limits.max_output_bytes`` from standard output, parsed and validated strictly by ``rules.parse_child_output``. A deterministic problem with the DOCUMENT (a timeout, a resource limit,
a malformed answer, a crash) is a ``failed`` ``ExtractionOutput`` with a code; a problem that is not the document's (the process cannot start, or the limits cannot be put in force) is
``ExtractorUnavailableError``, which the worker retries.

LIMITS OF THIS ISOLATION. It bounds time and memory and keeps secrets out of the child's environment. It is not a sandbox: the child runs as the same operating-system account and could read what that account can
read if the library were exploited. On Windows the wall-clock kill ends the child; the launcher that ``venv`` interpreters use is waited for as well.
"""

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .extractor_identity import installed_version
from .rules import (
    ERR_EXTRACTOR_FAILED,
    ERR_RESOURCE_LIMIT,
    ERR_TIMEOUT,
    FATAL_RESOURCE_LIMIT,
    ChildFatal,
    ChildOutputError,
    ExtractionLimits,
    ExtractionOutput,
    failed_output,
    parse_child_output,
)

CHILD_SCRIPT = str(Path(__file__).resolve().with_name("child.py"))
INTERPRETER_FLAGS = ("-s", "-P", "-B")  # no user site, no script directory on the path, no bytecode files
_CHUNK = 64 * 1024
_JOIN_SECONDS = 5


class ExtractorUnavailableError(Exception):
    """The isolated process could not be run (it did not start, or its limits could not be put in force). Not the document's fault: retryable."""


def child_environment() -> dict:
    environment = {"PYTHONHASHSEED": "0"}  # fixed, so set ordering inside the library cannot vary between runs
    if os.name == "nt":
        environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return environment


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _feed(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except OSError:  # the child ended before reading everything (a broken pipe)
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


class _Collector:
    def __init__(self, pipe, cap: int, on_overflow):
        self.data = bytearray()
        self.overflow = False
        self._pipe, self._cap, self._on_overflow = pipe, cap, on_overflow

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read1(_CHUNK)
                if not chunk:
                    return
                if len(self.data) + len(chunk) > self._cap:
                    self.overflow = True
                    self._on_overflow()
                    return
                self.data += chunk
        except (OSError, ValueError):
            return


class IsolatedExtractor:
    """Callable ``(data, limits) -> ExtractionOutput``. ``command`` is the child's command line without its six numeric arguments (tests pass a different script; production never does)."""

    def __init__(self, command=None):
        self._command = tuple(command) if command is not None else (sys.executable, *INTERPRETER_FLAGS, CHILD_SCRIPT)

    def __call__(self, data: bytes, limits: ExtractionLimits) -> ExtractionOutput:
        if not isinstance(data, (bytes, bytearray)) or not isinstance(limits, ExtractionLimits):
            raise TypeError("data must be bytes and limits an ExtractionLimits")
        arguments = [str(value) for value in (limits.max_pages, limits.max_page_chars, limits.max_total_chars, limits.max_bytes, limits.memory_bytes, limits.timeout_seconds)]
        try:
            process = subprocess.Popen(
                [*self._command, *arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_environment(),
                cwd=tempfile.gettempdir(),
                close_fds=True,
                shell=False,
                creationflags=_creation_flags(),
            )
        except (OSError, ValueError):
            raise ExtractorUnavailableError from None
        version = installed_version()
        collector = _Collector(process.stdout, limits.max_output_bytes, process.kill)
        feeder = threading.Thread(target=_feed, args=(process.stdin, data), daemon=True)
        reader = threading.Thread(target=collector.run, daemon=True)
        feeder.start()
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for thread in (feeder, reader):
                thread.join(_JOIN_SECONDS)
            for pipe in (process.stdout,):
                try:
                    pipe.close()
                except OSError:
                    pass
        if timed_out:
            return failed_output(ERR_TIMEOUT, extractor_version=version)
        if collector.overflow:
            return failed_output(ERR_EXTRACTOR_FAILED, extractor_version=version)
        try:
            return parse_child_output(bytes(collector.data), limits)
        except ChildFatal as fatal:
            if fatal.code == FATAL_RESOURCE_LIMIT:
                return failed_output(ERR_RESOURCE_LIMIT, extractor_version=version)
            raise ExtractorUnavailableError from None
        except ChildOutputError:
            return failed_output(ERR_EXTRACTOR_FAILED, extractor_version=version)
