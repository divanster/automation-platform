"""The native Ollama provider (``ollama``, AI-CON-2). Pure Python, standard library only; it opens no socket (see ``..transport``).

WHAT IT IS. A thin translation of one ``ProviderCall`` into one ``POST {endpoint}/api/chat`` in Ollama's NATIVE chat shape (not the OpenAI-compatible ``/v1``
surface, which is ``adapters/openai_compatible.py``), and of the answer back into the model's RAW text. It applies no policy and does not validate the structure:
the gateway does both. It never calls any other Ollama route: no ``/api/pull``, ``/api/create``, ``/api/delete``, ``/api/tags`` or ``/api/generate``, so it never
downloads, creates or removes a model, and it never starts Ollama.

LOCAL ONLY, BY CONSTRUCTION. The provider's locality is always ``local`` (not configurable, so there is no declaration to get wrong) and its endpoint must name
THIS machine: ``localhost`` or a loopback literal (``127.0.0.0/8``, ``::1``). A LAN, company or cloud host is refused at configuration; this slice has no switch for
one (a remote Ollama is reached, with TLS, a key and approved address ranges, through ``openai_compatible``). Every request carries the ``loopback``
``NetworkClass``, so the guarded transport (``config/ai_guarded_transport.py``) resolves the host and refuses the call unless every address is loopback, connects to
a validated address and follows no redirect; a redirect status is refused here too.

WHAT A DEPLOYMENT DECLARES, EXPLICITLY, WITH NO DEFAULT (``OllamaConfig``):

* ``endpoint``: the base URL; the adapter appends ``/api/chat`` and nothing else. No credentials, query, fragment or percent-encoding in it.
* ``allowed_origins``: the origins (``scheme://host:port``) this installation approves. The endpoint's origin must be one of them, so a changed endpoint alone sends
  nothing anywhere. The adapter re-checks the built URL against it before every request.
* ``model_id``: the one model this deployment calls (for example ``llama3.1:8b``); sent as ``model`` and recorded as the requested model. A model that is not
  installed is an error (``unsupported_model``); it is never pulled and no other model is tried.

THE REQUEST. ``{"model", "messages": [system, user], "stream": false, "format", "options"}``. ``format`` is ``"json"`` for ``json_mode`` and the output JSON Schema
itself for ``native_schema`` (Ollama's structured outputs); in both modes the system message also states the schema in words. ``options`` always holds
``num_predict`` (the call's output limit) and holds ``temperature``, ``top_p`` and ``seed`` only when the profile sets them, and ``num_ctx`` only when the
deployment sets it. Streaming is never used. Nothing else is sent (no ``keep_alive``, ``tools`` or ``think``).

THE CONTEXT WINDOW. Ollama silently truncates a prompt that does not fit its context window (a small default that depends on the release). The adapter cannot see the
loaded window, so the token limits are the operator's declaration and, when ``num_ctx`` is set, the configuration refuses limits whose sum exceeds it.

ERRORS. Every failure raises ``AIGatewayError`` with an adapter code and a fixed ``reason`` token; no server text (Ollama's ``error`` string names the model and path),
status line, body or URL is ever copied into one. 401 and 403 are ``authentication_failure`` (a reverse proxy in front of Ollama); 404 is ``unsupported_model`` (the
model, or the route, is not there); 429 is ``rate_limit``; 408 and a transport timeout are ``timeout``; 502, 503 (Ollama's "server busy") and 504 and a refused
connection (Ollama is not running) are ``provider_unavailable``; ``done_reason`` ``length`` is ``invalid_structured_output``; anything else is ``provider_error``.

RETRIES. At most ``max_retries`` further attempts to the SAME endpoint and model, only for a refused connection, 429, 502, 503 and 504, all inside the call's
deadline. There is no fallback to another model, endpoint or provider.
"""

import json
import logging
import time
from dataclasses import dataclass

from ..contracts import MODEL_ID_PATTERN, ProviderCapabilities, ProviderDescriptor, StructuredOutputMode
from ..errors import AIGatewayError, configuration_error
from ..jsonvalue import canonical_json, thaw_json
from ..network import NetworkClass, Origin, parse_ip, parse_url
from ..policy import DataLocality
from ..provider import AIProvider, ProviderCall, ProviderResult
from ..transport import HttpRequest, HttpTransport, TransportError

logger = logging.getLogger("apps.ai_gateway")

PROVIDER_ID = "ollama"
PROVIDER_KIND = "ollama_native_chat"
LOCALITY = DataLocality.LOCAL
CHAT_SUFFIX = "/api/chat"
UNREPORTED_MODEL = "unreported"  # recorded as the reported model when an answer names none

# Provisional: Ollama's default context window is small (4096 tokens in current releases, 2048 in older ones), shared by the prompt and the answer.
DEFAULT_MAX_INPUT_TOKENS = 3_072
DEFAULT_MAX_OUTPUT_TOKENS = 1_024
CAPABILITY_MAX_INPUT_TOKENS = 1_000_000
CAPABILITY_MAX_OUTPUT_TOKENS = 128_000
MIN_NUM_CTX = 512
MAX_NUM_CTX = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 90.0  # the first call after a model is loaded into memory can be slow
DEFAULT_MAX_RETRIES = 2
CONNECT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES_LIMIT = 5
MAX_ALLOWED_ORIGINS = 10
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0

_RETRYABLE_STATUSES = {429: "rate_limit", 502: "provider_unavailable", 503: "provider_unavailable", 504: "provider_unavailable"}
_STATUS_CODES = {401: "authentication_failure", 403: "authentication_failure", 404: "unsupported_model", 408: "timeout"}
_TRANSPORT_FAILURES = {  # kind -> (adapter code, retryable, reason)
    "timeout": ("timeout", False, "transport_timeout"),
    "connect": ("provider_unavailable", True, "transport_connect"),
    "tls": ("provider_error", False, "tls_failure"),
    "protocol": ("provider_error", False, "transport_protocol"),
    "destination": ("provider_error", False, "destination_refused"),
}


def origin_problem(origin: Origin):
    """A fixed reason token when ``origin`` is not this machine, else ``None``: the one rule for configuration and for the last check before a request."""
    ip = parse_ip(origin.host)
    if origin.host == "localhost" or (ip is not None and ip.is_loopback):
        return None
    return "endpoint_not_loopback"


@dataclass(frozen=True)
class OllamaConfig:
    """Everything the adapter needs, validated when built. Errors name the field and never echo the value."""

    endpoint: str
    allowed_origins: tuple
    model_id: str
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS
    num_ctx: int | None = None

    def __post_init__(self):
        if not isinstance(self.model_id, str) or MODEL_ID_PATTERN.fullmatch(self.model_id) is None:
            raise configuration_error("invalid_ollama_model", "the Ollama model identifier is missing or invalid")

        if isinstance(self.allowed_origins, (str, bytes)) or not hasattr(self.allowed_origins, "__iter__"):
            raise configuration_error("invalid_ollama_allowed_origins", "allowed_origins must be a collection of origins")
        texts = tuple(self.allowed_origins)
        if not texts or len(texts) > MAX_ALLOWED_ORIGINS:
            raise configuration_error("invalid_ollama_allowed_origins", f"allowed_origins must list from 1 to {MAX_ALLOWED_ORIGINS} origins")
        allowed = set()
        for text in texts:
            origin, _ = parse_url(text, field="ollama_allowed_origins", allow_path=False)
            problem = origin_problem(origin)
            if problem is not None:
                raise configuration_error("invalid_ollama_allowed_origins", f"an approved origin is not allowed for a local provider ({problem})")
            allowed.add(origin)
        object.__setattr__(self, "allowed_origins", tuple(sorted(allowed, key=str)))

        origin, path = parse_url(self.endpoint, field="ollama_endpoint")
        problem = origin_problem(origin)
        if problem is not None:
            raise configuration_error("invalid_ollama_endpoint", f"the Ollama endpoint is not allowed for a local provider ({problem})")
        if origin not in allowed:
            raise configuration_error("ollama_endpoint_not_approved", "the Ollama endpoint's origin is not among the approved origins")
        object.__setattr__(self, "_origin", origin)
        object.__setattr__(self, "_base_path", path.rstrip("/"))

        for name, limit in (("max_input_tokens", CAPABILITY_MAX_INPUT_TOKENS), ("max_output_tokens", CAPABILITY_MAX_OUTPUT_TOKENS)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= limit:
                raise configuration_error(f"invalid_ollama_{name}", f"the Ollama {name} must be an integer from 1 to {limit}")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= MAX_RETRIES_LIMIT:
            raise configuration_error("invalid_ollama_max_retries", f"the Ollama max_retries must be an integer from 0 to {MAX_RETRIES_LIMIT}")
        if not isinstance(self.connect_timeout_seconds, (int, float)) or isinstance(self.connect_timeout_seconds, bool) or not 0 < self.connect_timeout_seconds <= 60:
            raise configuration_error("invalid_ollama_connect_timeout", "the Ollama connect timeout must be above 0 and at most 60 seconds")
        if self.num_ctx is not None:
            if not isinstance(self.num_ctx, int) or isinstance(self.num_ctx, bool) or not MIN_NUM_CTX <= self.num_ctx <= MAX_NUM_CTX:
                raise configuration_error("invalid_ollama_num_ctx", f"the Ollama num_ctx must be an integer from {MIN_NUM_CTX} to {MAX_NUM_CTX}")
            if self.max_input_tokens + self.max_output_tokens > self.num_ctx:
                raise configuration_error("ollama_limits_exceed_num_ctx", "the input and output token limits together exceed num_ctx, so Ollama would truncate the prompt")

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def url(self) -> str:
        return str(self._origin) + self._base_path + CHAT_SUFFIX


def _destination_allowed(config: OllamaConfig, url: str) -> bool:
    """The last check before a request leaves the adapter: the URL is well formed, its origin is approved and is this machine, and it is the chat path."""
    try:
        origin, path = parse_url(url, field="ollama_endpoint")
    except AIGatewayError:
        return False
    return origin in config.allowed_origins and origin_problem(origin) is None and path.endswith(CHAT_SUFFIX)


def _reject_constant(_name):
    raise ValueError("non-finite number")


class OllamaProvider(AIProvider):
    """Registered by OBJECT in a static composition root. Construction reads nothing and makes no call."""

    def __init__(self, config: OllamaConfig, transport: HttpTransport, *, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(config, OllamaConfig):
            raise configuration_error("invalid_ollama_config", "config must be an OllamaConfig")
        if not isinstance(transport, HttpTransport):
            raise configuration_error("invalid_ollama_transport", "transport must be an HttpTransport")
        self._config = config
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            kind=PROVIDER_KIND,
            locality=LOCALITY,
            capabilities=ProviderCapabilities(
                supported_models=frozenset({config.model_id}),
                structured_output_modes=frozenset({StructuredOutputMode.JSON_MODE, StructuredOutputMode.NATIVE_SCHEMA}),
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens,
                generation_parameters=frozenset({"temperature", "top_p", "seed"}),
            ),
        )

    def __repr__(self):
        return f"OllamaProvider(provider_id={PROVIDER_ID!r}, locality={LOCALITY.value!r})"

    @property
    def descriptor(self):
        return self._descriptor

    def generate(self, call: ProviderCall) -> ProviderResult:
        config = self._config
        if call.model_id != config.model_id:
            raise AIGatewayError("unsupported_model", reason="unknown_model", provider_id=PROVIDER_ID)
        if call.structured_output_mode not in (StructuredOutputMode.JSON_MODE, StructuredOutputMode.NATIVE_SCHEMA):  # second line of defence behind the descriptor
            raise AIGatewayError("provider_error", reason="unsupported_request_option", provider_id=PROVIDER_ID)
        url = config.url
        if not _destination_allowed(config, url):
            raise AIGatewayError("provider_error", reason="destination_not_allowed", provider_id=PROVIDER_ID)
        headers = (("Content-Type", "application/json"), ("Accept", "application/json"))
        body = _build_body(call, config.num_ctx)

        deadline = self._clock() + call.timeout_seconds
        code, reason = "timeout", "deadline_exceeded"
        for attempt in range(config.max_retries + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                code, reason = "timeout", "deadline_exceeded"
                break
            request = HttpRequest(url, headers, body, min(config.connect_timeout_seconds, remaining), remaining, NetworkClass.LOOPBACK)
            result, code, retryable, reason = self._attempt(request)
            if result is not None:
                return result
            if not retryable or attempt == config.max_retries:
                break
            delay = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)
            if deadline - self._clock() <= delay:  # not enough time left to wait and try again: stop instead of overrunning the deadline
                break
            logger.warning("ollama attempt failed, retrying attempt=%d code=%s", attempt + 1, code)
            self._sleep(delay)
        raise AIGatewayError(code, reason=reason, provider_id=PROVIDER_ID)

    def _attempt(self, request: HttpRequest):
        """One transport call: ``(result, None, False, None)`` or ``(None, adapter code, retryable, reason)``. Never raises, never keeps a foreign exception."""
        response = None
        failure = None
        try:
            response = self._transport.send(request)
        except TransportError as error:
            failure = _TRANSPORT_FAILURES.get(error.kind, ("provider_error", False, "transport_protocol"))
        except Exception:  # a foreign exception may carry the URL or the prompt: classify it, drop it
            failure = ("provider_error", False, "transport_failure")
        if failure is not None:
            return None, failure[0], failure[1], failure[2]
        status = getattr(response, "status", None)
        body = getattr(response, "body", None)
        if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, (bytes, bytearray)):
            return None, "provider_error", False, "invalid_response"
        if status == 200:
            result, code, reason = _parse_answer(bytes(body))
            return result, code, False, reason
        if 300 <= status < 400:
            return None, "provider_error", False, "redirect_refused"  # never followed
        token = f"http_{status}" if 100 <= status <= 599 else "http_status"
        if status in _RETRYABLE_STATUSES:
            return None, _RETRYABLE_STATUSES[status], True, token
        return None, _STATUS_CODES.get(status, "provider_error"), False, token


def _build_body(call: ProviderCall, num_ctx) -> bytes:
    """``format`` is ``"json"`` or the schema itself; the schema is also described in the instructions, which both modes need and Ollama recommends for the second."""
    instructions = f"{call.instructions}\n\nRespond with a single JSON object that conforms to this JSON Schema:\n{canonical_json(call.output_schema)}"
    options = {"num_predict": call.max_output_tokens}
    for name in sorted(call.parameters.names()):
        options[name] = getattr(call.parameters, name)
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    body = {
        "model": call.model_id,
        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": call.input_text}],
        "stream": False,
        "format": thaw_json(call.output_schema) if call.structured_output_mode is StructuredOutputMode.NATIVE_SCHEMA else "json",
        "options": options,
    }
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _parse_answer(raw: bytes):
    """``(ProviderResult, None, None)`` for a recognised answer, else ``(None, adapter code, reason)``. Nothing of the body is copied into a failure."""
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None, "provider_error", "unreadable_answer"
    if not isinstance(document, dict):
        return None, "provider_error", "unexpected_answer_shape"
    if "error" in document:
        return None, "provider_error", "provider_reported_error"  # its text names models and paths: never copied
    message = document.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        return None, "provider_error", "no_message_content"
    if document.get("done") is not True:
        return None, "provider_error", "incomplete_answer"
    finish = document.get("done_reason")
    if finish == "length":  # cut off at num_predict or the context window: the text cannot be a complete answer
        return None, "invalid_structured_output", "output_truncated"
    if finish not in (None, "stop"):
        return None, "provider_error", "unexpected_finish_reason"
    reported = document.get("model")
    if not isinstance(reported, str) or not reported:
        reported = UNREPORTED_MODEL  # recorded as such; never replaced by the model that was requested

    def tokens(name):
        value = document.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return ProviderResult(message["content"], reported, tokens("prompt_eval_count"), tokens("eval_count")), None, None
