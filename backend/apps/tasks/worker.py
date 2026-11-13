"""The worker: process one task, or poll until asked to stop.

One iteration (``process_one``): recover expired leases, claim one task (short transaction,
committed), run the handler with NO database transaction open, then record the outcome under the
lease checks in ``apps.tasks.services``. A worker whose lease expired or was taken over cannot
record anything: its late result is discarded and logged without the payload.

COMPOSITION. The handler allowlist and the after-iteration hook come from ``install_composition``, called by
``runworker`` with a composition built outside this app, so this app imports no business code.

LIMITATIONS OF THIS SLICE. There is no heartbeat: a handler must finish within the lease
(``AP_WORKER_LEASE_SECONDS``, default 60). Handlers are at-least-once; one can run again after a
crash or an expired lease. Logs carry task ids, kinds, attempt numbers, status, and safe failure
codes; never payloads, results, exception messages, or tracebacks.
"""

import logging
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from django.db import DatabaseError, close_old_connections, connection, connections
from django.utils import timezone

from .errors import LeaseLostError
from .handlers import TaskHandler, get_handler
from .policy import (
    MAX_RESULT_BYTES,
    Failure,
    FailureCode,
    QueuePolicyError,
    TaskState,
    canonical_json,
    classify_exception,
)
from .services import claim_next_task, complete_task, fail_task, recover_expired_leases

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_DATABASE_ERRORS = 5
WAIT_SLICE_SECONDS = 1.0


class ProcessStatus(str, Enum):
    IDLE = "idle"  # no task was available
    SUCCEEDED = "succeeded"
    REQUEUED = "requeued"  # the attempt failed and another one is scheduled
    FAILED = "failed"  # the task failed for good
    LEASE_LOST = "lease_lost"  # the outcome was discarded because this worker no longer held the lease


@dataclass(frozen=True)
class ProcessOutcome:
    status: ProcessStatus
    task_id: uuid.UUID | None = None
    kind: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class Composition:
    """What the worker entry point adds to the queue: a handler allowlist and an optional hook.

    ``registry`` replaces the default allowlist (``apps.tasks.handlers.HANDLERS``) for ``process_one`` calls
    that do not pass their own ``registry``. ``after_iteration`` is called with no arguments after every
    ``process_one`` call, idle or not (the request-intake failure reconciler uses it). This app imports no
    business code: the composition is built OUTSIDE it (``config.task_registry``) and installed once by the
    ``runworker`` command.
    """

    registry: Mapping[str, TaskHandler] | None = None
    after_iteration: Callable[[], object] | None = None


_composition = Composition()


def install_composition(composition: Composition) -> None:
    """Install the composition for this process. Call it once, from the worker entry point.

    A registry must be a mapping from each kind to a ``TaskHandler`` of that same kind; nothing is
    resolved from a string and nothing is imported by name. Installing replaces the previous composition.
    """
    global _composition
    if not isinstance(composition, Composition):
        raise TypeError("composition must be an apps.tasks.worker.Composition")
    registry = composition.registry
    if registry is not None:
        if not isinstance(registry, Mapping) or not registry:
            raise TypeError("the registry must be a non-empty mapping")
        for kind, handler in registry.items():
            if not isinstance(handler, TaskHandler) or handler.kind != kind:
                raise TypeError("every registry entry must be a TaskHandler registered under its own kind")
    if composition.after_iteration is not None and not callable(composition.after_iteration):
        raise TypeError("after_iteration must be callable")
    _composition = composition


def _run_after_iteration() -> None:
    hook = _composition.after_iteration
    if hook is None:
        return
    try:
        hook()
    except DatabaseError:
        raise  # the worker loop closes the connections and retries
    except Exception as error:  # a failing hook must not stop the worker; only the class is logged
        logger.error("after-iteration hook raised %s", type(error).__name__)


def make_worker_id() -> str:
    """A short identifier for attempt records: process id plus a random suffix. No host or user name."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _refuse_open_transaction():
    if connection.in_atomic_block:
        raise RuntimeError("A handler must not run inside a database transaction.")


def _run_handler(claimed, registry):
    """Run the handler for a claimed task. Returns ``(result, failure)``; exactly one is not None."""
    handler = get_handler(claimed.kind, registry)
    if handler is None:
        return None, Failure(FailureCode.UNKNOWN_KIND, retryable=False)
    _refuse_open_transaction()
    try:
        result = handler.run(handler.validate(claimed.payload))  # re-validated: stored data is not trusted
    except Exception as error:  # classified by type only; the message is neither stored nor logged
        failure = classify_exception(error)
        logger.warning("task %s kind=%s attempt=%s: handler raised %s -> %s", claimed.task_id, claimed.kind, claimed.attempt_number, type(error).__name__, failure.code.value)
        return None, failure
    try:
        if not isinstance(result, dict):
            raise QueuePolicyError("result must be an object")
        canonical_json(result, max_bytes=MAX_RESULT_BYTES, what="result")
    except QueuePolicyError:
        return None, Failure(FailureCode.INVALID_RESULT, retryable=False)
    return result, None


def process_one(*, worker_id: str, lease_seconds: int, clock=timezone.now, registry=None) -> ProcessOutcome:
    """Recover expired leases, claim at most one task, run it, and record the outcome.

    ``clock`` is read afresh for every step, so the lease is judged at the moment the outcome is
    recorded, not at the moment of the claim. ``registry`` exists for tests; without it the installed
    composition's allowlist is used, and without a composition the fixed allowlist in
    ``apps.tasks.handlers``. The composition's after-iteration hook runs once the outcome is recorded.
    """
    outcome = _process_one(worker_id=worker_id, lease_seconds=lease_seconds, clock=clock, registry=registry)
    _run_after_iteration()
    return outcome


def _process_one(*, worker_id: str, lease_seconds: int, clock, registry) -> ProcessOutcome:
    if registry is None:
        registry = _composition.registry
    recover_expired_leases(now=clock())
    claimed = claim_next_task(worker_id=worker_id, lease_seconds=lease_seconds, now=clock())
    if claimed is None:
        return ProcessOutcome(ProcessStatus.IDLE)

    result, failure = _run_handler(claimed, registry)  # no transaction is open here
    try:
        if failure is None:
            complete_task(task_id=claimed.task_id, lease_token=claimed.lease_token, result=result, now=clock())
            status, code = ProcessStatus.SUCCEEDED, None
        else:
            outcome = fail_task(task_id=claimed.task_id, lease_token=claimed.lease_token, failure=failure, now=clock())
            status = ProcessStatus.REQUEUED if outcome.state is TaskState.QUEUED else ProcessStatus.FAILED
            code = failure.code.value
    except LeaseLostError as error:
        logger.warning("task %s kind=%s attempt=%s: lease lost (%s); outcome discarded", claimed.task_id, claimed.kind, claimed.attempt_number, error.reason)
        return ProcessOutcome(ProcessStatus.LEASE_LOST, claimed.task_id, claimed.kind)
    logger.info("task %s kind=%s attempt=%s: %s", claimed.task_id, claimed.kind, claimed.attempt_number, status.value)
    return ProcessOutcome(status, claimed.task_id, claimed.kind, code)


def wait_for(stop: threading.Event, seconds: float) -> None:
    """Sleep up to ``seconds`` in slices of at most one second, returning early once ``stop`` is set."""
    remaining = float(seconds)
    while remaining > 0 and not stop.is_set():
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        stop.wait(slice_seconds)
        remaining -= slice_seconds


def run_until_stopped(*, worker_id: str, lease_seconds: int, poll_seconds: int, stop: threading.Event, process=process_one, wait=wait_for) -> int:
    """Poll until ``stop`` is set; return the number of tasks processed.

    After a task the loop checks again at once (more may be waiting); when idle it waits
    ``poll_seconds``, so there is no busy loop. A database error closes the connections and is
    retried after the poll interval, up to ``MAX_CONSECUTIVE_DATABASE_ERRORS`` times in a row, then
    it is raised. The error text is not logged (it can contain connection details); its class is.
    """
    processed = 0
    consecutive_errors = 0
    while not stop.is_set():
        try:
            close_old_connections()
            outcome = process(worker_id=worker_id, lease_seconds=lease_seconds)
        except DatabaseError as error:
            consecutive_errors += 1
            logger.error("database error in worker loop (%s), %s of %s", type(error).__name__, consecutive_errors, MAX_CONSECUTIVE_DATABASE_ERRORS)
            connections.close_all()
            if consecutive_errors >= MAX_CONSECUTIVE_DATABASE_ERRORS:
                raise
            wait(stop, poll_seconds)
            continue
        consecutive_errors = 0
        if outcome.status is ProcessStatus.IDLE:
            wait(stop, poll_seconds)
        else:
            processed += 1
    return processed
