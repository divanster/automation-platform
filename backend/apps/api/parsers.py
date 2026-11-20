"""The request parsers: strict bounded JSON (the only default) and, for the document upload route alone, a parser that reads nothing.

DRF's stock JSON parser reads the whole body, whatever its size, and a deeply nested document can raise
``RecursionError`` (which DRF does not turn into a 400). This parser reads at most ``settings.API_MAX_BODY_BYTES``
plus one byte, refuses a larger body with 413 BEFORE decoding it, rejects NaN and Infinity (strict mode), and
turns a nesting or syntax error into the ordinary malformed-JSON error. Neither the body nor the decoder's
message is ever echoed (the error handler uses fixed messages).
"""

import io

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException, ParseError
from rest_framework.parsers import BaseParser, JSONParser


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "The request body is too large."
    default_code = "payload_too_large"


class UnreadBodyParser(BaseParser):
    """Accepts any media type and reads NOTHING: for the one route whose body is a raw upload that its storage streams itself (``apps.api.views_documents``).

    DRF's ``SessionAuthentication`` runs Django's CSRF check, which reads ``request.POST``; DRF answers that by parsing the body, and a media type no parser accepts is a 415 before the view runs.
    This parser lets that read succeed with empty data while the stream stays unread and available to ``request.stream``. It is set on that view only, never as a default, and it keeps no bytes in memory.
    """

    media_type = "*/*"

    def parse(self, stream, media_type=None, parser_context=None):
        return {}


class BoundedJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.API_MAX_BODY_BYTES
        body = stream.read(limit + 1)
        if len(body) > limit:
            raise PayloadTooLarge()
        try:
            return super().parse(io.BytesIO(body), media_type, parser_context)
        except RecursionError:
            raise ParseError("malformed") from None
