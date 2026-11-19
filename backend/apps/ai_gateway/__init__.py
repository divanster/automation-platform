"""The AI Gateway foundation (AI-1a): provider-neutral contracts, data-locality policy enforcement, a static provider registry and a
deterministic test provider. Standard library only.

This package is NOT a Django app: it has no models, no migrations, no settings entry, no URLs and no management command, and it imports no
Django, no other ``apps.*`` package and no business workflow. It can be imported, and its tests run, with plain ``unittest``.

* ``contracts``: immutable ``ModelProfile``, ``ProviderDescriptor`` and ``ProviderCapabilities``, ``GenerationRequest``,
  ``GenerationResponse`` and ``Provenance``.
* ``policy``: the three policy modes, the three localities and ``enforce_policy``.
* ``provider``: the adapter base class and the call and result values.
* ``registry``: the immutable, allowlisted ``ProviderRegistry``.
* ``gateway``: ``AIGateway``, which checks policy, calls a provider and validates the structured output.
* ``structured``: the closed JSON Schema subset and its validator. ``jsonvalue``: bounded immutable JSON.
* ``errors``: the stable error codes.
* ``adapters``: provider adapters. AI-1a has only the deterministic TEST adapter (not re-exported here: it is test-only).

NO REAL PROVIDER EXISTS. Nothing calls this package yet: Request Intake still uses its own deterministic fixture parser. The only place
that names providers and profiles is the composition root ``config/ai_gateway.py``, whose built-in tuples are EMPTY. Design and limits:
docs/architecture/ai-gateway.md.
"""

from .contracts import (
    GenerationParameters,
    GenerationRequest,
    GenerationResponse,
    ModelProfile,
    Provenance,
    ProviderCapabilities,
    ProviderDescriptor,
    StructuredOutputConfig,
    StructuredOutputMode,
    TokenLimits,
)
from .errors import ADAPTER_CODES, ERROR_CODES, RETRYABLE_CODES, AIGatewayError
from .gateway import AIGateway
from .policy import ALLOWED_LOCALITIES, DataLocality, PolicyContext, PolicyMode, enforce_policy
from .provider import AIProvider, ProviderCall, ProviderResult
from .registry import ProviderRegistry

__all__ = [
    "ADAPTER_CODES", "ALLOWED_LOCALITIES", "ERROR_CODES", "RETRYABLE_CODES", "AIGateway", "AIGatewayError", "AIProvider", "DataLocality",
    "GenerationParameters", "GenerationRequest", "GenerationResponse", "ModelProfile", "PolicyContext", "PolicyMode", "Provenance",
    "ProviderCall", "ProviderCapabilities", "ProviderDescriptor", "ProviderRegistry", "ProviderResult", "StructuredOutputConfig",
    "StructuredOutputMode", "TokenLimits", "enforce_policy",
]  # fmt: skip
