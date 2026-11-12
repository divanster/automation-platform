"""Task handlers and the handler allowlist.

Standard library only (plus ``apps.tasks.policy``). A task's ``kind`` is looked up in the fixed
``HANDLERS`` mapping below; nothing is resolved from a dotted path, imported by name, evaluated,
or executed from a string, and no handler can be supplied by a user.

The only handler in this slice is ``test.add_numbers``. It is a TEST handler: a pure function of
its validated payload with no network, filesystem, AI, clock, randomness, or database access.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from .policy import InvalidPayloadError

TEST_ADD_NUMBERS = "test.add_numbers"

MAX_VALUES = 20
MAX_ABS_VALUE = 1_000_000
MAX_LABEL_LENGTH = 40


def validate_add_numbers_payload(payload) -> dict:
    """Return a clean copy of a valid ``test.add_numbers`` payload, or raise ``InvalidPayloadError``.

    ``{"values": [1 to 20 integers, each with absolute value <= 1,000,000], "label": optional str <= 40}``.
    Error messages name the field and the rule, never the offending value.
    """
    if not isinstance(payload, dict):
        raise InvalidPayloadError("payload must be an object")
    unknown = set(payload) - {"values", "label"}
    if unknown:
        raise InvalidPayloadError("payload has an unsupported field")
    values = payload.get("values")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
        raise InvalidPayloadError(f"values must be a list of 1 to {MAX_VALUES} integers")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_ABS_VALUE:
            raise InvalidPayloadError(f"each value must be an integer with absolute value at most {MAX_ABS_VALUE}")
    clean = {"values": list(values)}
    if "label" in payload:
        label = payload["label"]
        if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
            raise InvalidPayloadError(f"label must be a string of at most {MAX_LABEL_LENGTH} characters")
        clean["label"] = label
    return clean


def run_add_numbers(payload: dict) -> dict:
    """Deterministic: the same payload always gives the same result."""
    result = {"count": len(payload["values"]), "sum": sum(payload["values"])}
    if "label" in payload:
        result["label"] = payload["label"]
    return result


@dataclass(frozen=True)
class TaskHandler:
    kind: str
    validate: Callable[[object], dict]  # clean copy of the payload, or InvalidPayloadError
    run: Callable[[dict], dict]  # result for a validated payload; may raise TaskRetryableError / TaskPermanentError
    is_test_handler: bool


HANDLERS: Mapping[str, TaskHandler] = MappingProxyType(
    {
        TEST_ADD_NUMBERS: TaskHandler(
            kind=TEST_ADD_NUMBERS,
            validate=validate_add_numbers_payload,
            run=run_add_numbers,
            is_test_handler=True,
        ),
    }
)


def get_handler(kind, registry: Mapping[str, TaskHandler] | None = None) -> TaskHandler | None:
    """The handler registered for ``kind``, or None. ``registry`` exists so tests can inject fakes."""
    registry = HANDLERS if registry is None else registry
    if not isinstance(kind, str):
        return None
    return registry.get(kind)
