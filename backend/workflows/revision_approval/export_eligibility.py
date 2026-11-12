"""Pure export-eligibility policy: may THIS revision be exported, given THIS approval?

``check_export_eligibility`` returns a structured decision with stable reason codes. It writes
no file, produces no export artifact, and has no side effects. It works only on the snapshot of
inputs the caller passes in. It is generic: it imports no workflow-specific types.

THE CONCURRENCY BOUNDARY. This check, by itself, cannot prevent an edit from racing with an
export: another edit may create a newer revision after the caller read the "current revision"
and before the export is carried out. The future persistence and application layer must
re-check the current revision and the approval ATOMICALLY at execution and commit time (for
example inside one database transaction with appropriate locking), and discard the result if
they no longer match. Nothing here provides transactional behavior, and nothing here promises
exactly-once export.

SCOPE. A revision and an approval are each bound to a case, a workflow run, a revision id, and a
content hash. The caller states which case and which run the export is for; a revision or an
approval from any other run never authorizes this export, even if the case, the payload, and the
content hash are all equal. An approval for revision A never authorizes revision B, even if their
content hashes are equal: a new revision needs a new approval, and the old approval is untouched.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from .approval import ApprovalRecord
from .identifiers import validate_uuid
from .revision import ResultRevision


class ExportDenialReason(StrEnum):
    """Stable reason codes. Part of the policy contract; reported in this order."""

    CASE_MISMATCH = "case_mismatch"  # the requested or current revision is not in the expected case
    RUN_MISMATCH = "run_mismatch"  # the requested or current revision is not in the expected run
    REVISION_INCONSISTENT = "revision_inconsistent"  # content, hash, version, or findings do not agree
    NOT_CURRENT_REVISION = "not_current_revision"  # the requested revision is not the caller's current one
    NO_APPROVAL = "no_approval"  # no approval was supplied
    APPROVAL_CASE_MISMATCH = "approval_case_mismatch"  # the approval belongs to another case
    APPROVAL_RUN_MISMATCH = "approval_run_mismatch"  # the approval belongs to another workflow run
    APPROVAL_REVISION_MISMATCH = "approval_revision_mismatch"  # the approval is for another revision id
    APPROVAL_HASH_MISMATCH = "approval_hash_mismatch"  # the approval's hash is not this revision's hash
    WARNINGS_NOT_ACKNOWLEDGED = "warnings_not_acknowledged"  # the revision has warnings the approval did not record


@dataclass(frozen=True)
class ExportDecision:
    """Allowed or denied, with every applicable reason. ``allowed`` is true only if there is none."""

    allowed: bool
    reasons: tuple[ExportDenialReason, ...]
    case_id: UUID
    run_id: UUID
    revision_id: UUID

    def __post_init__(self):
        if self.allowed != (len(self.reasons) == 0):
            raise ValueError("a decision is allowed exactly when it has no denial reasons")

    @property
    def denied(self) -> bool:
        return not self.allowed


def _same_revision(a: ResultRevision, b: ResultRevision) -> bool:
    return (
        a.case_id,
        a.run_id,
        a.revision_id,
        a.revision_number,
        a.canonical_format_version,
        a.canonical_bytes,
        a.content_hash,
    ) == (
        b.case_id,
        b.run_id,
        b.revision_id,
        b.revision_number,
        b.canonical_format_version,
        b.canonical_bytes,
        b.content_hash,
    )


def check_export_eligibility(
    *,
    expected_case_id: UUID,
    expected_run_id: UUID,
    requested_revision: ResultRevision,
    current_revision: ResultRevision,
    approval: ApprovalRecord | None,
) -> ExportDecision:
    """Decide whether ``requested_revision`` may be exported for the expected case and run.

    ``current_revision`` is the revision the CALLER believes is current for the expected run;
    this function cannot know whether it still is (see the concurrency boundary above). All
    applicable reasons are returned, in the fixed order of ``ExportDenialReason``. A missing
    approval (``None``) is a denial, not an error; wrong types and non-UUID identifiers raise.
    """
    validate_uuid(expected_case_id, "expected_case_id")
    validate_uuid(expected_run_id, "expected_run_id")
    if not isinstance(requested_revision, ResultRevision):
        raise TypeError("requested_revision must be a ResultRevision")
    if not isinstance(current_revision, ResultRevision):
        raise TypeError("current_revision must be a ResultRevision")
    if approval is not None and not isinstance(approval, ApprovalRecord):
        raise TypeError("approval must be an ApprovalRecord or None")

    reasons = []
    if requested_revision.case_id != expected_case_id or current_revision.case_id != expected_case_id:
        reasons.append(ExportDenialReason.CASE_MISMATCH)
    if requested_revision.run_id != expected_run_id or current_revision.run_id != expected_run_id:
        reasons.append(ExportDenialReason.RUN_MISMATCH)
    if requested_revision.consistency_problems():
        reasons.append(ExportDenialReason.REVISION_INCONSISTENT)
    if not _same_revision(requested_revision, current_revision):
        reasons.append(ExportDenialReason.NOT_CURRENT_REVISION)

    if approval is None:
        reasons.append(ExportDenialReason.NO_APPROVAL)
    else:
        # The requested revision is already checked against the expected case and run above, so
        # comparing the approval with the requested revision also pins it to that scope.
        if approval.case_id != requested_revision.case_id:
            reasons.append(ExportDenialReason.APPROVAL_CASE_MISMATCH)
        if approval.run_id != requested_revision.run_id:
            reasons.append(ExportDenialReason.APPROVAL_RUN_MISMATCH)
        if approval.revision_id != requested_revision.revision_id:
            reasons.append(ExportDenialReason.APPROVAL_REVISION_MISMATCH)
        if approval.content_hash != requested_revision.content_hash:
            reasons.append(ExportDenialReason.APPROVAL_HASH_MISMATCH)
        needed = {(finding.code, finding.field_path) for finding in requested_revision.warnings}
        if needed and not needed <= set(approval.acknowledged_warnings):
            reasons.append(ExportDenialReason.WARNINGS_NOT_ACKNOWLEDGED)

    return ExportDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        case_id=requested_revision.case_id,
        run_id=requested_revision.run_id,
        revision_id=requested_revision.revision_id,
    )
