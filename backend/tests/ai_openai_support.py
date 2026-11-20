"""Helpers for the OpenAI-compatible adapter tests: builders and a rig over a recording fake transport. DB-free; no network, no real credential.

The endpoints use the reserved ``.invalid`` top-level domain and loopback literals only; nothing here resolves or connects.
"""

from apps.ai_gateway import AIGateway, DataLocality, GenerationParameters, ModelProfile, PolicyContext, PolicyMode, ProviderCall, ProviderRegistry, StructuredOutputMode
from apps.ai_gateway.adapters import openai_compatible
from config import ai_openai_compatible
from tests.ai_gateway_support import GOOD_OUTPUT, SCHEMA, SECRET_SENTINEL, FakeClock, make_request
from tests.ai_workbench_support import FakeTransport, TransportError, answer, status

LOCAL_ENDPOINT = "http://127.0.0.1:11434/v1"
LOCAL_ORIGINS = ("http://127.0.0.1:11434",)
CORP_ENDPOINT = "https://llm.corp.example.invalid/v1"
CORP_ORIGINS = ("https://llm.corp.example.invalid",)
CLOUD_ENDPOINT = "https://api.vendor.example.invalid/v1"
CLOUD_ORIGINS = ("https://api.vendor.example.invalid",)
MODEL = "local-model:8b"

COMPANY_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

DEFAULTS = {
    DataLocality.LOCAL: dict(endpoint=LOCAL_ENDPOINT, allowed_origins=LOCAL_ORIGINS, api_key=None),
    DataLocality.COMPANY_ENDPOINT: dict(endpoint=CORP_ENDPOINT, allowed_origins=CORP_ORIGINS, api_key=SECRET_SENTINEL, approved_networks=COMPANY_NETWORKS),
    DataLocality.CLOUD: dict(endpoint=CLOUD_ENDPOINT, allowed_origins=CLOUD_ORIGINS, api_key=SECRET_SENTINEL),
}

POLICIES = {
    "local_only": PolicyContext(PolicyMode.LOCAL_ONLY),
    "company_endpoint": PolicyContext(PolicyMode.COMPANY_ENDPOINT),
    "approved_cloud": PolicyContext(PolicyMode.APPROVED_CLOUD, frozenset({openai_compatible.PROVIDER_ID})),
}


def make_config(locality=DataLocality.COMPANY_ENDPOINT, **overrides):
    values = dict(DEFAULTS[locality], locality=locality, model_id=MODEL)
    values.update(overrides)
    return openai_compatible.OpenAICompatibleConfig(**values)


def make_profile(config, timeout=90.0, **parameters):
    settings = ai_openai_compatible.OpenAICompatibleSettings(config, timeout)
    profile = ai_openai_compatible.build_openai_compatible_profile(settings)
    if parameters:
        profile = ModelProfile(
            profile_id=profile.profile_id, provider_id=profile.provider_id, model_id=profile.model_id, policy_mode=profile.policy_mode,
            timeout_seconds=profile.timeout_seconds, limits=profile.limits, structured_output=profile.structured_output, parameters=GenerationParameters(**parameters),
        )  # fmt: skip
    return profile


def make_call(model_id=MODEL, timeout=60.0, **overrides):
    values = dict(
        model_id=model_id, instructions="Extract.", input_text="Some text.", output_schema=SCHEMA, structured_output_mode=StructuredOutputMode.JSON_MODE,
        timeout_seconds=timeout, max_output_tokens=500, parameters=GenerationParameters(),
    )  # fmt: skip
    values.update(overrides)
    return ProviderCall(**values)


class Rig:
    """A provider on a fake transport and a fake clock, with a gateway over its one profile."""

    def __init__(self, *script, locality=DataLocality.COMPANY_ENDPOINT, timeout=90.0, cost=0.0, parameters=None, **config_overrides):
        self.clock = FakeClock()
        self.sleeps = []
        self.transport = FakeTransport(*script, clock=self.clock, cost=cost)
        self.config = make_config(locality, **config_overrides)
        self.provider = openai_compatible.OpenAICompatibleProvider(self.config, self.transport, clock=self.clock, sleep=self._sleep)
        self.profile = make_profile(self.config, timeout, **(parameters or {}))
        self.gateway = AIGateway(ProviderRegistry.build([self.provider]), (self.profile,), clock=self.clock)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def generate(self, policy=None, **request_overrides):
        policy = policy or POLICIES["approved_cloud"]
        return self.gateway.generate(make_request(profile_id=ai_openai_compatible.PROFILE_ID, **request_overrides), policy=policy)


__all__ = [
    "CLOUD_ENDPOINT", "CLOUD_ORIGINS", "COMPANY_NETWORKS", "CORP_ENDPOINT", "CORP_ORIGINS", "GOOD_OUTPUT", "LOCAL_ENDPOINT", "LOCAL_ORIGINS", "MODEL", "POLICIES", "SECRET_SENTINEL",
    "FakeTransport", "Rig", "TransportError", "answer", "make_call", "make_config", "make_profile", "status",
]  # fmt: skip
