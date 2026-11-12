"""Errors raised by the revision and approval policy.

Messages never echo identifiers' content, hashes' content, or business content.
"""

from enum import StrEnum


class PolicyInputError(ValueError):
    """Caller-supplied input is invalid (a programming or integration error, not a decision)."""


class InvalidIdentifierError(PolicyInputError):
    """An identifier is not a ``uuid.UUID`` (or not canonical UUID text, for ``parse_uuid``)."""


class InvalidRevisionError(PolicyInputError):
    """A ``ResultRevision`` is not internally consistent. ``problems`` lists stable codes."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("The revision is not internally consistent: " + ", ".join(str(p) for p in self.problems))


class InvalidApprovalError(PolicyInputError):
    """An ``ApprovalRecord`` is malformed."""


class ApprovalRefusal(StrEnum):
    """Stable reasons an approval cannot be created. Part of the policy contract."""

    BLOCKING_FINDINGS = "blocking_findings"
    REVISION_INCONSISTENT = "revision_inconsistent"
    WARNINGS_NOT_REVIEWED = "warnings_not_reviewed"


class ApprovalRefusedError(PolicyInputError):
    """An approval was refused. ``reason`` is an ``ApprovalRefusal``; ``warnings`` are kept for review."""

    def __init__(self, reason: ApprovalRefusal, warnings=()):
        self.reason = reason
        self.warnings = tuple(warnings)
        super().__init__(f"Approval refused: {reason}.")
