"""Persistence for PDF text extraction (DOC-2): ``TextExtraction`` (one request), ``TextExtractionResult`` (its one outcome) and ``TextExtractionPage`` (the text of one page).

APPEND ONLY. All three are ``apps.cases.models.ImmutableRecord``: a row is inserted once and is never updated, overwritten or deleted through the Django ORM (``save()`` on an existing row, ``update()``,
``bulk_update()``, ``delete()`` and an upsert are refused; foreign keys are ``PROTECT``). A new extraction of the same document is a NEW ``TextExtraction``; the earlier ones and their pages stay as history.
This is ORM-level only: there is no trigger and no revoked privilege, so raw SQL, a database administrator and a migration are not stopped. The task queue's own ``Task`` row (which this slice does not
change) is the only mutable part of the process.

ONE RESULT PER REQUEST. ``TextExtractionResult.extraction`` is one-to-one: the database allows at most one outcome per request, which is what makes a retried or concurrent run safe (the loser finds the
winner's row, see ``apps.document_text.services``). A request without a result is pending (its task is queued or running) or, if its task failed for good, waits for the reconciler to record
``extraction_unavailable``.

WHAT THE ROWS HOLD. The text of a PDF (user-provided data, possibly personal): ``TextExtractionPage.text``. It is never logged, never audited and returned only to a caller who may download the document. The
document's bytes are not copied. ``source_sha256`` is the document's recorded SHA-256; for a result that read the file it was verified against the bytes that were read.

The constraints below were written without a PostgreSQL server in the development VM and are covered by PostgreSQL tests that have not been run there.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.cases.models import ImmutableRecord
from apps.documents.models import Document
from apps.tasks.models import Task

from .rules import (
    ERROR_CODE_SHAPE,
    PAGE_FAILED,
    PAGE_STATUSES,
    RESULT_EXTRACTED,
    RESULT_FAILED,
    RESULT_STATUSES,
)

_HASH = r"^[0-9a-f]{64}$"


class TextExtraction(ImmutableRecord):
    """One request to extract the text of one document, and the queue task that carries it out."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", help_text="Who asked. Re-checked when the worker runs.")
    task = models.OneToOneField(Task, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["document", "-requested_at"], name="doctext_extraction_doc_idx")]

    def __str__(self):
        return f"TextExtraction {self.pk}"


class TextExtractionResult(ImmutableRecord):
    """The one outcome of a request. ``status`` and ``error_code`` are the closed vocabularies of ``apps.document_text.rules``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    extraction = models.OneToOneField(TextExtraction, on_delete=models.PROTECT, related_name="result")
    status = models.CharField(max_length=16)
    error_code = models.CharField(max_length=40, blank=True, default="", help_text="Empty exactly when the status is extracted.")
    extractor_name = models.CharField(max_length=32)
    extractor_version = models.CharField(max_length=32)
    page_count = models.PositiveIntegerField(null=True, blank=True, help_text="Null only when a failed result never established it.")
    char_count = models.PositiveBigIntegerField(help_text="The characters of all page texts together.")
    source_sha256 = models.CharField(max_length=64, help_text="The document's recorded SHA-256 (verified against the bytes when they were read).")
    source_size_bytes = models.PositiveBigIntegerField()
    text_sha256 = models.CharField(max_length=64, help_text="SHA-256 of the canonical JSON of the pages (apps.document_text.rules.pages_sha256).")
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(status__in=list(RESULT_STATUSES)), name="doctext_result_status_valid"),
            models.CheckConstraint(condition=Q(error_code="") | Q(error_code__regex=ERROR_CODE_SHAPE), name="doctext_result_code_shape"),
            models.CheckConstraint(
                condition=(Q(status=RESULT_EXTRACTED, error_code="") | (~Q(status=RESULT_EXTRACTED) & ~Q(error_code=""))),
                name="doctext_result_code_matches_status",
            ),
            models.CheckConstraint(condition=Q(status=RESULT_FAILED) | Q(page_count__isnull=False), name="doctext_result_page_count_known"),
            models.CheckConstraint(condition=~Q(status=RESULT_FAILED) | Q(char_count=0), name="doctext_result_failed_has_no_text"),
            models.CheckConstraint(condition=Q(source_sha256__regex=_HASH) & Q(text_sha256__regex=_HASH), name="doctext_result_hash_shape"),
        ]

    def __str__(self):
        return f"TextExtractionResult {self.pk}"


class TextExtractionPage(ImmutableRecord):
    """The text of one page of one result. A ``failed`` page has an error code and no text; nothing is ever shortened to fit."""

    id = models.BigAutoField(primary_key=True)
    result = models.ForeignKey(TextExtractionResult, on_delete=models.PROTECT, related_name="pages")
    page_number = models.PositiveIntegerField(help_text="1-based.")
    status = models.CharField(max_length=8)
    error_code = models.CharField(max_length=40, blank=True, default="")
    text = models.TextField(blank=True, default="", help_text="User-provided data (the document's own text). Never logged or audited.")
    char_count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["result", "page_number"], name="doctext_page_number_unique"),
            models.CheckConstraint(condition=Q(page_number__gte=1), name="doctext_page_number_at_least_1"),
            models.CheckConstraint(condition=Q(status__in=list(PAGE_STATUSES)), name="doctext_page_status_valid"),
            models.CheckConstraint(
                condition=(Q(status=PAGE_FAILED, text="") & ~Q(error_code="")) | (~Q(status=PAGE_FAILED) & Q(error_code="")),
                name="doctext_page_code_matches_status",
            ),
            models.CheckConstraint(condition=Q(error_code="") | Q(error_code__regex=ERROR_CODE_SHAPE), name="doctext_page_code_shape"),
        ]

    def __str__(self):
        return f"TextExtractionPage {self.pk}"
