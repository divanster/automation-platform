"""DB-free static tests: the API is a thin transport over the services, and its security settings are explicit.

They read source with ``ast`` and import settings fresh under controlled environments; no database is touched.
"""

import ast
import inspect
import textwrap
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import URLPattern, get_resolver

from apps.api import base, views_auth, views_requests
from apps.intake import queries, review, submission
from tests.settings_loader import load_settings
from tests.test_request_intake_boundaries import imported_modules

BACKEND = Path(__file__).resolve().parent.parent
API = BACKEND / "apps" / "api"
FAKE_KEY = "test-only-key-0123456789-0123456789-0123456789-abcdef"
PROD = {"AP_SECRET_KEY": FAKE_KEY, "AP_ALLOWED_HOSTS": "app.example.invalid"}

SERVICES = {
    "submit_request_as": submission.submit_request_as,
    "list_requests": queries.list_requests,
    "get_request_detail": queries.get_request_detail,
    "edit_request": review.edit_request,
    "approve_request": review.approve_request,
    "reject_request": review.reject_request,
    "list_run_audit": queries.list_run_audit,
}


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def api_sources():
    return sorted(API.glob("*.py"))


def calls_in(node):
    out = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            out.append((name, call))
    return out


class RouteInventoryTests(SimpleTestCase):
    def routes(self):
        found = {}

        def walk(patterns, prefix=""):
            for p in patterns:
                if isinstance(p, URLPattern):
                    view_class = getattr(p.callback, "cls", None)  # the admin's routes are plain functions
                    if view_class is not None:
                        found[prefix + str(p.pattern)] = view_class
                else:
                    walk(p.url_patterns, prefix + str(p.pattern))

        walk(get_resolver().url_patterns)
        return found

    def test_the_api_routes_are_exactly_the_documented_ones(self):
        api = {path: cls.__name__ for path, cls in self.routes().items() if path.startswith("api/") and not path.startswith("api/health/")}
        self.assertEqual(
            api,
            {
                "api/auth/csrf/": "CsrfView",
                "api/auth/login/": "LoginView",
                "api/auth/logout/": "LogoutView",
                "api/auth/me/": "MeView",
                "api/requests/": "RequestCollectionView",
                "api/requests/<uuid:run_id>/": "RequestDetailView",
                "api/requests/<uuid:run_id>/edit/": "RequestEditView",
                "api/requests/<uuid:run_id>/approve/": "RequestApproveView",
                "api/requests/<uuid:run_id>/reject/": "RequestRejectView",
                "api/requests/<uuid:run_id>/audit/": "RequestAuditView",
            },
        )

    def test_every_run_path_segment_is_a_uuid(self):
        for path in self.routes():
            if "run_id" in path:
                self.assertIn("<uuid:run_id>", path)

    def test_no_route_is_a_file_upload_export_or_dynamic_handler_registration(self):
        for path in self.routes():
            for word in ("upload", "export", "handler", "register", "task", "worker"):
                self.assertNotIn(word, path)

    def test_the_health_routes_are_unchanged(self):
        paths = {p for p in self.routes() if p.startswith("api/health/")}
        self.assertEqual(paths, {"api/health/live/", "api/health/ready/"})

    def test_each_view_exposes_only_the_documented_methods(self):
        expected = {
            views_auth.CsrfView: {"GET"},
            views_auth.LoginView: {"POST"},
            views_auth.LogoutView: {"POST"},
            views_auth.MeView: {"GET"},
            views_requests.RequestCollectionView: {"GET", "POST"},
            views_requests.RequestDetailView: {"GET"},
            views_requests.RequestEditView: {"POST"},
            views_requests.RequestApproveView: {"POST"},
            views_requests.RequestRejectView: {"POST"},
            views_requests.RequestAuditView: {"GET"},
        }
        for cls, methods in expected.items():
            with self.subTest(view=cls.__name__):
                self.assertEqual({m.upper() for m in cls.http_method_names if hasattr(cls, m)} - {"OPTIONS"}, methods)


class ViewsAreTransportAdaptersTests(SimpleTestCase):
    VIEW_FILES = ("views_requests.py", "views_auth.py")

    def test_views_import_no_model_no_orm_no_database_and_no_domain_package(self):
        for name in self.VIEW_FILES:
            path = API / name
            with self.subTest(module=name):
                modules = imported_modules(path)
                self.assertEqual({m for m in modules if m.endswith(".models") or m.startswith("django.db")}, set())
                self.assertEqual({m for m in modules if m.startswith("workflows")}, set())
                source = path.read_text(encoding="utf-8")
                for token in (".objects", "transaction", "cursor", "select_for_update", ".save(", ".delete("):
                    self.assertNotIn(token, source)

    def test_every_request_view_method_calls_exactly_one_service(self):
        for cls in (views_requests.RequestCollectionView, views_requests.RequestDetailView, views_requests.RequestEditView, views_requests.RequestApproveView, views_requests.RequestRejectView, views_requests.RequestAuditView):
            for method in ("get", "post"):
                function = cls.__dict__.get(method)
                if function is None:
                    continue
                node = ast.parse(inspect.getsource(function).lstrip() if False else "\n".join(line[4:] if line.startswith("    ") else line for line in inspect.getsource(function).splitlines()))
                used = {name for name, _ in calls_in(node) if name in SERVICES}
                with self.subTest(view=cls.__name__, method=method):
                    self.assertEqual(len(used), 1, used)

    def test_the_keywords_views_pass_are_real_parameters_of_the_services(self):
        for name in ("views_requests.py",):
            for call_name, call in calls_in(tree(API / name)):
                if call_name in SERVICES:
                    parameters = set(inspect.signature(SERVICES[call_name]).parameters)
                    with self.subTest(service=call_name):
                        self.assertLessEqual({k.arg for k in call.keywords}, parameters)

    def test_the_actor_is_always_request_user(self):
        for call_name, call in calls_in(tree(API / "views_requests.py")):
            if call_name in SERVICES:
                user = next(k for k in call.keywords if k.arg == "user")
                self.assertEqual(ast.unparse(user.value), "request.user", call_name)

    def test_no_identity_is_ever_read_from_the_request_body(self):
        source = (API / "views_requests.py").read_text(encoding="utf-8")
        for token in ("approver", "public_id", "actor", "user_id", "rejected_by", "edited_by"):
            self.assertNotIn(token, source)

    def test_views_do_not_log_print_or_format_values_into_messages(self):
        for name in self.VIEW_FILES:
            names = {n.id for n in ast.walk(tree(API / name)) if isinstance(n, ast.Name)}
            with self.subTest(module=name):
                self.assertEqual(names & {"print", "logger", "logging", "pprint"}, set())

    def test_only_the_error_module_logs_and_only_the_class_name(self):
        for path in api_sources():
            source = path.read_text(encoding="utf-8")
            if path.name == "errors.py":
                lines = [line for line in source.splitlines() if "logger." in line]
                self.assertEqual(len(lines), 1)
                self.assertIn("type(exc).__name__", lines[0])
            else:
                self.assertNotIn("logger.", source, path.name)

    def test_the_two_public_views_are_the_only_ones_that_set_authentication_or_permissions(self):
        for path in api_sources():
            identifiers = {n.id for n in ast.walk(tree(path)) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            if path.name != "views_auth.py":
                with self.subTest(module=path.name):
                    self.assertEqual(identifiers & {"authentication_classes", "permission_classes", "AllowAny", "csrf_exempt"}, set())
        view_tree = tree(API / "views_auth.py")
        assigns = [n for n in ast.walk(view_tree) if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "authentication_classes" for t in n.targets)]
        self.assertEqual(len(assigns), 2)  # CsrfView and LoginView, nothing else
        names = {n.id for n in ast.walk(view_tree) if isinstance(n, ast.Name)}
        self.assertNotIn("csrf_exempt", names)

    def test_the_base_view_keeps_the_project_authentication_and_permission_defaults(self):
        self.assertNotIn("authentication_classes", base.ApiView.__dict__)
        self.assertNotIn("permission_classes", base.ApiView.__dict__)
        for cls in (views_auth.LogoutView, views_auth.MeView, views_requests.RequestCollectionView):
            self.assertTrue(issubclass(cls, base.ApiView))


class NoDuplicatedLogicTests(SimpleTestCase):
    def test_the_api_names_no_domain_validation_canonicalization_approval_or_persistence_operation(self):
        banned = {
            "canonicalize_request", "canonical_decimal_string", "validate_request", "validate_request_text", "approve_revision", "record_approval",
            "record_revision", "record_rejection", "transition_run", "lock_run", "check_export_eligibility", "create_request_intake_revision",
            "approval_problem", "edit_problem", "rejection_problem", "is_self_approval", "access_problem", "make_password", "check_password",
            "pbkdf2", "set_password", "bcrypt", "argon2",
        }  # fmt: skip
        for path in api_sources():
            used = {n.id for n in ast.walk(tree(path)) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(module=path.name):
                self.assertEqual(used & banned, set())

    def test_only_the_throttle_module_hashes_and_only_a_username_for_a_cache_key(self):
        for path in api_sources():
            hashes = "hashlib" in imported_modules(path)
            self.assertEqual(hashes, path.name == "throttles.py", path.name)
        throttles = tree(API / "throttles.py")
        identifiers = {n.id for n in ast.walk(throttles) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(throttles) if isinstance(n, ast.Attribute)}
        self.assertIn("sha256", identifiers)
        self.assertFalse({i for i in identifiers if "password" in i.lower()})  # the password never enters this module's code

    def test_no_second_password_check_exists_authentication_is_djangos(self):
        view_tree = tree(API / "views_auth.py")
        self.assertIn("from django.contrib.auth import authenticate, login as django_login, logout as django_logout", (API / "views_auth.py").read_text(encoding="utf-8"))
        calls = [name for name, _ in calls_in(view_tree) if name == "authenticate"]
        self.assertEqual(len(calls), 1)  # one call to Django's authenticate; no other credential check anywhere

    def test_the_api_package_imports_no_forbidden_capability(self):
        forbidden = {"jwt", "jose", "requests", "urllib", "http", "socket", "subprocess", "pickle", "ctypes", "anthropic", "openai", "importlib", "corsheaders"}
        for path in api_sources():
            with self.subTest(module=path.name):
                self.assertEqual({m.split(".")[0] for m in imported_modules(path)} & forbidden, set())

    def test_the_new_read_services_use_the_same_pure_rules_and_no_hashing(self):
        source = (BACKEND / "apps" / "intake" / "queries.py").read_text(encoding="utf-8")
        self.assertNotIn("hashlib", source)
        self.assertNotIn("canonicalize", source)
        self.assertNotIn(".save(", source)
        self.assertNotIn(".create(", source)
        self.assertNotIn(".delete(", source)


class SecuritySettingsTests(SimpleTestCase):
    def setUp(self):
        self.base = load_settings("config.settings.base")

    def test_session_authentication_is_the_only_authentication_and_no_token_scheme_exists(self):
        drf = self.base.REST_FRAMEWORK
        self.assertEqual(drf["DEFAULT_AUTHENTICATION_CLASSES"], ["rest_framework.authentication.SessionAuthentication"])
        self.assertEqual(drf["DEFAULT_PERMISSION_CLASSES"], ["rest_framework.permissions.IsAuthenticated"])
        installed = " ".join(self.base.INSTALLED_APPS + self.base.MIDDLEWARE)
        for name in ("authtoken", "corsheaders", "jwt", "simplejwt", "oauth", "knox"):
            self.assertNotIn(name, installed)
        self.assertFalse(hasattr(self.base, "CORS_ALLOWED_ORIGINS"))
        self.assertFalse(hasattr(self.base, "CORS_ALLOW_ALL_ORIGINS"))
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", self.base.MIDDLEWARE)

    def test_the_parser_error_handler_and_proxy_settings(self):
        drf = self.base.REST_FRAMEWORK
        self.assertEqual(drf["DEFAULT_PARSER_CLASSES"], ["apps.api.parsers.BoundedJSONParser"])
        self.assertEqual(drf["EXCEPTION_HANDLER"], "apps.api.errors.api_exception_handler")
        self.assertEqual(drf["NUM_PROXIES"], 0)  # unset would make DRF trust a forgeable X-Forwarded-For
        self.assertEqual(drf["DEFAULT_RENDERER_CLASSES"], ["rest_framework.renderers.JSONRenderer"])

    def test_cookie_attributes(self):
        b = self.base
        self.assertTrue(b.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(b.CSRF_COOKIE_HTTPONLY)
        self.assertEqual((b.SESSION_COOKIE_SAMESITE, b.CSRF_COOKIE_SAMESITE), ("Lax", "Lax"))
        self.assertFalse(getattr(b, "SESSION_COOKIE_DOMAIN", None))  # host-only cookies: no sharing with subdomains

    def test_the_session_lifetime_is_bounded_and_configurable_within_limits(self):
        self.assertEqual(self.base.SESSION_COOKIE_AGE, 8 * 60 * 60)
        self.assertEqual(load_settings("config.settings.base", {"AP_SESSION_AGE_SECONDS": "3600"}).SESSION_COOKIE_AGE, 3600)
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_SESSION_AGE_SECONDS": "60"})
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_SESSION_AGE_SECONDS": "soon"})

    def test_the_proxy_count_is_explicit_and_validated(self):
        self.assertEqual(load_settings("config.settings.base", {"AP_NUM_PROXIES": "2"}).REST_FRAMEWORK["NUM_PROXIES"], 2)
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_NUM_PROXIES": "-1"})

    def test_the_body_limit_is_bounded(self):
        self.assertEqual(self.base.API_MAX_BODY_BYTES, 256 * 1024)
        with self.assertRaises(ImproperlyConfigured):
            load_settings("config.settings.base", {"AP_API_MAX_BODY_BYTES": "10"})

    def test_throttle_rates_exist_for_every_scope_and_login_is_stricter_than_general_use(self):
        rates = self.base.API_THROTTLE_RATES
        self.assertEqual(set(rates), {"login_ip", "login_user", "login_user_hour", "api_user"})
        per_minute = lambda r: int(r.split("/")[0]) * {"sec": 60, "min": 1, "hour": 1 / 60, "day": 1 / 1440}[r.split("/")[1]]  # noqa: E731
        self.assertLess(per_minute(rates["login_user"]), per_minute(rates["api_user"]))
        self.assertLess(per_minute(rates["login_ip"]), per_minute(rates["api_user"]))

    def test_the_throttle_cache_is_explicit_local_memory_and_documented_as_per_process(self):
        self.assertEqual(self.base.CACHES["default"]["BACKEND"], "django.core.cache.backends.locmem.LocMemCache")
        self.assertIn("ONE process", (BACKEND / "apps" / "api" / "throttles.py").read_text(encoding="utf-8"))

    def test_production_keeps_the_secure_baseline_and_inherits_the_api_settings(self):
        prod = load_settings("config.settings.production", PROD)
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)
        self.assertTrue(prod.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(prod.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(prod.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertFalse(prod.DEBUG)
        self.assertEqual(prod.REST_FRAMEWORK["EXCEPTION_HANDLER"], "apps.api.errors.api_exception_handler")
        self.assertEqual(prod.REST_FRAMEWORK["NUM_PROXIES"], 0)
        self.assertEqual(prod.API_THROTTLE_RATES, self.base.API_THROTTLE_RATES)
        self.assertEqual(prod.CSRF_TRUSTED_ORIGINS, [])

    def test_development_does_not_force_secure_cookies_so_localhost_http_works(self):
        dev = load_settings("config.settings.development")
        self.assertFalse(getattr(dev, "SESSION_COOKIE_SECURE", False))
        self.assertFalse(getattr(dev, "CSRF_COOKIE_SECURE", False))
