"""An immutable approval record bound to one specific revision, run, and content hash.

``approve_revision`` is PURE POLICY. It does not authenticate anyone, check permissions, store
anything, or prove that a human acted. The caller supplies ``approver_id`` and
``warnings_reviewed``; a future authenticated application service must establish who the
approver really is, that they were authorized, and that they actually reviewed any warnings.
``warnings_reviewed=True`` is only a declaration by the caller, and must not be read as proof
of a real human action.

An approval authorizes exactly one immutable revision: it records the case, the workflow run,
the revision id, AND that revision's content hash. Creating a later revision never changes an
existing approval; the old approval stays as history and simply does not match the new
revision. Equal content in a new revision (a different revision id) still needs a new
approval, and an approval from another run never authorizes this run's revision.

Whether the revision being approved is the CURRENT one, and whether the run is in a state that
permits approval, are decided by the caller's workflow; this module does not know them.

This module is generic: it imports no workflow-specific types.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from .errors import ApprovalRefusal, ApprovalRefusedError, InvalidApprovalError
from .findings import RevisionFinding
from .identifiers import validate_uuid
from .revision import ResultRevision

_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ApprovalRecord:
    """Case, run, revision, the content hash approved, the approver, and warnings acknowledged.

    All identifiers are ``uuid.UUID``. ``acknowledged_warnings`` is a tuple of
    ``(finding code, field path)`` pairs naming exactly the warnings the caller declared
    reviewed (empty when the revision had none). It carries no business content.
    """

    case_id: UUID
    run_id: UUID
    revision_id: UUID
    content_hash: str
    approver_id: UUID
    acknowledged_warnings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        validate_uuid(self.case_id, "case_id")
        validate_uuid(self.run_id, "run_id")
        validate_uuid(self.revision_id, "revision_id")
        validate_uuid(self.approver_id, "approver_id")
        if not (isinstance(self.content_hash, str) and _HASH.fullmatch(self.content_hash)):
            raise InvalidApprovalError("content_hash must be 64 lowercase hex characters")
        warnings = self.acknowledged_warnings
        if isinstance(warnings, list):
            warnings = tuple(tuple(pair) if isinstance(pair, list) else pair for pair in warnings)
            object.__setattr__(self, "acknowledged_warnings", warnings)
        if not isinstance(warnings, tuple) or not all(
            isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in warnings
        ):
            raise InvalidApprovalError("acknowledged_warnings must be a tuple of (code, path) string pairs")


def approve_revision(revision: ResultRevision, *, approver_id: UUID, warnings_reviewed: bool = False) -> ApprovalRecord:
    """Create an approval bound to exactly ``revision`` (case, run, revision id, content hash).

    Refused with ``ApprovalRefusedError`` (stable ``reason``):

    * ``blocking_findings``: the revision carries any blocking finding.
    * ``revision_inconsistent``: the revision is not internally consistent (re-checked now).
    * ``warnings_not_reviewed``: the revision has warnings and ``warnings_reviewed`` is not
      exactly ``True``. The warnings stay available on the error and on the revision.

    Warnings never block an approval and are never removed; they are recorded by code and path.
    """
    if not isinstance(revision, ResultRevision):
        raise TypeError("revision must be a ResultRevision")
    validate_uuid(approver_id, "approver_id")

    findings = revision.findings
    if isinstance(findings, tuple) and any(isinstance(f, RevisionFinding) and f.blocking for f in findings):
        raise ApprovalRefusedError(ApprovalRefusal.BLOCKING_FINDINGS)
    if revision.consistency_problems():
        raise ApprovalRefusedError(ApprovalRefusal.REVISION_INCONSISTENT)

    warnings = revision.warnings
    if warnings and warnings_reviewed is not True:
        raise ApprovalRefusedError(ApprovalRefusal.WARNINGS_NOT_REVIEWED, warnings)

    return ApprovalRecord(
        case_id=revision.case_id,
        run_id=revision.run_id,
        revision_id=revision.revision_id,
        content_hash=revision.content_hash,
        approver_id=approver_id,
        acknowledged_warnings=tuple((finding.code, finding.field_path) for finding in warnings),
    )
