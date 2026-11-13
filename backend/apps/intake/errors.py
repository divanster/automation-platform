"""Errors raised by the request-intake orchestration. Messages never include request text or findings."""


class IntakeError(Exception):
    """Base class for errors raised by ``apps.intake``."""


class ClientRequestConflictError(IntakeError):
    """A client request id was already used by this user for a different request text."""


class RequestSourceMissingError(IntakeError):
    """The run has no stored request source (it was not created through ``submit_request``)."""


class UnsupportedDraftError(IntakeError):
    """A draft holds a value the draft JSON form cannot store exactly (for example a non-Decimal quantity)."""
