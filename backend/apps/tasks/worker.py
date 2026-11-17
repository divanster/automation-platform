"""The worker: process one task, or poll until asked to stop.

One iteration (``process_one``): recover expired leases, claim one task (short transaction,
committed), run the handler with NO database transaction open, then record the outcome under the
lease checks in ``apps.tasks.services``. A worker whose lease expired or was taken over cannot
record anything: its late result is discarded and logged without the payload.

COMPOSITION. The handler allowlist, the after-iteration hook, further maintenance hooks and an optional kind gate
come from ``install_composition``, called by ``runworker`` with a composition built outside this app, so this app
imports no business code.

KIND GATE. Without a gate the worker claims any kind, as before. With one, each iteration asks the gate which
kinds may be claimed, narrows that to the kinds this worker has a handler for, and passes the result to the
claim query; every other task stays queued, untouched. A gate that fails or answers badly allows nothing.

LIMITATIONS OF THIS SLICE. There is no heartbeat: a handler must finish within the lease
(``AP_WORKER_LEASE_SECONDS``, default 60). Handlers are at-least-once; one can run again after a
crash or an expired lease. Logs carry task ids, kinds, attempt numbers, status, and safe failure
codes; never payloads, results, exception messages, or tracebacks.
"""

import logging
import os
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from django.db import DatabaseError, close_old_connections, connection, connections
from django.utils import timezone

from .errors import LeaseLostError
from .handlers import HANDLERS, TaskHandler, get_handler
from .policy import (
    MAX_RESULT_BYTES,
    Failure,
    FailureCode,
    QueuePolicyError,
    TaskState,
    canonical_json,
    classify_exception,
    normalize_allowed_kinds,
)
from .services import claim_next_task, complete_task, fail_task, recover_expired_leases

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_DATABASE_ERRORS = 5
WAIT_SLICE_SECONDS = 1.0

# The identifier of the legacy ``Composition.after_iteration`` hook. ``core`` is a namespace the module contract
# reserves for Core, so no module's hook can be named like it; ``install_composition`` refuses it for any other hook.
LEGACY_HOOK_ID = "core.after_iteration"
_HOOK_ID = re.compile(r"[a-z][a-z0-9_]{0,39}\.[a-z][a-z0-9_]{0,39}")  # exactly <namespace>.<name>, as in the module contract


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
class MaintenanceHook:
    """One named piece of bookkeeping the worker runs after every iteration.

    ``hook_id`` is an opaque, stable name (``<namespace>.<name>``) used for duplicate checks and log lines; it is
    never resolved to code. ``run`` is a callable the composition root imported, called with no arguments.
    """

    hook_id: str
    run: Callable[[], object]


@dataclass(frozen=True)
class Composition:
    """What the worker entry point adds to the queue: a handler allowlist, hooks and an optional kind gate.

    ``registry`` replaces the default allowlist (``apps.tasks.handlers.HANDLERS``) for ``process_one`` calls
    that do not pass their own ``registry``. ``after_iteration`` is called with no arguments after every
    ``process_one`` call, idle or not (the request-intake failure reconciler uses it). ``maintenance_hooks`` are
    further hooks, run after it in the order given, every iteration, under the same failure rules. ``kind_gate``
    is called with no arguments before each claim and returns the task kinds that may be claimed (a set, list or
    tuple of strings); without one every kind is claimable. This app imports no business code: the composition is
    built OUTSIDE it (``config.task_registry``) and installed once by the ``runworker`` command.
    """

    registry: Mapping[str, TaskHandler] | None = None
    after_iteration: Callable[[], object] | None = None
    maintenance_hooks: tuple[MaintenanceHook, ...] = ()
    kind_gate: Callable[[], Iterable[str]] | None = None


_composition = Composition()


def install_composition(composition: Composition) -> None:
    """Install the composition for this process. Call it once, from the worker entry point.

    A registry must be a mapping from each kind to a ``TaskHandler`` of that same kind; nothing is
    resolved from a string and nothing is imported by name. Maintenance hooks must be a tuple of
    ``MaintenanceHook`` with distinct, well-formed ids (``core.after_iteration`` is reserved for
    ``after_iteration``). Installing replaces the previous composition; a refused one changes nothing.
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
    _check_maintenance_hooks(composition.maintenance_hooks)
    if composition.kind_gate is not None and not callable(composition.kind_gate):
        raise TypeError("kind_gate must be callable")
    _composition = composition


def _check_maintenance_hooks(hooks) -> None:
    if not isinstance(hooks, tuple):
        raise TypeError("maintenance_hooks must be a tuple")
    seen = {LEGACY_HOOK_ID}
    for hook in hooks:
        if not isinstance(hook, MaintenanceHook):
            raise TypeError("every maintenance hook must be an apps.tasks.worker.MaintenanceHook")
        if not isinstance(hook.hook_id, str) or not _HOOK_ID.fullmatch(hook.hook_id):
            raise ValueError("a maintenance hook id must be <namespace>.<name> in lower case")
        if not callable(hook.run):
            raise TypeError("a maintenance hook must be callable")
        if hook.hook_id in seen:
            raise ValueError(f"duplicate maintenance hook id {hook.hook_id}")
        seen.add(hook.hook_id)


def _run_hook(hook_id: str, run: Callable[[], object]) -> None:
    try:
        run()
    except DatabaseError:
        raise  # the worker loop closes the connections and retries; the hooks after this one wait for the next iteration
    except Exception as error:  # a failing hook must not stop the worker or the hooks after it; only the class is logged
        if hook_id == LEGACY_HOOK_ID:
            logger.error("after-iteration hook raised %s", type(error).__name__)
        else:
            logger.error("maintenance hook %s raised %s", hook_id, type(error).__name__)


def _run_after_iteration() -> None:
    """The legacy hook first, then the maintenance hooks in the order they were composed."""
    composition = _composition  # one composition for the whole iteration
    if composition.after_iteration is not None:
        _run_hook(LEGACY_HOOK_ID, composition.after_iteration)
    for hook in composition.maintenance_hooks:
        _run_hook(hook.hook_id, hook.run)


def _claimable_kinds(registry) -> frozenset | None:
    """The kinds this iteration may claim: None (any) without a gate, otherwise the gate's answer, fail-closed.

    The gate's kinds are narrowed to those this worker has a handler for, so a task of a kind the worker cannot
    run stays queued instead of being claimed and failed. A gate that raises, answers ``None`` or answers
    anything but a set, list or tuple of strings allows nothing. A database error from the gate is re-raised
    (nothing is claimed) and follows the worker loop's connection recovery. Only the exception class is logged.
    """
    gate = _composition.kind_gate
    if gate is None:
        return None
    try:
        answer = gate()
    except DatabaseError:
        raise
    except Exception as error:
        logger.error("kind gate raised %s; no task is claimed", type(error).__name__)
        return frozenset()
    try:
        if answer is None:
            raise QueuePolicyError("the gate answered None")
        allowed = normalize_allowed_kinds(answer)
    except QueuePolicyError:
        logger.error("kind gate returned an invalid answer; no task is claimed")
        return frozenset()
    return allowed & frozenset(HANDLERS if registry is None else registry)


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
    ``apps.tasks.handlers``. The composition's hooks (``after_iteration``, then ``maintenance_hooks``) run once the outcome is recorded.
    """
    outcome = _process_one(worker_id=worker_id, lease_seconds=lease_seconds, clock=clock, registry=registry)
    _run_after_iteration()
    return outcome


def _process_one(*, worker_id: str, lease_seconds: int, clock, registry) -> ProcessOutcome:
    if registry is None:
        registry = _composition.registry
    recover_expired_leases(now=clock())
    claimed = claim_next_task(worker_id=worker_id, lease_seconds=lease_seconds, now=clock(), allowed_kinds=_claimable_kinds(registry))
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
