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
* AI extraction (AI-2a, only when the worker installed an AI extractor source, see below): ``AIExtractionError`` is mapped by its ``retryable``
  flag, which comes from the AI Gateway: a retryable provider failure (unavailable, timeout, rate limit) is ``TaskRetryableError`` (the queue's
  ``temporary_failure``, bounded by the task's attempt limit and back-off); every other AI failure (policy, authentication, configuration, an answer
  that is not valid structured output, an answer Request Intake refuses) is ``TaskPermanentError`` (``rejected``). No new failure code exists: the closed
  set of the queue is unchanged. A permanent failure fails the task, and the existing reconciler then moves the run to ``failed``. Nothing was written
  by a failed call: the revision and the outcome are only created in the second transaction of ``run_extraction``, after a candidate exists.
* everything else (the database, the extractor, an unexpected error) propagates and the queue treats it as
  retryable within the attempt limit (``handler_error``).

THE EXTRACTOR SOURCE. By default nothing is installed and the handler calls ``run_extraction(run_id)``: the deterministic fixture parser, exactly as
before. The worker entry point installs a source (``install_extractor_source``, from the composition root ``config/task_registry.py``) that returns an
AI extractor only when AI mode was explicitly enabled; the source returns ``None`` for the deterministic mode. This module imports nothing from the AI
Gateway and reads no environment. A failure is logged with the run id and the stable code and reason tokens, never a message.

Nothing from the request text, the draft, or an exception message is put into an error, a log line, or the
task result. The result is ``ExtractionSummary.as_task_result()``: a status and identifiers.
"""

import logging
import uuid
from collections.abc import Callable

from apps.cases.errors import RecordNotFoundError
from apps.tasks.handlers import TaskHandler
from apps.tasks.policy import FailureCode, InvalidPayloadError, TaskPermanentError, TaskRetryableError
from workflows.request_intake import CanonicalizationError

from .errors import AIExtractionError, ExtractionConfigurationError, RequestSourceMissingError, UnsupportedDraftError
from .policy import KIND_EXTRACT, InvalidTaskPayloadError, validate_extraction_payload
from .services import run_extraction


logger = logging.getLogger(__name__)

# The installed extractor source: a callable with no arguments that returns the extractor to use, or None for the deterministic default.
_extractor_source: Callable[[], object] | None = None


def install_extractor_source(source: Callable[[], object] | None) -> None:
    """Install (or, with ``None``, remove) the extractor source for this process. Call it once, from the worker entry point's composition root."""
    global _extractor_source
    if source is not None and not callable(source):
        raise TypeError("the extractor source must be callable or None")
    _extractor_source = source


def validate_payload(payload) -> dict:
    try:
        return validate_extraction_payload(payload)
    except InvalidTaskPayloadError as error:
        raise InvalidPayloadError(str(error)) from None


def run_extract(payload: dict) -> dict:
    run_id = uuid.UUID(payload["run_id"])
    try:
        extractor = None if _extractor_source is None else _extractor_source()
        summary = run_extraction(run_id) if extractor is None else run_extraction(run_id, extractor=extractor)
    except (RecordNotFoundError, RequestSourceMissingError, CanonicalizationError, UnsupportedDraftError):
        raise TaskPermanentError(FailureCode.REJECTED) from None
    except ExtractionConfigurationError:
        logger.error("request_intake.extract run=%s: the extraction configuration is invalid", run_id)
        raise TaskPermanentError(FailureCode.REJECTED) from None
    except AIExtractionError as error:
        logger.warning("request_intake.extract run=%s: AI extraction failed code=%s reason=%s retryable=%s", run_id, error.code, error.reason, error.retryable)
        if error.retryable:
            raise TaskRetryableError(FailureCode.TEMPORARY_FAILURE) from None
        raise TaskPermanentError(FailureCode.REJECTED) from None
    return summary.as_task_result()


EXTRACT_HANDLER = TaskHandler(kind=KIND_EXTRACT, validate=validate_payload, run=run_extract, is_test_handler=False)
