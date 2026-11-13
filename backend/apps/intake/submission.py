"""``submit_request``: text in, a queued run out. One atomic transaction.

What one call creates, all in ONE ``transaction.atomic()`` block (all of it commits, or none of it):

1. a ``Case`` with a generated reference (``REQ-YYYYMMDD-XXXXXXXX``),
2. a ``WorkflowRun`` in state ``queued`` (the existing ``apps.cases`` run, with no revision yet),
3. an immutable ``RequestSource`` holding the exact text, its SHA-256 and its size,
4. ONE extraction task, through the existing ``apps.tasks.services.enqueue_task`` (idempotency key
   ``request_intake.extract:<run id>:1``; payload ``{"run_id": "<uuid>"}``, never the text),
5. the ``StepTask`` row linking that run and step to that task.

The worker cannot see the task before the transaction commits, so it can never run an extraction whose
source text or run is missing. If any step fails (including the enqueue), nothing is stored.

``client_request_id`` (optional) makes a repeated submit by the same user safe: the same id with the same text
returns the original run and creates nothing (``created=False``); the same id with different text raises
``ClientRequestConflictError``. Two concurrent submits with one id create one run: the database unique
constraint decides, and the loser reads the winner's row.

Authentication and authorization are NOT implemented: ``submitted_by`` is recorded as the provenance of the text and
nothing checks whether that user may submit. There is no API.
"""

import hashlib
import uuid
from datetime import datetime
from typing import NamedTuple

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.cases.models import Case, WorkflowRun
from apps.cases.services import create_case, create_run
from apps.tasks.models import Task
from apps.tasks.services import enqueue_task

from .errors import ClientRequestConflictError
from .models import RequestSource, StepTask
from .policy import (
    FIRST_GENERATION,
    KIND_EXTRACT,
    STEP_EXTRACT,
    extraction_idempotency_key,
    extraction_payload,
    new_case_reference,
    validate_request_text,
)
from .registry import PRODUCTION_REGISTRY

_REFERENCE_ATTEMPTS = 3


class SubmissionResult(NamedTuple):
    case: Case
    run: WorkflowRun
    source: RequestSource
    task: Task
    created: bool  # False when an earlier submit with the same client request id was returned


def _replay(submitted_by, client_request_id, text_sha256) -> SubmissionResult | None:
    source = RequestSource.objects.select_related("run__case").filter(submitted_by=submitted_by, client_request_id=client_request_id).first()
    if source is None:
        return None
    if source.text_sha256 != text_sha256:
        raise ClientRequestConflictError("The client request id was already used for a different request text.")
    step = StepTask.objects.select_related("task").get(run_id=source.run_id, step=STEP_EXTRACT, generation=FIRST_GENERATION)
    return SubmissionResult(source.run.case, source.run, source, step.task, False)


def _create_case_with_reference(now: datetime) -> Case:
    """Create the case; a (very unlikely) reference collision is retried with a new random part."""
    for attempt in range(_REFERENCE_ATTEMPTS):
        try:
            with transaction.atomic():  # a savepoint: a collision must not poison the outer transaction
                return create_case(reference=new_case_reference(now, uuid.uuid4().hex))
        except IntegrityError:
            if attempt == _REFERENCE_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def submit_request(*, text: str, submitted_by, client_request_id: uuid.UUID | None = None, now: datetime | None = None) -> SubmissionResult:
    """Validate ``text`` and create the case, run, source, extraction task and link in one transaction.

    Raises ``RequestTextError`` (nothing stored) for text that may not be stored, ``ClientRequestConflictError``
    for a reused client request id with different text, and ``apps.tasks`` errors if the enqueue is refused.
    ``now`` is for tests (the date part of the case reference).
    """
    validate_request_text(text)
    if client_request_id is not None and not isinstance(client_request_id, uuid.UUID):
        raise TypeError("client_request_id must be a uuid.UUID or None")
    encoded = text.encode("utf-8")
    text_sha256 = hashlib.sha256(encoded).hexdigest()
    moment = timezone.now() if now is None else now

    if client_request_id is not None:
        replayed = _replay(submitted_by, client_request_id, text_sha256)
        if replayed is not None:
            return replayed
    try:
        with transaction.atomic():
            case = _create_case_with_reference(moment)
            run = create_run(case_id=case.pk)
            source = RequestSource.objects.create(
                run=run,
                submitted_by=submitted_by,
                text=text,
                text_sha256=text_sha256,
                byte_length=len(encoded),
                client_request_id=client_request_id,
            )
            task = enqueue_task(
                kind=KIND_EXTRACT,
                payload=extraction_payload(run.pk),
                idempotency_key=extraction_idempotency_key(run.pk, FIRST_GENERATION),
                registry=PRODUCTION_REGISTRY,
            ).task
            StepTask.objects.create(run=run, step=STEP_EXTRACT, generation=FIRST_GENERATION, task=task)
    except IntegrityError:
        if client_request_id is None:
            raise
        # Lost a race for the same client request id: the winner's rows are committed by now.
        replayed = _replay(submitted_by, client_request_id, text_sha256)
        if replayed is None:
            raise
        return replayed
    return SubmissionResult(case, run, source, task, True)
