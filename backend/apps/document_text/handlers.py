"""The text extraction task handler: the bridge between the queue (``apps.tasks``) and ``execution.run_text_extraction``.

A ``TaskHandler(kind, validate, run, is_test_handler)``. It is NOT pure: ``run`` reads and writes the database and the document storage, each time in short transactions of its own (the worker refuses to call a
handler inside an open transaction, and this one opens none that spans the extraction). The payload is ``{"extraction_id": "<uuid>"}`` and nothing else; the worker validates the stored payload again before ``run``.

THE RUNTIME (store, limits, isolated extractor) is installed once by the worker entry point from the composition root (``config/document_extraction.py``); this module imports no composition code and reads no
environment. Without one the handler fails permanently (``rejected``) instead of guessing.

FAILURES are mapped to the queue's closed set of codes by TYPE only, with no new code: an unknown request or a missing runtime is permanent (``rejected``); a storage that cannot be used, or an isolated
process that cannot start, is retryable (``temporary_failure``) within the task's attempt limit. Everything about the document itself is a stored ``failed`` result, not an exception. Nothing from the document is
put into an error, a log line or the task result.
"""

import logging
import uuid

from apps.documents.errors import DocumentStorageError
from apps.tasks.handlers import TaskHandler
from apps.tasks.policy import FailureCode, InvalidPayloadError, TaskPermanentError, TaskRetryableError

from .errors import ExtractionRecordMissingError
from .execution import ExtractionRuntime, run_text_extraction
from .isolation import ExtractorUnavailableError
from .rules import KIND_EXTRACT_TEXT

logger = logging.getLogger(__name__)

_runtime: ExtractionRuntime | None = None


def install_runtime(runtime: ExtractionRuntime | None) -> None:
    """Install (or, with ``None``, remove) the extraction runtime of this process. Call it once, from the worker entry point's composition root."""
    global _runtime
    if runtime is not None and not isinstance(runtime, ExtractionRuntime):
        raise TypeError("the runtime must be an apps.document_text.execution.ExtractionRuntime or None")
    _runtime = runtime


def validate_payload(payload) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"extraction_id"}:
        raise InvalidPayloadError("payload must be an object with exactly an extraction_id")
    value = payload["extraction_id"]
    try:
        canonical = isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        canonical = False
    if not canonical:
        raise InvalidPayloadError("extraction_id must be a canonical UUID")
    return {"extraction_id": value}


def payload_for(extraction_id: uuid.UUID) -> dict:
    return {"extraction_id": str(extraction_id)}


def idempotency_key_for(extraction_id: uuid.UUID) -> str:
    return f"{KIND_EXTRACT_TEXT}:{extraction_id}"


def run_extract(payload: dict) -> dict:
    extraction_id = uuid.UUID(payload["extraction_id"])
    runtime = _runtime
    if runtime is None:
        logger.error("documents.extract_text extraction=%s: no extraction runtime is installed in this process", extraction_id)
        raise TaskPermanentError(FailureCode.REJECTED) from None
    try:
        summary = run_text_extraction(extraction_id, runtime)
    except ExtractionRecordMissingError:
        raise TaskPermanentError(FailureCode.REJECTED) from None
    except DocumentStorageError as error:
        logger.warning("documents.extract_text extraction=%s: the document storage cannot be used code=%s", extraction_id, error.code)
        raise TaskRetryableError(FailureCode.TEMPORARY_FAILURE) from None
    except ExtractorUnavailableError:
        logger.warning("documents.extract_text extraction=%s: the isolated extractor is unavailable", extraction_id)
        raise TaskRetryableError(FailureCode.TEMPORARY_FAILURE) from None
    return summary.as_task_result()


EXTRACT_TEXT_HANDLER = TaskHandler(kind=KIND_EXTRACT_TEXT, validate=validate_payload, run=run_extract, is_test_handler=False)
