"""AI-2a composition: which extractor Request Intake uses, decided explicitly and failing closed. Reads the process environment only when a function runs.

TWO MODES, NO AUTOMATIC SWITCH. ``deterministic`` (the default, and the behaviour of every installation that sets nothing) leaves the worker on the fixture
parser. ``ai`` must be asked for by name. There is no fallback from either to the other: with ``ai`` a failure is a failure, and an unset or invalid value
of any setting below stops the worker at start-up (``runworker``) instead of choosing a default. This module itself touches no database; the Workbench key is
read by ``config/ai_workbench.py`` from the environment, as in AI-1b, and is never stored.

STORED SETTINGS (AI settings API). ``build_extractor_source(stored=...)`` takes the mode, profile, data policy and timeout from the row an administrator saved
through ``/api/ai-settings/extraction/`` (read by ``config/task_registry.prepare_worker`` ONCE, at worker start-up; a running worker never re-reads it). When a
row exists it takes precedence over the three ``AP_INTAKE_*`` / ``AP_AI_POLICY_MODE`` variables below, which are then not read; with no row the variables
decide as before. It is the precedence of two configuration sources, not a provider fallback, and an invalid row is an error, never replaced by the
environment. ``build_catalog`` is what the API offers an administrator, derived from the gateway's declarations.

STORED CONNECTIONS (AI-SET-2). A stored row may select a NAMED CONNECTION (``connection_id``) and one of its profiles instead of the legacy, environment-configured Workbench. The worker
then builds the gateway for that connection alone (``config/ai_connections.py``), after checking that it exists in the stored list, is enabled and still passes the catalog; a missing,
disabled or invalid connection, an unset credential reference or an endpoint the operator has not approved stops the worker at start-up. A row with no ``connection_id`` behaves exactly as
before. There is no fallback from one connection, provider, model or locality to another, and none to the environment-configured Workbench or to the deterministic parser.

Variables (docs/development/runbook.md, section 5.6):

* ``AP_INTAKE_EXTRACTION_MODE``: ``deterministic`` (default when unset or empty) or ``ai``. Anything else is refused.
* ``AP_AI_POLICY_MODE``: REQUIRED in ``ai`` mode, no default: ``local_only``, ``company_endpoint`` or ``approved_cloud``. The Workbench is a
  ``company_endpoint`` provider, so ``local_only`` is refused here at start-up (it would fail every call); the gateway enforces the mode on every call as well.
  This is the explicit statement that this installation permits sending request text to a company endpoint. There is no stored company policy yet.
* ``AP_INTAKE_AI_TIMEOUT_SECONDS``: optional, 5 to 300, default 45: the deadline of one AI call, retries inside the provider included. It can only LOWER the
  profile's own timeout (``AP_AI_WORKBENCH_TIMEOUT_SECONDS``, default 90); the lower of the two is used.
* ``AP_AI_WORKBENCH_*``: the provider's own settings (``config/ai_workbench.py``). Only ``AP_AI_WORKBENCH_API_KEY`` is required.

THE LEASE. The worker has no heartbeat, so an attempt must finish within its lease (``AP_WORKER_LEASE_SECONDS``, default 60). A real model call can take
longer than the deterministic parser, so in ``ai`` mode the worker refuses to start unless the lease is at least the call deadline plus
``LEASE_MARGIN_SECONDS`` (the two short transactions and the recording of the result). With the defaults, 60 >= 45 + 15.

THE PROFILE. ``APPROVED_INTAKE_AI_PROFILES`` is the allowlist of profiles Request Intake may use; the one used is ``INTAKE_AI_PROFILE_ID``
(``workbench-gpt5``, the one the owner smoke-tested). ``workbench-gpt41`` is not approved for this workflow; approving it is a reviewed edit here. There is
no environment variable that selects a profile and nothing ever switches from one to another.
"""

import os
from dataclasses import dataclass

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyContext, PolicyMode, enforce_policy
from apps.ai_gateway.transport import HttpTransport
from apps.ai_settings.errors import AISettingsValidationError
from apps.ai_settings.rules import (
    ISSUE_INVALID_CONFIGURATION,
    ISSUE_MISSING_API_KEY,
    ConnectionValues,
    ExtractionCatalog,
    ProfileEntry,
    ProviderEntry,
    ProviderStatus,
    validate_values,
)
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import AIExtractionError, ExtractionConfigurationError
from apps.intake.policy import KIND_EXTRACT

from config import ai_connections, ai_workbench

MODE_DETERMINISTIC = "deterministic"
MODE_AI = "ai"
MODES = (MODE_DETERMINISTIC, MODE_AI)

ENV_MODE = "AP_INTAKE_EXTRACTION_MODE"
ENV_POLICY_MODE = "AP_AI_POLICY_MODE"
ENV_TIMEOUT_SECONDS = "AP_INTAKE_AI_TIMEOUT_SECONDS"

INTAKE_AI_PROFILE_ID = ai_workbench.PROFILE_GPT5
APPROVED_INTAKE_AI_PROFILES = frozenset({ai_workbench.PROFILE_GPT5})

DEFAULT_TIMEOUT_SECONDS = 45
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300
LEASE_MARGIN_SECONDS = 15  # provisional, not measured: two short transactions and the recording of the task result


@dataclass(frozen=True)
class IntakeExtractionSettings:
    mode: str
    policy_mode: PolicyMode | None = None  # set only in ai mode
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    profile_id: str | None = None  # set only for stored settings in ai mode; the environment path uses INTAKE_AI_PROFILE_ID
    stored_revision: int | None = None  # set only when the settings came from the stored row (AI settings API)
    connection: ConnectionValues | None = None  # set only when the stored row selects a connection; None: the legacy, environment-configured Workbench


def _read(environ, name):
    return environ.get(name, "").strip() or None  # an empty value counts as unset, the convention of config/env.py


def build_catalog() -> ExtractionCatalog:
    """What an administrator may choose, built from the gateway's own declarations. Pure: reads no environment and no database, calls nothing.

    Providers: the environment-configured Workbench (the legacy path). Profiles: the Workbench profiles that ``APPROVED_INTAKE_AI_PROFILES`` approves for Request Intake. A profile's permitted
    policy modes are not listed by hand: the gateway's own ``enforce_policy`` is asked, so the locality rule has one source. The gateway still enforces
    it on every call and ``build_extractor_source`` again at start-up. ``provider_types`` and ``locality_policies`` describe what a stored CONNECTION may be (three static types and, per
    locality, the policy modes the gateway permits; ``config/ai_connections.py``).
    """
    provider_id = ai_workbench.workbench.PROVIDER_ID
    profiles = []
    for profile in ai_workbench.build_workbench_profiles(float(DEFAULT_TIMEOUT_SECONDS)):
        if profile.profile_id not in APPROVED_INTAKE_AI_PROFILES:
            continue
        permitted = []
        for mode in PolicyMode:
            try:
                enforce_policy(PolicyContext(mode), profile_mode=profile.policy_mode, provider_id=profile.provider_id, locality=DataLocality.COMPANY_ENDPOINT, profile_id=profile.profile_id)
            except AIGatewayError:
                continue
            permitted.append(mode.value)
        profiles.append(ProfileEntry(profile.profile_id, profile.provider_id, profile.model_id, tuple(permitted)))
    provider = ProviderEntry(provider_id, "KPMG Workbench", DataLocality.COMPANY_ENDPOINT.value, workbench_status)
    return ExtractionCatalog(
        providers=(provider,),
        profiles=tuple(profiles),
        policy_modes=tuple(mode.value for mode in PolicyMode),
        timeout_min=MIN_TIMEOUT_SECONDS,
        timeout_max=MAX_TIMEOUT_SECONDS,
        timeout_default=DEFAULT_TIMEOUT_SECONDS,
        lease_margin_seconds=LEASE_MARGIN_SECONDS,
        provider_types=ai_connections.build_provider_types(),
        locality_policies=tuple((locality.value, ai_connections.permitted_policy_modes(locality)) for locality in DataLocality),
    )


def workbench_status(environ=None) -> ProviderStatus:
    """Whether the Workbench's own variables are usable in THIS process: a fixed code, never a value, the key included. Reads the environment; no network."""
    try:
        ai_workbench.load_workbench_settings(os.environ if environ is None else environ)
    except AIGatewayError as error:
        missing = (error.reason or "").startswith("missing_")
        return ProviderStatus(False, ISSUE_MISSING_API_KEY if missing else ISSUE_INVALID_CONFIGURATION)
    return ProviderStatus(True)


def settings_from_stored(stored) -> IntakeExtractionSettings:
    """The settings a stored row describes, validated again with the same rules the API used: a row that no longer passes (edited elsewhere, or written
    for an older catalog) stops the worker instead of being trusted. Raises ``ExtractionConfigurationError`` naming fields and codes, never values.

    A row that selects a connection must find it enabled among the stored connections and still acceptable to the catalog; a missing, disabled or no longer valid connection stops
    the worker. The environment is not consulted for another one."""
    connections = tuple(getattr(stored, "connections", ()))
    try:
        values = validate_values(stored.values.as_mapping(), build_catalog(), connections=connections)
    except AISettingsValidationError as error:
        problems = ", ".join(f"{name}: {'/'.join(codes)}" for name, codes in sorted(error.fields.items()))
        raise ExtractionConfigurationError(f"The stored AI extraction settings (revision {stored.revision}) are not valid ({problems}).") from None
    if values.extraction_mode == MODE_DETERMINISTIC:
        return IntakeExtractionSettings(MODE_DETERMINISTIC, stored_revision=stored.revision)
    connection = None if values.connection_id is None else next(c for c in connections if c.connection_id == values.connection_id)
    return IntakeExtractionSettings(MODE_AI, PolicyMode(values.policy_mode), values.timeout_seconds, values.profile_id, stored.revision, connection)


def load_intake_extraction_settings(environ=None) -> IntakeExtractionSettings:
    """Read and validate this module's variables. Raises ``ExtractionConfigurationError`` naming the VARIABLE, never its value."""
    environ = os.environ if environ is None else environ
    mode = _read(environ, ENV_MODE) or MODE_DETERMINISTIC
    if mode not in MODES:
        raise ExtractionConfigurationError(f"{ENV_MODE} must be one of: {', '.join(MODES)}.")
    if mode == MODE_DETERMINISTIC:
        return IntakeExtractionSettings(MODE_DETERMINISTIC)

    policy_text = _read(environ, ENV_POLICY_MODE)
    if policy_text is None:
        raise ExtractionConfigurationError(f"{ENV_POLICY_MODE} is required in {MODE_AI} mode and has no default.")
    try:
        policy_mode = PolicyMode(policy_text)
    except ValueError:
        raise ExtractionConfigurationError(f"{ENV_POLICY_MODE} must be one of: {', '.join(m.value for m in PolicyMode)}.") from None

    timeout_text = _read(environ, ENV_TIMEOUT_SECONDS)
    timeout = DEFAULT_TIMEOUT_SECONDS
    if timeout_text is not None:
        try:
            timeout = int(timeout_text)
        except ValueError:
            raise ExtractionConfigurationError(f"{ENV_TIMEOUT_SECONDS} must be an integer.") from None
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ExtractionConfigurationError(f"{ENV_TIMEOUT_SECONDS} must be from {MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS}.")
    return IntakeExtractionSettings(MODE_AI, policy_mode, timeout)


def module_preflight(module_gate):
    """A preflight that stops the AI call unless ``request_intake.extract`` is still claimable (the module is enabled and version-matched).

    ``module_gate`` is the worker's own kind gate (``Composition.kind_gate``), called again immediately before the request text is sent. It is read-only
    and is not changed by AI-2a. A gate that answers anything but a collection of kinds allows nothing (fail closed). A database error is not caught: it
    propagates and the queue retries the attempt.
    """

    def preflight() -> None:
        allowed = module_gate()
        if not (isinstance(allowed, (set, frozenset, list, tuple)) and KIND_EXTRACT in allowed):
            raise AIExtractionError("module_unavailable", "module_not_claimable", retryable=True)

    return preflight


class IntakeExtractorSource:
    """What the handler asks for each task: the extractor to use, or ``None`` for the deterministic fixture parser. Immutable; built once per worker process."""

    __slots__ = ("settings", "extractor", "effective_timeout_seconds")

    def __init__(self, settings: IntakeExtractionSettings, extractor: AIRequestExtractor | None = None, effective_timeout_seconds: float | None = None):
        if (settings.mode == MODE_AI) != (extractor is not None):
            raise ExtractionConfigurationError("an AI extractor exists exactly in ai mode")
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "extractor", extractor)
        object.__setattr__(self, "effective_timeout_seconds", effective_timeout_seconds)

    def __setattr__(self, name, value):
        raise AttributeError("an IntakeExtractorSource is immutable")

    def __call__(self):
        return self.extractor

    def describe(self) -> str:
        """One line for the operator: the mode and, for ai, the profile, the policy and the deadline. No setting that could be secret."""
        source = "" if self.settings.stored_revision is None else f" Source: stored settings, revision {self.settings.stored_revision}."
        if self.extractor is None:
            return f"Request Intake extraction mode: {MODE_DETERMINISTIC} (fixture parser; no AI provider is called).{source}"
        connection = "" if self.settings.connection is None else f"connection {self.settings.connection.connection_id}, "
        return (
            f"Request Intake extraction mode: {MODE_AI} ({connection}profile {self.extractor.profile_id}, data policy {self.settings.policy_mode.value}, "
            f"call deadline {self.effective_timeout_seconds:g}s).{source}"
        )


def check_lease(effective_timeout_seconds: float, lease_seconds: int) -> None:
    if lease_seconds < effective_timeout_seconds + LEASE_MARGIN_SECONDS:
        raise ExtractionConfigurationError(
            f"In {MODE_AI} mode the worker lease ({lease_seconds}s) must be at least the AI call deadline ({effective_timeout_seconds:g}s) "
            f"plus {LEASE_MARGIN_SECONDS}s. Raise AP_WORKER_LEASE_SECONDS or lower {ENV_TIMEOUT_SECONDS} (or the stored timeout)."
        )


def build_extractor_source(
    environ=None,
    *,
    module_gate=None,
    lease_seconds: int | None = None,
    transport: HttpTransport | None = None,
    stored=None,
) -> IntakeExtractorSource:
    """The extractor source for this process, or ``ExtractionConfigurationError``. Opens no connection and makes no network call.

    ``stored`` (a ``StoredSettings`` read by the worker from the database, or ``None``) takes precedence over ``AP_INTAKE_EXTRACTION_MODE``,
    ``AP_AI_POLICY_MODE`` and ``AP_INTAKE_AI_TIMEOUT_SECONDS``, which are then not read; with ``None`` those variables decide exactly as before. This is
    the precedence of two configuration SOURCES, not a provider fallback: a stored row that is invalid is an error and never replaced by the environment.
    ``AP_AI_WORKBENCH_*`` (the key and the provider's own settings) always come from the environment.

    ``deterministic`` mode reads nothing else and builds no gateway. ``ai`` mode additionally needs ``module_gate`` (the worker's kind gate: without
    it the module's state could not be re-checked before sending, so it is refused) and builds the Workbench gateway from ``AP_AI_WORKBENCH_*``
    (``transport`` is for tests; the real, TLS-verifying one is built by default). It then checks, at start-up, that the profile is approved for this
    workflow, that the data policy permits its provider, and (with ``lease_seconds``) that the lease covers the call deadline.
    """
    environ = os.environ if environ is None else environ
    settings = load_intake_extraction_settings(environ) if stored is None else settings_from_stored(stored)
    if settings.mode == MODE_DETERMINISTIC:
        return IntakeExtractorSource(settings)

    if module_gate is None or not callable(module_gate):
        raise ExtractionConfigurationError("AI extraction needs the worker's module gate to confirm the module is enabled before sending text.")
    try:
        if settings.connection is not None:
            profile_id = settings.profile_id
            connection = settings.connection
            gateway = ai_connections.build_connection_gateway(connection, profile_id, environ, transport=transport)
            profile = gateway.profile(profile_id)
            policy = ai_connections.policy_for(connection, settings.policy_mode)
            provider_id, locality = connection.provider_type, DataLocality(connection.locality)
        else:
            gateway = ai_workbench.build_workbench_gateway(environ, transport=transport)
            profile_id = settings.profile_id or INTAKE_AI_PROFILE_ID
            if profile_id not in APPROVED_INTAKE_AI_PROFILES:
                raise ExtractionConfigurationError("The profile is not approved for Request Intake extraction.")
            profile = gateway.profile(profile_id)
            policy = PolicyContext(settings.policy_mode)
            provider_id, locality = ai_workbench.workbench.PROVIDER_ID, DataLocality.COMPANY_ENDPOINT
        # The gateway's own rule, evaluated now so a closed policy stops the worker at start-up instead of failing every task. It runs again on every call.
        enforce_policy(policy, profile_mode=profile.policy_mode, provider_id=provider_id, locality=locality, profile_id=profile_id)
    except AIGatewayError as error:
        raise ExtractionConfigurationError(f"The AI provider configuration is not usable ({error.code}, {error.reason or 'no_reason'}).") from None
    effective = min(float(settings.timeout_seconds), profile.timeout_seconds)
    if lease_seconds is not None:
        check_lease(effective, lease_seconds)
    extractor = AIRequestExtractor(gateway, profile_id=profile_id, policy=policy, timeout_seconds=effective, preflight=module_preflight(module_gate))
    return IntakeExtractorSource(settings, extractor, effective)

