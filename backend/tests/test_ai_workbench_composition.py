"""DB-free tests of ``config/ai_workbench.py``: the environment contract, fail-closed configuration, the opt-in registration, and the promise that
importing and registering make no network call and read no environment. Only explicit mappings are used as environments, never the real one.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyContext, PolicyMode
from apps.ai_gateway.adapters import workbench
from config import ai_workbench
from config.ai_http_transport import StdlibHttpsTransport
from tests.ai_gateway_support import INPUT_SENTINEL, PROMPT_SENTINEL, SECRET_SENTINEL, make_request
from tests.ai_workbench_support import GPT5, GPT41, PROFILE_GPT5, PROFILE_GPT41, FakeTransport, answer, verified_url

BACKEND = Path(__file__).resolve().parent.parent

MINIMAL = {"AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL}  # the key is the only required value


def env(**changes):
    values = dict(MINIMAL)
    for name, value in changes.items():
        full = f"AP_AI_WORKBENCH_{name.upper()}"
        if value is None:
            values.pop(full, None)
        else:
            values[full] = value
    return values


class LoadSettingsTests(unittest.TestCase):
    def test_the_key_alone_gives_the_client_side_contract_defaults(self):
        settings = ai_workbench.load_workbench_settings(MINIMAL)
        config = settings.config
        self.assertEqual(
            (config.endpoint, config.api_version, config.charge_code, config.max_retries, settings.timeout_seconds, settings.proxy_url, settings.ca_bundle),
            ("https://api.workbench.kpmg/genai/azure/openai", "2024-10-21", "00000000", 2, 90.0, None, None),
        )
        self.assertEqual(
            (config.api_key, config.auth_header, config.chat_path),
            (SECRET_SENTINEL, "Ocp-Apim-Subscription-Key", "/deployments/{deployment}/chat/completions?api-version={api_version}"),
        )

    def test_every_optional_value_can_be_overridden(self):
        settings = ai_workbench.load_workbench_settings(
            env(endpoint="https://api.workbench.kpmg/other/base", api_version="2025-03-01-preview", charge_code="12345678", timeout_seconds="30", max_retries="1", proxy_url="http://proxy.example.invalid:3128", ca_bundle="company.pem")
        )
        config = settings.config
        self.assertEqual(
            (config.endpoint, config.api_version, config.charge_code, config.max_retries, settings.timeout_seconds, settings.proxy_url, settings.ca_bundle),
            ("https://api.workbench.kpmg/other/base", "2025-03-01-preview", "12345678", 1, 30.0, "http://proxy.example.invalid:3128", "company.pem"),
        )

    def test_the_auth_header_and_path_can_be_overridden_after_live_evidence(self):
        settings = ai_workbench.load_workbench_settings(env(auth_header="X-Other-Key", chat_path="/v2/{deployment}/chat?v={api_version}"))
        self.assertEqual((settings.config.auth_header, settings.config.url_for(GPT5)), ("X-Other-Key", "https://api.workbench.kpmg/genai/azure/openai/v2/gpt-5-2025-08-07-dzs-sdc/chat?v=2024-10-21"))

    def test_values_are_stripped_and_an_empty_value_counts_as_unset(self):
        settings = ai_workbench.load_workbench_settings(env(api_key=f"  {SECRET_SENTINEL}  ", charge_code="", max_retries="  ", proxy_url=" "))
        self.assertEqual((settings.config.api_key, settings.config.charge_code, settings.config.max_retries, settings.proxy_url), (SECRET_SENTINEL, "00000000", 2, None))

    def test_a_missing_credential_fails_closed(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), self.assertRaises(AIGatewayError) as ctx:
                ai_workbench.load_workbench_settings(env(api_key=value))
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "missing_ap_ai_workbench_api_key"))
            self.assertIn("AP_AI_WORKBENCH_API_KEY", str(ctx.exception))

    def test_an_empty_path_or_header_falls_back_to_the_verified_default_not_to_nothing(self):
        settings = ai_workbench.load_workbench_settings(env(auth_header="  ", chat_path=""))
        self.assertEqual((settings.config.auth_header, settings.config.chat_path), (workbench.DEFAULT_AUTH_HEADER, workbench.DEFAULT_CHAT_PATH))

    def test_an_endpoint_on_another_host_is_refused_by_the_environment_loader(self):
        for bad in ("https://evil.example/genai/azure/openai", "http://api.workbench.kpmg/genai/azure/openai", "https://api.workbench.kpmg.evil.example/x", "https://api.workbench.kpmg@evil.example/x", "https://api.workbench.kpmg:444/x"):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                ai_workbench.build_workbench_gateway(env(endpoint=bad), transport=FakeTransport())
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "invalid_workbench_endpoint"))

    def test_an_empty_environment_fails_closed(self):
        with self.assertRaises(AIGatewayError) as ctx:
            ai_workbench.load_workbench_settings({})
        self.assertEqual(ctx.exception.code, "configuration_error")

    def test_invalid_values_fail_closed_without_echoing_them(self):
        bad = {
            "api_key": f"{SECRET_SENTINEL} with space",
            "auth_header": f"Bad Header {SECRET_SENTINEL}",
            "chat_path": f"no-slash-{SECRET_SENTINEL}",
            "endpoint": f"http://{SECRET_SENTINEL}.example.invalid",
            "api_version": f"bad version {SECRET_SENTINEL}",
            "charge_code": f"bad code {SECRET_SENTINEL}",
            "max_retries": f"two-{SECRET_SENTINEL}",
            "timeout_seconds": f"ninety-{SECRET_SENTINEL}",
            "proxy_url": f"https://{SECRET_SENTINEL}.example.invalid",
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(AIGatewayError) as ctx:
                settings = ai_workbench.load_workbench_settings(env(**{name: value}))
                ai_workbench.build_workbench_transport(settings)  # the proxy URL is checked when the transport is built
            self.assertEqual(ctx.exception.code, "configuration_error")
            self.assertNotIn(SECRET_SENTINEL, f"{ctx.exception} {ctx.exception!r} {ctx.exception.args} {ctx.exception.reason}")

    def test_numbers_are_bounded(self):
        for name, value in (("max_retries", "-1"), ("max_retries", "6"), ("max_retries", "1.5"), ("timeout_seconds", "0"), ("timeout_seconds", "301"), ("timeout_seconds", "9.5")):
            with self.subTest(name=name, value=value), self.assertRaises(AIGatewayError):
                ai_workbench.load_workbench_settings(env(**{name: value}))

    def test_the_settings_repr_shows_neither_the_key_nor_the_proxy(self):
        settings = ai_workbench.load_workbench_settings(env(proxy_url="http://user:secretpw@proxy.example.invalid:3128"))
        text = repr(settings) + str(settings)
        for secret in (SECRET_SENTINEL, "secretpw", "user:"):
            self.assertNotIn(secret, text)

    def test_it_reads_the_process_environment_only_when_asked_and_only_when_called(self):
        with mock.patch.dict(os.environ, MINIMAL):
            self.assertEqual(ai_workbench.load_workbench_settings().config.api_key, SECRET_SENTINEL)
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in MINIMAL:
                os.environ.pop(name, None)
            with self.assertRaises(AIGatewayError):
                ai_workbench.load_workbench_settings()

    def test_the_variable_names_follow_the_ap_convention_the_local_env_loader_accepts(self):
        from config.localenv import _NAME

        source = (BACKEND / "config" / "ai_workbench.py").read_text(encoding="utf-8")
        names = set(__import__("re").findall(r'"(AP_AI_WORKBENCH_[A-Z0-9_]+)"', source))
        self.assertEqual(len(names), 10)
        for name in names:
            self.assertTrue(_NAME.fullmatch(name), name)


class ProfilesAndGatewayTests(unittest.TestCase):
    def test_the_gateway_has_exactly_the_workbench_provider_and_the_two_profiles(self):
        gateway = ai_workbench.build_workbench_gateway(MINIMAL, transport=FakeTransport())
        self.assertEqual(gateway.profile_ids, (PROFILE_GPT41, PROFILE_GPT5))
        gpt5 = gateway.profile(PROFILE_GPT5)
        self.assertEqual((gpt5.provider_id, gpt5.model_id, gpt5.timeout_seconds, gpt5.policy_mode), ("kpmg_workbench", GPT5, 90.0, PolicyMode.COMPANY_ENDPOINT))
        self.assertEqual(gateway.profile(PROFILE_GPT41).model_id, GPT41)

    def test_the_configured_timeout_is_the_profile_deadline(self):
        gateway = ai_workbench.build_workbench_gateway(env(timeout_seconds="45"), transport=FakeTransport())
        self.assertEqual({gateway.profile(p).timeout_seconds for p in gateway.profile_ids}, {45.0})

    def test_an_end_to_end_call_through_the_composition_with_only_the_key_configured(self):
        transport = FakeTransport(answer(), answer())
        gateway = ai_workbench.build_workbench_gateway(MINIMAL, transport=transport)
        response = gateway.generate(make_request(profile_id=PROFILE_GPT5), policy=PolicyContext(PolicyMode.COMPANY_ENDPOINT))
        self.assertEqual((response.provenance.provider_id, response.provenance.locality), ("kpmg_workbench", DataLocality.COMPANY_ENDPOINT))
        self.assertEqual(transport.last.url, verified_url(GPT5))
        gateway.generate(make_request(profile_id=PROFILE_GPT41), policy=PolicyContext(PolicyMode.COMPANY_ENDPOINT))
        self.assertEqual((transport.last.url, len(transport.requests)), (verified_url(GPT41), 2))

    def test_local_only_refuses_before_the_transport_is_used(self):
        transport = FakeTransport(answer())
        gateway = ai_workbench.build_workbench_gateway(MINIMAL, transport=transport)
        for profile_id in (PROFILE_GPT5, PROFILE_GPT41):
            with self.subTest(profile_id), self.assertRaises(AIGatewayError) as ctx:
                gateway.generate(make_request(profile_id=profile_id, instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL), policy=PolicyContext(PolicyMode.LOCAL_ONLY))
            self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertEqual(transport.requests, [])

    def test_a_missing_key_builds_no_gateway_and_no_transport(self):
        with mock.patch.object(ai_workbench, "build_workbench_transport", side_effect=AssertionError("a transport was built")):
            with self.assertRaises(AIGatewayError) as ctx:
                ai_workbench.build_workbench_gateway(env(api_key=None))
        self.assertEqual(ctx.exception.code, "configuration_error")

    def test_without_an_explicit_transport_the_real_verifying_one_is_built(self):
        gateway = ai_workbench.build_workbench_gateway(MINIMAL)
        provider = gateway._registry.provider("kpmg_workbench")
        self.assertIsInstance(provider._transport, StdlibHttpsTransport)

    def test_nothing_is_a_default_the_gateway_never_picks_a_profile_for_the_caller(self):
        gateway = ai_workbench.build_workbench_gateway(MINIMAL, transport=FakeTransport())
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.profile("workbench")
        self.assertEqual(ctx.exception.reason, "unknown_profile")


class NoNetworkOrEnvironmentAtImportOrRegistrationTests(unittest.TestCase):
    def test_registration_makes_no_network_call_and_reads_no_process_environment(self):
        import socket

        def boom(*args, **kwargs):
            raise AssertionError("network access")

        class Exploding(dict):
            def __getitem__(self, key):
                raise AssertionError("process environment read")

            get = __contains__ = keys = items = __iter__ = __getitem__

        with mock.patch.object(socket, "create_connection", boom), mock.patch.object(socket, "getaddrinfo", boom), mock.patch.object(socket.socket, "connect", boom), mock.patch.object(os, "environ", Exploding()):
            gateway = ai_workbench.build_workbench_gateway(MINIMAL, transport=FakeTransport())  # an explicit mapping: our code never reads the real environment
        self.assertEqual(len(gateway.profile_ids), 2)

    def test_registration_with_the_real_transport_makes_no_network_call(self):
        import socket

        def boom(*args, **kwargs):
            raise AssertionError("network access")

        with mock.patch.object(socket, "create_connection", boom), mock.patch.object(socket, "getaddrinfo", boom), mock.patch.object(socket.socket, "connect", boom):
            gateway = ai_workbench.build_workbench_gateway(env(proxy_url="http://proxy.example.invalid:3128"))
        self.assertEqual(len(gateway.profile_ids), 2)

    def test_importing_the_modules_makes_no_network_call_and_reads_no_environment(self):
        script = (
            "import os, socket, ssl, http.client\n"
            "def boom(*a, **k): raise AssertionError('network access')\n"
            "socket.create_connection = boom; socket.getaddrinfo = boom; socket.socket.connect = boom\n"
            "class Exploding(dict):\n"
            "    def _b(self, *a, **k): raise AssertionError('environment read')\n"
            "    __getitem__ = get = __contains__ = keys = items = __iter__ = _b\n"
            "os.environ = Exploding()\n"
            "import config.ai_workbench, config.ai_http_transport, apps.ai_gateway.adapters.workbench\n"
            "print('imported')\n"
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith("AP_")}
        result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=clean, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "imported"), result.stderr[-500:])


class ProductionCompositionIsUntouchedTests(unittest.TestCase):
    def test_the_production_root_still_names_no_provider_and_does_not_mention_the_workbench(self):
        from config import ai_gateway

        text = (BACKEND / "config" / "ai_gateway.py").read_text(encoding="utf-8").lower()
        self.assertEqual((ai_gateway.BUILTIN_PROVIDERS, ai_gateway.BUILTIN_PROFILES), ((), ()))
        self.assertNotIn("workbench", text)
        self.assertEqual(ai_gateway.build_provider_registry().provider_ids, ())

    def test_django_settings_hold_no_workbench_value(self):
        from django.conf import settings

        self.assertFalse([name for name in dir(settings) if "WORKBENCH" in name.upper()])
        for path in sorted((BACKEND / "config" / "settings").glob("*.py")) + [BACKEND / "config" / "env.py"]:
            self.assertNotIn("workbench", path.read_text(encoding="utf-8").lower(), path.name)

    def test_nothing_in_the_application_calls_the_workbench_composition(self):
        for top in ("apps", "workflows"):
            for path in sorted((BACKEND / top).rglob("*.py")):
                if "ai_gateway" in path.parts or "__pycache__" in path.parts:
                    continue
                with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                    self.assertNotIn("ai_workbench", path.read_text(encoding="utf-8"))

    def test_request_intake_extraction_is_unchanged_by_this_slice(self):
        from apps.intake import extraction
        from workflows.request_intake import DeterministicTestExtractor

        self.assertIs(extraction.DeterministicTestExtractor, DeterministicTestExtractor)
        text = (BACKEND / "apps" / "intake" / "extraction.py").read_text(encoding="utf-8")
        for word in ("workbench", "ai_gateway", "ai_http_transport"):
            self.assertNotIn(word, text)

    def test_the_composition_module_defines_no_module_level_call_that_could_do_io(self):
        tree = ast.parse((BACKEND / "config" / "ai_workbench.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)) and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
                self.assertFalse(calls, ast.unparse(node))


if __name__ == "__main__":
    unittest.main()
