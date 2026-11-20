"""Helpers for the native Ollama adapter tests: builders and a rig over a recording fake transport. DB-free; no network, no Ollama, no real credential.

Endpoints are loopback literals and ``localhost`` only; nothing here resolves or connects.
"""

import json

from apps.ai_gateway import AIGateway, GenerationParameters, ModelProfile, PolicyContext, PolicyMode, ProviderCall, ProviderRegistry, StructuredOutputMode
from apps.ai_gateway.adapters import ollama
from apps.ai_gateway.transport import HttpResponse
from config import ai_ollama
from tests.ai_gateway_support import GOOD_OUTPUT, SCHEMA, SECRET_SENTINEL, FakeClock, make_request
from tests.ai_workbench_support import FakeTransport, TransportError, status

ENDPOINT = "http://127.0.0.1:11434"
ORIGINS = ("http://127.0.0.1:11434",)
MODEL = "llama3.1:8b"

POLICIES = {
    "local_only": PolicyContext(PolicyMode.LOCAL_ONLY),
    "company_endpoint": PolicyContext(PolicyMode.COMPANY_ENDPOINT),
    "approved_cloud": PolicyContext(PolicyMode.APPROVED_CLOUD),
}


def answer(content=GOOD_OUTPUT, model=MODEL, done=True, done_reason="stop", usage=(11, 7), **extra):
    """An Ollama ``/api/chat`` answer with ``stream`` false."""
    document = {"model": model, "created_at": "2025-01-01T00:00:00Z", "message": {"role": "assistant", "content": content}, "done": done}
    if done_reason is not None:
        document["done_reason"] = done_reason
    if usage is not None:
        document["prompt_eval_count"], document["eval_count"] = usage
    document.update(extra)
    return HttpResponse(200, json.dumps(document).encode("utf-8"))


def make_config(**overrides):
    values = dict(endpoint=ENDPOINT, allowed_origins=ORIGINS, model_id=MODEL)
    values.update(overrides)
    return ollama.OllamaConfig(**values)


def make_profile(config, timeout=90.0, mode=StructuredOutputMode.JSON_MODE, **parameters):
    settings = ai_ollama.OllamaSettings(config, timeout, mode)
    profile = ai_ollama.build_ollama_profile(settings)
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

    def __init__(self, *script, timeout=90.0, cost=0.0, mode=StructuredOutputMode.JSON_MODE, parameters=None, **config_overrides):
        self.clock = FakeClock()
        self.sleeps = []
        self.transport = FakeTransport(*script, clock=self.clock, cost=cost)
        self.config = make_config(**config_overrides)
        self.provider = ollama.OllamaProvider(self.config, self.transport, clock=self.clock, sleep=self._sleep)
        self.profile = make_profile(self.config, timeout, mode, **(parameters or {}))
        self.gateway = AIGateway(ProviderRegistry.build([self.provider]), (self.profile,), clock=self.clock)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def generate(self, policy=None, **request_overrides):
        policy = policy or POLICIES["local_only"]
        return self.gateway.generate(make_request(profile_id=ai_ollama.PROFILE_ID, **request_overrides), policy=policy)


__all__ = [
    "ENDPOINT", "GOOD_OUTPUT", "MODEL", "ORIGINS", "POLICIES", "SCHEMA", "SECRET_SENTINEL", "FakeTransport", "Rig", "TransportError", "answer", "make_call", "make_config",
    "make_profile", "status",
]  # fmt: skip
