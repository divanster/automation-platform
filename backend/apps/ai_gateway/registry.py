"""The provider registry: a static, immutable, allowlisted set of providers. Pure Python, standard library only.

``ProviderRegistry.build(providers)`` takes provider OBJECTS, checks them and returns a read-only registry. It imports nothing, scans
nothing, resolves no name or path and makes no external call: it reads each provider's descriptor (a plain data object) and nothing else,
so it cannot find a provider that was not handed to it. Which providers exist is decided in one place, the composition root
``config/ai_gateway.py``.

GUARANTEES (each pinned by tests/test_ai_gateway_registry.py):

* IMMUTABLE. No attribute can be set or deleted after ``build``; the list passed in can be changed afterwards without effect.
* DETERMINISTIC. Providers are ordered by provider id, whatever the input order.
* NO OVERWRITING. Two providers with one id are an error, never "last one wins".
* DESCRIPTORS ARE SNAPSHOTS. The descriptor is read once at ``build``; the gateway decides policy from that copy, not from a value the
  provider could change later.
* FAIL CLOSED. Any problem raises ``AIGatewayError`` (``configuration_error``) and builds nothing.
"""

from collections.abc import Iterable, Iterator

from .contracts import ProviderDescriptor
from .errors import configuration_error
from .provider import AIProvider

_BUILD = object()


class ProviderRegistry:
    """An immutable set of providers keyed by provider id. Create it only with ``ProviderRegistry.build``."""

    __slots__ = ("_providers", "_descriptors")

    def __init__(self, *, _token=None, providers=None, descriptors=None):
        if _token is not _BUILD:
            raise TypeError("a ProviderRegistry is created with ProviderRegistry.build()")
        object.__setattr__(self, "_providers", providers)
        object.__setattr__(self, "_descriptors", descriptors)

    def __setattr__(self, name, value):
        raise AttributeError("a ProviderRegistry is immutable")

    def __delattr__(self, name):
        raise AttributeError("a ProviderRegistry is immutable")

    @classmethod
    def build(cls, providers: Iterable[AIProvider]) -> "ProviderRegistry":
        if isinstance(providers, (str, bytes)) or not isinstance(providers, Iterable):
            raise configuration_error("invalid_provider_collection", "providers must be an iterable of provider objects")
        items = list(providers)  # a private copy
        found = {}
        descriptors = {}
        for provider in items:
            if not isinstance(provider, AIProvider):
                raise configuration_error("invalid_provider_object", "every registry entry must be an AIProvider object, never a path, name or class")
            descriptor = provider.descriptor
            if not isinstance(descriptor, ProviderDescriptor):
                raise configuration_error("invalid_provider_descriptor", "a provider's descriptor must be a ProviderDescriptor")
            if descriptor.provider_id in found:
                raise configuration_error("duplicate_provider_id", f"provider id '{descriptor.provider_id}' is registered twice")
            found[descriptor.provider_id] = provider
            descriptors[descriptor.provider_id] = descriptor
        order = sorted(found)
        return cls(
            _token=_BUILD,
            providers=tuple((provider_id, found[provider_id]) for provider_id in order),
            descriptors=tuple((provider_id, descriptors[provider_id]) for provider_id in order),
        )

    @property
    def provider_ids(self) -> tuple:
        return tuple(provider_id for provider_id, _ in self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, provider_id) -> bool:
        return any(provider_id == known for known, _ in self._providers)

    def __iter__(self) -> Iterator[str]:
        return iter(self.provider_ids)

    def provider(self, provider_id: str) -> AIProvider:
        for known, provider in self._providers:
            if known == provider_id:
                return provider
        raise configuration_error("unknown_provider", "the provider is not registered")

    def descriptor(self, provider_id: str) -> ProviderDescriptor:
        """The descriptor as it was when the registry was built."""
        for known, descriptor in self._descriptors:
            if known == provider_id:
                return descriptor
        raise configuration_error("unknown_provider", "the provider is not registered")
