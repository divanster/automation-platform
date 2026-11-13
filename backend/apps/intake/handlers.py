"""The request-intake task handler: the bridge between the queue and ``run_extraction``.

A handler for ``apps.tasks`` is a ``TaskHandler(kind, validate, run, is_test_handler)``. This one is
NOT a test handler, and it is NOT pure: ``run`` reads and writes the database through
``apps.intake.services.run_extraction``, each time in its own short transaction. The worker
refuses to call a handler inside an open transaction and this handler opens none that spans the extraction.

The payload is ``{"run_id": "<uuid>"}`` and nothing else. The stored payload is validated again by the worker
before ``run`` is called. Failures are mapped to the queue's closed set of safe codes by TYPE only:

* permanent (``TaskPermanentError``, code ``rejected``): the run or its stored text does not exist, a value
  validates but cannot be serialized safely, or the draft holds a value the JSON form cannot store. Retrying
  cannot change any of these.
* everything else (the database, the extractor, an unexpected error) propagates and the queue treats it as
  retryable within the attempt limit (``handler_error``).

Nothing from the request text, the draft, or an exception message is put into an error, a log line, or the
task result. The result is ``ExtractionSummary.as_task_result()``: a status and identifiers.
"""

import uuid

from apps.cases.errors import RecordNotFoundError
from apps.tasks.handlers import TaskHandler
from apps.tasks.policy import FailureCode, InvalidPayloadError, TaskPermanentError
from workflows.request_intake import CanonicalizationError

from .errors import RequestSourceMissingError, UnsupportedDraftError
from .policy import KIND_EXTRACT, InvalidTaskPayloadError, validate_extraction_payload
from .services import run_extraction


def validate_payload(payload) -> dict:
    try:
        return validate_extraction_payload(payload)
    except InvalidTaskPayloadError as error:
        raise InvalidPayloadError(str(error)) from None


def run_extract(payload: dict) -> dict:
    run_id = uuid.UUID(payload["run_id"])
    try:
        summary = run_extraction(run_id)
    except (RecordNotFoundError, RequestSourceMissingError, CanonicalizationError, UnsupportedDraftError):
        raise TaskPermanentError(FailureCode.REJECTED) from None
    return summary.as_task_result()


EXTRACT_HANDLER = TaskHandler(kind=KIND_EXTRACT, validate=validate_payload, run=run_extract, is_test_handler=False)
