"""AI-2a composition: which extractor Request Intake uses, decided explicitly and failing closed. Reads the process environment only when a function runs.

TWO MODES, NO AUTOMATIC SWITCH. ``deterministic`` (the default, and the behaviour of every installation that sets nothing) leaves the worker on the fixture
parser. ``ai`` must be asked for by name. There is no fallback from either to the other: with ``ai`` a failure is a failure, and an unset or invalid value
of any setting below stops the worker at start-up (``runworker``) instead of choosing a default. Nothing here touches the database and no setting is a
database field, so no credential can be stored there; the Workbench key is read by ``config/ai_workbench.py`` from the environment, as in AI-1b.

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
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import AIExtractionError, ExtractionConfigurationError
from apps.intake.policy import KIND_EXTRACT

from config import ai_workbench

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


def _read(environ, name):
    return environ.get(name, "").strip() or None  # an empty value counts as unset, the convention of config/env.py


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
        if self.extractor is None:
            return f"Request Intake extraction mode: {MODE_DETERMINISTIC} (fixture parser; no AI provider is called)."
        return (
            f"Request Intake extraction mode: {MODE_AI} (profile {self.extractor.profile_id}, data policy {self.settings.policy_mode.value}, "
            f"call deadline {self.effective_timeout_seconds:g}s)."
        )


def check_lease(effective_timeout_seconds: float, lease_seconds: int) -> None:
    if lease_seconds < effective_timeout_seconds + LEASE_MARGIN_SECONDS:
        raise ExtractionConfigurationError(
            f"In {MODE_AI} mode the worker lease ({lease_seconds}s) must be at least the AI call deadline ({effective_timeout_seconds:g}s) "
            f"plus {LEASE_MARGIN_SECONDS}s. Raise AP_WORKER_LEASE_SECONDS or lower {ENV_TIMEOUT_SECONDS}."
        )


def build_extractor_source(
    environ=None,
    *,
    module_gate=None,
    lease_seconds: int | None = None,
    transport: HttpTransport | None = None,
) -> IntakeExtractorSource:
    """The extractor source for this process, or ``ExtractionConfigurationError``. Opens no connection and makes no network call.

    ``deterministic`` mode reads nothing else and builds no gateway. ``ai`` mode additionally needs ``module_gate`` (the worker's kind gate: without
    it the module's state could not be re-checked before sending, so it is refused) and builds the Workbench gateway from ``AP_AI_WORKBENCH_*``
    (``transport`` is for tests; the real, TLS-verifying one is built by default). It then checks, at start-up, that the profile is approved for this
    workflow, that the data policy permits its provider, and (with ``lease_seconds``) that the lease covers the call deadline.
    """
    environ = os.environ if environ is None else environ
    settings = load_intake_extraction_settings(environ)
    if settings.mode == MODE_DETERMINISTIC:
        return IntakeExtractorSource(settings)

    if module_gate is None or not callable(module_gate):
        raise ExtractionConfigurationError("AI extraction needs the worker's module gate to confirm the module is enabled before sending text.")
    try:
        gateway = ai_workbench.build_workbench_gateway(environ, transport=transport)
        profile_id = INTAKE_AI_PROFILE_ID
        if profile_id not in APPROVED_INTAKE_AI_PROFILES:
            raise ExtractionConfigurationError("The profile is not approved for Request Intake extraction.")
        profile = gateway.profile(profile_id)
        policy = PolicyContext(settings.policy_mode)
        # The gateway's own rule, evaluated now so a closed policy stops the worker at start-up instead of failing every task. It runs again on every call.
        enforce_policy(policy, profile_mode=profile.policy_mode, provider_id=ai_workbench.workbench.PROVIDER_ID, locality=DataLocality.COMPANY_ENDPOINT, profile_id=profile_id)
    except AIGatewayError as error:
        raise ExtractionConfigurationError(f"The AI provider configuration is not usable ({error.code}, {error.reason or 'no_reason'}).") from None
    effective = min(float(settings.timeout_seconds), profile.timeout_seconds)
    if lease_seconds is not None:
        check_lease(effective, lease_seconds)
    extractor = AIRequestExtractor(gateway, profile_id=profile_id, policy=policy, timeout_seconds=effective, preflight=module_preflight(module_gate))
    return IntakeExtractorSource(settings, extractor, effective)

