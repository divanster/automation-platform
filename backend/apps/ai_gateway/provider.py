"""The adapter side of the contract: what the gateway hands a provider and what it expects back. Pure Python, standard library only.

An adapter is a thin translation to one provider kind. It receives a ``ProviderCall`` (no policy, no profile, no company, no workflow) and
returns a ``ProviderResult`` with the model's RAW text, or raises ``AIGatewayError`` with a code from ``errors.ADAPTER_CODES``. It does not
validate the structure, apply policy, fall back to another provider or model, or log content: the gateway does the first two, nothing does the
others (docs/architecture/ai-policy.md, section 2). An adapter MAY retry the SAME provider and model for a transient failure, but only
boundedly and inside the call's ``timeout_seconds`` (AI-1b: ``adapters/workbench.py``).

An adapter MUST honor ``timeout_seconds`` with the provider's own timeout (for example a socket timeout); the gateway cannot interrupt a
call that is running, it can only refuse a late answer (docs/architecture/ai-gateway.md, "Security limitations").
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .contracts import GenerationParameters, ProviderDescriptor, StructuredOutputMode


@dataclass(frozen=True)
class ProviderCall:
    """Everything one provider invocation may use. ``instructions``, ``input_text`` and ``output_schema`` are content: not in ``repr``."""

    model_id: str
    instructions: str = field(repr=False)
    input_text: str = field(repr=False)
    output_schema: object = field(repr=False)
    structured_output_mode: StructuredOutputMode
    timeout_seconds: float
    max_output_tokens: int
    parameters: GenerationParameters


@dataclass(frozen=True)
class ProviderResult:
    """A provider's answer: the model's raw text (not yet parsed or validated) and what the provider reports about the run."""

    raw_output: str = field(repr=False)
    reported_model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class AIProvider(ABC):
    """Base class of every adapter. A subclass is registered by OBJECT in a static composition root, never found by name or path."""

    @property
    @abstractmethod
    def descriptor(self) -> ProviderDescriptor:
        """The provider's declaration (id, kind, locality, capabilities). Read once when the provider is registered."""

    @abstractmethod
    def generate(self, call: ProviderCall) -> ProviderResult:
        """Run one generation. Raise ``AIGatewayError`` (an adapter code) on failure; never put content in a message."""
