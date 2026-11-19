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
* ``transport``: the HTTP seam (AI-1b) that HTTP adapters use instead of opening sockets.
* ``adapters``: provider adapters: the deterministic TEST adapter (not re-exported here: it is test-only) and, since AI-1b, the KPMG Workbench
  adapter (``adapters/workbench.py``).

NO REAL PROVIDER HAS EVER BEEN RUN. Nothing calls this package yet: Request Intake still uses its own deterministic fixture parser. The
composition root ``config/ai_gateway.py`` has EMPTY built-in tuples; the one real adapter (AI-1b) has its own opt-in composition and a
separate transport outside this package. Design and limits: docs/architecture/ai-gateway.md.
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
