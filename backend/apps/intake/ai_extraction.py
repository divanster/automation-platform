"""The AI extractor of Request Intake: a ``workflows.request_intake.RequestExtractor`` that asks the AI Gateway (AI-2a).

THE ONLY FILE OF THE APP THAT NAMES THE GATEWAY. ``extract(text)`` builds one ``GenerationRequest`` (the prompt and schema of ``apps.intake.ai_output``),
calls ``AIGateway.generate`` with the policy it was built with, and turns the validated answer into the same ``ExtractionResult`` the fixture parser
returns. It does no HTTP, holds no credential, reads no environment and logs nothing: the provider, its authentication and its transport are the
gateway's and the composition root's (``config/intake_extraction.py``). The gateway checks the data policy BEFORE any
provider code runs, on every call, and never falls back to another provider or profile.

WHERE IT RUNS. ``run_extraction`` calls ``extract`` between its two short transactions, with no transaction open (``apps.intake.services``), so a slow
model never holds a lock or a connection transaction. Validation, canonicalization, hashing, revision 1, the outcome and the audit events are
``run_extraction``'s and are unchanged: this class returns a CANDIDATE and nothing else, and it has no way to approve, reject or write anything.

FAILURES. Every failure is an ``AIExtractionError`` with a stable ``code`` and a ``retryable`` flag taken from the gateway (only ``provider_unavailable``,
``timeout`` and ``rate_limit`` are retryable). The gateway's own message is fixed and the provider's exception is never chained, so nothing from the
request, the answer or the provider can reach the error, the task or a log line. There is no deterministic fallback: a failure stays a failure.
"""

from collections.abc import Callable

from apps.ai_gateway import AIGateway, AIGatewayError, GenerationRequest, PolicyContext
from workflows.request_intake import ExtractionResult

from .ai_output import EXTRACTOR_NAME, INSTRUCTIONS, OUTPUT_SCHEMA, PROMPT_VERSION, AIOutputError, build_result, delimited_request_text
from .errors import AIExtractionError


class AIRequestExtractor:
    """A ``RequestExtractor`` backed by the AI Gateway. Immutable after construction; one instance serves every extraction of a worker process.

    ``preflight`` is called with no arguments immediately before the text is sent (after the claim, outside any transaction). It raises
    ``AIExtractionError`` to stop the call, which is how the composition root re-checks that the module is still enabled and version-matched.
    ``timeout_seconds`` may only lower the profile's own timeout.
    """

    name = EXTRACTOR_NAME
    version = PROMPT_VERSION
    kind = "ai_gateway"

    __slots__ = ("_gateway", "_profile_id", "_policy", "_timeout_seconds", "_preflight")

    def __init__(self, gateway: AIGateway, *, profile_id: str, policy: PolicyContext, timeout_seconds: float | None = None, preflight: Callable[[], None] | None = None):
        if not isinstance(gateway, AIGateway):
            raise TypeError("gateway must be an AIGateway")
        if not isinstance(policy, PolicyContext):
            raise TypeError("policy must be a PolicyContext")
        if not isinstance(profile_id, str) or not profile_id:
            raise TypeError("profile_id must be a non-empty str")
        if preflight is not None and not callable(preflight):
            raise TypeError("preflight must be callable")
        object.__setattr__(self, "_gateway", gateway)
        object.__setattr__(self, "_profile_id", profile_id)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_timeout_seconds", timeout_seconds)
        object.__setattr__(self, "_preflight", preflight)

    def __setattr__(self, name, value):
        raise AttributeError("an AIRequestExtractor is immutable")

    def __repr__(self):  # no text, prompt or answer is ever held, but keep the repr to identifiers anyway
        return f"AIRequestExtractor(profile_id={self._profile_id!r}, policy={self._policy.mode.value!r})"

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def extract(self, text: str) -> ExtractionResult:
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        if self._preflight is not None:
            self._preflight()
        try:
            request = GenerationRequest(
                profile_id=self._profile_id,
                instructions=INSTRUCTIONS,
                input_text=delimited_request_text(text),
                output_schema=OUTPUT_SCHEMA,
                timeout_seconds=self._timeout_seconds,
            )
            response = self._gateway.generate(request, policy=self._policy)
        except AIGatewayError as error:
            raise AIExtractionError(error.code, error.reason, retryable=error.retryable) from None
        try:
            return build_result(
                response.output,
                text,
                profile_id=response.provenance.profile_id,
                reported_model_id=response.provenance.reported_model_id,
            )
        except AIOutputError as error:
            raise AIExtractionError("invalid_ai_output", error.reason) from None
