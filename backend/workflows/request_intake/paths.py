"""Field paths: the stable naming scheme for findings, ambiguities, and source references.

Grammar (the initial stable contract; see ``findings.FINDINGS_CONTRACT_VERSION``):

    requester.name        requester.contact
    items                                    the list as a whole
    items[N]                                 one item as a whole
    items[N].description  items[N].quantity  items[N].unit  items[N].notes
    notes

``N`` is a zero-based decimal index written without leading zeros (``0``, ``7``,
``12``; never ``01``). Which paths are *required* follows the result schema in
docs/architecture/first-workflow.md, section 4.
"""

import re
from dataclasses import dataclass

_INDEX = r"(?:0|[1-9][0-9]*)"
_ITEM_FIELD = re.compile(rf"items\[({_INDEX})\]\.(description|quantity|unit|notes)")
_ITEM = re.compile(rf"items\[({_INDEX})\]")

_REQUIRED_TOP_LEVEL = {"requester.name": True, "requester.contact": False, "notes": False}
_REQUIRED_ITEM_FIELDS = {"description": True, "quantity": True, "unit": False, "notes": False}


@dataclass(frozen=True)
class FieldPath:
    path: str
    required: bool
    item_index: int | None = None  # set for items[N] and items[N].<field>
    is_field: bool = True  # False for "items" and "items[N]" (containers, not value fields)


def parse_field_path(path) -> FieldPath | None:
    """Return the parsed path, or ``None`` if ``path`` is not in the grammar."""
    if not isinstance(path, str):
        return None
    if path in _REQUIRED_TOP_LEVEL:
        return FieldPath(path, _REQUIRED_TOP_LEVEL[path])
    if path == "items":
        return FieldPath(path, True, is_field=False)
    match = _ITEM_FIELD.fullmatch(path)
    if match:
        return FieldPath(path, _REQUIRED_ITEM_FIELDS[match.group(2)], item_index=int(match.group(1)))
    match = _ITEM.fullmatch(path)
    if match:
        # A whole item always contains the required description and quantity.
        return FieldPath(path, True, item_index=int(match.group(1)), is_field=False)
    return None
