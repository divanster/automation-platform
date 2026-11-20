"""AI-SET-2 composition: a stored CONNECTION -> provider settings, profile and gateway, for the worker, and the status probe for the API.

A connection (``apps.ai_settings.rules.ConnectionValues``) names a provider TYPE from a static allowlist (the three below; no dynamic import, no plugin), a locality, for a
network provider an endpoint, an optional credential REFERENCE and the models it may call. This module turns the selected one into the SAME settings objects, adapters, guarded
transports and gateway the single-provider compositions (``config/ai_workbench.py``, ``config/ai_openai_compatible.py``, ``config/ai_ollama.py``) build, by running THEIR loaders on a
copy of the process environment in which the connection's own values replace the provider's single-connection variables. So there is one validation path and no weaker second
one: the adapters' endpoint, origin, locality and destination rules, the guarded transport's address rules and the gateway's policy rule apply to a stored connection exactly as to
an environment-configured one. Nothing here weakens them.

WHAT THE STORE DECIDES AND WHAT THE OPERATOR DECIDES. An administrator (the API) chooses: which connections exist, their type, locality, endpoint, model names and enabled flag, and
which credential reference each uses. The OPERATOR (the process environment) still decides what an endpoint may be: the approved-origin lists (``AP_AI_OPENAI_ALLOWED_ORIGINS``,
``AP_AI_OLLAMA_ALLOWED_ORIGINS``), the company CIDR ranges (``AP_AI_OPENAI_COMPANY_CIDRS``), a proxy and CA bundle, the Workbench endpoint allowlist, limits and retries. A stored endpoint
whose origin the operator has not approved is refused at start-up (and reported as ``endpoint_not_approved`` by the status probe); the stored settings can never widen an allowlist.
Those operator variables are shared by every connection of a type: a locality-specific subset is used (see ``_origins_for``), which can only NARROW the list.

CREDENTIAL REFERENCES. A reference is the NAME of a backend environment variable and must start with ``AP_AI_CRED_``, so it can never name ``AP_SECRET_KEY``, a database password or
another provider's key. The value is read here, in the backend process, at start-up (or by the status probe, which only tests that it can be read); it is placed in the adapter's
config object (hidden from ``repr``) and nowhere else: not in a ``ModelProfile``, not in the database, not in an error, a log line, an audit event or any response. A reference whose variable is
not set is an error, never an unauthenticated call. The Workbench keeps working without a reference: it then reads ``AP_AI_WORKBENCH_API_KEY`` exactly as before.

NO FALLBACK. The gateway built here has ONE provider and ONE profile: the selected connection's selected profile. There is no other endpoint, model, provider or locality to fall back to.
Importing this module reads no environment and makes no network call.
"""

import dataclasses
import os

from apps.ai_gateway import AIGateway, AIGatewayError, DataLocality, ModelProfile, PolicyContext, PolicyMode, ProviderRegistry, enforce_policy
from apps.ai_gateway.adapters import ollama, openai_compatible, workbench
from apps.ai_gateway.errors import configuration_error
from apps.ai_gateway.network import parse_networks, parse_url
from apps.ai_gateway.transport import HttpTransport
from apps.ai_settings.rules import (
    CREDENTIAL_NONE,
    CREDENTIAL_OPTIONAL,
    CREDENTIAL_REQUIRED_UNLESS_LOCAL,
    CREDENTIAL_SOURCE_MANAGED,
    ISSUE_ENDPOINT_NOT_APPROVED,
    ISSUE_INVALID_CONFIGURATION,
    ISSUE_MISSING_API_KEY,
    PROFILES_DECLARED,
    PROFILES_FIXED,
    ConnectionValues,
    ProviderStatus,
    ProviderTypeEntry,
    is_credential_ref,
)
from apps.secret_store import CODE_NOT_FOUND, SecretStoreError

from config import ai_ollama, ai_openai_compatible, ai_workbench
from config.secret_store import build_secret_store

TYPE_WORKBENCH = workbench.PROVIDER_ID
TYPE_OPENAI_COMPATIBLE = openai_compatible.PROVIDER_ID
TYPE_OLLAMA = ollama.PROVIDER_ID
PROVIDER_TYPES = (TYPE_WORKBENCH, TYPE_OPENAI_COMPATIBLE, TYPE_OLLAMA)  # the static allowlist: a fixed tuple, never read from settings or the database

# The least permissive policy mode that permits a locality: what a connection's profile is approved for (the same mapping the single-provider compositions use).
PROFILE_MODE_BY_LOCALITY = {DataLocality.LOCAL: PolicyMode.LOCAL_ONLY, DataLocality.COMPANY_ENDPOINT: PolicyMode.COMPANY_ENDPOINT, DataLocality.CLOUD: PolicyMode.APPROVED_CLOUD}

_PROBE_MODEL = "status-probe"  # a syntactically valid model name for a probe of a connection that declares no profile


def permitted_policy_modes(locality: DataLocality) -> tuple:
    """The policy modes under which the gateway would let a provider of ``locality`` run, asked of the gateway's own ``enforce_policy`` so the rule has one source.

    A cloud provider also has to be named in the policy's approved providers; selecting a cloud connection under ``approved_cloud`` is the explicit statement that does so
    (``policy_for``), so the probe names it here and only ``approved_cloud`` comes out."""
    probe = "probe"
    permitted = []
    for mode in PolicyMode:
        try:
            enforce_policy(PolicyContext(mode, frozenset({probe})), profile_mode=PROFILE_MODE_BY_LOCALITY[locality], provider_id=probe, locality=locality)
        except AIGatewayError:
            continue
        permitted.append(mode.value)
    return tuple(permitted)


def policy_for(connection: ConnectionValues, mode: PolicyMode) -> PolicyContext:
    """The policy for a call through ``connection`` under ``mode``. For a cloud connection (which validation allows only under ``approved_cloud``) the provider type is the
    approved cloud provider: choosing that connection and that mode is the explicit approval. Every other locality needs no approved provider."""
    approved = frozenset({connection.provider_type}) if connection.locality == DataLocality.CLOUD.value else frozenset()
    return PolicyContext(mode, approved)


def resolve_credential(reference, environ) -> str | None:
    """The value of the environment variable ``reference`` names, or ``None`` when there is no reference. Backend only: the value is never returned to a caller that logs or stores it.

    A reference that is not shaped ``AP_AI_CRED_*`` is refused again here (defence in depth: the rules refuse it at save time), and a variable that is not set is an error.
    The messages name no variable and no value."""
    if reference is None:
        return None
    if not is_credential_ref(reference):
        raise configuration_error("invalid_credential_reference", "The credential reference of the connection is not valid.")
    value = environ.get(reference, "").strip()
    if not value:
        raise configuration_error("missing_credential_reference", "The credential reference of the connection names a variable that is not set.")
    return value


def resolve_managed_credential(reference, store) -> str:
    """The secret stored under ``reference`` in the secret store ``store``. Backend only. The environment is NEVER consulted: a managed credential that is not stored, or whose store
    cannot answer (disabled, unavailable, busy, unreadable for this Windows identity), is an error, not a lookup anywhere else. The messages name no reference, path or value."""
    if not is_credential_ref(reference):
        raise configuration_error("invalid_credential_reference", "The credential reference of the connection is not valid.")
    try:
        return store.get(reference).reveal()
    except SecretStoreError as error:
        if error.code == CODE_NOT_FOUND:
            raise configuration_error("missing_credential_reference", "The managed credential of the connection is not stored.") from None
        raise configuration_error("credential_store_unavailable", "The secret store cannot supply the managed credential of the connection.") from None


def _managed_key(connection: ConnectionValues, env, secret_store):
    """The key of a MANAGED connection, from the secret store (``secret_store``, or the one this process is configured for)."""
    if connection.credential_ref is None:
        raise configuration_error("missing_credential_reference", "The managed credential of the connection has no reference.")
    try:
        store = build_secret_store(env) if secret_store is None else secret_store
    except SecretStoreError:
        raise configuration_error("credential_store_unavailable", "The secret store cannot supply the managed credential of the connection.") from None
    return resolve_managed_credential(connection.credential_ref, store)


def _origins_for(environ, name, locality: DataLocality, networks_name=None) -> str:
    """The operator's approved origins that suit ``locality``, as the comma-separated text the loader reads. It can only DROP an origin (one that is not valid for this locality);
    it never adds, rewrites or infers one. A malformed origin is still an error (the parser raises)."""
    text = environ.get(name, "").strip()
    if not text:
        return ""  # the loader reports it as missing
    networks = ()
    if networks_name is not None and locality is DataLocality.COMPANY_ENDPOINT:
        cidr_text = environ.get(networks_name, "").strip()
        networks = parse_networks(tuple(part.strip() for part in cidr_text.split(",")), field="company_cidrs") if cidr_text else ()
    kept = []
    for part in (piece.strip() for piece in text.split(",")):
        origin, _ = parse_url(part, field="allowed_origins", allow_path=False)
        if openai_compatible.origin_problem(origin, locality, networks) is None:
            kept.append(part)
    if not kept:
        raise configuration_error("openai_endpoint_not_approved", "No approved origin of the operator suits the connection's locality.")
    return ",".join(kept)


def connection_environ(connection: ConnectionValues, model_id: str, environ=None, *, secret_store=None) -> dict:
    """A COPY of the environment in which ``connection``'s values replace the provider's single-connection variables. The process environment is never modified.

    The key of a connection comes from ONE source, the one the connection names: an ``environment`` connection reads the variable its credential reference names and never the secret
    store; a ``managed`` one reads the secret store under that reference and never the environment (an ``AP_AI_OPENAI_API_KEY`` or ``AP_AI_WORKBENCH_API_KEY`` in the environment is
    replaced by the managed key, and a missing managed key is an error). For a
    local OpenAI-compatible connection the company CIDRs and the proxy are dropped (they do not apply to a loopback endpoint); for a company or cloud one the origins are
    reduced to those that suit its locality. The Ollama variables that exist (origins, limits, structured output) are the operator's, as is a refused proxy."""
    env = dict(os.environ if environ is None else environ)
    if connection.credential_source == CREDENTIAL_SOURCE_MANAGED:
        key = _managed_key(connection, env, secret_store)
    else:
        key = resolve_credential(connection.credential_ref, env)
    kind = connection.provider_type
    if kind == TYPE_WORKBENCH:
        if key is not None:
            env["AP_AI_WORKBENCH_API_KEY"] = key
    elif kind == TYPE_OPENAI_COMPATIBLE:
        try:
            locality = DataLocality(connection.locality)
        except ValueError:
            raise configuration_error("invalid_locality", "The connection's locality is not supported.") from None
        env["AP_AI_OPENAI_ENDPOINT"] = connection.endpoint or ""
        env["AP_AI_OPENAI_LOCALITY"] = connection.locality
        env["AP_AI_OPENAI_MODEL"] = model_id
        env["AP_AI_OPENAI_API_KEY"] = key or ""
        env["AP_AI_OPENAI_ALLOWED_ORIGINS"] = _origins_for(env, "AP_AI_OPENAI_ALLOWED_ORIGINS", locality, "AP_AI_OPENAI_COMPANY_CIDRS")
        if locality is not DataLocality.COMPANY_ENDPOINT:
            env["AP_AI_OPENAI_COMPANY_CIDRS"] = ""
        if locality is DataLocality.LOCAL:
            env["AP_AI_OPENAI_PROXY_URL"] = ""
    elif kind == TYPE_OLLAMA:
        env["AP_AI_OLLAMA_ENDPOINT"] = connection.endpoint or ""
        env["AP_AI_OLLAMA_MODEL"] = model_id
    else:
        raise configuration_error("unknown_provider_type", "The connection's provider type is not supported.")
    return env


def _load_settings(connection: ConnectionValues, model_id: str, environ, secret_store=None):
    env = connection_environ(connection, model_id, environ, secret_store=secret_store)
    if connection.provider_type == TYPE_WORKBENCH:
        return ai_workbench.load_workbench_settings(env)
    if connection.provider_type == TYPE_OPENAI_COMPATIBLE:
        return ai_openai_compatible.load_openai_compatible_settings(env)
    return ai_ollama.load_ollama_settings(env)


def build_connection_gateway(
    connection: ConnectionValues, profile_id: str, environ=None, *, transport: HttpTransport | None = None, secret_store=None, max_response_bytes: int | None = None
) -> AIGateway:
    """A gateway whose only provider is ``connection`` and whose only profile is ``profile_id``. Raises ``AIGatewayError('configuration_error')`` naming a field, never a value.
    Opens no connection and makes no network call; ``transport`` is for tests (otherwise the provider's own real, guarded transport is built). ``secret_store`` is for tests: a
    managed connection otherwise reads the store this process is configured for (``config/secret_store.py``). ``max_response_bytes`` (the connection test) can only LOWER the
    real transport's response cap; it does not apply to a ``transport`` that is passed in."""
    selected = connection.profile(profile_id)
    if selected is None:
        raise configuration_error("unknown_profile", "The connection has no such profile.")
    kind = connection.provider_type
    if kind == TYPE_WORKBENCH:
        env = connection_environ(connection, selected.model_id, environ, secret_store=secret_store)
        if transport is None and max_response_bytes is not None:
            transport = ai_workbench.build_workbench_transport(ai_workbench.load_workbench_settings(env), max_response_bytes=max_response_bytes)
        gateway = ai_workbench.build_workbench_gateway(env, transport=transport)
        if gateway.profile(profile_id).model_id != selected.model_id:
            raise configuration_error("profile_model_mismatch", "The profile's model is not the connection's.")
        return gateway
    settings = _load_settings(connection, selected.model_id, environ, secret_store)
    if kind == TYPE_OPENAI_COMPATIBLE:
        transport = ai_openai_compatible.build_openai_compatible_transport(settings, max_response_bytes=max_response_bytes) if transport is None else transport
        provider = openai_compatible.OpenAICompatibleProvider(settings.config, transport)
        profile = ai_openai_compatible.build_openai_compatible_profile(settings)
    else:
        transport = ai_ollama.build_ollama_transport(max_response_bytes=max_response_bytes) if transport is None else transport
        provider = ollama.OllamaProvider(settings.config, transport)
        profile = ai_ollama.build_ollama_profile(settings)
    return AIGateway(ProviderRegistry.build([provider]), (_named(profile, profile_id),))


def _named(profile: ModelProfile, profile_id: str) -> ModelProfile:
    return dataclasses.replace(profile, profile_id=profile_id)


def issue_of(error: AIGatewayError) -> str:
    """The fixed status issue (one of ``PROVIDER_ISSUES``) a configuration error stands for. Never a message or a value."""
    reason = error.reason or ""
    if "api_key" in reason or "credential" in reason:
        return ISSUE_MISSING_API_KEY if reason.startswith("missing_") else ISSUE_INVALID_CONFIGURATION
    if "endpoint_not_approved" in reason:
        return ISSUE_ENDPOINT_NOT_APPROVED
    return ISSUE_INVALID_CONFIGURATION


def connection_status(connection: ConnectionValues, environ=None, secret_store=None) -> ProviderStatus:
    """Whether ``connection`` is usable in THIS process: a fixed code, never a value, a name or a message. Reads the environment, or for a managed connection the secret store (the
    credential only to test that it can be read); makes no network call and builds no transport."""
    model_id = connection.profiles[0].model_id if connection.profiles else _PROBE_MODEL
    try:
        _load_settings(connection, model_id, environ, secret_store)
    except AIGatewayError as error:
        return ProviderStatus(False, issue_of(error))
    return ProviderStatus(True)


# --- the provider types ---------------------------------------------------------------------------------


def _check_openai_endpoint(endpoint: str, locality_text: str):
    """A fixed reason when ``endpoint`` cannot serve ``locality``: the adapter's own rule, pure. An internal address literal of a company endpoint can only be judged against the
    operator's CIDR ranges, which are not known here; that check is made at start-up and by the status probe."""
    try:
        locality = DataLocality(locality_text)
        origin, _ = parse_url(endpoint, field="endpoint")
    except (ValueError, AIGatewayError):
        return "invalid_endpoint"
    problem = openai_compatible.origin_problem(origin, locality, ())
    if problem == "destination_not_allowed" and locality is DataLocality.COMPANY_ENDPOINT:
        return None
    return problem


def _check_ollama_endpoint(endpoint: str, locality_text: str):
    try:
        origin, _ = parse_url(endpoint, field="endpoint")
    except AIGatewayError:
        return "invalid_endpoint"
    return ollama.origin_problem(origin)


def _no_endpoint(endpoint, locality):
    return "not_allowed"


def build_provider_types() -> tuple:
    """The provider types an administrator may use for a connection. Built from the adapters' own declarations; a constant tuple of three."""
    return (
        ProviderTypeEntry(
            TYPE_WORKBENCH, "KPMG Workbench", (DataLocality.COMPANY_ENDPOINT.value,), False, CREDENTIAL_OPTIONAL, PROFILES_FIXED, _no_endpoint, connection_status,
        ),
        ProviderTypeEntry(
            TYPE_OPENAI_COMPATIBLE, "OpenAI-compatible endpoint", tuple(locality.value for locality in DataLocality), True, CREDENTIAL_REQUIRED_UNLESS_LOCAL, PROFILES_DECLARED,
            _check_openai_endpoint, connection_status,
        ),
        ProviderTypeEntry(TYPE_OLLAMA, "Ollama (local)", (ollama.LOCALITY.value,), True, CREDENTIAL_NONE, PROFILES_DECLARED, _check_ollama_endpoint, connection_status),
    )  # fmt: skip
