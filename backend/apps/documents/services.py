"""The document services: authorized upload, list, read and download, each with its audit event.

AUTHORIZATION. Every call reloads the user (``apps.accounts.authz.load_actor``: an inactive or unknown user is refused) and applies the pure rules of ``apps.documents.access``: the PERMISSION part first, before
any document is looked up or any byte is read (``missing_permission``), the SCOPE part after the lookup (``out_of_scope``: the API answers it with the same 404 as an unknown document). A caller supplies no
identity: the owner is the authenticated user. Reading a document's metadata never grants its content: download is a separate permission.

UPLOAD. Order: permission, then the cheap checks on what the client DECLARED (name, ``application/pdf``, a length within the limit), then the body is streamed into the protected storage directory and
checked (``DocumentStore.ingest``), and only then one database transaction inserts the ``Document`` row and its ``document.uploaded`` audit event. A refused upload stores nothing.

ATOMICITY. The file is installed BEFORE the transaction, under a random key nothing refers to yet. If the transaction fails before the commit (the insert, the audit event), the object is removed again and
the error propagates: no row, no event, no file. If the failure is the COMMIT itself its outcome is unknown (the server may have committed and the acknowledgement been lost), so the object is NEVER removed to
guess it (a committed row without its file would be data loss; an orphan file is only waste): one fixed ERROR line is logged for the operator and the original exception propagates. A crash between the
install and the commit leaves the same orphan object. An orphan holds no personal data in its name and stays inside the protected directory.

DOWNLOAD. The object is opened and checked (size against the metadata) BEFORE anything is recorded; the ``document.downloaded`` event is then written in its own transaction, and a failed audit write stops the
download (no event, no bytes). The event records that an AUTHORIZED download was STARTED, not that the client received every byte.

LOGGING. Identifiers only (document id, actor public id), never a file name, hash, path or content. A refusal is logged by ``apps.accounts.authz`` with the action, the reason code and the actor's public id.
"""

import logging
import uuid
from datetime import datetime
from typing import NamedTuple

from django.db import transaction

from apps.accounts.authz import load_actor, log_refusal
from apps.accounts.errors import AuthorizationError
from apps.audit.events import ACTION_DOCUMENT_DOWNLOADED, ACTION_DOCUMENT_UPLOADED, SUBJECT_DOCUMENT, user_event
from apps.audit.recorder import record_event

from . import rules
from .access import Action, access_problem, permission_problem, scope_problem
from .errors import DocumentNotFoundError, DocumentStorageError
from .models import Document
from .storage import DocumentStream

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

_BEFORE_COMMIT, _COMMITTING = "before_commit", "committing"


class DocumentItem(NamedTuple):
    """The read model of one document: metadata only. There is no storage key, path or content here."""

    id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_at: datetime
    processing_state: str
    uploaded_by: uuid.UUID  # the uploader's public_id
    allowed_actions: tuple  # what the caller may do with the document beyond reading this: ("download",) or ()


class DocumentPage(NamedTuple):
    items: tuple
    count: int
    page: int
    page_size: int


class Download(NamedTuple):
    stream: DocumentStream
    filename: str
    content_type: str
    size_bytes: int


def _authorize(user, action: Action, name: str):
    actor = load_actor(user)
    problem = permission_problem(action, is_superuser=actor.is_superuser, permissions=actor.permissions)
    if problem is not None:
        log_refusal(f"document.{name}", problem, actor.public_id)
        raise AuthorizationError(problem)
    return actor


def _require_scope(actor, action: Action, name: str, document) -> None:
    problem = scope_problem(action, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=document.owner_id)
    if problem is not None:
        log_refusal(f"document.{name}", problem, actor.public_id)
        raise AuthorizationError(problem)


def _item(document, actor) -> DocumentItem:
    may_download = (
        access_problem(Action.DOWNLOAD, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=document.owner_id) is None
    )
    return DocumentItem(
        id=document.pk,
        original_filename=document.original_filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        uploaded_at=document.uploaded_at,
        processing_state=document.processing_state,
        uploaded_by=document.owner.public_id,
        allowed_actions=("download",) if may_download else (),
    )


def _check_page(page, page_size):
    for name, value in (("page", page), ("page_size", page_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be an integer of at least 1")
    if page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be at most {MAX_PAGE_SIZE}")


def _get(document_id) -> Document:
    document = Document.objects.select_related("owner").filter(pk=document_id).first()
    if document is None:
        raise DocumentNotFoundError()
    return document


def _log_discard_failed(document_id) -> None:
    try:
        logger.error("Document upload failed and its stored object could not be removed; remove the orphan (document=%s).", document_id)
    except Exception:  # noqa: BLE001 - diagnosis must not replace the failure it reports
        pass


def _log_uncertain(document_id) -> None:
    try:
        logger.error("Document upload: the database commit was not confirmed; the stored object was kept; reconcile (document=%s).", document_id)
    except Exception:  # noqa: BLE001
        pass


def upload_document(*, user, filename_header, content_type, content_length, read, store_factory) -> DocumentItem:
    """Store one PDF the caller uploads and return its metadata.

    ``filename_header`` is the raw ``X-Document-Filename`` header (the client's name, percent-encoded UTF-8; ``None`` when absent), ``content_type`` the declared type, ``content_length`` the declared
    length (the header's text), ``read`` the request stream's ``read(n)`` and ``store_factory`` the composition root's way to get the ``DocumentStore`` (it may raise ``DocumentStorageError``). Everything
    the client sent is looked at only AFTER the permission check. Raises ``AuthorizationError``, ``DocumentRejectedError`` or ``DocumentStorageError``; nothing is stored in any of those cases.
    """
    actor = _authorize(user, Action.UPLOAD, "upload")
    name = rules.filename_from_header(filename_header)
    rules.check_content_type(content_type)
    store = store_factory()
    declared = rules.parse_content_length(content_length, store.max_bytes)
    stored = store.ingest(read, declared)
    document_id = uuid.uuid4()
    phase = _BEFORE_COMMIT
    try:
        with transaction.atomic():
            document = Document.objects.create(
                id=document_id,
                owner=actor.user,
                original_filename=name,
                content_type=rules.CONTENT_TYPE_PDF,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.key,
            )
            record_event(user_event(actor.public_id, ACTION_DOCUMENT_UPLOADED, SUBJECT_DOCUMENT, document.pk, byte_length=stored.size_bytes, content_hash=stored.sha256))
            phase = _COMMITTING  # from here a failure is the commit's, whose outcome is unknown
    except BaseException:
        if phase == _COMMITTING:
            _log_uncertain(document_id)
        else:
            try:
                store.delete(stored.key)
            except DocumentStorageError:
                _log_discard_failed(document_id)
        raise
    logger.info("Document uploaded: document=%s actor=%s", document.pk, actor.public_id)
    return _item(document, actor)


def list_documents(*, user, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> DocumentPage:
    """One page of the documents ``user`` may view, newest first (the id breaks ties). A user with ``view_any_document`` (or a superuser) sees all of them, one with only ``view_own_document`` the ones they uploaded."""
    _check_page(page, page_size)
    actor = _authorize(user, Action.VIEW, "list")
    sees_all = scope_problem(Action.VIEW, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, owner_pk=None) is None
    documents = Document.objects.select_related("owner")
    if not sees_all:
        documents = documents.filter(owner_id=actor.pk)
    documents = documents.order_by("-uploaded_at", "-id")
    count = documents.count()
    offset = (page - 1) * page_size
    items = tuple(_item(document, actor) for document in documents[offset : offset + page_size])
    return DocumentPage(items, count, page, page_size)


def get_document(*, user, document_id) -> DocumentItem:
    """The metadata of one document. ``DocumentNotFoundError`` for an unknown one, ``AuthorizationError`` (``out_of_scope``) for one the caller may not view."""
    actor = _authorize(user, Action.VIEW, "view")
    document = _get(document_id)
    _require_scope(actor, Action.VIEW, "view", document)
    return _item(document, actor)


def open_download(*, user, document_id, store_factory) -> Download:
    """Authorize a download, open the stored object and record ``document.downloaded``. The caller must close ``stream`` (a streaming response does)."""
    actor = _authorize(user, Action.DOWNLOAD, "download")
    document = _get(document_id)
    _require_scope(actor, Action.DOWNLOAD, "download", document)
    store = store_factory()
    stream = store.open(document.storage_key, document.size_bytes)
    try:
        with transaction.atomic():
            record_event(
                user_event(
                    actor.public_id,
                    ACTION_DOCUMENT_DOWNLOADED,
                    SUBJECT_DOCUMENT,
                    document.pk,
                    byte_length=document.size_bytes,
                    content_hash=document.sha256,
                    by_owner=document.owner_id == actor.pk,
                )
            )
    except BaseException:
        stream.close()
        raise
    logger.info("Document download started: document=%s actor=%s", document.pk, actor.public_id)
    return Download(stream, document.original_filename, document.content_type, document.size_bytes)
