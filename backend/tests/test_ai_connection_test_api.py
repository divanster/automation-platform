"""DB-free tests of ``POST /api/ai-settings/connections/<connection_id>/test/`` (AI-CON-TEST-1) through the real URL, authentication, CSRF, throttling, view, service, composition,
connection gateway, provider adapters and error stack. The ONLY things replaced are the database touch points of the settings services (``tests/ai_settings_support.py``) and the real
network: the ``send`` of the real transports is replaced by a scripted fake, so no name is resolved and no socket is opened. No real endpoint, no real credential: every secret is a sentinel."""

import ast
import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.errors import AuthorizationError
from apps.accounts.models import User
from apps.ai_gateway.errors import ERROR_CODES
from apps.ai_gateway.transport import HttpResponse, TransportError
from apps.ai_settings import connection_test
from apps.ai_settings.errors import ConnectionTestBusyError, ConnectionTestRefusedError
from apps.ai_settings.rules import PROVIDER_ISSUES, parse_stored_connection
from apps.api import views_ai_connection_test
from config import ai_connection_test, ai_workbench
from config.ai_guarded_transport import GuardedHttpTransport
from config.ai_http_transport import StdlibHttpsTransport
from tests.ai_connections_support import (
    CLOUD_ENDPOINT,
    CORP_ENDPOINT,
    OPERATOR_ENV,
    REF_CORP,
    SECRET_A,
    SECRET_B,
    SECRET_WORKBENCH,
    all_connections,
    cloud_connection,
    corp_connection,
    local_openai_connection,
    ollama_connection,
    workbench_connection,
)
from tests.ai_credentials_support import MemoryStore
from tests.ai_gateway_support import PROMPT_SENTINEL, SECRET_SENTINEL
from tests.ai_ollama_support import answer as ollama_answer
from tests.ai_settings_support import ADMIN_ID, ALL_ROLE_PERMISSIONS, FakeStore, actor, patched_services, stored_row
from tests.ai_workbench_support import FakeTransport
from tests.ai_workbench_support import answer as chat_answer
from tests.ai_workbench_support import status
from tests.settings_loader import load_settings

BACKEND = Path(__file__).resolve().parent.parent
PROFILE_WORKBENCH = ai_workbench.PROFILE_GPT5
RATES = {
    "login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min",
    "ai_connection_test": "1000/min", "ai_connection_test_hour": "1000/hour", "ai_connection_test_all": "1000/min",
}  # fmt: skip
PROVIDER_TEXT = "PROVIDER-SAID-SENTINEL-c41d"  # text a provider might send back; it must never reach a response or a log line
OUTPUT_SENTINEL = "OUTPUT-SENTINEL-8e02"
MANAGED_SECRET = "MANAGED-SENTINEL-3b77"
SENTINELS = (SECRET_A, SECRET_B, SECRET_WORKBENCH, SECRET_SENTINEL, PROVIDER_TEXT, OUTPUT_SENTINEL, MANAGED_SECRET, PROMPT_SENTINEL)
GOOD = '{"status": "ok"}'
RESULT_KEYS = {"connection_id", "profile_id", "provider_type", "locality", "ok", "code", "retryable", "duration_ms"}


def url(connection_id):
    return f"/api/ai-settings/connections/{connection_id}/test/"


def admin_user(pk=7):
    return User(pk=pk, username=f"synthetic-admin-{pk}", is_active=True, is_superuser=True)


def plain_user(pk=8):
    return User(pk=pk, username=f"synthetic-user-{pk}", is_active=True)


def saved(connections=None, **kwargs):
    return stored_row(revision=3, connections=list(all_connections() if connections is None else connections), **kwargs)


# One entry per provider type: (the connection to test, the profile to test, a function from the model's text to the provider's wire answer).
PROVIDERS = {
    "workbench": (workbench_connection(), PROFILE_WORKBENCH, lambda text: chat_answer(text)),
    "openai_compatible": (corp_connection(), "corp-small", lambda text: chat_answer(text, model="corp-model-8b")),
    "ollama": (ollama_connection(), "llama", lambda text: ollama_answer(text)),
}


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


@contextlib.contextmanager
def fake_network(transport):
    """Replace the ``send`` of the two REAL transport classes with the scripted ``transport``: everything above the socket (adapter, guarded-transport construction, gateway) is real."""

    def send(self, request):
        return transport.send(request)

    with mock.patch.object(StdlibHttpsTransport, "send", send), mock.patch.object(GuardedHttpTransport, "send", send):
        yield


class ApiCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = APIClient()
        self.capture = Capture()
        root = logging.getLogger()
        level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, level)
        self.texts = []

    def call(self, connection_id, body, *, row=None, who=None, user=None, script=(), environ=None, secret_store=None):
        """One request through the URL configuration and everything behind it. Returns ``(response, store, transport)``."""
        store = FakeStore(saved() if row is None else row)
        transport = FakeTransport(*script)
        self.client.force_authenticate(user=user or admin_user())
        env = dict(OPERATOR_ENV if environ is None else environ)
        patches = [mock.patch.dict(os.environ, env), patched_services(store, who or actor(is_superuser=True)), fake_network(transport)]
        if secret_store is not None:
            patches.append(mock.patch("config.secret_store.DisabledSecretStore", return_value=secret_store))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            response = self.client.post(url(connection_id), body, format="json")
        self.texts.append(response.content.decode())
        return response, store, transport

    def assertNoLeak(self):
        everything = "\n".join(self.texts + self.capture.lines)
        for secret in SENTINELS:
            self.assertNotIn(secret, everything)
        for probe_text in (ai_connection_test.PROBE_INSTRUCTIONS, ai_connection_test.PROBE_INPUT):
            self.assertNotIn(probe_text, "\n".join(self.texts))

    def assertUntouched(self, store, row_revision=3):
        """Nothing was saved, locked or audited."""
        self.assertEqual((store.row.revision, store.row.saved_with, store.events, store.locked), (row_revision, None, [], False))


class RoutingTests(ApiCase):
    def test_the_route_resolves_to_the_view_and_only_post_is_allowed(self):
        self.assertIs(resolve(url("corp-llm")).func.cls, views_ai_connection_test.AIConnectionTestView)
        self.assertEqual(reverse("ai-connection-test", kwargs={"connection_id": "corp-llm"}), url("corp-llm"))
        self.client.force_authenticate(user=admin_user())
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method):
                self.assertEqual(getattr(self.client, method)(url("corp-llm"), {}, format="json").status_code, 405)

    def test_the_view_fails_closed_without_a_prober(self):
        request = APIRequestFactory().post(url("corp-llm"), {"profile_id": "corp-small"}, format="json")
        request.user = admin_user()
        store = FakeStore(saved())
        with patched_services(store, actor(is_superuser=True)):
            response = views_ai_connection_test.AIConnectionTestView.as_view()(request, connection_id="corp-llm")
            body = json.loads(response.rendered_content)
        self.assertEqual((response.status_code, body["error"]["code"]), (500, "internal_error"))
        self.assertEqual(store.events, [])


class SuccessTests(ApiCase):
    def test_each_provider_type_is_tested_with_exactly_one_request_and_a_sanitized_result(self):
        for name, (connection, profile, wire) in PROVIDERS.items():
            with self.subTest(name):
                response, store, transport = self.call(connection["connection_id"], {"profile_id": profile}, script=[wire(GOOD)])
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(set(body), RESULT_KEYS)
                self.assertEqual(
                    {k: body[k] for k in ("connection_id", "profile_id", "provider_type", "locality", "ok", "code", "retryable")},
                    {"connection_id": connection["connection_id"], "profile_id": profile, "provider_type": connection["provider_type"], "locality": connection["locality"], "ok": True, "code": None, "retryable": False},
                )
                self.assertIsInstance(body["duration_ms"], int)
                self.assertEqual(len(transport.requests), 1)
                self.assertUntouched(store)
        self.assertNoLeak()

    def test_only_the_selected_model_of_the_selected_connection_is_called(self):
        # corp-llm has two profiles; the test names corp-large, so the request must carry that model and no other, and only that endpoint is used.
        response, _, transport = self.call("corp-llm", {"profile_id": "corp-large"}, script=[chat_answer(GOOD, model="corp-model-70b")])
        self.assertEqual(response.status_code, 200)
        (request,) = transport.requests
        self.assertEqual(json.loads(request.body)["model"], "corp-model-70b")
        self.assertEqual(urlsplit(request.url).hostname, "llm.corp.example.invalid")

    def test_the_credential_is_resolved_backend_only_and_reaches_only_the_request_header(self):
        response, _, transport = self.call("corp-llm", {"profile_id": "corp-small"}, script=[chat_answer(GOOD)])
        self.assertEqual(response.status_code, 200)
        self.assertIn(SECRET_A, " ".join(value for _, value in transport.requests[0].headers))
        self.assertNoLeak()

    def test_a_managed_credential_comes_from_the_secret_store_and_never_from_the_environment(self):
        connection = corp_connection(credential_source="managed")
        store = MemoryStore().seed(REF_CORP, MANAGED_SECRET)
        environ = {**OPERATOR_ENV, REF_CORP: SECRET_A}  # the environment holds ANOTHER value for the same name: it must not be used
        response, _, transport = self.call("corp-llm", {"profile_id": "corp-small"}, row=saved([connection]), environ=environ, secret_store=store, script=[chat_answer(GOOD)])
        self.assertEqual(response.status_code, 200)
        headers = " ".join(value for _, value in transport.requests[0].headers)
        self.assertIn(MANAGED_SECRET, headers)
        self.assertNotIn(SECRET_A, headers)
        self.assertNoLeak()

    def test_a_local_openai_compatible_connection_needs_no_key(self):
        response, _, transport = self.call("local-server", {"profile_id": "local-chat"}, script=[chat_answer(GOOD, model="local-model:8b")])
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True))
        self.assertNotIn("Authorization", dict(transport.requests[0].headers))

    def test_the_probe_is_bounded_in_time_and_output_and_makes_a_single_attempt(self):
        for name, (connection, profile, wire) in PROVIDERS.items():
            with self.subTest(name):
                _, _, transport = self.call(connection["connection_id"], {"profile_id": profile}, script=[wire(GOOD)])
                request = transport.requests[0]
                self.assertLessEqual(request.read_timeout_seconds, ai_connection_test.TEST_TIMEOUT_SECONDS)
                body = json.loads(request.body)
                if name == "openai_compatible":
                    self.assertEqual(body["max_tokens"], ai_connection_test.TEST_MAX_OUTPUT_TOKENS)
                if name == "ollama":
                    self.assertEqual(body["options"]["num_predict"], ai_connection_test.TEST_MAX_OUTPUT_TOKENS)
                self.assertNotIn("INPUT-SENTINEL", request.body.decode())

    def test_a_cloud_connection_is_tested_only_under_the_saved_approved_cloud_policy(self):
        row = saved(mode="ai", provider="openai_compatible", profile="vendor-chat", policy="approved_cloud", timeout=30, connection="vendor")
        response, _, transport = self.call("vendor", {"profile_id": "vendor-chat"}, row=row, script=[chat_answer(GOOD, model="vendor-model")])
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True))
        self.assertEqual(urlsplit(transport.requests[0].url).hostname, "api.vendor.example.invalid")
        self.assertNoLeak()

    def test_a_cloud_connection_is_refused_by_the_gateway_when_no_policy_approves_it_and_nothing_is_sent(self):
        for row in (saved(), saved(mode="ai", provider="openai_compatible", profile="corp-small", policy="company_endpoint", timeout=30, connection="corp-llm")):
            with self.subTest(row.extraction_mode):
                response, store, transport = self.call("vendor", {"profile_id": "vendor-chat"}, row=row)
                self.assertEqual(response.status_code, 200)
                self.assertEqual((response.json()["ok"], response.json()["code"], response.json()["retryable"]), (False, "policy_violation", False))
                self.assertEqual(transport.requests, [])
                self.assertUntouched(store)

    def test_a_saved_local_only_policy_refuses_a_company_connection_before_any_provider_code_runs(self):
        row = saved(mode="ai", provider="ollama", profile="llama", policy="local_only", timeout=30, connection="ollama-local")
        response, _, transport = self.call("corp-llm", {"profile_id": "corp-small"}, row=row)
        self.assertEqual((response.json()["ok"], response.json()["code"]), (False, "policy_violation"))
        self.assertEqual(transport.requests, [])

    def test_testing_never_changes_the_selected_connection_or_any_setting(self):
        row = saved(mode="ai", provider="openai_compatible", profile="corp-small", policy="company_endpoint", timeout=30, connection="corp-llm")
        before = dict(row.__dict__)
        response, store, _ = self.call("ollama-local", {"profile_id": "llama"}, row=row, script=[ollama_answer(GOOD)])  # a connection that is NOT the selected one
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.row.__dict__, before)
        self.assertUntouched(store)


class FailureOutcomeTests(ApiCase):
    """A provider that cannot answer is a 200 with ``ok: false`` and one stable code: the endpoint did its job. One request is made, never a retry."""

    def expect(self, script_for, code, retryable):
        for name, (connection, profile, wire) in PROVIDERS.items():
            with self.subTest(name, code=code):
                response, store, transport = self.call(connection["connection_id"], {"profile_id": profile}, script=[script_for(wire)])
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual((body["ok"], body["code"], body["retryable"]), (False, code, retryable))
                self.assertIn(code, ERROR_CODES)
                self.assertEqual(len(transport.requests), 1)  # a retry would have asked the exhausted script for a second answer
                self.assertUntouched(store)
        self.assertNoLeak()

    def test_a_timeout_is_reported_as_timeout(self):
        self.expect(lambda wire: TransportError("timeout"), "timeout", True)

    def test_a_refused_connection_is_provider_unavailable(self):
        self.expect(lambda wire: TransportError("connect"), "provider_unavailable", True)

    def test_a_rate_limit_is_reported_and_not_retried(self):
        self.expect(lambda wire: status(429, PROVIDER_TEXT.encode()), "rate_limit", True)

    def test_a_rejected_key_is_an_authentication_failure(self):
        for http_status in (401, 403):
            with self.subTest(http_status):
                self.expect(lambda wire: status(http_status, PROVIDER_TEXT.encode()), "authentication_failure", False)

    def test_an_unknown_model_is_unsupported_model(self):
        self.expect(lambda wire: status(404, PROVIDER_TEXT.encode()), "unsupported_model", False)

    def test_a_server_error_is_a_provider_failure_without_its_text(self):
        for name, (connection, profile, _) in PROVIDERS.items():
            with self.subTest(name):
                response, _, _ = self.call(connection["connection_id"], {"profile_id": profile}, script=[status(500, PROVIDER_TEXT.encode())])
                body = response.json()
                self.assertEqual((response.status_code, body["ok"]), (200, False))
                self.assertIn(body["code"], ERROR_CODES)
        self.assertNoLeak()

    def test_a_tls_or_destination_refusal_is_a_non_retryable_provider_error(self):
        for kind in ("tls", "destination", "protocol"):
            self.expect(lambda wire, kind=kind: TransportError(kind), "provider_error", False)

    def test_an_answer_that_does_not_satisfy_the_probe_is_invalid_structured_output_and_is_not_echoed(self):
        bad = (f'{{"status": "{OUTPUT_SENTINEL}"}}', f"not json {PROVIDER_TEXT}", f'{{"status": "ok", "extra": "{OUTPUT_SENTINEL}"}}', "[]")
        for text in bad:
            self.expect(lambda wire, text=text: wire(text), "invalid_structured_output", False)

    def test_the_provider_answer_is_not_stored_logged_or_returned_on_success_either(self):
        response, store, _ = self.call("corp-llm", {"profile_id": "corp-small"}, script=[chat_answer(GOOD, model="corp-model-8b", system_fingerprint=OUTPUT_SENTINEL)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), RESULT_KEYS)
        self.assertUntouched(store)
        self.assertNoLeak()

    def test_a_foreign_exception_from_the_transport_is_classified_and_its_text_dropped(self):
        response, _, _ = self.call("corp-llm", {"profile_id": "corp-small"}, script=[RuntimeError(SECRET_A + PROVIDER_TEXT)])
        body = response.json()
        self.assertEqual((response.status_code, body["ok"]), (200, False))
        self.assertIn(body["code"], ERROR_CODES)
        self.assertNoLeak()


class RefusalTests(ApiCase):
    """A connection that cannot be tested as saved is refused BEFORE any request is made."""

    def refused(self, connection_id, body, status_code, code, details=None, **kwargs):
        response, store, transport = self.call(connection_id, body, **kwargs)
        error = response.json()["error"]
        self.assertEqual((response.status_code, error["code"]), (status_code, code))
        if details is not None:
            self.assertEqual(error["details"], details)
        self.assertEqual(transport.requests, [])
        self.assertUntouched(store)
        self.assertIn("no-store", response["Cache-Control"])
        return error

    def test_a_disabled_connection_is_refused(self):
        row = saved([corp_connection(enabled=False)])
        error = self.refused("corp-llm", {"profile_id": "corp-small"}, 409, "connection_disabled", row=row)
        self.assertEqual(error["message"], "The connection is disabled and cannot be tested.")

    def test_a_missing_credential_is_refused_for_both_sources(self):
        environ = {k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP}
        self.refused("corp-llm", {"profile_id": "corp-small"}, 409, "connection_not_usable", {"issue": "missing_api_key"}, environ=environ)
        managed = saved([corp_connection(credential_source="managed")])
        self.refused("corp-llm", {"profile_id": "corp-small"}, 409, "connection_not_usable", {"issue": "missing_api_key"}, row=managed, secret_store=MemoryStore())
        self.assertNoLeak()

    def test_a_managed_credential_is_never_looked_up_in_the_environment_when_the_store_is_unavailable(self):
        managed = saved([corp_connection(credential_source="managed")])  # the environment HAS the key; the (disabled) store does not
        self.refused("corp-llm", {"profile_id": "corp-small"}, 409, "connection_not_usable", {"issue": "invalid_configuration"}, row=managed)
        self.assertNoLeak()

    def test_a_workbench_connection_without_any_key_is_refused(self):
        environ = {k: v for k, v in OPERATOR_ENV.items() if k != "AP_AI_WORKBENCH_API_KEY"}
        self.refused("workbench", {"profile_id": PROFILE_WORKBENCH}, 409, "connection_not_usable", {"issue": "missing_api_key"}, environ=environ)

    def test_an_endpoint_the_operator_has_not_approved_is_refused(self):
        environ = {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://other.example.invalid"}
        self.refused("corp-llm", {"profile_id": "corp-small"}, 409, "connection_not_usable", {"issue": "endpoint_not_approved"}, environ=environ)
        self.refused("vendor", {"profile_id": "vendor-chat"}, 409, "connection_not_usable", {"issue": "endpoint_not_approved"}, environ=environ)
        self.refused("ollama-local", {"profile_id": "llama"}, 409, "connection_not_usable", {"issue": "endpoint_not_approved"}, environ={**OPERATOR_ENV, "AP_AI_OLLAMA_ALLOWED_ORIGINS": "http://127.0.0.1:9999"})

    def test_an_invalid_or_forbidden_endpoint_is_refused_with_a_fixed_issue(self):
        cases = (
            corp_connection(endpoint="http://llm.corp.example.invalid/v1"),  # plain http to a remote host
            corp_connection(endpoint="https://user:pw@llm.corp.example.invalid/v1"),  # credentials in the URL
            corp_connection(endpoint="not a url"),
            local_openai_connection(endpoint="http://169.254.169.254/v1"),  # a metadata address declared as local
            ollama_connection(endpoint="https://ollama.corp.example.invalid"),  # Ollama is loopback only
        )
        for connection in cases:
            with self.subTest(connection["endpoint"]):
                profile = connection["profiles"][0]["profile_id"]
                response, store, transport = self.call(connection["connection_id"], {"profile_id": profile}, row=saved([connection]))
                error = response.json()["error"]
                self.assertEqual((response.status_code, error["code"]), (409, "connection_not_usable"))
                self.assertIn(error["details"]["issue"], PROVIDER_ISSUES)
                self.assertEqual(transport.requests, [])
        self.assertNoLeak()

    def test_a_proxy_is_never_used_for_a_host_name_endpoint(self):
        # The operator set a proxy for the OpenAI-compatible provider. A host-name endpoint is refused (a proxy would resolve the name where no address rule applies), here already when the
        # provider's settings are loaded, so no transport is built and no connection or socket is ever opened.
        environ = {**OPERATOR_ENV, "AP_AI_OPENAI_PROXY_URL": "http://proxy.example.invalid:8080"}
        store = FakeStore(saved())
        self.client.force_authenticate(user=admin_user())
        with (
            mock.patch.dict(os.environ, environ),
            patched_services(store, actor(is_superuser=True)),
            mock.patch("config.ai_guarded_transport.http.client.HTTPSConnection", side_effect=AssertionError("a proxy connection was built")) as proxy,
            mock.patch("config.ai_guarded_transport.socket.create_connection", side_effect=AssertionError("a socket was opened")) as sock,
        ):
            response = self.client.post(url("corp-llm"), {"profile_id": "corp-small"}, format="json")
        self.assertEqual((response.status_code, response.json()["error"]["code"], response.json()["error"]["details"]), (409, "connection_not_usable", {"issue": "invalid_configuration"}))
        proxy.assert_not_called()
        sock.assert_not_called()

    def test_a_private_or_metadata_address_behind_a_cloud_name_is_refused_by_the_real_transport(self):
        from config.ai_guarded_transport import GuardedHttpTransport as Real

        opened = []
        real = Real(resolver=lambda host, port: ["169.254.169.254"], connection_factory=lambda *args: opened.append(args))
        row = saved(mode="ai", provider="openai_compatible", profile="vendor-chat", policy="approved_cloud", timeout=30, connection="vendor")
        prober = ai_connection_test.build_prober(OPERATOR_ENV, transport=real)
        outcome = None
        with patched_services(FakeStore(row), actor(is_superuser=True)):
            connection = next(c for c in connection_test.services.read_connection_test_context().connections if c.connection_id == "vendor")
            outcome = prober(connection, "vendor-chat", "approved_cloud")
        self.assertEqual((outcome.ok, outcome.code), (False, "provider_error"))
        self.assertEqual(opened, [])  # nothing was connected to

    def test_an_unknown_connection_and_the_legacy_provider_are_404(self):
        for connection_id in ("nope", "legacy", "x" * 300, "CORP-LLM", "a b"):
            with self.subTest(connection_id[:10]):
                self.refused(connection_id, {"profile_id": "corp-small"}, 404, "not_found")
        self.refused("corp-llm", {"profile_id": "corp-small"}, 404, "not_found", row=saved([]))
        self.client.force_authenticate(user=admin_user())
        self.assertEqual(self.client.post("/api/ai-settings/connections/..%2fcorp-llm/test/", {"profile_id": "corp-small"}, format="json").status_code, 404)  # a path segment cannot hold a slash

    def test_an_unknown_profile_is_a_400_with_a_code_only(self):
        error = self.refused("corp-llm", {"profile_id": "vendor-chat"}, 400, "validation_error", {"fields": {"profile_id": ["unknown_profile"]}})
        self.assertNotIn("vendor-chat", json.dumps(error))

    def test_the_body_carries_a_profile_identifier_and_nothing_else(self):
        for body in ({}, {"profile_id": None}, {"profile_id": 5}, {"profile_id": "corp-small", "endpoint": "https://evil.invalid"}, {"profile_id": "corp-small", "api_key": SECRET_A},
                     {"profile_id": "corp-small", "prompt": PROMPT_SENTINEL}, {"profile_id": "corp-small", "timeout_seconds": 999}, {"profile_id": "x" * 100}):  # fmt: skip
            with self.subTest(sorted(body)):
                response, store, transport = self.call("corp-llm", body)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
                self.assertEqual(transport.requests, [])
        self.assertNoLeak()

    def test_a_body_that_is_not_json_is_malformed(self):
        self.client.force_authenticate(user=admin_user())
        with patched_services(FakeStore(saved()), actor(is_superuser=True)):
            response = self.client.post(url("corp-llm"), "not json", content_type="application/json")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "malformed_json"))

    def test_an_unreadable_stored_connection_is_not_found(self):
        row = saved([{"connection_id": "corp-llm", "garbage": True}, ollama_connection()])
        self.refused("corp-llm", {"profile_id": "corp-small"}, 404, "not_found", row=row)

    def test_a_prober_that_breaks_its_contract_is_a_bare_500_and_never_a_guess(self):
        for broken in (lambda *a: "ok", lambda *a: connection_test.ProbeOutcome(True, "timeout", False, 1), lambda *a: connection_test.ProbeOutcome(False, "made_up", False, 1), mock.Mock(side_effect=RuntimeError(SECRET_A))):
            with self.subTest(broken), patched_services(FakeStore(saved()), actor(is_superuser=True)), self.assertLogs(level="ERROR") as logs, self.assertRaises(connection_test.ProbeFailedError):
                connection_test.run_connection_test(user=admin_user(), connection_id="corp-llm", profile_id="corp-small", prober=broken)
            self.assertNotIn(SECRET_A, "\n".join(logs.output))


class SecurityTests(ApiCase):
    def test_an_anonymous_request_is_403_not_authenticated_and_nothing_runs(self):
        store = FakeStore(saved())
        transport = FakeTransport()
        with patched_services(store, actor(is_superuser=True)) as lookup, fake_network(transport):
            response = APIClient(enforce_csrf_checks=True).post(url("corp-llm"), {"profile_id": "corp-small"}, format="json")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))
        lookup.assert_not_called()
        self.assertEqual(transport.requests, [])

    def test_a_non_administrator_is_refused_before_anything_is_validated_or_looked_up(self):
        # Whatever they send (a valid body, an invalid one, an unknown connection) the answer is the same 403: no 400, no 404, no hint which connections exist.
        who = actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS)
        for connection_id, body in (("corp-llm", {"profile_id": "corp-small"}), ("corp-llm", {"nonsense": 1}), ("nope", {"profile_id": "x"}), ("corp-llm", {"profile_id": "corp-small", "endpoint": "x"})):
            with self.subTest(connection_id=connection_id, body=sorted(body)), self.assertLogs("apps.accounts.authz", "WARNING"):
                response, store, transport = self.call(connection_id, body, who=who, user=plain_user())
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))
            self.assertEqual(response.json()["error"].get("details"), None)
            self.assertEqual(transport.requests, [])
            self.assertUntouched(store)
        self.assertNoLeak()

    def test_a_non_administrator_cannot_even_read_the_credential_or_the_store(self):
        store = MemoryStore().seed(REF_CORP, MANAGED_SECRET)
        with self.assertLogs("apps.accounts.authz", "WARNING"):
            response, _, _ = self.call("corp-llm", {"profile_id": "corp-small"}, who=actor(is_superuser=False), user=plain_user(), row=saved([corp_connection(credential_source="managed")]), secret_store=store)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(store.calls, [])

    def test_an_unauthenticated_or_inactive_actor_lookup_is_refused(self):
        for reason in ("not_authenticated", "inactive"):
            with self.subTest(reason):
                response, _, transport = self.call("corp-llm", {"profile_id": "corp-small"}, who=AuthorizationError(reason))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(transport.requests, [])

    def test_a_post_without_a_csrf_token_is_refused_before_any_service_runs(self):
        request = APIRequestFactory(enforce_csrf_checks=True).post(url("corp-llm"), {"profile_id": "corp-small"}, format="json")
        request.user = admin_user()  # what AuthenticationMiddleware sets from a valid session
        prober = mock.Mock(side_effect=AssertionError("a provider was called without a CSRF token"))
        with mock.patch.object(views_ai_connection_test.connection_test, "run_connection_test") as service, mock.patch.object(views_ai_connection_test.connection_test, "authorize") as authorize:
            response = views_ai_connection_test.AIConnectionTestView.as_view(prober=prober)(request, connection_id="corp-llm")
            body = json.loads(response.rendered_content)
        self.assertEqual((response.status_code, body["error"]["code"]), (403, "csrf_failed"))
        service.assert_not_called()
        authorize.assert_not_called()
        prober.assert_not_called()

    def test_every_response_is_never_cached(self):
        cases = (
            ("corp-llm", {"profile_id": "corp-small"}, [chat_answer(GOOD)], admin_user()),
            ("nope", {"profile_id": "corp-small"}, [], admin_user()),
            ("corp-llm", {}, [], admin_user()),
            ("corp-llm", {"profile_id": "corp-small"}, [], plain_user()),
        )
        for connection_id, body, script, user in cases:
            with self.subTest(connection_id=connection_id, user=user.pk):
                who = actor(is_superuser=user.is_superuser)
                with mock.patch("apps.accounts.authz.logger"):
                    response, _, _ = self.call(connection_id, body, who=who, user=user, script=script)
                self.assertIn("no-store", response["Cache-Control"])

    def test_the_throttle_is_strict_and_counts_every_attempt(self):
        rates = {**RATES, "ai_connection_test": "3/min"}
        with override_settings(API_THROTTLE_RATES=rates):
            codes = [self.call("corp-llm", {"profile_id": "corp-small"}, script=[chat_answer(GOOD)])[0].status_code for _ in range(4)]
            self.assertEqual(codes, [200, 200, 200, 429])
            response, _, transport = self.call("corp-llm", {"profile_id": "corp-small"})
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (429, "throttled"))
            self.assertIn("Retry-After", response)
            self.assertEqual(transport.requests, [])  # a throttled request reaches no provider

    def test_the_shipped_rates_are_strict(self):
        per_minute = lambda rate: int(rate.split("/")[0]) * {"sec": 60, "min": 1, "hour": 1 / 60}[rate.split("/")[1]]  # noqa: E731
        shipped = load_settings("config.settings.base").API_THROTTLE_RATES
        self.assertLessEqual(per_minute(shipped["ai_connection_test"]), 10)
        self.assertLess(per_minute(shipped["ai_connection_test"]), per_minute(shipped["api_user"]) / 50)
        self.assertLessEqual(per_minute(shipped["ai_connection_test_hour"]), 1)
        self.assertLessEqual(per_minute(shipped["ai_connection_test_all"]), 20)

    def test_a_throttled_request_for_a_non_administrator_is_refused_with_a_403_first(self):
        # The authentication and permission order is DRF's: a user who is not an administrator is a 403 from the service, and does not use up the administrators' shared ceiling.
        rates = {**RATES, "ai_connection_test": "50/min", "ai_connection_test_all": "2/min"}
        with override_settings(API_THROTTLE_RATES=rates), mock.patch("apps.accounts.authz.logger"):
            for _ in range(6):
                response, _, _ = self.call("corp-llm", {"profile_id": "corp-small"}, who=actor(is_superuser=False), user=plain_user())
                self.assertEqual(response.status_code, 403)
            ok = [self.call("corp-llm", {"profile_id": "corp-small"}, script=[chat_answer(GOOD)])[0].status_code for _ in range(2)]
            self.assertEqual(ok, [200, 200])
            third = self.call("corp-llm", {"profile_id": "corp-small"}, user=admin_user(9))[0]  # another administrator: the shared ceiling applies
            self.assertEqual(third.status_code, 429)

    def test_logs_and_responses_hold_no_secret_endpoint_reference_or_provider_text(self):
        scenarios = (
            ("corp-llm", {"profile_id": "corp-small"}, [chat_answer(GOOD, model="corp-model-8b")], None),
            ("corp-llm", {"profile_id": "corp-small"}, [status(401, PROVIDER_TEXT.encode())], None),
            ("corp-llm", {"profile_id": "corp-small"}, [TransportError("timeout")], None),
            ("workbench", {"profile_id": PROFILE_WORKBENCH}, [status(429, PROVIDER_TEXT.encode())], None),
        )
        for scenario in scenarios:
            connection_id, body, script, row = scenario
            self.call(connection_id, body, script=script, row=row)
        everything = "\n".join(self.texts + self.capture.lines)
        for hidden in (CORP_ENDPOINT, CLOUD_ENDPOINT, REF_CORP, "llm.corp.example.invalid", "api.workbench.kpmg"):
            self.assertNotIn(hidden, everything)
        self.assertNoLeak()
        self.assertTrue(any("AI connection test" in line and str(ADMIN_ID) in line for line in self.capture.lines))


class ConcurrencyTests(ApiCase):
    """At most ``max_concurrent`` probes run at once in a process; the next is refused immediately with a 429 and nothing is read or sent for it."""

    def connection(self):
        return parse_stored_connection(corp_connection())

    def test_a_burst_beyond_the_cap_is_refused_at_once_and_the_slot_is_released_afterwards(self):
        entered, gate, calls = threading.Event(), threading.Event(), []

        class Blocking(FakeTransport):
            def send(self, request):
                calls.append(request)
                entered.set()
                gate.wait(10)
                return super().send(request)

        transport = Blocking(chat_answer(GOOD), chat_answer(GOOD))
        prober = ai_connection_test.build_prober(OPERATOR_ENV, transport=transport, max_concurrent=1)
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(prober(self.connection(), "corp-small", "company_endpoint")))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            with self.assertRaises(ConnectionTestBusyError):
                prober(self.connection(), "corp-small", "company_endpoint")
            self.assertLess(time.monotonic() - started, 1.0)  # it did not wait for the running probe
            self.assertEqual(len(calls), 1)  # and sent nothing
        finally:
            gate.set()
            worker.join(10)
        self.assertEqual([o.ok for o in outcomes], [True])
        self.assertTrue(prober(self.connection(), "corp-small", "company_endpoint").ok)  # the slot came back

    def test_the_slot_is_released_when_a_probe_is_refused_or_fails(self):
        prober = ai_connection_test.build_prober({k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP}, transport=FakeTransport(), max_concurrent=1)
        for _ in range(3):
            with self.assertRaises(ConnectionTestRefusedError):  # not usable: raised inside the slot
                prober(self.connection(), "corp-small", "company_endpoint")
        failing = ai_connection_test.build_prober(OPERATOR_ENV, transport=FakeTransport(RuntimeError(SECRET_A), RuntimeError(SECRET_A), RuntimeError(SECRET_A)), max_concurrent=1)
        for _ in range(3):
            self.assertFalse(failing(self.connection(), "corp-small", "company_endpoint").ok)

    def test_the_cap_is_two_per_process_by_default_and_the_api_answers_429_busy(self):
        self.assertEqual(ai_connection_test.MAX_CONCURRENT_TESTS, 2)
        busy = mock.Mock(side_effect=ConnectionTestBusyError())
        request = APIRequestFactory().post(url("corp-llm"), {"profile_id": "corp-small"}, format="json")
        request.user = admin_user()
        store = FakeStore(saved())
        with patched_services(store, actor(is_superuser=True)):
            response = views_ai_connection_test.AIConnectionTestView.as_view(prober=busy)(request, connection_id="corp-llm")
            body = json.loads(response.rendered_content)
        self.assertEqual((response.status_code, body["error"]["code"]), (429, "connection_test_busy"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertUntouched(store)


class DesignTests(SimpleTestCase):
    """What the endpoint must not be able to do is checked in the source, so a later edit cannot add it unnoticed."""

    FILES = (BACKEND / "apps" / "ai_settings" / "connection_test.py", BACKEND / "config" / "ai_connection_test.py", BACKEND / "apps" / "api" / "views_ai_connection_test.py")

    def imports(self, path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * node.level) + (node.module or ""))
        return names

    def test_the_test_has_no_path_to_a_workflow_a_case_a_revision_a_task_the_audit_log_or_the_queue(self):
        forbidden = ("apps.cases", "apps.tasks", "apps.intake", "apps.audit", "workflows", "apps.module_host", "apps.modules", "django.db", "apps.secret_store")
        for path in self.FILES:
            with self.subTest(path.name):
                for name in self.imports(path):
                    self.assertFalse(name.startswith(forbidden), name)
                source = path.read_text(encoding="utf-8")
                for token in (".objects", ".save(", ".create(", ".delete(", "atomic", "record_event", "enqueue", "select_for_update", "os.environ[", "subprocess"):
                    self.assertNotIn(token, source)

    def test_the_apps_stay_free_of_the_ai_gateway(self):
        for path in (BACKEND / "apps" / "ai_settings" / "connection_test.py", BACKEND / "apps" / "api" / "views_ai_connection_test.py"):
            self.assertFalse({n for n in self.imports(path) if n.startswith(("apps.ai_gateway", "config"))}, path.name)

    def test_the_failure_codes_are_exactly_the_gateways_stable_codes(self):
        self.assertEqual(connection_test.FAILURE_CODES, ERROR_CODES)

    def test_the_provider_answer_cannot_leave_the_prober(self):
        source = (BACKEND / "config" / "ai_connection_test.py").read_text(encoding="utf-8")
        self.assertIn("gateway.generate(request, policy=policy)", source)
        self.assertNotIn("= gateway.generate", source)  # the response object is never bound to a name

    def test_the_composition_reads_no_environment_when_imported_and_never_names_a_default_fallback(self):
        source = (BACKEND / "config" / "ai_connection_test.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", source.split("def build_prober")[0].split('"""', 2)[2])  # nothing at module level
        for word in ("fallback_to", "except ImportError", "getenv"):
            self.assertNotIn(word, source)

    def test_every_provider_is_limited_to_one_attempt_by_the_environment_copy(self):
        self.assertEqual(ai_connection_test._NO_RETRIES, {"AP_AI_WORKBENCH_MAX_RETRIES": "0", "AP_AI_OPENAI_MAX_RETRIES": "0", "AP_AI_OLLAMA_MAX_RETRIES": "0"})
        with mock.patch.dict(os.environ, {"AP_AI_OPENAI_MAX_RETRIES": "4"}):
            prober_env = dict(os.environ)
            prober_env.update(ai_connection_test._NO_RETRIES)
        self.assertEqual(prober_env["AP_AI_OPENAI_MAX_RETRIES"], "0")
        self.assertEqual(os.environ.get("AP_AI_OPENAI_MAX_RETRIES"), None)  # the process environment itself is never modified

    def test_the_probe_is_a_fixed_synthetic_request_that_holds_no_company_data(self):
        self.assertIn("JSON", ai_connection_test.PROBE_INSTRUCTIONS)
        self.assertLess(len(ai_connection_test.PROBE_INSTRUCTIONS) + len(ai_connection_test.PROBE_INPUT), 300)
        self.assertEqual(ai_connection_test.TEST_TIMEOUT_SECONDS, 20.0)
        self.assertLessEqual(ai_connection_test.TEST_MAX_RESPONSE_BYTES, 64 * 1024)
