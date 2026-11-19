"""Helpers for the KPMG Workbench adapter tests: a recording fake transport and builders. DB-free; no network, no real credential.

The URL and header below are written out literally, independently of the implementation, from the client-side contract reported from the FI
integration audit (GP-0299-FI-APP). That the live Workbench accepts them is not shown by any test.
"""

import json

from apps.ai_gateway import AIGateway, ProviderRegistry
from apps.ai_gateway.adapters import workbench
from apps.ai_gateway.transport import HttpResponse, HttpTransport, TransportError
from config.ai_workbench import PROFILE_GPT5, PROFILE_GPT41, build_workbench_profiles
from tests.ai_gateway_support import GOOD_OUTPUT, SECRET_SENTINEL, FakeClock, make_request

VERIFIED_AUTH_HEADER = "Ocp-Apim-Subscription-Key"
GPT5 = workbench.DEPLOYMENT_GPT5
GPT41 = workbench.DEPLOYMENT_GPT41


def verified_url(deployment, api_version="2024-10-21"):
    return f"https://api.workbench.kpmg/genai/azure/openai/deployments/{deployment}/chat/completions?api-version={api_version}"


def answer(content=GOOD_OUTPUT, model="gpt-5-2025-08-07", finish="stop", usage=(11, 7), **extra):
    document = {"id": "chatcmpl-test", "object": "chat.completion", "model": model, "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        document["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    document.update(extra)
    return HttpResponse(200, json.dumps(document).encode("utf-8"))


def status(code, body=b"{}"):
    return HttpResponse(code, body)


class FakeTransport(HttpTransport):
    """Plays a script: each item is an ``HttpResponse`` (returned) or an exception (raised). Records every request."""

    def __init__(self, *script, clock=None, cost=0.0):
        self.script = list(script)
        self.requests = []
        self.clock = clock
        self.cost = cost

    def send(self, request):
        self.requests.append(request)
        if self.clock is not None:
            self.clock.advance(self.cost)
        if not self.script:
            raise AssertionError("the fake transport was called more often than scripted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last(self):
        return self.requests[-1]

    def body(self, index=-1):
        return json.loads(self.requests[index].body.decode("utf-8"))

    def headers(self, index=-1):
        return dict(self.requests[index].headers)


def make_config(**overrides):
    values = dict(api_key=SECRET_SENTINEL)
    values.update(overrides)
    return workbench.WorkbenchConfig(**values)


class Rig:
    """A Workbench provider on a fake transport and a fake clock, with a gateway over the two real profiles."""

    def __init__(self, *script, profiles=None, timeout=90.0, cost=0.0, **config_overrides):
        self.clock = FakeClock()
        self.sleeps = []
        self.transport = FakeTransport(*script, clock=self.clock, cost=cost)
        self.provider = workbench.WorkbenchProvider(make_config(**config_overrides), self.transport, clock=self.clock, sleep=self._sleep)
        self.gateway = AIGateway(ProviderRegistry.build([self.provider]), profiles if profiles is not None else build_workbench_profiles(timeout), clock=self.clock)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def generate(self, policy, profile_id=PROFILE_GPT5, **request_overrides):
        return self.gateway.generate(make_request(profile_id=profile_id, **request_overrides), policy=policy)


__all__ = ["GPT5", "GPT41", "PROFILE_GPT5", "PROFILE_GPT41", "VERIFIED_AUTH_HEADER", "FakeTransport", "Rig", "TransportError", "answer", "make_config", "status", "verified_url"]
