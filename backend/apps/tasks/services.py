"""Queue operations on top of the pure policy: enqueue, claim, lease, finish, recover, cancel.

TIME. Every operation reads the application clock (``django.utils.timezone.now``) once, or takes
an explicit ``now``, and uses that single instant for every availability and lease comparison in
the operation. The database clock is not used. Tests pass fixed instants instead of sleeping.

TRANSACTIONS. Each operation is one short ``transaction.atomic()`` block. ``claim_next_task``
commits before the handler runs; no operation here is held open while a handler executes.

LOCKING. Claim and recovery use ``SELECT ... FOR UPDATE SKIP LOCKED`` so competing workers skip
rows another worker has locked. The worker mutations (``renew_lease``, ``complete_task``,
``fail_task``) lock the one row with ``FOR UPDATE`` and a WHERE clause on state, lease token, and
lease expiry, so the lease is checked on the locked row version, and the task and its attempt
are changed in the same transaction. All of this is written against PostgreSQL and has not been
run in the development VM; see docs/development/persistent-worker-slice.md.

HANDLERS MAY RUN MORE THAN ONCE (at-least-once). Fencing stops a stale worker from recording a
result; it cannot stop the stale worker's handler from having run.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .errors import (
    CancellationNotSupportedError,
    IdempotencyConflictError,
    LeaseLostError,
    StoredQueueDataError,
    TaskNotFoundError,
    TaskTerminalError,
    UnknownTaskKindError,
)
from .handlers import get_handler
from .models import Task, TaskAttempt
from .policy import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_RESULT_BYTES,
    AttemptOutcome,
    Failure,
    FailureCode,
    QueuePolicyError,
    TaskState,
    canonical_json,
    check_cancellation,
    decide_recovery,
    decide_retry,
    lease_expiry,
    lease_problem,
    request_fingerprint,
    validate_aware,
    validate_idempotency_key,
    validate_max_attempts,
)

MAX_WORKER_ID_LENGTH = 100
RECOVERY_BATCH_LIMIT = 50


def _instant(now: datetime | None) -> datetime:
    return timezone.now() if now is None else validate_aware(now, "now")


def _check_token(token) -> uuid.UUID:
    if not isinstance(token, uuid.UUID):
        raise QueuePolicyError("lease_token must be a UUID")
    return token


# --- enqueue -----------------------------------------------------------------------------------


class EnqueueResult(NamedTuple):
    task: Task
    created: bool  # False when an existing task was returned for a repeated idempotency key


def _same_request_or_conflict(existing: Task, fingerprint: str) -> EnqueueResult:
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyConflictError("The idempotency key was already used for a different task request.")
    return EnqueueResult(existing, False)


def enqueue_task(
    *,
    kind: str,
    payload,
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    available_at: datetime | None = None,
    now: datetime | None = None,
    registry=None,
) -> EnqueueResult:
    """Create a queued task, or return the existing one for a repeated idempotency key.

    ``kind`` must be in the handler allowlist and ``payload`` must satisfy that handler's schema
    and the size bound; both are checked here, before anything is stored. With a key: the same
    kind and payload returns the existing task in whatever state it is in (a finished task is
    never restarted); a different kind or payload raises ``IdempotencyConflictError``. The key is
    unique across the whole installation. Uniqueness is enforced by the database, so two
    concurrent enqueues of one key create one task. ``max_attempts`` and ``available_at`` are not
    part of the identity: a repeat with other values returns the original task unchanged.
    """
    handler = get_handler(kind, registry)
    if handler is None:
        raise UnknownTaskKindError("The task kind is not in the handler allowlist.")
    clean_payload = handler.validate(payload)
    key = validate_idempotency_key(idempotency_key)
    attempts_allowed = validate_max_attempts(max_attempts)
    created_at = _instant(now)
    first_available = created_at if available_at is None else validate_aware(available_at, "available_at")
    fingerprint = request_fingerprint(handler.kind, clean_payload)  # also enforces the payload size bound

    if key is not None:
        existing = Task.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return _same_request_or_conflict(existing, fingerprint)
    try:
        with transaction.atomic():  # a savepoint when the caller already has a transaction
            task = Task.objects.create(
                kind=handler.kind,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                payload=clean_payload,
                state=TaskState.QUEUED.value,
                available_at=first_available,
                max_attempts=attempts_allowed,
                created_at=created_at,
            )
    except IntegrityError:
        if key is None:
            raise
        # Lost a race for the same key: the winner's row is committed by now.
        existing = Task.objects.filter(idempotency_key=key).first()
        if existing is None:
            raise
        return _same_request_or_conflict(existing, fingerprint)
    return EnqueueResult(task, True)


# --- claim -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimedTask:
    """What a worker needs to run one attempt. ``payload`` is business input: never log it."""

    task_id: uuid.UUID
    kind: str
    payload: dict
    attempt_id: uuid.UUID
    attempt_number: int
    max_attempts: int
    lease_token: uuid.UUID
    lease_expires_at: datetime


def claim_next_task(*, worker_id: str, lease_seconds: int, now: datetime | None = None) -> ClaimedTask | None:
    """Claim the oldest eligible task in one short transaction, or return None when there is none.

    Eligible: ``queued``, ``available_at <= now``, attempts remaining. Order: ``available_at``,
    then ``created_at``, then id. The row is locked with ``SKIP LOCKED``, so a concurrent worker
    takes a different task or none. In the same transaction: state -> running, a NEW lease token,
    the lease expiry, ``attempt_count + 1``, and a ``TaskAttempt`` row. Committed before return,
    i.e. before the handler runs.
    """
    if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= MAX_WORKER_ID_LENGTH:
        raise QueuePolicyError(f"worker_id must be 1 to {MAX_WORKER_ID_LENGTH} characters")
    claimed_at = _instant(now)
    expires_at = lease_expiry(claimed_at, lease_seconds)

    with transaction.atomic():
        task = (
            Task.objects.select_for_update(skip_locked=True)
            .filter(state=TaskState.QUEUED.value, available_at__lte=claimed_at, attempt_count__lt=F("max_attempts"))
            .order_by("available_at", "created_at", "id")
            .first()
        )
        if task is None:
            return None
        token = uuid.uuid4()
        task.state = TaskState.RUNNING.value
        task.lease_token = token
        task.lease_expires_at = expires_at
        task.attempt_count += 1
        task.save(update_fields=["state", "lease_token", "lease_expires_at", "attempt_count"])
        attempt = TaskAttempt.objects.create(
            task=task,
            attempt_number=task.attempt_count,
            worker_id=worker_id,
            lease_token=token,
            started_at=claimed_at,
        )
    return ClaimedTask(
        task_id=task.pk,
        kind=task.kind,
        payload=task.payload,
        attempt_id=attempt.pk,
        attempt_number=attempt.attempt_number,
        max_attempts=task.max_attempts,
        lease_token=token,
        lease_expires_at=expires_at,
    )


# --- lease-fenced worker mutations -----------------------------------------------------------------


def _diagnose(task_id, lease_token, now) -> str:
    """Why a lease check failed (advisory; read after the refused write)."""
    row = Task.objects.filter(pk=task_id).values("state", "lease_token", "lease_expires_at").first()
    if row is None:
        return "not_found"
    return (
        lease_problem(
            state=row["state"],
            expected_token=lease_token,
            actual_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            now=now,
        )
        or "token_mismatch"
    )


def _lock_held_task(task_id, lease_token, now) -> Task:
    """Lock the task row only if it is running under this token with an unexpired lease (inside ``atomic``)."""
    task = (
        Task.objects.select_for_update()
        .filter(pk=task_id, state=TaskState.RUNNING.value, lease_token=lease_token, lease_expires_at__gt=now)
        .first()
    )
    if task is None:
        raise LeaseLostError(_diagnose(task_id, lease_token, now))
    return task


def _close_attempt(task: Task, *, outcome: AttemptOutcome, now: datetime, failure_code: str = ""):
    closed = TaskAttempt.objects.filter(task=task, lease_token=task.lease_token, outcome=AttemptOutcome.RUNNING.value).update(
        outcome=outcome.value, ended_at=now, failure_code=failure_code
    )
    if closed != 1:
        raise StoredQueueDataError("a running task does not have exactly one running attempt for its lease")


def renew_lease(*, task_id, lease_token, lease_seconds: int, now: datetime | None = None) -> datetime:
    """Extend the lease from ``now`` and return the new expiry. Refused (``LeaseLostError``) unless
    the task is running under this token and the lease has not expired.

    This slice has no heartbeat that calls it; it is the mechanism a later one will use.
    """
    _check_token(lease_token)
    renewed_at = _instant(now)
    new_expiry = lease_expiry(renewed_at, lease_seconds)
    with transaction.atomic():
        task = _lock_held_task(task_id, lease_token, renewed_at)
        task.lease_expires_at = new_expiry
        task.save(update_fields=["lease_expires_at"])
    return new_expiry


def complete_task(*, task_id, lease_token, result: dict, now: datetime | None = None) -> None:
    """Record success: task -> succeeded with the result, attempt -> succeeded, in one transaction.

    Refused with ``LeaseLostError`` (nothing is written) when the task is not running, the token is
    not the current one, or the lease has expired at ``now``. ``result`` must be a small JSON object.
    """
    _check_token(lease_token)
    if not isinstance(result, dict):
        raise QueuePolicyError("result must be an object")
    canonical_json(result, max_bytes=MAX_RESULT_BYTES, what="result")
    finished_at = _instant(now)
    with transaction.atomic():
        task = _lock_held_task(task_id, lease_token, finished_at)
        _close_attempt(task, outcome=AttemptOutcome.SUCCEEDED, now=finished_at)
        task.state = TaskState.SUCCEEDED.value
        task.result = result
        task.finished_at = finished_at
        task.lease_token = None
        task.lease_expires_at = None
        task.save(update_fields=["state", "result", "finished_at", "lease_token", "lease_expires_at"])


class FailureResult(NamedTuple):
    state: TaskState  # QUEUED when another attempt was scheduled, FAILED otherwise
    available_at: datetime | None  # when the next attempt becomes claimable (QUEUED only)


def fail_task(*, task_id, lease_token, failure: Failure, now: datetime | None = None, rng=random.random) -> FailureResult:
    """Record a failed attempt under the same lease checks as ``complete_task``.

    A retryable failure with attempts left requeues the task with a backoff ``available_at``;
    otherwise (permanent failure, or attempts exhausted) the task becomes ``failed``. Only the safe
    ``failure.code`` is stored. The attempt is closed as ``failed`` in the same transaction.
    """
    _check_token(lease_token)
    if not isinstance(failure, Failure):
        raise TypeError("failure must be an apps.tasks.policy.Failure")
    failed_at = _instant(now)
    with transaction.atomic():
        task = _lock_held_task(task_id, lease_token, failed_at)
        decision = decide_retry(failure=failure, attempts_made=task.attempt_count, max_attempts=task.max_attempts, now=failed_at, rng=rng)
        _close_attempt(task, outcome=AttemptOutcome.FAILED, now=failed_at, failure_code=failure.code.value)
        return _apply_decision(task, decision, failure.code, failed_at)


def _apply_decision(task: Task, decision, code: FailureCode, now: datetime) -> FailureResult:
    task.lease_token = None
    task.lease_expires_at = None
    if decision.requeue:
        task.state = TaskState.QUEUED.value
        task.available_at = decision.available_at
        task.save(update_fields=["state", "available_at", "lease_token", "lease_expires_at"])
        return FailureResult(TaskState.QUEUED, decision.available_at)
    task.state = TaskState.FAILED.value
    task.failure_code = code.value
    task.finished_at = now
    task.save(update_fields=["state", "failure_code", "finished_at", "lease_token", "lease_expires_at"])
    return FailureResult(TaskState.FAILED, None)


# --- recovery ----------------------------------------------------------------------------------------


class RecoveryResult(NamedTuple):
    requeued: int
    failed: int


def recover_expired_leases(*, now: datetime | None = None, limit: int = RECOVERY_BATCH_LIMIT, rng=random.random) -> RecoveryResult:
    """Close the attempts of running tasks whose lease expired (``lease_expires_at <= now``).

    Each such attempt is closed as ``abandoned`` (code ``lease_expired``). The task is requeued with
    a backoff ``available_at`` while attempts remain, otherwise it becomes ``failed``. The old
    lease token stops working immediately: the row no longer holds it. At most ``limit`` tasks are
    handled per call; rows locked by another worker are skipped. Safe to run from several workers.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise QueuePolicyError("limit must be a positive integer")
    recovered_at = _instant(now)
    requeued = failed = 0
    with transaction.atomic():
        expired = list(
            Task.objects.select_for_update(skip_locked=True)
            .filter(state=TaskState.RUNNING.value, lease_expires_at__lte=recovered_at)
            .order_by("lease_expires_at", "id")[:limit]
        )
        for task in expired:
            decision = decide_recovery(attempts_made=task.attempt_count, max_attempts=task.max_attempts, now=recovered_at, rng=rng)
            _close_attempt(task, outcome=AttemptOutcome.ABANDONED, now=recovered_at, failure_code=FailureCode.LEASE_EXPIRED.value)
            outcome = _apply_decision(task, decision, FailureCode.LEASE_EXPIRED, recovered_at)
            if outcome.state is TaskState.QUEUED:
                requeued += 1
            else:
                failed += 1
    return RecoveryResult(requeued, failed)


# --- cancellation --------------------------------------------------------------------------------------


def cancel_task(task_id, *, now: datetime | None = None) -> TaskState:
    """Cancel a queued task. Returns the resulting state (``CANCELLED``).

    Running tasks are refused with ``CancellationNotSupportedError``: cancelling cannot undo work
    already started or any external effect, and cooperative cancellation is not built yet.
    Succeeded or failed tasks are refused with ``TaskTerminalError``; cancelling a cancelled task
    changes nothing. A task is never restarted.
    """
    cancelled_at = _instant(now)
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(pk=task_id).first()
        if task is None:
            raise TaskNotFoundError("The task does not exist.")
        decision = check_cancellation(task.state)
        if decision.allowed:
            task.state = TaskState.CANCELLED.value
            task.finished_at = cancelled_at
            task.save(update_fields=["state", "finished_at"])
            return TaskState.CANCELLED
        if decision.already_cancelled:
            return TaskState.CANCELLED
        if decision.reason == "running_unsupported":
            raise CancellationNotSupportedError("Cancelling a running task is not supported yet.")
        raise TaskTerminalError("The task has already finished and cannot be changed.")
