"""The deterministic TEST provider. NOT an AI model and NOT a document-understanding implementation.

It exists so the gateway, and later the workflows that call it, can be tested end to end, failure paths included, with repeatable input and
output, no network and no credentials. It is declared ``test_only``: the gateway refuses to be built with it unless the caller says
``allow_test_providers=True``, and the production composition (``config/ai_gateway.py``) registers no provider at all, so it can never be
selected automatically. Its provenance says ``test_only=True`` and its kind is ``deterministic_test_fixture``: nothing may present its
output as produced by a real model.

SUCCESS output is, in order of preference, (1) a FIXTURE registered for the SHA-256 of the exact input text, or (2) a value generated from
the request's schema: the first enum value, the lowest allowed number (0 when unconstrained), ``False``, ``None`` for a null type, an empty
array (or ``minItems`` elements), a string ``"<property name>-<first eight hex digits of the input's SHA-256>"`` padded or cut to the
length limits, and objects with every declared property. It reads nothing but the call.

SCENARIOS (one per provider instance; the provider holds no other state, apart from ``call_count``):

* ``SUCCESS``: valid JSON text as above.
* ``MALFORMED_OUTPUT``: text that is not JSON. ``NOT_AN_OBJECT``: valid JSON that is an array. ``SCHEMA_MISMATCH``: an object whose every
  declared property holds an object (guaranteed invalid when the schema declares a property whose type is not object).
* ``TIMEOUT``, ``UNAVAILABLE``, ``RATE_LIMITED``, ``AUTHENTICATION_FAILURE``, ``UNSUPPORTED_MODEL``: the matching adapter error.
* ``UNEXPECTED_ERROR``: a foreign ``RuntimeError`` whose text contains the input, to prove the gateway never passes provider text on.
"""

import hashlib
import math
from collections.abc import Mapping
from enum import StrEnum

from ..contracts import (
    ModelProfile,
    ProviderCapabilities,
    ProviderDescriptor,
    StructuredOutputConfig,
    StructuredOutputMode,
    TokenLimits,
)
from ..errors import AIGatewayError
from ..jsonvalue import canonical_json, estimate_tokens, freeze_json
from ..policy import DataLocality, PolicyMode
from ..provider import AIProvider, ProviderCall, ProviderResult

PROVIDER_ID = "deterministic-test"
PROVIDER_KIND = "deterministic_test_fixture"
MODEL_ID = "deterministic-fixture-v1"


class Scenario(StrEnum):
    SUCCESS = "success"
    MALFORMED_OUTPUT = "malformed_output"
    NOT_AN_OBJECT = "not_an_object"
    SCHEMA_MISMATCH = "schema_mismatch"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_FAILURE = "authentication_failure"
    UNSUPPORTED_MODEL = "unsupported_model"
    UNEXPECTED_ERROR = "unexpected_error"


_RAISES = {
    Scenario.TIMEOUT: "timeout",
    Scenario.UNAVAILABLE: "provider_unavailable",
    Scenario.RATE_LIMITED: "rate_limit",
    Scenario.AUTHENTICATION_FAILURE: "authentication_failure",
    Scenario.UNSUPPORTED_MODEL: "unsupported_model",
}


def fixture_key(input_text: str) -> str:
    """The key a fixture is registered under: the SHA-256 (hex) of the exact input text."""
    return hashlib.sha256(input_text.encode("utf-8")).hexdigest()


def _generate(schema, name: str, digest: str):
    types = (schema["type"],) if isinstance(schema["type"], str) else tuple(schema["type"])
    if "enum" in schema:
        return schema["enum"][0]
    kind = next((t for t in types if t != "null"), "null")
    if kind == "object":
        return {key: _generate(sub, key, digest) for key, sub in sorted(schema.get("properties", {}).items())}
    if kind == "array":
        count = schema.get("minItems", 0)
        return [_generate(schema["items"], name, digest) for _ in range(count)] if "items" in schema else []
    if kind == "string":
        text = f"{name}-{digest}"
        text = text.ljust(schema.get("minLength", 0), "x")
        return text[: schema["maxLength"]] if "maxLength" in schema else text
    if kind in ("integer", "number"):
        value = 0
        if "minimum" in schema and value < schema["minimum"]:
            value = math.ceil(schema["minimum"])
        if "maximum" in schema and value > schema["maximum"]:
            value = math.floor(schema["maximum"])
        return value if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    return None


class DeterministicTestProvider(AIProvider):
    def __init__(self, scenario: Scenario = Scenario.SUCCESS, *, fixtures: Mapping | None = None, provider_id: str = PROVIDER_ID):
        self._scenario = Scenario(scenario)
        self._fixtures = {key: freeze_json(value, code="configuration_error", reason="invalid_fixture") for key, value in (fixtures or {}).items()}
        self._descriptor = ProviderDescriptor(
            provider_id=provider_id,
            kind=PROVIDER_KIND,
            locality=DataLocality.LOCAL,  # it runs inside the process and makes no network call
            capabilities=ProviderCapabilities(
                supported_models=frozenset({MODEL_ID}),
                structured_output_modes=frozenset(StructuredOutputMode),
                max_input_tokens=1_000_000,
                max_output_tokens=100_000,
                generation_parameters=frozenset({"seed"}),
                test_only=True,
            ),
        )
        self.call_count = 0

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def generate(self, call: ProviderCall) -> ProviderResult:
        self.call_count += 1
        if call.model_id != MODEL_ID:
            raise AIGatewayError("unsupported_model")
        scenario = self._scenario
        if scenario in _RAISES:
            raise AIGatewayError(_RAISES[scenario])
        if scenario is Scenario.UNEXPECTED_ERROR:
            raise RuntimeError(f"unexpected failure while processing: {call.input_text}")
        digest = fixture_key(call.input_text)
        if scenario is Scenario.MALFORMED_OUTPUT:
            raw = '{"result": '
        elif scenario is Scenario.NOT_AN_OBJECT:
            raw = "[]"
        else:
            value = self._fixtures.get(digest)
            if value is None:
                value = _generate(call.output_schema, "value", digest[:8])
            if scenario is Scenario.SCHEMA_MISMATCH:
                value = {key: {"mismatch": True} for key in call.output_schema.get("properties", {})}
            raw = canonical_json(value)
        return ProviderResult(
            raw_output=raw,
            reported_model_id=MODEL_ID,
            input_tokens=estimate_tokens(call.instructions) + estimate_tokens(call.input_text),
            output_tokens=estimate_tokens(raw),
        )


def deterministic_test_profile(
    profile_id: str = "deterministic-test",
    *,
    provider_id: str = PROVIDER_ID,
    policy_mode: PolicyMode = PolicyMode.LOCAL_ONLY,
    timeout_seconds: float = 5.0,
) -> ModelProfile:
    """A profile for the test provider, for tests and examples. Local-only by default: the test provider never leaves the process."""
    return ModelProfile(
        profile_id=profile_id,
        provider_id=provider_id,
        model_id=MODEL_ID,
        policy_mode=policy_mode,
        timeout_seconds=timeout_seconds,
        limits=TokenLimits(max_input_tokens=100_000, max_output_tokens=10_000),
        structured_output=StructuredOutputConfig(StructuredOutputMode.NATIVE_SCHEMA),
    )
