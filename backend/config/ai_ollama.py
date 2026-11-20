"""AI-CON-2 composition for the native Ollama provider: environment -> validated settings -> provider, profile and gateway.

OPT-IN, LIKE THE WORKBENCH AND THE OPENAI-COMPATIBLE PROVIDER. Nothing imports this module, ``config/ai_gateway.py`` (whose tuples stay EMPTY) does not use it, and
Request Intake's extraction and the AI settings catalog still know the Workbench only. A caller that wants this gateway calls ``build_ollama_gateway()`` explicitly.
Importing this module reads no environment and makes no network call; the environment is read, and every value validated, only when a ``load_`` or ``build_`` function
runs. The Django settings hold none of these values, and no value is stored in a database or in a ``ModelProfile``. Building the gateway never contacts, starts or
installs Ollama and never downloads a model.

FAIL CLOSED. A missing or invalid value raises ``AIGatewayError('configuration_error')`` naming the VARIABLE, never its value. There is NO default endpoint, approved
origin or model: an installation that sets nothing has no Ollama provider. There is no secret (Ollama's native API has no key here).

LOOPBACK ONLY. The provider is ``local``: the endpoint must be ``localhost`` or a loopback literal, and there is no variable for a locality, a company range or a proxy.
``AP_AI_OLLAMA_PROXY_URL`` is REFUSED when set (the guarded transport never sends a loopback request through a proxy or around one).

Variables (docs/development/runbook.md, section 5.9; docs/architecture/ai-gateway.md, section 18):

* required: ``AP_AI_OLLAMA_ENDPOINT`` (the base URL, for example ``http://127.0.0.1:11434``; ``/api/chat`` is appended), ``AP_AI_OLLAMA_ALLOWED_ORIGINS``
  (comma-separated ``scheme://host[:port]``; the endpoint's origin must be listed), ``AP_AI_OLLAMA_MODEL`` (the one installed model to call);
* optional: ``AP_AI_OLLAMA_STRUCTURED_OUTPUT`` (``json_mode``, the default, or ``native_schema``), ``AP_AI_OLLAMA_TIMEOUT_SECONDS`` (whole-call deadline, 90),
  ``AP_AI_OLLAMA_MAX_RETRIES`` (2), ``AP_AI_OLLAMA_MAX_INPUT_TOKENS`` (3072), ``AP_AI_OLLAMA_MAX_OUTPUT_TOKENS`` (1024), ``AP_AI_OLLAMA_NUM_CTX`` (unset: Ollama's own
  default window; when set it is sent as ``num_ctx`` and the two limits must fit inside it).
"""

import os
from dataclasses import dataclass

from apps.ai_gateway import AIGateway, AIGatewayError, ModelProfile, PolicyMode, ProviderRegistry, StructuredOutputConfig, StructuredOutputMode, TokenLimits
from apps.ai_gateway.adapters import ollama
from apps.ai_gateway.errors import configuration_error
from apps.ai_gateway.transport import HttpTransport

from config.ai_guarded_transport import GuardedHttpTransport

PROFILE_ID = "ollama-local"  # the one profile; there is no second one to fall back to
STRUCTURED_OUTPUT_MODES = {"json_mode": StructuredOutputMode.JSON_MODE, "native_schema": StructuredOutputMode.NATIVE_SCHEMA}


@dataclass(frozen=True)
class OllamaSettings:
    config: ollama.OllamaConfig
    timeout_seconds: float
    structured_output: StructuredOutputMode = StructuredOutputMode.JSON_MODE


def _read(environ, name, default=None, *, required=False):
    """A stripped value; an empty one counts as unset (the convention of ``config/env.py``)."""
    value = environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise configuration_error(f"missing_{name.lower()}", f"Environment variable {name} is required.")
    return default


def _read_int(environ, name, default, low, high):
    value = _read(environ, name)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise configuration_error(f"invalid_{name.lower()}", f"Environment variable {name} must be an integer.") from None
    if not low <= number <= high:
        raise configuration_error(f"invalid_{name.lower()}", f"Environment variable {name} must be from {low} to {high}.")
    return number


def load_ollama_settings(environ=None) -> OllamaSettings:
    """Read and validate the ``AP_AI_OLLAMA_*`` variables of ``environ`` (default: the process environment). No file, no network."""
    environ = os.environ if environ is None else environ
    if _read(environ, "AP_AI_OLLAMA_PROXY_URL") is not None:
        raise configuration_error("proxy_not_allowed_for_local", "AP_AI_OLLAMA_PROXY_URL cannot be used: Ollama is a loopback provider and is never reached through a proxy.")
    endpoint = _read(environ, "AP_AI_OLLAMA_ENDPOINT", required=True)
    origins = tuple(part.strip() for part in _read(environ, "AP_AI_OLLAMA_ALLOWED_ORIGINS", required=True).split(","))
    model_id = _read(environ, "AP_AI_OLLAMA_MODEL", required=True)
    mode_text = _read(environ, "AP_AI_OLLAMA_STRUCTURED_OUTPUT", "json_mode")
    if mode_text not in STRUCTURED_OUTPUT_MODES:
        raise configuration_error("invalid_ap_ai_ollama_structured_output", "Environment variable AP_AI_OLLAMA_STRUCTURED_OUTPUT must be one of: json_mode, native_schema.")
    max_input = _read_int(environ, "AP_AI_OLLAMA_MAX_INPUT_TOKENS", ollama.DEFAULT_MAX_INPUT_TOKENS, 1, ollama.CAPABILITY_MAX_INPUT_TOKENS)
    max_output = _read_int(environ, "AP_AI_OLLAMA_MAX_OUTPUT_TOKENS", ollama.DEFAULT_MAX_OUTPUT_TOKENS, 1, ollama.CAPABILITY_MAX_OUTPUT_TOKENS)
    max_retries = _read_int(environ, "AP_AI_OLLAMA_MAX_RETRIES", ollama.DEFAULT_MAX_RETRIES, 0, ollama.MAX_RETRIES_LIMIT)
    num_ctx = _read_int(environ, "AP_AI_OLLAMA_NUM_CTX", None, ollama.MIN_NUM_CTX, ollama.MAX_NUM_CTX)
    try:
        config = ollama.OllamaConfig(
            endpoint=endpoint,
            allowed_origins=origins,
            model_id=model_id,
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            max_retries=max_retries,
            num_ctx=num_ctx,
        )
    except AIGatewayError as error:
        # Keep the adapter's reason token (it names a field, never a value) and nothing else.
        reason = error.reason or "invalid_configuration"
        raise configuration_error(reason, f"The AP_AI_OLLAMA_* configuration is not valid ({reason}).") from None
    return OllamaSettings(
        config=config,
        timeout_seconds=float(_read_int(environ, "AP_AI_OLLAMA_TIMEOUT_SECONDS", int(ollama.DEFAULT_TIMEOUT_SECONDS), 1, 300)),
        structured_output=STRUCTURED_OUTPUT_MODES[mode_text],
    )


def build_ollama_transport() -> GuardedHttpTransport:
    """The real transport, with no proxy. Construction resolves and connects to nothing."""
    return GuardedHttpTransport(proxy_url=None, ca_bundle=None)


def build_ollama_profile(settings: OllamaSettings) -> ModelProfile:
    """The one immutable profile, approved for ``local_only`` (the strictest mode; every mode that is more permissive allows a local provider too)."""
    config = settings.config
    return ModelProfile(
        profile_id=PROFILE_ID,
        provider_id=ollama.PROVIDER_ID,
        model_id=config.model_id,
        policy_mode=PolicyMode.LOCAL_ONLY,
        timeout_seconds=settings.timeout_seconds,
        limits=TokenLimits(max_input_tokens=config.max_input_tokens, max_output_tokens=config.max_output_tokens),
        structured_output=StructuredOutputConfig(settings.structured_output),
    )


def build_ollama_gateway(environ=None, *, transport: HttpTransport | None = None) -> AIGateway:
    """A gateway whose only provider is the Ollama one. ``transport`` is for tests; by default the real, guarded one is built."""
    settings = load_ollama_settings(environ)
    if transport is None:
        transport = build_ollama_transport()
    provider = ollama.OllamaProvider(settings.config, transport)
    return AIGateway(ProviderRegistry.build([provider]), (build_ollama_profile(settings),))
