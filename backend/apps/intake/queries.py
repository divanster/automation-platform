"""Read services for the request API: a paginated list, the dashboard summary, the detail read model, and a paginated audit read.

Everything here is READ-ONLY and authorized like the write services (``apps.intake.authorization``): the
actor is reloaded from the database, and the rules are the pure ones in ``apps.intake.access`` and
``apps.intake.review_rules``. A view never decides who may see a request or which actions to offer; it asks
these functions.

VISIBILITY. A user with ``view_any_request`` (or a superuser) sees every request; a user with only
``view_own_request`` sees the requests they submitted; a user with neither gets ``AuthorizationError``
(``missing_permission``) before anything is read. ``get_request_detail`` raises ``AuthorizationError`` with
``out_of_scope`` for an existing request that is not the user's, and ``RecordNotFoundError`` for an unknown
one; the API maps both to the same 404 so it never says which.

NO CONTENT LEAKS HERE BY ACCIDENT. The detail returns the parsed draft and the findings (review material
for a user who may view the request) but never the canonical bytes, the source text, or a task's payload or
result; the processing status carries only the queue's closed failure-code vocabulary.

These functions take no lock and no snapshot across their queries: a read racing a write may see the run
before and the revision after, never a half-written revision (writes are single transactions).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from django.db.models import Count

from apps.accounts.authz import load_actor
from apps.cases import services as case_services
from apps.cases.errors import RecordNotFoundError
from apps.cases.models import Rejection, ResultRevision, WorkflowRun
from apps.cases.run_state import STATES
from workflows.revision_approval import RevisionFinding

from . import review
from .access import Action, access_problem, scope_problem
from .authorization import require_permission
from .draft_json import draft_from_canonical_bytes, draft_from_json, draft_to_json
from .models import ExtractionOutcome, RequestSource, RevisionEdit, StepTask
from .policy import OUTCOME_INVALID, REJECTION_REASONS, STEP_EXTRACT
from .review_rules import approval_problem, edit_problem, is_self_approval, rejection_problem

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

DRAFT_FROM_REVISION = "revision"
DRAFT_FROM_OUTCOME = "extraction_outcome"


def _check_page(page, page_size):
    for name, value in (("page", page), ("page_size", page_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be an integer of at least 1")
    if page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be at most {MAX_PAGE_SIZE}")


class RequestListItem(NamedTuple):
    run_id: uuid.UUID
    case_id: uuid.UUID
    reference: str
    state: str
    failed_step: str
    created_at: datetime
    current_revision_number: int | None
    outcome_status: str | None
    submitted_by: uuid.UUID  # the submitter's public_id


class RequestPage(NamedTuple):
    items: tuple
    count: int
    page: int
    page_size: int


LIST_RELATED = ("case", "current_revision", "request_source__submitted_by", "extraction_outcome")


def _visible_runs(actor_pk, sees_all: bool, state: str | None):
    """The runs a user may list, newest first. Only runs created through ``submit_request`` (they have a source)."""
    runs = WorkflowRun.objects.filter(request_source__isnull=False)
    if not sees_all:
        runs = runs.filter(request_source__submitted_by_id=actor_pk)
    if state is not None:
        runs = runs.filter(state=state)
    return runs.order_by("-created_at", "-id")


def _view_scope(user):
    """The actor and whether they see EVERY request (``view_any_request`` or a superuser) or only their own.

    The one place the list and the dashboard summary decide visibility: it raises ``AuthorizationError`` for an
    unauthenticated, inactive or view-permission-less user BEFORE any request is read.
    """
    actor = require_permission(user, Action.VIEW)
    sees_all = (
        scope_problem(
            Action.VIEW, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, submitter_pk=None
        )
        is None
    )
    return actor, sees_all


def _list_item(run) -> RequestListItem:
    """The list row of a run loaded with ``select_related(*LIST_RELATED)`` (no query per row)."""
    return RequestListItem(
        run_id=run.pk,
        case_id=run.case_id,
        reference=run.case.reference,
        state=run.state,
        failed_step=run.failed_step,
        created_at=run.created_at,
        current_revision_number=None if run.current_revision is None else run.current_revision.revision_number,
        outcome_status=getattr(getattr(run, "extraction_outcome", None), "status", None),
        submitted_by=run.request_source.submitted_by.public_id,
    )


def list_requests(*, user, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, state: str | None = None) -> RequestPage:
    """One page of the requests ``user`` may view, newest first (run id breaks ties, so the order is stable).

    ``state`` optionally narrows to one run state. Raises ``ValueError`` for a bad page, page size (1 to
    ``MAX_PAGE_SIZE``) or state, and ``AuthorizationError`` for an unauthenticated, inactive or
    view-permission-less user.
    """
    _check_page(page, page_size)
    if state is not None and state not in STATES:
        raise ValueError("state must be a known run state")
    actor, sees_all = _view_scope(user)

    runs = _visible_runs(actor.pk, sees_all, state)
    count = runs.count()
    rows = runs.select_related(*LIST_RELATED)[(page - 1) * page_size : page * page_size]
    return RequestPage(tuple(_list_item(run) for run in rows), count, page, page_size)


RECENT_LIMIT = 5  # the dashboard's "newest requests" size; a fixed bound, not a client choice
# The only columns ``_list_item`` reads. The list loads whole rows (its page may be 100 of them); the dashboard is polled, so it
# reads just these and never loads the source text, a revision's bytes, a draft or the submitter's password hash.
RECENT_FIELDS = (
    "state",
    "failed_step",
    "created_at",
    "case__reference",
    "current_revision__revision_number",
    "request_source__submitted_by__public_id",
    "extraction_outcome__status",
)
# States in which the system is still working on a request. FE-6 shows the same group as "Processing"
# (frontend/src/lib/format.ts PROCESSING_STATES); the group is defined here so the server owns the number.
PROCESSING_STATES = ("queued", "extracting", "exporting")


@dataclass(frozen=True)
class DashboardSummary:
    total: int
    processing: int
    needs_review: int
    approved: int
    failed: int
    by_state: dict  # every run state, zero when there is none, in the run-state vocabulary's order
    recent_requests: tuple  # RequestListItem, newest first


def get_dashboard_summary(*, user) -> DashboardSummary:
    """Counts of the requests ``user`` may view by run state, plus the newest few of them.

    VISIBILITY is exactly the list's (``_view_scope`` and ``_visible_runs``): ``view_any_request`` or a superuser
    sees every request, ``view_own_request`` only the user's own, anyone else gets ``AuthorizationError``.

    TWO queries, whatever the number of requests: one ``GROUP BY state`` aggregate (so ``total`` is the sum of
    ``by_state`` by construction; one statement is one snapshot) and one list query limited to ``RECENT_LIMIT``
    rows with the list's joins. The recent rows are a second statement: a request that changes between the two can
    appear in a different state than the counts show; the next refresh corrects it. Nothing here reads request text.
    """
    actor, sees_all = _view_scope(user)
    visible = _visible_runs(actor.pk, sees_all, None)

    by_state = dict.fromkeys(STATES, 0)
    for row in visible.order_by().values("state").annotate(n=Count("pk")):
        by_state[row["state"]] = row["n"]

    recent = tuple(_list_item(run) for run in visible.select_related(*LIST_RELATED).only(*RECENT_FIELDS)[:RECENT_LIMIT])
    return DashboardSummary(
        total=sum(by_state.values()),
        processing=sum(by_state[state] for state in PROCESSING_STATES),
        needs_review=by_state["needs_review"],
        approved=by_state["approved"],
        failed=by_state["failed"],
        by_state=by_state,
        recent_requests=recent,
    )


@dataclass(frozen=True)
class RequestDetail:
    run_id: uuid.UUID
    case_id: uuid.UUID
    reference: str
    state: str
    failed_step: str
    created_at: datetime
    submitted_by: uuid.UUID
    processing: dict | None  # {"step", "task_state", "attempt_count", "failure_code"} or None
    outcome_status: str | None
    current_revision: dict | None  # {"id", "number", "content_hash", "created_at"} or None
    draft: dict | None  # draft_to_json form (decimal quantities as text)
    draft_source: str | None
    findings: tuple[RevisionFinding, ...]
    warnings: tuple  # ((finding code, field path), ...)
    approval_count: int
    rejection_count: int
    allowed_actions: tuple[str, ...]
    action_blockers: dict  # {action: stable reason code}


def _extract_links(run_id):
    """The run's extraction step links, newest generation first, with their queue tasks."""
    return StepTask.objects.filter(run_id=run_id, step=STEP_EXTRACT).select_related("task").order_by("-generation")


def _processing(run_id) -> dict | None:
    link = _extract_links(run_id).first()
    if link is None:
        return None
    task = link.task
    return {
        "step": STEP_EXTRACT,
        "task_state": task.state,
        "attempt_count": task.attempt_count,
        "failure_code": task.failure_code or None,
    }


def _actions(*, actor, run, source, current, has_invalid_outcome, warnings) -> tuple[tuple, dict]:
    """The actions THIS user may attempt on THIS run now, and why not for the ones they hold permission for.

    Built from the same pure rules the services apply, with the inputs a correct caller would send (the
    current revision, its hash, its exact warnings), so "allowed" means "the service would not refuse it for
    a reason that depends on the request's state". An action the user lacks permission or scope for is not
    listed and has no blocker (the answer must not describe permissions the user does not have).
    """
    allowed, blockers = [], {}

    def holds(action):
        return (
            access_problem(
                action,
                is_superuser=actor.is_superuser,
                permissions=actor.permissions,
                actor_pk=actor.pk,
                submitter_pk=source.submitted_by_id,
            )
            is None
        )

    def record(action, reason):
        if reason is None:
            allowed.append(action.value)
        else:
            blockers[action.value] = reason

    current_id = run.current_revision_id
    current_hash = None if current is None else current.content_hash
    if holds(Action.EDIT):
        record(
            Action.EDIT,
            edit_problem(run_state=run.state, current_revision_id=current_id, base_revision_id=current_id, has_invalid_outcome=has_invalid_outcome),
        )
    if holds(Action.APPROVE):
        authors = RevisionEdit.objects.filter(revision__run_id=run.pk).values_list("edited_by_id", flat=True)
        problem = approval_problem(
            run_state=run.state,
            current_revision_id=current_id,
            revision_id=current_id,
            current_hash=current_hash,
            submitted_hash=current_hash,
            self_approval=is_self_approval(actor_pk=actor.pk, submitter_pk=source.submitted_by_id, author_pks=authors),
            revision_rejected=current is not None and Rejection.objects.filter(revision_id=current.revision_id).exists(),
            current_warnings=warnings,
            acknowledged_warnings=warnings,
        )
        record(Action.APPROVE, None if problem is None else problem[0])
    if holds(Action.REJECT):
        record(
            Action.REJECT,
            rejection_problem(
                run_state=run.state,
                current_revision_id=current_id,
                revision_id=current_id,
                current_hash=current_hash,
                submitted_hash=current_hash,
                reason_code=REJECTION_REASONS[0],
            ),
        )
    return tuple(allowed), blockers


def get_request_detail(*, user, run_id: uuid.UUID) -> RequestDetail:
    """Everything a reviewer needs about one request, for a user who may view it.

    Authorization and existence are those of ``review.get_review_summary`` (``AuthorizationError``,
    ``RecordNotFoundError``). Reads the stored revision through the domain adapter, which re-verifies its hash
    and consistency, so a corrupted revision raises ``StoredDataError`` instead of being shown.
    """
    summary = review.get_review_summary(user=user, run_id=run_id)
    actor = load_actor(user)
    run = WorkflowRun.objects.select_related("case").get(pk=summary.run_id)
    source = RequestSource.objects.select_related("submitted_by").get(run_id=run.pk)
    outcome = ExtractionOutcome.objects.filter(run_id=run.pk).first()

    current = case_services.get_current_revision(run.pk) if run.current_revision_id is not None else None
    revision_row = None
    if current is not None:
        revision_row = ResultRevision.objects.get(pk=current.revision_id)
        draft = draft_to_json(draft_from_canonical_bytes(current.canonical_bytes))
        draft_source = DRAFT_FROM_REVISION
    elif outcome is not None:
        draft = draft_to_json(draft_from_json(outcome.draft))
        draft_source = DRAFT_FROM_OUTCOME
    else:
        draft, draft_source = None, None

    allowed, blockers = _actions(
        actor=actor,
        run=run,
        source=source,
        current=current,
        has_invalid_outcome=outcome is not None and outcome.status == OUTCOME_INVALID,
        warnings=summary.warnings,
    )
    return RequestDetail(
        run_id=run.pk,
        case_id=run.case_id,
        reference=run.case.reference,
        state=run.state,
        failed_step=run.failed_step,
        created_at=run.created_at,
        submitted_by=source.submitted_by.public_id,
        processing=_processing(run.pk),
        outcome_status=summary.outcome_status,
        current_revision=None
        if current is None
        else {"id": current.revision_id, "number": current.revision_number, "content_hash": current.content_hash, "created_at": revision_row.created_at},
        draft=draft,
        draft_source=draft_source,
        findings=summary.findings,
        warnings=summary.warnings,
        approval_count=summary.approval_count,
        rejection_count=summary.rejection_count,
        allowed_actions=allowed,
        action_blockers=blockers,
    )


class AuditPage(NamedTuple):
    items: tuple  # AuditEvent rows, oldest first
    count: int
    page: int
    page_size: int


def list_run_audit(*, user, run_id: uuid.UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> AuditPage:
    """One page of the run's audit events, oldest first, through the existing audit reader.

    ``review.list_audit_events`` does the authorization (``audit.view_auditevent`` or a superuser) and
    returns every event of the run (a run's events are few: a handful per edit or decision); this slices one
    page. An unknown run is ``RecordNotFoundError`` (an auditor may know which runs exist).
    """
    _check_page(page, page_size)
    events = review.list_audit_events(user=user, run_id=run_id)
    if not events and not WorkflowRun.objects.filter(pk=run_id).exists():
        raise RecordNotFoundError("The run does not exist.")
    start = (page - 1) * page_size
    return AuditPage(tuple(events[start : start + page_size]), len(events), page, page_size)
