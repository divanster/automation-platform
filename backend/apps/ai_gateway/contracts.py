"""The provider-neutral contract values of the AI Gateway. Pure Python, standard library only.

Every value here is immutable and validates itself when it is built, so an invalid profile, request or descriptor never exists. Validation
errors name the field and never echo the value.

* ``ModelProfile``: which provider and model, under which data policy, with which timeout, token limits, structured-output
  configuration and optional generation parameters. It holds NO credential and no reference to one (credential resolution is a later
  slice).
* ``ProviderCapabilities`` and ``ProviderDescriptor``: what a provider declares about itself, including WHERE it processes data
  (``locality``).
* ``GenerationRequest``: instructions, input text and an output schema for one structured generation.
* ``GenerationResponse`` and ``Provenance``: the validated output and metadata about how it was produced (never the prompt).

Prompt text, input text and output are excluded from ``repr`` so that logging an object by accident does not log a document.
"""

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import AIGatewayError, configuration_error
from .jsonvalue import freeze_json
from .policy import DataLocality, PolicyMode, coerce_enum
from .structured import check_schema

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")

MAX_TIMEOUT_SECONDS = 300.0
MAX_INPUT_TOKENS_LIMIT = 10_000_000
MAX_OUTPUT_TOKENS_LIMIT = 1_000_000
MAX_INSTRUCTIONS_CHARS = 50_000
MAX_INPUT_CHARS = 4_000_000
MAX_MODELS_PER_PROVIDER = 100

GENERATION_PARAMETER_NAMES = frozenset({"temperature", "top_p", "seed"})


class StructuredOutputMode(StrEnum):
    """How a provider is asked for structure. The gateway validates the output against the schema in EVERY mode."""

    NATIVE_SCHEMA = "native_schema"  # the provider constrains its output to the schema itself
    JSON_MODE = "json_mode"  # the provider is asked for JSON; the schema is described in the prompt
    PROMPT_ONLY = "prompt_only"  # only instructions ask for JSON


def _identifier(value, field_name: str, pattern=_IDENTIFIER) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise configuration_error(f"invalid_{field_name}", f"{field_name} is not a valid identifier")
    return value


def _positive_int(value, field_name: str, upper: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= upper:
        raise configuration_error(f"invalid_{field_name}", f"{field_name} must be an integer from 1 to {upper}")
    return value


def _real(value, field_name: str, low: float, high: float, *, low_open: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise configuration_error(f"invalid_{field_name}", f"{field_name} must be a finite number")
    value = float(value)
    if value < low or value > high or (low_open and value == low):
        raise configuration_error(f"invalid_{field_name}", f"{field_name} is out of range")
    return value


@dataclass(frozen=True)
class TokenLimits:
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self):
        _positive_int(self.max_input_tokens, "max_input_tokens", MAX_INPUT_TOKENS_LIMIT)
        _positive_int(self.max_output_tokens, "max_output_tokens", MAX_OUTPUT_TOKENS_LIMIT)


@dataclass(frozen=True)
class StructuredOutputConfig:
    mode: StructuredOutputMode = StructuredOutputMode.NATIVE_SCHEMA

    def __post_init__(self):
        object.__setattr__(self, "mode", coerce_enum(StructuredOutputMode, self.mode, field_name="structured_output_mode"))


@dataclass(frozen=True)
class GenerationParameters:
    """Optional sampling parameters. ``None`` means "the provider's default"; nothing is invented."""

    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None

    def __post_init__(self):
        if self.temperature is not None:
            object.__setattr__(self, "temperature", _real(self.temperature, "temperature", 0.0, 2.0))
        if self.top_p is not None:
            object.__setattr__(self, "top_p", _real(self.top_p, "top_p", 0.0, 1.0, low_open=True))
        if self.seed is not None and (not isinstance(self.seed, int) or isinstance(self.seed, bool) or not 0 <= self.seed <= 2**31 - 1):
            raise configuration_error("invalid_seed", "seed must be an integer from 0 to 2147483647")

    def names(self) -> frozenset:
        return frozenset(name for name in GENERATION_PARAMETER_NAMES if getattr(self, name) is not None)


@dataclass(frozen=True)
class ModelProfile:
    """One immutable, named way of calling one model of one provider. No credential, no endpoint, no secret."""

    profile_id: str
    provider_id: str
    model_id: str
    policy_mode: PolicyMode
    timeout_seconds: float
    limits: TokenLimits
    structured_output: StructuredOutputConfig = field(default_factory=StructuredOutputConfig)
    parameters: GenerationParameters = field(default_factory=GenerationParameters)

    def __post_init__(self):
        _identifier(self.profile_id, "profile_id")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.model_id, "model_id", MODEL_ID_PATTERN)
        object.__setattr__(self, "policy_mode", coerce_enum(PolicyMode, self.policy_mode, field_name="policy_mode"))
        object.__setattr__(self, "timeout_seconds", _real(self.timeout_seconds, "timeout_seconds", 0.0, MAX_TIMEOUT_SECONDS, low_open=True))
        for name, kind in (("limits", TokenLimits), ("structured_output", StructuredOutputConfig), ("parameters", GenerationParameters)):
            if not isinstance(getattr(self, name), kind):
                raise configuration_error(f"invalid_{name}", f"{name} must be a {kind.__name__}")


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider says it can do. The gateway refuses a profile that needs more (``capability_missing``)."""

    supported_models: frozenset
    structured_output_modes: frozenset
    max_input_tokens: int
    max_output_tokens: int
    generation_parameters: frozenset = frozenset()
    test_only: bool = False  # a fixture provider: never real AI, never allowed in a production composition

    def __post_init__(self):
        models = self._frozenset_of_str(self.supported_models, "supported_models")
        if not models or len(models) > MAX_MODELS_PER_PROVIDER:
            raise configuration_error("invalid_supported_models", "supported_models must list from 1 to 100 models")
        for model in models:
            _identifier(model, "supported_models", MODEL_ID_PATTERN)
        object.__setattr__(self, "supported_models", models)
        modes = frozenset(coerce_enum(StructuredOutputMode, m, field_name="structured_output_modes") for m in self._iterable(self.structured_output_modes, "structured_output_modes"))
        if not modes:
            raise configuration_error("invalid_structured_output_modes", "structured_output_modes must not be empty")
        object.__setattr__(self, "structured_output_modes", modes)
        _positive_int(self.max_input_tokens, "max_input_tokens", MAX_INPUT_TOKENS_LIMIT)
        _positive_int(self.max_output_tokens, "max_output_tokens", MAX_OUTPUT_TOKENS_LIMIT)
        params = self._frozenset_of_str(self.generation_parameters, "generation_parameters")
        if not params <= GENERATION_PARAMETER_NAMES:
            raise configuration_error("invalid_generation_parameters", "generation_parameters names an unknown parameter")
        object.__setattr__(self, "generation_parameters", params)
        if not isinstance(self.test_only, bool):
            raise configuration_error("invalid_test_only", "test_only must be a boolean")

    @staticmethod
    def _iterable(value, field_name):
        if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
            raise configuration_error(f"invalid_{field_name}", f"{field_name} must be a collection")
        return value

    @classmethod
    def _frozenset_of_str(cls, value, field_name):
        items = frozenset(cls._iterable(value, field_name))
        if not all(isinstance(i, str) for i in items):
            raise configuration_error(f"invalid_{field_name}", f"{field_name} must contain only strings")
        return items


@dataclass(frozen=True)
class ProviderDescriptor:
    """A provider's declaration of itself. ``locality`` is where it processes data; policy decisions rest on it."""

    provider_id: str
    kind: str  # a short label of the adapter family, for example "deterministic_test_fixture"; informational, never a path or class name
    locality: DataLocality
    capabilities: ProviderCapabilities

    def __post_init__(self):
        _identifier(self.provider_id, "provider_id")
        _identifier(self.kind, "provider_kind")
        object.__setattr__(self, "locality", coerce_enum(DataLocality, self.locality, field_name="locality"))
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise configuration_error("invalid_capabilities", "capabilities must be a ProviderCapabilities")


def _text(value, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AIGatewayError("invalid_request", f"{field_name} must be a non-empty string", reason=f"invalid_{field_name}")
    if len(value) > limit:
        raise AIGatewayError("invalid_request", f"{field_name} is too long", reason=f"{field_name}_too_long")
    return value


@dataclass(frozen=True)
class GenerationRequest:
    """One structured generation: what to do (``instructions``), the data to do it on (``input_text``), and the shape of the answer.

    ``input_text`` is DATA, not instructions (docs/architecture/ai-policy.md, section 7); the workflow that builds the request delimits it.
    ``max_output_tokens`` and ``timeout_seconds`` may only LOWER the profile's limits.
    """

    profile_id: str
    instructions: str = field(repr=False)
    input_text: str = field(repr=False)
    output_schema: object = field(repr=False)
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or _IDENTIFIER.fullmatch(self.profile_id) is None:
            raise AIGatewayError("invalid_request", "profile_id is not a valid identifier", reason="invalid_profile_id")
        _text(self.instructions, "instructions", MAX_INSTRUCTIONS_CHARS)
        _text(self.input_text, "input_text", MAX_INPUT_CHARS)
        schema = freeze_json(self.output_schema, code="invalid_request", reason="invalid_output_schema")
        check_schema(schema)
        object.__setattr__(self, "output_schema", schema)
        if self.max_output_tokens is not None and (not isinstance(self.max_output_tokens, int) or isinstance(self.max_output_tokens, bool) or self.max_output_tokens < 1):
            raise AIGatewayError("invalid_request", "max_output_tokens must be a positive integer", reason="invalid_max_output_tokens")
        if self.timeout_seconds is not None:
            if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
                raise AIGatewayError("invalid_request", "timeout_seconds must be a positive number", reason="invalid_timeout_seconds")
            object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True)
class Provenance:
    """How a response was produced. Metadata only: no prompt, no input, no output, no credential."""

    profile_id: str
    provider_id: str
    provider_kind: str
    locality: DataLocality
    policy_mode: PolicyMode  # the mode in force for the call
    requested_model_id: str
    reported_model_id: str  # what the provider says it ran; recorded, not trusted
    test_only: bool  # True: a fixture, not a real model
    duration_seconds: float
    input_tokens: int | None  # as reported by the provider; None when it did not report
    output_tokens: int | None


@dataclass(frozen=True)
class GenerationResponse:
    """A successful, VALIDATED structured generation. Valid against the schema does not mean true: deterministic business validation and
    human review still apply (docs/architecture/ai-policy.md, section 6)."""

    output: object = field(repr=False)  # an immutable JSON object (read-only mapping)
    provenance: Provenance

    def __post_init__(self):
        object.__setattr__(self, "output", freeze_json(self.output, code="invalid_structured_output", reason="invalid_json_value"))
