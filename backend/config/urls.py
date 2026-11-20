from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.api.views_ai_connection_test import AIConnectionTestView
from apps.api.views_ai_credentials import AICredentialCollectionView, AICredentialDetailView
from apps.api.views_ai_settings import AIExtractionSettingsView
from config.ai_connection_test import build_prober
from config.intake_extraction import build_catalog
from config.secret_store import build_secret_store

# The AI settings route is wired here, not in apps.api.urls: the catalog an administrator may choose from comes from the composition root, and the app
# packages stay free of the AI Gateway. It is listed before the API include so that it is resolved first. The credential routes are wired here for the same
# reason: the secret store this process uses is the composition root's choice (``config/secret_store.py``), injected as a factory, so no app imports a store backend.
# The connection test route is wired here too: how a saved connection is called (``config/ai_connection_test.py``) is the composition root's, injected as a ``prober``.
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", include("apps.health.urls")),
    path(
        "api/ai-settings/extraction/",
        AIExtractionSettingsView.as_view(catalog=build_catalog(), lease_seconds=settings.WORKER_LEASE_SECONDS),
        name="ai-settings-extraction",
    ),
    path("api/ai-settings/connections/<str:connection_id>/test/", AIConnectionTestView.as_view(prober=build_prober()), name="ai-connection-test"),
    path("api/ai-credentials/", AICredentialCollectionView.as_view(store_factory=build_secret_store), name="ai-credential-collection"),
    path("api/ai-credentials/<str:reference>/", AICredentialDetailView.as_view(store_factory=build_secret_store), name="ai-credential-detail"),
    path("api/", include("apps.api.urls")),
]
