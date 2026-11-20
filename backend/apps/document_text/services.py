"""The text extraction services: authorized request, list, status and read of the extracted text, each with its audit event.

WHO MAY DO WHAT. Extracted text is the content of the document, so every operation here needs exactly what DOWNLOADING that document needs (``apps.documents.access`` ``Action.DOWNLOAD``: ``download_own_document`` for
the uploader, ``download_any_document`` for any document, a superuser passes both): the PERMISSION part first, before any record is looked up (``missing_permission``), the SCOPE part after (``out_of_scope``: the API
answers it with the same 404 as an unknown record). The Auditor, who may view metadata only, can neither request nor read an extraction. No new permission exists, so ``setup_roles`` is unchanged. The user is
reloaded from the database on every call (``apps.accounts.authz.load_actor``). A caller supplies no identity.

REQUEST. One short transaction: lock the DOCUMENT row (so concurrent requests for it are serialized), then either return an existing extraction that makes a new one pointless or create a new one:

* an extraction with no result yet (queued or running) is returned as it is, so a double click or a retried request queues nothing twice;
* an extraction whose result is ``extracted``, ``partial`` or ``empty`` and was made by the INSTALLED extractor version is returned as it is (the same bytes and the same library give the same text; changing the
  limits does not re-extract);
* otherwise (no extraction yet, the latest one ``failed``, or made by another library version) a new ``TextExtraction`` is created, its task is queued through ``apps.tasks.services.enqueue_task`` (idempotency
  key ``documents.extract_text:<extraction id>``, payload ``{"extraction_id": ...}``, never any text) and ``document.text_requested`` is recorded, all in the same transaction. The worker cannot see the task before
  the commit. Earlier extractions are history and are never changed.

READ. ``read_extracted_text`` returns the pages of a finished extraction. The ``document.text_read`` event is written in its own transaction BEFORE the text is returned, so a failed audit write returns nothing,
as for a download. An extraction without a result is ``ExtractionNotReadyError``.

LOGGING. Identifiers only (extraction id, document id, actor public id), never text, a file name, a hash or a path.
"""

import logging
import uuid
from datetime import datetime
from typing import NamedTuple

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from apps.accounts.authz import load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.audit.events import ACTION_DOCUMENT_TEXT_READ, ACTION_DOCUMENT_TEXT_REQUESTED, SUBJECT_DOCUMENT, user_event
from apps.audit.recorder import record_event
from apps.documents.access import Action as DocumentAction
from apps.documents.access import permission_problem, scope_problem
from apps.documents.errors import DocumentNotFoundError
from apps.documents.models import Document
from apps.tasks.policy import TaskState
from apps.tasks.services import enqueue_task

from .errors import ExtractionNotReadyError
from .extractor_identity import installed_version
from .handlers import idempotency_key_for, payload_for
from .models import TextExtraction, TextExtractionPage
from .registry import DOCUMENT_TEXT_REGISTRY
from .rules import (
    ERR_UNAVAILABLE,
    EXTRACTOR_NAME,
    KIND_EXTRACT_TEXT,
    RESULT_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    source_reference,
)

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
_REUSABLE_RESULTS = ("extracted", "partial", "empty")


class ExtractionItem(NamedTuple):
    """One extraction's status and facts: NO text. ``status`` is ``queued``, ``running``, or the result's ``extracted``, ``partial``, ``empty`` or ``failed``."""

    id: uuid.UUID
    document_id: uuid.UUID
    requested_at: datetime
    requested_by: uuid.UUID  # the requester's public_id
    status: str
    error_code: str
    extractor_name: str | None
    extractor_version: str | None
    page_count: int | None
    char_count: int | None
    source_sha256: str | None
    source_size_bytes: int | None
    text_sha256: str | None
    completed_at: datetime | None


class ExtractionPage(NamedTuple):
    items: tuple
    count: int
    page: int
    page_size: int


class PageItem(NamedTuple):
    page_number: int
    status: str
    error_code: str
    text: str
    char_count: int
    source_reference: str


class ExtractedText(NamedTuple):
    extraction: ExtractionItem
    pages: tuple


class RequestOutcome(NamedTuple):
    item: ExtractionItem
    created: bool  # False when an existing extraction was returned


def _authorize(user, name: str):
    actor = load_actor(user)
    problem = permission_problem(DocumentAction.DOWNLOAD, is_superuser=actor.is_superuser, permissions=actor.permissions)
    if problem is not None:
        log_refusal(f"document_text.{name}", problem, actor.public_id)
        raise AuthorizationError(problem)
    return actor


def _require_scope(actor, name: str, document) -> None:
    problem = scope_problem(DocumentAction.DOWNLOAD, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=document.owner_id)
    if problem is not None:
        log_refusal(f"document_text.{name}", problem, actor.public_id)
        raise AuthorizationError(problem)


def _document_in_scope(actor, name: str, document_id, *, lock: bool = False) -> Document:
    documents = Document.objects.select_for_update() if lock else Document.objects
    document = documents.filter(pk=document_id).first()
    if document is None:
        raise DocumentNotFoundError()
    _require_scope(actor, name, document)
    return document


def _result_of(extraction):
    try:
        return extraction.result
    except ObjectDoesNotExist:
        return None


def _item(extraction: TextExtraction) -> ExtractionItem:
    """The read model of one extraction (``select_related("task", "requested_by", "result")`` avoids queries)."""
    result = _result_of(extraction)
    common = dict(id=extraction.pk, document_id=extraction.document_id, requested_at=extraction.requested_at, requested_by=extraction.requested_by.public_id)
    if result is not None:
        return ExtractionItem(
            **common,
            status=result.status,
            error_code=result.error_code,
            extractor_name=result.extractor_name,
            extractor_version=result.extractor_version,
            page_count=result.page_count,
            char_count=result.char_count,
            source_sha256=result.source_sha256,
            source_size_bytes=result.source_size_bytes,
            text_sha256=result.text_sha256,
            completed_at=result.completed_at,
        )
    state = extraction.task.state
    if state == TaskState.QUEUED.value:
        status, code = STATE_QUEUED, ""
    elif state == TaskState.RUNNING.value:
        status, code = STATE_RUNNING, ""
    else:  # the task failed for good and the reconciler has not recorded the result yet
        status, code = RESULT_FAILED, ERR_UNAVAILABLE
    return ExtractionItem(**common, status=status, error_code=code, extractor_name=None, extractor_version=None, page_count=None, char_count=None, source_sha256=None, source_size_bytes=None, text_sha256=None, completed_at=None)


def _extractions(document_id):
    return TextExtraction.objects.select_related("task", "requested_by", "result").filter(document_id=document_id)


def _is_reusable(extraction: TextExtraction, version: str) -> bool:
    result = _result_of(extraction)
    if result is None:
        return extraction.task.state in (TaskState.QUEUED.value, TaskState.RUNNING.value)
    return result.status in _REUSABLE_RESULTS and result.extractor_name == EXTRACTOR_NAME and result.extractor_version == version


def request_text_extraction(*, user, document_id, extractor_version: str | None = None) -> RequestOutcome:
    """Request the text extraction of a document the caller may download. See the module docstring for when an existing extraction is returned instead of a new one.

    ``extractor_version`` is the installed library's version (read from package metadata when omitted). Raises ``AuthorizationError`` or ``DocumentNotFoundError``; nothing is queued in either case.
    """
    actor = _authorize(user, "request")
    version = installed_version() if extractor_version is None else extractor_version
    with transaction.atomic():
        document = _document_in_scope(actor, "request", document_id, lock=True)
        latest = _extractions(document.pk).order_by("-requested_at", "-id").first()
        if latest is not None and _is_reusable(latest, version):
            return RequestOutcome(_item(latest), False)
        extraction_id = uuid.uuid4()
        task = enqueue_task(kind=KIND_EXTRACT_TEXT, payload=payload_for(extraction_id), idempotency_key=idempotency_key_for(extraction_id), registry=DOCUMENT_TEXT_REGISTRY).task
        extraction = TextExtraction.objects.create(id=extraction_id, document=document, requested_by=actor.user, task=task)
        record_event(user_event(actor.public_id, ACTION_DOCUMENT_TEXT_REQUESTED, SUBJECT_DOCUMENT, document.pk, extraction_id=str(extraction.pk)))
        created = _extractions(document.pk).get(pk=extraction.pk)
    logger.info("Text extraction requested: extraction=%s document=%s actor=%s", extraction.pk, document.pk, actor.public_id)
    return RequestOutcome(_item(created), True)


def _check_page(page, page_size) -> None:
    for name, value in (("page", page), ("page_size", page_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be an integer of at least 1")
    if page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be at most {MAX_PAGE_SIZE}")


def list_text_extractions(*, user, document_id, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> ExtractionPage:
    """One page of a document's extractions, newest first (no text)."""
    _check_page(page, page_size)
    actor = _authorize(user, "list")
    document = _document_in_scope(actor, "list", document_id)
    extractions = _extractions(document.pk).order_by("-requested_at", "-id")
    offset = (page - 1) * page_size
    return ExtractionPage(tuple(_item(e) for e in extractions[offset : offset + page_size]), extractions.count(), page, page_size)


def _get(document_id, extraction_id) -> TextExtraction:
    extraction = _extractions(document_id).filter(pk=extraction_id).first()
    if extraction is None:
        raise DocumentNotFoundError()
    return extraction


def get_text_extraction(*, user, document_id, extraction_id) -> ExtractionItem:
    """The status and facts of one extraction (no text). ``DocumentNotFoundError`` for an unknown document or extraction (or one of another document)."""
    actor = _authorize(user, "view")
    document = _document_in_scope(actor, "view", document_id)
    return _item(_get(document.pk, extraction_id))


def read_extracted_text(*, user, document_id, extraction_id) -> ExtractedText:
    """The pages of a finished extraction, and a ``document.text_read`` event written first. ``ExtractionNotReadyError`` while the extraction has no result."""
    actor = _authorize(user, "read")
    document = _document_in_scope(actor, "read", document_id)
    extraction = _get(document.pk, extraction_id)
    result = _result_of(extraction)
    if result is None:
        raise ExtractionNotReadyError()
    rows = tuple(TextExtractionPage.objects.filter(result_id=result.pk).order_by("page_number"))
    pages = tuple(
        PageItem(row.page_number, row.status, row.error_code, row.text, row.char_count, source_reference(document.pk, result.source_sha256, row.page_number)) for row in rows
    )
    with transaction.atomic():
        record_event(
            user_event(
                actor.public_id,
                ACTION_DOCUMENT_TEXT_READ,
                SUBJECT_DOCUMENT,
                document.pk,
                extraction_id=str(extraction.pk),
                page_count=result.page_count,
                char_count=result.char_count,
                by_owner=document.owner_id == actor.pk,
            )
        )
    logger.info("Extracted text read: extraction=%s document=%s actor=%s", extraction.pk, document.pk, actor.public_id)
    return ExtractedText(_item(extraction), pages)
