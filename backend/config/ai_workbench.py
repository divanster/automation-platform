"""AI-1b composition for the KPMG Workbench provider: environment -> validated settings -> provider, profiles and gateway.

OPT-IN. Nothing imports this module and ``config/ai_gateway.py`` (the AI-1a root, whose tuples stay EMPTY) does not use it. A caller that
wants the Workbench gateway calls ``build_workbench_gateway()`` explicitly. Importing this module reads no environment and makes no network
call; the environment is read, and the key validated, only when a ``load_`` or ``build_`` function runs. The Django settings do not hold
any of these values (a settings dump must never show the key).

FAIL CLOSED. A missing or invalid value raises ``AIGatewayError('configuration_error')`` naming the VARIABLE, never its value. Only the key
has no default. The path, the header name, the API version and the charge code default to the client-side contract reported from the FI
integration audit (see ``apps/ai_gateway/adapters/workbench.py``); the endpoint host is checked against an allowlist, so no value here can
send the key to another host. Nothing is read from a file here; for local development ``manage.py`` has already copied ``AP_*`` names from
the git-ignored ``.env`` into the process environment.

Variables (docs/development/runbook.md, section 5.5; docs/architecture/ai-gateway.md, section 14.3):

* required: ``AP_AI_WORKBENCH_API_KEY``;
* optional, with the verified client-side default: ``AP_AI_WORKBENCH_AUTH_HEADER`` (``Ocp-Apim-Subscription-Key``),
  ``AP_AI_WORKBENCH_CHAT_PATH``, ``AP_AI_WORKBENCH_ENDPOINT`` (allowlisted host only), ``AP_AI_WORKBENCH_API_VERSION``,
  ``AP_AI_WORKBENCH_CHARGE_CODE``, ``AP_AI_WORKBENCH_TIMEOUT_SECONDS`` (whole-call deadline, 90), ``AP_AI_WORKBENCH_MAX_RETRIES`` (2);
* optional, no default: ``AP_AI_WORKBENCH_PROXY_URL`` (explicit proxy, ``http://host:port``), ``AP_AI_WORKBENCH_CA_BUNDLE`` (extra CA file).
"""

import os
from dataclasses import dataclass, field

from apps.ai_gateway import AIGateway, ModelProfile, PolicyMode, ProviderRegistry, StructuredOutputConfig, StructuredOutputMode, TokenLimits
from apps.ai_gateway.adapters import workbench
from apps.ai_gateway.errors import configuration_error
from apps.ai_gateway.transport import HttpTransport

from config.ai_http_transport import StdlibHttpsTransport

PROFILE_GPT5 = "workbench-gpt5"  # the preferred profile
PROFILE_GPT41 = "workbench-gpt41"  # used only when a request names it: nothing ever switches to it

PROFILE_MAX_INPUT_TOKENS = 100_000  # provisional, unmeasured
PROFILE_MAX_OUTPUT_TOKENS = 16_000  # provisional; a reasoning model's hidden reasoning shares this budget


@dataclass(frozen=True)
class WorkbenchSettings:
    config: workbench.WorkbenchConfig
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


def load_workbench_settings(environ=None) -> WorkbenchSettings:
    """Read and validate the ``AP_AI_WORKBENCH_*`` variables of ``environ`` (default: the process environment). No file, no network."""
    environ = os.environ if environ is None else environ
    config = workbench.WorkbenchConfig(
        api_key=_read(environ, "AP_AI_WORKBENCH_API_KEY", required=True),
        auth_header=_read(environ, "AP_AI_WORKBENCH_AUTH_HEADER", workbench.DEFAULT_AUTH_HEADER),
        chat_path=_read(environ, "AP_AI_WORKBENCH_CHAT_PATH", workbench.DEFAULT_CHAT_PATH),
        endpoint=_read(environ, "AP_AI_WORKBENCH_ENDPOINT", workbench.DEFAULT_ENDPOINT),
        api_version=_read(environ, "AP_AI_WORKBENCH_API_VERSION", workbench.DEFAULT_API_VERSION),
        charge_code=_read(environ, "AP_AI_WORKBENCH_CHARGE_CODE", workbench.DEFAULT_CHARGE_CODE),
        max_retries=_read_int(environ, "AP_AI_WORKBENCH_MAX_RETRIES", workbench.DEFAULT_MAX_RETRIES, 0, workbench.MAX_RETRIES_LIMIT),
    )
    return WorkbenchSettings(
        config=config,
        timeout_seconds=float(_read_int(environ, "AP_AI_WORKBENCH_TIMEOUT_SECONDS", int(workbench.DEFAULT_TIMEOUT_SECONDS), 1, 300)),
        proxy_url=_read(environ, "AP_AI_WORKBENCH_PROXY_URL"),
        ca_bundle=_read(environ, "AP_AI_WORKBENCH_CA_BUNDLE"),
    )


def build_workbench_transport(settings: WorkbenchSettings) -> StdlibHttpsTransport:
    """The real transport. Construction reads a CA file at most; it connects to nothing."""
    return StdlibHttpsTransport(proxy_url=settings.proxy_url, ca_bundle=settings.ca_bundle)


def build_workbench_profiles(timeout_seconds: float) -> tuple:
    """Two immutable profiles, one per deployment. A request names one explicitly; there is no default and no fallback between them.

    Both are approved for ``company_endpoint``: ``local_only`` refuses them, ``company_endpoint`` and ``approved_cloud`` allow them. Both use
    ``json_mode`` (``response_format`` ``json_object``, the verified shape) and set no sampling parameter: the request carries no temperature."""
    def profile(profile_id, deployment):
        return ModelProfile(
            profile_id=profile_id,
            provider_id=workbench.PROVIDER_ID,
            model_id=deployment,
            policy_mode=PolicyMode.COMPANY_ENDPOINT,
            timeout_seconds=timeout_seconds,
            limits=TokenLimits(max_input_tokens=PROFILE_MAX_INPUT_TOKENS, max_output_tokens=PROFILE_MAX_OUTPUT_TOKENS),
            structured_output=StructuredOutputConfig(StructuredOutputMode.JSON_MODE),
        )

    return (profile(PROFILE_GPT5, workbench.DEPLOYMENT_GPT5), profile(PROFILE_GPT41, workbench.DEPLOYMENT_GPT41))


def build_workbench_gateway(environ=None, *, transport: HttpTransport | None = None) -> AIGateway:
    """A gateway whose only provider is the Workbench. ``transport`` is for tests; by default the real, TLS-verifying one is built."""
    settings = load_workbench_settings(environ)
    if transport is None:
        transport = build_workbench_transport(settings)
    provider = workbench.WorkbenchProvider(settings.config, transport)
    return AIGateway(ProviderRegistry.build([provider]), build_workbench_profiles(settings.timeout_seconds))
