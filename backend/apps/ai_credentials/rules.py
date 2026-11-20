"""Pure rules of the managed credentials: which references are acceptable and the opaque id an audit event uses. Standard library only; no Django, no secret store."""

import re
import uuid

CREDENTIAL_REF_PREFIX = "AP_AI_CRED_"
# The same shape a connection's ``credential_ref`` must have (``apps.ai_settings.rules``), so a managed credential can only ever be one an AI connection could name, and this API
# can never touch another use of the secret store.
_REFERENCE = re.compile(rf"{CREDENTIAL_REF_PREFIX}[A-Z0-9_]{{1,48}}")
MAX_SECRET_LENGTH = 4096  # the secret store's own bound, repeated so a too long value is refused with a field code before it reaches any store


def reference_problem(reference):
    """A stable code when ``reference`` is not an acceptable managed credential reference, else ``None``. Never echoes the value."""
    if not isinstance(reference, str):
        return "invalid_type"
    if _REFERENCE.fullmatch(reference) is None:
        return "invalid_reference"
    return None


def credential_subject_id(reference: str) -> uuid.UUID:
    """The audit subject id of a credential: derived from the reference, so it is stable, needs no row of its own and does not reveal the reference name."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:automation-platform:ai-credential:{reference}")


def credential_lock_key(reference: str) -> int:
    """The signed 64-bit key of the PostgreSQL transaction-level advisory lock that serializes operations on ONE reference: the first eight bytes of its opaque subject id. Two references
    have two keys (a rare collision only makes two operations wait for each other; it never makes one skip a lock), and the key reveals nothing a subject id does not."""
    return int.from_bytes(credential_subject_id(reference).bytes[:8], "big", signed=True)
