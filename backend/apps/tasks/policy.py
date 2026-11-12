"""Pure queue policy: states, transitions, lease rules, retry/backoff, safe failure codes.

Standard library only: no Django, no database. Everything time-dependent takes ``now`` as an
argument, so each rule is testable with a fixed instant. The database operations in
``apps.tasks.services`` mirror these rules in SQL conditions; the PostgreSQL tests check that.

HANDLERS MAY RUN MORE THAN ONCE. A worker can finish the work and die before recording it, or
its lease can expire while it is still running; the task is then run again. This queue provides
at-least-once execution, never exactly-once.
"""

import enum
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# --- states ----------------------------------------------------------------------------------


class TaskState(str, enum.Enum):
    """Task states. These describe execution only; they are not ``WorkflowRun`` states."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptOutcome(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"  # the lease expired and recovery closed the attempt


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})

ALLOWED_TRANSITIONS = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    # running -> queued is a retry or a recovery requeue, always with a future available_at.
    TaskState.RUNNING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.QUEUED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def is_terminal(state) -> bool:
    return TaskState(state) in TERMINAL_STATES


def can_transition(current, target) -> bool:
    return TaskState(target) in ALLOWED_TRANSITIONS[TaskState(current)]


# --- limits and provisional defaults (NOT measured; see docs/development/persistent-worker-slice.md) ---

MAX_PAYLOAD_BYTES = 4096
MAX_RESULT_BYTES = 4096
MAX_JSON_DEPTH = 5
MAX_ATTEMPTS_LIMIT = 10
DEFAULT_MAX_ATTEMPTS = 3
MAX_KIND_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 200
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")

MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 60
MIN_POLL_SECONDS = 1
MAX_POLL_SECONDS = 60
DEFAULT_POLL_SECONDS = 2

BACKOFF_BASE_SECONDS = 5
BACKOFF_FACTOR = 2
BACKOFF_CAP_SECONDS = 300
BACKOFF_JITTER_RATIO = 0.2  # the delay is reduced by up to this fraction, never increased


class QueuePolicyError(ValueError):
    """A request violates a queue rule (bad key, bad attempt limit, oversized or non-JSON payload)."""


# --- failure classification --------------------------------------------------------------------


class FailureCode(str, enum.Enum):
    """The closed set of failure codes that may be stored. Never a raw message."""

    INVALID_PAYLOAD = "invalid_payload"
    UNKNOWN_KIND = "unknown_kind"
    INVALID_RESULT = "invalid_result"
    REJECTED = "rejected"  # generic permanent refusal by a handler
    TEMPORARY_FAILURE = "temporary_failure"  # generic retryable failure reported by a handler
    HANDLER_ERROR = "handler_error"  # an unexpected exception
    LEASE_EXPIRED = "lease_expired"


class TaskRetryableError(Exception):
    """Raised by a handler for a failure worth retrying. Carries a code only, never a message."""

    def __init__(self, code: FailureCode = FailureCode.TEMPORARY_FAILURE):
        self.code = FailureCode(code)
        super().__init__(self.code.value)


class TaskPermanentError(Exception):
    """Raised by a handler for a failure that retrying cannot fix."""

    def __init__(self, code: FailureCode = FailureCode.REJECTED):
        self.code = FailureCode(code)
        super().__init__(self.code.value)


class InvalidPayloadError(QueuePolicyError):
    """A task payload does not satisfy its handler's schema. The message names a field, not its value."""


@dataclass(frozen=True)
class Failure:
    """A classified failure: a safe code and whether another attempt is allowed."""

    code: FailureCode
    retryable: bool


def classify_exception(error: BaseException) -> Failure:
    """Map what a handler raised to a safe ``Failure``.

    Only the exception's type decides. Its message, arguments, and traceback are never read,
    stored, or returned, so they cannot leak payloads or credentials into a result.
    """
    if isinstance(error, TaskRetryableError):
        return Failure(error.code, retryable=True)
    if isinstance(error, TaskPermanentError):
        return Failure(error.code, retryable=False)
    if isinstance(error, InvalidPayloadError):
        return Failure(FailureCode.INVALID_PAYLOAD, retryable=False)
    # Unexpected: retried within the attempt limit, then the task fails.
    return Failure(FailureCode.HANDLER_ERROR, retryable=True)


# --- retry and backoff ---------------------------------------------------------------------------


def backoff_seconds(attempts_made: int, *, rng=random.random) -> float:
    """Delay before the next attempt, after ``attempts_made`` attempts (1 for the first failure).

    ``min(cap, base * factor ** (attempts_made - 1))``, reduced by up to ``BACKOFF_JITTER_RATIO``
    so that retries of tasks that failed together do not run together. ``rng`` returns a float in
    [0, 1) and is injectable for tests.
    """
    if isinstance(attempts_made, bool) or not isinstance(attempts_made, int) or attempts_made < 1:
        raise QueuePolicyError("attempts_made must be an integer of at least 1")
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** (attempts_made - 1))
    return delay * (1 - BACKOFF_JITTER_RATIO * rng())


@dataclass(frozen=True)
class RetryDecision:
    requeue: bool
    available_at: datetime | None  # set only when requeue is True


def decide_retry(*, failure: Failure, attempts_made: int, max_attempts: int, now: datetime, rng=random.random) -> RetryDecision:
    """Requeue (with a future ``available_at``) only a retryable failure with attempts left."""
    if failure.retryable and attempts_made < max_attempts:
        return RetryDecision(True, now + timedelta(seconds=backoff_seconds(attempts_made, rng=rng)))
    return RetryDecision(False, None)


def decide_recovery(*, attempts_made: int, max_attempts: int, now: datetime, rng=random.random) -> RetryDecision:
    """What to do with a task whose lease expired: requeue within the limit, else fail it."""
    return decide_retry(failure=Failure(FailureCode.LEASE_EXPIRED, retryable=True), attempts_made=attempts_made, max_attempts=max_attempts, now=now, rng=rng)


# --- leases ------------------------------------------------------------------------------------------


def lease_expiry(now: datetime, lease_seconds: int) -> datetime:
    return now + timedelta(seconds=validate_lease_seconds(lease_seconds))


def lease_problem(*, state, expected_token, actual_token, lease_expires_at: datetime | None, now: datetime) -> str | None:
    """Why a worker's mutation must be refused, or None when it may proceed.

    The lease is valid strictly while ``now < lease_expires_at``. The SQL conditions in
    ``services`` use the same rule (``lease_expires_at > now``).
    """
    if TaskState(state) is not TaskState.RUNNING:
        return "not_running"
    if actual_token is None or actual_token != expected_token:
        return "token_mismatch"
    if lease_expires_at is None or not now < lease_expires_at:
        return "lease_expired"
    return None


def is_available(*, state, available_at: datetime, now: datetime) -> bool:
    """A task can be claimed when it is queued and its availability time has been reached."""
    return TaskState(state) is TaskState.QUEUED and available_at <= now


# --- cancellation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelDecision:
    allowed: bool  # the queued task may be cancelled now
    already_cancelled: bool
    reason: str  # a stable code: "cancellable", "already_cancelled", "running_unsupported", "terminal"


def check_cancellation(state) -> CancelDecision:
    """Queued: cancel. Cancelled: nothing to do. Running: unsupported in this slice. Other terminal: refused."""
    state = TaskState(state)
    if state is TaskState.QUEUED:
        return CancelDecision(True, False, "cancellable")
    if state is TaskState.CANCELLED:
        return CancelDecision(False, True, "already_cancelled")
    if state is TaskState.RUNNING:
        return CancelDecision(False, False, "running_unsupported")
    return CancelDecision(False, False, "terminal")


# --- validation of requests and results ------------------------------------------------------------


def validate_max_attempts(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ATTEMPTS_LIMIT:
        raise QueuePolicyError(f"max_attempts must be an integer from 1 to {MAX_ATTEMPTS_LIMIT}")
    return value


def validate_lease_seconds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise QueuePolicyError(f"lease seconds must be an integer from {MIN_LEASE_SECONDS} to {MAX_LEASE_SECONDS}")
    return value


def validate_poll_seconds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_POLL_SECONDS <= value <= MAX_POLL_SECONDS:
        raise QueuePolicyError(f"poll seconds must be an integer from {MIN_POLL_SECONDS} to {MAX_POLL_SECONDS}")
    return value


def validate_idempotency_key(key):
    """None, or a string of 1 to 200 characters from ``A-Za-z0-9._:-``."""
    if key is None:
        return None
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise QueuePolicyError(f"idempotency_key must be 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters from A-Z a-z 0-9 . _ : -")
    return key


def validate_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise QueuePolicyError(f"{name} must be a timezone-aware datetime")
    return value


def _check_json(value, depth: int):
    if depth > MAX_JSON_DEPTH:
        raise QueuePolicyError(f"JSON nesting is deeper than {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QueuePolicyError("JSON object keys must be strings")
            _check_json(item, depth + 1)
        return
    # Floats are refused so that the canonical form (and the idempotency fingerprint) is exact.
    raise QueuePolicyError("only null, boolean, integer, string, list, and object values are allowed")


def canonical_json(value, *, max_bytes: int, what: str) -> str:
    """Canonical JSON text (sorted keys, no spaces, ASCII) of a bounded, restricted JSON value."""
    _check_json(value, 1)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(text.encode("ascii")) > max_bytes:
        raise QueuePolicyError(f"{what} is larger than {max_bytes} bytes")
    return text


def request_fingerprint(kind: str, payload) -> str:
    """SHA-256 (hex) of the kind and the canonical payload: the identity of an enqueue request.

    ``max_attempts`` and ``available_at`` are scheduling options and are deliberately not part
    of it. Two payloads that differ only in object key order have the same fingerprint.
    """
    text = canonical_json(payload, max_bytes=MAX_PAYLOAD_BYTES, what="payload")
    return hashlib.sha256(f"{kind}\n{text}".encode("utf-8")).hexdigest()
