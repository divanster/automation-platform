"""The connection test service (AI-CON-TEST-1): the administrator asks whether ONE saved connection and ONE of its saved model profiles can answer a fixed probe.

AUTHORIZATION FIRST. Only an administrator (an active ``is_superuser`` user, reloaded from the database on every call) may test a connection, like every AI settings operation. A refusal is
decided BEFORE the connection identifier or the profile is looked at, so a caller who is not allowed learns nothing about which connections exist.

WHAT IS TESTED. Exactly the connection and profile as SAVED (the settings row), never anything the request supplies: the request carries a profile identifier and nothing else, so there is
no endpoint, key, prompt or limit to submit. A connection that is not saved is ``ConnectionNotFoundError``; a disabled one is ``ConnectionTestRefusedError('connection_disabled')``; a profile the
connection does not have is a validation error. There is no fallback to another connection, profile, model or provider, and nothing falls back to the environment-configured provider.

WHAT IT NEVER DOES. It writes nothing: no settings, no audit row, no run, case, revision or task; it does not touch the queue, a workflow or the extraction settings. It holds no database
transaction while a provider is called (the row is read once, unlocked, before). The provider's answer is never stored, logged or returned: only a fixed outcome (``ok``, a stable gateway
code, whether a retry could help, a duration) leaves this module.

THE PROBER. How a connection is actually called (credential resolution, endpoint and locality rules, the data policy, the guarded transport) is the composition root's job
(``config/ai_connection_test.py``), injected as ``prober`` by ``config/urls.py``: this package imports nothing from the AI Gateway. The prober raises
``ConnectionTestRefusedError('connection_not_usable', issue)`` when the connection cannot be set up in this process (a missing credential, an endpoint the operator has not approved, an
invalid configuration) and otherwise returns a ``ProbeOutcome``. A prober that returns anything else, or an outcome with a code outside the closed set, produces an error, never a guess.

LOGGING. One line with identifiers only: the actor's public id, the connection and profile identifiers, the outcome code and the duration. Never a value, an endpoint, a reference or a message.
"""

import logging
from dataclasses import dataclass

from . import services
from .errors import AISettingsValidationError, ConnectionNotFoundError, ConnectionTestBusyError, ConnectionTestRefusedError

logger = logging.getLogger(__name__)

CODE_CONNECTION_DISABLED = "connection_disabled"
CODE_CONNECTION_NOT_USABLE = "connection_not_usable"

# The stable failure codes of the AI Gateway (a test pins the two sets equal). A probe outcome carries one of them or ``None``.
FAILURE_CODES = frozenset(
    {
        "provider_unavailable",
        "policy_violation",
        "timeout",
        "invalid_structured_output",
        "rate_limit",
        "authentication_failure",
        "unsupported_model",
        "configuration_error",
        "invalid_request",
        "capability_missing",
        "provider_error",
    }
)


@dataclass(frozen=True)
class ProbeOutcome:
    """What a prober reports: whether the probe succeeded, the stable failure code when it did not, whether a retry could help and how long the call took."""

    ok: bool
    code: str | None
    retryable: bool
    duration_ms: int


@dataclass(frozen=True)
class ConnectionTestResult:
    connection_id: str
    profile_id: str
    provider_type: str
    locality: str
    ok: bool
    code: str | None
    retryable: bool
    duration_ms: int


class ProbeFailedError(Exception):
    """The prober broke its contract (an exception it did not classify, or an invalid outcome). Carries no text: the cause could hold a value."""


def _checked_outcome(outcome) -> ProbeOutcome:
    valid = (
        isinstance(outcome, ProbeOutcome)
        and isinstance(outcome.ok, bool)
        and isinstance(outcome.retryable, bool)
        and isinstance(outcome.duration_ms, int)
        and not isinstance(outcome.duration_ms, bool)
        and outcome.duration_ms >= 0
        and ((outcome.ok and outcome.code is None and not outcome.retryable) or (not outcome.ok and outcome.code in FAILURE_CODES))
    )
    if not valid:
        raise ProbeFailedError()
    return outcome


def authorize(user) -> None:
    """Refuse everyone but an administrator, with nothing validated or looked up first. The API calls it before it parses the body; ``run_connection_test`` checks again."""
    services.administrator_actor(user, "ai_settings.connection_test")


def run_connection_test(*, user, connection_id, profile_id, prober) -> ConnectionTestResult:
    """Test the saved ``connection_id`` with its saved ``profile_id`` through ``prober``. Raises ``AuthorizationError``, ``ConnectionNotFoundError``, ``ConnectionTestRefusedError``,
    ``AISettingsValidationError`` or ``ProbeFailedError``. Changes nothing."""
    actor = services.administrator_actor(user, "ai_settings.connection_test")
    context = services.read_connection_test_context()
    connection = next((c for c in context.connections if c.connection_id == connection_id), None) if isinstance(connection_id, str) else None
    if connection is None:
        raise ConnectionNotFoundError()
    if not connection.enabled:
        raise ConnectionTestRefusedError(CODE_CONNECTION_DISABLED)
    if not isinstance(profile_id, str) or connection.profile(profile_id) is None:
        raise AISettingsValidationError({"profile_id": ["unknown_profile"]})
    try:
        outcome = _checked_outcome(prober(connection, profile_id, context.policy_mode))
    except (ConnectionTestRefusedError, ConnectionTestBusyError):
        logger.info("AI connection test not run: actor=%s connection=%s profile=%s", actor.public_id, connection.connection_id, profile_id)
        raise
    except ProbeFailedError:
        logger.error("AI connection test failed: the prober returned an invalid outcome (actor=%s connection=%s).", actor.public_id, connection.connection_id)
        raise
    except Exception as error:  # the class name only: the text could hold a value
        logger.error("AI connection test failed (%s): actor=%s connection=%s", type(error).__name__, actor.public_id, connection.connection_id)
        raise ProbeFailedError() from None
    logger.info(
        "AI connection test: actor=%s connection=%s profile=%s ok=%s code=%s duration_ms=%s", actor.public_id, connection.connection_id, profile_id, outcome.ok, outcome.code, outcome.duration_ms
    )
    return ConnectionTestResult(
        connection.connection_id, profile_id, connection.provider_type, connection.locality, outcome.ok, outcome.code, outcome.retryable, outcome.duration_ms
    )
