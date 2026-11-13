"""``/api/auth/`` and ``/api/requests/`` routes. The health routes stay in ``apps.health``.

Paths use the ``uuid`` converter, so a path segment that is not a UUID is a plain 404 before any view runs.
"""

from django.urls import path

from . import views_auth, views_requests

app_name = "api"

urlpatterns = [
    path("auth/csrf/", views_auth.CsrfView.as_view(), name="auth-csrf"),
    path("auth/login/", views_auth.LoginView.as_view(), name="auth-login"),
    path("auth/logout/", views_auth.LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", views_auth.MeView.as_view(), name="auth-me"),
    path("requests/", views_requests.RequestCollectionView.as_view(), name="request-collection"),
    path("requests/<uuid:run_id>/", views_requests.RequestDetailView.as_view(), name="request-detail"),
    path("requests/<uuid:run_id>/edit/", views_requests.RequestEditView.as_view(), name="request-edit"),
    path("requests/<uuid:run_id>/approve/", views_requests.RequestApproveView.as_view(), name="request-approve"),
    path("requests/<uuid:run_id>/reject/", views_requests.RequestRejectView.as_view(), name="request-reject"),
    path("requests/<uuid:run_id>/audit/", views_requests.RequestAuditView.as_view(), name="request-audit"),
]
