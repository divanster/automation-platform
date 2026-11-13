"""Review services: edit, approve, and reject a request, and read its review state. Authorization is HERE.

Every function takes the acting ``user`` (an ``accounts.User``), authenticates and authorizes it itself
(``apps.intake.authorization``), and only then writes. A future API view must call these functions and must
not repeat or skip their checks. The approver identity written into an approval is ``User.public_id`` of the
freshly loaded actor; there is no parameter through which a caller could supply one.

TRANSACTIONS. Each write is ONE ``transaction.atomic()`` block that starts with the run row lock
(``apps.cases.services.lock_run``): a refusal check, the revision / approval / rejection, the run state
change, and every audit event commit together or not at all. Two operations on the same run run one after the
other, and the second sees the first one's result (an edit racing an approval gives exactly one of: approved
then edited, or edited then a stale approval). Lock order is always the run row first; nothing here locks
anything else for write. Written against PostgreSQL; its PostgreSQL tests passed in the owner's local run
(tested scenarios only; docs/development/review-services-slice.md, section 10.4).

WHAT IS REUSED, NOT REPEATED. Validation, canonicalization and hashing: ``apps.intake.editing`` ->
``create_request_intake_revision``. The approval rules: ``approve_revision`` and ``record_approval``. The
revision write rules and the run state table: ``record_revision`` and ``transition_run``. The decisions made
here (permissions, scope, state guards, self-approval, stale checks, warning acknowledgement) are the pure
rules in ``apps.intake.access`` and ``apps.intake.review_rules``.

WHAT THIS DOES NOT PROVIDE. Nothing authenticates a caller (no login, session, or token exists here: the
caller passes a user it has authenticated). There is no API and no UI. A refused attempt is logged by code
only and is not audited (its transaction rolls back). Plaintext business content, and the limits of the
ORM-level immutability and audit append-only protection, are described in ``apps.cases.models`` and
``apps.audit.models``.
"""

import uuid
from dataclasses import dataclass
from typing import NamedTuple

from django.db import transaction

from apps.audit import events as audit
from apps.audit.recorder import list_run_events, record_event
from apps.cases import services as case_services
from apps.cases.adapters import findings_from_json
from apps.cases.errors import RecordNotFoundError, StoredDataError
from apps.cases.models import Approval, Rejection, WorkflowRun
from apps.cases.run_state import RunState
from workflows.request_intake import StructuredRequest, ValidationLimits
from workflows.revision_approval import RevisionFinding, approve_revision, validate_uuid

from .access import Action
from .authorization import authorize, require_permission
from .editing import build_edit_revision
from .errors import RequestSourceMissingError, refusal
from .models import ExtractionOutcome, RequestSource, RevisionEdit
from .policy import OUTCOME_INVALID
from .review_rules import (
    approval_problem,
    edit_problem,
    is_self_approval,
    normalize_warning_pairs,
    raise_if_refused,
    rejection_problem,
)


class EditResult(NamedTuple):
    revision_id: uuid.UUID
    revision_number: int
    content_hash: str
    run_state: str
    warnings: tuple  # ((finding code, field path), ...): what an approver must acknowledge
    approval_invalidated: bool  # the run was approved and is back in needs_review


class ApprovalResult(NamedTuple):
    approval_id: uuid.UUID
    revision_id: uuid.UUID
    content_hash: str
    run_state: str
    acknowledged_warnings: tuple


class RejectionResult(NamedTuple):
    rejection_id: uuid.UUID
    revision_id: uuid.UUID
    reason_code: str
    run_state: str


@dataclass(frozen=True)
class ReviewSummary:
    """What a reviewer needs to act on a run. A plain read: no lock, no snapshot across its queries.

    ``findings`` are those of the current revision, or of the invalid extraction outcome while the run has
    no revision (``current_revision_id`` is then None). They are review material and may quote source text.
    """

    run_id: uuid.UUID
    case_id: uuid.UUID
    state: str
    failed_step: str
    current_revision_id: uuid.UUID | None
    current_revision_number: int | None
    content_hash: str | None
    findings: tuple[RevisionFinding, ...]
    warnings: tuple  # ((finding code, field path), ...)
    outcome_status: str | None
    approval_count: int  # approvals recorded for the current revision
    rejection_count: int  # rejections recorded for the current revision


def _source_of(run: WorkflowRun) -> RequestSource:
    source = RequestSource.objects.filter(run_id=run.pk).first()
    if source is None:
        raise RequestSourceMissingError("The run has no stored request source.")
    return source


def _warning_pairs(revision) -> tuple:
    return tuple((finding.code, finding.field_path) for finding in revision.warnings)


def _current_revision(run: WorkflowRun):
    return None if run.current_revision_id is None else case_services.get_current_revision(run.pk)


def _ids(run: WorkflowRun) -> dict:
    return {"case_id": run.case_id, "run_id": run.pk}


# --- edit ---------------------------------------------------------------------------------------------


def edit_request(
    *,
    user,
    run_id: uuid.UUID,
    base_revision_id: uuid.UUID | None,
    draft: StructuredRequest,
    limits: ValidationLimits | None = None,
) -> EditResult:
    """Save ``draft`` as the run's next revision, on behalf of ``user``.

    ``base_revision_id`` is the revision the caller edited (the run's current revision when they loaded it),
    or None to edit a run whose extraction was invalid and so has no revision yet: that FIRST EDIT creates
    revision 1. The edit is refused, and nothing is stored, when: the user is not authenticated, not active,
    or not permitted (``AuthorizationError``); the run is not in ``needs_review`` or ``approved``; the base is
    not the current revision (``StaleRevisionError``); the draft has blocking findings or cannot be serialized
    (``DraftInvalidError``, carrying the findings); or the draft's content hash equals the current
    revision's (``NoChangeError``). Warnings the validation reports are stored on the new revision.

    A successful edit of an ``approved`` run returns it to ``needs_review``; the earlier approval is kept
    and simply no longer matches the current revision. Earlier revisions are never touched.
    """
    validate_uuid(run_id, "run_id")
    if base_revision_id is not None:
        validate_uuid(base_revision_id, "base_revision_id")
    if not isinstance(draft, StructuredRequest):
        raise TypeError("draft must be a workflows.request_intake.StructuredRequest")
    require_permission(user, Action.EDIT)

    with transaction.atomic():
        run = case_services.lock_run(run_id)
        source = _source_of(run)
        actor = authorize(user, Action.EDIT, submitter_pk=source.submitted_by_id)
        outcome = ExtractionOutcome.objects.filter(run_id=run.pk).first()
        raise_if_refused(
            edit_problem(
                run_state=run.state,
                current_revision_id=run.current_revision_id,
                base_revision_id=base_revision_id,
                has_invalid_outcome=outcome is not None and outcome.status == OUTCOME_INVALID,
            )
        )
        current = _current_revision(run)
        if current is None and RunState(run.state) is RunState.APPROVED:
            raise StoredDataError("an approved run has no revision")
        number = 1 if current is None else current.revision_number + 1
        revision = build_edit_revision(
            draft,
            source_text=source.text,
            case_id=run.case_id,
            run_id=run.pk,
            revision_id=uuid.uuid4(),
            revision_number=number,
            limits=limits,
        )
        if current is not None and revision.content_hash == current.content_hash:
            raise refusal("unchanged_content")

        row = case_services.record_revision(revision)
        RevisionEdit.objects.create(revision=row, base_revision_id=base_revision_id, edited_by=actor.user)

        approval_invalidated = RunState(run.state) is RunState.APPROVED
        if approval_invalidated:
            approval_count = Approval.objects.filter(revision_id=current.revision_id).count()
            case_services.transition_run(run.pk, to=RunState.NEEDS_REVIEW, allowed_from=[RunState.APPROVED])

        ids = _ids(run)
        record_event(
            audit.user_event(
                actor.public_id, audit.ACTION_REVISION_CREATED, audit.SUBJECT_REVISION, row.pk, **ids,
                revision_number=number, content_hash=revision.content_hash, origin="edit",
                blocking_count=0, warning_count=len(revision.warnings),
            )
        )  # fmt: skip
        record_event(
            audit.user_event(
                actor.public_id, audit.ACTION_REQUEST_EDITED, audit.SUBJECT_REVISION, row.pk, **ids,
                base_revision_id=None if base_revision_id is None else str(base_revision_id), first_edit=current is None,
            )
        )  # fmt: skip
        if approval_invalidated:
            record_event(
                audit.user_event(
                    actor.public_id, audit.ACTION_APPROVAL_INVALIDATED, audit.SUBJECT_REVISION, current.revision_id, **ids,
                    revision_id=str(current.revision_id), by_revision_id=str(row.pk), approval_count=approval_count,
                )
            )  # fmt: skip
            record_event(
                audit.user_event(
                    actor.public_id, audit.ACTION_RUN_STATE_CHANGED, audit.SUBJECT_RUN, run.pk, **ids,
                    from_state=RunState.APPROVED.value, to_state=RunState.NEEDS_REVIEW.value, cause="edit",
                )
            )  # fmt: skip

    return EditResult(row.pk, number, revision.content_hash, RunState.NEEDS_REVIEW.value, _warning_pairs(revision), approval_invalidated)


# --- approve ------------------------------------------------------------------------------------------


def approve_request(
    *,
    user,
    run_id: uuid.UUID,
    revision_id: uuid.UUID,
    content_hash: str,
    acknowledged_warnings=(),
) -> ApprovalResult:
    """Approve the run's current revision, on behalf of ``user``, and move the run to ``approved``.

    The approver recorded is ``user``'s ``public_id``, read fresh. Refused, and nothing is stored, when (in
    this order, after authentication, permission and scope): the run is not in ``needs_review``; it has no
    revision; the user submitted the request or authored a revision of it (``SelfApprovalError``, superusers
    included); ``revision_id`` is not the current revision (``StaleRevisionError``); ``content_hash`` is not
    its hash; it was rejected; or ``acknowledged_warnings`` is not exactly the revision's current warnings
    as ``(finding code, field path)`` pairs (``WarningsNotAcknowledgedError``). The revision is then approved
    through ``approve_revision`` and ``record_approval``. Earlier approvals and rejections are kept.
    """
    validate_uuid(run_id, "run_id")
    validate_uuid(revision_id, "revision_id")
    if not isinstance(content_hash, str):
        raise TypeError("content_hash must be a str")
    acknowledged = normalize_warning_pairs(acknowledged_warnings)
    require_permission(user, Action.APPROVE)

    with transaction.atomic():
        run = case_services.lock_run(run_id)
        source = _source_of(run)
        actor = authorize(user, Action.APPROVE, submitter_pk=source.submitted_by_id)
        current = _current_revision(run)
        authors = RevisionEdit.objects.filter(revision__run_id=run.pk).values_list("edited_by_id", flat=True)
        raise_if_refused(
            approval_problem(
                run_state=run.state,
                current_revision_id=run.current_revision_id,
                revision_id=revision_id,
                current_hash=None if current is None else current.content_hash,
                submitted_hash=content_hash,
                self_approval=is_self_approval(actor_pk=actor.pk, submitter_pk=source.submitted_by_id, author_pks=authors),
                revision_rejected=current is not None and Rejection.objects.filter(revision_id=current.revision_id).exists(),
                current_warnings=() if current is None else _warning_pairs(current),
                acknowledged_warnings=acknowledged,
            )
        )
        # The acknowledgement was just checked against the current warnings, so True is now a fact about the call.
        record = approve_revision(current, approver_id=actor.public_id, warnings_reviewed=True)
        row = case_services.record_approval(record)
        case_services.transition_run(run.pk, to=RunState.APPROVED, allowed_from=[RunState.NEEDS_REVIEW])

        ids = _ids(run)
        record_event(
            audit.user_event(
                actor.public_id, audit.ACTION_APPROVAL_RECORDED, audit.SUBJECT_APPROVAL, row.pk, **ids,
                revision_id=str(current.revision_id), content_hash=current.content_hash, warning_count=len(record.acknowledged_warnings),
            )
        )  # fmt: skip
        record_event(
            audit.user_event(
                actor.public_id, audit.ACTION_RUN_STATE_CHANGED, audit.SUBJECT_RUN, run.pk, **ids,
                from_state=RunState.NEEDS_REVIEW.value, to_state=RunState.APPROVED.value, cause="approval",
            )
        )  # fmt: skip

    return ApprovalResult(row.pk, current.revision_id, current.content_hash, RunState.APPROVED.value, record.acknowledged_warnings)


# --- reject -------------------------------------------------------------------------------------------


def reject_request(
    *,
    user,
    run_id: uuid.UUID,
    revision_id: uuid.UUID,
    content_hash: str,
    reason_code: str,
) -> RejectionResult:
    """Record a rejection of the run's current revision, on behalf of ``user``.

    RESULTING STATE: the run STAYS in ``needs_review`` (the normative table in first-workflow.md); a rejection
    is a recorded decision, not a state. The revision, its findings, and earlier approvals and rejections are
    untouched, the rejected revision can no longer be approved, and the way forward is an edit, which creates
    a new revision. ``reason_code`` must be one of ``apps.intake.policy.REJECTION_REASONS``; no free text is
    accepted anywhere. Refused, and nothing stored, for the run not being in ``needs_review``, no current
    revision, a stale revision id, a wrong hash, or an unknown reason code.
    """
    validate_uuid(run_id, "run_id")
    validate_uuid(revision_id, "revision_id")
    if not isinstance(content_hash, str):
        raise TypeError("content_hash must be a str")
    if not isinstance(reason_code, str):
        raise TypeError("reason_code must be a str")
    require_permission(user, Action.REJECT)

    with transaction.atomic():
        run = case_services.lock_run(run_id)
        source = _source_of(run)
        actor = authorize(user, Action.REJECT, submitter_pk=source.submitted_by_id)
        current = _current_revision(run)
        raise_if_refused(
            rejection_problem(
                run_state=run.state,
                current_revision_id=run.current_revision_id,
                revision_id=revision_id,
                current_hash=None if current is None else current.content_hash,
                submitted_hash=content_hash,
                reason_code=reason_code,
            )
        )
        row = case_services.record_rejection(
            revision_id=current.revision_id, content_hash=current.content_hash, rejected_by=actor.public_id, reason_code=reason_code
        )
        record_event(
            audit.user_event(
                actor.public_id, audit.ACTION_REJECTION_RECORDED, audit.SUBJECT_REJECTION, row.pk, **_ids(run),
                revision_id=str(current.revision_id), content_hash=current.content_hash, reason_code=reason_code,
            )
        )  # fmt: skip

    return RejectionResult(row.pk, current.revision_id, reason_code, run.state)


# --- read ---------------------------------------------------------------------------------------------


def get_review_summary(*, user, run_id: uuid.UUID) -> ReviewSummary:
    """The review state of a run, for a user who may view it (own requests, or any with ``view_any_request``)."""
    validate_uuid(run_id, "run_id")
    require_permission(user, Action.VIEW)
    try:
        run = WorkflowRun.objects.get(pk=run_id)
    except WorkflowRun.DoesNotExist as error:
        raise RecordNotFoundError("The run does not exist.") from error
    source = _source_of(run)
    authorize(user, Action.VIEW, submitter_pk=source.submitted_by_id)

    current = _current_revision(run)
    outcome = ExtractionOutcome.objects.filter(run_id=run.pk).first()
    if current is not None:
        findings, warnings = current.findings, _warning_pairs(current)
        approvals = Approval.objects.filter(revision_id=current.revision_id).count()
        rejections = Rejection.objects.filter(revision_id=current.revision_id).count()
    else:
        findings = () if outcome is None else findings_from_json(outcome.findings)
        warnings = tuple((f.code, f.field_path) for f in findings if not f.blocking)
        approvals = rejections = 0
    return ReviewSummary(
        run_id=run.pk,
        case_id=run.case_id,
        state=run.state,
        failed_step=run.failed_step,
        current_revision_id=None if current is None else current.revision_id,
        current_revision_number=None if current is None else current.revision_number,
        content_hash=None if current is None else current.content_hash,
        findings=findings,
        warnings=warnings,
        outcome_status=None if outcome is None else outcome.status,
        approval_count=approvals,
        rejection_count=rejections,
    )


def list_audit_events(*, user, run_id: uuid.UUID) -> list:
    """The audit events of a run, in the order written, for a user with ``audit.view_auditevent`` (or a superuser)."""
    validate_uuid(run_id, "run_id")
    require_permission(user, Action.VIEW_AUDIT)
    return list_run_events(run_id)
