"""Errors raised by the queue services. Messages never include payloads, results, or keys."""


class QueueError(Exception):
    """Base class for errors raised by ``apps.tasks`` services."""


class UnknownTaskKindError(QueueError):
    """The task kind is not in the handler allowlist."""


class IdempotencyConflictError(QueueError):
    """The idempotency key was already used for a different kind or payload."""


class TaskNotFoundError(QueueError):
    """The task does not exist."""


class LeaseLostError(QueueError):
    """A worker mutation was refused: the task is not running, the token is not the current one, or the lease expired.

    ``reason`` is one of ``not_found``, ``not_running``, ``token_mismatch``, ``lease_expired``.
    It is advisory (read after the refused write) and is not used to decide anything.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The lease is not held by this worker ({reason}).")


class CancellationNotSupportedError(QueueError):
    """A running task cannot be cancelled in this slice."""


class TaskTerminalError(QueueError):
    """The task already finished (succeeded or failed) and cannot be changed."""


class StoredQueueDataError(QueueError):
    """Stored queue data breaks an invariant (a bypassed guard or corruption)."""
