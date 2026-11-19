"""``GET`` and ``PUT /api/ai-settings/extraction/``: the administrator's view of how Request Intake extracts, and the way to change it.

A transport adapter like every API view: it validates the shape, calls ONE service with ``request.user`` and serializes the result. Authorization (an
administrator only), validation against the catalog, the revision check and the storage are the service's; this module has none of them, no database access
and no logging. The route is wired in ``config/urls.py``, which injects the ``catalog`` (what an administrator may choose, built by the composition root from
the AI Gateway's declarations) and the worker lease the timeout must fit in: this app and this package import nothing from the gateway.

A saved change does NOT reach a running worker: every response says so (``apply``). The view fails closed (a 500, no data) if it was not wired with a catalog.
"""

from django.core.exceptions import ImproperlyConfigured
from rest_framework.response import Response

from apps.ai_settings import services

from .base import ApiView, validated
from .serializers import AIExtractionSettingsInput, AIExtractionSettingsOutput


class AIExtractionSettingsView(ApiView):
    catalog = None  # set by as_view(catalog=...) in config/urls.py
    lease_seconds = None  # set by as_view(lease_seconds=...)

    def _catalog(self):
        if self.catalog is None:
            raise ImproperlyConfigured("AIExtractionSettingsView needs a catalog.")
        return self.catalog

    def get(self, request):
        view = services.get_extraction_settings(user=request.user, catalog=self._catalog())
        return Response(AIExtractionSettingsOutput(view).data)

    def put(self, request):
        data = dict(validated(AIExtractionSettingsInput, request.data).validated_data)
        expected_revision = data.pop("expected_revision")
        view = services.update_extraction_settings(
            user=request.user, data=data, expected_revision=expected_revision, catalog=self._catalog(), lease_seconds=self.lease_seconds
        )
        return Response(AIExtractionSettingsOutput(view).data)
