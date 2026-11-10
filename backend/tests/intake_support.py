"""Shared helpers for the pure-domain request-intake tests.

Standard library only: no Django, so the request-intake tests run both with
``python -m unittest`` and through ``manage.py test``.
"""

import dataclasses
import enum
from decimal import Decimal

from workflows.request_intake import RequestedItem, Requester, StructuredRequest

VALID_TEXT = """\
Requester: Jane Doe
Contact: jane@example.invalid
Item: 10 | boxes | A4 paper | for the print room
Item: 2 | | Stapler
Notes: Needed this week.
"""


def make_request(**overrides):
    """A valid request, with any part replaced by keyword."""
    fields = {
        "requester": Requester(name="Jane Doe", contact="jane@example.invalid"),
        "items": (
            RequestedItem(description="A4 paper", quantity=10, unit="boxes", notes="for printing"),
            RequestedItem(description="Stapler", quantity=2),
        ),
        "notes": "Needed this week.",
    }
    fields.update(overrides)
    return StructuredRequest(**fields)


def summary(result):
    """(code, path) pairs in order, for exact comparisons."""
    return [(finding.code.value, finding.field_path) for finding in result.findings]


_IMMUTABLE_LEAVES = (str, int, float, bool, Decimal, type(None), bytes, enum.Enum)


def find_mutable_state(obj, path="root"):
    """Return the paths of any mutable containers reachable from ``obj`` (empty if none).

    Walks frozen dataclasses and tuples. Lists, dicts, sets, and bytearrays are mutable
    state that could be shared between callers.
    """
    if isinstance(obj, (list, dict, set, bytearray)):
        return [f"{path} is a {type(obj).__name__}"]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        found = []
        for f in dataclasses.fields(obj):
            found += find_mutable_state(getattr(obj, f.name), f"{path}.{f.name}")
        return found
    if isinstance(obj, tuple):
        found = []
        for index, element in enumerate(obj):
            found += find_mutable_state(element, f"{path}[{index}]")
        return found
    if isinstance(obj, _IMMUTABLE_LEAVES):
        return []
    return [f"{path} is an unrecognized {type(obj).__name__}"]
