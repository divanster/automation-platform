"""DB-free tests of the Ollama composition (``config/ai_ollama.py``) and of how the adapter meets the guarded transport: environment -> settings -> provider, profile and
gateway, failing closed and naming only variables; loopback-only destination rules enforced on what a name resolves to; a proxy refused; that nothing runs at import; and
that the deterministic default, the Workbench and the OpenAI-compatible provider are untouched. No network, no Ollama, no database.
"""

import json
import unittest
from unittest import mock

from apps.ai_gateway import AIGatewayError, PolicyMode, StructuredOutputMode
from apps.ai_gateway.adapters import ollama
from apps.ai_gateway.network import NetworkClass
from apps.ai_gateway.transport import HttpRequest
from config import ai_ollama as composition
from config import ai_openai_compatible, ai_workbench, intake_extraction
from config.ai_guarded_transport import GuardedHttpTransport
from tests.ai_gateway_support import make_request
from tests.ai_ollama_support import GOOD_OUTPUT, MODEL, POLICIES, SECRET_SENTINEL, FakeTransport, answer
from tests.ai_openai_support import LOCAL_ENDPOINT
from tests.ai_openai_support import MODEL as OPENAI_MODEL
from tests.ai_openai_support import Rig as OpenAIRig
from tests.ai_openai_support import answer as openai_answer
from tests.ai_workbench_support import GPT5, verified_url
from tests.ai_workbench_support import answer as workbench_answer

BASE = {
    "AP_AI_OLLAMA_ENDPOINT": "http://127.0.0.1:11434",
    "AP_AI_OLLAMA_ALLOWED_ORIGINS": "http://127.0.0.1:11434",
    "AP_AI_OLLAMA_MODEL": MODEL,
}


def error_of(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except AIGatewayError as error:
        return error
    raise AssertionError("no AIGatewayError was raised")


class LoadSettingsTests(unittest.TestCase):
    def test_nothing_is_configured_by_default_and_the_first_missing_variable_is_named(self):
        error = error_of(composition.load_ollama_settings, {})
        self.assertEqual((error.code, error.reason), ("configuration_error", "missing_ap_ai_ollama_endpoint"))
        self.assertIn("AP_AI_OLLAMA_ENDPOINT", str(error))

    def test_every_required_variable_is_required_and_named_never_defaulted(self):
        for name in BASE:
            for blank in (None, "", "   "):
                with self.subTest(name=name, blank=blank):
                    env = dict(BASE)
                    if blank is None:
                        del env[name]
                    else:
                        env[name] = blank
                    error = error_of(composition.load_ollama_settings, env)
                    self.assertEqual((error.code, error.reason), ("configuration_error", f"missing_{name.lower()}"))

    def test_the_documented_defaults_and_overrides(self):
        settings = composition.load_ollama_settings(BASE)
        config = settings.config
        self.assertEqual((config.max_retries, config.max_input_tokens, config.max_output_tokens, config.num_ctx), (2, 3072, 1024, None))
        self.assertEqual((settings.timeout_seconds, settings.structured_output), (90.0, StructuredOutputMode.JSON_MODE))
        env = dict(BASE, AP_AI_OLLAMA_TIMEOUT_SECONDS="20", AP_AI_OLLAMA_MAX_RETRIES="0", AP_AI_OLLAMA_MAX_INPUT_TOKENS="6000", AP_AI_OLLAMA_MAX_OUTPUT_TOKENS="1500",
                   AP_AI_OLLAMA_NUM_CTX="8192", AP_AI_OLLAMA_STRUCTURED_OUTPUT="native_schema")  # fmt: skip
        settings = composition.load_ollama_settings(env)
        config = settings.config
        self.assertEqual((settings.timeout_seconds, config.max_retries, config.max_input_tokens, config.max_output_tokens, config.num_ctx, settings.structured_output), (20.0, 0, 6000, 1500, 8192, StructuredOutputMode.NATIVE_SCHEMA))

    def test_invalid_values_are_refused_naming_the_variable_and_not_the_value(self):
        for name, value in (
            ("AP_AI_OLLAMA_TIMEOUT_SECONDS", "0"), ("AP_AI_OLLAMA_TIMEOUT_SECONDS", "abc"), ("AP_AI_OLLAMA_MAX_RETRIES", "9"), ("AP_AI_OLLAMA_MAX_INPUT_TOKENS", "0"),
            ("AP_AI_OLLAMA_MAX_OUTPUT_TOKENS", "x"), ("AP_AI_OLLAMA_NUM_CTX", "10"), ("AP_AI_OLLAMA_NUM_CTX", "lots"), ("AP_AI_OLLAMA_STRUCTURED_OUTPUT", "prompt_only"),
            ("AP_AI_OLLAMA_STRUCTURED_OUTPUT", "JSON_MODE"),
        ):  # fmt: skip
            with self.subTest(name=name, value=value):
                error = error_of(composition.load_ollama_settings, dict(BASE, **{name: value}))
                self.assertEqual(error.code, "configuration_error")
                self.assertIn(name.lower(), error.reason)
                if len(value) > 3:
                    self.assertNotIn(value, str(error) + error.reason)

    def test_limits_that_do_not_fit_the_context_window_are_refused(self):
        error = error_of(composition.load_ollama_settings, dict(BASE, AP_AI_OLLAMA_NUM_CTX="2048"))  # 3072 + 1024 default limits do not fit
        self.assertEqual(error.reason, "ollama_limits_exceed_num_ctx")

    def test_a_non_loopback_endpoint_is_refused_whatever_is_approved(self):
        for host in ("10.0.0.5", "192.168.1.20", "8.8.8.8", "169.254.169.254", "ollama.corp.example.invalid", "api.vendor.example.invalid"):
            origin = f"https://{host}:11434"
            with self.subTest(host=host):
                env = dict(BASE, AP_AI_OLLAMA_ENDPOINT=origin, AP_AI_OLLAMA_ALLOWED_ORIGINS=origin)
                self.assertEqual(error_of(composition.load_ollama_settings, env).reason, "invalid_ollama_allowed_origins")

    def test_an_endpoint_that_is_not_approved_is_refused(self):
        env = dict(BASE, AP_AI_OLLAMA_ENDPOINT="http://127.0.0.1:11999")
        self.assertEqual(error_of(composition.load_ollama_settings, env).reason, "ollama_endpoint_not_approved")
        env = dict(BASE, AP_AI_OLLAMA_ALLOWED_ORIGINS="http://127.0.0.1:11434, http://localhost:11434")
        self.assertEqual(len(composition.load_ollama_settings(env).config.allowed_origins), 2)

    def test_credentials_in_the_endpoint_are_refused_without_echo(self):
        env = dict(BASE, AP_AI_OLLAMA_ENDPOINT=f"http://user:{SECRET_SENTINEL}@127.0.0.1:11434")
        error = error_of(composition.load_ollama_settings, env)
        self.assertEqual(error.reason, "invalid_ollama_endpoint")
        self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error) + error.reason)

    def test_a_proxy_is_refused_with_no_bypass(self):
        for value in ("http://proxy.example.invalid:3128", "http://127.0.0.1:3128", f"http://user:{SECRET_SENTINEL}@proxy.example.invalid:3128"):
            with self.subTest(value=value[:20]):
                error = error_of(composition.load_ollama_settings, dict(BASE, AP_AI_OLLAMA_PROXY_URL=value))
                self.assertEqual((error.code, error.reason), ("configuration_error", "proxy_not_allowed_for_local"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))
        self.assertTrue(composition.load_ollama_settings(dict(BASE, AP_AI_OLLAMA_PROXY_URL="  ")))  # an empty value counts as unset

    def test_the_process_environment_is_read_only_when_asked(self):
        with mock.patch.dict("os.environ", BASE, clear=False):
            self.assertEqual(composition.load_ollama_settings().config.model_id, MODEL)

    def test_no_secret_exists_to_leak(self):
        self.assertNotIn("api_key", {field for field in vars(composition.OllamaSettings)["__dataclass_fields__"]})
        self.assertNotIn("proxy_url", vars(composition.OllamaSettings)["__dataclass_fields__"])


class BuildTests(unittest.TestCase):
    def test_the_gateway_has_one_provider_and_one_profile_and_no_other(self):
        gateway = composition.build_ollama_gateway(BASE, transport=FakeTransport())
        self.assertEqual(gateway.profile_ids, ("ollama-local",))
        profile = gateway.profile("ollama-local")
        self.assertEqual((profile.provider_id, profile.model_id, profile.policy_mode, profile.structured_output.mode), ("ollama", MODEL, PolicyMode.LOCAL_ONLY, StructuredOutputMode.JSON_MODE))
        self.assertEqual(error_of(gateway.profile, "workbench-gpt5").reason, "unknown_profile")

    def test_the_profile_carries_no_endpoint_or_origin(self):
        profile = composition.build_ollama_profile(composition.load_ollama_settings(BASE))
        blob = repr(profile) + repr(vars(profile))
        for hidden in ("127.0.0.1", "11434", "http://"):
            self.assertNotIn(hidden, blob)

    def test_an_end_to_end_call_over_a_mocked_transport(self):
        transport = FakeTransport(answer(GOOD_OUTPUT))
        gateway = composition.build_ollama_gateway(BASE, transport=transport)
        response = gateway.generate(make_request(profile_id="ollama-local"), policy=POLICIES["local_only"])
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})
        self.assertEqual((response.provenance.provider_id, response.provenance.requested_model_id, response.provenance.reported_model_id), ("ollama", MODEL, MODEL))
        self.assertEqual(transport.last.url, "http://127.0.0.1:11434/api/chat")

    def test_the_real_transport_is_built_by_default_without_connecting_and_without_a_proxy(self):
        with mock.patch("config.ai_ollama.GuardedHttpTransport", wraps=GuardedHttpTransport) as built:
            composition.build_ollama_gateway(BASE)
        built.assert_called_once_with(proxy_url=None, ca_bundle=None)

    def test_a_bad_configuration_builds_nothing(self):
        for env in ({}, {k: v for k, v in BASE.items() if k != "AP_AI_OLLAMA_MODEL"}, dict(BASE, AP_AI_OLLAMA_PROXY_URL="http://p.example.invalid:1")):
            with self.subTest(env=sorted(env)), self.assertRaises(AIGatewayError):
                composition.build_ollama_gateway(env, transport=FakeTransport())

    def test_importing_and_building_do_not_touch_the_network(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network used")), mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")), mock.patch(
            "socket.create_connection", side_effect=AssertionError("connect used")
        ):
            import importlib

            importlib.reload(composition)
            composition.build_ollama_gateway(BASE)


class EndToEndAddressRulesTests(unittest.TestCase):
    """Configuration -> adapter -> guarded transport, with a fake resolver and a fake connection: only loopback is ever dialled, and never through a proxy."""

    class Connection:
        def __init__(self, log, ip, body, status=200):
            self.log, self.ip, self.body, self.status, self.sock = log, ip, body, status, mock.Mock()

        def connect(self):
            self.log.append(("connect", self.ip))

        def request(self, method, target, body=None, headers=None):
            self.log.append(("request", self.ip, method, target, dict(headers)))

        def getresponse(self):
            return mock.Mock(status=self.status, read=lambda amount=None: self.body)

        def close(self):
            pass

    def gateway(self, env, resolved, status=200, **transport_options):
        log = []
        body = answer(GOOD_OUTPUT).body
        transport = GuardedHttpTransport(
            resolver=lambda host, port: resolved, connection_factory=lambda scheme, host, port, ip, timeout, context: self.Connection(log, ip, body, status), **transport_options
        )
        return composition.build_ollama_gateway(env, transport=transport), log

    def call(self, gateway, policy="local_only"):
        return gateway.generate(make_request(profile_id="ollama-local"), policy=POLICIES[policy])

    def test_a_loopback_literal_is_called_at_that_address_over_plain_http(self):
        gateway, log = self.gateway(BASE, [])
        self.assertEqual(dict(self.call(gateway).output), {"name": "x", "count": 2})
        self.assertEqual([entry[:2] for entry in log if entry[0] == "connect"], [("connect", "127.0.0.1")])
        sent = [entry for entry in log if entry[0] == "request"]
        self.assertEqual((sent[0][2], sent[0][3]), ("POST", "/api/chat"))
        self.assertNotIn("Authorization", sent[0][4])

    def test_localhost_resolving_to_loopback_is_allowed_and_anywhere_else_is_refused_before_anything_is_sent(self):
        env = dict(BASE, AP_AI_OLLAMA_ENDPOINT="http://localhost:11434", AP_AI_OLLAMA_ALLOWED_ORIGINS="http://localhost:11434")
        gateway, log = self.gateway(env, ["127.0.0.1", "::1"])
        self.call(gateway)
        self.assertEqual([entry[1] for entry in log if entry[0] == "connect"], ["127.0.0.1"])
        for resolved in (["10.1.2.3"], ["192.168.1.5"], ["8.8.8.8"], ["169.254.169.254"], ["127.0.0.1", "10.1.2.3"], ["::ffff:10.0.0.5"], ["0.0.0.0"]):
            with self.subTest(resolved=resolved):
                gateway, log = self.gateway(env, resolved)
                error = error_of(self.call, gateway)
                self.assertEqual(error.code, "provider_error")
                self.assertEqual(log, [])  # nothing was dialled and no prompt was sent

    def test_a_proxy_is_refused_by_the_transport_and_nothing_is_sent_around_it_or_through_it(self):
        gateway, log = self.gateway(BASE, [], proxy_url="http://proxy.example.invalid:3128")
        error = error_of(self.call, gateway)
        self.assertEqual(error.code, "provider_error")
        self.assertEqual(log, [])
        transport = GuardedHttpTransport(proxy_url="http://proxy.example.invalid:3128")
        for url in ("http://127.0.0.1:11434/api/chat", "http://localhost:11434/api/chat"):
            with self.subTest(url=url):
                request = HttpRequest(url, (), b"{}", 1.0, 1.0, NetworkClass.LOOPBACK)
                with self.assertRaises(Exception) as ctx:
                    transport.send(request)
                self.assertEqual(ctx.exception.kind, "destination")

    def test_a_redirect_answer_is_refused_and_not_followed(self):
        gateway, log = self.gateway(BASE, [], status=307)
        error = error_of(self.call, gateway)
        self.assertEqual(error.code, "provider_error")
        self.assertEqual([entry[0] for entry in log if entry[0] == "connect"], ["connect"])  # one connection, no second hop

    def test_proxy_environment_variables_are_never_read(self):
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.example.invalid:3128", "HTTPS_PROXY": "http://proxy.example.invalid:3128", "ALL_PROXY": "http://proxy.example.invalid:3128"}):
            gateway, log = self.gateway(BASE, [])
            self.call(gateway)
        self.assertEqual([entry[1] for entry in log if entry[0] == "connect"], ["127.0.0.1"])


class NothingIsActivatedTests(unittest.TestCase):
    def test_the_deterministic_default_is_unchanged_whatever_these_variables_say(self):
        for environ in ({}, BASE):
            with self.subTest(keys=len(environ)):
                settings = intake_extraction.load_intake_extraction_settings(environ)
                self.assertEqual(settings.mode, intake_extraction.MODE_DETERMINISTIC)
                self.assertIsNone(intake_extraction.build_extractor_source(environ)())

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


class ExistingProvidersRegressionTests(unittest.TestCase):
    def test_the_workbench_request_is_unchanged(self):
        transport = FakeTransport(workbench_answer(GOOD_OUTPUT))
        gateway = ai_workbench.build_workbench_gateway({"AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL}, transport=transport)
        self.assertEqual(gateway.profile_ids, ("workbench-gpt41", "workbench-gpt5"))
        gateway.generate(make_request(profile_id="workbench-gpt5"), policy=POLICIES["company_endpoint"])
        request = transport.last
        self.assertEqual(request.url, verified_url(GPT5))
        self.assertIsNone(request.network_class)
        self.assertEqual(dict(request.headers)["Ocp-Apim-Subscription-Key"], SECRET_SENTINEL)
        self.assertEqual(set(json.loads(request.body)) & {"options", "format", "stream"}, set())  # nothing Ollama-shaped leaked into the Workbench body

    def test_the_openai_compatible_local_request_is_unchanged(self):
        from apps.ai_gateway import DataLocality

        rig = OpenAIRig(openai_answer(), locality=DataLocality.LOCAL)
        rig.generate(POLICIES["local_only"])
        request = rig.transport.last
        self.assertEqual(request.url, LOCAL_ENDPOINT + "/chat/completions")
        self.assertIs(request.network_class, NetworkClass.LOOPBACK)
        body = json.loads(request.body)
        self.assertEqual((body["model"], body["response_format"], body["stream"]), (OPENAI_MODEL, {"type": "json_object"}, False))
        self.assertNotIn("options", body)
        self.assertNotIn("format", body)
        self.assertEqual(rig.provider.descriptor.provider_id, "openai_compatible")

    def test_the_openai_compatible_composition_ignores_the_ollama_variables(self):
        env = {
            "AP_AI_OPENAI_ENDPOINT": "http://127.0.0.1:11434/v1", "AP_AI_OPENAI_ALLOWED_ORIGINS": "http://127.0.0.1:11434", "AP_AI_OPENAI_LOCALITY": "local",
            "AP_AI_OPENAI_MODEL": "llama3:8b", **BASE, "AP_AI_OLLAMA_PROXY_URL": "http://p.example.invalid:1",
        }  # fmt: skip
        settings = ai_openai_compatible.load_openai_compatible_settings(env)
        self.assertEqual(settings.config.model_id, "llama3:8b")
        self.assertIsNone(settings.proxy_url)

    def test_the_two_providers_have_different_ids_profiles_and_paths(self):
        gateway = composition.build_ollama_gateway(BASE, transport=FakeTransport())
        self.assertNotEqual(gateway.profile("ollama-local").provider_id, ai_openai_compatible.openai_compatible.PROVIDER_ID)
        self.assertNotEqual(composition.PROFILE_ID, ai_openai_compatible.PROFILE_ID)
        self.assertNotEqual(ollama.CHAT_SUFFIX, ai_openai_compatible.openai_compatible.CHAT_SUFFIX)


if __name__ == "__main__":
    unittest.main()
