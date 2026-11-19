"""DB-free tests of the gateway call flow: success, limits, error mapping, timeouts, output validation and start-up configuration checks."""

import unittest

from apps.ai_gateway import (
    AIGateway,
    AIGatewayError,
    DataLocality,
    GenerationParameters,
    PolicyMode,
    ProviderRegistry,
    ProviderResult,
    StructuredOutputConfig,
    StructuredOutputMode,
    TokenLimits,
)
from apps.ai_gateway.errors import ADAPTER_CODES, ERROR_CODES, MESSAGES
from tests.ai_gateway_support import GOOD_OUTPUT, FakeClock, ScriptedProvider, local_policy, make_gateway, make_profile, make_request


def run(provider_behaviour=None, request=None, *, clock=None, provider=None, profile=None):
    provider = provider or ScriptedProvider(behaviour=provider_behaviour)
    gateway = make_gateway(provider, profiles=(profile or make_profile(),), clock=clock)
    return provider, gateway.generate(request or make_request(), policy=local_policy())


def fails(testcase, behaviour, request=None, *, clock=None, profile=None):
    provider = ScriptedProvider(behaviour=behaviour)
    gateway = make_gateway(provider, profiles=(profile or make_profile(),), clock=clock)
    with testcase.assertRaises(AIGatewayError) as ctx:
        gateway.generate(request or make_request(), policy=local_policy())
    return ctx.exception


def returns(raw, **kwargs):
    return lambda call: ProviderResult(raw, kwargs.pop("model", "model-a"), **kwargs)


class SuccessTests(unittest.TestCase):
    def test_a_valid_answer_is_returned_validated_and_immutable(self):
        provider, response = run()
        self.assertEqual(dict(response.output), {"name": "x", "count": 2})
        with self.assertRaises(TypeError):
            response.output["name"] = "changed"
        self.assertEqual(len(provider.calls), 1)

    def test_provenance_records_metadata_only(self):
        clock = FakeClock()

        def slow(call):
            clock.advance(2.5)
            return ProviderResult(GOOD_OUTPUT, "model-a-2026", 11, 6)

        _, response = run(slow, clock=clock)
        p = response.provenance
        self.assertEqual((p.profile_id, p.provider_id, p.provider_kind), ("profile-a", "scripted", "scripted_test"))
        self.assertEqual((p.requested_model_id, p.reported_model_id), ("model-a", "model-a-2026"))
        self.assertEqual((p.locality, p.policy_mode, p.test_only), (DataLocality.LOCAL, PolicyMode.LOCAL_ONLY, False))
        self.assertEqual((p.duration_seconds, p.input_tokens, p.output_tokens), (2.5, 11, 6))

    def test_the_provider_receives_the_profile_and_request_values_and_no_policy_information(self):
        profile = make_profile(parameters=GenerationParameters(temperature=0.3, seed=4), structured_output=StructuredOutputConfig(StructuredOutputMode.JSON_MODE))
        provider, _ = run(provider=ScriptedProvider(), profile=profile)
        call = provider.calls[0]
        self.assertEqual((call.model_id, call.timeout_seconds, call.max_output_tokens), ("model-a", 10.0, 1_000))
        self.assertIs(call.structured_output_mode, StructuredOutputMode.JSON_MODE)
        self.assertEqual((call.parameters.temperature, call.parameters.seed), (0.3, 4))
        self.assertEqual({f for f in vars(call)}, {"model_id", "instructions", "input_text", "output_schema", "structured_output_mode", "timeout_seconds", "max_output_tokens", "parameters"})

    def test_a_request_may_lower_the_timeout_and_the_output_limit(self):
        provider, _ = run(request=make_request(timeout_seconds=2, max_output_tokens=50))
        self.assertEqual((provider.calls[0].timeout_seconds, provider.calls[0].max_output_tokens), (2.0, 50))

    def test_a_request_may_not_raise_the_timeout_or_the_output_limit(self):
        for overrides, reason in (({"timeout_seconds": 10.5}, "timeout_above_profile"), ({"max_output_tokens": 1_001}, "output_limit_above_profile")):
            with self.subTest(overrides):
                provider = ScriptedProvider()
                gateway = make_gateway(provider)
                with self.assertRaises(AIGatewayError) as ctx:
                    gateway.generate(make_request(**overrides), policy=local_policy())
                self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_request", reason))
                self.assertEqual(provider.calls, [])

    def test_oversized_input_fails_explicitly_and_is_never_truncated(self):
        provider = ScriptedProvider()
        gateway = make_gateway(provider, profiles=(make_profile(limits=TokenLimits(max_input_tokens=100, max_output_tokens=50)),))
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(input_text="word " * 500), policy=local_policy())
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_request", "input_too_large"))
        self.assertEqual(provider.calls, [])

    def test_the_gateway_rejects_things_that_are_not_requests(self):
        gateway = make_gateway()
        for bad in (None, {"profile_id": "profile-a"}, "x"):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                gateway.generate(bad, policy=local_policy())
            self.assertEqual(ctx.exception.code, "invalid_request")

    def test_an_unknown_profile_is_a_configuration_error_without_calling_any_provider(self):
        provider = ScriptedProvider()
        gateway = make_gateway(provider)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(profile_id="unknown"), policy=local_policy())
        self.assertEqual(ctx.exception.code, "configuration_error")
        self.assertEqual(provider.calls, [])


class ErrorMappingTests(unittest.TestCase):
    def test_every_adapter_code_is_reported_with_the_same_code_and_a_fixed_message(self):
        for code in sorted(ADAPTER_CODES):
            with self.subTest(code):
                def behaviour(call, code=code):
                    raise AIGatewayError(code, "provider detail: should never be shown")

                error = fails(self, behaviour)
                self.assertEqual(error.code, code)
                self.assertEqual(str(error), MESSAGES[code])
                self.assertEqual((error.provider_id, error.profile_id), ("scripted", "profile-a"))

    def test_retryability_follows_the_code(self):
        for code, retryable in (("provider_unavailable", True), ("timeout", True), ("rate_limit", True), ("authentication_failure", False), ("unsupported_model", False)):
            with self.subTest(code):
                def behaviour(call, code=code):
                    raise AIGatewayError(code)

                self.assertEqual(fails(self, behaviour).retryable, retryable)

    def test_an_adapter_cannot_claim_a_verdict_that_belongs_to_the_gateway(self):
        for code in sorted(ERROR_CODES - ADAPTER_CODES):
            with self.subTest(code):
                def behaviour(call, code=code):
                    raise AIGatewayError(code)

                self.assertEqual(fails(self, behaviour).code, "provider_error")

    def test_a_foreign_exception_becomes_provider_error_and_carries_no_context(self):
        def behaviour(call):
            raise RuntimeError("secret detail " + call.input_text)

        error = fails(self, behaviour)
        self.assertEqual(error.code, "provider_error")
        self.assertEqual(str(error), MESSAGES["provider_error"])
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_a_keyboard_interrupt_is_not_swallowed(self):
        def behaviour(call):
            raise KeyboardInterrupt

        gateway = make_gateway(ScriptedProvider(behaviour=behaviour))
        with self.assertRaises(KeyboardInterrupt):
            gateway.generate(make_request(), policy=local_policy())

    def test_malformed_provider_results_are_provider_errors(self):
        bad_results = {
            "not a result": lambda call: GOOD_OUTPUT,
            "none": lambda call: None,
            "raw not str": lambda call: ProviderResult(b"{}", "model-a"),
            "reported model not str": lambda call: ProviderResult(GOOD_OUTPUT, None),
            "reported model invalid": lambda call: ProviderResult(GOOD_OUTPUT, "bad model\n"),
            "negative usage": lambda call: ProviderResult(GOOD_OUTPUT, "model-a", -1, 1),
            "bool usage": lambda call: ProviderResult(GOOD_OUTPUT, "model-a", 1, True),
            "str usage": lambda call: ProviderResult(GOOD_OUTPUT, "model-a", "1", 1),
        }
        for label, behaviour in bad_results.items():
            with self.subTest(label):
                self.assertEqual(fails(self, behaviour).code, "provider_error")


class TimeoutTests(unittest.TestCase):
    def test_an_answer_that_arrives_after_the_deadline_is_refused(self):
        clock = FakeClock()

        def late(call):
            clock.advance(call.timeout_seconds + 0.001)
            return ProviderResult(GOOD_OUTPUT, "model-a")

        error = fails(self, late, clock=clock)
        self.assertEqual((error.code, error.reason, error.retryable), ("timeout", "answer_after_deadline", True))

    def test_an_answer_exactly_on_the_deadline_is_accepted(self):
        clock = FakeClock()

        def on_time(call):
            clock.advance(call.timeout_seconds)
            return ProviderResult(GOOD_OUTPUT, "model-a")

        _, response = run(on_time, clock=clock)
        self.assertEqual(response.provenance.duration_seconds, 10.0)

    def test_the_request_timeout_is_the_deadline_when_it_is_lower(self):
        clock = FakeClock()

        def late(call):
            clock.advance(3)
            return ProviderResult(GOOD_OUTPUT, "model-a")

        self.assertEqual(fails(self, late, make_request(timeout_seconds=2), clock=clock).code, "timeout")

    def test_a_provider_reported_timeout_is_a_retryable_timeout(self):
        def behaviour(call):
            raise AIGatewayError("timeout")

        error = fails(self, behaviour)
        self.assertEqual((error.code, error.retryable), ("timeout", True))


class OutputValidationTests(unittest.TestCase):
    def invalid(self, raw, reason, profile=None, **kwargs):
        error = fails(self, returns(raw, **kwargs), profile=profile)
        self.assertEqual((error.code, error.reason), ("invalid_structured_output", reason), raw)
        self.assertFalse(error.retryable)
        return error

    def test_text_that_is_not_json_is_refused(self):
        for raw in ("", "not json", '{"name": ', "```json\n{}\n```", "{'name': 'x'}", '{"name": "x"} trailing', "﻿{}"):
            with self.subTest(raw):
                self.invalid(raw, "not_json")

    def test_non_standard_json_is_refused(self):
        for raw in ('{"name": "x", "count": NaN}', '{"name": "x", "count": Infinity}', '{"name": "x", "count": -Infinity}', '{"name": "x", "name": "y"}'):
            with self.subTest(raw):
                self.invalid(raw, "not_json")

    def test_deeply_nested_json_is_refused_not_crashed_on(self):
        roomy = make_profile(limits=TokenLimits(max_input_tokens=10_000, max_output_tokens=100_000))
        error = fails(self, returns("[" * 5000 + "]" * 5000), profile=roomy)  # the parser's recursion limit (caught) or the depth bound, by interpreter
        self.assertEqual(error.code, "invalid_structured_output")
        self.assertIn(error.reason, {"not_json", "invalid_json_value"})
        self.invalid('{"a":' * 40 + "1" + "}" * 40, "invalid_json_value", profile=roomy)  # parses, but is deeper than the bound

    def test_valid_json_that_is_not_an_object_is_refused(self):
        for raw in ("[]", '"x"', "5", "null", "true"):
            with self.subTest(raw):
                self.invalid(raw, "not_an_object")

    def test_output_that_does_not_match_the_schema_is_refused_with_safe_violations(self):
        error = self.invalid('{"name": 5, "count": -1, "extra": "PRIVATE-TEXT"}', "schema_violation")
        self.assertEqual(set(error.violations), {"$.name: type", "$.count: minimum", "$: additional_property"})
        self.assertNotIn("PRIVATE-TEXT", f"{error} {error.violations}")

    def test_a_missing_required_field_is_not_filled_in(self):
        error = self.invalid('{"count": 1}', "schema_violation")
        self.assertEqual(error.violations, ("$.name: required",))

    def test_output_over_the_output_limit_is_refused_not_truncated(self):
        big = '{"name": "' + "x" * 5_000 + '"}'
        self.invalid(big, "output_too_large")
        profile = make_profile(limits=TokenLimits(max_input_tokens=10_000, max_output_tokens=1_000))
        error = fails(self, returns(GOOD_OUTPUT, input_tokens=1, output_tokens=1_001), profile=profile)
        self.assertEqual(error.reason, "output_too_large")

    def test_a_request_may_lower_the_output_limit_and_it_is_enforced(self):
        error = fails(self, returns('{"name": "' + "x" * 200 + '"}'), make_request(max_output_tokens=10))
        self.assertEqual(error.reason, "output_too_large")

    def test_a_well_formed_answer_is_accepted(self):
        for raw in ('{"name": "x"}', ' {"name": "x", "count": 0} ', '{"count": 3, "name": "ünï"}'):
            with self.subTest(raw):
                _, response = run(returns(raw))
                self.assertIn("name", response.output)


class ConfigurationChecksAtConstructionTests(unittest.TestCase):
    def build(self, provider=None, profile=None, **kwargs):
        provider = provider or ScriptedProvider()
        return AIGateway(ProviderRegistry.build([provider]), [profile or make_profile(provider_id=provider.descriptor.provider_id)], **kwargs)

    def assertConfig(self, reason, **kwargs):
        with self.assertRaises(AIGatewayError) as ctx:
            self.build(**kwargs)
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", reason))

    def test_the_basic_gateway_builds(self):
        self.assertEqual(self.build().profile_ids, ("profile-a",))

    def test_a_gateway_may_have_no_profile_and_then_every_call_fails_closed(self):
        gateway = AIGateway(ProviderRegistry.build([ScriptedProvider()]), [])
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=local_policy())
        self.assertEqual(ctx.exception.reason, "unknown_profile")

    def test_a_profile_must_name_a_registered_provider(self):
        self.assertConfig("unknown_provider", profile=make_profile(provider_id="elsewhere"))

    def test_duplicate_profile_ids_are_refused(self):
        provider = ScriptedProvider()
        with self.assertRaises(AIGatewayError) as ctx:
            AIGateway(ProviderRegistry.build([provider]), [make_profile(), make_profile()])
        self.assertEqual(ctx.exception.reason, "duplicate_profile_id")

    def test_only_profile_objects_and_registries_are_accepted(self):
        with self.assertRaises(AIGatewayError) as ctx:
            AIGateway(ProviderRegistry.build([]), [{"profile_id": "x"}])
        self.assertEqual(ctx.exception.reason, "invalid_profile_object")
        for bad in ("profile-a", None, 5):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                AIGateway(ProviderRegistry.build([]), bad)
        for bad in (None, [], "registry"):
            with self.subTest(registry=bad), self.assertRaises(AIGatewayError) as ctx:
                AIGateway(bad, [])
            self.assertEqual(ctx.exception.reason, "invalid_registry")
        with self.assertRaises(AIGatewayError):
            AIGateway(ProviderRegistry.build([]), [], allow_test_providers="yes")

    def test_a_model_the_provider_does_not_support_is_unsupported_model(self):
        with self.assertRaises(AIGatewayError) as ctx:
            self.build(profile=make_profile(model_id="other-model"))
        self.assertEqual(ctx.exception.code, "unsupported_model")

    def test_a_profile_that_needs_more_than_the_provider_declares_is_capability_missing(self):
        cases = {
            "structured mode": (ScriptedProvider(modes={StructuredOutputMode.PROMPT_ONLY}), make_profile()),
            "input limit": (ScriptedProvider(max_input_tokens=100), make_profile()),
            "output limit": (ScriptedProvider(max_output_tokens=100), make_profile()),
            "parameter": (ScriptedProvider(parameters=frozenset()), make_profile(parameters=GenerationParameters(temperature=0.1))),
        }
        for label, (provider, profile) in cases.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                self.build(provider=provider, profile=profile)
            self.assertEqual(ctx.exception.code, "capability_missing", label)

    def test_a_test_only_provider_is_refused_unless_explicitly_allowed(self):
        provider = ScriptedProvider(test_only=True)
        self.assertConfig("test_provider_not_allowed", provider=provider)
        with self.assertRaises(AIGatewayError):  # even when no profile uses it
            AIGateway(ProviderRegistry.build([provider]), [])
        self.build(provider=ScriptedProvider(test_only=True), allow_test_providers=True)

    def test_a_gateway_is_immutable(self):
        gateway = self.build()
        for name in ("_registry", "_profiles", "_clock", "extra"):
            with self.subTest(name):
                with self.assertRaises(AttributeError):
                    setattr(gateway, name, None)
                with self.assertRaises(AttributeError):
                    delattr(gateway, name)

    def test_a_profile_list_changed_afterwards_changes_nothing(self):
        profiles = [make_profile()]
        gateway = AIGateway(ProviderRegistry.build([ScriptedProvider()]), profiles)
        profiles.clear()
        self.assertEqual(gateway.profile_ids, ("profile-a",))
