"""AI-CON-TEST-1 composition: the prober behind ``POST /api/ai-settings/connections/<connection_id>/test/``. Reads the process environment only when a probe runs.

WHAT IT DOES. For ONE saved connection and ONE of its saved profiles it builds, with ``config/ai_connections.build_connection_gateway``, the SAME gateway the worker would build (one provider,
one profile, the provider's own loader, adapter and guarded transport), and sends ONE fixed, synthetic request through ``AIGateway.generate``. So nothing is re-implemented and nothing is
weakened: the credential is resolved backend-only (the environment variable the reference names, or the secret store, as the connection's ``credential_source`` says, with no fallback between
them), the operator's approved-origin lists, company CIDR ranges, proxy rules and CA settings apply unchanged, the guarded transport resolves and checks every address before it connects, and the
gateway's data-policy rule runs before any provider code. There is no fallback to another connection, profile, model, provider or locality.

WHAT IT ADDS. Fixed bounds that can only be tighter than the connection's own: one attempt (no retry: the environment copy sets every provider's retry count to 0), a deadline of
``TEST_TIMEOUT_SECONDS`` (never above the profile's), ``TEST_MAX_OUTPUT_TOKENS`` of output, and a response cap of ``TEST_MAX_RESPONSE_BYTES`` enforced by the real transport while it reads.

THE POLICY. The data policy in force is the saved one when the saved mode is ``ai`` (so a connection is tested under the rule it would run under). When the saved mode is deterministic there
is none, and the connection's own locality decides the least permissive mode that permits it (``local``: ``local_only``; ``company_endpoint``: ``company_endpoint``); a ``cloud`` connection is
then refused by the gateway as ``policy_violation`` (no policy approves a cloud provider), so a probe never leaves for a cloud provider unless the saved policy is ``approved_cloud``. The probe holds
no company data: a fixed prompt and nothing else.

WHAT LEAVES. A ``ProbeOutcome``: success, or one stable gateway code (``timeout``, ``rate_limit``, ``authentication_failure``...), whether a retry could help and a duration. The provider's answer is
validated against the probe's schema by the gateway and then discarded: it is not stored, logged or returned. A connection that cannot be set up in this process (a missing or unreadable
credential, an endpoint the operator has not approved, an invalid configuration, a store that is unavailable) raises ``ConnectionTestRefusedError('connection_not_usable', issue)`` BEFORE any
request is made, with one of the status probe's three fixed issue codes. Importing this module reads no environment and makes no network call.
"""

import logging
import os
import threading
import time

from apps.ai_gateway import AIGatewayError, DataLocality, GenerationRequest, PolicyMode
from apps.ai_settings.connection_test import CODE_CONNECTION_NOT_USABLE, ProbeOutcome
from apps.ai_settings.errors import ConnectionTestBusyError, ConnectionTestRefusedError
from apps.ai_settings.rules import ISSUE_INVALID_CONFIGURATION

from config import ai_connections

logger = logging.getLogger(__name__)

TEST_TIMEOUT_SECONDS = 20.0  # provisional, not measured: a model that is loading can be slower than this, which a test reports as ``timeout``
TEST_MAX_OUTPUT_TOKENS = 128
TEST_MAX_RESPONSE_BYTES = 64 * 1024
MAX_CONCURRENT_TESTS = 2  # per process; a test holds a web thread for up to TEST_TIMEOUT_SECONDS

# One attempt, whatever the operator configured for real calls: a test is a question, not a job, and a retry would multiply the load on a provider that is already refusing.
_NO_RETRIES = {"AP_AI_WORKBENCH_MAX_RETRIES": "0", "AP_AI_OPENAI_MAX_RETRIES": "0", "AP_AI_OLLAMA_MAX_RETRIES": "0"}

PROBE_INSTRUCTIONS = 'This is a connectivity check with no data. Reply with only this JSON object: {"status": "ok"}'
PROBE_INPUT = "connectivity check"
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}


def _default_policy_mode(locality: DataLocality) -> PolicyMode:
    """The mode a probe runs under when no AI policy is saved: the least permissive one that permits the locality, and for ``cloud`` the strictest, which the gateway refuses."""
    if locality is DataLocality.CLOUD:
        return PolicyMode.LOCAL_ONLY
    return ai_connections.PROFILE_MODE_BY_LOCALITY[locality]


def _not_usable(issue: str) -> ConnectionTestRefusedError:
    return ConnectionTestRefusedError(CODE_CONNECTION_NOT_USABLE, issue)


def _milliseconds(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def build_prober(environ=None, *, transport=None, secret_store=None, clock=time.monotonic, max_concurrent=MAX_CONCURRENT_TESTS):
    """The prober ``apps.ai_settings.connection_test.run_connection_test`` calls. ``environ``, ``transport``, ``secret_store``, ``clock`` and ``max_concurrent`` are for tests: by default the
    process environment (read when a probe runs), the real guarded transports and the process's own secret store are used. At most ``max_concurrent`` probes run at once in this
    process; another one raises ``ConnectionTestBusyError`` immediately (the throttles are a read-modify-write on a local cache and do not hold under a burst, this does)."""

    semaphore = threading.BoundedSemaphore(max_concurrent)  # one per prober, and the route builds one per process

    def probe(connection, profile_id, policy_mode) -> ProbeOutcome:
        env = dict(os.environ if environ is None else environ)
        env.update(_NO_RETRIES)
        try:
            locality = DataLocality(connection.locality)
            mode = PolicyMode(policy_mode) if policy_mode is not None else _default_policy_mode(locality)
            gateway = ai_connections.build_connection_gateway(connection, profile_id, env, transport=transport, secret_store=secret_store, max_response_bytes=TEST_MAX_RESPONSE_BYTES)
            profile = gateway.profile(profile_id)
        except AIGatewayError as error:
            raise _not_usable(ai_connections.issue_of(error)) from None
        except Exception as error:  # the class name only: the text could hold a value
            logger.error("AI connection test setup failed (%s).", type(error).__name__)
            raise _not_usable(ISSUE_INVALID_CONFIGURATION) from None
        request = GenerationRequest(
            profile_id=profile_id,
            instructions=PROBE_INSTRUCTIONS,
            input_text=PROBE_INPUT,
            output_schema=PROBE_SCHEMA,
            max_output_tokens=min(TEST_MAX_OUTPUT_TOKENS, profile.limits.max_output_tokens),
            timeout_seconds=min(TEST_TIMEOUT_SECONDS, profile.timeout_seconds),
        )
        policy = ai_connections.policy_for(connection, mode)
        started = clock()
        try:
            gateway.generate(request, policy=policy)  # the answer is validated by the gateway and dropped here
        except AIGatewayError as error:
            return ProbeOutcome(False, error.code, error.retryable, _milliseconds(clock() - started))
        except Exception as error:  # the gateway classifies everything it knows; this is a bug, reported as a provider failure with the class name only
            logger.error("AI connection test call failed unexpectedly (%s).", type(error).__name__)
            return ProbeOutcome(False, "provider_error", False, _milliseconds(clock() - started))
        return ProbeOutcome(True, None, False, _milliseconds(clock() - started))

    def prober(connection, profile_id, policy_mode) -> ProbeOutcome:
        # Never wait for a slot: a test holds a web thread while a provider answers, so a burst is refused at once (429), before any credential is read or any request is made.
        if not semaphore.acquire(blocking=False):
            raise ConnectionTestBusyError()
        try:
            return probe(connection, profile_id, policy_mode)
        finally:
            semaphore.release()

    return prober
