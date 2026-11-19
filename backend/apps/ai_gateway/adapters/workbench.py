"""The KPMG Workbench provider (``kpmg_workbench``). Pure Python, standard library only; it opens no socket (see ``..transport``).

WHAT IT IS. A thin translation of one ``ProviderCall`` into one chat-completions POST to the Workbench Azure OpenAI gateway, and of the
answer back into the model's RAW text. It applies no policy and does not validate the structure: the gateway does both. It declares the
locality ``company_endpoint``, so ``local_only`` refuses it before this code runs.

WHERE THE WIRE CONTRACT COMES FROM. The client-side request shape below was reported by the owner from a read-only audit of the existing
FI integration (GP-0299-FI-APP); see docs/architecture/ai-gateway.md, section 14.10. It is CLIENT-SIDE evidence: no request from this
repository has ever reached the Workbench, so live compatibility is unproven.

* ``POST {endpoint}/deployments/{deployment}/chat/completions?api-version={api_version}``; the deployment is routed by the PATH;
* header ``Ocp-Apim-Subscription-Key: <raw key>`` and header ``x-kpmg-charge-code: <code>``;
* body ``{"messages": [system, user], "response_format": {"type": "json_object"}}`` and NOTHING else: no ``model`` (the api-version is in
  the URL), no temperature or other sampling parameter, and no token-limit parameter (none is verified; the gateway enforces the output
  limit after the fact);
* the answer is read from ``choices[0].message.content``, which holds JSON-encoded text.

Everything that is not in that list is conservative handling, not contract: ``finish_reason``, ``model``, ``usage`` and ``refusal`` are read
if present, and a missing ``model`` is recorded as ``unreported`` instead of being invented.

DESTINATION. The endpoint host must be on ``ALLOWED_ENDPOINT_HOSTS`` (one name, no port), so a configuration value can never send the key
somewhere else; the deployment is one of two known identifiers and the API version a validated token before either enters the URL; and the
built URL is checked again against the allowlist immediately before a request is handed to the transport.

CREDENTIAL. The key is held in ``WorkbenchConfig.api_key`` (excluded from ``repr`` and comparison), used only to build the authentication
header of an ``HttpRequest`` (whose ``repr`` excludes headers), and appears in no error, message, log line or reason. Errors raised for a
call carry fixed messages; the transport's and the server's text, status line and body are never copied.

RETRIES. At most ``max_retries`` further attempts to the SAME deployment, and only for conditions that never reached the model: a refused
connection, HTTP 429, 502, 503 and 504. The whole call, retries and back-off included, lives inside ``ProviderCall.timeout_seconds``; no
back-off sleeps past the deadline. There is no fallback to another deployment, model or provider, and a profile never changes deployment.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field

from ..contracts import ProviderCapabilities, ProviderDescriptor, StructuredOutputMode
from ..errors import AIGatewayError, configuration_error
from ..jsonvalue import canonical_json
from ..policy import DataLocality
from ..provider import AIProvider, ProviderCall, ProviderResult
from ..transport import HttpRequest, HttpTransport, TransportError

logger = logging.getLogger("apps.ai_gateway")

PROVIDER_ID = "kpmg_workbench"
PROVIDER_KIND = "kpmg_workbench_azure_openai"

# Client-side contract (owner's read-only audit of FI, GP-0299-FI-APP). Not proof of live compatibility.
DEFAULT_ENDPOINT = "https://api.workbench.kpmg/genai/azure/openai"
ALLOWED_ENDPOINT_HOSTS = frozenset({"api.workbench.kpmg"})  # the only hosts the key may be sent to; adding one is a reviewed code change
REGION = "westeurope"  # informational: nothing is sent for it
DEPLOYMENT_GPT5 = "gpt-5-2025-08-07-dzs-sdc"  # the preferred deployment
DEPLOYMENT_GPT41 = "gpt-4-1-2025-04-14-dzs-sdc"  # an alternative a caller must select explicitly, through its own profile
DEPLOYMENTS = frozenset({DEPLOYMENT_GPT5, DEPLOYMENT_GPT41})
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_CHAT_PATH = "/deployments/{deployment}/chat/completions?api-version={api_version}"
DEFAULT_AUTH_HEADER = "Ocp-Apim-Subscription-Key"  # carries the raw key, with no scheme prefix
DEFAULT_CHARGE_CODE = "00000000"  # a placeholder
CHARGE_CODE_HEADER = "x-kpmg-charge-code"
UNREPORTED_MODEL = "unreported"  # recorded as the reported model when an answer names none
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 2

CONNECT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES_LIMIT = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0

# Provisional, unmeasured limits that both deployments are documented (publicly) to exceed.
CAPABILITY_MAX_INPUT_TOKENS = 200_000
CAPABILITY_MAX_OUTPUT_TOKENS = 32_000

_RETRYABLE_STATUSES = {429: "rate_limit", 502: "provider_unavailable", 503: "provider_unavailable", 504: "provider_unavailable"}
_RESERVED_HEADERS = frozenset({"content-type", "accept", "content-length", "host", "transfer-encoding", "connection", "proxy-authorization", CHARGE_CODE_HEADER})

_ENDPOINT = re.compile(r"https://([a-z0-9][a-z0-9.-]{0,251}[a-z0-9])(/[A-Za-z0-9._~/-]*)?")  # lower-case host, no port, no credentials, no query
_PATH = re.compile(r"/[A-Za-z0-9._~/{}?&=-]{0,255}")
_DEPLOYMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
_API_KEY = re.compile(r"[\x21-\x7e]{1,512}")  # visible ASCII only: no space, control character or line break can reach a header
_API_VERSION = re.compile(r"[0-9A-Za-z.-]{1,32}")
_CHARGE_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class WorkbenchConfig:
    """Everything the adapter needs, validated when built. Errors name the field and never echo the value."""

    api_key: str = field(repr=False, compare=False)
    auth_header: str = DEFAULT_AUTH_HEADER  # the NAME of the header that carries the key (the value is the raw key, with no scheme prefix)
    chat_path: str = DEFAULT_CHAT_PATH  # path and query appended to the endpoint; placeholders {deployment} (required) and {api_version}
    endpoint: str = DEFAULT_ENDPOINT
    api_version: str = DEFAULT_API_VERSION
    charge_code: str = DEFAULT_CHARGE_CODE
    max_retries: int = DEFAULT_MAX_RETRIES
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS

    def __post_init__(self):
        def check(value, pattern, name):
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise configuration_error(f"invalid_workbench_{name}", f"the Workbench {name} is missing or invalid")

        check(self.api_key, _API_KEY, "api_key")
        check(self.auth_header, _HEADER_NAME, "auth_header")
        if self.auth_header.lower() in _RESERVED_HEADERS:
            raise configuration_error("invalid_workbench_auth_header", "the Workbench auth_header names a header the adapter sets itself")
        check(self.endpoint, _ENDPOINT, "endpoint")
        host = _ENDPOINT.fullmatch(self.endpoint).group(1)
        if host not in ALLOWED_ENDPOINT_HOSTS or ".." in self.endpoint or "//" in self.endpoint[len("https://") :]:
            raise configuration_error("invalid_workbench_endpoint", "the Workbench endpoint is not an allowed destination")
        check(self.api_version, _API_VERSION, "api_version")
        check(self.charge_code, _CHARGE_CODE, "charge_code")
        check(self.chat_path, _PATH, "chat_path")
        names = _PLACEHOLDER.findall(self.chat_path)
        leftover = _PLACEHOLDER.sub("", self.chat_path)
        if "{deployment}" not in self.chat_path or not set(names) <= {"deployment", "api_version"} or "{" in leftover or "}" in leftover or ".." in self.chat_path or "//" in self.chat_path:
            raise configuration_error("invalid_workbench_chat_path", "the Workbench chat_path is missing or invalid")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= MAX_RETRIES_LIMIT:
            raise configuration_error("invalid_workbench_max_retries", f"the Workbench max_retries must be an integer from 0 to {MAX_RETRIES_LIMIT}")
        if not isinstance(self.connect_timeout_seconds, (int, float)) or isinstance(self.connect_timeout_seconds, bool) or not 0 < self.connect_timeout_seconds <= 60:
            raise configuration_error("invalid_workbench_connect_timeout", "the Workbench connect timeout must be above 0 and at most 60 seconds")

    def url_for(self, deployment: str) -> str:
        """The request URL. The deployment must be a known identifier; the endpoint host was checked against the allowlist when this was built."""
        if deployment not in DEPLOYMENTS or _DEPLOYMENT.fullmatch(deployment) is None:
            raise configuration_error("invalid_workbench_deployment", "the Workbench deployment is not a known identifier")
        return self.endpoint.rstrip("/") + self.chat_path.replace("{deployment}", deployment).replace("{api_version}", self.api_version)


def _destination_allowed(url: str) -> bool:
    """True only for ``https://<allowed host>/...``: the last check before a request leaves the adapter."""
    match = re.fullmatch(r"https://([^/?#@:]+)/.*", url)
    return match is not None and match.group(1) in ALLOWED_ENDPOINT_HOSTS


def _reject_constant(_name):
    raise ValueError("non-finite number")


class WorkbenchProvider(AIProvider):
    """Registered by OBJECT in a static composition root. Construction reads nothing and makes no call."""

    def __init__(self, config: WorkbenchConfig, transport: HttpTransport, *, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(config, WorkbenchConfig):
            raise configuration_error("invalid_workbench_config", "config must be a WorkbenchConfig")
        if not isinstance(transport, HttpTransport):
            raise configuration_error("invalid_workbench_transport", "transport must be an HttpTransport")
        self._config = config
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            kind=PROVIDER_KIND,
            locality=DataLocality.COMPANY_ENDPOINT,
            capabilities=ProviderCapabilities(
                supported_models=DEPLOYMENTS,
                structured_output_modes=frozenset({StructuredOutputMode.JSON_MODE}),  # the one verified shape: response_format json_object
                max_input_tokens=CAPABILITY_MAX_INPUT_TOKENS,
                max_output_tokens=CAPABILITY_MAX_OUTPUT_TOKENS,
                generation_parameters=frozenset(),  # no sampling parameter is verified for either deployment, so none is ever sent
            ),
        )

    def __repr__(self):
        return f"WorkbenchProvider(provider_id={PROVIDER_ID!r})"

    @property
    def descriptor(self):
        return self._descriptor

    # the call ----------------------------------------------------------------------------------------------------------

    def generate(self, call: ProviderCall) -> ProviderResult:
        if call.model_id not in DEPLOYMENTS:
            raise AIGatewayError("unsupported_model", reason="unknown_deployment", provider_id=PROVIDER_ID)
        # Second line of defence behind the descriptor: nothing is sent that the verified request shape does not contain.
        if call.parameters.names() or call.structured_output_mode is not StructuredOutputMode.JSON_MODE:
            raise AIGatewayError("provider_error", reason="unverified_request_option", provider_id=PROVIDER_ID)

        config = self._config
        url = config.url_for(call.model_id)
        if not _destination_allowed(url):
            raise AIGatewayError("provider_error", reason="destination_not_allowed", provider_id=PROVIDER_ID)
        headers = (
            ("Content-Type", "application/json"),
            ("Accept", "application/json"),
            (config.auth_header, config.api_key),
            (CHARGE_CODE_HEADER, config.charge_code),
        )
        body = _build_body(call)

        deadline = self._clock() + call.timeout_seconds
        code = "timeout"
        for attempt in range(config.max_retries + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                code = "timeout"
                break
            request = HttpRequest(url, headers, body, min(config.connect_timeout_seconds, remaining), remaining)
            result, code, retryable = self._attempt(request)
            if result is not None:
                return result
            if not retryable or attempt == config.max_retries:
                break
            delay = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)
            if deadline - self._clock() <= delay:  # not enough time left to wait and try again: stop instead of overrunning the deadline
                break
            logger.warning("workbench attempt failed, retrying attempt=%d code=%s", attempt + 1, code)
            self._sleep(delay)
        raise AIGatewayError(code, reason="workbench_call_failed", provider_id=PROVIDER_ID)

    def _attempt(self, request: HttpRequest):
        """One transport call: ``(result, None, False)`` or ``(None, adapter code, retryable)``. Never raises, never keeps a foreign exception."""
        response = None
        failure = None
        try:
            response = self._transport.send(request)
        except TransportError as error:
            failure = {"timeout": ("timeout", False), "connect": ("provider_unavailable", True), "tls": ("provider_error", False)}.get(error.kind, ("provider_error", False))
        except Exception:  # a foreign exception may carry the URL, a header or the prompt: classify it, drop it
            failure = ("provider_error", False)
        if failure is not None:
            return None, failure[0], failure[1]
        status = getattr(response, "status", None)
        body = getattr(response, "body", None)
        if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, (bytes, bytearray)):
            return None, "provider_error", False
        if status == 200:
            result, code = _parse_answer(bytes(body))
            return result, code, False
        if status in _RETRYABLE_STATUSES:
            return None, _RETRYABLE_STATUSES[status], True
        return None, _STATUS_CODES.get(status, "provider_error"), False


# 401 and 403 share one code: the gateway has no separate authorization code (docs/architecture/ai-gateway.md, section 8).
# 404 is read as "the deployment is not available to this key"; with an unverified path it can also mean a wrong path.
_STATUS_CODES = {401: "authentication_failure", 403: "authentication_failure", 404: "unsupported_model", 408: "timeout"}


def _build_body(call: ProviderCall) -> bytes:
    """The verified request body: ``messages`` (system, user) and ``response_format`` ``json_object``, nothing else. ``json_object`` needs the word
    JSON in the messages, and the schema is not sent natively, so the instructions describe it."""
    instructions = f"{call.instructions}\n\nRespond with a single JSON object that conforms to this JSON Schema:\n{canonical_json(call.output_schema)}"
    body = {
        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": call.input_text}],
        "response_format": {"type": "json_object"},
    }
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _parse_answer(raw: bytes):
    """``(ProviderResult, None)`` for a recognised answer, else ``(None, adapter code)``. Nothing of the body is copied into a failure."""
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None, "provider_error"
    choices = document.get("choices") if isinstance(document, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, "provider_error"
    message = choices[0].get("message")  # the contract: choices[0].message.content holds JSON-encoded text
    if not isinstance(message, dict) or message.get("refusal") or not isinstance(message.get("content"), str):
        return None, "provider_error"
    finish = choices[0].get("finish_reason")
    if finish == "length":  # cut off; a missing finish_reason is accepted (the contract does not mention it) and the gateway validates the text
        return None, "invalid_structured_output"
    if finish not in (None, "stop"):
        return None, "provider_error"
    reported = document.get("model")
    if not isinstance(reported, str) or not reported:
        reported = UNREPORTED_MODEL  # recorded as such; never replaced by the deployment that was requested
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else {}

    def tokens(name):
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return ProviderResult(message["content"], reported, tokens("prompt_tokens"), tokens("completion_tokens")), None
