"""UUID identifiers and revision numbers supplied by the caller.

The canonical internal identifier type is ``uuid.UUID``, for the case, the workflow run, the
revision, and the approver (the confirmed direction is UUID identifiers). The policy accepts
only ``uuid.UUID`` instances: an arbitrary printable string is NOT a valid identifier, and a
string that merely looks like a UUID is not accepted either; convert it explicitly with
``parse_uuid``.

The policy never generates identifiers, UUIDs, timestamps, or sequence values: the caller (a
future application service) supplies them. Which UUID version or variant is used, and whether
an identifier refers to something that exists, is not checked here.
"""

import re
import uuid

from .errors import InvalidIdentifierError

# The canonical textual form only: 8-4-4-4-12 hexadecimal digits separated by hyphens (either
# case). Braces, "urn:uuid:" prefixes, missing hyphens, and surrounding whitespace are rejected.
_CANONICAL_UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def is_valid_uuid(value) -> bool:
    return isinstance(value, uuid.UUID)


def validate_uuid(value, label: str) -> uuid.UUID:
    """Return ``value`` if it is a ``uuid.UUID``, else raise ``InvalidIdentifierError``.

    Strings are never accepted here, even well-formed ones; use ``parse_uuid`` to convert.
    """
    if not is_valid_uuid(value):
        raise InvalidIdentifierError(f"{label} must be a uuid.UUID (convert a string explicitly with parse_uuid)")
    return value


def parse_uuid(text, label: str = "identifier") -> uuid.UUID:
    """Explicitly convert canonical UUID text (8-4-4-4-12 hex) to a ``uuid.UUID``.

    Raises ``InvalidIdentifierError`` for anything else (other types, other textual forms,
    malformed text). The message never echoes the text.
    """
    if not isinstance(text, str) or not _CANONICAL_UUID_TEXT.fullmatch(text):
        raise InvalidIdentifierError(f"{label} must be a UUID in canonical 8-4-4-4-12 hexadecimal form")
    return uuid.UUID(text)


def is_valid_revision_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def validate_revision_number(value) -> int:
    """Return ``value`` if it is an integer of at least 1 (never a bool), else raise."""
    if not is_valid_revision_number(value):
        raise InvalidIdentifierError("revision_number must be an integer of at least 1")
    return value
