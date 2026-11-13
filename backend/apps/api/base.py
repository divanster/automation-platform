"""Shared view plumbing. Views are TRANSPORT ADAPTERS: they never decide who may do what.

``ApiView`` deliberately does NOT set ``authentication_classes`` or ``permission_classes``: every view that
inherits it keeps the project defaults (``SessionAuthentication``, ``IsAuthenticated``), which a test pins for
every non-public route. The only public exceptions (the CSRF bootstrap and the login) are declared where they
are defined and listed in tests/test_api_security.py.

Every response gets ``Cache-Control: no-store`` so a browser or proxy never keeps a user's data.
"""

from collections.abc import Mapping

from django.utils.cache import add_never_cache_headers
from rest_framework.exceptions import ParseError
from rest_framework.views import APIView

from .throttles import ApiUserThrottle


class NoStoreMixin:
    """Adds ``Cache-Control: no-store`` to EVERY response of the view, errors included."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


class ApiView(NoStoreMixin, APIView):
    throttle_classes = [ApiUserThrottle]


def validated(serializer_class, data, **kwargs):
    """Validate ``data`` with ``serializer_class`` and return the serializer; raises the 400 errors of the contract.

    A body that is not a JSON object is ``malformed_json`` (not a field error). ``ValidationError`` carries
    error codes only; the error handler never echoes a value.
    """
    if not isinstance(data, Mapping) and not hasattr(data, "keys"):
        raise ParseError("malformed")
    serializer = serializer_class(data=data, **kwargs)
    serializer.is_valid(raise_exception=True)
    return serializer
