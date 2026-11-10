"""DB-free tests: the public health endpoints must not weaken authentication elsewhere.

The DRF defaults are authenticated + session only. The health views opt out
individually. These tests fail if any other API view becomes public, or if the
global defaults change.
"""

from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.health import views as health_views

PUBLIC_BY_DESIGN = {health_views.LivenessView, health_views.ReadinessView}


def _api_view_classes(patterns):
    """Yield every DRF view class reachable from the project's URL configuration."""
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _api_view_classes(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            view_class = getattr(pattern.callback, "cls", None)
            if view_class is not None and issubclass(view_class, APIView):
                yield view_class


class GlobalDefaultsTests(SimpleTestCase):
    def test_a_view_with_no_overrides_requires_authentication(self):
        class Probe(APIView):
            def get(self, request):  # pragma: no cover - must never be reached
                raise AssertionError("anonymous request reached an un-overridden view")

        self.assertEqual(Probe.permission_classes, [IsAuthenticated])
        self.assertEqual(Probe.authentication_classes, [SessionAuthentication])

        request = APIRequestFactory().get("/probe/")
        response = Probe.as_view()(request)
        self.assertIn(response.status_code, (401, 403))

class PublicEndpointInventoryTests(SimpleTestCase):
    def test_only_the_health_views_are_public(self):
        classes = set(_api_view_classes(get_resolver().url_patterns))
        self.assertTrue(PUBLIC_BY_DESIGN <= classes, "health views are not routed")
        public = {cls for cls in classes if AllowAny in cls.permission_classes or not cls.authentication_classes}
        self.assertEqual(public, PUBLIC_BY_DESIGN)

    def test_every_other_api_view_keeps_the_defaults(self):
        for cls in _api_view_classes(get_resolver().url_patterns):
            if cls in PUBLIC_BY_DESIGN:
                continue
            with self.subTest(view=cls.__name__):
                self.assertEqual(cls.permission_classes, [IsAuthenticated])
                self.assertEqual(cls.authentication_classes, [SessionAuthentication])

    def test_health_views_are_get_only(self):
        for cls in PUBLIC_BY_DESIGN:
            with self.subTest(view=cls.__name__):
                allowed = {m.upper() for m in cls.http_method_names if hasattr(cls, m)}
                self.assertEqual(allowed - {"OPTIONS"}, {"GET"})
