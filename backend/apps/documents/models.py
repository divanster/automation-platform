"""Persistence for stored documents: METADATA only. The bytes are never in PostgreSQL (docs/architecture/data-model.md): ``storage_key`` names an object in the managed storage directory
(``apps.documents.storage``) and is never returned by the API, logged or audited.

``Document`` rows are written once, by ``apps.documents.services.upload_document``, in the same transaction as their audit event. Everything except ``processing_state`` is immutable at the
Django ORM and service boundary ONLY: ``save()`` on an existing row is refused unless it names only ``processing_state`` in ``update_fields``; ``QuerySet.update()`` and ``bulk_update()`` refuse any
other column; ``delete()`` and ``QuerySet.delete()`` are refused (and the owner foreign key is ``PROTECT``). There is no trigger and no revoked privilege: raw SQL, ``QuerySet.raw()``, the
``_base_manager``, a database administrator and a migration are not stopped. Nothing in DOC-1 changes ``processing_state``: it is ``uploaded`` for every row.

The database checks the shapes it can: the content type is ``application/pdf``, the hash is 64 lowercase hex characters, the size is between 1 byte and the hard maximum, the storage key has the
exact random-key shape and is unique, the state is in the closed vocabulary and the file name is not empty.

No case link yet: the data model's ``Document`` belongs to a ``Case`` with a unique ``(case, SHA-256)``; attaching a document to a case, and the duplicate-upload policy (open question 2.1), belong
to the slice that first uses a document. Today the same bytes uploaded twice are two documents.

These constraints were written without a PostgreSQL server in the development VM and are covered by PostgreSQL tests that have not been run there.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from .errors import DocumentImmutableError
from .rules import CONTENT_TYPE_PDF, HARD_MAX_BYTES, PROCESSING_STATES, SHA256_PATTERN, STATE_UPLOADED, STORAGE_KEY_PATTERN

MUTABLE_FIELDS = frozenset({"processing_state"})


class DocumentQuerySet(models.QuerySet):
    """A queryset that refuses every bulk change except the processing state, and every bulk delete."""

    def update(self, **kwargs):
        if not set(kwargs) <= MUTABLE_FIELDS:
            raise DocumentImmutableError("A stored document's metadata is immutable: only processing_state can be updated.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if not set(fields) <= MUTABLE_FIELDS:
            raise DocumentImmutableError("A stored document's metadata is immutable: only processing_state can be updated.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise DocumentImmutableError("Stored documents are never deleted through the ORM.")

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
        if update_conflicts:
            raise DocumentImmutableError("A stored document's metadata is immutable: an upsert is refused.")
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts)


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", help_text="The user who uploaded it.")
    original_filename = models.CharField(max_length=255, help_text="The client's file name as a LABEL (user-provided data, last path component only). Never a path.")
    content_type = models.CharField(max_length=32, default=CONTENT_TYPE_PDF)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, help_text="SHA-256 of the stored bytes, computed while they were received.")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    storage_key = models.CharField(max_length=35, unique=True, help_text="Random key of the object in the managed storage directory. Never exposed.")
    processing_state = models.CharField(max_length=16, default=STATE_UPLOADED)

    objects = DocumentQuerySet.as_manager()

    class Meta:
        # The document permissions (``apps.accounts.roles`` lists which role holds which) live here because Django creates a permission only for a model. The default add/change/delete/view
        # permissions are switched off: nothing checks them. A test compares this list with ``apps.accounts.roles``.
        default_permissions = ()
        permissions = [
            ("upload_document", "Can upload a document"),
            ("view_own_document", "Can view the documents the user uploaded"),
            ("view_any_document", "Can view any document"),
            ("download_own_document", "Can download the documents the user uploaded"),
            ("download_any_document", "Can download any document"),
        ]
        indexes = [models.Index(fields=["owner", "-uploaded_at"], name="documents_owner_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(content_type=CONTENT_TYPE_PDF), name="documents_content_type_pdf"),
            models.CheckConstraint(condition=Q(sha256__regex=f"^{SHA256_PATTERN}$"), name="documents_sha256_shape"),
            models.CheckConstraint(condition=Q(size_bytes__gte=1) & Q(size_bytes__lte=HARD_MAX_BYTES), name="documents_size_bounds"),
            models.CheckConstraint(condition=Q(storage_key__regex=f"^{STORAGE_KEY_PATTERN}$"), name="documents_storage_key_shape"),
            models.CheckConstraint(condition=Q(processing_state__in=list(PROCESSING_STATES)), name="documents_state_valid"),
            models.CheckConstraint(condition=~Q(original_filename=""), name="documents_filename_not_empty"),
        ]

    def __str__(self):
        return f"Document {self.pk}"

    def save_base(self, raw=False, force_insert=False, force_update=False, using=None, update_fields=None):
        # Every save path ends here (save(), loaddata's save_base(raw=True), direct calls).
        if self._state.adding:
            force_insert = True  # a new row is always INSERTed: an existing row is never overwritten
        elif update_fields is None or not set(update_fields) <= MUTABLE_FIELDS:
            raise DocumentImmutableError("A stored document's metadata is immutable: only a save naming processing_state in update_fields is allowed.")
        super().save_base(raw=raw, force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)

    def delete(self, *args, **kwargs):
        raise DocumentImmutableError("Stored documents are never deleted through the ORM.")
