"""The AI Gateway: the one place where a structured generation is checked, called and validated. Pure Python, standard library only.

``AIGateway(registry, profiles)`` is built once from a provider registry and a set of model profiles and is immutable afterwards.
``generate(request, policy=...)`` then runs, in this order and failing closed at the first problem:

1. the request is a ``GenerationRequest`` and its profile is known;
2. DATA POLICY (``policy.enforce_policy``): the provider's declared locality must be allowed by the profile's mode and by the mode in
   force for THIS call. This happens BEFORE any provider code runs. There is no fallback to another provider;
3. limits: a per-call timeout or output limit may only lower the profile's, and an input over the profile's token limit is refused, not
   truncated;
4. the provider is called with a ``ProviderCall`` that holds no policy or company information;
5. the answer is checked: a failure or a foreign exception becomes a stable error with a FIXED message (never the provider's text); an
   answer that arrived after the deadline is refused; the raw text must be JSON, within the output limit, a JSON object, and valid
   against the request's schema. Nothing is repaired, filled in or cut to fit.

CONFIGURATION is checked at construction, so a mistake surfaces at start-up and not on the first call: every profile names a registered
provider that supports its model, structured-output mode, limits and parameters, and whose locality its own policy mode allows; a test-only
provider is refused unless ``allow_test_providers=True`` (the production composition never passes it).

Nothing here logs prompts, input, output, credentials or provider messages: log lines hold identifiers, a code and a duration only.
"""

import json
import logging
import time
from collections.abc import Iterable, Mapping

from .contracts import MODEL_ID_PATTERN, GenerationRequest, GenerationResponse, ModelProfile, Provenance
from .errors import ADAPTER_CODES, AIGatewayError, configuration_error
from .jsonvalue import canonical_json, estimate_tokens, freeze_json
from .policy import PolicyContext, enforce_policy, locality_allowed
from .provider import ProviderCall, ProviderResult
from .registry import ProviderRegistry
from .structured import validate_output

logger = logging.getLogger("apps.ai_gateway")
logger.addHandler(logging.NullHandler())  # a library logger: silent unless the application configures logging


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _parse_json(raw: str):
    """``(True, value)`` for strict JSON text (no NaN or Infinity, no duplicate keys), else ``(False, None)``. Never raises."""
    try:
        return True, json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return False, None


def _freeze_output(parsed):
    """``(True, immutable value)``, or ``(False, None)`` when the parsed output is too large or too deeply nested."""
    try:
        return True, freeze_json(parsed, code="invalid_structured_output", reason="invalid_json_value")
    except AIGatewayError:
        return False, None


class AIGateway:
    __slots__ = ("_registry", "_profiles", "_clock")

    def __init__(self, registry: ProviderRegistry, profiles: Iterable[ModelProfile], *, allow_test_providers: bool = False, clock=time.monotonic):
        if not isinstance(registry, ProviderRegistry):
            raise configuration_error("invalid_registry", "registry must be a ProviderRegistry")
        if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Iterable):
            raise configuration_error("invalid_profile_collection", "profiles must be an iterable of ModelProfile objects")
        if not isinstance(allow_test_providers, bool):
            raise configuration_error("invalid_allow_test_providers", "allow_test_providers must be a boolean")
        for provider_id in registry:
            if registry.descriptor(provider_id).capabilities.test_only and not allow_test_providers:
                raise configuration_error("test_provider_not_allowed", f"provider '{provider_id}' is test-only and this gateway does not allow test providers")

        by_id = {}
        for profile in list(profiles):
            if not isinstance(profile, ModelProfile):
                raise configuration_error("invalid_profile_object", "every profile must be a ModelProfile object")
            if profile.profile_id in by_id:
                raise configuration_error("duplicate_profile_id", f"profile id '{profile.profile_id}' is configured twice")
            self._check_profile(registry, profile)
            by_id[profile.profile_id] = profile
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_profiles", tuple(sorted(by_id.items())))
        object.__setattr__(self, "_clock", clock)

    def __setattr__(self, name, value):
        raise AttributeError("an AIGateway is immutable")

    def __delattr__(self, name):
        raise AttributeError("an AIGateway is immutable")

    @staticmethod
    def _check_profile(registry: ProviderRegistry, profile: ModelProfile) -> None:
        """Fail closed at construction when a profile cannot work with its provider."""
        if profile.provider_id not in registry:
            raise configuration_error("unknown_provider", f"profile '{profile.profile_id}' names a provider that is not registered")
        descriptor = registry.descriptor(profile.provider_id)
        caps = descriptor.capabilities
        if profile.model_id not in caps.supported_models:
            raise AIGatewayError("unsupported_model", reason="profile_model_unsupported", provider_id=profile.provider_id, profile_id=profile.profile_id)
        if not locality_allowed(profile.policy_mode, descriptor.locality):
            raise configuration_error("profile_locality_mismatch", f"profile '{profile.profile_id}' has a policy mode that does not allow its provider's locality")
        missing = (
            profile.structured_output.mode not in caps.structured_output_modes
            or profile.limits.max_input_tokens > caps.max_input_tokens
            or profile.limits.max_output_tokens > caps.max_output_tokens
            or not profile.parameters.names() <= caps.generation_parameters
        )
        if missing:
            raise AIGatewayError("capability_missing", reason="profile_exceeds_capabilities", provider_id=profile.provider_id, profile_id=profile.profile_id)

    @property
    def profile_ids(self) -> tuple:
        return tuple(profile_id for profile_id, _ in self._profiles)

    def profile(self, profile_id: str) -> ModelProfile:
        for known, profile in self._profiles:
            if known == profile_id:
                return profile
        raise AIGatewayError("configuration_error", reason="unknown_profile")

    # the call ----------------------------------------------------------------------------------------------------------

    def generate(self, request: GenerationRequest, *, policy: PolicyContext) -> GenerationResponse:
        if not isinstance(request, GenerationRequest):
            raise AIGatewayError("invalid_request", reason="not_a_generation_request")
        profile = self.profile(request.profile_id)
        descriptor = self._registry.descriptor(profile.provider_id)
        provider_id = descriptor.provider_id

        def fail(code, reason=None, violations=()):
            logger.warning("ai_gateway call failed profile=%s provider=%s code=%s reason=%s", profile.profile_id, provider_id, code, reason)
            return AIGatewayError(code, reason=reason, provider_id=provider_id, profile_id=profile.profile_id, violations=violations)

        try:
            enforce_policy(policy, profile_mode=profile.policy_mode, provider_id=provider_id, locality=descriptor.locality, profile_id=profile.profile_id)
        except AIGatewayError as error:
            code, reason = error.code, error.reason
        else:
            code = reason = None
        if code is not None:
            raise fail(code, reason)

        timeout = profile.timeout_seconds if request.timeout_seconds is None else request.timeout_seconds
        max_output = profile.limits.max_output_tokens if request.max_output_tokens is None else request.max_output_tokens
        if timeout > profile.timeout_seconds:
            raise fail("invalid_request", "timeout_above_profile")
        if max_output > profile.limits.max_output_tokens:
            raise fail("invalid_request", "output_limit_above_profile")
        input_estimate = estimate_tokens(request.instructions) + estimate_tokens(request.input_text) + estimate_tokens(canonical_json(request.output_schema))
        if input_estimate > profile.limits.max_input_tokens:
            raise fail("invalid_request", "input_too_large")

        call = ProviderCall(
            model_id=profile.model_id,
            instructions=request.instructions,
            input_text=request.input_text,
            output_schema=request.output_schema,
            structured_output_mode=profile.structured_output.mode,
            timeout_seconds=timeout,
            max_output_tokens=max_output,
            parameters=profile.parameters,
        )
        result = None
        failure = None
        started = self._clock()
        try:
            result = self._registry.provider(provider_id).generate(call)
        except AIGatewayError as error:
            failure = error.code if error.code in ADAPTER_CODES else "provider_error"
        except Exception:  # a foreign exception may carry the prompt or a secret in its text: classify it, drop it
            failure = "provider_error"
        duration = self._clock() - started
        # Raised outside the except blocks above on purpose: the provider's exception must not become this error's __context__.
        if failure is not None:
            raise fail(failure, "provider_failed")
        if duration > timeout:
            raise fail("timeout", "answer_after_deadline")

        if not isinstance(result, ProviderResult) or not isinstance(result.raw_output, str):
            raise fail("provider_error", "malformed_provider_result")
        if not isinstance(result.reported_model_id, str) or MODEL_ID_PATTERN.fullmatch(result.reported_model_id) is None:
            raise fail("provider_error", "invalid_reported_model")
        for reported in (result.input_tokens, result.output_tokens):
            if reported is not None and (not isinstance(reported, int) or isinstance(reported, bool) or reported < 0):
                raise fail("provider_error", "invalid_reported_usage")
        if estimate_tokens(result.raw_output) > max_output or (result.output_tokens is not None and result.output_tokens > max_output):
            raise fail("invalid_structured_output", "output_too_large")

        parsed_ok, parsed = _parse_json(result.raw_output)
        if not parsed_ok:
            raise fail("invalid_structured_output", "not_json")
        frozen_ok, output = _freeze_output(parsed)
        if not frozen_ok:
            raise fail("invalid_structured_output", "invalid_json_value")
        if not isinstance(output, Mapping):
            raise fail("invalid_structured_output", "not_an_object")
        violations = validate_output(request.output_schema, output)
        if violations:
            raise fail("invalid_structured_output", "schema_violation", violations)

        logger.info("ai_gateway call ok profile=%s provider=%s locality=%s duration=%.3f", profile.profile_id, provider_id, descriptor.locality.value, duration)
        return GenerationResponse(
            output=output,
            provenance=Provenance(
                profile_id=profile.profile_id,
                provider_id=provider_id,
                provider_kind=descriptor.kind,
                locality=descriptor.locality,
                policy_mode=policy.mode,
                requested_model_id=profile.model_id,
                reported_model_id=result.reported_model_id,
                test_only=descriptor.capabilities.test_only,
                duration_seconds=duration,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
        )
