"""DB-free tests of the OpenAI-compatible composition (``config/ai_openai_compatible.py``): environment -> settings -> provider, profile and gateway, failing closed
and naming only variables; that the deterministic default and the Workbench are untouched; and that nothing in the application activates the provider.
No network, no real key, no database.
"""

import unittest
from unittest import mock

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyMode
from config import ai_openai_compatible as composition
from config import ai_workbench, intake_extraction
from tests.ai_gateway_support import make_request
from tests.ai_openai_support import GOOD_OUTPUT, POLICIES, SECRET_SENTINEL, FakeTransport, answer
from tests.ai_workbench_support import GPT5, verified_url

BASE = {
    "AP_AI_OPENAI_ENDPOINT": "https://llm.corp.example.invalid/v1",
    "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://llm.corp.example.invalid",
    "AP_AI_OPENAI_LOCALITY": "company_endpoint",
    "AP_AI_OPENAI_MODEL": "corp-model-1",
    "AP_AI_OPENAI_API_KEY": SECRET_SENTINEL,
    "AP_AI_OPENAI_COMPANY_CIDRS": "10.0.0.0/8,192.168.0.0/16",
}
CLOUD_ENV = {
    "AP_AI_OPENAI_ENDPOINT": "https://api.vendor.example.invalid/v1",
    "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://api.vendor.example.invalid",
    "AP_AI_OPENAI_LOCALITY": "cloud",
    "AP_AI_OPENAI_MODEL": "vendor-model",
    "AP_AI_OPENAI_API_KEY": SECRET_SENTINEL,
}
IP_ENV = {  # a company endpoint given as an IP literal: the only kind that may use a proxy
    "AP_AI_OPENAI_ENDPOINT": "https://10.20.30.40:8443/v1",
    "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://10.20.30.40:8443",
    "AP_AI_OPENAI_LOCALITY": "company_endpoint",
    "AP_AI_OPENAI_MODEL": "corp-model-1",
    "AP_AI_OPENAI_API_KEY": SECRET_SENTINEL,
    "AP_AI_OPENAI_COMPANY_CIDRS": "10.20.0.0/16",
}
LOCAL_ENV = {
    "AP_AI_OPENAI_ENDPOINT": "http://127.0.0.1:11434/v1",
    "AP_AI_OPENAI_ALLOWED_ORIGINS": "http://127.0.0.1:11434",
    "AP_AI_OPENAI_LOCALITY": "local",
    "AP_AI_OPENAI_MODEL": "llama3:8b",
}


def error_of(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except AIGatewayError as error:
        return error
    raise AssertionError("no AIGatewayError was raised")


class LoadSettingsTests(unittest.TestCase):
    def test_nothing_is_configured_by_default_and_the_first_missing_variable_is_named(self):
        error = error_of(composition.load_openai_compatible_settings, {})
        self.assertEqual((error.code, error.reason), ("configuration_error", "missing_ap_ai_openai_locality"))
        self.assertIn("AP_AI_OPENAI_LOCALITY", str(error))

    def test_every_required_variable_is_required_and_named_never_defaulted(self):
        for name in ("AP_AI_OPENAI_ENDPOINT", "AP_AI_OPENAI_ALLOWED_ORIGINS", "AP_AI_OPENAI_LOCALITY", "AP_AI_OPENAI_MODEL", "AP_AI_OPENAI_API_KEY", "AP_AI_OPENAI_COMPANY_CIDRS"):
            for blank in (None, "", "   "):
                with self.subTest(name=name, blank=blank):
                    env = dict(BASE)
                    if blank is None:
                        del env[name]
                    else:
                        env[name] = blank
                    error = error_of(composition.load_openai_compatible_settings, env)
                    self.assertEqual(error.code, "configuration_error")
                    self.assertTrue(error.reason.startswith("missing_"), error.reason)
                    self.assertNotIn(SECRET_SENTINEL, str(error))

    def test_a_local_endpoint_needs_no_key(self):
        settings = composition.load_openai_compatible_settings(LOCAL_ENV)
        self.assertEqual((settings.config.locality, settings.config.api_key, settings.timeout_seconds), (DataLocality.LOCAL, None, 90.0))

    def test_a_company_or_cloud_endpoint_without_a_key_fails_closed(self):
        env = {k: v for k, v in BASE.items() if k != "AP_AI_OPENAI_API_KEY"}
        self.assertEqual(error_of(composition.load_openai_compatible_settings, env).reason, "missing_openai_api_key")
        cloud = {k: v for k, v in CLOUD_ENV.items() if k != "AP_AI_OPENAI_API_KEY"}
        self.assertEqual(error_of(composition.load_openai_compatible_settings, cloud).reason, "missing_openai_api_key")

    def test_the_documented_defaults_and_overrides(self):
        settings = composition.load_openai_compatible_settings(BASE)
        self.assertEqual((settings.config.max_retries, settings.config.max_input_tokens, settings.config.max_output_tokens, settings.proxy_url, settings.ca_bundle), (2, 32_000, 4_096, None, None))
        env = dict(BASE, AP_AI_OPENAI_TIMEOUT_SECONDS="20", AP_AI_OPENAI_MAX_RETRIES="0", AP_AI_OPENAI_MAX_INPUT_TOKENS="8000", AP_AI_OPENAI_MAX_OUTPUT_TOKENS="1000", AP_AI_OPENAI_CA_BUNDLE="ca.pem")
        settings = composition.load_openai_compatible_settings(env)
        self.assertEqual((settings.timeout_seconds, settings.config.max_retries, settings.config.max_input_tokens, settings.config.max_output_tokens, settings.ca_bundle), (20.0, 0, 8000, 1000, "ca.pem"))

    def test_invalid_values_are_refused_without_echo(self):
        cases = {
            "AP_AI_OPENAI_LOCALITY": ("remote", "LOCAL", "local,cloud"), "AP_AI_OPENAI_ENDPOINT": ("ftp://h.example.invalid", "https://2130706433/v1", "https://u:p@llm.corp.example.invalid/v1"),
            "AP_AI_OPENAI_ALLOWED_ORIGINS": ("https://other.example.invalid", "https://llm.corp.example.invalid,", "*", "https://llm.corp.example.invalid/v1"),
            "AP_AI_OPENAI_MODEL": ("bad model", "-x"), "AP_AI_OPENAI_API_KEY": ("has space",), "AP_AI_OPENAI_TIMEOUT_SECONDS": ("0", "301", "abc", "1.5"),
            "AP_AI_OPENAI_MAX_RETRIES": ("-1", "6", "x"), "AP_AI_OPENAI_MAX_INPUT_TOKENS": ("0", "x", "99999999"), "AP_AI_OPENAI_MAX_OUTPUT_TOKENS": ("0", "x", "9999999"),
        }  # fmt: skip
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    error = error_of(composition.load_openai_compatible_settings, dict(BASE, **{name: value}))
                    self.assertEqual(error.code, "configuration_error")
                    if len(value) > 8:  # a short value can be a substring of the message by chance
                        self.assertNotIn(value, str(error))
                    self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))

    def test_a_declaration_that_contradicts_the_endpoint_is_refused(self):
        env = dict(BASE, AP_AI_OPENAI_LOCALITY="local")
        self.assertEqual(error_of(composition.load_openai_compatible_settings, env).reason, "invalid_openai_allowed_origins")
        env = dict(LOCAL_ENV, AP_AI_OPENAI_LOCALITY="company_endpoint", AP_AI_OPENAI_API_KEY=SECRET_SENTINEL, AP_AI_OPENAI_COMPANY_CIDRS="10.0.0.0/8")
        self.assertEqual(error_of(composition.load_openai_compatible_settings, env).reason, "invalid_openai_allowed_origins")

    def test_a_company_endpoint_needs_approved_cidr_ranges_and_never_defaults_them(self):
        for value in (None, "", "   "):
            env = {k: v for k, v in BASE.items() if k != "AP_AI_OPENAI_COMPANY_CIDRS"}
            if value is not None:
                env["AP_AI_OPENAI_COMPANY_CIDRS"] = value
            with self.subTest(value=value):
                error = error_of(composition.load_openai_compatible_settings, env)
                self.assertEqual((error.code, error.reason), ("configuration_error", "missing_ap_ai_openai_company_cidrs"))
                self.assertIn("AP_AI_OPENAI_COMPANY_CIDRS", str(error))

    def test_the_approved_ranges_are_loaded_normalised(self):
        settings = composition.load_openai_compatible_settings(dict(BASE, AP_AI_OPENAI_COMPANY_CIDRS=" 192.168.0.0/16 , 10.0.0.0/8,10.0.0.0/8 "))
        self.assertEqual(settings.config.approved_networks, ("10.0.0.0/8", "192.168.0.0/16"))

    def test_malformed_or_dangerous_ranges_are_refused_without_echo(self):
        bad = ("garbage", "10.0.0.0", "10.0.0.1/8", "10.0.0.0/8,", ",10.0.0.0/8", "0.0.0.0/0", "::/0", "10.0.0.0/7", "*", "any", "10.0.0.0/255.0.0.0", "127.0.0.0/8", "169.254.0.0/16",
               "169.254.169.254/32", "198.18.0.0/15", "192.0.0.0/24", "192.88.99.0/24", "100.64.0.0/10", "fd00:ec2::/32", "2001:db8::/32", "168.63.129.16/32", "10.0.0.0/8;rm -rf /")  # fmt: skip
        for value in bad:
            with self.subTest(value=value):
                error = error_of(composition.load_openai_compatible_settings, dict(BASE, AP_AI_OPENAI_COMPANY_CIDRS=value))
                self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_openai_approved_networks"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))
                if len(value) > 8:
                    self.assertNotIn(value, str(error))

    def test_a_literal_company_endpoint_must_lie_inside_the_approved_ranges(self):
        error = error_of(composition.load_openai_compatible_settings, dict(IP_ENV, AP_AI_OPENAI_COMPANY_CIDRS="10.99.0.0/16"))
        self.assertEqual(error.reason, "invalid_openai_allowed_origins")
        self.assertTrue(composition.load_openai_compatible_settings(IP_ENV))

    def test_approved_ranges_are_refused_for_a_local_or_cloud_endpoint(self):
        for env in (dict(LOCAL_ENV, AP_AI_OPENAI_COMPANY_CIDRS="10.0.0.0/8"), dict(CLOUD_ENV, AP_AI_OPENAI_COMPANY_CIDRS="10.0.0.0/8")):
            with self.subTest(locality=env["AP_AI_OPENAI_LOCALITY"]):
                self.assertEqual(error_of(composition.load_openai_compatible_settings, env).reason, "openai_approved_networks_not_applicable")
        for env in (LOCAL_ENV, CLOUD_ENV):
            self.assertEqual(composition.load_openai_compatible_settings(env).config.approved_networks, ())

    def test_an_endpoint_that_is_not_approved_is_refused(self):
        env = dict(BASE, AP_AI_OPENAI_ALLOWED_ORIGINS="https://elsewhere.example.invalid")
        self.assertEqual(error_of(composition.load_openai_compatible_settings, env).reason, "openai_endpoint_not_approved")

    def test_several_origins_may_be_approved(self):
        env = dict(BASE, AP_AI_OPENAI_ALLOWED_ORIGINS=" https://llm.corp.example.invalid , https://llm2.corp.example.invalid:8443 ")
        self.assertEqual(len(composition.load_openai_compatible_settings(env).config.allowed_origins), 2)

    def test_a_proxy_is_refused_for_a_local_endpoint(self):
        error = error_of(composition.load_openai_compatible_settings, dict(LOCAL_ENV, AP_AI_OPENAI_PROXY_URL="http://proxy.invalid:3128"))
        self.assertEqual(error.reason, "proxy_not_allowed_for_local")

    def test_a_proxy_is_refused_for_every_host_name_endpoint_with_no_bypass(self):
        proxy = "http://user:" + SECRET_SENTINEL + "@proxy.invalid:3128"
        for env in (BASE, CLOUD_ENV):
            with self.subTest(locality=env["AP_AI_OPENAI_LOCALITY"]):
                error = error_of(composition.load_openai_compatible_settings, dict(env, AP_AI_OPENAI_PROXY_URL=proxy))
                self.assertEqual((error.code, error.reason), ("configuration_error", "proxy_not_allowed_for_hostname_endpoint"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))
                self.assertNotIn("proxy.invalid", str(error))
                with self.assertRaises(AIGatewayError):  # and no variable turns the refusal off
                    composition.load_openai_compatible_settings(dict(env, AP_AI_OPENAI_PROXY_URL=proxy, AP_AI_OPENAI_PROXY_RESOLVES_DNS="acknowledged", AP_AI_OPENAI_ALLOW_PROXY="1"))
        with self.assertRaises(AIGatewayError):
            composition.build_openai_compatible_gateway(dict(BASE, AP_AI_OPENAI_PROXY_URL=proxy), transport=FakeTransport())

    def test_a_proxy_stays_possible_for_an_ip_literal_endpoint_and_stays_out_of_the_repr(self):
        proxy = "http://user:" + SECRET_SENTINEL + "@proxy.invalid:3128"
        settings = composition.load_openai_compatible_settings(dict(IP_ENV, AP_AI_OPENAI_PROXY_URL=proxy))
        self.assertEqual(settings.proxy_url, proxy)
        self.assertNotIn(SECRET_SENTINEL, repr(settings))
        self.assertEqual(repr(composition.build_openai_compatible_transport(settings)), "GuardedHttpTransport(proxy=yes)")

    def test_the_key_never_appears_in_the_settings_repr(self):
        self.assertNotIn(SECRET_SENTINEL, repr(composition.load_openai_compatible_settings(BASE)))

    def test_the_process_environment_is_read_only_when_asked(self):
        with mock.patch.dict("os.environ", BASE, clear=False):
            self.assertEqual(composition.load_openai_compatible_settings().config.model_id, "corp-model-1")


class BuildTests(unittest.TestCase):
    def test_the_gateway_has_one_provider_and_one_profile_and_no_other(self):
        gateway = composition.build_openai_compatible_gateway(BASE, transport=FakeTransport())
        self.assertEqual(gateway.profile_ids, ("openai-compatible",))
        profile = gateway.profile("openai-compatible")
        self.assertEqual((profile.provider_id, profile.model_id, profile.policy_mode), ("openai_compatible", "corp-model-1", PolicyMode.COMPANY_ENDPOINT))
        self.assertEqual(error_of(gateway.profile, "workbench-gpt5").reason, "unknown_profile")

    def test_the_profile_policy_mode_follows_the_declared_locality(self):
        for environment, mode in ((LOCAL_ENV, PolicyMode.LOCAL_ONLY), (BASE, PolicyMode.COMPANY_ENDPOINT), (CLOUD_ENV, PolicyMode.APPROVED_CLOUD)):
            with self.subTest(mode=mode.value):
                settings = composition.load_openai_compatible_settings(environment)
                self.assertEqual(composition.build_openai_compatible_profile(settings).policy_mode, mode)

    def test_the_profile_carries_no_secret_endpoint_or_origin(self):
        settings = composition.load_openai_compatible_settings(BASE)
        profile = composition.build_openai_compatible_profile(settings)
        blob = repr(profile) + repr(vars(profile))
        for hidden in (SECRET_SENTINEL, "corp.example", "https://"):
            self.assertNotIn(hidden, blob)

    def test_an_end_to_end_call_over_a_mocked_transport(self):
        transport = FakeTransport(answer(GOOD_OUTPUT, model="corp-model-1"))
        gateway = composition.build_openai_compatible_gateway(BASE, transport=transport)
        response = gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["company_endpoint"])
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})
        self.assertEqual((response.provenance.provider_id, response.provenance.requested_model_id, response.provenance.reported_model_id), ("openai_compatible", "corp-model-1", "corp-model-1"))
        self.assertEqual(transport.last.url, "https://llm.corp.example.invalid:443/v1/chat/completions")

    def test_a_local_gateway_is_allowed_by_local_only_and_reaches_only_loopback(self):
        transport = FakeTransport(answer())
        gateway = composition.build_openai_compatible_gateway(LOCAL_ENV, transport=transport)
        gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["local_only"])
        self.assertTrue(transport.last.url.startswith("http://127.0.0.1:11434/"))

    def test_the_real_transport_is_built_by_default_without_connecting(self):
        from config.ai_guarded_transport import GuardedHttpTransport

        with mock.patch("config.ai_openai_compatible.GuardedHttpTransport", wraps=GuardedHttpTransport) as built:
            composition.build_openai_compatible_gateway(BASE)
        built.assert_called_once_with(proxy_url=None, ca_bundle=None)

    def test_a_bad_configuration_builds_nothing(self):
        for env in ({}, {k: v for k, v in BASE.items() if k != "AP_AI_OPENAI_API_KEY"}):
            with self.subTest(env=sorted(env)), self.assertRaises(AIGatewayError):
                composition.build_openai_compatible_gateway(env, transport=FakeTransport())


class EndToEndAddressRulesTests(unittest.TestCase):
    """Configuration -> adapter -> guarded transport, with a fake resolver and a fake connection: the company's CIDR ranges are enforced on what the name resolves to."""

    class Connection:
        def __init__(self, log, ip, body):
            self.log, self.ip, self.body, self.sock = log, ip, body, mock.Mock()

        def connect(self):
            self.log.append(("connect", self.ip))

        def request(self, method, target, body=None, headers=None):
            self.log.append(("request", self.ip, dict(headers)))

        def getresponse(self):
            return mock.Mock(status=200, read=lambda amount=None: self.body)

        def close(self):
            pass

    def gateway(self, env, resolved):
        from config.ai_guarded_transport import GuardedHttpTransport

        log = []
        body = answer(GOOD_OUTPUT, model="corp-model-1").body
        transport = GuardedHttpTransport(resolver=lambda host, port: resolved, connection_factory=lambda scheme, host, port, ip, timeout, context: self.Connection(log, ip, body))
        return composition.build_openai_compatible_gateway(env, transport=transport), log

    def call(self, gateway):
        return gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["company_endpoint"])

    def test_a_name_that_resolves_inside_the_approved_range_is_called_at_that_address_only(self):
        gateway, log = self.gateway(BASE, ["10.1.2.3"])
        self.assertEqual(dict(self.call(gateway).output), {"name": "x", "count": 2})
        self.assertEqual([entry[:2] for entry in log if entry[0] == "connect"], [("connect", "10.1.2.3")])

    def test_a_public_cloud_host_declared_as_a_company_endpoint_is_refused_at_connection_time(self):
        for resolved in (["8.8.8.8"], ["93.184.216.34"], ["10.1.2.3", "8.8.8.8"], ["11.0.0.1"], ["169.254.169.254"], ["127.0.0.1"], ["10.1.2.3", "168.63.129.16"]):
            with self.subTest(resolved=resolved):
                gateway, log = self.gateway(BASE, resolved)
                with self.assertRaises(AIGatewayError) as ctx:
                    self.call(gateway)
                self.assertEqual(ctx.exception.code, "provider_error")
                self.assertEqual(log, [])  # nothing was dialled and no request, key or prompt was sent
                self.assertNotIn(SECRET_SENTINEL, str(ctx.exception) + repr(ctx.exception) + str(ctx.exception.reason))

    def test_the_key_is_sent_only_to_the_validated_address(self):
        gateway, log = self.gateway(BASE, ["192.168.7.7"])
        self.call(gateway)
        sent = [entry for entry in log if entry[0] == "request"]
        self.assertEqual([entry[1] for entry in sent], ["192.168.7.7"])
        self.assertEqual(sent[0][2]["Authorization"], f"Bearer {SECRET_SENTINEL}")

    def test_a_cloud_endpoint_is_refused_any_non_global_or_special_purpose_answer(self):
        for resolved in (["10.1.2.3"], ["198.18.0.1"], ["168.63.129.16"], ["fd00:ec2::254"], ["192.88.99.1"], ["100.100.100.200"]):
            with self.subTest(resolved=resolved):
                gateway, log = self.gateway(CLOUD_ENV, resolved)
                with self.assertRaises(AIGatewayError):
                    gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["approved_cloud"])
                self.assertEqual(log, [])

    def test_a_local_gateway_still_reaches_loopback_only(self):
        gateway, log = self.gateway(LOCAL_ENV, ["127.0.0.1"])
        gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["local_only"])
        self.assertEqual([entry[1] for entry in log if entry[0] == "connect"], ["127.0.0.1"])
        localhost = dict(LOCAL_ENV, AP_AI_OPENAI_ENDPOINT="http://localhost:11434/v1", AP_AI_OPENAI_ALLOWED_ORIGINS="http://localhost:11434")
        gateway, log = self.gateway(localhost, ["127.0.0.1", "::1"])
        gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["local_only"])
        gateway, log = self.gateway(localhost, ["10.1.2.3"])  # the name "localhost" resolved somewhere remote: refused, nothing dialled
        with self.assertRaises(AIGatewayError):
            gateway.generate(make_request(profile_id="openai-compatible"), policy=POLICIES["local_only"])
        self.assertEqual(log, [])


class NothingIsActivatedTests(unittest.TestCase):
    def test_the_deterministic_default_is_unchanged_whatever_these_variables_say(self):
        for environ in ({}, BASE, LOCAL_ENV):
            with self.subTest(keys=len(environ)):
                settings = intake_extraction.load_intake_extraction_settings(environ)
                self.assertEqual(settings.mode, intake_extraction.MODE_DETERMINISTIC)
                source = intake_extraction.build_extractor_source(environ)
                self.assertIsNone(source())

    def test_request_intake_ai_mode_still_builds_the_workbench_and_never_this_provider(self):
        environ = {"AP_INTAKE_EXTRACTION_MODE": "ai", "AP_AI_POLICY_MODE": "company_endpoint", "AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL, **BASE}
        source = intake_extraction.build_extractor_source(environ, module_gate=lambda: frozenset(), transport=FakeTransport())
        self.assertEqual(source.extractor.profile_id, ai_workbench.PROFILE_GPT5)

    def test_the_ai_settings_catalog_still_offers_the_workbench_only(self):
        catalog = intake_extraction.build_catalog()
        self.assertEqual([p.provider_id for p in catalog.providers], ["kpmg_workbench"])
        self.assertEqual({p.profile_id for p in catalog.profiles}, {"workbench-gpt5"})

    def test_the_production_composition_root_is_still_empty(self):
        from config import ai_gateway

        self.assertEqual((ai_gateway.BUILTIN_PROVIDERS, ai_gateway.BUILTIN_PROFILES), ((), ()))

    def test_the_workbench_gateway_is_unchanged(self):
        transport = FakeTransport(answer(GOOD_OUTPUT, model="gpt-5-2025-08-07"))
        gateway = ai_workbench.build_workbench_gateway({"AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL}, transport=transport)
        self.assertEqual(gateway.profile_ids, ("workbench-gpt41", "workbench-gpt5"))
        gateway.generate(make_request(profile_id="workbench-gpt5"), policy=POLICIES["company_endpoint"])
        request = transport.last
        self.assertEqual(request.url, verified_url(GPT5))
        self.assertIsNone(request.network_class)  # the Workbench carries no address rule: its behaviour is as before
        self.assertEqual(dict(request.headers)["Ocp-Apim-Subscription-Key"], SECRET_SENTINEL)
        self.assertNotIn("Authorization", dict(request.headers))


if __name__ == "__main__":
    unittest.main()
