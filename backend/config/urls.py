from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.api.views_ai_settings import AIExtractionSettingsView
from config.intake_extraction import build_catalog

# The AI settings route is wired here, not in apps.api.urls: the catalog an administrator may choose from comes from the composition root, and the app
# packages stay free of the AI Gateway. It is listed before the API include so that it is resolved first.
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", include("apps.health.urls")),
    path(
        "api/ai-settings/extraction/",
        AIExtractionSettingsView.as_view(catalog=build_catalog(), lease_seconds=settings.WORKER_LEASE_SECONDS),
        name="ai-settings-extraction",
    ),
    path("api/", include("apps.api.urls")),
]
