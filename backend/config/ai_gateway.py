"""The AI Gateway composition root (AI-1a): the ONE place that names AI providers and model profiles.

Both tuples are EMPTY. There is no real provider yet (AI-1b and later), and the deterministic test provider is NEVER listed here: it is
built by tests, and the gateway refuses a test-only provider unless asked (``allow_test_providers``), which this module never does. With
nothing registered, every call fails with ``configuration_error``; there is no default provider and no fallback.

Providers are objects in a static tuple: no import by name, no discovery, no environment or settings lookup, no file read. Nothing in the
application imports this module yet (Request Intake is unchanged), and no setting or environment variable is read, so there is nothing to
misconfigure and no secret to hold. See docs/architecture/ai-gateway.md.
"""

from apps.ai_gateway import AIGateway, ProviderRegistry

BUILTIN_PROVIDERS: tuple = ()
BUILTIN_PROFILES: tuple = ()


def build_provider_registry() -> ProviderRegistry:
    return ProviderRegistry.build(BUILTIN_PROVIDERS)


def build_gateway() -> AIGateway:
    return AIGateway(build_provider_registry(), BUILTIN_PROFILES)
