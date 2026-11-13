"""Errors raised by the request-intake orchestration. Messages never include request text or findings."""


class IntakeError(Exception):
    """Base class for errors raised by ``apps.intake``."""


class ClientRequestConflictError(IntakeError):
    """A client request id was already used by this user for a different request text."""


class RequestSourceMissingError(IntakeError):
    """The run has no stored request source (it was not created through ``submit_request``)."""


class UnsupportedDraftError(IntakeError):
    """A draft holds a value the draft JSON form cannot store exactly (for example a non-Decimal quantity)."""


class ReviewRefusedError(IntakeError):
    """A review operation (edit, approve, reject) is refused. Nothing was written.

    ``reason`` is a stable code (see ``apps.intake.review_rules``). The message names the code only: it
    never includes content, hashes, or identifiers.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The review operation is refused ({reason}).")


class StaleRevisionError(ReviewRefusedError):
    """The caller acted on a revision that is not the run's current one (``stale_base_revision``, ``stale_revision``)."""


class SelfApprovalError(ReviewRefusedError):
    """The approver submitted the request or authored a revision of it (``self_approval_forbidden``)."""


class WarningsNotAcknowledgedError(ReviewRefusedError):
    """The acknowledged warnings are not exactly the current revision's warnings.

    ``missing`` and ``unexpected`` are tuples of ``(finding code, field path)`` pairs: codes and paths of
    findings, never business content.
    """

    def __init__(self, reason: str, missing=(), unexpected=()):
        self.missing = tuple(missing)
        self.unexpected = tuple(unexpected)
        super().__init__(reason)


class NoChangeError(ReviewRefusedError):
    """The edited draft has the same content hash as the current revision (``unchanged_content``)."""


class DraftInvalidError(ReviewRefusedError):
    """The edited draft cannot become a revision. Nothing was stored.

    ``reason`` is ``blocking_findings`` (``findings`` holds every finding, as generic revision findings) or
    ``not_serializable`` (a value that validates but cannot be serialized safely; ``path`` is its field
    path when known). A draft with blocking findings is never saved as a revision.
    """

    def __init__(self, reason: str, findings=(), path=None):
        self.findings = tuple(findings)
        self.path = path
        super().__init__(reason)


_BY_REASON = {
    "stale_base_revision": StaleRevisionError,
    "stale_revision": StaleRevisionError,
    "self_approval_forbidden": SelfApprovalError,
    "unchanged_content": NoChangeError,
}


def refusal(reason: str, **details) -> ReviewRefusedError:
    """The error class for ``reason``: a specific subclass when there is one, else ``ReviewRefusedError``."""
    if reason in ("warnings_not_acknowledged", "unknown_warning_acknowledged"):
        return WarningsNotAcknowledgedError(reason, **details)
    return _BY_REASON.get(reason, ReviewRefusedError)(reason)
