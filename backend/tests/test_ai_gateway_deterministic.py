"""DB-free tests of the deterministic TEST provider: repeatable output, scenarios, no network, never selected in production."""

import unittest
from unittest import mock

from apps.ai_gateway import AIGateway, AIGatewayError, DataLocality, GenerationRequest, PolicyContext, PolicyMode, ProviderRegistry
from apps.ai_gateway.adapters.deterministic import (
    MODEL_ID,
    PROVIDER_ID,
    PROVIDER_KIND,
    DeterministicTestProvider,
    Scenario,
    deterministic_test_profile,
    fixture_key,
)
from tests.ai_gateway_support import INPUT_SENTINEL, SCHEMA


def gateway_for(scenario=Scenario.SUCCESS, **kwargs):
    provider = DeterministicTestProvider(scenario, **kwargs)
    return provider, AIGateway(ProviderRegistry.build([provider]), [deterministic_test_profile()], allow_test_providers=True)


def request(schema=SCHEMA, text="Some document.", **kwargs):
    return GenerationRequest("deterministic-test", "Extract.", text, schema, **kwargs)


LOCAL = PolicyContext(PolicyMode.LOCAL_ONLY)


class IdentityTests(unittest.TestCase):
    def test_it_is_declared_a_local_test_only_fixture_provider_and_not_real_ai(self):
        descriptor = DeterministicTestProvider().descriptor
        self.assertEqual((descriptor.provider_id, descriptor.kind, descriptor.locality), (PROVIDER_ID, PROVIDER_KIND, DataLocality.LOCAL))
        self.assertTrue(descriptor.capabilities.test_only)
        self.assertEqual(descriptor.capabilities.supported_models, {MODEL_ID})
        self.assertIn("fixture", descriptor.kind)
        self.assertIn("test", PROVIDER_ID)

    def test_responses_are_marked_as_test_output(self):
        _, gateway = gateway_for()
        provenance = gateway.generate(request(), policy=LOCAL).provenance
        self.assertTrue(provenance.test_only)
        self.assertEqual((provenance.provider_kind, provenance.reported_model_id), (PROVIDER_KIND, MODEL_ID))

    def test_it_is_refused_by_a_gateway_that_does_not_explicitly_allow_test_providers(self):
        with self.assertRaises(AIGatewayError) as ctx:
            AIGateway(ProviderRegistry.build([DeterministicTestProvider()]), [deterministic_test_profile()])
        self.assertEqual(ctx.exception.reason, "test_provider_not_allowed")

    def test_the_default_profile_is_local_only(self):
        self.assertIs(deterministic_test_profile().policy_mode, PolicyMode.LOCAL_ONLY)


class SuccessTests(unittest.TestCase):
    def test_the_same_input_gives_the_same_output_every_time(self):
        _, gateway = gateway_for()
        first = gateway.generate(request(), policy=LOCAL)
        for _ in range(3):
            again = gateway.generate(request(), policy=LOCAL)
            self.assertEqual(dict(again.output), dict(first.output))
        other_instance = gateway_for()[1].generate(request(), policy=LOCAL)
        self.assertEqual(dict(other_instance.output), dict(first.output))

    def test_the_output_depends_on_the_input_and_validates_against_the_schema(self):
        _, gateway = gateway_for()
        a = gateway.generate(request(text="one"), policy=LOCAL).output
        b = gateway.generate(request(text="two"), policy=LOCAL).output
        self.assertNotEqual(dict(a), dict(b))
        self.assertEqual(set(a), {"name", "count"})
        self.assertTrue(a["name"].startswith("name-"))
        self.assertEqual(a["count"], 0)

    def test_generated_values_respect_the_schema_constraints(self):
        schema = {
            "type": "object",
            "properties": {
                "short": {"type": "string", "maxLength": 3},
                "long": {"type": "string", "minLength": 30},
                "n": {"type": "integer", "minimum": 5, "maximum": 9},
                "m": {"type": "integer", "maximum": -2},
                "x": {"type": "number", "minimum": 1.5},
                "e": {"type": "string", "enum": ["b", "a"]},
                "flag": {"type": "boolean"},
                "nothing": {"type": "null"},
                "opt": {"type": ["null", "string"]},
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                "plain": {"type": "array"},
                "child": {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]},
            },
            "required": ["short"],
        }
        _, gateway = gateway_for()
        out = gateway.generate(request(schema), policy=LOCAL).output  # the gateway validates it against the schema
        self.assertEqual((len(out["short"]), len(out["long"]) >= 30, out["n"], out["m"], out["e"], out["flag"], out["nothing"]), (3, True, 5, -2, "b", False, None))
        self.assertEqual((out["x"], len(out["tags"]), out["plain"], dict(out["child"])), (2.0, 2, (), {"k": 0}))
        self.assertTrue(out["opt"].startswith("opt-"))  # a nullable string is generated as a string

    def test_a_registered_fixture_is_returned_for_its_exact_input(self):
        fixtures = {fixture_key("the invoice"): {"name": "Fixture Co", "count": 7}}
        _, gateway = gateway_for(fixtures=fixtures)
        self.assertEqual(dict(gateway.generate(request(text="the invoice"), policy=LOCAL).output), {"name": "Fixture Co", "count": 7})
        self.assertNotEqual(dict(gateway.generate(request(text="the invoice "), policy=LOCAL).output).get("name"), "Fixture Co")

    def test_an_invalid_fixture_is_still_refused_by_the_gateway(self):
        fixtures = {fixture_key("doc"): {"name": 5}}
        _, gateway = gateway_for(fixtures=fixtures)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(request(text="doc"), policy=LOCAL)
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_structured_output", "schema_violation"))

    def test_a_fixture_must_be_plain_json(self):
        with self.assertRaises(AIGatewayError):
            DeterministicTestProvider(fixtures={"k": {"v": object()}})

    def test_it_reports_deterministic_usage(self):
        _, gateway = gateway_for()
        provenance = gateway.generate(request(text="abc"), policy=LOCAL).provenance
        self.assertIsInstance(provenance.input_tokens, int)
        self.assertIsInstance(provenance.output_tokens, int)


class FailureScenarioTests(unittest.TestCase):
    EXPECTED = {
        Scenario.TIMEOUT: ("timeout", True),
        Scenario.UNAVAILABLE: ("provider_unavailable", True),
        Scenario.RATE_LIMITED: ("rate_limit", True),
        Scenario.AUTHENTICATION_FAILURE: ("authentication_failure", False),
        Scenario.UNSUPPORTED_MODEL: ("unsupported_model", False),
        Scenario.UNEXPECTED_ERROR: ("provider_error", False),
        Scenario.MALFORMED_OUTPUT: ("invalid_structured_output", False),
        Scenario.NOT_AN_OBJECT: ("invalid_structured_output", False),
        Scenario.SCHEMA_MISMATCH: ("invalid_structured_output", False),
    }

    def test_every_scenario_maps_to_its_stable_code(self):
        self.assertEqual(set(self.EXPECTED) | {Scenario.SUCCESS}, set(Scenario))
        for scenario, (code, retryable) in self.EXPECTED.items():
            with self.subTest(scenario.value):
                provider, gateway = gateway_for(scenario)
                with self.assertRaises(AIGatewayError) as ctx:
                    gateway.generate(request(), policy=LOCAL)
                self.assertEqual((ctx.exception.code, ctx.exception.retryable), (code, retryable))
                self.assertEqual(provider.call_count, 1)

    def test_malformed_output_and_schema_mismatch_are_distinguished(self):
        reasons = {}
        for scenario in (Scenario.MALFORMED_OUTPUT, Scenario.NOT_AN_OBJECT, Scenario.SCHEMA_MISMATCH):
            with self.assertRaises(AIGatewayError) as ctx:
                gateway_for(scenario)[1].generate(request(), policy=LOCAL)
            reasons[scenario] = ctx.exception.reason
        self.assertEqual(reasons, {Scenario.MALFORMED_OUTPUT: "not_json", Scenario.NOT_AN_OBJECT: "not_an_object", Scenario.SCHEMA_MISMATCH: "schema_violation"})

    def test_an_unknown_scenario_is_refused(self):
        with self.assertRaises(ValueError):
            DeterministicTestProvider("explode")

    def test_the_unexpected_error_scenario_carries_the_input_in_its_own_text_and_the_gateway_drops_it(self):
        provider = DeterministicTestProvider(Scenario.UNEXPECTED_ERROR)
        with self.assertRaises(RuntimeError) as raw:
            provider.generate(mock.Mock(model_id=MODEL_ID, input_text=INPUT_SENTINEL))
        self.assertIn(INPUT_SENTINEL, str(raw.exception))  # the premise: the provider's own error leaks
        _, gateway = gateway_for(Scenario.UNEXPECTED_ERROR)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(request(text=INPUT_SENTINEL), policy=LOCAL)
        self.assertNotIn(INPUT_SENTINEL, f"{ctx.exception} {ctx.exception!r}")


class NoNetworkNoCredentialsTests(unittest.TestCase):
    def test_a_full_call_needs_no_network_and_no_environment(self):
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("socket.socket", refuse), mock.patch("socket.create_connection", refuse), mock.patch("socket.getaddrinfo", refuse):
            for scenario in Scenario:
                _, gateway = gateway_for(scenario)
                try:
                    gateway.generate(request(), policy=LOCAL)
                except AIGatewayError:
                    pass

    def test_the_provider_makes_every_decision_from_the_call_alone(self):
        a, b = DeterministicTestProvider(), DeterministicTestProvider()
        gateway_a = AIGateway(ProviderRegistry.build([a]), [deterministic_test_profile()], allow_test_providers=True)
        gateway_b = AIGateway(ProviderRegistry.build([b]), [deterministic_test_profile()], allow_test_providers=True)
        self.assertEqual(dict(gateway_a.generate(request(), policy=LOCAL).output), dict(gateway_b.generate(request(), policy=LOCAL).output))
