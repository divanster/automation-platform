"""``/api/documents/<id>/text-extractions/``: request the text extraction of a stored PDF, list a document's extractions, read one's status and read its extracted text (DOC-2).

A transport adapter like every API view: it hands the request's transport facts to ONE service with ``request.user`` and serializes the result. Authorization (the same permission and scope as downloading the
document), the queueing, the audit events and every rule are the service's (``apps.document_text.services``); this module has none of them, no database access and no logging. It needs no storage: only the
worker reads the stored file.

THE REQUEST BODY IS IGNORED. ``POST`` carries no parameters: it asks for the text of that document and nothing else (no limit, language, page range or file name comes from the client). The one parser reads
nothing, so any body is simply not looked at. The answer is ``201`` with the new extraction, or ``200`` with the existing one that makes a new one pointless (still queued or running, or already done by the installed
extractor version).

STATUS AND TEXT ARE SEPARATE ROUTES. The collection and detail routes return status and facts only (no text field exists in their serializer). The text is returned only by ``.../text/`` and, like a download,
is audited; ``409 extraction_not_ready`` while the extraction has no result. Every response is ``Cache-Control: no-store``.
"""

import uuid

from rest_framework.response import Response

from apps.document_text import services

from .base import ApiView, validated
from .parsers import UnreadBodyParser
from .serializers import ExtractedTextOutput, PageQuery, TextExtractionOutput, page_output
from .throttles import ApiUserThrottle, DocumentTextHourThrottle, DocumentTextThrottle


class TextExtractionCollectionView(ApiView):
    parser_classes = [UnreadBodyParser]

    def get_throttles(self):
        extra = [DocumentTextThrottle, DocumentTextHourThrottle] if self.request.method == "POST" else []
        return [throttle() for throttle in [ApiUserThrottle, *extra]]

    def get(self, request, document_id: uuid.UUID):
        query = validated(PageQuery, request.query_params).validated_data
        page = services.list_text_extractions(user=request.user, document_id=document_id, page=query["page"], page_size=query["page_size"])
        return Response(page_output(TextExtractionOutput, page))

    def post(self, request, document_id: uuid.UUID):
        outcome = services.request_text_extraction(user=request.user, document_id=document_id)
        return Response(TextExtractionOutput(outcome.item).data, status=201 if outcome.created else 200)


class TextExtractionDetailView(ApiView):
    def get(self, request, document_id: uuid.UUID, extraction_id: uuid.UUID):
        return Response(TextExtractionOutput(services.get_text_extraction(user=request.user, document_id=document_id, extraction_id=extraction_id)).data)


class TextExtractionTextView(ApiView):
    def get(self, request, document_id: uuid.UUID, extraction_id: uuid.UUID):
        return Response(ExtractedTextOutput(services.read_extracted_text(user=request.user, document_id=document_id, extraction_id=extraction_id)).data)
