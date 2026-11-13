"""Persistence operations for revisions, approvals and rejections, on top of the pure policy.

Every write goes through ``record_revision``, ``record_approval``, ``record_rejection`` or
``transition_run``; there is no other supported way to change a run's current revision or state. Reads
return the pure domain objects (``workflows.revision_approval``), which re-verify hashes and consistency
on the way out.

WRITES ARE SERIALIZED PER RUN. ``record_revision``, ``record_approval``, ``record_rejection`` and ``transition_run`` open
``transaction.atomic()`` and lock the run's row with ``SELECT ... FOR UPDATE`` before reading or
writing anything else for that run, so two competing writers for the same run run one after the
other, and the second one sees the first one's result. This relies on PostgreSQL row locking and was
written against it. Database tests are not run in the development VM; the project owner ran the
revision and approval tests locally against PostgreSQL 18.6 and they passed for the tested scenarios
only (see docs/development/local-validation-record.md and
docs/development/revision-approval-policy.md). ``transition_run`` and ``lock_run`` were added with the
request-intake orchestration slice and have not been run against PostgreSQL yet. None of this proves
that no other race exists.

WHAT THIS DOES NOT PROVIDE. Persistence alone does not make an export race-safe.
``check_persisted_export_eligibility`` is a plain read: it takes no lock and does not hold a
snapshot, so a revision recorded a moment later is not seen. The export-commit re-check
(re-reading the current revision and approval atomically when the export is committed) is
future work. Nothing here authenticates or authorizes anyone: ``approver_id`` is stored as data.
"""

import re
import uuid
from uuid import UUID

from django.db import transaction
from django.db.models import Max

from workflows.revision_approval import (
    ApprovalRecord,
    ExportDecision,
    InvalidRevisionError,
    approve_revision,
    check_export_eligibility,
    validate_uuid,
)
from workflows.revision_approval import ResultRevision as DomainRevision

from .adapters import StoredApproval, findings_to_json, to_domain_revision, to_stored_approval
from .errors import (
    ApprovalMismatchError,
    BindingMismatchError,
    InvalidReasonCodeError,
    InvalidTransitionError,
    NotCurrentRevisionError,
    RecordNotFoundError,
    RevisionNumberError,
    StoredDataError,
)
from .models import REASON_CODE_PATTERN, Approval, Case, Rejection, ResultRevision, WorkflowRun
from .run_state import RunState, transition_problem


# --- pure write rules (no database access; unit-testable without a database) ------------------


def check_revision_write(*, run_case_id, revision: DomainRevision, highest_number, current_number):
    """Refuse a revision write that breaks the persistence rules; return None when it is fine.

    ``highest_number`` is the highest stored revision number of the run (None when there is
    none); ``current_number`` is the number of the revision the run points at (None when the
    pointer is unset). Rules: the revision belongs to the run's case; the pointer, if set,
    points at the highest revision (otherwise the stored data is inconsistent); and the number
    is exactly one more than the highest (numbers start at 1 per run and increase by one).
    """
    if run_case_id != revision.case_id:
        raise BindingMismatchError("The revision's case is not the case of its run.")
    if highest_number != current_number:
        raise StoredDataError("the run's current-revision pointer is not its highest revision")
    expected = (highest_number or 0) + 1
    if revision.revision_number != expected:
        raise RevisionNumberError(expected)


def check_approval_write(*, run_case_id, run_id, current_revision_id, revision_row_run_id, stored_hash, approval: ApprovalRecord):
    """Refuse an approval write that is not bound to the run's stored current revision."""
    if run_case_id != approval.case_id:
        raise BindingMismatchError("The approval's case is not the case of its run.")
    if run_id != approval.run_id or revision_row_run_id != approval.run_id:
        raise BindingMismatchError("The approval's run is not the run of its revision.")
    if stored_hash != approval.content_hash:
        raise BindingMismatchError("The approval's content hash is not the stored revision's hash.")
    if current_revision_id != approval.revision_id:
        raise NotCurrentRevisionError("An approval can be recorded only for the run's current revision.")


# --- minimal case and run creation --------------------------------------------------------------


def create_case(*, reference: str, case_id: UUID | None = None) -> Case:
    """Create a case. ``case_id`` is generated here (the persistence layer) when not supplied."""
    if case_id is not None:
        validate_uuid(case_id, "case_id")
    return Case.objects.create(id=case_id or uuid.uuid4(), reference=reference)


def create_run(*, case_id: UUID, run_id: UUID | None = None) -> WorkflowRun:
    """Create a run for an existing case, with no current revision yet."""
    validate_uuid(case_id, "case_id")
    if run_id is not None:
        validate_uuid(run_id, "run_id")
    try:
        case = Case.objects.get(pk=case_id)
    except Case.DoesNotExist as error:
        raise RecordNotFoundError("The case does not exist.") from error
    return WorkflowRun.objects.create(id=run_id or uuid.uuid4(), case=case)


# --- writes ---------------------------------------------------------------------------------------


def _lock_run(run_id: UUID) -> WorkflowRun:
    """Lock the run row until the surrounding transaction ends (must be inside ``atomic``)."""
    try:
        return WorkflowRun.objects.select_for_update().get(pk=run_id)
    except WorkflowRun.DoesNotExist as error:
        raise RecordNotFoundError("The run does not exist.") from error


def lock_run(run_id: UUID) -> WorkflowRun:
    """Lock the run row until the surrounding transaction ends. Call it inside ``atomic``.

    The public form of the lock every run write takes. Locking a row this transaction already holds
    is harmless, so a caller can lock the run first and then call ``record_revision`` or
    ``transition_run`` in the same transaction.
    """
    validate_uuid(run_id, "run_id")
    return _lock_run(run_id)


def transition_run(run_id: UUID, *, to: RunState, failed_step: str = "", allowed_from=None) -> WorkflowRun:
    """Move the run to state ``to`` if the transition table allows it; the only way state changes.

    One transaction: lock the run, check ``transition_problem`` against the stored state and failed
    step (and, when ``allowed_from`` is given, that the stored state is one of those), then save. A
    refusal raises ``InvalidTransitionError`` and writes nothing. ``failed_step`` is required exactly
    when ``to`` is ``failed``; it is cleared on every other transition. Returns the updated row.
    """
    validate_uuid(run_id, "run_id")
    target = RunState(to)
    with transaction.atomic():
        run = _lock_run(run_id)
        if allowed_from is not None and run.state not in {RunState(state).value for state in allowed_from}:
            raise InvalidTransitionError("state_mismatch")
        problem = transition_problem(run.state, target, current_failed_step=run.failed_step, new_failed_step=failed_step)
        if problem is not None:
            raise InvalidTransitionError(problem)
        run.state = target.value
        run.failed_step = failed_step
        run.save(update_fields=["state", "failed_step"])
    return run


def record_revision(revision: DomainRevision) -> ResultRevision:
    """Persist a domain revision as the run's next revision and make it the current one.

    One transaction: lock the run, check the persistence rules (``check_revision_write``),
    insert the immutable row, and move the run's pointer to it. The pointer only moves forward
    because the number must be the highest plus one. Raises ``RevisionNumberError`` for a
    duplicate or skipped number, ``BindingMismatchError`` when the run belongs to another case,
    ``RecordNotFoundError`` for an unknown run.
    """
    if not isinstance(revision, DomainRevision):
        raise TypeError("revision must be a workflows.revision_approval.ResultRevision")
    problems = revision.consistency_problems()
    if problems:
        raise InvalidRevisionError(problems)

    with transaction.atomic():
        run = _lock_run(revision.run_id)
        highest = ResultRevision.objects.filter(run_id=run.pk).aggregate(highest=Max("revision_number"))["highest"]
        current_number = None
        if run.current_revision_id is not None:
            current_number = ResultRevision.objects.filter(pk=run.current_revision_id, run_id=run.pk).values_list("revision_number", flat=True).first()
        check_revision_write(run_case_id=run.case_id, revision=revision, highest_number=highest, current_number=current_number)

        row = ResultRevision.objects.create(
            id=revision.revision_id,
            run=run,
            revision_number=revision.revision_number,
            canonical_format_version=revision.canonical_format_version,
            canonical_bytes=revision.canonical_bytes,
            content_hash=revision.content_hash,
            findings=findings_to_json(revision.findings),
        )
        run.current_revision = row
        run.save(update_fields=["current_revision"])
    return row


def record_approval(approval: ApprovalRecord, *, approval_id: UUID | None = None) -> Approval:
    """Persist a domain approval for the run's current revision.

    One transaction: lock the run, load the stored revision (re-verifying it through the
    domain), check the binding (case, run, revision, content hash) and that the revision is
    still the run's current one, then insert the immutable row. The supplied record must equal
    what ``approve_revision`` produces for the stored revision, so an approval that skipped the
    blocking-findings or warning-acknowledgement rules is refused. Earlier approvals are never
    touched. ``approver_id`` is stored as data; nothing here authenticates it.
    """
    if not isinstance(approval, ApprovalRecord):
        raise TypeError("approval must be a workflows.revision_approval.ApprovalRecord")
    if approval_id is not None:
        validate_uuid(approval_id, "approval_id")

    with transaction.atomic():
        run = _lock_run(approval.run_id)
        try:
            revision_row = ResultRevision.objects.select_related("run").get(pk=approval.revision_id)
        except ResultRevision.DoesNotExist as error:
            raise RecordNotFoundError("The revision does not exist.") from error
        check_approval_write(
            run_case_id=run.case_id,
            run_id=run.pk,
            current_revision_id=run.current_revision_id,
            revision_row_run_id=revision_row.run_id,
            stored_hash=revision_row.content_hash,
            approval=approval,
        )
        stored = to_domain_revision(revision_row)
        # Used only to derive the exact record the policy yields for this revision (warning
        # acknowledgements included); passing True here is not a claim that anyone reviewed it.
        expected = approve_revision(stored, approver_id=approval.approver_id, warnings_reviewed=True)
        if approval != expected:
            raise ApprovalMismatchError("The approval does not match what the policy produces for the stored revision.")
        return Approval.objects.create(
            id=approval_id or uuid.uuid4(),
            revision=revision_row,
            content_hash=approval.content_hash,
            approver_id=approval.approver_id,
            acknowledged_warnings=[list(pair) for pair in approval.acknowledged_warnings],
        )


def check_rejection_write(*, revision_id, current_revision_id, stored_hash, submitted_hash, reason_code):
    """Refuse a rejection write that is not bound to the run's stored current revision; None when fine."""
    if not isinstance(reason_code, str) or not re.fullmatch(REASON_CODE_PATTERN, reason_code):
        raise InvalidReasonCodeError("The reason code is not a stable code.")
    if stored_hash != submitted_hash:
        raise BindingMismatchError("The rejection's content hash is not the stored revision's hash.")
    if current_revision_id != revision_id:
        raise NotCurrentRevisionError("A rejection can be recorded only for the run's current revision.")


def record_rejection(*, revision_id: UUID, content_hash: str, rejected_by: UUID, reason_code: str, rejection_id: UUID | None = None) -> Rejection:
    """Persist a rejection of the run's current revision.

    One transaction: find the revision's run, lock it, re-read the stored revision (re-verifying it
    through the domain), check that the submitted hash is the stored one and that the revision is still
    the run's current one, then insert the immutable row. The revision, earlier approvals and rejections,
    and the run are never touched (state and audit are the caller's job). ``rejected_by`` is stored as
    data; nothing here authenticates it. ``reason_code`` is checked for shape only.
    """
    validate_uuid(revision_id, "revision_id")
    validate_uuid(rejected_by, "rejected_by")
    if rejection_id is not None:
        validate_uuid(rejection_id, "rejection_id")

    with transaction.atomic():
        run_id = _fetch_revision_row(revision_id).run_id  # revisions are immutable: the run cannot change
        run = _lock_run(run_id)
        revision_row = _fetch_revision_row(revision_id)
        to_domain_revision(revision_row)  # re-verify the stored bytes and hash; StoredDataError if corrupted
        check_rejection_write(
            revision_id=revision_row.pk,
            current_revision_id=run.current_revision_id,
            stored_hash=revision_row.content_hash,
            submitted_hash=content_hash,
            reason_code=reason_code,
        )
        return Rejection.objects.create(
            id=rejection_id or uuid.uuid4(),
            revision=revision_row,
            content_hash=content_hash,
            rejected_by=rejected_by,
            reason_code=reason_code,
        )


# --- reads ----------------------------------------------------------------------------------------


def _fetch_revision_row(revision_id: UUID) -> ResultRevision:
    try:
        return ResultRevision.objects.select_related("run").get(pk=revision_id)
    except ResultRevision.DoesNotExist as error:
        raise RecordNotFoundError("The revision does not exist.") from error


def _fetch_run_row(run_id: UUID) -> WorkflowRun:
    try:
        return WorkflowRun.objects.get(pk=run_id)
    except WorkflowRun.DoesNotExist as error:
        raise RecordNotFoundError("The run does not exist.") from error


def _fetch_approval_row(approval_id: UUID) -> Approval:
    try:
        return Approval.objects.select_related("revision__run").get(pk=approval_id)
    except Approval.DoesNotExist as error:
        raise RecordNotFoundError("The approval does not exist.") from error


def load_revision(revision_id: UUID) -> DomainRevision:
    """The domain revision with this id (hash and consistency re-verified)."""
    validate_uuid(revision_id, "revision_id")
    return to_domain_revision(_fetch_revision_row(revision_id))


def get_current_revision(run_id: UUID) -> DomainRevision | None:
    """The run's current revision, or None when it has none yet."""
    validate_uuid(run_id, "run_id")
    run = _fetch_run_row(run_id)
    if run.current_revision_id is None:
        return None
    row = _fetch_revision_row(run.current_revision_id)
    if row.run_id != run.pk:
        raise StoredDataError("the run's current-revision pointer refers to another run's revision")
    return to_domain_revision(row)


def list_revisions(run_id: UUID) -> tuple[DomainRevision, ...]:
    """All of the run's revisions, oldest first."""
    validate_uuid(run_id, "run_id")
    _fetch_run_row(run_id)
    rows = ResultRevision.objects.filter(run_id=run_id).select_related("run").order_by("revision_number")
    return tuple(to_domain_revision(row) for row in rows)


def load_approval(approval_id: UUID) -> StoredApproval:
    validate_uuid(approval_id, "approval_id")
    return to_stored_approval(_fetch_approval_row(approval_id))


def list_approvals(revision_id: UUID) -> tuple[StoredApproval, ...]:
    """All approvals recorded for the revision, oldest first (historical ones included)."""
    validate_uuid(revision_id, "revision_id")
    _fetch_revision_row(revision_id)
    rows = Approval.objects.filter(revision_id=revision_id).select_related("revision__run").order_by("created_at", "pk")
    return tuple(to_stored_approval(row) for row in rows)


def check_persisted_export_eligibility(
    *,
    expected_case_id: UUID,
    expected_run_id: UUID,
    requested_revision_id: UUID,
    approval_id: UUID | None = None,
) -> ExportDecision:
    """Load the stored revision, the expected run's current revision, and the approval, then
    ask the pure policy. The decision, its denial codes, and their order come unchanged from
    ``check_export_eligibility``; nothing is added or reinterpreted here.

    The approval is loaded by id and may belong to any run, so an approval from another run
    reaches the policy and is denied there (``approval_run_mismatch``). A run with no current
    revision raises ``RecordNotFoundError``: there is nothing to compare. This is a plain read
    (no lock, no snapshot): it is NOT the atomic re-check an export commit will need.
    """
    validate_uuid(expected_case_id, "expected_case_id")
    validate_uuid(expected_run_id, "expected_run_id")
    validate_uuid(requested_revision_id, "requested_revision_id")
    if approval_id is not None:
        validate_uuid(approval_id, "approval_id")

    requested = to_domain_revision(_fetch_revision_row(requested_revision_id))
    current = get_current_revision(expected_run_id)
    if current is None:
        raise RecordNotFoundError("The expected run has no current revision.")
    approval = None if approval_id is None else to_stored_approval(_fetch_approval_row(approval_id)).record
    return check_export_eligibility(
        expected_case_id=expected_case_id,
        expected_run_id=expected_run_id,
        requested_revision=requested,
        current_revision=current,
        approval=approval,
    )
