"""Errors of the AI Gateway. Pure Python, standard library only.

Every error carries a STABLE ``code`` (listed in ``ERROR_CODES``). The message of an error raised for a CALL is taken from a fixed table
(``MESSAGES``), never from a provider, a prompt, a model output or any submitted value, so an error can be logged or shown without
leaking a secret or document text. An error raised while CONFIGURING (a profile, a registry, a gateway) may carry a ``detail`` that names
the field or rule that was broken; it never echoes the value. Callers and tests decide from ``code`` (and, for configuration problems,
``reason``), never from the text.

``retryable`` classifies an error for a future worker: only conditions that can pass by themselves (an unavailable provider, a timeout, a
rate limit) are retryable. A policy violation, an authentication failure, a configuration error or an unsupported model never is.
"""

ERROR_CODES = frozenset(
    {
        "provider_unavailable",
        "policy_violation",
        "timeout",
        "invalid_structured_output",
        "rate_limit",
        "authentication_failure",
        "unsupported_model",
        "configuration_error",
        "invalid_request",
        "capability_missing",
        "provider_error",
    }
)

# Codes whose cause can disappear without anyone changing anything.
RETRYABLE_CODES = frozenset({"provider_unavailable", "timeout", "rate_limit"})

# What an ADAPTER may raise. Anything else an adapter raises (including a code outside this set, or a foreign exception) is reported as
# ``provider_error``: an adapter cannot claim a policy or configuration verdict.
ADAPTER_CODES = frozenset(
    {"provider_unavailable", "timeout", "rate_limit", "authentication_failure", "unsupported_model", "invalid_structured_output", "provider_error"}
)

MESSAGES = {
    "provider_unavailable": "The AI provider is unavailable.",
    "policy_violation": "The data policy does not permit this call.",
    "timeout": "The AI provider did not answer within the time limit.",
    "invalid_structured_output": "The AI provider returned output that does not satisfy the requested structure.",
    "rate_limit": "The AI provider refused the call because of a rate limit.",
    "authentication_failure": "The AI provider rejected the credentials.",
    "unsupported_model": "The model is not supported by the provider.",
    "configuration_error": "The AI gateway configuration is missing or invalid.",
    "invalid_request": "The generation request is invalid.",
    "capability_missing": "The provider does not support what the profile requires.",
    "provider_error": "The AI provider failed.",
}

assert set(MESSAGES) == ERROR_CODES and RETRYABLE_CODES <= ERROR_CODES and ADAPTER_CODES <= ERROR_CODES


class AIGatewayError(Exception):
    """The one error type of the gateway. ``code`` is stable; ``reason`` is an optional finer token; the message never holds content."""

    def __init__(self, code: str, detail: str | None = None, *, reason: str | None = None, provider_id: str | None = None, profile_id: str | None = None, violations: tuple = ()):
        if code not in ERROR_CODES:
            raise ValueError("unknown AI gateway error code")
        self.code = code
        self.reason = reason
        self.provider_id = provider_id
        self.profile_id = profile_id
        self.violations = tuple(violations)  # "path: rule" strings built from schema names and indexes only, never from values
        super().__init__(detail if detail is not None else MESSAGES[code])

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def configuration_error(reason: str, detail: str) -> AIGatewayError:
    return AIGatewayError("configuration_error", detail, reason=reason)
