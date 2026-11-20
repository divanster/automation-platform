"""DB-free tests of the native Ollama adapter (``apps.ai_gateway.adapters.ollama``) and its profile, over a MOCKED transport: loopback-only configuration, the request on
the wire, structured output, model selection, error classification, retries and deadlines, no fallback and no model management, the policy matrix and no leakage.

The expected request is written out literally here, independently of the implementation. That a real Ollama server accepts it is not shown by any test.
"""

import json
import logging
import traceback
import unittest

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyContext, PolicyMode, StructuredOutputMode
from apps.ai_gateway.adapters import ollama
from apps.ai_gateway.network import NetworkClass
from apps.ai_gateway.transport import HttpResponse
from tests.ai_gateway_support import INPUT_SENTINEL, PROMPT_SENTINEL
from tests.ai_ollama_support import ENDPOINT, GOOD_OUTPUT, MODEL, ORIGINS, POLICIES, SCHEMA, SECRET_SENTINEL, FakeTransport, Rig, TransportError, answer, make_call, make_config, status


def reason_of(call_function):
    with_error = None
    try:
        call_function()
    except AIGatewayError as error:
        with_error = error
    return with_error


class ConfigurationTests(unittest.TestCase):
    def test_only_a_loopback_endpoint_is_accepted(self):
        for endpoint, origin in (
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            ("http://localhost:11434", "http://localhost:11434"),
            ("http://[::1]:11434", "http://[::1]:11434"),
            ("http://127.0.0.2:8080", "http://127.0.0.2:8080"),
            ("https://127.0.0.1:8443", "https://127.0.0.1:8443"),
        ):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(make_config(endpoint=endpoint, allowed_origins=(origin,)).url.endswith("/api/chat"))

    def test_every_non_loopback_endpoint_is_refused_even_when_approved(self):
        for host in (
            "10.0.0.5", "192.168.1.20", "172.16.0.1", "8.8.8.8", "169.254.169.254", "0.0.0.0", "[::]", "[fd12::5]", "[::ffff:10.0.0.5]", "ollama.corp.example.invalid",
            "api.vendor.example.invalid", "app.localhost", "localhost.example.invalid", "local.corp.example.invalid",
        ):
            for scheme in ("http", "https"):
                origin = f"{scheme}://{host}:11434"
                with self.subTest(origin=origin):
                    error = reason_of(lambda: make_config(endpoint=origin, allowed_origins=(origin,)))
                    self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_ollama_allowed_origins"))

    def test_the_url_is_the_endpoint_plus_the_native_chat_path_only(self):
        self.assertEqual(make_config().url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(make_config(endpoint="http://127.0.0.1:11434/ollama/").url, "http://127.0.0.1:11434/ollama/api/chat")
        self.assertNotIn("/v1", make_config().url)  # the native API, not the OpenAI-compatible surface
        self.assertNotIn("chat/completions", make_config().url)

    def test_the_endpoint_must_be_approved_separately(self):
        error = reason_of(lambda: make_config(endpoint="http://127.0.0.1:11435", allowed_origins=ORIGINS))
        self.assertEqual((error.code, error.reason), ("configuration_error", "ollama_endpoint_not_approved"))
        for origins in ((), "http://127.0.0.1:11434", None, ("http://127.0.0.1:11434/api",), tuple(f"http://127.0.0.{i}:1" for i in range(1, 12)), ("ftp://127.0.0.1:1",)):
            with self.subTest(origins=origins):
                self.assertEqual(reason_of(lambda: make_config(allowed_origins=origins)).reason, "invalid_ollama_allowed_origins")

    def test_a_second_approved_origin_must_also_be_loopback(self):
        self.assertEqual(len(make_config(allowed_origins=ORIGINS + ("http://localhost:11434",)).allowed_origins), 2)
        self.assertEqual(reason_of(lambda: make_config(allowed_origins=ORIGINS + ("https://llm.corp.example.invalid",))).reason, "invalid_ollama_allowed_origins")

    def test_credentials_queries_fragments_and_odd_urls_are_refused_without_echo(self):
        bad = [f"http://user:{SECRET_SENTINEL}@127.0.0.1:11434", f"http://127.0.0.1:11434/?key={SECRET_SENTINEL}", f"http://127.0.0.1:11434/#{SECRET_SENTINEL}",
               "http://2130706433:11434", "http://0x7f000001:11434", "http://127.1:11434", "http://127.0.0.1:11434/../x", "ftp://127.0.0.1:11434", "", None, 5]  # fmt: skip
        for endpoint in bad:
            with self.subTest(endpoint=endpoint):
                error = reason_of(lambda: make_config(endpoint=endpoint))
                self.assertEqual((error.code, error.reason), ("configuration_error", "invalid_ollama_endpoint"))
                self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error))

    def test_a_model_is_required_and_explicit(self):
        with self.assertRaises(TypeError):
            ollama.OllamaConfig(endpoint=ENDPOINT, allowed_origins=ORIGINS)  # no default model
        for model in ("", " ", "a b", "x" * 200, None, "../model", "-model", 5):
            with self.subTest(model=model):
                self.assertEqual(reason_of(lambda: make_config(model_id=model)).reason, "invalid_ollama_model")
        for model in ("llama3.1:8b", "library/qwen2.5:7b-instruct-q4_K_M", "mistral"):
            self.assertEqual(make_config(model_id=model).model_id, model)

    def test_there_is_no_locality_key_or_proxy_to_configure(self):
        for name in ("locality", "api_key", "approved_networks", "proxy_url"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                make_config(**{name: "x"})

    def test_limits_retries_timeouts_and_context(self):
        for name, value in (("max_input_tokens", 0), ("max_output_tokens", True), ("max_retries", 6), ("max_retries", -1), ("connect_timeout_seconds", 0), ("connect_timeout_seconds", 61), ("num_ctx", 100), ("num_ctx", True)):
            with self.subTest(name=name, value=value):
                self.assertIsNotNone(reason_of(lambda: make_config(**{name: value})))
        self.assertEqual(reason_of(lambda: make_config(num_ctx=4096, max_input_tokens=4000, max_output_tokens=500)).reason, "ollama_limits_exceed_num_ctx")
        self.assertEqual(make_config(num_ctx=8192).num_ctx, 8192)
        self.assertIsNone(make_config().num_ctx)

    def test_the_configuration_is_immutable_and_the_provider_refuses_foreign_objects(self):
        config = make_config()
        with self.assertRaises(AttributeError):
            config.endpoint = "http://127.0.0.1:1"
        for foreign_config, transport in ((object(), FakeTransport()), (config, object())):
            with self.assertRaises(AIGatewayError):
                ollama.OllamaProvider(foreign_config, transport)

    def test_the_last_check_before_a_request_refuses_any_other_destination(self):
        config = make_config()
        self.assertTrue(ollama._destination_allowed(config, config.url))
        for url in (
            "http://127.0.0.1:11435/api/chat", "http://localhost:11434/api/chat", "https://127.0.0.1:11434/api/chat", "http://10.0.0.5:11434/api/chat", "http://127.0.0.1:11434/api/pull",
            "http://127.0.0.1:11434/v1/chat/completions", "http://user@127.0.0.1:11434/api/chat", "https://api.vendor.example.invalid/api/chat", "not a url",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama._destination_allowed(config, url))


class DescriptorAndProfileTests(unittest.TestCase):
    def test_the_provider_declares_local_the_configured_model_and_both_json_modes(self):
        rig = Rig()
        descriptor = rig.provider.descriptor
        self.assertEqual((descriptor.provider_id, descriptor.kind, descriptor.locality), ("ollama", "ollama_native_chat", DataLocality.LOCAL))
        self.assertEqual(descriptor.capabilities.supported_models, frozenset({MODEL}))
        self.assertEqual(descriptor.capabilities.structured_output_modes, frozenset({StructuredOutputMode.JSON_MODE, StructuredOutputMode.NATIVE_SCHEMA}))
        self.assertFalse(descriptor.capabilities.test_only)
        self.assertEqual((rig.profile.policy_mode, rig.gateway.profile_ids), (PolicyMode.LOCAL_ONLY, ("ollama-local",)))

    def test_the_profile_holds_no_endpoint_and_no_secret(self):
        text = repr(Rig().profile)
        for hidden in (SECRET_SENTINEL, "127.0.0.1", "11434", "http://"):
            self.assertNotIn(hidden, text)


class RequestOnTheWireTests(unittest.TestCase):
    def test_a_call_posts_to_the_native_chat_path_on_loopback_with_no_credentials(self):
        rig = Rig(answer())
        rig.generate()
        request = rig.transport.last
        self.assertEqual(request.url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(rig.transport.headers(), {"Content-Type": "application/json", "Accept": "application/json"})
        self.assertIs(request.network_class, NetworkClass.LOOPBACK)
        self.assertEqual(request.approved_networks, ())

    def test_the_body_is_exactly_the_native_chat_shape_in_json_mode(self):
        rig = Rig(answer())
        rig.generate(instructions="Extract the fields.", input_text="Some document text.")
        body = rig.transport.body()
        self.assertEqual(set(body), {"model", "messages", "stream", "format", "options"})
        self.assertEqual((body["model"], body["stream"], body["format"]), (MODEL, False, "json"))
        self.assertEqual(body["options"], {"num_predict": rig.profile.limits.max_output_tokens})
        system, user = body["messages"]
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith("Extract the fields."))
        self.assertIn("JSON Schema", system["content"])
        self.assertEqual(user, {"role": "user", "content": "Some document text."})

    def test_it_is_not_the_openai_compatible_shape(self):
        rig = Rig(answer())
        rig.generate()
        body = rig.transport.body()
        for openai_only in ("response_format", "max_tokens", "temperature", "top_p", "seed", "tools", "keep_alive", "think"):
            self.assertNotIn(openai_only, body)

    def test_native_schema_mode_sends_the_schema_itself_as_format(self):
        rig = Rig(answer(), mode=StructuredOutputMode.NATIVE_SCHEMA)
        rig.generate()
        body = rig.transport.body()
        self.assertEqual(body["format"], SCHEMA)
        self.assertIn("JSON Schema", body["messages"][0]["content"])  # also described in words

    def test_the_output_limit_goes_to_num_predict_and_may_only_be_lowered(self):
        rig = Rig(answer(), timeout=40.0)
        rig.generate(max_output_tokens=100, timeout_seconds=12.0)
        self.assertEqual(rig.transport.body()["options"]["num_predict"], 100)
        self.assertEqual((rig.transport.last.read_timeout_seconds, rig.transport.last.connect_timeout_seconds), (12.0, 10.0))
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(timeout_seconds=41.0)
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_request", "timeout_above_profile"))

    def test_sampling_parameters_and_the_context_window_are_sent_only_when_set(self):
        rig = Rig(answer())
        rig.generate()
        self.assertEqual(set(rig.transport.body()["options"]), {"num_predict"})
        rig = Rig(answer(), parameters={"temperature": 0.0, "top_p": 0.9, "seed": 7}, num_ctx=8192)
        rig.generate()
        self.assertEqual(rig.transport.body()["options"], {"num_predict": 1024, "temperature": 0.0, "top_p": 0.9, "seed": 7, "num_ctx": 8192})

    def test_the_configured_model_is_the_one_sent(self):
        for model in ("mistral", "qwen2.5:7b"):
            with self.subTest(model=model):
                rig = Rig(answer(model=model), model_id=model)
                response = rig.generate()
                self.assertEqual(rig.transport.body()["model"], model)
                self.assertEqual((response.provenance.requested_model_id, response.provenance.reported_model_id), (model, model))

    def test_the_provider_refuses_another_model_or_an_unsupported_mode_before_any_request(self):
        rig = Rig()
        for call, code in ((make_call(model_id="other-model"), "unsupported_model"), (make_call(structured_output_mode=StructuredOutputMode.PROMPT_ONLY), "provider_error")):
            with self.subTest(code=code), self.assertRaises(AIGatewayError) as ctx:
                rig.provider.generate(call)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(rig.transport.requests, [])

    def test_the_prompt_and_input_reach_only_the_body(self):
        rig = Rig(answer())
        rig.generate(instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL)
        request = rig.transport.last
        self.assertNotIn(PROMPT_SENTINEL, request.url)
        self.assertEqual([n for n, v in request.headers if PROMPT_SENTINEL in v or INPUT_SENTINEL in v], [])
        self.assertIn(INPUT_SENTINEL.encode(), request.body)


class StructuredOutputTests(unittest.TestCase):
    def test_a_valid_answer_is_returned_with_provider_and_model_provenance(self):
        rig = Rig(answer(model="llama3.1:8b-instruct-q4", usage=(21, 9)))
        response = rig.generate()
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})
        p = response.provenance
        self.assertEqual((p.provider_id, p.provider_kind, p.locality, p.policy_mode), ("ollama", "ollama_native_chat", DataLocality.LOCAL, PolicyMode.LOCAL_ONLY))
        self.assertEqual((p.requested_model_id, p.reported_model_id, p.input_tokens, p.output_tokens, p.test_only), (MODEL, "llama3.1:8b-instruct-q4", 21, 9, False))

    def test_native_schema_mode_returns_a_valid_answer_too(self):
        response = Rig(answer(), mode=StructuredOutputMode.NATIVE_SCHEMA).generate()
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})

    def test_a_missing_reported_model_is_unreported_never_the_requested_one(self):
        raw = json.dumps({"message": {"role": "assistant", "content": GOOD_OUTPUT}, "done": True}).encode()
        provenance = Rig(HttpResponse(200, raw)).generate().provenance
        self.assertEqual((provenance.reported_model_id, provenance.input_tokens, provenance.output_tokens), ("unreported", None, None))

    def test_unusable_token_counts_are_dropped(self):
        provenance = Rig(answer(usage=None, prompt_eval_count=-1, eval_count=True)).generate().provenance
        self.assertEqual((provenance.input_tokens, provenance.output_tokens), (None, None))

    def test_output_that_is_not_valid_structured_json_is_refused(self):
        bad = {
            "prose": "Sure, here you go!", "fenced": "```json\n" + GOOD_OUTPUT + "\n```", "array": "[1, 2]", "wrong type": '{"name": 5}', "missing required": '{"count": 1}',
            "extra property": '{"name": "x", "other": 1}', "negative": '{"name": "x", "count": -1}', "empty": "", "nan": '{"name": "x", "count": NaN}',
        }  # fmt: skip
        for label, content in bad.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                Rig(answer(content)).generate()
            self.assertEqual(ctx.exception.code, "invalid_structured_output")

    def test_a_truncated_incomplete_or_malformed_answer_is_refused(self):
        cases = {
            "length": (answer(done_reason="length"), "invalid_structured_output"),
            "load": (answer(done_reason="load"), "provider_error"),
            "unload": (answer(done_reason="unload"), "provider_error"),
            "not done": (answer(done=False), "provider_error"),
            "done missing": (HttpResponse(200, json.dumps({"message": {"content": GOOD_OUTPUT}}).encode()), "provider_error"),
            "server error field": (answer(error="model not found"), "provider_error"),
            "null content": (HttpResponse(200, json.dumps({"message": {"content": None}, "done": True}).encode()), "provider_error"),
            "no message": (HttpResponse(200, b'{"done": true}'), "provider_error"),
            "openai shaped answer": (HttpResponse(200, json.dumps({"choices": [{"message": {"content": GOOD_OUTPUT}}]}).encode()), "provider_error"),
            "not an object": (HttpResponse(200, b"[1]"), "provider_error"),
            "not json": (HttpResponse(200, b"<html>proxy</html>"), "provider_error"),
            "ndjson stream": (HttpResponse(200, (json.dumps({"message": {"content": "{"}, "done": False}) + "\n" + json.dumps({"message": {"content": "}"}, "done": True})).encode()), "provider_error"),
            "not utf-8": (HttpResponse(200, b"\xff\xfe\x00"), "provider_error"),
            "non-finite": (HttpResponse(200, b'{"message": {"content": "{}"}, "done": true, "eval_count": NaN}'), "provider_error"),
            "deeply nested": (HttpResponse(200, b"[" * 100000), "provider_error"),
        }  # fmt: skip
        for label, (response, code) in cases.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                Rig(response).generate()
            self.assertEqual(ctx.exception.code, code)

    def test_the_provider_gives_a_fixed_reason_for_each_malformed_answer(self):
        for response, reason in ((HttpResponse(200, b"nope"), "unreadable_answer"), (answer(done_reason="length"), "output_truncated"), (answer(done=False), "incomplete_answer"),
                                 (answer(error="x"), "provider_reported_error"), (answer(done_reason="load"), "unexpected_finish_reason")):  # fmt: skip
            with self.subTest(reason=reason):
                rig = Rig(response, max_retries=0)
                self.assertEqual(reason_of(lambda: rig.provider.generate(make_call())).reason, reason)


class ErrorClassificationTests(unittest.TestCase):
    def failure(self, response, **rig_options):
        rig = Rig(*response if isinstance(response, list) else [response], **rig_options)
        return reason_of(lambda: rig.provider.generate(make_call())), rig

    def test_a_missing_model_is_unsupported_model_not_retried_and_never_pulled(self):
        body = json.dumps({"error": f"model '{MODEL}' not found, try pulling it first"}).encode()
        error, rig = self.failure(status(404, body))
        self.assertEqual((error.code, error.reason, error.retryable), ("unsupported_model", "http_404", False))
        self.assertEqual(len(rig.transport.requests), 1)
        self.assertNotIn(MODEL, str(error) + repr(error) + str(error.reason))

    def test_http_statuses_map_to_the_gateway_codes(self):
        expected = {400: "provider_error", 401: "authentication_failure", 403: "authentication_failure", 404: "unsupported_model", 408: "timeout", 413: "provider_error",
                    500: "provider_error", 501: "provider_error"}  # fmt: skip
        for code, adapter_code in expected.items():
            with self.subTest(status=code):
                error, rig = self.failure(status(code))
                self.assertEqual((error.code, error.reason, len(rig.transport.requests)), (adapter_code, f"http_{code}", 1))  # none of these is retried

    def test_a_busy_or_unavailable_server_is_retried_then_reported_with_its_code(self):
        for http_status, code in ((429, "rate_limit"), (502, "provider_unavailable"), (503, "provider_unavailable"), (504, "provider_unavailable")):
            with self.subTest(status=http_status):
                error, rig = self.failure([status(http_status)] * 3)
                self.assertEqual((error.code, error.retryable, len(rig.transport.requests), rig.sleeps), (code, True, 3, [1.0, 2.0]))

    def test_a_busy_call_that_then_succeeds_returns_the_answer(self):
        rig = Rig(status(503), answer())
        self.assertEqual(dict(rig.generate().output), {"name": "x", "count": 2})
        self.assertEqual((len(rig.transport.requests), rig.sleeps), (2, [1.0]))

    def test_retries_can_be_switched_off(self):
        error, rig = self.failure(status(503), max_retries=0)
        self.assertEqual((error.code, len(rig.transport.requests), rig.sleeps), ("provider_unavailable", 1, []))

    def test_a_redirect_is_refused_never_followed_and_never_retried(self):
        for code in (301, 302, 307, 308):
            with self.subTest(status=code):
                error, rig = self.failure(status(code, b"<a href='http://10.0.0.5/'>"))
                self.assertEqual((error.code, error.reason, len(rig.transport.requests)), ("provider_error", "redirect_refused", 1))

    def test_transport_failures_map_to_fixed_codes(self):
        cases = {"timeout": ("timeout", "transport_timeout", 1), "connect": ("provider_unavailable", "transport_connect", 3), "tls": ("provider_error", "tls_failure", 1),
                 "protocol": ("provider_error", "transport_protocol", 1), "destination": ("provider_error", "destination_refused", 1)}  # fmt: skip
        for kind, (code, reason, attempts) in cases.items():  # Ollama not running (a refused connection) is retried; a timeout may have reached the model
            with self.subTest(kind=kind):
                error, rig = self.failure([TransportError(kind)] * 3)
                self.assertEqual((error.code, error.reason, len(rig.transport.requests)), (code, reason, attempts))

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
        body = json.dumps({"error": f"bad thing {SECRET_SENTINEL} for prompt {PROMPT_SENTINEL}"}).encode()
        for code in (400, 404, 429, 500, 503):
            with self.subTest(status=code):
                error, _ = self.failure([status(code, body)] * 3)
                text = str(error) + repr(error) + str(error.reason) + str(error.args)
                self.assertNotIn(SECRET_SENTINEL, text)
                self.assertNotIn(PROMPT_SENTINEL, text)
        error, _ = self.failure(answer(error=f"oops {SECRET_SENTINEL}"))
        self.assertNotIn(SECRET_SENTINEL, str(error) + repr(error) + str(error.reason))


class DeadlineTests(unittest.TestCase):
    def test_a_slow_attempt_that_exhausts_the_deadline_stops_the_retries(self):
        rig = Rig(status(503), status(503), status(503), timeout=5.0, cost=4.0)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate()
        self.assertEqual(ctx.exception.code, "provider_unavailable")
        self.assertEqual(len(rig.transport.requests), 1)

    def test_each_attempt_gets_only_the_time_that_remains(self):
        rig = Rig(status(503), answer(), timeout=30.0, cost=10.0)
        rig.generate()
        first, second = rig.transport.requests
        self.assertEqual((first.read_timeout_seconds, second.read_timeout_seconds), (30.0, 30.0 - 10.0 - 1.0))

    def test_an_answer_that_arrives_after_the_deadline_is_refused_by_the_gateway(self):
        rig = Rig(answer(), timeout=5.0, cost=6.0)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate()
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("timeout", "answer_after_deadline"))

    def test_the_connect_timeout_never_exceeds_the_time_left(self):
        rig = Rig(answer(), timeout=3.0)
        rig.generate(timeout_seconds=2.0)
        self.assertEqual(rig.transport.last.connect_timeout_seconds, 2.0)


class NoFallbackAndNoModelManagementTests(unittest.TestCase):
    def test_every_attempt_goes_to_the_same_endpoint_and_model(self):
        rig = Rig(status(503), status(429), status(502))
        with self.assertRaises(AIGatewayError):
            rig.generate()
        self.assertEqual({r.url for r in rig.transport.requests}, {"http://127.0.0.1:11434/api/chat"})
        self.assertEqual({json.loads(r.body)["model"] for r in rig.transport.requests}, {MODEL})

    def test_no_request_ever_goes_to_a_model_management_or_other_route(self):
        scripts = [[answer()], [status(404)], [status(503)] * 3, [TransportError("connect")] * 3, [answer(done_reason="length")]]
        for script in scripts:
            rig = Rig(*script)
            try:
                rig.generate()
            except AIGatewayError:
                pass
            for request in rig.transport.requests:
                with self.subTest(script=len(script)):
                    self.assertEqual(request.url, "http://127.0.0.1:11434/api/chat")
        for route in ("pull", "create", "delete", "tags", "generate", "show", "copy", "push"):
            self.assertFalse(ollama._destination_allowed(make_config(), f"http://127.0.0.1:11434/api/{route}"), route)

    def test_a_failed_call_is_not_answered_by_another_provider_model_or_mode(self):
        rig = Rig(status(404))
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate()
        self.assertEqual(ctx.exception.code, "unsupported_model")
        self.assertEqual((len(rig.transport.requests), rig.gateway.profile_ids), (1, ("ollama-local",)))
        self.assertEqual(rig.provider.descriptor.capabilities.supported_models, frozenset({MODEL}))

    def test_invalid_output_is_not_retried_or_repaired(self):
        rig = Rig(answer("not json"))
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate()
        self.assertEqual(ctx.exception.code, "invalid_structured_output")
        self.assertEqual(len(rig.transport.requests), 1)

    def test_the_adapter_names_no_route_but_the_chat_one(self):
        import ast

        with open(ollama.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        docstrings = {id(node.body[0].value) for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr)}
        literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]
        self.assertEqual([text for text in literals if "api/" in text], ["/api/chat"])


class DataLocalityPolicyTests(unittest.TestCase):
    def test_ollama_is_permitted_by_every_policy_mode_and_is_declared_local(self):
        for name in ("local_only", "company_endpoint", "approved_cloud"):
            with self.subTest(policy=name):
                rig = Rig(answer())
                self.assertEqual(dict(rig.generate(POLICIES[name]).output), {"name": "x", "count": 2})
                self.assertEqual(len(rig.transport.requests), 1)
                self.assertIs(rig.transport.last.network_class, NetworkClass.LOOPBACK)

    def test_approved_cloud_needs_no_provider_approval_for_a_local_provider(self):
        rig = Rig(answer())
        self.assertEqual(dict(rig.generate(PolicyContext(PolicyMode.APPROVED_CLOUD)).output), {"name": "x", "count": 2})

    def test_a_policy_that_is_not_a_policy_refuses_before_the_adapter_runs(self):
        rig = Rig(answer())
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(policy="local_only")
        self.assertEqual(ctx.exception.code, "configuration_error")
        self.assertEqual(rig.transport.requests, [])

    def test_there_is_no_cloud_fallback_when_ollama_is_down(self):
        rig = Rig(*[TransportError("connect")] * 3)
        with self.assertRaises(AIGatewayError) as ctx:
            rig.generate(POLICIES["approved_cloud"])
        self.assertEqual(ctx.exception.code, "provider_unavailable")
        self.assertEqual({r.url for r in rig.transport.requests}, {"http://127.0.0.1:11434/api/chat"})
        self.assertEqual({r.network_class for r in rig.transport.requests}, {NetworkClass.LOOPBACK})


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

    def test_no_prompt_input_or_secret_reaches_a_log_line_an_error_or_a_repr(self):
        scripts = [[answer()], [status(404)], [status(503)] * 3, [TransportError("connect")] * 3, [RuntimeError(SECRET_SENTINEL)], [answer("not json")], [status(302)], [answer(error=SECRET_SENTINEL)]]
        for script in scripts:
            rig = Rig(*script)
            texts = []
            try:
                rig.generate(instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL)
            except AIGatewayError as error:
                texts.append("".join(traceback.format_exception(error)) + f"{error} {error!r} {error.args} {error.reason} {error.violations}")
            texts += [repr(rig.provider), repr(rig.config), repr(rig.profile), repr(rig.gateway), *map(repr, rig.transport.requests)]
            blob = "\n".join(texts) + "\n" + "\n".join(self.capture.lines)
            for sentinel in (PROMPT_SENTINEL, INPUT_SENTINEL, SECRET_SENTINEL):
                with self.subTest(script=len(script), sentinel=sentinel[:8]):
                    self.assertNotIn(sentinel, blob)
            self.capture.lines.clear()

    def test_the_request_repr_hides_the_body(self):
        rig = Rig(answer())
        rig.generate(input_text=INPUT_SENTINEL)
        self.assertNotIn(INPUT_SENTINEL, repr(rig.transport.last))


if __name__ == "__main__":
    unittest.main()
