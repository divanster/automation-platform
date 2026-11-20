"""DB-free tests of ``config/ai_connections.py``: how a stored connection becomes a provider, a profile and a gateway, how credential references are resolved, the status probe, and
that no existing endpoint, locality or network rule is weakened. The adapters, the policy rule and the schema validation are the REAL ones; the transport is a fake that records the
request, so nothing is resolved or connected and no real credential exists."""

import dataclasses
import re
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyContext, PolicyMode
from apps.ai_settings.rules import validate_connections
from config import ai_connections
from config.ai_guarded_transport import GuardedHttpTransport
from config.intake_extraction import build_catalog
from tests import ai_ollama_support
from tests.ai_connections_support import (
    OPERATOR_ENV,
    REF_CORP,
    SECRET_A,
    SECRET_B,
    SECRET_WORKBENCH,
    cloud_connection,
    corp_connection,
    local_openai_connection,
    ollama_connection,
    workbench_connection,
)
from tests.ai_gateway_support import make_request
from tests.ai_workbench_support import FakeTransport, answer, status

BACKEND = Path(__file__).resolve().parent.parent
CATALOG = build_catalog()
ALL_SECRETS = (SECRET_A, SECRET_B, SECRET_WORKBENCH)


def conn(raw):
    (connection,) = validate_connections([raw], CATALOG)
    return connection


def gateway_for(raw, profile_id, environ=None, transport=None):
    connection = conn(raw)
    transport = FakeTransport(answer('{"name": "x"}')) if transport is None else transport
    gateway = ai_connections.build_connection_gateway(connection, profile_id, OPERATOR_ENV if environ is None else environ, transport=transport)
    return connection, gateway, transport


def generate(connection, gateway, profile_id, policy=None):
    policy = policy or ai_connections.policy_for(connection, PolicyMode.APPROVED_CLOUD)
    return gateway.generate(make_request(profile_id=profile_id), policy=policy)


class ProviderSelectionTests(SimpleTestCase):
    def test_a_company_openai_compatible_connection_calls_its_own_endpoint_model_and_key(self):
        connection, gateway, transport = gateway_for(corp_connection(), "corp-small")
        response = generate(connection, gateway, "corp-small")
        self.assertEqual(response.output["name"], "x")
        (request,) = transport.requests
        self.assertEqual(request.url, "https://llm.corp.example.invalid:443/v1/chat/completions")
        self.assertEqual(transport.body()["model"], "corp-model-8b")
        self.assertIn(SECRET_A, repr(request.headers))
        self.assertNotIn(SECRET_B, repr(request.headers))
        self.assertNotIn(SECRET_WORKBENCH, repr(request.headers))

    def test_the_model_follows_the_selected_profile_and_nothing_else(self):
        for profile_id, model in (("corp-small", "corp-model-8b"), ("corp-large", "corp-model-70b")):
            with self.subTest(profile_id):
                connection, gateway, transport = gateway_for(corp_connection(), profile_id)
                generate(connection, gateway, profile_id)
                self.assertEqual(transport.body()["model"], model)
                self.assertEqual([p.profile_id for p in [gateway.profile(profile_id)]], [profile_id])

    def test_the_gateway_has_one_profile_only_so_there_is_no_model_fallback(self):
        connection, gateway, _ = gateway_for(corp_connection(), "corp-small")
        with self.assertRaises(AIGatewayError) as caught:
            gateway.profile("corp-large")
        self.assertEqual(caught.exception.reason, "unknown_profile")

    def test_an_unknown_profile_is_a_configuration_error(self):
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.build_connection_gateway(conn(corp_connection()), "nope", OPERATOR_ENV, transport=FakeTransport())
        self.assertEqual((caught.exception.code, caught.exception.reason), ("configuration_error", "unknown_profile"))

    def test_a_cloud_connection_calls_the_vendor_with_its_own_key(self):
        connection, gateway, transport = gateway_for(cloud_connection(), "vendor-chat")
        generate(connection, gateway, "vendor-chat")
        self.assertEqual(transport.last.url, "https://api.vendor.example.invalid:443/v1/chat/completions")
        self.assertIn(SECRET_B, repr(transport.last.headers))
        self.assertNotIn(SECRET_A, repr(transport.last.headers))

    def test_a_local_openai_compatible_connection_needs_no_key_and_sends_none(self):
        connection, gateway, transport = gateway_for(local_openai_connection(), "local-chat", environ={**OPERATOR_ENV, "AP_AI_OPENAI_API_KEY": SECRET_A})
        generate(connection, gateway, "local-chat", PolicyContext(PolicyMode.LOCAL_ONLY))
        self.assertEqual(transport.last.url, "http://127.0.0.1:8000/v1/chat/completions")
        self.assertNotIn(SECRET_A, repr(transport.last.headers))  # the global variable is never used for a stored connection
        self.assertNotIn("Authorization", dict(transport.last.headers))

    def test_an_ollama_connection_calls_the_native_chat_endpoint_with_its_model(self):
        transport = FakeTransport(ai_ollama_support.answer('{"name": "x"}'))
        connection, gateway, transport = gateway_for(ollama_connection(), "llama", transport=transport)
        generate(connection, gateway, "llama", PolicyContext(PolicyMode.LOCAL_ONLY))
        self.assertEqual(transport.last.url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(transport.body()["model"], "llama3.1:8b")
        self.assertIs(transport.body()["stream"], False)

    def test_two_connections_of_one_type_are_independent(self):
        first = corp_connection()
        second = corp_connection(connection_id="corp-b", credential_ref="AP_AI_CRED_OTHER", profiles=[{"profile_id": "b-chat", "model_id": "model-b"}])
        env = {**OPERATOR_ENV, "AP_AI_CRED_OTHER": SECRET_B}
        _, gateway_a, transport_a = gateway_for(first, "corp-small", env)
        _, gateway_b, transport_b = gateway_for(second, "b-chat", env)
        generate(conn(first), gateway_a, "corp-small")
        generate(conn(second), gateway_b, "b-chat")
        self.assertIn(SECRET_A, repr(transport_a.last.headers))
        self.assertIn(SECRET_B, repr(transport_b.last.headers))
        self.assertEqual((transport_a.body()["model"], transport_b.body()["model"]), ("corp-model-8b", "model-b"))

    def test_the_workbench_without_a_reference_still_reads_its_own_variable_exactly_as_before(self):
        connection, gateway, transport = gateway_for(workbench_connection(), "workbench-gpt5")
        generate(connection, gateway, "workbench-gpt5", PolicyContext(PolicyMode.COMPANY_ENDPOINT))
        self.assertIn(SECRET_WORKBENCH, repr(transport.last.headers))
        self.assertIn("api.workbench.kpmg", transport.last.url)

    def test_a_workbench_connection_with_a_reference_uses_that_key(self):
        env = {**OPERATOR_ENV, "AP_AI_WORKBENCH_API_KEY": "LEGACY-SENTINEL-1", "AP_AI_CRED_WORKBENCH_B": SECRET_B}
        connection, gateway, transport = gateway_for(workbench_connection(credential_ref="AP_AI_CRED_WORKBENCH_B"), "workbench-gpt5", env)
        generate(connection, gateway, "workbench-gpt5", PolicyContext(PolicyMode.COMPANY_ENDPOINT))
        self.assertIn(SECRET_B, repr(transport.last.headers))
        self.assertNotIn("LEGACY-SENTINEL-1", repr(transport.last.headers))

    def test_the_real_guarded_transport_is_built_by_default_without_any_connection(self):
        for raw, profile in ((corp_connection(), "corp-small"), (ollama_connection(), "llama"), (workbench_connection(), "workbench-gpt5")):
            with self.subTest(raw["connection_id"]):
                with mock.patch.object(GuardedHttpTransport, "send", side_effect=AssertionError("no network")):
                    gateway = ai_connections.build_connection_gateway(conn(raw), profile, OPERATOR_ENV)
                self.assertEqual(gateway.profile(profile).profile_id, profile)

    def test_the_process_environment_is_never_modified(self):
        env = dict(OPERATOR_ENV)
        before = dict(env)
        gateway_for(corp_connection(), "corp-small", env)
        self.assertEqual(env, before)
        self.assertNotIn("AP_AI_OPENAI_ENDPOINT", env)


class PolicyEnforcementTests(SimpleTestCase):
    def test_local_only_refuses_a_company_connection_before_any_request_is_made(self):
        connection, gateway, transport = gateway_for(corp_connection(), "corp-small")
        with self.assertRaises(AIGatewayError) as caught:
            generate(connection, gateway, "corp-small", PolicyContext(PolicyMode.LOCAL_ONLY))
        self.assertEqual((caught.exception.code, caught.exception.reason), ("policy_violation", "locality_not_allowed"))
        self.assertEqual(transport.requests, [])

    def test_company_endpoint_refuses_a_cloud_connection_before_any_request_is_made(self):
        connection, gateway, transport = gateway_for(cloud_connection(), "vendor-chat")
        with self.assertRaises(AIGatewayError):
            generate(connection, gateway, "vendor-chat", PolicyContext(PolicyMode.COMPANY_ENDPOINT))
        self.assertEqual(transport.requests, [])

    def test_a_cloud_connection_is_not_approved_by_the_mode_alone(self):
        connection, gateway, transport = gateway_for(cloud_connection(), "vendor-chat")
        with self.assertRaises(AIGatewayError) as caught:
            generate(connection, gateway, "vendor-chat", PolicyContext(PolicyMode.APPROVED_CLOUD))
        self.assertEqual(caught.exception.reason, "cloud_provider_not_approved")
        self.assertEqual(transport.requests, [])

    def test_policy_for_approves_a_cloud_provider_only_for_a_cloud_connection(self):
        cloud = ai_connections.policy_for(conn(cloud_connection()), PolicyMode.APPROVED_CLOUD)
        self.assertEqual(cloud.approved_cloud_providers, frozenset({"openai_compatible"}))
        for raw in (corp_connection(), local_openai_connection(), ollama_connection(), workbench_connection()):
            self.assertEqual(ai_connections.policy_for(conn(raw), PolicyMode.APPROVED_CLOUD).approved_cloud_providers, frozenset())

    def test_a_local_connection_may_run_under_every_mode(self):
        for mode in PolicyMode:
            with self.subTest(mode):
                connection, gateway, transport = gateway_for(ollama_connection(), "llama", transport=FakeTransport(ai_ollama_support.answer('{"name": "x"}')))
                generate(connection, gateway, "llama", ai_connections.policy_for(connection, mode))
                self.assertEqual(len(transport.requests), 1)

    def test_the_permitted_modes_come_from_the_gateways_own_rule(self):
        self.assertEqual(ai_connections.permitted_policy_modes(DataLocality.LOCAL), ("local_only", "company_endpoint", "approved_cloud"))
        self.assertEqual(ai_connections.permitted_policy_modes(DataLocality.COMPANY_ENDPOINT), ("company_endpoint", "approved_cloud"))
        self.assertEqual(ai_connections.permitted_policy_modes(DataLocality.CLOUD), ("approved_cloud",))


class EndpointSecurityTests(SimpleTestCase):
    """The operator, not the stored settings, decides what an endpoint may be. None of the AI-CON-1 and AI-CON-2 protections is weaker for a stored connection."""

    def fails(self, raw, profile, environ, expected_reason=None):
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.build_connection_gateway(conn(raw), profile, environ, transport=FakeTransport())
        if expected_reason is not None:
            self.assertEqual(caught.exception.reason, expected_reason)
        return caught.exception

    def test_a_stored_endpoint_the_operator_has_not_approved_is_refused(self):
        env = {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://other.example.invalid"}
        self.fails(corp_connection(), "corp-small", env, "openai_endpoint_not_approved")

    def test_a_stored_endpoint_cannot_widen_an_empty_allowlist(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k != "AP_AI_OPENAI_ALLOWED_ORIGINS"}
        self.fails(corp_connection(), "corp-small", env, "missing_ap_ai_openai_allowed_origins")
        env = {k: v for k, v in OPERATOR_ENV.items() if k != "AP_AI_OLLAMA_ALLOWED_ORIGINS"}
        self.fails(ollama_connection(), "llama", env, "missing_ap_ai_ollama_allowed_origins")

    def test_a_company_connection_needs_the_operators_company_ranges(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k != "AP_AI_OPENAI_COMPANY_CIDRS"}
        self.fails(corp_connection(), "corp-small", env, "missing_ap_ai_openai_company_cidrs")

    def test_a_company_address_literal_outside_the_operators_ranges_or_in_a_denied_range_is_refused(self):
        raw = corp_connection(endpoint="https://169.254.169.254/v1")
        env = {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://169.254.169.254"}
        self.fails(raw, "corp-small", env)
        self.fails(raw, "corp-small", {**env, "AP_AI_OPENAI_COMPANY_CIDRS": "169.254.0.0/16"})
        outside = corp_connection(endpoint="https://203.0.113.5/v1")
        self.fails(outside, "corp-small", {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://203.0.113.5"})

    def test_a_proxy_is_refused_for_a_host_name_endpoint_and_for_a_local_one_and_ollama(self):
        self.fails(corp_connection(), "corp-small", {**OPERATOR_ENV, "AP_AI_OPENAI_PROXY_URL": "http://proxy.example.invalid:3128"}, "proxy_not_allowed_for_hostname_endpoint")
        self.fails(cloud_connection(), "vendor-chat", {**OPERATOR_ENV, "AP_AI_OPENAI_PROXY_URL": "http://proxy.example.invalid:3128"}, "proxy_not_allowed_for_hostname_endpoint")
        self.fails(ollama_connection(), "llama", {**OPERATOR_ENV, "AP_AI_OLLAMA_PROXY_URL": "http://proxy.example.invalid:3128"}, "proxy_not_allowed_for_local")
        # a local connection never uses the operator's proxy: it is dropped for it, not applied
        gateway = ai_connections.build_connection_gateway(conn(local_openai_connection()), "local-chat", {**OPERATOR_ENV, "AP_AI_OPENAI_PROXY_URL": "http://proxy.example.invalid:3128"}, transport=FakeTransport())
        self.assertEqual(gateway.profile("local-chat").profile_id, "local-chat")

    def test_the_operators_origin_list_is_only_narrowed_for_a_locality_never_widened(self):
        env = ai_connections.connection_environ(conn(corp_connection()), "m", OPERATOR_ENV)
        self.assertEqual(env["AP_AI_OPENAI_ALLOWED_ORIGINS"], "https://llm.corp.example.invalid,https://api.vendor.example.invalid")  # the loopback origin is dropped
        local = ai_connections.connection_environ(conn(local_openai_connection()), "m", OPERATOR_ENV)
        self.assertEqual(local["AP_AI_OPENAI_ALLOWED_ORIGINS"], "http://127.0.0.1:8000")
        self.assertEqual((local["AP_AI_OPENAI_COMPANY_CIDRS"], local["AP_AI_OPENAI_PROXY_URL"]), ("", ""))
        original = set(OPERATOR_ENV["AP_AI_OPENAI_ALLOWED_ORIGINS"].split(","))
        for raw in (corp_connection(), cloud_connection(), local_openai_connection()):
            kept = set(ai_connections.connection_environ(conn(raw), "m", OPERATOR_ENV)["AP_AI_OPENAI_ALLOWED_ORIGINS"].split(","))
            self.assertLessEqual(kept, original)

    def test_a_malformed_operator_origin_is_still_an_error(self):
        self.fails(corp_connection(), "corp-small", {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "not a url"})

    def test_ollama_is_always_loopback_whatever_the_operator_lists(self):
        env = {**OPERATOR_ENV, "AP_AI_OLLAMA_ALLOWED_ORIGINS": "http://10.0.0.5:11434"}
        self.fails(ollama_connection(), "llama", env)

    def test_the_destination_rules_are_the_adapters_own(self):
        text = (BACKEND / "config" / "ai_connections.py").read_text(encoding="utf-8")
        for name in ("origin_problem", "load_openai_compatible_settings", "load_ollama_settings", "load_workbench_settings"):
            self.assertIn(name, text)
        for forbidden in ("socket", "urllib", "http.client", "requests", "ssl", "getaddrinfo"):
            self.assertNotRegex(text, rf"\b{forbidden}\b")


class CredentialReferenceTests(SimpleTestCase):
    def test_a_reference_is_resolved_from_the_environment_only(self):
        self.assertIsNone(ai_connections.resolve_credential(None, {}))
        self.assertEqual(ai_connections.resolve_credential(REF_CORP, {REF_CORP: f"  {SECRET_A}  "}), SECRET_A)

    def test_an_unset_or_empty_variable_is_an_error_never_an_unauthenticated_call(self):
        for env in ({}, {REF_CORP: ""}, {REF_CORP: "   "}):
            with self.subTest(env), self.assertRaises(AIGatewayError) as caught:
                ai_connections.resolve_credential(REF_CORP, env)
            self.assertEqual(caught.exception.reason, "missing_credential_reference")
            self.assertNotIn(REF_CORP, str(caught.exception))

    def test_a_reference_outside_the_prefix_is_refused_even_if_it_reached_the_resolver(self):
        from apps.ai_settings.rules import ConnectionValues

        for ref in ("AP_SECRET_KEY", "AP_AI_OPENAI_API_KEY", "PATH", "AP_AI_CRED"):
            with self.subTest(ref), self.assertRaises(AIGatewayError) as caught:
                ai_connections.resolve_credential(ref, {ref: "TOPSECRET"})
            self.assertEqual(caught.exception.reason, "invalid_credential_reference")
            self.assertNotIn("TOPSECRET", str(caught.exception))
        forged = ConnectionValues("x", "X", "openai_compatible", "company_endpoint", "https://llm.corp.example.invalid/v1", "AP_SECRET_KEY", True, ())
        with self.assertRaises(AIGatewayError):
            ai_connections.connection_environ(forged, "m", {**OPERATOR_ENV, "AP_SECRET_KEY": "TOPSECRET"})

    def test_a_connection_whose_variable_is_not_set_stops_before_a_gateway_exists(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP}
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.build_connection_gateway(conn(corp_connection()), "corp-small", env, transport=FakeTransport())
        self.assertEqual(caught.exception.reason, "missing_credential_reference")

    def test_a_local_connection_that_names_a_reference_needs_that_variable(self):
        raw = local_openai_connection(credential_ref="AP_AI_CRED_LOCAL")
        with self.assertRaises(AIGatewayError):
            ai_connections.build_connection_gateway(conn(raw), "local-chat", OPERATOR_ENV, transport=FakeTransport())
        gateway = ai_connections.build_connection_gateway(conn(raw), "local-chat", {**OPERATOR_ENV, "AP_AI_CRED_LOCAL": SECRET_A}, transport=FakeTransport())
        self.assertEqual(gateway.profile("local-chat").profile_id, "local-chat")

    def test_no_credential_is_in_a_profile_a_repr_or_an_error(self):
        connection, gateway, _ = gateway_for(corp_connection(), "corp-small")
        profile = gateway.profile("corp-small")
        blob = repr(dataclasses.asdict(profile)) + repr(profile) + repr(gateway) + repr(connection)
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, blob)
        bad_env = {**OPERATOR_ENV, REF_CORP: "has a space"}  # an invalid key value
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.build_connection_gateway(connection, "corp-small", bad_env, transport=FakeTransport())
        self.assertNotIn("has a space", str(caught.exception) + repr(caught.exception.reason))

    def test_the_failure_of_a_call_does_not_carry_the_key(self):
        connection, gateway, _ = gateway_for(corp_connection(), "corp-small", transport=FakeTransport(status(401, b'{"error":"bad key"}')))
        with self.assertRaises(AIGatewayError) as caught:
            generate(connection, gateway, "corp-small")
        self.assertEqual(caught.exception.code, "authentication_failure")
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, str(caught.exception) + repr(caught.exception.__dict__))


class StatusProbeTests(SimpleTestCase):
    def status(self, raw, environ=None):
        return ai_connections.connection_status(conn(raw), OPERATOR_ENV if environ is None else environ)

    def test_a_usable_connection_is_configured(self):
        for raw in (workbench_connection(), corp_connection(), cloud_connection(), local_openai_connection(), ollama_connection()):
            with self.subTest(raw["connection_id"]):
                result = self.status(raw)
                self.assertEqual((result.configured, result.issue), (True, None))

    def test_a_missing_credential_is_reported_with_a_fixed_code(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP}
        result = self.status(corp_connection(), env)
        self.assertEqual((result.configured, result.issue), (False, "missing_api_key"))
        env = {k: v for k, v in OPERATOR_ENV.items() if k != "AP_AI_WORKBENCH_API_KEY"}
        self.assertEqual(self.status(workbench_connection(), env).issue, "missing_api_key")

    def test_an_endpoint_the_operator_has_not_approved_is_reported(self):
        result = self.status(corp_connection(), {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://other.example.invalid"})
        self.assertEqual((result.configured, result.issue), (False, "endpoint_not_approved"))
        result = self.status(ollama_connection(endpoint="http://127.0.0.1:9999"))
        self.assertEqual((result.configured, result.issue), (False, "endpoint_not_approved"))

    def test_any_other_problem_is_invalid_configuration_and_names_nothing(self):
        for environ in ({**OPERATOR_ENV, "AP_AI_OPENAI_TIMEOUT_SECONDS": SECRET_A}, {**OPERATOR_ENV, REF_CORP: "has a space"}, {**OPERATOR_ENV, "AP_AI_OPENAI_MAX_RETRIES": "many"}):
            with self.subTest(list(environ)[-1]):
                result = self.status(corp_connection(), environ)
                self.assertEqual((result.configured, result.issue), (False, "invalid_configuration"))
                self.assertNotIn(SECRET_A, repr(result))

    def test_the_probe_makes_no_network_call_and_builds_no_transport(self):
        with mock.patch.object(GuardedHttpTransport, "__init__", side_effect=AssertionError("no transport")):
            self.assertTrue(self.status(corp_connection()).configured)


class StaticAllowlistTests(SimpleTestCase):
    def test_there_are_exactly_three_provider_types_and_no_dynamic_loading(self):
        self.assertEqual(ai_connections.PROVIDER_TYPES, ("kpmg_workbench", "openai_compatible", "ollama"))
        self.assertEqual([t.provider_type for t in ai_connections.build_provider_types()], list(ai_connections.PROVIDER_TYPES))
        text = (BACKEND / "config" / "ai_connections.py").read_text(encoding="utf-8")
        for forbidden in (r"importlib", r"__import__", r"import_module", r"\bexec\(", r"\beval\(", r"\bgetattr\(", r"subprocess", r"pkg_resources", r"entry_points"):
            self.assertIsNone(re.search(forbidden, text), forbidden)

    def test_an_unknown_provider_type_cannot_build_a_gateway(self):
        from apps.ai_settings.rules import ConnectionValues, ProfileValues

        forged = ConnectionValues("x", "X", "plugin", "local", "http://127.0.0.1/v1", None, True, (ProfileValues("p", "m"),))
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.build_connection_gateway(forged, "p", OPERATOR_ENV, transport=FakeTransport())
        self.assertEqual(caught.exception.reason, "unknown_provider_type")
