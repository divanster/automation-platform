"""The OpenAI-compatible provider (``openai_compatible``). Pure Python, standard library only; it opens no socket (see ``..transport``).

WHAT IT IS. A thin translation of one ``ProviderCall`` into one ``POST {endpoint}/chat/completions`` in the widely implemented OpenAI chat-completions
shape (vLLM, llama.cpp server, Ollama's ``/v1`` surface, LiteLLM-style company gateways), and of the answer back into the model's RAW text. It applies no
policy and does not validate the structure: the gateway does both. It is NOT the Ollama-native API and not a cloud-vendor adapter.

WHAT A DEPLOYMENT DECLARES, EXPLICITLY, WITH NO DEFAULT (``OpenAICompatibleConfig``):

* ``endpoint``: the base URL; the adapter appends ``/chat/completions`` and nothing else. No credentials, query, fragment or percent-encoding in it.
* ``allowed_origins``: the origins (``scheme://host:port``) this installation approves as destinations. The endpoint's origin must be one of them, so a changed
  endpoint alone sends nothing anywhere: someone has to approve the destination separately. The adapter re-checks the built URL against it before every request.
* ``locality``: where the endpoint processes data (``local``, ``company_endpoint`` or ``cloud``). It is a declaration by the operator and is never inferred
  from a host name; the gateway's policy then decides, before any call, whether the installation's mode permits it. The configuration refuses a declaration that
  contradicts the endpoint: a loopback host is only ever ``local``, a non-loopback host is never ``local``, and a literal IP address must be one the locality may
  reach (inside an approved CIDR range for ``company_endpoint``, global for ``cloud``, never a denied special-purpose range for either).
* ``approved_networks``: REQUIRED for ``company_endpoint`` and refused for the other localities: the CIDR ranges (``10.20.0.0/16``) the company's endpoint may
  resolve to. Every address its host name resolves to must be inside one of them, so a public cloud host cannot be passed off as a company endpoint by
  declaration alone, and a range can never include a denied special-purpose range (``network.DENIED_NETWORKS``).
* ``model_id``: the one model this deployment calls; it is sent as ``model`` and recorded as the requested model.
* ``api_key``: optional only for ``local``; ``company_endpoint`` and ``cloud`` fail closed without one. It is sent as ``Authorization: Bearer <key>`` and nowhere else.

TRANSPORT RULES. ``https`` always; plain ``http`` only for a ``local`` endpoint whose host is ``localhost`` or a loopback literal. Every request carries the
``NetworkClass`` of its locality (and, for ``company_endpoint``, its approved CIDR ranges), and the transport (not this module) resolves the host, refuses unless every address it may use is permitted, connects to a
validated address and follows no redirect; a redirect status is refused here too (``redirect_refused``). What the transport enforces cannot be seen from
this module.

THE REQUEST. ``{"model", "messages": [system, user], "response_format": {"type": "json_object"}, "stream": false, "max_tokens"}`` plus ``temperature``,
``top_p`` and ``seed`` only when the profile sets them. Structured output is ``json_mode`` only; ``json_schema`` (native) is not used because servers differ in
what they accept, and the gateway validates the schema in every mode anyway.

ERRORS. Every failure raises ``AIGatewayError`` with an adapter code and a fixed ``reason`` token; no server text, status line, body, URL, header or key is ever
copied into one. 401 and 403 are ``authentication_failure``; 404 is ``unsupported_model`` (model or path not found); 429 is ``rate_limit``; 408 and a transport
timeout are ``timeout``; 502, 503, 504 and a refused connection are ``provider_unavailable``; anything else is ``provider_error``.

RETRIES. At most ``max_retries`` further attempts to the SAME endpoint and model, only for a refused connection, 429, 502, 503 and 504, all inside the call's
deadline; no back-off sleeps past it. There is no fallback to another endpoint, model or provider.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field

from ..contracts import MODEL_ID_PATTERN, ProviderCapabilities, ProviderDescriptor, StructuredOutputMode
from ..errors import AIGatewayError, configuration_error
from ..jsonvalue import canonical_json
from ..network import NETWORK_CLASS_BY_LOCALITY, Origin, address_permitted, is_loopback_name, parse_ip, parse_networks, parse_url
from ..policy import DataLocality, coerce_enum
from ..provider import AIProvider, ProviderCall, ProviderResult
from ..transport import HttpRequest, HttpTransport, TransportError

logger = logging.getLogger("apps.ai_gateway")

PROVIDER_ID = "openai_compatible"
PROVIDER_KIND = "openai_compatible_chat"
CHAT_SUFFIX = "/chat/completions"
UNREPORTED_MODEL = "unreported"  # recorded as the reported model when an answer names none

DEFAULT_MAX_INPUT_TOKENS = 32_000  # provisional, per deployment: a local model's window is usually far smaller than a hosted one's
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
CAPABILITY_MAX_INPUT_TOKENS = 1_000_000
CAPABILITY_MAX_OUTPUT_TOKENS = 128_000
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 2
CONNECT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES_LIMIT = 5
MAX_ALLOWED_ORIGINS = 10
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0

_API_KEY = re.compile(r"[\x21-\x7e]{1,512}")  # visible ASCII only: no space, control character or line break can reach a header

_RETRYABLE_STATUSES = {429: "rate_limit", 502: "provider_unavailable", 503: "provider_unavailable", 504: "provider_unavailable"}
_STATUS_CODES = {401: "authentication_failure", 403: "authentication_failure", 404: "unsupported_model", 408: "timeout"}
_TRANSPORT_FAILURES = {  # kind -> (adapter code, retryable, reason)
    "timeout": ("timeout", False, "transport_timeout"),
    "connect": ("provider_unavailable", True, "transport_connect"),
    "tls": ("provider_error", False, "tls_failure"),
    "protocol": ("provider_error", False, "transport_protocol"),
    "destination": ("provider_error", False, "destination_refused"),
}


def origin_problem(origin: Origin, locality: DataLocality, approved_networks=()):
    """A fixed reason token when ``origin`` may not serve ``locality``, else ``None``. The one rule for configuration and for the last check before a request."""
    if locality is DataLocality.LOCAL:
        ip = parse_ip(origin.host)
        if origin.host != "localhost" and not (ip is not None and ip.is_loopback):
            return "endpoint_locality_mismatch"  # a local endpoint must name this machine, by ``localhost`` or a loopback literal
        return None
    if is_loopback_name(origin.host):
        return "endpoint_locality_mismatch"  # a loopback host is never a company or cloud endpoint
    if origin.scheme != "https":
        return "plain_http_not_allowed"
    if parse_ip(origin.host) is not None and not address_permitted(origin.host, NETWORK_CLASS_BY_LOCALITY[locality], approved_networks):
        return "destination_not_allowed"
    return None


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """Everything the adapter needs, validated when built. Errors name the field and never echo the value."""

    endpoint: str
    allowed_origins: tuple
    locality: DataLocality
    model_id: str
    api_key: str | None = field(default=None, repr=False, compare=False)
    approved_networks: tuple = ()  # CIDR ranges: required for company_endpoint, refused for the other localities
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS

    def __post_init__(self):
        locality = coerce_enum(DataLocality, self.locality, field_name="locality")
        object.__setattr__(self, "locality", locality)
        if not isinstance(self.model_id, str) or MODEL_ID_PATTERN.fullmatch(self.model_id) is None:
            raise configuration_error("invalid_openai_model", "the OpenAI-compatible model identifier is missing or invalid")
        if self.api_key is not None and (not isinstance(self.api_key, str) or _API_KEY.fullmatch(self.api_key) is None):
            raise configuration_error("invalid_openai_api_key", "the OpenAI-compatible api_key is invalid")
        if self.api_key is None and locality is not DataLocality.LOCAL:
            raise configuration_error("missing_openai_api_key", "a company or cloud endpoint needs an api_key; only a local endpoint may go without")

        networks = ()
        provided = self.approved_networks
        if locality is DataLocality.COMPANY_ENDPOINT:
            if isinstance(self.approved_networks, (str, bytes)) or not hasattr(self.approved_networks, "__iter__") or not tuple(self.approved_networks):
                raise configuration_error("missing_openai_approved_networks", "a company endpoint needs the CIDR ranges it may resolve to")
            networks = parse_networks(self.approved_networks, field="openai_approved_networks")
        object.__setattr__(self, "approved_networks", networks)

        if isinstance(self.allowed_origins, (str, bytes)) or not hasattr(self.allowed_origins, "__iter__"):
            raise configuration_error("invalid_openai_allowed_origins", "allowed_origins must be a collection of origins")
        texts = tuple(self.allowed_origins)
        if not texts or len(texts) > MAX_ALLOWED_ORIGINS:
            raise configuration_error("invalid_openai_allowed_origins", f"allowed_origins must list from 1 to {MAX_ALLOWED_ORIGINS} origins")
        allowed = set()
        for text in texts:
            origin, _ = parse_url(text, field="openai_allowed_origins", allow_path=False)
            problem = origin_problem(origin, locality, networks)
            if problem is not None:
                raise configuration_error("invalid_openai_allowed_origins", f"an approved origin is not allowed for this locality ({problem})")
            allowed.add(origin)
        object.__setattr__(self, "allowed_origins", tuple(sorted(allowed, key=str)))

        origin, path = parse_url(self.endpoint, field="openai_endpoint")
        problem = origin_problem(origin, locality, networks)
        if problem is not None:
            raise configuration_error("invalid_openai_endpoint", f"the OpenAI-compatible endpoint is not allowed for this locality ({problem})")
        if origin not in allowed:
            raise configuration_error("openai_endpoint_not_approved", "the OpenAI-compatible endpoint's origin is not among the approved origins")
        if locality is not DataLocality.COMPANY_ENDPOINT and provided:
            raise configuration_error("openai_approved_networks_not_applicable", "approved_networks applies to a company endpoint only; a local endpoint is loopback and a cloud one is global")
        object.__setattr__(self, "_origin", origin)
        object.__setattr__(self, "_base_path", path.rstrip("/"))

        for name, limit in (("max_input_tokens", CAPABILITY_MAX_INPUT_TOKENS), ("max_output_tokens", CAPABILITY_MAX_OUTPUT_TOKENS)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= limit:
                raise configuration_error(f"invalid_openai_{name}", f"the OpenAI-compatible {name} must be an integer from 1 to {limit}")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= MAX_RETRIES_LIMIT:
            raise configuration_error("invalid_openai_max_retries", f"the OpenAI-compatible max_retries must be an integer from 0 to {MAX_RETRIES_LIMIT}")
        if not isinstance(self.connect_timeout_seconds, (int, float)) or isinstance(self.connect_timeout_seconds, bool) or not 0 < self.connect_timeout_seconds <= 60:
            raise configuration_error("invalid_openai_connect_timeout", "the OpenAI-compatible connect timeout must be above 0 and at most 60 seconds")

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def url(self) -> str:
        return str(self._origin) + self._base_path + CHAT_SUFFIX


def _destination_allowed(config: OpenAICompatibleConfig, url: str) -> bool:
    """The last check before a request leaves the adapter: the URL is well formed, its origin is approved and suits the locality, and it is the chat path."""
    try:
        origin, path = parse_url(url, field="openai_endpoint")
    except AIGatewayError:
        return False
    return origin in config.allowed_origins and origin_problem(origin, config.locality, config.approved_networks) is None and path.endswith(CHAT_SUFFIX)


def _reject_constant(_name):
    raise ValueError("non-finite number")


class OpenAICompatibleProvider(AIProvider):
    """Registered by OBJECT in a static composition root. Construction reads nothing and makes no call."""

    def __init__(self, config: OpenAICompatibleConfig, transport: HttpTransport, *, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(config, OpenAICompatibleConfig):
            raise configuration_error("invalid_openai_config", "config must be an OpenAICompatibleConfig")
        if not isinstance(transport, HttpTransport):
            raise configuration_error("invalid_openai_transport", "transport must be an HttpTransport")
        self._config = config
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            kind=PROVIDER_KIND,
            locality=config.locality,  # the operator's explicit declaration; never inferred from the host name
            capabilities=ProviderCapabilities(
                supported_models=frozenset({config.model_id}),
                structured_output_modes=frozenset({StructuredOutputMode.JSON_MODE}),
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens,
                generation_parameters=frozenset({"temperature", "top_p", "seed"}),
            ),
        )

    def __repr__(self):
        return f"OpenAICompatibleProvider(provider_id={PROVIDER_ID!r}, locality={self._config.locality.value!r})"

    @property
    def descriptor(self):
        return self._descriptor

    def generate(self, call: ProviderCall) -> ProviderResult:
        config = self._config
        if call.model_id != config.model_id:
            raise AIGatewayError("unsupported_model", reason="unknown_model", provider_id=PROVIDER_ID)
        if call.structured_output_mode is not StructuredOutputMode.JSON_MODE:  # second line of defence behind the descriptor
            raise AIGatewayError("provider_error", reason="unsupported_request_option", provider_id=PROVIDER_ID)
        url = config.url
        if not _destination_allowed(config, url):
            raise AIGatewayError("provider_error", reason="destination_not_allowed", provider_id=PROVIDER_ID)
        headers = [("Content-Type", "application/json"), ("Accept", "application/json")]
        if config.api_key is not None:
            headers.append(("Authorization", f"Bearer {config.api_key}"))
        headers = tuple(headers)
        body = _build_body(call)
        network_class = NETWORK_CLASS_BY_LOCALITY[config.locality]

        deadline = self._clock() + call.timeout_seconds
        code, reason = "timeout", "deadline_exceeded"
        for attempt in range(config.max_retries + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                code, reason = "timeout", "deadline_exceeded"
                break
            request = HttpRequest(url, headers, body, min(config.connect_timeout_seconds, remaining), remaining, network_class, config.approved_networks)
            result, code, retryable, reason = self._attempt(request)
            if result is not None:
                return result
            if not retryable or attempt == config.max_retries:
                break
            delay = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)
            if deadline - self._clock() <= delay:  # not enough time left to wait and try again: stop instead of overrunning the deadline
                break
            logger.warning("openai-compatible attempt failed, retrying attempt=%d code=%s", attempt + 1, code)
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
        except Exception:  # a foreign exception may carry the URL, a header or the prompt: classify it, drop it
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
            return None, "provider_error", False, "redirect_refused"  # never followed: it would carry the key to wherever the answer points
        token = f"http_{status}" if 100 <= status <= 599 else "http_status"
        if status in _RETRYABLE_STATUSES:
            return None, _RETRYABLE_STATUSES[status], True, token
        return None, _STATUS_CODES.get(status, "provider_error"), False, token


def _build_body(call: ProviderCall) -> bytes:
    """``json_object`` needs the word JSON in the messages and the schema is not sent natively, so the instructions describe it."""
    instructions = f"{call.instructions}\n\nRespond with a single JSON object that conforms to this JSON Schema:\n{canonical_json(call.output_schema)}"
    body = {
        "model": call.model_id,
        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": call.input_text}],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": call.max_output_tokens,
    }
    for name in sorted(call.parameters.names()):
        body[name] = getattr(call.parameters, name)
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _parse_answer(raw: bytes):
    """``(ProviderResult, None, None)`` for a recognised answer, else ``(None, adapter code, reason)``. Nothing of the body is copied into a failure."""
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None, "provider_error", "unreadable_answer"
    choices = document.get("choices") if isinstance(document, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, "provider_error", "unexpected_answer_shape"
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("refusal") or not isinstance(message.get("content"), str):
        return None, "provider_error", "no_message_content"
    finish = choices[0].get("finish_reason")
    if finish == "length":  # cut off: the text cannot be a complete answer
        return None, "invalid_structured_output", "output_truncated"
    if finish not in (None, "stop"):
        return None, "provider_error", "unexpected_finish_reason"
    reported = document.get("model")
    if not isinstance(reported, str) or not reported:
        reported = UNREPORTED_MODEL  # recorded as such; never replaced by the model that was requested
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else {}

    def tokens(name):
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return ProviderResult(message["content"], reported, tokens("prompt_tokens"), tokens("completion_tokens")), None, None
