"""The AI extraction settings as values and rules: pure Python, standard library only. No Django, no database, no environment, no gateway.

WHAT THE SETTINGS ARE. Which extractor Request Intake uses (``deterministic``, the default, or ``ai``) and, for ``ai``, the provider, the model profile, the
data policy and the call deadline. They are stored (``apps.ai_settings.models``) and read by an administrator's API and by the worker at start-up. They hold no
credential: a provider's key stays in the backend process environment (read by the provider's composition root) and never reaches this package.

ALL OR NOTHING. ``deterministic`` has no provider, profile, policy or timeout (all four must be ``None``); ``ai`` has all four. There is no fallback in either
direction: a value is accepted or refused, never replaced by a default or by another provider.

THE CATALOG. What a provider, a profile or a policy MAY be is not decided here: the composition root (``config/intake_extraction.py``) builds an
``ExtractionCatalog`` from the gateway's own declarations (the profiles Request Intake approves, the provider's locality and the gateway's locality rule) and
passes it in. This module only checks values against it, so the gateway's policy rule is not duplicated; the gateway still enforces it again on every call.

``validate_values`` is the single validation: the API runs it on what an administrator sends, and the worker runs it again on what it reads back from the
database (a row written by anything else, or by an older catalog, is refused at start-up instead of trusted). It reports field names and stable codes only,
never a value.

CONNECTIONS (AI-SET-2). Besides the legacy, environment-configured provider (``provider_id`` with no ``connection_id``, exactly as before), an administrator can
declare several NAMED CONNECTIONS: a connection is one explicit, statically allowlisted provider TYPE with its locality, (for a network provider) its endpoint, an
optional credential REFERENCE, the model profiles it exposes and an enabled flag. The settings then select ONE connection and ONE of its profiles. Nothing here
holds a credential: ``credential_ref`` is the NAME of a backend environment variable (``AP_AI_CRED_*``), never its value. Which provider types exist, which
localities they may declare and how an endpoint is judged is the composition root's decision (``ProviderTypeEntry``, built in ``config/ai_connections.py``);
this module only applies it. The structural checks (``validate_connections``) are pure; the endpoint-against-the-operator's-allowlist check is NOT made here (it
needs the process environment): the status probe reports it and the worker enforces it at start-up.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .errors import AISettingsValidationError

MODE_DETERMINISTIC = "deterministic"
MODE_AI = "ai"
MODES = (MODE_DETERMINISTIC, MODE_AI)

AI_ONLY_FIELDS = ("provider_id", "connection_id", "profile_id", "policy_mode", "timeout_seconds")
FIELDS = ("extraction_mode",) + AI_ONLY_FIELDS

# The closed set of provider-status issues the API may report. Nothing else (no message, no variable value) is ever put in a status.
ISSUE_MISSING_API_KEY = "missing_api_key"  # also: a connection's credential reference names a variable that is not set in this process
ISSUE_INVALID_CONFIGURATION = "invalid_configuration"
ISSUE_ENDPOINT_NOT_APPROVED = "endpoint_not_approved"  # the endpoint's origin is not in the operator's approved-origin list of this process
PROVIDER_ISSUES = (ISSUE_MISSING_API_KEY, ISSUE_INVALID_CONFIGURATION, ISSUE_ENDPOINT_NOT_APPROVED)

CONNECTION_FIELDS = ("connection_id", "display_name", "provider_type", "locality", "endpoint", "credential_ref", "enabled", "profiles")
PROFILE_FIELDS = ("profile_id", "model_id")
MAX_CONNECTIONS = 10
MAX_PROFILES_PER_CONNECTION = 10
MAX_ENDPOINT_LENGTH = 512
CREDENTIAL_REF_PREFIX = "AP_AI_CRED_"  # a reference may name only a variable with this prefix, so it can never name DJANGO_SECRET_KEY, a database password or another provider's key

_CONNECTION_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}")
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")  # the gateway's own model identifier pattern
_CREDENTIAL_REF = re.compile(rf"{CREDENTIAL_REF_PREFIX}[A-Z0-9_]{{1,48}}")
_DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f\s](?:[^\x00-\x1f\x7f]{0,78}[^\x00-\x1f\x7f\s])?")  # printable, no leading or trailing space, 1 to 80 characters
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

CREDENTIAL_NONE = "none"  # the provider type takes no credential reference
CREDENTIAL_OPTIONAL = "optional"
CREDENTIAL_REQUIRED_UNLESS_LOCAL = "required_unless_local"
PROFILES_FIXED = "fixed"  # the profiles are the catalog's (``catalog.profiles_of``): the administrator can only pick among them
PROFILES_DECLARED = "declared"  # the administrator names the model identifiers the connection may call


@dataclass(frozen=True)
class ProviderStatus:
    """Whether the provider's own configuration is usable in THIS process. ``issue`` is one of ``PROVIDER_ISSUES`` or ``None``. Never a value."""

    configured: bool
    issue: str | None = None


@dataclass(frozen=True)
class ProfileEntry:
    profile_id: str
    provider_id: str
    model_id: str
    permitted_policy_modes: tuple  # the policy modes under which the gateway would let this profile's provider run


@dataclass(frozen=True)
class ProviderEntry:
    provider_id: str
    label: str
    locality: str
    status: Callable[[], ProviderStatus]  # called per API read, never at import; reads the environment of the calling process only


@dataclass(frozen=True)
class ProviderTypeEntry:
    """A statically allowlisted provider TYPE that a connection may use. Built by the composition root from the gateway's own declarations; this module never
    imports a provider. There is no way to add one at run time."""

    provider_type: str
    label: str
    localities: tuple  # the localities a connection of this type may declare
    needs_endpoint: bool
    credential: str  # CREDENTIAL_NONE, CREDENTIAL_OPTIONAL or CREDENTIAL_REQUIRED_UNLESS_LOCAL
    profile_source: str  # PROFILES_FIXED or PROFILES_DECLARED
    check_endpoint: Callable[[str, str], str | None]  # (endpoint, locality) -> a fixed reason code, or None; pure: no environment, no network
    status: Callable[["ConnectionValues"], ProviderStatus]  # called per API read, never at import; reads the environment of the calling process only


@dataclass(frozen=True)
class ExtractionCatalog:
    providers: tuple
    profiles: tuple
    policy_modes: tuple
    timeout_min: int
    timeout_max: int
    timeout_default: int
    lease_margin_seconds: int
    provider_types: tuple = ()  # ``ProviderTypeEntry``; empty: no connection can be declared
    locality_policies: tuple = ()  # ``((locality, (policy modes the gateway permits for it, ...)), ...)``

    def provider(self, provider_id):
        return next((p for p in self.providers if p.provider_id == provider_id), None)

    def profile(self, profile_id):
        return next((p for p in self.profiles if p.profile_id == profile_id), None)

    def profiles_of(self, provider_id):
        return tuple(p for p in self.profiles if p.provider_id == provider_id)

    def provider_type(self, provider_type):
        return next((p for p in self.provider_types if p.provider_type == provider_type), None)

    def policy_modes_for(self, locality) -> tuple:
        return next((modes for name, modes in self.locality_policies if name == locality), ())


@dataclass(frozen=True)
class ProfileValues:
    profile_id: str
    model_id: str


@dataclass(frozen=True)
class ConnectionValues:
    """One named connection as stored. No field holds a credential: ``credential_ref`` is the NAME of a backend environment variable."""

    connection_id: str
    display_name: str
    provider_type: str
    locality: str
    endpoint: str | None
    credential_ref: str | None
    enabled: bool
    profiles: tuple

    def as_mapping(self) -> dict:
        return {
            "connection_id": self.connection_id,
            "display_name": self.display_name,
            "provider_type": self.provider_type,
            "locality": self.locality,
            "endpoint": self.endpoint,
            "credential_ref": self.credential_ref,
            "enabled": self.enabled,
            "profiles": [{"profile_id": p.profile_id, "model_id": p.model_id} for p in self.profiles],
        }

    def profile(self, profile_id):
        return next((p for p in self.profiles if p.profile_id == profile_id), None)


@dataclass(frozen=True)
class ExtractionValues:
    extraction_mode: str = MODE_DETERMINISTIC
    provider_id: str | None = None
    profile_id: str | None = None
    policy_mode: str | None = None
    timeout_seconds: int | None = None
    connection_id: str | None = None  # None: the legacy, environment-configured provider (``provider_id`` names it); else a stored connection, and ``provider_id`` is its type

    def as_mapping(self) -> dict:
        return {name: getattr(self, name) for name in FIELDS}


DEFAULT_VALUES = ExtractionValues()


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 64


def _safe_name(name) -> str:
    return name if isinstance(name, str) and _FIELD_NAME.fullmatch(name) else "_unknown"


def is_credential_ref(value) -> bool:
    """Whether ``value`` has the shape of a credential reference: ``AP_AI_CRED_`` and upper-case letters, digits and underscores. Used again where a reference is resolved."""
    return isinstance(value, str) and _CREDENTIAL_REF.fullmatch(value) is not None


def _structure(raw):
    """``(ConnectionValues | None, {relative field: [codes]})``: the types and shapes of one connection, with no catalog. The values are never in an error."""
    if not isinstance(raw, Mapping):
        return None, {"_": ["invalid_type"]}
    errors: dict = {}

    def add(name, code):
        errors.setdefault(name, []).append(code)

    for name in raw:
        if name not in CONNECTION_FIELDS:
            add(_safe_name(name), "unknown_field")

    def text(name, pattern, *, required=True):
        value = raw.get(name)
        if value is None:
            if required:
                add(name, "required")
            return None
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            add(name, "invalid_value")
            return None
        return value

    connection_id = text("connection_id", _CONNECTION_ID)
    display_name = text("display_name", _DISPLAY_NAME)
    provider_type = text("provider_type", _CODE)
    locality = text("locality", _CODE)
    endpoint = raw.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH):
        add("endpoint", "invalid_value")
        endpoint = None
    credential_ref = text("credential_ref", _CREDENTIAL_REF, required=False)
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        add("enabled", "required" if enabled is None else "invalid_value")

    profiles = []
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, (list, tuple)):
        add("profiles", "required" if raw_profiles is None else "invalid_type")
    elif len(raw_profiles) > MAX_PROFILES_PER_CONNECTION:
        add("profiles", "too_many")
    else:
        seen = set()
        for index, item in enumerate(raw_profiles):
            prefix = f"profiles[{index}]"
            if not isinstance(item, Mapping):
                add(prefix, "invalid_type")
                continue
            for name in item:
                if name not in PROFILE_FIELDS:
                    add(f"{prefix}.{_safe_name(name)}", "unknown_field")
            profile_id, model_id = item.get("profile_id"), item.get("model_id")
            ok = True
            for name, value, pattern in (("profile_id", profile_id, _PROFILE_ID), ("model_id", model_id, _MODEL_ID)):
                if value is None:
                    add(f"{prefix}.{name}", "required")
                    ok = False
                elif not isinstance(value, str) or pattern.fullmatch(value) is None:
                    add(f"{prefix}.{name}", "invalid_value")
                    ok = False
            if ok:
                if profile_id in seen:
                    add(f"{prefix}.profile_id", "duplicate_profile_id")
                seen.add(profile_id)
                profiles.append(ProfileValues(profile_id, model_id))
    if errors:
        return None, errors
    return ConnectionValues(connection_id, display_name, provider_type, locality, endpoint, credential_ref, enabled, tuple(profiles)), {}


def parse_stored_connection(raw) -> ConnectionValues | None:
    """The connection a stored item describes if it has the right STRUCTURE, else ``None``. No catalog: used where a damaged item must not break a read."""
    connection, errors = _structure(raw)
    return None if errors else connection


def connection_problems(connection: ConnectionValues, catalog: ExtractionCatalog) -> dict:
    """``{relative field: [codes]}`` for what the catalog refuses in a structurally valid connection; empty when acceptable. Pure: no environment, no network."""
    errors: dict = {}

    def add(name, code):
        errors.setdefault(name, []).append(code)

    entry = catalog.provider_type(connection.provider_type)
    if entry is None:
        add("provider_type", "unknown_provider_type")
        return errors
    locality_ok = connection.locality in entry.localities
    if not locality_ok:
        add("locality", "locality_not_allowed_for_provider")
    if not entry.needs_endpoint:
        if connection.endpoint is not None:
            add("endpoint", "not_allowed")
    elif connection.endpoint is None:
        add("endpoint", "required")
    elif locality_ok:  # an endpoint is judged against a locality that is acceptable for the type
        code = entry.check_endpoint(connection.endpoint, connection.locality)
        if code is not None:
            add("endpoint", code if isinstance(code, str) and _CODE.fullmatch(code) else "invalid_endpoint")
    if entry.credential == CREDENTIAL_NONE:
        if connection.credential_ref is not None:
            add("credential_ref", "not_allowed")
    elif entry.credential == CREDENTIAL_REQUIRED_UNLESS_LOCAL and connection.credential_ref is None and connection.locality != "local":
        add("credential_ref", "required")
    if not connection.profiles:
        add("profiles", "required")
    for index, profile in enumerate(connection.profiles):
        if entry.profile_source == PROFILES_FIXED:
            known = catalog.profile(profile.profile_id)
            if known is None or known.provider_id != connection.provider_type:
                add(f"profiles[{index}].profile_id", "unknown_profile")
            elif known.model_id != profile.model_id:
                add(f"profiles[{index}].model_id", "model_not_of_profile")
    return errors


def validate_connections(raw, catalog: ExtractionCatalog) -> tuple:
    """The connections ``raw`` (a list of objects) describes, each checked structurally and against the catalog, or ``AISettingsValidationError`` with
    ``{"connections[i].field": [codes]}``. The list REPLACES the stored one as a whole."""
    if not isinstance(raw, (list, tuple)):
        raise AISettingsValidationError({"connections": ["invalid_type"]})
    if len(raw) > MAX_CONNECTIONS:
        raise AISettingsValidationError({"connections": ["too_many"]})
    errors: dict = {}
    connections = []
    seen = set()
    for index, item in enumerate(raw):
        connection, problems = _structure(item)
        if connection is not None:
            problems = connection_problems(connection, catalog)
            if connection.connection_id in seen:
                problems.setdefault("connection_id", []).append("duplicate_connection_id")
            seen.add(connection.connection_id)
            connections.append(connection)
        for name, codes in problems.items():
            errors[f"connections[{index}]" if name == "_" else f"connections[{index}].{name}"] = list(codes)
    if errors:
        raise AISettingsValidationError(errors)
    return tuple(connections)


def validate_values(raw, catalog: ExtractionCatalog, *, lease_seconds: int | None = None, connections: tuple = ()) -> ExtractionValues:
    """The values ``raw`` describes, or ``AISettingsValidationError`` with ``{field: [codes]}``.

    ``lease_seconds`` (the API passes the web process's worker lease; the worker passes nothing because it checks its own lease at start-up) additionally
    refuses a timeout the lease could not cover, so an administrator cannot save a value that would stop the worker from starting.

    ``connections`` are the connections that will be in force (``ConnectionValues``). A selected connection must be among them, must be enabled and is
    checked against the catalog again (``invalid_connection``); the others are not examined here.
    """
    if not isinstance(raw, Mapping):
        raise AISettingsValidationError({"_": ["invalid_type"]})
    errors: dict = {}

    def add(name, code):
        errors.setdefault(name, []).append(code)

    for name in raw:
        if name not in FIELDS:
            add(_safe_name(name), "unknown_field")
    mode = raw.get("extraction_mode")
    if not isinstance(mode, str) or mode not in MODES:
        add("extraction_mode", "invalid_choice")
        raise AISettingsValidationError(errors)

    if mode == MODE_DETERMINISTIC:
        for name in AI_ONLY_FIELDS:
            if raw.get(name) is not None:
                add(name, "not_allowed_in_deterministic_mode")
        if errors:
            raise AISettingsValidationError(errors)
        return DEFAULT_VALUES

    connection_id = raw.get("connection_id")
    for name in ("provider_id", "profile_id", "policy_mode"):
        value = raw.get(name)
        if value is None:
            if name != "provider_id" or connection_id is None:  # with a connection, the provider type is the connection's
                add(name, "required")
        elif not _is_text(value):
            add(name, "invalid_value")
    if connection_id is not None and not _is_text(connection_id):
        add("connection_id", "invalid_value")
    timeout = raw.get("timeout_seconds")
    timeout_ok = False
    if timeout is None:
        add("timeout_seconds", "required")
    elif isinstance(timeout, bool) or not isinstance(timeout, int):
        add("timeout_seconds", "invalid_value")
    elif not catalog.timeout_min <= timeout <= catalog.timeout_max:
        add("timeout_seconds", "out_of_range")
    else:
        timeout_ok = True
        if lease_seconds is not None and timeout + catalog.lease_margin_seconds > lease_seconds:
            add("timeout_seconds", "exceeds_worker_lease")

    provider_id, profile_id, policy_mode = (raw.get(name) for name in ("provider_id", "profile_id", "policy_mode"))
    if connection_id is not None:
        provider_id = _check_connection_selection(connection_id, provider_id, profile_id, policy_mode, catalog, connections, add)
    else:
        provider = catalog.provider(provider_id) if _is_text(provider_id) else None
        if _is_text(provider_id) and provider is None:
            add("provider_id", "unknown_provider")
        profile = catalog.profile(profile_id) if _is_text(profile_id) else None
        if _is_text(profile_id):
            if profile is None:
                add("profile_id", "unknown_profile")
            elif provider is not None and profile.provider_id != provider.provider_id:
                add("profile_id", "profile_not_of_provider")
        if _is_text(policy_mode):
            if policy_mode not in catalog.policy_modes:
                add("policy_mode", "invalid_choice")
            elif profile is not None and policy_mode not in profile.permitted_policy_modes:
                add("policy_mode", "policy_forbids_provider")  # e.g. local_only with a company endpoint: refused here as well as by the gateway on every call

    if errors or not timeout_ok:
        raise AISettingsValidationError(errors)
    return ExtractionValues(MODE_AI, provider_id, profile_id, policy_mode, timeout, connection_id)


def _check_connection_selection(connection_id, provider_id, profile_id, policy_mode, catalog, connections, add):
    """Check a selection by connection; returns the provider type (the selected connection's) for the values, or the given ``provider_id`` when unusable."""
    if not _is_text(connection_id):
        return provider_id
    connection = next((c for c in connections if c.connection_id == connection_id), None)
    if connection is None:
        add("connection_id", "unknown_connection")
        return provider_id
    if not connection.enabled:
        add("connection_id", "connection_disabled")
        return provider_id
    if connection_problems(connection, catalog):
        add("connection_id", "invalid_connection")
        return provider_id
    if provider_id is not None and provider_id != connection.provider_type:
        add("provider_id", "provider_not_of_connection")
    if _is_text(profile_id) and connection.profile(profile_id) is None:
        add("profile_id", "unknown_profile")
    if _is_text(policy_mode):
        if policy_mode not in catalog.policy_modes:
            add("policy_mode", "invalid_choice")
        elif policy_mode not in catalog.policy_modes_for(connection.locality):
            add("policy_mode", "policy_forbids_provider")  # the same gateway rule as for the legacy provider: local_only never reaches a company or cloud connection
    return connection.provider_type
