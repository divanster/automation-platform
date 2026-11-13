"""The only request parser: strict JSON with a hard size bound.

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
from rest_framework.parsers import JSONParser


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "The request body is too large."
    default_code = "payload_too_large"


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
