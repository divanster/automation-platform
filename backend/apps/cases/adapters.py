"""Conversions between stored rows and the pure revision/approval domain objects.

No database access happens here beyond reading attributes already loaded on the rows (callers
load them with ``select_related``). Hashing and consistency are NOT reimplemented: building a
``workflows.revision_approval.ResultRevision`` re-runs the domain's own checks (SHA-256 of the
bytes, format version, findings), so a corrupted row is reported as ``StoredDataError`` instead
of being trusted.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from workflows.revision_approval import (
    ApprovalRecord,
    InvalidApprovalError,
    InvalidRevisionError,
    PolicyInputError,
    ResultRevision as DomainRevision,
    RevisionFinding,
)

from .errors import StoredDataError
from .models import Approval, ResultRevision

_FINDING_KEYS = {"code", "field_path", "blocking", "message", "excerpt"}


@dataclass(frozen=True)
class StoredApproval:
    """A persisted approval: its row id and creation time next to the domain record."""

    approval_id: UUID
    record: ApprovalRecord
    created_at: datetime | None


def findings_to_json(findings) -> list:
    """Plain JSON-able form of generic findings (stored outside the hashed bytes)."""
    return [
        {"code": f.code, "field_path": f.field_path, "blocking": f.blocking, "message": f.message, "excerpt": f.excerpt}
        for f in findings
    ]


def findings_from_json(data) -> tuple[RevisionFinding, ...]:
    """Strictly rebuild findings from stored JSON; anything unexpected is ``StoredDataError``."""
    if not isinstance(data, list):
        raise StoredDataError("findings are not a list")
    findings = []
    for item in data:
        if not isinstance(item, dict) or set(item) != _FINDING_KEYS:
            raise StoredDataError("a stored finding has unexpected keys")
        try:
            findings.append(RevisionFinding(**item))
        except PolicyInputError as error:
            raise StoredDataError("a stored finding is invalid") from error
    return tuple(findings)


def to_domain_revision(row: ResultRevision) -> DomainRevision:
    """The domain revision for ``row``. ``row.run`` must be loaded (it supplies the case id)."""
    try:
        return DomainRevision(
            case_id=row.run.case_id,
            run_id=row.run_id,
            revision_id=row.pk,
            revision_number=row.revision_number,
            canonical_format_version=row.canonical_format_version,
            canonical_bytes=bytes(row.canonical_bytes),
            content_hash=row.content_hash,
            findings=findings_from_json(row.findings),
        )
    except InvalidRevisionError as error:
        raise StoredDataError("the stored revision fails the domain consistency checks", error.problems) from error


def to_domain_approval(row: Approval) -> ApprovalRecord:
    """The domain record for ``row``. ``row.revision.run`` must be loaded (case and run ids)."""
    run = row.revision.run
    warnings = row.acknowledged_warnings
    if not isinstance(warnings, list):
        raise StoredDataError("acknowledged warnings are not a list")
    try:
        return ApprovalRecord(
            case_id=run.case_id,
            run_id=run.pk,
            revision_id=row.revision_id,
            content_hash=row.content_hash,
            approver_id=row.approver_id,
            acknowledged_warnings=warnings,
        )
    except (InvalidApprovalError, PolicyInputError) as error:
        raise StoredDataError("the stored approval is malformed") from error


def to_stored_approval(row: Approval) -> StoredApproval:
    return StoredApproval(approval_id=row.pk, record=to_domain_approval(row), created_at=row.created_at)
