"""Health endpoints.

Both are deliberately reachable without a login (an explicit exception to the
authenticated-by-default DRF configuration) so a supervisor can probe them. They
return only a fixed, minimal status and never configuration, paths, versions,
credentials, or exception text.
"""

import logging

from django.db import connections
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class _PublicHealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class LivenessView(_PublicHealthView):
    """The process is up and able to answer HTTP. Never touches the database."""

    def get(self, request):
        return Response({"status": "ok"})


class ReadinessView(_PublicHealthView):
    """PostgreSQL accepted a connection and answered ``SELECT 1``.

    This does NOT prove that migrations are applied, that the schema is correct,
    or that any other service is healthy.
    """

    def get(self, request):
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            # Log only the exception type: messages can contain host names or
            # connection details.
            logger.warning("Readiness check failed (%s).", type(exc).__name__)
            return Response({"status": "unavailable"}, status=503)
        return Response({"status": "ok"})
