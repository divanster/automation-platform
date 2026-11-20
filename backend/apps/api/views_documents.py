"""``/api/documents/``: upload a PDF, list and read the metadata of the documents the user may view, and download the original.

A transport adapter like every API view: it hands the request's transport facts to ONE service with ``request.user`` and serializes the result. Authorization, every rule on the name, type, size and content,
the storage and the audit events are the service's (``apps.documents.services``); this module has none of them, no database access and no logging. The routes are wired in ``config/urls.py``, which injects
``store_factory`` (the composition root's choice of document storage, ``config/document_storage.py``) so this package imports no storage backend. A view that was not wired with one fails closed (a 500, nothing
stored).

UPLOAD IS A RAW BODY, NOT MULTIPART. ``POST /api/documents/`` carries the PDF itself as the request body with ``Content-Type: application/pdf``, ``Content-Length`` and the file name in the
``X-Document-Filename`` header (percent-encoded UTF-8). Multipart is refused: Django parses a form body in the middleware / authentication stage, before any view, and would stage a large file part in
the system temporary directory (see the settings); a raw body is read once, by the storage, straight into its own protected directory. The view never reads ``request.data``, but session authentication's
CSRF check reads ``request.POST``, which makes DRF parse the body: with the default JSON-only parser that is a 415 for every authenticated ``application/pdf`` upload. So the collection view sets ONE parser,
``UnreadBodyParser``, which accepts any media type and reads nothing; the CSRF check then succeeds or fails on the token alone and the stream is left for the storage. The default parsers are unchanged.
Session authentication and CSRF are the project defaults (the ``X-CSRFToken`` header). A WSGI server is required: the ASGI handler spools every request body to the system temporary directory before the view
runs, so the view refuses an ASGI request (``document_storage_unavailable``) rather than weaken the guarantee.

DOWNLOAD STREAMS the stored file in chunks as ``application/pdf`` with ``Content-Disposition: attachment``, ``X-Content-Type-Options: nosniff`` and a ``sandbox`` Content-Security-Policy, ``no-store`` caching, and
no range support. The client's ``Accept`` header is ignored for this one route (the body is a file, not JSON); errors are still JSON. There is no public URL for a file: only this authorized route.
"""

from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.asgi import ASGIRequest
from django.http import StreamingHttpResponse
from django.utils.http import content_disposition_header
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response

from apps.documents import services
from apps.documents.errors import CODE_UNAVAILABLE, DocumentStorageError

from .base import ApiView, validated
from .parsers import UnreadBodyParser
from .serializers import DocumentOutput, PageQuery, page_output
from .throttles import ApiUserThrottle, DocumentUploadHourThrottle, DocumentUploadThrottle


class _AnyAcceptNegotiation(DefaultContentNegotiation):
    """The download answers with a file whatever the client's ``Accept`` says; DRF still needs a renderer for the JSON error bodies, so it takes the first one."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class _DocumentView(ApiView):
    store_factory = None  # set by as_view(store_factory=...) in config/urls.py

    def _factory(self):
        if self.store_factory is None:
            raise ImproperlyConfigured("The document views need a store factory.")
        return self.store_factory


class DocumentCollectionView(_DocumentView):
    """``GET /api/documents/`` (a page of the documents the user may view) and ``POST`` (upload one PDF as the raw request body)."""

    parser_classes = [UnreadBodyParser]

    def get_throttles(self):
        extra = [DocumentUploadThrottle, DocumentUploadHourThrottle] if self.request.method == "POST" else []
        return [throttle() for throttle in [ApiUserThrottle, *extra]]

    def get(self, request):
        query = validated(PageQuery, request.query_params).validated_data
        page = services.list_documents(user=request.user, page=query["page"], page_size=query["page_size"])
        return Response(page_output(DocumentOutput, page))

    def post(self, request):
        if isinstance(request._request, ASGIRequest):
            raise DocumentStorageError(CODE_UNAVAILABLE)
        meta = request.META
        item = services.upload_document(
            user=request.user,
            filename_header=meta.get("HTTP_X_DOCUMENT_FILENAME"),
            content_type=meta.get("CONTENT_TYPE"),
            content_length=meta.get("CONTENT_LENGTH"),
            read=lambda size: request.stream.read(size),
            store_factory=self._factory(),
        )
        return Response(DocumentOutput(item).data, status=201)


class DocumentDetailView(_DocumentView):
    def get(self, request, document_id):
        return Response(DocumentOutput(services.get_document(user=request.user, document_id=document_id)).data)


class DocumentContentView(_DocumentView):
    content_negotiation_class = _AnyAcceptNegotiation

    def get(self, request, document_id):
        download = services.open_download(user=request.user, document_id=document_id, store_factory=self._factory())
        response = StreamingHttpResponse(download.stream, content_type=download.content_type)
        response["Content-Length"] = str(download.size_bytes)
        response["Content-Disposition"] = content_disposition_header(True, download.filename)
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Accept-Ranges"] = "none"
        return response
