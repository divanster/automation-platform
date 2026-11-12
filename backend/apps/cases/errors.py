"""Errors raised by the persistence layer for revisions and approvals.

Messages name the problem and the field, never identifiers' content, hashes, or business
content. Policy errors (``workflows.revision_approval.errors``) are not wrapped: they pass
through unchanged.
"""


class PersistenceError(Exception):
    """Base class for errors raised by ``apps.cases``."""


class RecordNotFoundError(PersistenceError):
    """A referenced case, run, revision, or approval does not exist."""


class BindingMismatchError(PersistenceError):
    """A record does not belong to the case, run, revision, or content hash it claims."""


class RevisionNumberError(PersistenceError):
    """The supplied revision number is not the run's next number (1, 2, 3, ...).

    ``expected`` is the number that would have been accepted.
    """

    def __init__(self, expected: int):
        self.expected = expected
        super().__init__(f"The revision number is not the run's next revision number (expected {expected}).")


class NotCurrentRevisionError(PersistenceError):
    """An approval was recorded for a revision that is not the run's current revision."""


class ApprovalMismatchError(PersistenceError):
    """The supplied approval is not what the policy produces for the stored revision."""


class ImmutableRecordError(PersistenceError):
    """A write was attempted that would modify or delete an immutable record."""


class StoredDataError(PersistenceError):
    """Stored data does not satisfy the domain contract (corruption or a bypassed guard).

    ``problems`` holds stable codes when the domain reports them, else a short reason.
    """

    def __init__(self, reason: str, problems=()):
        self.problems = tuple(str(p) for p in problems)
        detail = ": " + ", ".join(self.problems) if self.problems else ""
        super().__init__(f"Stored data is inconsistent ({reason}){detail}")
