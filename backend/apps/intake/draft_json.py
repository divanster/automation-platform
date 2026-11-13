"""JSON form of a request-intake draft, for storage next to its findings.

THIS IS NOT THE CANONICAL FORM. The canonical bytes and their SHA-256 come only from
``workflows.request_intake.canonicalize_request`` and are what a ``ResultRevision`` stores. This module
exists because an INVALID draft cannot be canonicalized (canonicalization refuses blocking findings), yet a
reviewer still needs to see what the extractor produced: ``ExtractionOutcome.draft`` holds this plain JSON
copy. It is never hashed, never compared to a content hash, and never used to create a revision.

Quantities are written as the exact text of the ``Decimal`` (``str(Decimal)``) and read back with
``Decimal(text)``, so nothing is rounded and no float is involved. A value the form cannot store exactly
(a quantity that is not a ``Decimal`` or ``None``, a text field that is not ``str`` or ``None``, a line that
is not an ``int`` or ``None``) raises ``UnsupportedDraftError``; it is never converted or dropped. The
fixture extractor produces only storable values.

The field list mirrors ``workflows.request_intake.schema``; a test compares the two so a schema change
cannot go unnoticed here.
"""

from decimal import Decimal, InvalidOperation

from workflows.request_intake import Ambiguity, RequestedItem, Requester, SourceRef, StructuredRequest

from .errors import UnsupportedDraftError

_REQUESTER_KEYS = {"name", "contact"}
_ITEM_KEYS = {"description", "quantity", "unit", "notes"}
_REF_KEYS = {"field_path", "excerpt", "line"}
_DRAFT_KEYS = {"requester", "items", "notes", "source_refs"}
_AMBIGUITY_KEYS = {"field_path", "excerpt"}


def _text(value, what):
    if value is not None and not isinstance(value, str):
        raise UnsupportedDraftError(f"{what} must be text or null")
    return value


def _quantity_to_json(value):
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise UnsupportedDraftError("a quantity must be a Decimal or null")
    return str(value)


def _quantity_from_json(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnsupportedDraftError("a stored quantity must be text or null")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise UnsupportedDraftError("a stored quantity is not a decimal number") from None


def _line(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise UnsupportedDraftError("a source reference line must be an integer or null")
    return value


def _exact_keys(data, keys, what):
    if not isinstance(data, dict) or set(data) != keys:
        raise UnsupportedDraftError(f"{what} has unexpected fields")
    return data


def draft_to_json(draft: StructuredRequest) -> dict:
    """The JSON-able dict for ``draft``. Raises ``UnsupportedDraftError`` for a value it cannot store exactly."""
    if not isinstance(draft, StructuredRequest):
        raise TypeError("draft must be a StructuredRequest")
    requester = draft.requester
    if not isinstance(requester, Requester) or not isinstance(draft.items, tuple) or not isinstance(draft.source_refs, tuple):
        raise UnsupportedDraftError("the draft has an unexpected structure")
    items = []
    for item in draft.items:
        if not isinstance(item, RequestedItem):
            raise UnsupportedDraftError("an item has an unexpected structure")
        items.append(
            {
                "description": _text(item.description, "an item description"),
                "quantity": _quantity_to_json(item.quantity),
                "unit": _text(item.unit, "an item unit"),
                "notes": _text(item.notes, "item notes"),
            }
        )
    refs = []
    for ref in draft.source_refs:
        if not isinstance(ref, SourceRef):
            raise UnsupportedDraftError("a source reference has an unexpected structure")
        refs.append({"field_path": _text(ref.field_path, "a field path"), "excerpt": _text(ref.excerpt, "an excerpt"), "line": _line(ref.line)})
    return {
        "requester": {"name": _text(requester.name, "the requester name"), "contact": _text(requester.contact, "the requester contact")},
        "items": items,
        "notes": _text(draft.notes, "the notes"),
        "source_refs": refs,
    }


def draft_from_json(data) -> StructuredRequest:
    """The draft stored by ``draft_to_json``. Anything unexpected raises ``UnsupportedDraftError``."""
    _exact_keys(data, _DRAFT_KEYS, "the stored draft")
    requester = _exact_keys(data["requester"], _REQUESTER_KEYS, "the stored requester")
    if not isinstance(data["items"], list) or not isinstance(data["source_refs"], list):
        raise UnsupportedDraftError("the stored draft has unexpected lists")
    items = []
    for raw in data["items"]:
        raw = _exact_keys(raw, _ITEM_KEYS, "a stored item")
        items.append(
            RequestedItem(
                description=_text(raw["description"], "an item description"),
                quantity=_quantity_from_json(raw["quantity"]),
                unit=_text(raw["unit"], "an item unit"),
                notes=_text(raw["notes"], "item notes"),
            )
        )
    refs = []
    for raw in data["source_refs"]:
        raw = _exact_keys(raw, _REF_KEYS, "a stored source reference")
        refs.append(SourceRef(field_path=_text(raw["field_path"], "a field path"), excerpt=_text(raw["excerpt"], "an excerpt"), line=_line(raw["line"])))
    return StructuredRequest(
        requester=Requester(name=_text(requester["name"], "the requester name"), contact=_text(requester["contact"], "the requester contact")),
        items=tuple(items),
        notes=_text(data["notes"], "the notes"),
        source_refs=tuple(refs),
    )


def ambiguities_to_json(ambiguities) -> list:
    return [{"field_path": _text(a.field_path, "a field path"), "excerpt": _text(a.excerpt, "an excerpt")} for a in ambiguities]


def ambiguities_from_json(data) -> tuple[Ambiguity, ...]:
    if not isinstance(data, list):
        raise UnsupportedDraftError("the stored ambiguities are not a list")
    result = []
    for raw in data:
        raw = _exact_keys(raw, _AMBIGUITY_KEYS, "a stored ambiguity")
        result.append(Ambiguity(field_path=_text(raw["field_path"], "a field path"), excerpt=_text(raw["excerpt"], "an excerpt")))
    return tuple(result)
