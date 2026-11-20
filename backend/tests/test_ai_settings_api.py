"""DB-free tests of the AI settings services and of ``/api/ai-settings/extraction/`` through the real URL, authentication, CSRF, throttling and error stack.

The services run for real; only the database touch points are replaced (``tests/ai_settings_support.py``). What needs a real database is in
``tests/test_ai_settings_postgres.py`` and is not run here. No network, no Workbench, no real credential: every "secret" below is a sentinel.
"""

import json
import logging
import os
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.errors import AuthorizationError
from apps.accounts.models import User
from apps.ai_settings import services
from apps.ai_settings.errors import AISettingsConflictError, AISettingsValidationError
from apps.api import views_ai_settings
from config import ai_workbench
from config.intake_extraction import build_catalog
from tests.ai_settings_support import ADMIN_ID, ALL_ROLE_PERMISSIONS, FakeStore, actor, patched_services, stored_row

CATALOG = build_catalog()
PROVIDER = ai_workbench.workbench.PROVIDER_ID
PROFILE = ai_workbench.PROFILE_GPT5
URL = "/api/ai-settings/extraction/"
RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min"}
KEY_SENTINEL = "WORKBENCH-KEY-SENTINEL-7f3a"
PROXY_SENTINEL = "http://proxyuser:PROXY-PASSWORD-SENTINEL-9c@proxy.invalid:8080"
CHARGE_SENTINEL = "CHARGE-CODE-SENTINEL-55"


def ai_body(**changes):
    body = {"extraction_mode": "ai", "provider_id": PROVIDER, "profile_id": PROFILE, "policy_mode": "company_endpoint", "timeout_seconds": 45, "expected_revision": 0}
    body.update(changes)
    return body


def fake_user():
    return User(pk=7, username="synthetic-admin", is_active=True)


# --- the services ---------------------------------------------------------------------------------------


class AuthorizationTests(SimpleTestCase):
    def test_a_user_who_is_not_an_administrator_is_refused_whatever_permissions_they_hold(self):
        store = FakeStore()
        with patched_services(store, actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS)):
            with self.assertRaises(AuthorizationError) as read, self.assertLogs("apps.accounts.authz", "WARNING"):
                services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
            with self.assertRaises(AuthorizationError) as write, self.assertLogs("apps.accounts.authz", "WARNING"):
                services.update_extraction_settings(user=fake_user(), data={"extraction_mode": "deterministic"}, expected_revision=0, catalog=CATALOG)
        self.assertEqual((read.exception.reason, write.exception.reason), ("missing_permission", "missing_permission"))
        self.assertIsNone(store.row)
        self.assertFalse(store.locked)

    def test_the_refusal_comes_before_validation_and_before_the_catalog_is_touched(self):
        store = FakeStore()
        probe = mock.Mock(side_effect=AssertionError("the provider status must not be computed for a refused caller"))
        catalog = build_catalog()
        catalog = type(catalog)(**{**catalog.__dict__, "providers": (type(catalog.providers[0])(PROVIDER, "x", "company_endpoint", probe),)})
        with patched_services(store, actor(is_superuser=False)), self.assertLogs("apps.accounts.authz", "WARNING"):
            with self.assertRaises(AuthorizationError):
                services.update_extraction_settings(user=fake_user(), data={"extraction_mode": "nonsense"}, expected_revision=0, catalog=catalog)
            with self.assertRaises(AuthorizationError):
                services.get_extraction_settings(user=fake_user(), catalog=catalog)
        probe.assert_not_called()

    def test_an_unauthenticated_or_inactive_user_is_refused_by_the_actor_lookup(self):
        for reason in ("not_authenticated", "inactive"):
            with self.subTest(reason), patched_services(FakeStore(), AuthorizationError(reason)):
                with self.assertRaises(AuthorizationError) as caught:
                    services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
                self.assertEqual(caught.exception.reason, reason)

    def test_the_actor_is_reloaded_on_every_call(self):
        with patched_services(FakeStore(), actor(is_superuser=True)) as lookup:
            services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
            services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
        self.assertEqual(lookup.call_count, 2)

    def test_the_worker_read_needs_no_actor_and_returns_none_without_a_row(self):
        with patched_services(FakeStore(), AssertionError("no actor lookup")) as lookup:
            self.assertIsNone(services.load_stored_settings())
        lookup.assert_not_called()


class ReadTests(SimpleTestCase):
    def test_without_a_row_the_default_is_deterministic_revision_zero_not_stored(self):
        with patched_services(FakeStore(), actor(is_superuser=True)):
            view = services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
        self.assertEqual((view.record.stored, view.record.revision, view.record.values.extraction_mode), (False, 0, "deterministic"))
        self.assertEqual(view.apply_mode, "worker_restart_required")

    def test_a_stored_row_is_returned_as_stored(self):
        row = stored_row(revision=4, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=30)
        with patched_services(FakeStore(row), actor(is_superuser=True)):
            view = services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
        self.assertEqual((view.record.stored, view.record.revision, view.record.values.timeout_seconds), (True, 4, 30))


class UpdateTests(SimpleTestCase):
    def update(self, store, data, expected=0, who=None, **kwargs):
        with patched_services(store, who or actor(is_superuser=True)):
            return services.update_extraction_settings(user=fake_user(), data=data, expected_revision=expected, catalog=CATALOG, lease_seconds=60, **kwargs)

    def test_the_first_save_creates_revision_one_and_records_who(self):
        store = FakeStore()
        view = self.update(store, {k: v for k, v in ai_body().items() if k != "expected_revision"})
        self.assertEqual((view.record.revision, view.record.stored, view.record.updated_by_public_id), (1, True, ADMIN_ID))
        self.assertEqual((store.row.extraction_mode, store.row.profile_id, store.row.timeout_seconds), ("ai", PROFILE, 45))
        self.assertTrue(store.locked)

    def test_a_save_bumps_the_revision_and_replaces_every_field(self):
        store = FakeStore(stored_row(revision=3, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=45))
        view = self.update(store, {"extraction_mode": "deterministic"}, expected=3)
        self.assertEqual(view.record.revision, 4)
        row = store.row
        self.assertEqual((row.extraction_mode, row.provider_id, row.profile_id, row.policy_mode, row.timeout_seconds), ("deterministic", None, None, None, None))
        self.assertIn("revision", row.saved_with)

    def test_a_stale_revision_is_a_conflict_and_changes_nothing(self):
        store = FakeStore(stored_row(revision=3))
        for expected in (0, 2, 4):
            with self.subTest(expected), self.assertRaises(AISettingsConflictError):
                self.update(store, {"extraction_mode": "deterministic"}, expected=expected)
        self.assertEqual(store.row.revision, 3)

    def test_expecting_revision_zero_when_a_row_exists_is_a_conflict(self):
        with self.assertRaises(AISettingsConflictError):
            self.update(FakeStore(stored_row(revision=1)), {"extraction_mode": "deterministic"}, expected=0)

    def test_losing_the_race_to_create_the_row_is_a_conflict(self):
        with self.assertRaises(AISettingsConflictError):
            self.update(FakeStore(race=True), {"extraction_mode": "deterministic"})

    def test_an_invalid_body_is_refused_before_any_row_is_read_or_locked(self):
        store = FakeStore(stored_row(revision=1))
        bad = (
            {"extraction_mode": "ai"},
            {"extraction_mode": "ai", **{k: v for k, v in ai_body().items() if k != "expected_revision"}, "policy_mode": "local_only"},
            {"extraction_mode": "ai", **{k: v for k, v in ai_body().items() if k != "expected_revision"}, "profile_id": "nope"},
            {"extraction_mode": "deterministic", "provider_id": PROVIDER},
        )
        for data in bad:
            with self.subTest(data), self.assertRaises(AISettingsValidationError):
                self.update(store, data, expected=1)
        self.assertFalse(store.locked)
        self.assertEqual(store.row.revision, 1)

    def test_a_timeout_the_worker_lease_cannot_cover_is_refused(self):
        data = {k: v for k, v in ai_body(timeout_seconds=60).items() if k != "expected_revision"}
        with self.assertRaises(AISettingsValidationError) as caught:
            self.update(FakeStore(), data)
        self.assertEqual(caught.exception.fields, {"timeout_seconds": ["exceeds_worker_lease"]})

    def test_the_log_line_holds_identifiers_only(self):
        data = {k: v for k, v in ai_body().items() if k != "expected_revision"}
        with mock.patch.dict(os.environ, {"AP_AI_WORKBENCH_API_KEY": KEY_SENTINEL}), self.assertLogs(services.logger, "INFO") as logs:
            self.update(FakeStore(), data)
        text = "\n".join(logs.output)
        self.assertIn(str(ADMIN_ID), text)
        self.assertNotIn(KEY_SENTINEL, text)


class ProviderStatusTests(SimpleTestCase):
    def status(self, environ):
        with mock.patch.dict(os.environ, environ, clear=True), patched_services(FakeStore(), actor(is_superuser=True)):
            view = services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
        return view.providers[0].status

    def test_a_missing_key_is_reported_with_a_fixed_code(self):
        status = self.status({})
        self.assertEqual((status.configured, status.issue), (False, "missing_api_key"))

    def test_a_present_key_is_configured_and_the_key_is_not_in_the_status(self):
        status = self.status({"AP_AI_WORKBENCH_API_KEY": KEY_SENTINEL})
        self.assertEqual((status.configured, status.issue), (True, None))
        self.assertNotIn(KEY_SENTINEL, repr(status))

    def test_an_invalid_optional_setting_is_invalid_configuration_without_its_value(self):
        status = self.status({"AP_AI_WORKBENCH_API_KEY": KEY_SENTINEL, "AP_AI_WORKBENCH_ENDPOINT": "https://evil.invalid/SECRET-HOST", "AP_AI_WORKBENCH_MAX_RETRIES": "many"})
        self.assertEqual((status.configured, status.issue), (False, "invalid_configuration"))
        self.assertNotIn("SECRET-HOST", repr(status))
        self.assertNotIn(KEY_SENTINEL, repr(status))

    def test_a_probe_that_raises_or_lies_is_reported_as_invalid_without_its_text(self):
        for probe in (mock.Mock(side_effect=RuntimeError(KEY_SENTINEL)), mock.Mock(return_value="configured"), mock.Mock(return_value=services.ProviderStatus(True, "something_new"))):
            entry = type(CATALOG.providers[0])(PROVIDER, "x", "company_endpoint", probe)
            catalog = type(CATALOG)(**{**CATALOG.__dict__, "providers": (entry,)})
            with self.subTest(probe), patched_services(FakeStore(), actor(is_superuser=True)), self.assertLogs(services.logger, "ERROR") if isinstance(probe.side_effect, Exception) else mock.MagicMock():
                view = services.get_extraction_settings(user=fake_user(), catalog=catalog)
            self.assertEqual((view.providers[0].status.configured, view.providers[0].status.issue), (False, "invalid_configuration"))


# --- the API --------------------------------------------------------------------------------------------


class ApiCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = APIClient()

    def call(self, method, store, who, data=None, environ=None):
        self.client.force_authenticate(user=fake_user())
        env = {"AP_AI_WORKBENCH_API_KEY": KEY_SENTINEL, "AP_AI_WORKBENCH_PROXY_URL": PROXY_SENTINEL, "AP_AI_WORKBENCH_CHARGE_CODE": CHARGE_SENTINEL}
        env.update(environ or {})
        with mock.patch.dict(os.environ, env), patched_services(store, who):
            response = getattr(self.client, method)(URL, data, format="json") if data is not None else getattr(self.client, method)(URL)
        return response, response.content.decode()


class ApiAuthorizationTests(ApiCase):
    def test_anonymous_requests_are_403_not_authenticated_for_both_methods(self):
        for method in ("get", "put"):
            with self.subTest(method):
                response = getattr(APIClient(enforce_csrf_checks=True), method)(URL, {} if method == "put" else None, format="json")
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))

    def test_a_non_administrator_gets_403_permission_denied_with_no_settings_in_the_body(self):
        store = FakeStore(stored_row(revision=2, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=45))
        for method, data in (("get", None), ("put", ai_body(expected_revision=2))):
            with self.subTest(method), self.assertLogs("apps.accounts.authz", "WARNING"):
                response, text = self.call(method, store, actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS), data)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))
            self.assertNotIn(PROFILE, text)
            self.assertNotIn(PROVIDER, text)
        self.assertEqual(store.row.revision, 2)

    def test_the_administrator_may_read(self):
        response, _ = self.call("get", FakeStore(), actor(is_superuser=True))
        self.assertEqual(response.status_code, 200)

    def test_a_put_without_a_csrf_token_is_refused_before_the_service_runs(self):
        request = APIRequestFactory(enforce_csrf_checks=True).put(URL, ai_body(), format="json")
        request.user = fake_user()  # what AuthenticationMiddleware sets from a valid session
        with mock.patch.object(services, "update_extraction_settings") as service:
            response = views_ai_settings.AIExtractionSettingsView.as_view(catalog=CATALOG, lease_seconds=60)(request)
        self.assertEqual((response.status_code, json.loads(response.rendered_content)["error"]["code"]), (403, "csrf_failed"))
        service.assert_not_called()

    def test_other_methods_are_not_allowed(self):
        self.client.force_authenticate(user=fake_user())
        for method in ("post", "patch", "delete"):
            with self.subTest(method):
                self.assertEqual(getattr(self.client, method)(URL, {}, format="json").status_code, 405)

    def test_responses_are_never_cached(self):
        response, _ = self.call("get", FakeStore(), actor(is_superuser=True))
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_view_fails_closed_without_a_catalog(self):
        request = APIRequestFactory().get(URL)
        request.user = fake_user()
        from rest_framework.test import force_authenticate

        force_authenticate(request, user=fake_user())
        with patched_services(FakeStore(), actor(is_superuser=True)), self.assertLogs("apps.api.errors", "ERROR"):
            response = views_ai_settings.AIExtractionSettingsView.as_view()(request)
        self.assertEqual(response.status_code, 500)


class ApiContractTests(ApiCase):
    def test_get_returns_the_documented_shape_for_a_fresh_installation(self):
        response, _ = self.call("get", FakeStore(), actor(is_superuser=True))
        data = response.json()
        self.assertEqual(set(data), {"settings", "stored", "revision", "updated_at", "updated_by", "apply", "catalog"})
        self.assertEqual(data["settings"], {"extraction_mode": "deterministic", "provider_id": None, "profile_id": None, "policy_mode": None, "timeout_seconds": None})
        self.assertEqual((data["stored"], data["revision"], data["updated_at"], data["updated_by"]), (False, 0, None, None))
        self.assertEqual(data["apply"], "worker_restart_required")
        catalog = data["catalog"]
        self.assertEqual(set(catalog), {"providers", "policy_modes", "timeout"})
        self.assertEqual(catalog["timeout"], {"min": 5, "max": 300, "default": 45})
        self.assertEqual(catalog["policy_modes"], ["local_only", "company_endpoint", "approved_cloud"])
        (provider,) = catalog["providers"]
        self.assertEqual(set(provider), {"provider_id", "label", "locality", "status", "profiles"})
        self.assertEqual(provider["status"], {"configured": True, "issue": None})
        (profile,) = provider["profiles"]
        self.assertEqual(profile["profile_id"], PROFILE)
        self.assertEqual(profile["permitted_policy_modes"], ["company_endpoint", "approved_cloud"])

    def test_the_default_is_deterministic_and_changes_nothing_by_being_read(self):
        store = FakeStore()
        self.call("get", store, actor(is_superuser=True))
        self.assertIsNone(store.row)

    def test_put_saves_and_returns_the_new_state_with_the_restart_notice(self):
        store = FakeStore()
        response, _ = self.call("put", store, actor(is_superuser=True), ai_body())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["revision"], data["stored"], data["apply"], data["updated_by"]), (1, True, "worker_restart_required", str(ADMIN_ID)))
        self.assertEqual(data["settings"], {"extraction_mode": "ai", "provider_id": PROVIDER, "profile_id": PROFILE, "policy_mode": "company_endpoint", "timeout_seconds": 45})

    def test_put_deterministic_needs_no_ai_field(self):
        store = FakeStore(stored_row(revision=1, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=45))
        response, _ = self.call("put", store, actor(is_superuser=True), {"extraction_mode": "deterministic", "expected_revision": 1})
        self.assertEqual((response.status_code, response.json()["settings"]["extraction_mode"], response.json()["revision"]), (200, "deterministic", 2))

    def test_a_stale_revision_is_a_409_with_a_fixed_message(self):
        response, _ = self.call("put", FakeStore(stored_row(revision=5)), actor(is_superuser=True), ai_body(expected_revision=4))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "stale_settings_revision"))
        self.assertEqual(response.json()["error"]["message"], "The settings were changed since they were read.")

    def test_shape_errors_are_400_validation_error_with_codes_and_no_service_call(self):
        sentinel = "SHAPE-SENTINEL-2b"
        bodies = (
            {},
            {"extraction_mode": "ai"},
            ai_body(timeout_seconds="45"),
            ai_body(timeout_seconds=45.5),
            ai_body(timeout_seconds=True),
            ai_body(expected_revision=-1),
            ai_body(expected_revision="0"),
            ai_body(provider_id=5),
            ai_body(api_key=sentinel),
            ai_body(endpoint=sentinel),
            {**ai_body(), "extraction_mode": sentinel * 10},
        )
        for body in bodies:
            with self.subTest(body=list(body)), mock.patch.object(services, "update_extraction_settings") as service:
                self.client.force_authenticate(user=fake_user())
                response = self.client.put(URL, body, format="json")
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
                self.assertNotIn(sentinel, response.content.decode())
                service.assert_not_called()

    def test_domain_refusals_are_400_validation_error_with_field_codes(self):
        expectations = (
            ({"policy_mode": "local_only"}, {"policy_mode": ["policy_forbids_provider"]}),
            ({"provider_id": "other_cloud"}, {"provider_id": ["unknown_provider"]}),
            ({"profile_id": ai_workbench.PROFILE_GPT41}, {"profile_id": ["unknown_profile"]}),
            ({"profile_id": "made-up"}, {"profile_id": ["unknown_profile"]}),
            ({"policy_mode": "open_internet"}, {"policy_mode": ["invalid_choice"]}),
            ({"timeout_seconds": 4}, {"timeout_seconds": ["out_of_range"]}),
            ({"timeout_seconds": 301}, {"timeout_seconds": ["out_of_range"]}),
            ({"timeout_seconds": 60}, {"timeout_seconds": ["exceeds_worker_lease"]}),
            ({"extraction_mode": "auto"}, {"extraction_mode": ["invalid_choice"]}),
        )
        for changes, fields in expectations:
            with self.subTest(changes):
                store = FakeStore()
                response, _ = self.call("put", store, actor(is_superuser=True), ai_body(**changes))
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
                self.assertEqual(response.json()["error"]["details"], {"fields": fields})
                self.assertIsNone(store.row)  # nothing was saved

    def test_deterministic_with_an_ai_field_is_refused(self):
        response, _ = self.call("put", FakeStore(), actor(is_superuser=True), {"extraction_mode": "deterministic", "provider_id": PROVIDER, "expected_revision": 0})
        self.assertEqual(response.json()["error"]["details"], {"fields": {"provider_id": ["not_allowed_in_deterministic_mode"]}})

    def test_there_is_no_fallback_field_and_no_automatic_choice(self):
        for name in ("fallback_provider_id", "fallback_profile_id", "fallback", "auto"):
            with self.subTest(name):
                response, _ = self.call("put", FakeStore(), actor(is_superuser=True), ai_body(**{name: PROVIDER}))
                self.assertEqual(response.json()["error"]["details"], {"fields": {name: ["unknown_field"]}})


class SecretRedactionTests(ApiCase):
    SENTINELS = (KEY_SENTINEL, "PROXY-PASSWORD-SENTINEL-9c", "proxyuser", CHARGE_SENTINEL, "proxy.invalid")

    def assert_clean(self, text):
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_no_response_ever_contains_a_credential_or_a_provider_setting(self):
        store = FakeStore()
        calls = (("get", None), ("put", ai_body()), ("get", None), ("put", ai_body(expected_revision=1, timeout_seconds=30)), ("put", ai_body(expected_revision=99)), ("put", ai_body(profile_id="x")))
        for method, data in calls:
            with self.subTest(method=method, data=data):
                _, text = self.call(method, store, actor(is_superuser=True), data)
                self.assert_clean(text)

    def test_the_response_has_no_field_named_like_a_secret_at_any_depth(self):
        response, _ = self.call("get", FakeStore(), actor(is_superuser=True))

        def names(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        for name in names(response.json()):
            for word in ("key", "secret", "token", "password", "header", "endpoint", "proxy", "charge", "credential"):
                self.assertNotIn(word, name)

    def test_a_key_sent_to_the_api_is_refused_not_stored_and_not_echoed(self):
        store = FakeStore()
        response, text = self.call("put", store, actor(is_superuser=True), ai_body(api_key=KEY_SENTINEL))
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(KEY_SENTINEL, text)
        self.assertIsNone(store.row)

    def test_the_provider_status_never_carries_a_value_even_for_a_broken_environment(self):
        for environ in ({"AP_AI_WORKBENCH_API_KEY": ""}, {"AP_AI_WORKBENCH_ENDPOINT": "https://x.invalid/" + KEY_SENTINEL}, {"AP_AI_WORKBENCH_TIMEOUT_SECONDS": KEY_SENTINEL}):
            with self.subTest(environ):
                response, text = self.call("get", FakeStore(), actor(is_superuser=True), environ=environ)
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.json()["catalog"]["providers"][0]["status"]["issue"], {"missing_api_key", "invalid_configuration"})
                self.assertNotIn(KEY_SENTINEL, text)

    def test_logs_of_a_save_and_a_refusal_hold_no_credential(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("apps.test").debug("start")
            self.call("put", FakeStore(), actor(is_superuser=True), ai_body())
            with self.assertLogs("apps.accounts.authz", "WARNING"):
                self.call("put", FakeStore(), actor(is_superuser=False), ai_body())
        self.assert_clean("\n".join(logs.output))

    def test_the_stored_model_has_no_field_that_could_hold_a_credential(self):
        from apps.ai_settings.models import AIExtractionSettings

        names = {f.name for f in AIExtractionSettings._meta.get_fields()}
        self.assertEqual(names, {"id", "revision", "extraction_mode", "provider_id", "profile_id", "policy_mode", "timeout_seconds", "updated_at", "updated_by_public_id"})
