"""Revision identity, approval, and export-eligibility policy: pure Python, DB-independent.

Generic plain-Python rules. Identifiers are ``uuid.UUID`` values supplied by the caller, and
every revision and approval is bound to a case, a workflow run, a revision, and a content hash.
This package's generic modules import nothing from any workflow package. The request-intake
integration lives in ``workflows.revision_approval.request_intake_revision`` (the only module
that imports ``workflows.request_intake``) and is intentionally NOT imported here. There is no
persistence, endpoint, worker, file export, authentication, or state machine. See
docs/development/revision-approval-policy.md for what exists, what the caller must do, and what
remains deferred.
"""

from .approval import ApprovalRecord, approve_revision
from .errors import (
    ApprovalRefusal,
    ApprovalRefusedError,
    InvalidApprovalError,
    InvalidIdentifierError,
    InvalidRevisionError,
    PolicyInputError,
)
from .export_eligibility import ExportDecision, ExportDenialReason, check_export_eligibility
from .findings import RevisionFinding
from .identifiers import is_valid_uuid, parse_uuid, validate_revision_number, validate_uuid
from .revision import ResultRevision, RevisionProblem

__all__ = [
    "ApprovalRecord",
    "ApprovalRefusal",
    "ApprovalRefusedError",
    "ExportDecision",
    "ExportDenialReason",
    "InvalidApprovalError",
    "InvalidIdentifierError",
    "InvalidRevisionError",
    "PolicyInputError",
    "ResultRevision",
    "RevisionFinding",
    "RevisionProblem",
    "approve_revision",
    "check_export_eligibility",
    "is_valid_uuid",
    "parse_uuid",
    "validate_revision_number",
    "validate_uuid",
]
