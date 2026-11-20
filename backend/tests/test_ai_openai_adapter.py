"""DB-free tests of the OpenAI-compatible adapter (``apps.ai_gateway.adapters.openai_compatible``) and its profile, over a MOCKED transport: configuration and
destination rules, the request on the wire, authentication, structured output, error classification, retries and deadlines, no fallback, policy and no leakage.

The expected request is written out literally here, independently of the implementation. That a real local or company server accepts it is not shown by any test.
"""

import json
import logging
import traceback
import unittest

from apps.ai_gateway import AIGatewayError, DataLocality, GenerationParameters, PolicyContext, PolicyMode, StructuredOutputMode
from apps.ai_gateway.adapters import openai_compatible
from apps.ai_gateway.network import NetworkClass
from apps.ai_gateway.transport import HttpRequest, HttpResponse
from tests.ai_gateway_support import INPUT_SENTINEL, PROMPT_SENTINEL
from tests.ai_openai_support import (
    CLOUD_ENDPOINT, CLOUD_ORIGINS, CORP_ENDPOINT, CORP_ORIGINS, GOOD_OUTPUT, LOCAL_ENDPOINT, LOCAL_ORIGINS, MODEL, POLICIES, SECRET_SENTINEL,
    FakeTransport, Rig, TransportError, answer, make_call, make_config, status,
)  # fmt: skip

LOCAL, COMPANY, CLOUD = DataLocality.LOCAL, DataLocality.COMPANY_ENDPOINT, DataLocality.CLOUD


def reason_of(call_function):
    with_error = None
    try:
        call_function()
    except AIGatewayError as error:
        with_error = error
    return with_error


class ConfigurationTests(unittest.TestCase):
    def test_a_local_endpoint_may_use_plain_http_on_loopback_and_go_without_a_key(self):
        for endpoint, origin in (
            ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434"),
            ("http://localhost:8000/v1", "http://localhost:8000"),
            ("http://[::1]:8000/v1", "http://[::1]:8000"),
            ("https://127.0.0.1:8443", "https://127.0.0.1:8443"),
        ):
            with self.subTest(endpoint=endpoint):
                config = make_config(LOCAL, endpoint=endpoint, allowed_origins=(origin,))
                self.assertIsNone(config.api_key)
                self.assertTrue(config.url.endswith("/chat/completions"))

    def test_company_and_cloud_endpoints_need_https_and_a_key(self):
        for locality, endpoint, origins in ((COMPANY, CORP_ENDPOINT, CORP_ORIGINS), (CLOUD, CLOUD_ENDPOINT, CLOUD_ORIGINS)):
            with self.subTest(locality=locality.value):
                self.assertEqual(make_config(locality).url, endpoint.replace(".invalid/v1", ".invalid:443/v1") + "/chat/completions")
                error = reason_of(lambda: make_config(locality, api_key=None))
                self.assertEqual((error.code, error.reason), ("configuration_error", "missing_openai_api_key"))
                error = reason_of(lambda: make_config(locality, endpoint=endpoint.replace("https", "http"), allowed_origins=tuple(o.replace("https", "http") for o in origins)))
                self.assertEqual(error.reason, "invalid_openai_allowed_origins")

    def test_the_url_is_the_endpoint_plus_the_chat_path_only(self):
        self.assertEqual(make_config(COMPANY, endpoint="https://llm.corp.example.invalid/gateway/v1/").url, "https://llm.corp.example.invalid:443/gateway/v1/chat/completions")
        self.assertEqual(make_config(LOCAL).url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(make_config(COMPANY, endpoint="https://llm.corp.example.invalid").url, "https://llm.corp.example.invalid:443/chat/completions")

    def test_the_locality_is_explicit_and_never_inferred_from_the_name(self):
        with self.assertRaises(TypeError):
            openai_compatible.OpenAICompatibleConfig(endpoint=CORP_ENDPOINT, allowed_origins=CORP_ORIGINS, model_id=MODEL, api_key=SECRET_SENTINEL)
        for bad in ("remote", "", None, "LOCAL"):
            with self.subTest(locality=bad):
                error = reason_of(lambda: openai_compatible.OpenAICompatibleConfig(endpoint=CORP_ENDPOINT, allowed_origins=CORP_ORIGINS, api_key=SECRET_SENTINEL, model_id=MODEL, locality=bad))
                self.assertEqual(error.reason, "invalid_locality")
        # A name that SOUNDS local or corporate decides nothing: the declaration is checked against the host, not derived from it.
        error = reason_of(lambda: make_config(LOCAL, endpoint="https://local.corp.example.invalid/v1", allowed_origins=CORP_ORIGINS))
        self.assertEqual(error.reason, "invalid_openai_allowed_origins")

    def test_a_declaration_that_contradicts_the_host_is_refused(self):
        cases = [
            ("local with a remote host", LOCAL, "https://llm.corp.example.invalid/v1"),
            ("local with a private address", LOCAL, "http://10.0.0.5:8000/v1"),
            ("company with localhost", COMPANY, "https://localhost/v1"),
            ("company with a loopback literal", COMPANY, "https://127.0.0.1/v1"),
            ("company with an ipv6 loopback", COMPANY, "https://[::1]/v1"),
            ("company with a mapped loopback", COMPANY, "https://[::ffff:127.0.0.1]/v1"),
            ("company with a .localhost name", COMPANY, "https://app.localhost/v1"),
            ("company with the metadata address", COMPANY, "https://169.254.169.254/v1"),
            ("company with the unspecified address", COMPANY, "https://0.0.0.0/v1"),
            ("cloud with localhost", CLOUD, "https://localhost/v1"),
            ("cloud with a loopback literal", CLOUD, "https://127.0.0.1/v1"),
            ("cloud with a private literal", CLOUD, "https://10.0.0.5/v1"),
            ("cloud with the metadata address", CLOUD, "https://169.254.169.254/latest"),
            ("cloud with a mapped private address", CLOUD, "https://[::ffff:10.0.0.5]/v1"),
        ]
        for label, locality, endpoint in cases:
            origin = endpoint.split("/v1")[0].split("/latest")[0]
            with self.subTest(label):
                error = reason_of(lambda: make_config(locality, endpoint=endpoint, allowed_origins=(origin,), api_key=SECRET_SENTINEL if locality is not LOCAL else None))
                self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_openai_allowed_origins"))

    def test_a_private_literal_is_a_valid_company_endpoint(self):
        self.assertTrue(make_config(COMPANY, endpoint="https://10.20.30.40:8443/v1", allowed_origins=("https://10.20.30.40:8443",)))

    def test_a_company_endpoint_needs_approved_cidr_ranges(self):
        for value in ((), None, "", "10.0.0.0/8", [], b"10.0.0.0/8"):
            with self.subTest(value=value):
                error = reason_of(lambda: make_config(COMPANY, approved_networks=value))
                self.assertEqual((error.code, error.reason), ("configuration_error", "missing_openai_approved_networks"))
        with self.assertRaises(AIGatewayError):
            openai_compatible.OpenAICompatibleConfig(endpoint=CORP_ENDPOINT, allowed_origins=CORP_ORIGINS, locality=COMPANY, model_id=MODEL, api_key=SECRET_SENTINEL)  # none given at all

    def test_malformed_or_dangerous_cidr_ranges_are_refused_without_echo(self):
        bad = (["garbage"], ["10.0.0.1/8"], ["0.0.0.0/0"], ["::/0"], ["10.0.0.0/7"], ["10.0.0.0/8", "nope"], ["127.0.0.0/8"], ["169.254.0.0/16"], ["198.18.0.0/15"], ["192.0.0.0/24"],
               ["192.88.99.0/24"], ["2001:db8::/32"], ["fd00:ec2::/32"], ["168.63.129.16/32"], ["10.0.0.0/255.0.0.0"], [None], [5], tuple(f"10.{n}.0.0/16" for n in range(21)))  # fmt: skip
        for value in bad:
            with self.subTest(value=repr(value)[:40]):
                error = reason_of(lambda: make_config(COMPANY, approved_networks=value))
                self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_openai_approved_networks"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))
                for item in value:
                    if isinstance(item, str) and len(item) > 8:
                        self.assertNotIn(item, str(error))

    def test_the_approved_ranges_are_normalised_and_immutable(self):
        config = make_config(COMPANY, approved_networks=["192.168.0.0/16", "10.0.0.0/8", "10.0.0.0/8"])
        self.assertEqual(config.approved_networks, ("10.0.0.0/8", "192.168.0.0/16"))
        with self.assertRaises(AttributeError):
            config.approved_networks = ()

    def test_approved_ranges_are_refused_for_a_local_or_cloud_endpoint(self):
        for locality in (LOCAL, CLOUD):
            with self.subTest(locality=locality.value):
                error = reason_of(lambda: make_config(locality, approved_networks=("10.0.0.0/8",)))
                self.assertEqual((error.code, error.reason), ("configuration_error", "openai_approved_networks_not_applicable"))
            self.assertEqual(make_config(locality).approved_networks, ())

    def test_a_literal_company_endpoint_must_lie_inside_an_approved_range(self):
        def literal(host, networks):
            return make_config(COMPANY, endpoint=f"https://{host}/v1", allowed_origins=(f"https://{host}",), approved_networks=networks)

        self.assertTrue(literal("10.20.30.40", ("10.20.0.0/16",)))
        self.assertTrue(literal("[fd12::5]", ("fd12::/16",)))
        self.assertEqual(reason_of(lambda: literal("10.99.0.1", ("10.20.0.0/16",))).reason, "invalid_openai_allowed_origins")
        # A public address is a company endpoint only if the company approved the range that holds it; it is never classified as one by default.
        self.assertEqual(reason_of(lambda: literal("8.8.8.8", ("10.0.0.0/8",))).reason, "invalid_openai_allowed_origins")
        self.assertTrue(literal("8.8.8.8", ("8.8.8.0/24",)))

    def test_special_purpose_literals_are_refused_as_a_company_or_cloud_endpoint(self):
        literals = ("169.254.169.254", "169.254.170.2", "168.63.129.16", "100.100.100.200", "198.18.0.1", "192.0.0.192", "192.88.99.1", "0.1.2.3", "[fd00:ec2::254]", "[2001:db8::1]", "[64:ff9b::a00:1]", "[2002:a00:1::]")
        for host in literals:
            with self.subTest(host=host, locality="company"):
                self.assertEqual(reason_of(lambda: make_config(COMPANY, endpoint=f"https://{host}/v1", allowed_origins=(f"https://{host}",), approved_networks=("10.0.0.0/8",))).reason, "invalid_openai_allowed_origins")
            with self.subTest(host=host, locality="cloud"):
                self.assertEqual(reason_of(lambda: make_config(CLOUD, endpoint=f"https://{host}/v1", allowed_origins=(f"https://{host}",))).reason, "invalid_openai_allowed_origins")

    def test_the_endpoint_must_be_approved_separately(self):
        for origins in (("https://other.corp.example.invalid",), ("https://llm.corp.example.invalid:8443",), ("http://llm.corp.example.invalid",)):
            with self.subTest(origins=origins):
                self.assertIsNotNone(reason_of(lambda: make_config(COMPANY, allowed_origins=origins)))
        error = reason_of(lambda: make_config(COMPANY, allowed_origins=("https://other.corp.example.invalid",)))
        self.assertEqual(error.reason, "openai_endpoint_not_approved")
        for bad in ((), "https://llm.corp.example.invalid", None, ("https://llm.corp.example.invalid/v1",), ("https://llm.corp.example.invalid", "ftp://x.example.invalid"), tuple(f"https://h{i}.example.invalid" for i in range(11))):
            with self.subTest(bad=bad):
                self.assertEqual(reason_of(lambda: make_config(COMPANY, allowed_origins=bad)).reason, "invalid_openai_allowed_origins")

    def test_extra_approved_origins_are_validated_but_the_endpoint_is_the_only_destination(self):
        config = make_config(COMPANY, allowed_origins=CORP_ORIGINS + ("https://backup.corp.example.invalid",))
        self.assertEqual(len(config.allowed_origins), 2)
        self.assertEqual(config.url, "https://llm.corp.example.invalid:443/v1/chat/completions")

    def test_credentials_queries_fragments_and_odd_urls_in_the_endpoint_are_refused_without_echo(self):
        bad = ["https://user:" + SECRET_SENTINEL + "@llm.corp.example.invalid/v1", "https://llm.corp.example.invalid/v1?key=" + SECRET_SENTINEL, "https://llm.corp.example.invalid/v1#" + SECRET_SENTINEL,
               "https://2130706433/v1", "https://llm.corp.example.invalid/../v1", "ftp://llm.corp.example.invalid/v1", "", None, 5]  # fmt: skip
        for endpoint in bad:
            with self.subTest(endpoint=endpoint):
                error = reason_of(lambda: make_config(COMPANY, endpoint=endpoint))
                self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_openai_endpoint"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))

    def test_an_invalid_key_model_or_limit_is_refused(self):
        for key in ("", "has space", "line\nbreak", "tab\tkey", "k" * 513, "ключ", 5):
            with self.subTest(key=repr(key)[:20]):
                self.assertEqual(reason_of(lambda: make_config(COMPANY, api_key=key)).reason, "invalid_openai_api_key")
        for model in ("", " ", "a b", "x" * 200, None, "../model", "-model"):
            with self.subTest(model=model):
                self.assertEqual(reason_of(lambda: make_config(COMPANY, model_id=model)).reason, "invalid_openai_model")
        for name, value in (("max_input_tokens", 0), ("max_output_tokens", True), ("max_retries", 6), ("max_retries", -1), ("connect_timeout_seconds", 0), ("connect_timeout_seconds", 61)):
            with self.subTest(name=name, value=value):
                self.assertIsNotNone(reason_of(lambda: make_config(COMPANY, **{name: value})))

    def test_the_configuration_is_immutable_and_its_repr_has_no_key(self):
        config = make_config(COMPANY)
        with self.assertRaises(AttributeError):
            config.endpoint = "https://other.invalid"
        for text in (repr(config), str(config)):
            self.assertNotIn(SECRET_SENTINEL, text)
        self.assertNotIn(SECRET_SENTINEL, repr(openai_compatible.OpenAICompatibleProvider(config, FakeTransport())))

    def test_the_provider_refuses_a_foreign_config_or_transport(self):
        for config, transport in ((object(), FakeTransport()), (make_config(COMPANY), object())):
            with self.assertRaises(AIGatewayError):
                openai_compatible.OpenAICompatibleProvider(config, transport)

    def test_the_last_check_before_a_request_refuses_any_other_destination(self):
        config = make_config(COMPANY)
        ok = config.url
        self.assertTrue(openai_compatible._destination_allowed(config, ok))
        for url in (
            "https://evil.example.invalid/v1/chat/completions", "http://llm.corp.example.invalid:443/v1/chat/completions", "https://llm.corp.example.invalid:8443/v1/chat/completions",
            "https://llm.corp.example.invalid:443/v1/other", "https://user@llm.corp.example.invalid:443/v1/chat/completions", "https://127.0.0.1:443/v1/chat/completions", "not a url",
        ):
            with self.subTest(url=url):
                self.assertFalse(openai_compatible._destination_allowed(config, url))


class DescriptorAndProfileTests(unittest.TestCase):
    def test_the_provider_declares_the_configured_locality_model_and_json_mode_only(self):
        for locality, policy_mode in ((LOCAL, PolicyMode.LOCAL_ONLY), (COMPANY, PolicyMode.COMPANY_ENDPOINT), (CLOUD, PolicyMode.APPROVED_CLOUD)):
            with self.subTest(locality=locality.value):
                rig = Rig(locality=locality)
                descriptor = rig.provider.descriptor
                self.assertEqual((descriptor.provider_id, descriptor.kind, descriptor.locality), ("openai_compatible", "openai_compatible_chat", locality))
                self.assertEqual(descriptor.capabilities.supported_models, frozenset({MODEL}))
                self.assertEqual(descriptor.capabilities.structured_output_modes, frozenset({StructuredOutputMode.JSON_MODE}))
                self.assertFalse(descriptor.capabilities.test_only)
                self.assertEqual(rig.profile.policy_mode, policy_mode)
                self.assertEqual(rig.gateway.profile_ids, ("openai-compatible",))

    def test_the_profile_holds_no_secret_and_no_endpoint(self):
        profile = Rig(locality=COMPANY).profile
        text = repr(profile)
        for hidden in (SECRET_SENTINEL, "corp.example", "https://"):
            self.assertNotIn(hidden, text)


class RequestOnTheWireTests(unittest.TestCase):
    def test_a_local_call_posts_to_the_loopback_chat_path_with_no_authorization(self):
        rig = Rig(answer(), locality=LOCAL)
        rig.generate(POLICIES["local_only"])
        request = rig.transport.last
        self.assertEqual(request.url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(rig.transport.headers(), {"Content-Type": "application/json", "Accept": "application/json"})
        self.assertIs(request.network_class, NetworkClass.LOOPBACK)

    def test_a_local_call_sends_a_key_when_one_is_configured(self):
        rig = Rig(answer(), locality=LOCAL, api_key=SECRET_SENTINEL)
        rig.generate(POLICIES["local_only"])
        self.assertEqual(rig.transport.headers()["Authorization"], f"Bearer {SECRET_SENTINEL}")

    def test_a_company_call_authenticates_with_a_bearer_key_and_carries_the_internal_class(self):
        rig = Rig(answer())
        rig.generate(POLICIES["company_endpoint"])
        request = rig.transport.last
        self.assertEqual(request.url, "https://llm.corp.example.invalid:443/v1/chat/completions")
        self.assertEqual(rig.transport.headers(), {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {SECRET_SENTINEL}"})
        self.assertIs(request.network_class, NetworkClass.INTERNAL)
        self.assertEqual(request.approved_networks, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))  # the company's ranges travel with every request, for the transport to enforce

    def test_a_cloud_call_carries_the_public_class(self):
        rig = Rig(answer(), locality=CLOUD)
        rig.generate(POLICIES["approved_cloud"])
        self.assertIs(rig.transport.last.network_class, NetworkClass.PUBLIC)
        self.assertEqual(rig.transport.last.approved_networks, ())  # a cloud endpoint is never judged against a company range
        self.assertTrue(rig.transport.last.url.startswith("https://api.vendor.example.invalid:443/"))

    def test_the_key_appears_nowhere_but_the_authorization_header(self):
        rig = Rig(answer())
        rig.generate(POLICIES["company_endpoint"])
        request = rig.transport.last
        self.assertNotIn(SECRET_SENTINEL, request.url)
        self.assertNotIn(SECRET_SENTINEL.encode(), request.body)
        self.assertEqual([name for name, value in request.headers if SECRET_SENTINEL in value], ["Authorization"])

    def test_the_body_is_exactly_the_chat_completions_shape(self):
        rig = Rig(answer(), timeout=40.0)
        rig.generate(POLICIES["company_endpoint"], instructions="Extract the fields.", input_text="Some document text.")
        body = rig.transport.body()
        self.assertEqual(set(body), {"model", "messages", "response_format", "stream", "max_tokens"})
        self.assertEqual((body["model"], body["stream"], body["response_format"]), (MODEL, False, {"type": "json_object"}))
        self.assertEqual(body["max_tokens"], rig.profile.limits.max_output_tokens)
        system, user = body["messages"]
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith("Extract the fields."))
        self.assertIn("JSON Schema", system["content"])
        self.assertEqual(user, {"role": "user", "content": "Some document text."})

    def test_a_request_may_lower_the_output_limit_and_the_timeout_but_not_raise_them(self):
        rig = Rig(answer(), timeout=40.0)
        rig.generate(POLICIES["company_endpoint"], max_output_tokens=100, timeout_seconds=12.0)
        self.assertEqual(rig.transport.body()["max_tokens"], 100)
        self.assertEqual(rig.transport.last.read_timeout_seconds, 12.0)
        self.assertEqual(rig.transport.last.connect_timeout_seconds, 10.0)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["company_endpoint"], timeout_seconds=41.0)
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_request", "timeout_above_profile"))

    def test_sampling_parameters_are_sent_only_when_the_profile_sets_them(self):
        rig = Rig(answer())
        rig.generate(POLICIES["company_endpoint"])
        self.assertTrue({"temperature", "top_p", "seed"}.isdisjoint(rig.transport.body()))
        rig = Rig(answer(), parameters={"temperature": 0.0, "top_p": 0.9, "seed": 7})
        rig.generate(POLICIES["company_endpoint"])
        body = rig.transport.body()
        self.assertEqual((body["temperature"], body["top_p"], body["seed"]), (0.0, 0.9, 7))

    def test_the_provider_refuses_a_model_or_mode_it_was_not_configured_for(self):
        rig = Rig()
        for call, code in ((make_call(model_id="other-model"), "unsupported_model"), (make_call(structured_output_mode=StructuredOutputMode.NATIVE_SCHEMA), "provider_error")):
            with self.subTest(code=code), self.assertRaises(AIGatewayError) as ctx:
                rig.provider.generate(call)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(rig.transport.requests, [])


class StructuredOutputTests(unittest.TestCase):
    def test_a_valid_answer_is_returned_with_provider_and_model_provenance(self):
        rig = Rig(answer(model="served-model-8b-q4", usage=(21, 9)), locality=LOCAL)
        response = rig.generate(POLICIES["local_only"])
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})
        p = response.provenance
        self.assertEqual((p.provider_id, p.provider_kind, p.locality, p.policy_mode), ("openai_compatible", "openai_compatible_chat", LOCAL, PolicyMode.LOCAL_ONLY))
        self.assertEqual((p.requested_model_id, p.reported_model_id, p.input_tokens, p.output_tokens, p.test_only), (MODEL, "served-model-8b-q4", 21, 9, False))

    def test_a_missing_reported_model_is_recorded_as_unreported_never_as_the_requested_one(self):
        raw = json.dumps({"choices": [{"message": {"content": GOOD_OUTPUT}}]}).encode()
        rig = Rig(HttpResponse(200, raw))
        response = rig.generate(POLICIES["company_endpoint"])
        self.assertEqual(response.provenance.reported_model_id, "unreported")
        self.assertEqual((response.provenance.input_tokens, response.provenance.output_tokens), (None, None))

    def test_unusable_usage_numbers_are_dropped(self):
        raw = json.dumps({"model": "m", "choices": [{"message": {"content": GOOD_OUTPUT}}], "usage": {"prompt_tokens": -1, "completion_tokens": True}}).encode()
        provenance = Rig(HttpResponse(200, raw)).generate(POLICIES["company_endpoint"]).provenance
        self.assertEqual((provenance.input_tokens, provenance.output_tokens), (None, None))

    def test_output_that_is_not_valid_structured_json_is_refused(self):
        bad = {
            "prose": "Sure, here you go!", "fenced": "```json\n" + GOOD_OUTPUT + "\n```", "array": "[1, 2]", "wrong type": '{"name": 5}', "missing required": '{"count": 1}',
            "extra property": '{"name": "x", "other": 1}', "negative": '{"name": "x", "count": -1}', "empty": "", "nan": '{"name": "x", "count": NaN}',
        }  # fmt: skip
        for label, content in bad.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                Rig(answer(content)).generate(POLICIES["company_endpoint"])
            self.assertEqual(ctx.exception.code, "invalid_structured_output")

    def test_a_truncated_refused_or_malformed_answer_is_refused(self):
        cases = {
            "length": (answer(finish="length"), "invalid_structured_output"),
            "content filter": (answer(finish="content_filter"), "provider_error"),
            "tool calls": (answer(finish="tool_calls"), "provider_error"),
            "refusal": (HttpResponse(200, json.dumps({"choices": [{"message": {"content": "x", "refusal": "no"}}]}).encode()), "provider_error"),
            "null content": (HttpResponse(200, json.dumps({"choices": [{"message": {"content": None}}]}).encode()), "provider_error"),
            "no choices": (HttpResponse(200, b'{"choices": []}'), "provider_error"),
            "choices not a list": (HttpResponse(200, b'{"choices": {}}'), "provider_error"),
            "not an object": (HttpResponse(200, b"[1]"), "provider_error"),
            "not json": (HttpResponse(200, b"<html>gateway</html>"), "provider_error"),
            "not utf-8": (HttpResponse(200, b"\xff\xfe\x00"), "provider_error"),
            "non-finite": (HttpResponse(200, b'{"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": NaN}}'), "provider_error"),
            "deeply nested": (HttpResponse(200, b"[" * 100000), "provider_error"),
        }  # fmt: skip
        for label, (response, code) in cases.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                Rig(response).generate(POLICIES["company_endpoint"])
            self.assertEqual(ctx.exception.code, code)

    def test_the_provider_gives_a_fixed_reason_for_each_malformed_answer(self):
        rig = Rig(HttpResponse(200, b"nope"), max_retries=0)
        self.assertEqual(reason_of(lambda: rig.provider.generate(make_call())).reason, "unreadable_answer")
        rig = Rig(answer(finish="length"), max_retries=0)
        self.assertEqual(reason_of(lambda: rig.provider.generate(make_call())).reason, "output_truncated")


class ErrorClassificationTests(unittest.TestCase):
    def failure(self, response, **rig_options):
        rig = Rig(*response if isinstance(response, list) else [response], **rig_options)
        error = reason_of(lambda: rig.provider.generate(make_call()))
        return error, rig

    def test_http_statuses_map_to_the_gateway_codes(self):
        expected = {400: "provider_error", 401: "authentication_failure", 403: "authentication_failure", 404: "unsupported_model", 408: "timeout", 413: "provider_error",
                    422: "provider_error", 500: "provider_error", 501: "provider_error", 505: "provider_error"}  # fmt: skip
        for code, adapter_code in expected.items():
            with self.subTest(status=code):
                error, rig = self.failure(status(code))
                self.assertEqual((error.code, error.reason), (adapter_code, f"http_{code}"))
                self.assertEqual(len(rig.transport.requests), 1)  # none of these is retried

    def test_rate_limits_and_unavailable_servers_are_retried_then_reported_with_their_code(self):
        for http_status, code in ((429, "rate_limit"), (502, "provider_unavailable"), (503, "provider_unavailable"), (504, "provider_unavailable")):
            with self.subTest(status=http_status):
                error, rig = self.failure([status(http_status)] * 3)
                self.assertEqual((error.code, error.retryable), (code, True))
                self.assertEqual(len(rig.transport.requests), 3)
                self.assertEqual(rig.sleeps, [1.0, 2.0])

    def test_a_rate_limited_call_that_then_succeeds_returns_the_answer(self):
        rig = Rig(status(429), answer())
        self.assertEqual(dict(rig.generate(POLICIES["company_endpoint"]).output), {"name": "x", "count": 2})
        self.assertEqual((len(rig.transport.requests), rig.sleeps), (2, [1.0]))

    def test_retries_can_be_switched_off(self):
        error, rig = self.failure(status(429), max_retries=0)
        self.assertEqual((error.code, len(rig.transport.requests), rig.sleeps), ("rate_limit", 1, []))

    def test_a_redirect_is_refused_never_followed_and_never_retried(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(status=code):
                error, rig = self.failure(status(code, b"<a href='https://evil.example.invalid/'>"))
                self.assertEqual((error.code, error.reason), ("provider_error", "redirect_refused"))
                self.assertEqual(len(rig.transport.requests), 1)

    def test_an_unknown_status_gets_a_bounded_reason(self):
        for code, reason in ((999, "http_status"), (100, "http_100")):
            with self.subTest(status=code):
                self.assertEqual(self.failure(status(code))[0].reason, reason)

    def test_transport_failures_map_to_fixed_codes(self):
        cases = {"timeout": ("timeout", 1), "connect": ("provider_unavailable", 3), "tls": ("provider_error", 1), "protocol": ("provider_error", 1), "destination": ("provider_error", 1)}
        reasons = {"timeout": "transport_timeout", "connect": "transport_connect", "tls": "tls_failure", "protocol": "transport_protocol", "destination": "destination_refused"}
        for kind, (code, attempts) in cases.items():  # only a refused connection is retried; a timeout may have reached the model, the rest are not transient
            with self.subTest(kind=kind):
                error, rig = self.failure([TransportError(kind)] * 3)
                self.assertEqual((error.code, error.reason, len(rig.transport.requests)), (code, reasons[kind], attempts))

    def test_a_foreign_exception_is_classified_and_dropped(self):
        error, _ = self.failure(RuntimeError(f"boom {SECRET_SENTINEL} {PROMPT_SENTINEL}"))
        self.assertEqual((error.code, error.reason), ("provider_error", "transport_failure"))
        text = "".join(traceback.format_exception(error)) + str(error) + repr(error)
        self.assertNotIn(SECRET_SENTINEL, text)
        self.assertNotIn(PROMPT_SENTINEL, text)

    def test_a_malformed_response_object_is_a_provider_error(self):
        class Odd:
            status = "200"
            body = b"{}"

        error, _ = self.failure(Odd())
        self.assertEqual((error.code, error.reason), ("provider_error", "invalid_response"))

    def test_an_error_body_is_never_copied_into_the_error(self):
        body = json.dumps({"error": {"message": f"bad key {SECRET_SENTINEL} for prompt {PROMPT_SENTINEL}"}}).encode()
        for code in (400, 401, 429, 500, 503):
            with self.subTest(status=code):
                error, _ = self.failure([status(code, body)] * 3)
                text = str(error) + repr(error) + str(error.reason) + str(error.args)
                self.assertNotIn(SECRET_SENTINEL, text)
                self.assertNotIn(PROMPT_SENTINEL, text)


class DeadlineTests(unittest.TestCase):
    def test_a_slow_attempt_that_exhausts_the_deadline_stops_the_retries(self):
        rig = Rig(status(503), status(503), status(503), timeout=5.0, cost=4.0)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["company_endpoint"])
        self.assertEqual(ctx.exception.code, "provider_unavailable")
        self.assertEqual(len(rig.transport.requests), 1)  # 4 s used of 5 s: not enough left for the 1 s back-off plus another try

    def test_each_attempt_gets_only_the_time_that_remains(self):
        rig = Rig(status(503), answer(), timeout=30.0, cost=10.0)
        rig.generate(POLICIES["company_endpoint"])
        first, second = rig.transport.requests
        self.assertEqual(first.read_timeout_seconds, 30.0)
        self.assertEqual(second.read_timeout_seconds, 30.0 - 10.0 - 1.0 - 0.0)

    def test_an_answer_that_arrives_after_the_deadline_is_refused_by_the_gateway(self):
        rig = Rig(answer(), timeout=5.0, cost=6.0)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["company_endpoint"])
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("timeout", "answer_after_deadline"))

    def test_the_connect_timeout_never_exceeds_the_time_left(self):
        rig = Rig(answer(), timeout=3.0)
        rig.generate(POLICIES["company_endpoint"], timeout_seconds=2.0)
        self.assertEqual(rig.transport.last.connect_timeout_seconds, 2.0)


class NoFallbackTests(unittest.TestCase):
    def test_every_attempt_goes_to_the_same_endpoint_and_model(self):
        rig = Rig(status(503), status(429), status(502))
        with self.assertRaises(AIGatewayError):
            rig.generate(POLICIES["company_endpoint"])
        self.assertEqual({r.url for r in rig.transport.requests}, {rig.config.url})
        self.assertEqual({json.loads(r.body)["model"] for r in rig.transport.requests}, {MODEL})

    def test_a_failed_call_is_not_answered_by_another_provider_model_or_mode(self):
        rig = Rig(status(401))
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["company_endpoint"])
        self.assertEqual(ctx.exception.code, "authentication_failure")
        self.assertEqual((len(rig.transport.requests), rig.gateway.profile_ids), (1, ("openai-compatible",)))
        self.assertEqual(rig.provider.descriptor.capabilities.supported_models, frozenset({MODEL}))

    def test_a_policy_refusal_does_not_become_a_call_to_anything(self):
        rig = Rig(answer())
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["local_only"])
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("policy_violation", "locality_not_allowed"))
        self.assertEqual(rig.transport.requests, [])


class DataLocalityPolicyTests(unittest.TestCase):
    def outcome(self, locality, policy_name):
        rig = Rig(answer(), locality=locality)
        try:
            rig.generate(POLICIES[policy_name])
        except AIGatewayError as error:
            return (error.code, error.reason), rig
        return "ok", rig

    def test_the_matrix_of_locality_and_policy_mode(self):
        refused = ("policy_violation", "locality_not_allowed")
        expected = {
            (LOCAL, "local_only"): "ok", (LOCAL, "company_endpoint"): "ok", (LOCAL, "approved_cloud"): "ok",
            (COMPANY, "local_only"): refused, (COMPANY, "company_endpoint"): "ok", (COMPANY, "approved_cloud"): "ok",
            (CLOUD, "local_only"): refused, (CLOUD, "company_endpoint"): refused, (CLOUD, "approved_cloud"): "ok",
        }  # fmt: skip
        for (locality, policy_name), result in expected.items():
            with self.subTest(locality=locality.value, policy=policy_name):
                outcome, rig = self.outcome(locality, policy_name)
                self.assertEqual(outcome, result)
                self.assertEqual(len(rig.transport.requests), 1 if result == "ok" else 0)  # a refusal happens before the adapter runs

    def test_a_cloud_provider_must_also_be_named_as_approved(self):
        rig = Rig(answer(), locality=CLOUD)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(PolicyContext(PolicyMode.APPROVED_CLOUD))
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("policy_violation", "cloud_provider_not_approved"))
        self.assertEqual(rig.transport.requests, [])

    def test_local_only_never_reaches_a_non_loopback_destination(self):
        rig = Rig(answer(), locality=LOCAL)
        rig.generate(POLICIES["local_only"])
        self.assertTrue(rig.transport.last.url.startswith("http://127.0.0.1:"))
        self.assertIs(rig.transport.last.network_class, NetworkClass.LOOPBACK)
        self.assertEqual(rig.transport.last.approved_networks, ())


class NoLeakageTests(unittest.TestCase):
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))

    def setUp(self):
        self.capture = self.Capture()
        root = logging.getLogger()
        level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, level)

    def test_no_prompt_input_or_key_reaches_a_log_line_an_error_or_a_repr(self):
        scripts = [[answer()], [status(401)], [status(429)] * 3, [TransportError("connect")] * 3, [RuntimeError(SECRET_SENTINEL)], [answer("not json")], [status(302)]]
        for script in scripts:
            rig = Rig(*script)
            texts = []
            try:
                rig.generate(POLICIES["company_endpoint"], instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL)
            except AIGatewayError as error:
                texts.append("".join(traceback.format_exception(error)) + f"{error} {error!r} {error.args} {error.reason} {error.violations}")
            texts += [repr(rig.provider), repr(rig.config), repr(rig.profile), repr(rig.gateway), *map(repr, rig.transport.requests)]
            blob = "\n".join(texts) + "\n" + "\n".join(self.capture.lines)
            for sentinel in (SECRET_SENTINEL, PROMPT_SENTINEL, INPUT_SENTINEL):
                with self.subTest(script=repr(script)[:40], sentinel=sentinel):
                    self.assertNotIn(sentinel, blob)

    def test_a_successful_response_does_not_log_the_output_or_the_key(self):
        Rig(answer()).generate(POLICIES["company_endpoint"])
        blob = "\n".join(self.capture.lines)
        self.assertNotIn(SECRET_SENTINEL, blob)
        self.assertNotIn(GOOD_OUTPUT, blob)


class DefaultsTests(unittest.TestCase):
    def test_the_adapter_registers_nothing_and_reads_nothing_by_importing_it(self):
        import config.ai_gateway as production

        self.assertEqual((production.BUILTIN_PROVIDERS, production.BUILTIN_PROFILES), ((), ()))
        self.assertIsInstance(HttpRequest("https://h/p", (), b"", 1.0, 2.0).network_class, type(None))  # the Workbench's requests carry no class


if __name__ == "__main__":
    unittest.main()
