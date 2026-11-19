"""DB-free tests of the AI Gateway contract values: model profiles, requests, descriptors, capabilities and the error table."""

import dataclasses
import re
import unittest

from apps.ai_gateway import (
    ERROR_CODES,
    RETRYABLE_CODES,
    AIGatewayError,
    DataLocality,
    GenerationParameters,
    GenerationRequest,
    GenerationResponse,
    ModelProfile,
    PolicyMode,
    Provenance,
    ProviderCapabilities,
    ProviderDescriptor,
    StructuredOutputConfig,
    StructuredOutputMode,
    TokenLimits,
)
from apps.ai_gateway.errors import ADAPTER_CODES, MESSAGES
from tests.ai_gateway_support import INPUT_SENTINEL, PROMPT_SENTINEL, SCHEMA, make_profile, make_request


class ModelProfileTests(unittest.TestCase):
    def test_a_valid_profile_holds_exactly_what_the_task_requires(self):
        profile = make_profile(parameters=GenerationParameters(temperature=0.2, top_p=0.9, seed=7))
        self.assertEqual(profile.profile_id, "profile-a")
        self.assertEqual(profile.provider_id, "scripted")
        self.assertEqual(profile.model_id, "model-a")
        self.assertIs(profile.policy_mode, PolicyMode.LOCAL_ONLY)
        self.assertEqual(profile.timeout_seconds, 10.0)
        self.assertEqual((profile.limits.max_input_tokens, profile.limits.max_output_tokens), (10_000, 1_000))
        self.assertIs(profile.structured_output.mode, StructuredOutputMode.NATIVE_SCHEMA)
        self.assertEqual(profile.parameters.names(), {"temperature", "top_p", "seed"})

    def test_a_profile_is_immutable(self):
        profile = make_profile()
        for name, value in (("model_id", "other"), ("policy_mode", PolicyMode.APPROVED_CLOUD), ("timeout_seconds", 1.0)):
            with self.subTest(name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(profile, name, value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.limits.max_output_tokens = 5

    def test_a_profile_holds_no_credential_endpoint_or_secret_field(self):
        pattern = re.compile(r"api_?key|secret|password|credential|authorization|bearer|endpoint|url|header|cert", re.IGNORECASE)
        names = set()
        for cls in (ModelProfile, TokenLimits, StructuredOutputConfig, GenerationParameters):
            names |= {f.name for f in dataclasses.fields(cls)}
        self.assertEqual({n for n in names if pattern.search(n)}, set())

    def test_a_profile_cannot_be_given_a_credential_field(self):
        for extra in ("api_key", "credential_ref", "endpoint", "password"):
            with self.subTest(extra), self.assertRaises(TypeError):
                ModelProfile(
                    profile_id="p", provider_id="q", model_id="m", policy_mode="local_only", timeout_seconds=1,
                    limits=TokenLimits(10, 10), **{extra: "x"},
                )  # fmt: skip

    def test_invalid_identifiers_are_refused(self):
        bad = ["", "A", "1abc", "has space", "a" * 65, "line\nbreak", "tab\t", "a.b", None, 5, "x\n"]
        for value in bad:
            for field in ("profile_id", "provider_id"):
                with self.subTest(field=field, value=value), self.assertRaises(AIGatewayError) as ctx:
                    make_profile(**{field: value})
                self.assertEqual(ctx.exception.code, "configuration_error")
        for value in ["", " m", "m n", "-m", "x" * 129, None, "m\n", "m;rm"]:
            with self.subTest(model_id=value), self.assertRaises(AIGatewayError):
                make_profile(model_id=value)

    def test_policy_mode_must_be_one_of_the_three_modes(self):
        for good in ("local_only", "company_endpoint", "approved_cloud", PolicyMode.LOCAL_ONLY):
            with self.subTest(good):
                self.assertIsInstance(make_profile(policy_mode=good).policy_mode, PolicyMode)
        for bad in ("", "cloud", "LOCAL_ONLY", "approved_external", None, 1, "local_only "):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                make_profile(policy_mode=bad)
            self.assertEqual(ctx.exception.reason, "invalid_policy_mode")

    def test_timeout_must_be_positive_finite_and_bounded(self):
        for good in (0.1, 1, 300, 300.0):
            with self.subTest(good):
                make_profile(timeout_seconds=good)
        for bad in (0, -1, 300.1, float("nan"), float("inf"), True, "5", None):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                make_profile(timeout_seconds=bad)

    def test_token_limits_must_be_positive_bounded_integers(self):
        for bad in (0, -1, 1.5, True, "10", None, 10_000_001):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                TokenLimits(max_input_tokens=bad, max_output_tokens=10)
        for bad in (0, -1, 1.5, True, None, 1_000_001):
            with self.subTest(out=bad), self.assertRaises(AIGatewayError):
                TokenLimits(max_input_tokens=10, max_output_tokens=bad)

    def test_generation_parameters_are_optional_and_bounded(self):
        self.assertEqual(GenerationParameters().names(), frozenset())
        GenerationParameters(temperature=0, top_p=1, seed=0)
        for kwargs in ({"temperature": -0.1}, {"temperature": 2.1}, {"temperature": float("nan")}, {"temperature": True}, {"top_p": 0}, {"top_p": 1.1}, {"seed": -1}, {"seed": 2**31}, {"seed": 1.5}, {"seed": True}):
            with self.subTest(kwargs), self.assertRaises(AIGatewayError):
                GenerationParameters(**kwargs)

    def test_structured_output_mode_must_be_known(self):
        for good in StructuredOutputMode:
            StructuredOutputConfig(good)
        for bad in ("schema", "", None, 3):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                StructuredOutputConfig(bad)

    def test_nested_values_must_have_the_right_type(self):
        for field in ("limits", "structured_output", "parameters"):
            with self.subTest(field), self.assertRaises(AIGatewayError):
                make_profile(**{field: {"max_input_tokens": 1}})

    def test_validation_messages_never_echo_the_submitted_value(self):
        for field, value in (("profile_id", "SECRET-VALUE-1"), ("provider_id", "SECRET-VALUE-2"), ("model_id", "SECRET VALUE 3"), ("policy_mode", "SECRET-VALUE-4"), ("timeout_seconds", "SECRET-VALUE-5")):
            with self.subTest(field), self.assertRaises(AIGatewayError) as ctx:
                make_profile(**{field: value})
            self.assertNotIn("SECRET", str(ctx.exception))


class DescriptorAndCapabilityTests(unittest.TestCase):
    def caps(self, **overrides):
        values = dict(supported_models=frozenset({"m"}), structured_output_modes=frozenset(StructuredOutputMode), max_input_tokens=10, max_output_tokens=10)
        values.update(overrides)
        return ProviderCapabilities(**values)

    def test_valid_capabilities_are_normalised_to_frozensets(self):
        caps = self.caps(supported_models=["m", "n"], structured_output_modes=["native_schema"], generation_parameters=["seed"])
        self.assertEqual(caps.supported_models, {"m", "n"})
        self.assertEqual(caps.structured_output_modes, {StructuredOutputMode.NATIVE_SCHEMA})
        self.assertIsInstance(caps.generation_parameters, frozenset)
        self.assertFalse(caps.test_only)

    def test_invalid_capabilities_are_refused(self):
        for overrides in (
            {"supported_models": frozenset()},
            {"supported_models": "m"},
            {"supported_models": [1]},
            {"supported_models": ["bad model"]},
            {"structured_output_modes": frozenset()},
            {"structured_output_modes": ["nope"]},
            {"max_input_tokens": 0},
            {"max_output_tokens": True},
            {"generation_parameters": ["unknown"]},
            {"generation_parameters": "seed"},
            {"test_only": "yes"},
            {"supported_models": {f"m{i}" for i in range(101)}},
        ):
            with self.subTest(overrides), self.assertRaises(AIGatewayError) as ctx:
                self.caps(**overrides)
            self.assertEqual(ctx.exception.code, "configuration_error")

    def test_a_descriptor_declares_locality_and_validates_its_parts(self):
        descriptor = ProviderDescriptor("p", "kind_a", "company_endpoint", self.caps())
        self.assertIs(descriptor.locality, DataLocality.COMPANY_ENDPOINT)
        for kwargs in ({"provider_id": "Bad Id"}, {"kind": "bad kind"}, {"locality": "everywhere"}, {"locality": None}, {"capabilities": {}}):
            values = dict(provider_id="p", kind="kind_a", locality="local", capabilities=self.caps())
            values.update(kwargs)
            with self.subTest(kwargs), self.assertRaises(AIGatewayError):
                ProviderDescriptor(**values)

    def test_a_descriptor_is_immutable(self):
        descriptor = ProviderDescriptor("p", "kind_a", "local", self.caps())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            descriptor.locality = DataLocality.CLOUD


class GenerationRequestTests(unittest.TestCase):
    def test_a_valid_request_freezes_its_schema(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        request = make_request(output_schema=schema)
        schema["properties"]["a"]["type"] = "integer"  # a later change by the caller must not reach the request
        self.assertEqual(request.output_schema["properties"]["a"]["type"], "string")
        with self.assertRaises(TypeError):
            request.output_schema["type"] = "array"
        self.assertIsInstance(request.output_schema["required"], tuple)

    def test_a_request_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            make_request().input_text = "x"

    def test_invalid_requests_are_refused_with_invalid_request(self):
        cases = {
            "empty instructions": {"instructions": "  "},
            "non-string instructions": {"instructions": 5},
            "empty input": {"input_text": ""},
            "non-string input": {"input_text": None},
            "oversized instructions": {"instructions": "x" * 50_001},
            "oversized input": {"input_text": "x" * 4_000_001},
            "bad profile id": {"profile_id": "Bad Id"},
            "schema not a mapping": {"output_schema": "object"},
            "schema root not object": {"output_schema": {"type": "array", "items": {"type": "string"}}},
            "non-json schema": {"output_schema": {"type": "object", "properties": {"a": {"type": object}}}},
            "zero output limit": {"max_output_tokens": 0},
            "bool output limit": {"max_output_tokens": True},
            "negative timeout": {"timeout_seconds": -1},
            "nan timeout": {"timeout_seconds": float("nan")},
            "bool timeout": {"timeout_seconds": True},
        }
        for label, overrides in cases.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                make_request(**overrides)
            self.assertEqual(ctx.exception.code, "invalid_request", label)

    def test_the_repr_of_a_request_does_not_contain_content(self):
        request = make_request(instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL)
        for text in (repr(request), str(request)):
            self.assertNotIn(PROMPT_SENTINEL, text)
            self.assertNotIn(INPUT_SENTINEL, text)
            self.assertNotIn("properties", text)

    def test_the_repr_of_a_response_does_not_contain_the_output(self):
        provenance = Provenance("p", "q", "k", DataLocality.LOCAL, PolicyMode.LOCAL_ONLY, "m", "m", True, 0.1, None, None)
        response = GenerationResponse({"secret": INPUT_SENTINEL}, provenance)
        self.assertNotIn(INPUT_SENTINEL, repr(response))
        with self.assertRaises(TypeError):
            response.output["secret"] = "x"


class ErrorTableTests(unittest.TestCase):
    def test_the_required_error_codes_exist(self):
        required = {"provider_unavailable", "policy_violation", "timeout", "invalid_structured_output", "rate_limit", "authentication_failure", "unsupported_model", "configuration_error"}
        self.assertLessEqual(required, ERROR_CODES)

    def test_every_code_has_a_fixed_message_and_a_retry_class(self):
        self.assertEqual(set(MESSAGES), set(ERROR_CODES))
        self.assertEqual(RETRYABLE_CODES, {"provider_unavailable", "timeout", "rate_limit"})
        for code in ERROR_CODES:
            error = AIGatewayError(code)
            self.assertEqual(str(error), MESSAGES[code])
            self.assertEqual(error.retryable, code in RETRYABLE_CODES)

    def test_non_retryable_conditions_are_never_retryable(self):
        for code in ("policy_violation", "authentication_failure", "configuration_error", "unsupported_model", "invalid_structured_output", "invalid_request", "capability_missing"):
            self.assertFalse(AIGatewayError(code).retryable, code)

    def test_an_unknown_code_cannot_be_created(self):
        with self.assertRaises(ValueError):
            AIGatewayError("made_up")

    def test_an_adapter_cannot_claim_a_policy_or_configuration_verdict(self):
        self.assertFalse({"policy_violation", "configuration_error", "invalid_request", "capability_missing"} & ADAPTER_CODES)
