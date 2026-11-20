"""DB-free tests of the managed AI credential API (AI-SEC-2): routing, session authentication, CSRF, administrator-only access, the request and response contract, the error mapping and the
absence of any secret from a response or a log line. The views, the serializers, the error handler and the services are the real ones; the database touch points and the secret store are the
stand-ins of ``tests/ai_credentials_support.py``."""

import json
import logging
import os
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.errors import AuthorizationError
from apps.accounts.models import User
from apps.api import views_ai_credentials
from apps.secret_store import CODE_BUSY, CODE_DISABLED, CODE_UNAVAILABLE, CODE_UNREADABLE
from tests.ai_credentials_support import (
    ENV_SENTINEL,
    OTHER_REF,
    REF,
    SECRET_ONE,
    SECRET_TWO,
    SENTINELS,
    FakeStore,
    MemoryStore,
    Using,
    actor,
    patched_credentials,
)
from tests.ai_settings_support import ALL_ROLE_PERMISSIONS

COLLECTION = "/api/ai-credentials/"
DETAIL = f"/api/ai-credentials/{REF}/"
RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min"}
STATUS_KEYS = {"reference", "source", "status", "store", "connections", "apply"}


def fake_user():
    return User(pk=7, username="synthetic-admin", is_active=True)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


class ApiCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = APIClient()
        self.db = FakeStore()
        self.store = MemoryStore()
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)
        self.texts = []

    def call(self, method, path, data=None, who=None, store=None, using=None, environ=None):
        """One request through the URL configuration, the real view and the real services. The route's store factory is the composition root's ``build_secret_store``; here it
        returns ``store`` (the disabled backend's class is replaced by a callable returning the stand-in)."""
        self.client.force_authenticate(user=fake_user())
        chosen = self.store if store is None else store
        with (
            mock.patch.dict(os.environ, environ or {}),
            patched_credentials(self.db, actor(is_superuser=True) if who is None else who, using=using),
            mock.patch("config.secret_store.DisabledSecretStore", return_value=chosen),
        ):
            call = getattr(self.client, method)
            response = call(path, data, format="json") if data is not None else call(path)
        text = response.content.decode()
        self.texts.append(text)
        return response, text

    def assertNoLeak(self):
        everything = "\n".join(self.texts + self.capture.lines)
        for secret in SENTINELS:
            self.assertNotIn(secret, everything)


class RoutingTests(ApiCase):
    def test_the_routes_resolve_to_the_two_views(self):
        self.assertIs(resolve(COLLECTION).func.cls, views_ai_credentials.AICredentialCollectionView)
        self.assertIs(resolve(DETAIL).func.cls, views_ai_credentials.AICredentialDetailView)
        self.assertEqual(reverse("ai-credential-collection"), COLLECTION)
        self.assertEqual(reverse("ai-credential-detail", kwargs={"reference": REF}), DETAIL)

    def test_only_the_documented_methods_are_allowed(self):
        for method, path, allowed in (
            ("get", COLLECTION, False), ("put", COLLECTION, False), ("delete", COLLECTION, False), ("post", COLLECTION, True),
            ("post", DETAIL, False), ("patch", DETAIL, False), ("get", DETAIL, True), ("put", DETAIL, True), ("delete", DETAIL, True),
        ):  # fmt: skip
            with self.subTest(method=method, path=path):
                self.client.force_authenticate(user=fake_user())
                response = getattr(self.client, method)(path, {}, format="json")
                self.assertEqual(response.status_code == 405, not allowed)

    def test_responses_are_never_cached(self):
        for method, path, data in (("get", DETAIL, None), ("post", COLLECTION, {"reference": REF, "value": SECRET_ONE}), ("delete", DETAIL, None)):
            response, _ = self.call(method, path, data)
            self.assertIn("no-store", response["Cache-Control"], method)
        response, _ = self.call("get", DETAIL, who=actor(is_superuser=False))
        self.assertIn("no-store", response["Cache-Control"])


class AuthorizationTests(ApiCase):
    def test_anonymous_requests_are_403_not_authenticated_for_every_operation_and_touch_nothing(self):
        for method, path, data in (("get", DETAIL, None), ("post", COLLECTION, {"reference": REF, "value": SECRET_ONE}), ("put", DETAIL, {"value": SECRET_ONE}), ("delete", DETAIL, None)):
            with self.subTest(method):
                response = getattr(APIClient(enforce_csrf_checks=True), method)(path, data, format="json") if data else getattr(APIClient(enforce_csrf_checks=True), method)(path)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))
        self.assertEqual((self.store.calls, self.db.events), ([], []))

    def test_a_non_administrator_gets_403_permission_denied_with_nothing_in_the_body_and_nothing_stored(self):
        self.store.seed(REF, SECRET_ONE)
        before = list(self.store.calls)
        for method, path, data in (("get", DETAIL, None), ("post", COLLECTION, {"reference": REF, "value": SECRET_TWO}), ("put", DETAIL, {"value": SECRET_TWO}), ("delete", DETAIL, None)):
            with self.subTest(method), self.assertLogs("apps.accounts.authz", "WARNING"):
                response, text = self.call(method, path, data, who=actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS))
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))
            self.assertNotIn(REF, text)
            self.assertNotIn("status", response.json())
        self.assertEqual((self.store.calls, self.store.value_of(REF), self.db.events), (before, SECRET_ONE, []))

    def test_an_unauthenticated_lookup_is_403_not_authenticated(self):
        response, _ = self.call("get", DETAIL, who=AuthorizationError("not_authenticated"))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))

    def test_the_administrator_may_use_every_operation(self):
        self.assertEqual(self.call("post", COLLECTION, {"reference": REF, "value": SECRET_ONE})[0].status_code, 201)
        self.assertEqual(self.call("get", DETAIL)[0].status_code, 200)
        self.assertEqual(self.call("put", DETAIL, {"value": SECRET_TWO})[0].status_code, 200)
        self.assertEqual(self.call("delete", DETAIL)[0].status_code, 200)


class CsrfTests(ApiCase):
    def view(self, name):
        return getattr(views_ai_credentials, name).as_view(store_factory=lambda: self.store)

    def test_a_state_changing_request_without_a_csrf_token_is_refused_before_any_service_runs(self):
        for name, method, path, data, service in (
            ("AICredentialCollectionView", "post", COLLECTION, {"reference": REF, "value": SECRET_ONE}, "create_credential"),
            ("AICredentialDetailView", "put", DETAIL, {"value": SECRET_ONE}, "replace_credential"),
            ("AICredentialDetailView", "delete", DETAIL, None, "delete_credential"),
        ):
            with self.subTest(method):
                factory = APIRequestFactory(enforce_csrf_checks=True)
                request = getattr(factory, method)(path, data, format="json") if data else getattr(factory, method)(path)
                request.user = fake_user()  # what AuthenticationMiddleware sets from a valid session
                with mock.patch.object(views_ai_credentials.services, service) as called:
                    response = self.view(name)(request, **({} if name == "AICredentialCollectionView" else {"reference": REF}))
                    body = json.loads(response.rendered_content)
                self.assertEqual((response.status_code, body["error"]["code"]), (403, "csrf_failed"))
                called.assert_not_called()
        self.assertEqual(self.store.calls, [])

    def test_a_read_needs_no_csrf_token(self):
        request = APIRequestFactory(enforce_csrf_checks=True).get(DETAIL)
        request.user = fake_user()
        with patched_credentials(self.db, actor(is_superuser=True)):
            response = self.view("AICredentialDetailView")(request, reference=REF)
        self.assertEqual(response.status_code, 200)


class ContractTests(ApiCase):
    def test_create_returns_201_and_a_status_with_no_value(self):
        response, text = self.call("post", COLLECTION, {"reference": REF, "value": SECRET_ONE})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"reference": REF, "source": "managed", "status": "set", "store": "memory", "connections": [], "apply": "worker_restart_required"})
        self.assertEqual(self.store.value_of(REF), SECRET_ONE)
        self.assertNotIn(SECRET_ONE, text)
        self.assertEqual([e.action for e in self.db.events], ["ai_credential.created"])

    def test_the_whole_lifecycle(self):
        self.assertEqual(self.call("get", DETAIL)[0].json()["status"], "not_set")
        self.assertEqual(self.call("put", DETAIL, {"value": SECRET_TWO})[0].status_code, 404)  # replace never creates
        self.call("post", COLLECTION, {"reference": REF, "value": SECRET_ONE})
        self.assertEqual(self.call("post", COLLECTION, {"reference": REF, "value": SECRET_TWO})[0].status_code, 409)  # create never overwrites
        self.assertEqual(self.store.value_of(REF), SECRET_ONE)
        response, _ = self.call("put", DETAIL, {"value": SECRET_TWO})
        self.assertEqual((response.status_code, set(response.json()), self.store.value_of(REF)), (200, STATUS_KEYS, SECRET_TWO))
        self.assertEqual(self.call("get", DETAIL)[0].json()["status"], "set")
        response, _ = self.call("delete", DETAIL)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "not_set"))
        self.assertEqual(self.call("delete", DETAIL)[0].status_code, 404)
        self.assertEqual([e.action for e in self.db.events], ["ai_credential.created", "ai_credential.replaced", "ai_credential.deleted"])
        self.assertNoLeak()

    def test_a_status_lists_the_connections_that_use_the_credential(self):
        self.store.seed(REF, SECRET_ONE)
        response, _ = self.call("get", DETAIL, using=Using({REF: ("corp-llm", "backup")}))
        self.assertEqual(response.json()["connections"], ["corp-llm", "backup"])

    def test_delete_of_a_credential_in_use_is_409_and_names_only_connection_identifiers(self):
        self.store.seed(REF, SECRET_ONE)
        response, text = self.call("delete", DETAIL, using=Using({REF: ("corp-llm",)}))
        self.assertEqual((response.status_code, response.json()["error"]["code"], response.json()["error"]["details"]), (409, "credential_in_use", {"connections": ["corp-llm"]}))
        self.assertEqual(self.store.value_of(REF), SECRET_ONE)
        self.assertNotIn(SECRET_ONE, text)

    def test_an_unreadable_credential_is_a_status_not_an_error(self):
        response, _ = self.call("get", DETAIL, store=MemoryStore(fail={"get": CODE_UNREADABLE}))
        self.assertEqual((response.status_code, response.json()["status"]), (200, "unreadable"))

    def test_the_four_error_statuses_have_stable_codes_and_fixed_messages(self):
        cases = (
            ("get", DETAIL, None, MemoryStore(fail={"get": CODE_DISABLED}), 503, "secret_store_disabled"),
            ("get", DETAIL, None, MemoryStore(fail={"get": CODE_UNAVAILABLE}), 503, "secret_store_unavailable"),
            ("post", COLLECTION, {"reference": REF, "value": SECRET_ONE}, MemoryStore(fail={"put": CODE_BUSY}), 503, "secret_store_busy"),
            ("delete", DETAIL, None, MemoryStore(), 404, "credential_not_found"),
        )
        for method, path, data, store, status, code in cases:
            with self.subTest(code=code):
                response, text = self.call(method, path, data, store=store)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (status, code))
                self.assertNotIn(REF, text)
                self.assertNotIn("Traceback", text)
        self.assertEqual(self.db.events, [])


class ValidationTests(ApiCase):
    def test_an_invalid_reference_in_the_path_is_a_400_with_a_field_code_and_nothing_is_touched(self):
        for bad in ("AP_SECRET_KEY", "ap_ai_cred_x", "AP_AI_CRED_", "AP_AI_CRED_lower", "AP_AI_CRED_" + "X" * 49, "DJANGO_SECRET_KEY"):
            for method, data in (("get", None), ("put", {"value": SECRET_ONE}), ("delete", None)):
                with self.subTest(reference=bad, method=method):
                    response, text = self.call(method, f"/api/ai-credentials/{bad}/", data)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "validation_error")
                    self.assertEqual(response.json()["error"]["details"], {"fields": {"reference": ["invalid_reference"]}})
        self.assertEqual((self.store.calls, self.db.events), ([], []))

    def test_an_invalid_reference_in_the_body_is_a_400(self):
        for bad in ("AP_SECRET_KEY", "", "AP_AI_CRED_x", "AP_AI_CRED_A B"):
            response, _ = self.call("post", COLLECTION, {"reference": bad, "value": SECRET_ONE})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("reference", response.json()["error"]["details"]["fields"])
        self.assertEqual(self.store.calls, [])

    def test_an_invalid_value_is_a_400_and_the_value_is_never_echoed(self):
        for bad in (" " + SECRET_ONE, SECRET_ONE + " ", SECRET_ONE + "\n" + SECRET_TWO, "", SECRET_ONE * 200):
            for method, path, data in (("post", COLLECTION, {"reference": REF, "value": bad}), ("put", DETAIL, {"value": bad})):
                with self.subTest(method=method, value=len(bad)):
                    response, text = self.call(method, path, data)
                    self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
                    self.assertIn("value", response.json()["error"]["details"]["fields"])
                    self.assertNotIn(SECRET_ONE, text)
                    self.assertNotIn(SECRET_TWO, text)
        self.assertEqual((self.store.calls, self.store.items, self.db.events), ([], {}, []))
        self.assertNoLeak()

    def test_a_value_that_is_not_a_string_is_refused(self):
        for bad in (5, True, None, ["x"], {"a": 1}):
            response, _ = self.call("put", DETAIL, {"value": bad})
            self.assertEqual(response.status_code, 400, repr(bad))
        self.assertEqual(self.store.calls, [])

    def test_unknown_or_missing_fields_are_refused_and_no_identity_or_provider_can_be_sent(self):
        for method, path, data, field in (
            ("post", COLLECTION, {"reference": REF, "value": SECRET_ONE, "provider": "x"}, "provider"),
            ("post", COLLECTION, {"reference": REF, "value": SECRET_ONE, "actor": "someone"}, "actor"),
            ("put", DETAIL, {"value": SECRET_ONE, "reference": OTHER_REF}, "reference"),
            ("put", DETAIL, {"value": SECRET_ONE, "replace": True}, "replace"),
            ("post", COLLECTION, {"value": SECRET_ONE}, "reference"),
            ("post", COLLECTION, {"reference": REF}, "value"),
            ("put", DETAIL, {}, "value"),
        ):
            with self.subTest(field=field, method=method):
                response, _ = self.call(method, path, data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json()["error"]["details"]["fields"])
        self.assertEqual((self.store.calls, self.db.events), ([], []))

    def test_a_body_that_is_not_an_object_is_malformed_json(self):
        self.client.force_authenticate(user=fake_user())
        with patched_credentials(self.db, actor(is_superuser=True)):
            response = self.client.post(COLLECTION, "[1, 2]", content_type="application/json")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "malformed_json"))


class RedactionTests(ApiCase):
    def test_a_secret_is_in_no_response_and_no_log_line_for_any_outcome(self):
        self.call("post", COLLECTION, {"reference": REF, "value": SECRET_ONE})
        self.call("post", COLLECTION, {"reference": REF, "value": SECRET_TWO})
        self.call("put", DETAIL, {"value": SECRET_TWO})
        self.call("put", f"/api/ai-credentials/{OTHER_REF}/", {"value": SECRET_TWO})
        self.call("post", COLLECTION, {"reference": REF, "value": " " + SECRET_TWO})
        self.call("get", DETAIL, store=MemoryStore(fail={"get": CODE_UNAVAILABLE}).seed(REF, SECRET_ONE))
        self.call("delete", DETAIL, who=actor(is_superuser=False))
        self.call("delete", DETAIL)
        self.assertNoLeak()

    def test_an_unexpected_error_inside_the_store_becomes_secret_store_unavailable_with_no_detail(self):
        exploding = MemoryStore()

        def boom(*args, **kwargs):
            raise RuntimeError(SECRET_ONE)

        exploding._put = boom
        response, text = self.call("post", COLLECTION, {"reference": REF, "value": SECRET_ONE}, store=exploding)
        # the store's own guard turns the failure into a fixed code before it reaches the API
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "secret_store_unavailable"))
        self.assertEqual(self.db.events, [])
        self.assertNoLeak()

    def test_a_failure_outside_the_store_is_a_bare_500_that_logs_the_class_only(self):
        with (
            mock.patch.object(views_ai_credentials.services, "get_status", side_effect=RuntimeError(SECRET_ONE)),
            self.assertLogs("apps.api.errors", "ERROR") as logged,
        ):
            response, text = self.call("get", DETAIL)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (500, "internal_error"))
        self.assertEqual(logged.output, ["ERROR:apps.api.errors:Unexpected API error (RuntimeError)."])
        self.assertNotIn(SECRET_ONE, text)

    def test_the_value_is_marked_sensitive_for_error_reports(self):
        factory = APIRequestFactory()
        for name, method, path, data, kwargs in (
            ("AICredentialCollectionView", "post", COLLECTION, {"reference": REF, "value": SECRET_ONE}, {}),
            ("AICredentialDetailView", "put", DETAIL, {"value": SECRET_ONE}, {"reference": REF}),
        ):
            request = getattr(factory, method)(path, data, format="json")
            from rest_framework.test import force_authenticate

            force_authenticate(request, user=fake_user())
            with patched_credentials(self.db, actor(is_superuser=True)):
                getattr(views_ai_credentials, name).as_view(store_factory=lambda: self.store)(request, **kwargs)
            self.assertEqual(tuple(request.sensitive_post_parameters), ("value",), name)

    def test_the_views_fail_closed_without_a_store_factory(self):
        request = APIRequestFactory().get(DETAIL)
        from rest_framework.test import force_authenticate

        force_authenticate(request, user=fake_user())
        with patched_credentials(self.db, actor(is_superuser=True)), self.assertLogs("apps.api.errors", "ERROR"):
            response = views_ai_credentials.AICredentialDetailView.as_view()(request, reference=REF)
        self.assertEqual(response.status_code, 500)

    def test_no_environment_value_is_ever_used_or_returned(self):
        response, text = self.call("get", DETAIL, environ={REF: ENV_SENTINEL})
        self.assertEqual(response.json()["status"], "not_set")
        self.assertNoLeak()
