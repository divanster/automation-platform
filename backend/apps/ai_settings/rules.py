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
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .errors import AISettingsValidationError

MODE_DETERMINISTIC = "deterministic"
MODE_AI = "ai"
MODES = (MODE_DETERMINISTIC, MODE_AI)

AI_ONLY_FIELDS = ("provider_id", "profile_id", "policy_mode", "timeout_seconds")
FIELDS = ("extraction_mode",) + AI_ONLY_FIELDS

# The closed set of provider-status issues the API may report. Nothing else (no message, no variable value) is ever put in a status.
ISSUE_MISSING_API_KEY = "missing_api_key"
ISSUE_INVALID_CONFIGURATION = "invalid_configuration"
PROVIDER_ISSUES = (ISSUE_MISSING_API_KEY, ISSUE_INVALID_CONFIGURATION)


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
class ExtractionCatalog:
    providers: tuple
    profiles: tuple
    policy_modes: tuple
    timeout_min: int
    timeout_max: int
    timeout_default: int
    lease_margin_seconds: int

    def provider(self, provider_id):
        return next((p for p in self.providers if p.provider_id == provider_id), None)

    def profile(self, profile_id):
        return next((p for p in self.profiles if p.profile_id == profile_id), None)

    def profiles_of(self, provider_id):
        return tuple(p for p in self.profiles if p.provider_id == provider_id)


@dataclass(frozen=True)
class ExtractionValues:
    extraction_mode: str = MODE_DETERMINISTIC
    provider_id: str | None = None
    profile_id: str | None = None
    policy_mode: str | None = None
    timeout_seconds: int | None = None

    def as_mapping(self) -> dict:
        return {name: getattr(self, name) for name in FIELDS}


DEFAULT_VALUES = ExtractionValues()


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 64


def validate_values(raw, catalog: ExtractionCatalog, *, lease_seconds: int | None = None) -> ExtractionValues:
    """The values ``raw`` describes, or ``AISettingsValidationError`` with ``{field: [codes]}``.

    ``lease_seconds`` (the API passes the web process's worker lease; the worker passes nothing because it checks its own lease at start-up) additionally
    refuses a timeout the lease could not cover, so an administrator cannot save a value that would stop the worker from starting.
    """
    if not isinstance(raw, Mapping):
        raise AISettingsValidationError({"_": ["invalid_type"]})
    errors: dict = {}

    def add(name, code):
        errors.setdefault(name, []).append(code)

    for name in raw:
        if name not in FIELDS:
            add(name if isinstance(name, str) and name.isidentifier() and len(name) <= 64 else "_unknown", "unknown_field")
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

    for name in ("provider_id", "profile_id", "policy_mode"):
        value = raw.get(name)
        if value is None:
            add(name, "required")
        elif not _is_text(value):
            add(name, "invalid_value")
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
    return ExtractionValues(MODE_AI, provider_id, profile_id, policy_mode, timeout)
