"""AI-CON-1 composition for the OpenAI-compatible provider: environment -> validated settings -> provider, profile and gateway.

OPT-IN, LIKE THE WORKBENCH. Nothing imports this module, ``config/ai_gateway.py`` (whose tuples stay EMPTY) does not use it, and Request Intake's extraction and the
AI settings catalog still know the Workbench only. A caller that wants this gateway calls ``build_openai_compatible_gateway()`` explicitly. Importing this module reads
no environment and makes no network call; the environment is read, and every value validated, only when a ``load_`` or ``build_`` function runs. The Django settings
hold none of these values, and no value is stored in a database or in a ``ModelProfile``.

FAIL CLOSED. A missing or invalid value raises ``AIGatewayError('configuration_error')`` naming the VARIABLE, never its value. There is NO default endpoint, locality,
model or approved origin: an installation that sets nothing has no OpenAI-compatible provider. The key is the only secret (it never appears in a ``repr``, an error or a
log line) and is required unless the locality is ``local``.

Variables (docs/development/runbook.md, section 5.8; docs/architecture/ai-gateway.md, section 17):

* required: ``AP_AI_OPENAI_ENDPOINT`` (the base URL; ``/chat/completions`` is appended), ``AP_AI_OPENAI_ALLOWED_ORIGINS`` (comma-separated ``scheme://host[:port]``; the
  endpoint's origin must be listed), ``AP_AI_OPENAI_LOCALITY`` (``local``, ``company_endpoint`` or ``cloud``), ``AP_AI_OPENAI_MODEL``;
* required when the locality is ``company_endpoint`` (and refused for the others): ``AP_AI_OPENAI_COMPANY_CIDRS``, comma-separated CIDR ranges (``10.20.0.0/16``) the
  endpoint may resolve to; every resolved address must be inside one, and no range may include a denied special-purpose range;
* required unless the locality is ``local``: ``AP_AI_OPENAI_API_KEY``;
* optional: ``AP_AI_OPENAI_TIMEOUT_SECONDS`` (whole-call deadline, 90), ``AP_AI_OPENAI_MAX_RETRIES`` (2), ``AP_AI_OPENAI_MAX_INPUT_TOKENS`` (32000),
  ``AP_AI_OPENAI_MAX_OUTPUT_TOKENS`` (4096), ``AP_AI_OPENAI_PROXY_URL`` (explicit proxy, ``http://host:port``; refused for a ``local`` endpoint and for ANY endpoint that
  is a host name, so it can only be used with an IP-literal endpoint), ``AP_AI_OPENAI_CA_BUNDLE`` (extra CA file).
"""

import os
from dataclasses import dataclass, field

from apps.ai_gateway import AIGateway, AIGatewayError, DataLocality, ModelProfile, PolicyMode, ProviderRegistry, StructuredOutputConfig, StructuredOutputMode, TokenLimits
from apps.ai_gateway.adapters import openai_compatible
from apps.ai_gateway.errors import configuration_error
from apps.ai_gateway.network import parse_ip
from apps.ai_gateway.transport import HttpTransport

from config.ai_guarded_transport import GuardedHttpTransport

PROFILE_ID = "openai-compatible"  # the one profile; there is no second one to fall back to

_POLICY_MODE_BY_LOCALITY = {DataLocality.LOCAL: PolicyMode.LOCAL_ONLY, DataLocality.COMPANY_ENDPOINT: PolicyMode.COMPANY_ENDPOINT, DataLocality.CLOUD: PolicyMode.APPROVED_CLOUD}


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    config: openai_compatible.OpenAICompatibleConfig
    timeout_seconds: float
    proxy_url: str | None = field(default=None, repr=False)  # may carry proxy credentials
    ca_bundle: str | None = None


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


def load_openai_compatible_settings(environ=None) -> OpenAICompatibleSettings:
    """Read and validate the ``AP_AI_OPENAI_*`` variables of ``environ`` (default: the process environment). No file, no network."""
    environ = os.environ if environ is None else environ
    locality_text = _read(environ, "AP_AI_OPENAI_LOCALITY", required=True)
    try:
        locality = DataLocality(locality_text)
    except ValueError:
        raise configuration_error("invalid_ap_ai_openai_locality", "Environment variable AP_AI_OPENAI_LOCALITY must be one of: local, company_endpoint, cloud.") from None
    origins = tuple(part.strip() for part in _read(environ, "AP_AI_OPENAI_ALLOWED_ORIGINS", required=True).split(","))
    proxy_url = _read(environ, "AP_AI_OPENAI_PROXY_URL")
    if proxy_url is not None and locality is DataLocality.LOCAL:
        raise configuration_error("proxy_not_allowed_for_local", "AP_AI_OPENAI_PROXY_URL cannot be used with a local endpoint.")
    endpoint = _read(environ, "AP_AI_OPENAI_ENDPOINT", required=True)
    model_id = _read(environ, "AP_AI_OPENAI_MODEL", required=True)
    cidr_text = _read(environ, "AP_AI_OPENAI_COMPANY_CIDRS", required=locality is DataLocality.COMPANY_ENDPOINT)
    approved_networks = () if cidr_text is None else tuple(part.strip() for part in cidr_text.split(","))
    max_input = _read_int(environ, "AP_AI_OPENAI_MAX_INPUT_TOKENS", openai_compatible.DEFAULT_MAX_INPUT_TOKENS, 1, openai_compatible.CAPABILITY_MAX_INPUT_TOKENS)
    max_output = _read_int(environ, "AP_AI_OPENAI_MAX_OUTPUT_TOKENS", openai_compatible.DEFAULT_MAX_OUTPUT_TOKENS, 1, openai_compatible.CAPABILITY_MAX_OUTPUT_TOKENS)
    max_retries = _read_int(environ, "AP_AI_OPENAI_MAX_RETRIES", openai_compatible.DEFAULT_MAX_RETRIES, 0, openai_compatible.MAX_RETRIES_LIMIT)
    try:
        config = openai_compatible.OpenAICompatibleConfig(
            endpoint=endpoint,
            allowed_origins=origins,
            locality=locality,
            model_id=model_id,
            api_key=_read(environ, "AP_AI_OPENAI_API_KEY"),
            approved_networks=approved_networks,
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            max_retries=max_retries,
        )
    except AIGatewayError as error:
        # Keep the adapter's reason token (it names a field, never a value) and nothing else.
        reason = error.reason or "invalid_configuration"
        raise configuration_error(reason, f"The AP_AI_OPENAI_* configuration is not valid ({reason}).") from None
    if proxy_url is not None and parse_ip(config.origin.host) is None:
        # A proxy resolves host names itself, where no destination-address rule can be applied: refused, with no acknowledgement or other bypass.
        raise configuration_error("proxy_not_allowed_for_hostname_endpoint", "AP_AI_OPENAI_PROXY_URL cannot be used with a host-name endpoint; use a direct connection or an IP-literal endpoint.")
    return OpenAICompatibleSettings(
        config=config,
        timeout_seconds=float(_read_int(environ, "AP_AI_OPENAI_TIMEOUT_SECONDS", int(openai_compatible.DEFAULT_TIMEOUT_SECONDS), 1, 300)),
        proxy_url=proxy_url,
        ca_bundle=_read(environ, "AP_AI_OPENAI_CA_BUNDLE"),
    )


def build_openai_compatible_transport(settings: OpenAICompatibleSettings, *, max_response_bytes=None) -> GuardedHttpTransport:
    """The real transport. Construction reads a CA file at most; it resolves and connects to nothing. ``max_response_bytes`` can only LOWER the transport's response cap."""
    return GuardedHttpTransport(proxy_url=settings.proxy_url, ca_bundle=settings.ca_bundle, **({} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}))


def build_openai_compatible_profile(settings: OpenAICompatibleSettings) -> ModelProfile:
    """The one immutable profile. Its policy mode is the one its declared locality needs: a ``local`` profile is approved for ``local_only``, a ``company_endpoint``
    profile for ``company_endpoint`` and a ``cloud`` profile for ``approved_cloud`` (where the provider must also be named in the policy's approved providers)."""
    config = settings.config
    return ModelProfile(
        profile_id=PROFILE_ID,
        provider_id=openai_compatible.PROVIDER_ID,
        model_id=config.model_id,
        policy_mode=_POLICY_MODE_BY_LOCALITY[config.locality],
        timeout_seconds=settings.timeout_seconds,
        limits=TokenLimits(max_input_tokens=config.max_input_tokens, max_output_tokens=config.max_output_tokens),
        structured_output=StructuredOutputConfig(StructuredOutputMode.JSON_MODE),
    )


def build_openai_compatible_gateway(environ=None, *, transport: HttpTransport | None = None) -> AIGateway:
    """A gateway whose only provider is the OpenAI-compatible one. ``transport`` is for tests; by default the real, guarded one is built."""
    settings = load_openai_compatible_settings(environ)
    if transport is None:
        transport = build_openai_compatible_transport(settings)
    provider = openai_compatible.OpenAICompatibleProvider(settings.config, transport)
    return AIGateway(ProviderRegistry.build([provider]), (build_openai_compatible_profile(settings),))
