"""DB-free tests for settings parsing and the production safety baseline.

Placeholder values below are fake and only exist inside these tests.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from tests.settings_loader import load_settings

FAKE_KEY = "test-only-not-a-real-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz"


class ActiveSettingsTests(SimpleTestCase):
    """The settings the test run is actually using."""

    def test_auth_user_model(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")

    def test_postgresql_is_the_only_database(self):
        self.assertEqual(list(settings.DATABASES), ["default"])
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.postgresql")
        for alias in settings.DATABASES.values():
            self.assertNotIn("sqlite", alias["ENGINE"])

    def test_drf_defaults_are_authenticated_session_json(self):
        drf = settings.REST_FRAMEWORK
        self.assertEqual(drf["DEFAULT_AUTHENTICATION_CLASSES"], ["rest_framework.authentication.SessionAuthentication"])
        self.assertEqual(drf["DEFAULT_PERMISSION_CLASSES"], ["rest_framework.permissions.IsAuthenticated"])
        self.assertEqual(drf["DEFAULT_RENDERER_CLASSES"], ["rest_framework.renderers.JSONRenderer"])

    def test_no_cors_or_csrf_bypass(self):
        self.assertNotIn("corsheaders", " ".join(settings.INSTALLED_APPS + settings.MIDDLEWARE))
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)

    def test_only_planned_project_apps_are_installed(self):
        project_apps = [app for app in settings.INSTALLED_APPS if app.startswith("apps.")]
        self.assertEqual(project_apps, ["apps.accounts", "apps.cases", "apps.health"])


class DatabaseSettingsParsingTests(SimpleTestCase):
    def test_values_are_read_from_environment(self):
        base = load_settings(
            "config.settings.base",
            {
                "AP_DB_NAME": "name_x",
                "AP_DB_USER": "user_x",
                "AP_DB_PASSWORD": "placeholder-not-real",
                "AP_DB_HOST": "db.example.invalid",
                "AP_DB_PORT": "5433",
                "AP_DB_CONNECT_TIMEOUT": "5",
            },
        )
        db = base.DATABASES["default"]
        self.assertEqual(
            (db["NAME"], db["USER"], db["HOST"], db["PORT"]),
            ("name_x", "user_x", "db.example.invalid", "5433"),
        )
        self.assertEqual(db["PASSWORD"], "placeholder-not-real")
        self.assertEqual(db["OPTIONS"]["connect_timeout"], 5)

    def test_defaults_are_local_and_bounded(self):
        db = load_settings("config.settings.base").DATABASES["default"]
        self.assertEqual(db["HOST"], "127.0.0.1")
        self.assertEqual(db["PORT"], "5432")
        self.assertEqual(db["OPTIONS"]["connect_timeout"], 3)
        self.assertEqual((db["NAME"], db["USER"], db["PASSWORD"]), ("", "", ""))

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_DB_CONNECT_TIMEOUT": "soon"})
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_DB_CONNECT_TIMEOUT": "0"})


class DevelopmentSettingsTests(SimpleTestCase):
    def test_localhost_only_by_default(self):
        dev = load_settings("config.settings.development")
        self.assertEqual(dev.ALLOWED_HOSTS, ["localhost", "127.0.0.1", "[::1]"])
        self.assertTrue(dev.DEBUG)

    def test_secret_key_is_generated_when_not_supplied(self):
        first = load_settings("config.settings.development").SECRET_KEY
        second = load_settings("config.settings.development").SECRET_KEY
        self.assertTrue(first)
        self.assertNotEqual(first, second)

    def test_supplied_secret_key_is_used(self):
        dev = load_settings("config.settings.development", {"AP_SECRET_KEY": FAKE_KEY})
        self.assertEqual(dev.SECRET_KEY, FAKE_KEY)


class ProductionSettingsTests(SimpleTestCase):
    VALID = {"AP_SECRET_KEY": FAKE_KEY, "AP_ALLOWED_HOSTS": "app.example.invalid"}

    def test_secret_key_is_required(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            load_settings("config.settings.production", {"AP_ALLOWED_HOSTS": "app.example.invalid"})
        self.assertIn("AP_SECRET_KEY", str(ctx.exception))

    def test_blank_secret_key_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.production", {**self.VALID, "AP_SECRET_KEY": "   "})

    def test_allowed_hosts_are_required(self):
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.production", {"AP_SECRET_KEY": FAKE_KEY})

    def test_wildcard_hosts_are_rejected(self):
        for hosts in ("*", ".example.invalid,*", "*.example.invalid"):
            with self.subTest(hosts=hosts), self.assertRaises(ImproperlyConfigured):
                load_settings("config.settings.production", {**self.VALID, "AP_ALLOWED_HOSTS": hosts})

    def test_valid_configuration(self):
        prod = load_settings("config.settings.production", self.VALID)
        self.assertFalse(prod.DEBUG)
        self.assertEqual(prod.SECRET_KEY, FAKE_KEY)
        self.assertEqual(prod.ALLOWED_HOSTS, ["app.example.invalid"])
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)
        self.assertEqual(prod.CSRF_TRUSTED_ORIGINS, [])

    def test_debug_cannot_be_enabled_by_environment(self):
        prod = load_settings("config.settings.production", {**self.VALID, "DEBUG": "1", "AP_DEBUG": "1"})
        self.assertFalse(prod.DEBUG)

    def test_base_settings_contain_no_secret_key_to_inherit(self):
        # Neither environment can silently inherit a key from shared settings.
        self.assertFalse(hasattr(load_settings("config.settings.base"), "SECRET_KEY"))

    def test_production_does_not_use_the_development_generated_key(self):
        # With no AP_SECRET_KEY, development generates a key; production must refuse instead.
        load_settings("config.settings.development")
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.production", {"AP_ALLOWED_HOSTS": "app.example.invalid"})

    def test_production_shares_the_same_baseline(self):
        prod = load_settings("config.settings.production", self.VALID)
        self.assertEqual(prod.AUTH_USER_MODEL, "accounts.User")
        self.assertEqual(prod.DATABASES["default"]["ENGINE"], "django.db.backends.postgresql")
