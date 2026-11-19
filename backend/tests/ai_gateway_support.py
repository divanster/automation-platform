"""Helpers for the AI Gateway tests: a scriptable provider, a fake clock and small builders. DB-free; no network."""

from apps.ai_gateway import (
    AIGateway,
    AIProvider,
    DataLocality,
    GenerationParameters,
    GenerationRequest,
    ModelProfile,
    PolicyContext,
    PolicyMode,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRegistry,
    ProviderResult,
    StructuredOutputConfig,
    StructuredOutputMode,
    TokenLimits,
)

# Sentinels that must never appear in an error, a log line or a repr.
PROMPT_SENTINEL = "PROMPT-SENTINEL-4f9a1c"
INPUT_SENTINEL = "INPUT-SENTINEL-7b3e5d"
SECRET_SENTINEL = "sk-test-SECRET-SENTINEL-91ab"

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "count": {"type": "integer", "minimum": 0}},
    "required": ["name"],
    "additionalProperties": False,
}
GOOD_OUTPUT = '{"name": "x", "count": 2}'

ALL_MODES = frozenset(StructuredOutputMode)


class FakeClock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedProvider(AIProvider):
    """A provider whose behaviour is a function of the call. Records every call it receives."""

    def __init__(
        self,
        provider_id="scripted",
        locality=DataLocality.LOCAL,
        *,
        kind="scripted_test",
        test_only=False,
        models=("model-a",),
        modes=ALL_MODES,
        max_input_tokens=1_000_000,
        max_output_tokens=100_000,
        parameters=frozenset({"temperature", "top_p", "seed"}),
        behaviour=None,
    ):
        self._descriptor = ProviderDescriptor(
            provider_id=provider_id,
            kind=kind,
            locality=locality,
            capabilities=ProviderCapabilities(
                supported_models=frozenset(models),
                structured_output_modes=frozenset(modes),
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                generation_parameters=frozenset(parameters),
                test_only=test_only,
            ),
        )
        self.behaviour = behaviour or (lambda call: ProviderResult(GOOD_OUTPUT, "model-a", 10, 5))
        self.calls = []

    @property
    def descriptor(self):
        return self._descriptor

    def generate(self, call):
        self.calls.append(call)
        return self.behaviour(call)


def make_profile(**overrides):
    values = dict(
        profile_id="profile-a",
        provider_id="scripted",
        model_id="model-a",
        policy_mode=PolicyMode.LOCAL_ONLY,
        timeout_seconds=10.0,
        limits=TokenLimits(max_input_tokens=10_000, max_output_tokens=1_000),
        structured_output=StructuredOutputConfig(StructuredOutputMode.NATIVE_SCHEMA),
        parameters=GenerationParameters(),
    )
    values.update(overrides)
    return ModelProfile(**values)


def make_request(**overrides):
    values = dict(profile_id="profile-a", instructions="Extract the fields.", input_text="Some document text.", output_schema=SCHEMA)
    values.update(overrides)
    return GenerationRequest(**values)


def make_gateway(*providers, profiles=None, clock=None, allow_test_providers=False):
    providers = providers or (ScriptedProvider(),)
    profiles = profiles if profiles is not None else (make_profile(provider_id=providers[0].descriptor.provider_id),)
    kwargs = {"allow_test_providers": allow_test_providers}
    if clock is not None:
        kwargs["clock"] = clock
    return AIGateway(ProviderRegistry.build(providers), profiles, **kwargs)


def local_policy():
    return PolicyContext(PolicyMode.LOCAL_ONLY)
