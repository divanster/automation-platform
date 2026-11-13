"""The extraction step and the failure reconciler, on top of ``apps.cases`` and ``apps.tasks``.

TRANSACTIONS. ``run_extraction`` uses two short transactions and NO transaction around the extraction:

1. T1 (short): lock the run row; if an outcome already exists, return it; otherwise move the run to
   ``extracting`` and read the stored text. Commit.
2. No transaction: extract, validate, canonicalize (``apps.intake.extraction``). Nothing is locked, so a
   slow extractor (a real model, later) never holds a lock or a connection transaction.
3. T2 (short, one ``atomic`` block): lock the run row again and re-check; insert revision 1 through
   ``apps.cases.services.record_revision`` (only for a valid draft), insert the ``ExtractionOutcome``, and
   move the run to ``needs_review``, and write the audit events (``revision.created`` for a valid draft and
   ``run.state_changed``, both by the ``system`` actor). All of it commits together or none of it does.

IDEMPOTENCY. At most one outcome exists per run: the database enforces it (one-to-one on the run) and T1
and T2 both check for it under the run lock. A repeated call (a retry, or a recovery after a crash after T2
but before the queue recorded the task as succeeded) returns the EXISTING outcome and writes nothing; it
never creates a second revision and never overwrites the first committed result. Two calls racing for
one run: the second waits on the run lock in T2, then finds the first one's outcome and discards its own work.

WHAT THE QUEUE'S LEASE FENCING DOES NOT COVER. The queue fences only its own bookkeeping
(``complete_task``). A worker whose lease expired can still reach T2; the outcome row and the run state
decide which result stands, not the lease. With the deterministic extractor both would be identical.

The task result is small and carries no business content: a status and identifiers.

Written against PostgreSQL; not run against it yet (see docs/development/request-intake-orchestration-slice.md).
"""

import uuid
from typing import NamedTuple

from django.db import transaction

from apps.audit import events as audit
from apps.audit.recorder import record_event
from apps.cases import services as case_services
from apps.cases.adapters import findings_to_json
from apps.cases.run_state import FailedStep, RunState
from apps.tasks.models import Task
from apps.tasks.policy import TaskState
from workflows.request_intake import RequestExtractor, ValidationLimits

from .draft_json import ambiguities_to_json, draft_to_json
from .errors import RequestSourceMissingError
from .extraction import build_extraction
from .models import ExtractionOutcome, RequestSource, StepTask
from .policy import OUTCOME_INVALID, OUTCOME_VALID, RESULT_DISCARDED, STEP_EXTRACT

RECONCILE_BATCH_LIMIT = 50
_EXTRACTABLE = (RunState.QUEUED, RunState.EXTRACTING)


class ExtractionSummary(NamedTuple):
    """The task result of one extraction call. ``status`` is ``valid``, ``invalid`` or ``discarded``.

    ``created`` is False when an existing outcome was returned. ``run_state`` is set only for a discarded
    call (the state that made the call a no-op).
    """

    status: str
    outcome_id: uuid.UUID | None
    revision_id: uuid.UUID | None
    created: bool
    run_state: str | None = None

    def as_task_result(self) -> dict:
        """A small JSON object for ``complete_task``: identifiers and a status, never business content.

        Identical for a first call and for every repeat, so a recovered task records the same result.
        """
        result = {"status": self.status}
        if self.outcome_id is not None:
            result["outcome_id"] = str(self.outcome_id)
            result["revision_id"] = None if self.revision_id is None else str(self.revision_id)
        if self.run_state is not None:
            result["run_state"] = self.run_state
        return result


def _summary_of(outcome: ExtractionOutcome, *, created: bool) -> ExtractionSummary:
    return ExtractionSummary(outcome.status, outcome.pk, outcome.revision_id, created)


def _discarded(run) -> ExtractionSummary:
    return ExtractionSummary(RESULT_DISCARDED, None, None, False, run_state=run.state)


def run_extraction(
    run_id: uuid.UUID,
    *,
    extractor: RequestExtractor | None = None,
    limits: ValidationLimits | None = None,
) -> ExtractionSummary:
    """Extract the run's stored text exactly once and persist the outcome. See the module docstring.

    Raises ``RecordNotFoundError`` for an unknown run and ``RequestSourceMissingError`` when the run
    has no stored text (neither can be fixed by retrying). Extractor, validation-safety and storage
    errors propagate; the queue classifies them by type.
    """
    # T1: claim the step under the run lock.
    with transaction.atomic():
        run = case_services.lock_run(run_id)
        existing = ExtractionOutcome.objects.filter(run_id=run.pk).first()
        if existing is not None:
            return _summary_of(existing, created=False)
        if RunState(run.state) not in _EXTRACTABLE:
            return _discarded(run)
        source = RequestSource.objects.filter(run_id=run.pk).first()
        if source is None:
            raise RequestSourceMissingError("The run has no stored request source.")
        case_services.transition_run(run.pk, to=RunState.EXTRACTING)
        case_id, text = run.case_id, source.text

    # No transaction is open from here until T2.
    decision = build_extraction(text, case_id=case_id, run_id=run.pk, revision_id=uuid.uuid4(), extractor=extractor, limits=limits)
    draft_json = draft_to_json(decision.draft)
    ambiguities_json = ambiguities_to_json(decision.ambiguities)
    findings_json = findings_to_json(decision.findings)

    # T2: commit the result under the run lock.
    with transaction.atomic():
        run = case_services.lock_run(run.pk)
        existing = ExtractionOutcome.objects.filter(run_id=run.pk).first()
        if existing is not None:
            return _summary_of(existing, created=False)  # another call committed first; its result stands
        if RunState(run.state) is not RunState.EXTRACTING:
            return _discarded(run)
        revision_row = case_services.record_revision(decision.revision) if decision.is_valid else None
        outcome = ExtractionOutcome.objects.create(
            run=run,
            status=OUTCOME_VALID if decision.is_valid else OUTCOME_INVALID,
            revision=revision_row,
            extractor_name=decision.extractor_name,
            extractor_version=decision.extractor_version,
            draft=draft_json,
            ambiguities=ambiguities_json,
            findings=findings_json,
        )
        case_services.transition_run(run.pk, to=RunState.NEEDS_REVIEW)
        ids = {"case_id": run.case_id, "run_id": run.pk}
        if revision_row is not None:
            record_event(
                audit.system_event(
                    audit.ACTION_REVISION_CREATED, audit.SUBJECT_REVISION, revision_row.pk, **ids,
                    revision_number=revision_row.revision_number, content_hash=revision_row.content_hash, origin="extraction",
                    blocking_count=0, warning_count=len(decision.revision.warnings),
                )
            )  # fmt: skip
        record_event(
            audit.system_event(
                audit.ACTION_RUN_STATE_CHANGED, audit.SUBJECT_RUN, run.pk, **ids,
                from_state=RunState.EXTRACTING.value, to_state=RunState.NEEDS_REVIEW.value, cause="extraction",
            )
        )  # fmt: skip
    return _summary_of(outcome, created=True)


class ReconcileResult(NamedTuple):
    failed: int  # runs moved to failed by this call


def reconcile_runs(*, limit: int = RECONCILE_BATCH_LIMIT) -> ReconcileResult:
    """Move runs whose extraction task failed for good to ``failed`` (``failed_step = "extract"``).

    A task that failed an attempt but was requeued for a retry is ``queued``, not ``failed``, so its run is
    left alone. Only a task in the terminal ``failed`` state counts. Idempotent and safe from several
    workers: each run is handled in its own short transaction under the run lock, the conditions are
    re-checked under the lock, and a run that is no longer ``queued`` or ``extracting`` (or already has an
    outcome) is skipped. The newest generation of the step decides. Only the step name is recorded on the
    run; the task's own safe ``failure_code`` stays on the task. No exception text exists here to expose.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    candidates = list(
        StepTask.objects.filter(
            step=STEP_EXTRACT,
            task__state=TaskState.FAILED.value,
            run__state__in=[state.value for state in _EXTRACTABLE],
        )
        .order_by("run_id")
        .values_list("run_id", flat=True)
        .distinct()[:limit]
    )
    failed = 0
    for run_id in candidates:
        with transaction.atomic():
            run = case_services.lock_run(run_id)
            if RunState(run.state) not in _EXTRACTABLE:
                continue
            if ExtractionOutcome.objects.filter(run_id=run.pk).exists():
                continue
            newest = StepTask.objects.filter(run_id=run.pk, step=STEP_EXTRACT).order_by("-generation").values_list("task_id", flat=True).first()
            if newest is None or Task.objects.filter(pk=newest).exclude(state=TaskState.FAILED.value).exists():
                continue
            case_services.transition_run(run.pk, to=RunState.FAILED, failed_step=FailedStep.EXTRACT.value)
            failed += 1
    return ReconcileResult(failed)


def reconcile_after_iteration() -> None:
    """The worker's after-iteration hook (see ``config.task_registry``): one bounded reconcile pass."""
    reconcile_runs()
