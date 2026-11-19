"""Data-locality policy. Pure Python, standard library only.

THREE POLICY MODES, THREE LOCALITIES. A company runs in exactly one policy mode. A provider DECLARES where it processes data (its
locality). A call is permitted only when the locality is allowed by the mode, and the allowance is cumulative toward the more
restrictive localities:

    local_only         -> local
    company_endpoint   -> local, company_endpoint
    approved_cloud     -> local, company_endpoint, cloud   (a cloud provider must also be named in ``approved_cloud_providers``)

RULES (pinned by tests/test_ai_gateway_policy.py):

* ``local_only`` never permits a company endpoint or a cloud provider.
* The check runs BEFORE any provider code is called, on every call, against the mode passed to THAT call.
* There is no fallback anywhere: the gateway calls the one provider a profile names, or fails. It never tries another.
* Anything missing or invalid (a mode that is not a ``PolicyMode``, a context that is not a ``PolicyContext``) is a configuration error,
  never a default.

The locality is the provider's own declaration and is trusted as such; nothing here can observe where a provider really sends data
(docs/architecture/ai-gateway.md, "Security limitations").
"""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .errors import AIGatewayError, configuration_error


class PolicyMode(StrEnum):
    LOCAL_ONLY = "local_only"
    COMPANY_ENDPOINT = "company_endpoint"
    APPROVED_CLOUD = "approved_cloud"


class DataLocality(StrEnum):
    LOCAL = "local"
    COMPANY_ENDPOINT = "company_endpoint"
    CLOUD = "cloud"


ALLOWED_LOCALITIES = MappingProxyType(
    {
        PolicyMode.LOCAL_ONLY: frozenset({DataLocality.LOCAL}),
        PolicyMode.COMPANY_ENDPOINT: frozenset({DataLocality.LOCAL, DataLocality.COMPANY_ENDPOINT}),
        PolicyMode.APPROVED_CLOUD: frozenset({DataLocality.LOCAL, DataLocality.COMPANY_ENDPOINT, DataLocality.CLOUD}),
    }
)


def coerce_enum(enum_type, value, *, field_name: str):
    """``value`` as a member of ``enum_type``: a member itself, or its exact string. Anything else is a configuration error."""
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    raise configuration_error(f"invalid_{field_name}", f"{field_name} is not one of the supported values")


@dataclass(frozen=True)
class PolicyContext:
    """The data policy in force for one call: the company's mode and, for the cloud mode, the providers an administrator approved."""

    mode: PolicyMode
    approved_cloud_providers: frozenset = field(default_factory=frozenset)

    def __post_init__(self):
        object.__setattr__(self, "mode", coerce_enum(PolicyMode, self.mode, field_name="policy_mode"))
        providers = self.approved_cloud_providers
        if isinstance(providers, (str, bytes)) or not all(isinstance(p, str) for p in providers):
            raise configuration_error("invalid_approved_cloud_providers", "approved_cloud_providers must be a collection of provider ids")
        object.__setattr__(self, "approved_cloud_providers", frozenset(providers))


def locality_allowed(mode: PolicyMode, locality: DataLocality) -> bool:
    return locality in ALLOWED_LOCALITIES[mode]


def enforce_policy(policy, *, profile_mode: PolicyMode, provider_id: str, locality: DataLocality, profile_id: str | None = None) -> None:
    """Raise ``policy_violation`` unless the call may proceed. Calls no provider code and does no I/O.

    The locality must be allowed by the profile's own mode (what the profile was approved for) AND by the mode in force now.
    """
    if not isinstance(policy, PolicyContext):
        raise configuration_error("missing_policy_context", "a PolicyContext is required for every call")
    if not locality_allowed(profile_mode, locality) or not locality_allowed(policy.mode, locality):
        raise AIGatewayError("policy_violation", reason="locality_not_allowed", provider_id=provider_id, profile_id=profile_id)
    if locality is DataLocality.CLOUD and provider_id not in policy.approved_cloud_providers:
        raise AIGatewayError("policy_violation", reason="cloud_provider_not_approved", provider_id=provider_id, profile_id=profile_id)
