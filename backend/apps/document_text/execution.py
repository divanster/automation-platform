"""The worker side of text extraction: carry out one queued ``TextExtraction`` exactly once and record its result, and close the requests whose task failed for good. Written against PostgreSQL; not run
against it yet (docs/development/document-text-extraction.md section on validation).

STEPS OF ONE RUN (``run_text_extraction``), with NO transaction around the slow part:

1. T1 (short): lock the request row. If it already has a result, return that result (a retry, or a recovery after a crash between the result and the queue's bookkeeping, repeats nothing). Otherwise read the
   document's metadata and re-check that the requester may still read the document (``apps.documents.access``, the DOWNLOAD rule, on a freshly loaded user). Commit.
2. No transaction: open the stored object through the injected store (size checked against the metadata), read it with a size bound, verify its SHA-256 against the recorded one and run the isolated extractor
   (``apps.document_text.isolation``). The original is only read; it is never modified, moved or copied.
3. T2 (short, one ``atomic`` block): lock the request row again, re-check for a result, and insert the result, its pages and the ``document.text_extracted`` audit event together, or none of it.

IDEMPOTENCY AND RETRIES. ``TextExtractionResult.extraction`` is one-to-one, and both T1 and T2 look for the result under the request's row lock, so there is never a second result and a finished result is
never replaced. Two runs racing for one request (a stale worker whose lease expired and the worker that took over): the second waits on the lock in T2, finds the first one's result and discards its own work.
The queue fences only its own bookkeeping, so a stale worker can still reach T2; the one-to-one result decides which outcome stands, as in Request Intake.

FAILURE KINDS. A problem with the DOCUMENT or the requester's right (not authorized, object missing, hash mismatch, too large, encrypted, malformed, a limit, a timeout) is recorded as a ``failed`` result and the
task SUCCEEDS: the work was done and its outcome is stored. A problem OUTSIDE the document (the storage cannot be used, the isolated process cannot start) raises, the handler maps it to a retryable task
failure, and when the attempts are used up ``reconcile_failed_extractions`` records ``extraction_unavailable``.

NOTHING from the document or the extracted text is logged: identifiers and a status only.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable
from typing import NamedTuple

from django.db import transaction

from apps.accounts.authz import load_actor
from apps.accounts.errors import AuthorizationError
from apps.audit.events import ACTION_DOCUMENT_TEXT_EXTRACTED, SUBJECT_DOCUMENT, system_event
from apps.audit.recorder import record_event
from apps.documents.access import Action as DocumentAction
from apps.documents.access import access_problem
from apps.documents.errors import CODE_OBJECT_MISSING, CODE_UNAVAILABLE, DocumentStorageError
from apps.tasks.policy import TaskState

from .errors import ExtractionRecordMissingError
from .extractor_identity import installed_version
from .models import TextExtraction, TextExtractionPage, TextExtractionResult
from .rules import (
    ERR_DOCUMENT_MISSING,
    ERR_DOCUMENT_TOO_LARGE,
    ERR_INTEGRITY_MISMATCH,
    ERR_NOT_AUTHORIZED,
    ERR_UNAVAILABLE,
    ExtractionLimits,
    ExtractionOutput,
    failed_output,
)

logger = logging.getLogger(__name__)

RECONCILE_BATCH_LIMIT = 50
_READ_CHUNK = 64 * 1024
_PAGE_BATCH = 100


class ExtractionRuntime(NamedTuple):
    """What a worker needs to extract: how to get the document store (it may raise ``DocumentStorageError``), the limits and the extractor, a callable ``(bytes, ExtractionLimits) -> ExtractionOutput``."""

    store_factory: Callable
    limits: ExtractionLimits
    extractor: Callable


class ExtractionSummary(NamedTuple):
    extraction_id: uuid.UUID
    status: str
    error_code: str
    page_count: int | None
    created: bool  # False when an existing result was returned

    def as_task_result(self) -> dict:
        """A small JSON object for the queue: the status, the code and the page count, never any text. Identical for a first run and for every repeat."""
        return {"status": self.status, "error_code": self.error_code or None, "page_count": self.page_count}


def _summary(result: TextExtractionResult, *, created: bool) -> ExtractionSummary:
    return ExtractionSummary(result.extraction_id, result.status, result.error_code, result.page_count, created)


class _Job(NamedTuple):
    extraction_id: uuid.UUID
    document_id: uuid.UUID
    storage_key: str
    sha256: str
    size_bytes: int
    authorized: bool


def _still_authorized(user, document) -> bool:
    try:
        actor = load_actor(user)
    except AuthorizationError:
        return False
    return access_problem(DocumentAction.DOWNLOAD, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=document.owner_id) is None


def _begin(extraction_id: uuid.UUID):
    with transaction.atomic():
        extraction = TextExtraction.objects.select_for_update(of=("self",)).select_related("document", "requested_by").filter(pk=extraction_id).first()
        if extraction is None:
            raise ExtractionRecordMissingError()
        existing = TextExtractionResult.objects.filter(extraction_id=extraction.pk).first()
        if existing is not None:
            return _summary(existing, created=False)
        document = extraction.document
        return _Job(extraction.pk, document.pk, document.storage_key, document.sha256, document.size_bytes, _still_authorized(extraction.requested_by, document))


def _read_object(job: _Job, runtime: ExtractionRuntime, version: str):
    """The document's bytes, or the failed output that says why they cannot be extracted. A storage that cannot be used raises ``DocumentStorageError`` (retryable)."""
    if job.size_bytes > runtime.limits.max_bytes:
        return None, failed_output(ERR_DOCUMENT_TOO_LARGE, extractor_version=version)
    store = runtime.store_factory()
    try:
        stream = store.open(job.storage_key, job.size_bytes)
    except DocumentStorageError as error:
        if error.code == CODE_OBJECT_MISSING:
            return None, failed_output(ERR_DOCUMENT_MISSING, extractor_version=version)
        raise
    digest, data = hashlib.sha256(), bytearray()
    try:
        for chunk in stream:
            if len(data) + len(chunk) > job.size_bytes:
                data += chunk
                break
            digest.update(chunk)
            data += chunk
    except OSError:
        raise DocumentStorageError(CODE_UNAVAILABLE) from None
    finally:
        stream.close()
    if len(data) != job.size_bytes:
        return None, failed_output(ERR_DOCUMENT_MISSING, extractor_version=version)
    if digest.hexdigest() != job.sha256:
        return None, failed_output(ERR_INTEGRITY_MISMATCH, extractor_version=version)
    return data, None


def _store_result(extraction: TextExtraction, output: ExtractionOutput) -> TextExtractionResult:
    """Insert the result, its pages and the audit event (inside the caller's transaction)."""
    document = extraction.document
    result = TextExtractionResult.objects.create(
        extraction=extraction,
        status=output.status,
        error_code=output.error_code,
        extractor_name=output.extractor_name,
        extractor_version=output.extractor_version,
        page_count=output.page_count,
        char_count=output.char_count,
        source_sha256=document.sha256,
        source_size_bytes=document.size_bytes,
        text_sha256=output.text_sha256,
    )
    TextExtractionPage.objects.bulk_create(
        [TextExtractionPage(result=result, page_number=p.page_number, status=p.status, error_code=p.error_code, text=p.text, char_count=p.char_count) for p in output.pages],
        batch_size=_PAGE_BATCH,
    )
    record_event(
        system_event(
            ACTION_DOCUMENT_TEXT_EXTRACTED,
            SUBJECT_DOCUMENT,
            document.pk,
            extraction_id=str(extraction.pk),
            status=output.status,
            error_code=output.error_code or None,
            page_count=output.page_count,
            char_count=output.char_count,
            content_hash=output.text_sha256,
            extractor_version=output.extractor_version,
        )
    )
    return result


def _commit(extraction_id: uuid.UUID, output: ExtractionOutput) -> ExtractionSummary:
    with transaction.atomic():
        extraction = TextExtraction.objects.select_for_update(of=("self",)).select_related("document").get(pk=extraction_id)
        existing = TextExtractionResult.objects.filter(extraction_id=extraction.pk).first()
        if existing is not None:
            return _summary(existing, created=False)  # another run committed first; its result stands
        result = _store_result(extraction, output)
    logger.info("Text extraction finished: extraction=%s document=%s status=%s", extraction.pk, extraction.document_id, result.status)
    return _summary(result, created=True)


def run_text_extraction(extraction_id: uuid.UUID, runtime: ExtractionRuntime) -> ExtractionSummary:
    """Extract the document of one request exactly once and persist the result (see the module docstring).

    Raises ``ExtractionRecordMissingError`` for an unknown request (permanent). ``DocumentStorageError`` and ``apps.document_text.isolation.ExtractorUnavailableError`` propagate (the handler makes them
    retryable); everything about the document itself is recorded as a result.
    """
    job = _begin(extraction_id)
    if isinstance(job, ExtractionSummary):
        return job
    version = installed_version()
    if not job.authorized:
        logger.warning("Text extraction refused: the requester may no longer read the document (extraction=%s document=%s)", job.extraction_id, job.document_id)
        output = failed_output(ERR_NOT_AUTHORIZED, extractor_version=version)
    else:
        data, output = _read_object(job, runtime, version)
        if output is None:
            output = runtime.extractor(data, runtime.limits)
    return _commit(job.extraction_id, output)


def reconcile_failed_extractions(*, limit: int = RECONCILE_BATCH_LIMIT) -> int:
    """Record ``extraction_unavailable`` for requests whose task failed for good without a result (the attempts ran out on a problem outside the document). Returns the number closed.

    Idempotent and safe from several workers: each request is handled in its own short transaction under its row lock, and the conditions (no result, task failed) are re-checked under it.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    candidates = list(
        TextExtraction.objects.filter(task__state=TaskState.FAILED.value, result__isnull=True).order_by("requested_at", "id").values_list("pk", flat=True)[:limit]
    )
    closed, version = 0, installed_version()
    for extraction_id in candidates:
        with transaction.atomic():
            extraction = TextExtraction.objects.select_for_update(of=("self",)).select_related("document", "task").filter(pk=extraction_id).first()
            if extraction is None or extraction.task.state != TaskState.FAILED.value:
                continue
            if TextExtractionResult.objects.filter(extraction_id=extraction.pk).exists():
                continue
            _store_result(extraction, failed_output(ERR_UNAVAILABLE, extractor_version=version))
            closed += 1
    return closed


def reconcile_after_iteration() -> None:
    """The worker's maintenance hook (see ``config.task_registry``): one bounded reconcile pass."""
    reconcile_failed_extractions()
