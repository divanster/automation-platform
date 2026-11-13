"""Pure validation of a ``StructuredRequest`` (first-workflow.md, section 6).

``validate_request`` is a pure function: same input, same findings, no I/O, no state, and
no mutation of its arguments. It validates data; it never produces or repairs it.
Extraction (producing candidate data) is a separate step in ``extraction.py``.

Three kinds of outcome are kept distinct:

* valid input            -> no blocking findings (``ValidationResult.is_valid``)
* missing required data  -> ``missing_required``
* invalid field values   -> ``invalid_type``, ``invalid_value``, ``exceeds_maximum``,
                            ``exceeds_length``, ``invalid_source_ref``

Quantity rules (decided; see quantity.py): a quantity is required; it must be a number
(int, float, or Decimal; never a bool or text); it must be finite; it must be greater than
zero; and it must not exceed the maximum *if one is configured*. Fractions are allowed.
Quantities are compared as exact ``Decimal`` values; nothing is rounded. Unit-specific
integer restrictions are deferred.

Ambiguity: an ambiguity on a REQUIRED field (``requester.name``, ``items``, ``items[N]``,
``items[N].description``, ``items[N].quantity``) is BLOCKING (``ambiguous_required_value``)
even if the field still carries a value. On an optional field it is a warning
(``ambiguous_optional_value``). Required values that are missing are still reported as
``missing_required``; the ambiguity finding is added, never substituted.

Duplicates: warning only, conservative rule (see ``_description_key``). Items are never
merged, deleted, or summed.

No price is calculated and no availability is claimed anywhere in this module.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .findings import Finding, FindingCode, ValidationResult
from .paths import parse_field_path
from .quantity import MAX_PADDING_ZEROS, is_finite, is_number, plain_notation_length, plain_padding_zeros, to_decimal
from .schema import (
    Ambiguity,
    Quantity,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    make_excerpt,
)


@dataclass(frozen=True)
class ValidationLimits:
    """Optional, explicitly configured limits. ``None`` means "not configured": skipped.

    No business limits are invented here and none has a default. ``max_text_length`` is a
    single limit applied to every text field; whether limits should be per field, and what
    the values should be, is left unresolved for the later input/storage phase.
    ``max_quantity`` is stored as a canonical ``Decimal``.
    """

    max_quantity: Quantity | None = None
    max_text_length: int | None = None

    def __post_init__(self):
        if self.max_quantity is not None:
            if not is_number(self.max_quantity) or not is_finite(self.max_quantity) or self.max_quantity <= 0:
                raise ValueError("max_quantity must be a finite number greater than zero")
            object.__setattr__(self, "max_quantity", to_decimal(self.max_quantity))
        if self.max_text_length is not None:
            if isinstance(self.max_text_length, bool) or not isinstance(self.max_text_length, int) or self.max_text_length < 1:
                raise ValueError("max_text_length must be an integer of at least 1")


def validate_request(
    request: StructuredRequest,
    *,
    limits: ValidationLimits | None = None,
    source_text: str | None = None,
    ambiguities: Iterable[Ambiguity] = (),
) -> ValidationResult:
    """Return the findings for ``request``.

    ``source_text`` enables two checks that need the original text: the "value cannot be
    located in the source" warning, and validation of ``request.source_refs`` against it.
    ``ambiguities`` are reported by the extractor. Findings are ordered deterministically:
    rule findings in field order, duplicate items, source-location warnings, source
    reference findings, then ambiguity findings.
    """
    if not isinstance(request, StructuredRequest):
        raise TypeError("request must be a StructuredRequest")
    if source_text is not None and not isinstance(source_text, str):
        raise TypeError("source_text must be a str or None")
    limits = limits if limits is not None else ValidationLimits()

    findings: list[Finding] = []
    _check_requester(request.requester, limits, findings)
    _check_items(request.items, limits, findings)
    _check_text(request.notes, "notes", required=False, limits=limits, findings=findings)
    _check_source_ref_types(request.source_refs, findings)
    _check_duplicate_items(request.items, findings)
    if source_text is not None:
        _check_values_in_source(request, source_text, findings)
        _check_source_refs(request, source_text, findings)
    _add_ambiguity_findings(ambiguities, findings)
    return ValidationResult(findings=tuple(findings))


# --- requester ---------------------------------------------------------------


def _check_requester(requester, limits, findings):
    if requester is None:
        findings.append(_missing("requester.name", "Requester name is required."))
        return
    if not isinstance(requester, Requester):
        findings.append(_finding(FindingCode.INVALID_TYPE, "requester", "Requester must be a Requester."))
        return
    _check_text(requester.name, "requester.name", required=True, limits=limits, findings=findings)
    _check_text(requester.contact, "requester.contact", required=False, limits=limits, findings=findings)


# --- items -------------------------------------------------------------------


def _check_items(items, limits, findings):
    if items is None:
        findings.append(_missing("items", "At least one requested item is required."))
        return
    if not isinstance(items, (tuple, list)):
        findings.append(_finding(FindingCode.INVALID_TYPE, "items", "Items must be a list of requested items."))
        return
    if len(items) == 0:
        findings.append(_missing("items", "At least one requested item is required."))
        return
    for index, item in enumerate(items):
        path = f"items[{index}]"
        if not isinstance(item, RequestedItem):
            findings.append(_finding(FindingCode.INVALID_TYPE, path, "Item must be a RequestedItem."))
            continue
        _check_text(item.description, f"{path}.description", required=True, limits=limits, findings=findings)
        _check_quantity(item.quantity, f"{path}.quantity", limits, findings)
        _check_text(item.unit, f"{path}.unit", required=False, limits=limits, findings=findings)
        _check_text(item.notes, f"{path}.notes", required=False, limits=limits, findings=findings)


def _check_quantity(quantity, path, limits, findings):
    if quantity is None:
        findings.append(_missing(path, "Quantity is required."))
        return
    if not is_number(quantity):
        findings.append(_finding(FindingCode.INVALID_TYPE, path, "Quantity must be a number."))
        return
    if not is_finite(quantity):
        findings.append(_finding(FindingCode.INVALID_VALUE, path, "Quantity must be a finite number."))
        return
    value = to_decimal(quantity)  # exact; the canonical type; no rounding
    if not value > 0:
        findings.append(_finding(FindingCode.INVALID_VALUE, path, "Quantity must be greater than zero."))
        return
    if limits.max_quantity is not None and value > limits.max_quantity:
        findings.append(_finding(FindingCode.EXCEEDS_MAXIMUM, path, "Quantity exceeds the configured maximum."))


# --- duplicates (warning only) ------------------------------------------------


def _description_key(description: str) -> str:
    """Conservative comparison key for the duplicate-item warning.

    Applied: strip surrounding whitespace, collapse runs of whitespace to one space, and
    casefold. NOT applied: Unicode normalization, punctuation removal, plural or stem
    handling, abbreviations, synonyms, translations, or any semantic similarity. Two
    descriptions are reported only if they are identical after those three steps, so
    "A4 paper" and "a4   PAPER" match, while "A4 paper", "A4-paper" and "A4 papers" do not.
    """
    return " ".join(description.split()).casefold()


def _check_duplicate_items(items, findings):
    """Warn when two items have the same description key. Never merges, deletes, or sums."""
    if not isinstance(items, (tuple, list)):
        return
    first_seen: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, RequestedItem):
            continue
        if not isinstance(item.description, str) or not item.description.strip():
            continue
        key = _description_key(item.description)
        if key in first_seen:
            findings.append(
                _finding(
                    FindingCode.DUPLICATE_ITEM,
                    f"items[{index}].description",
                    f"Description matches the item at index {first_seen[key]}.",
                )
            )
        else:
            first_seen[key] = index


# --- text fields -------------------------------------------------------------


def _check_text(value, path, *, required, limits, findings):
    if value is None:
        if required:
            findings.append(_missing(path, "A value is required."))
        return
    if not isinstance(value, str):
        findings.append(_finding(FindingCode.INVALID_TYPE, path, "Value must be text."))
        return
    if required and not value.strip():
        findings.append(_missing(path, "A value is required."))
        return
    if limits.max_text_length is not None and len(value) > limits.max_text_length:
        findings.append(_finding(FindingCode.EXCEEDS_LENGTH, path, "Text exceeds the configured maximum length."))


# --- source location (warning only) ------------------------------------------


def _normalize(text):
    """Trim, collapse runs of whitespace, and casefold."""
    return " ".join(text.split()).casefold()


def _check_values_in_source(request, source_text, findings):
    """Warn about values that cannot be located in the source text.

    A simple, deterministic check: text values are compared after trimming, collapsing
    whitespace, and ignoring case; quantities are looked up as standalone numbers written
    exactly as the canonical Decimal prints (so ``2.50`` is not found in a text that only
    says ``2.5``). The exact matching rules are unspecified, so this is deliberately
    conservative and only ever a warning.
    """
    haystack = _normalize(source_text)

    def check_text(value, path):
        if isinstance(value, str) and value.strip() and _normalize(value) not in haystack:
            findings.append(_finding(FindingCode.VALUE_NOT_IN_SOURCE, path, "Value was not found in the source text."))

    requester = request.requester
    if isinstance(requester, Requester):
        check_text(requester.name, "requester.name")
        check_text(requester.contact, "requester.contact")
    if isinstance(request.items, (tuple, list)):
        for index, item in enumerate(request.items):
            if not isinstance(item, RequestedItem):
                continue
            path = f"items[{index}]"
            check_text(item.description, f"{path}.description")
            check_text(item.unit, f"{path}.unit")
            check_text(item.notes, f"{path}.notes")
            if is_number(item.quantity) and is_finite(item.quantity):
                value = to_decimal(item.quantity)
                if value > 0 and not _number_in_text(value, haystack):
                    findings.append(
                        _finding(FindingCode.VALUE_NOT_IN_SOURCE, f"{path}.quantity", "Value was not found in the source text.")
                    )
    check_text(request.notes, "notes")


def _number_in_text(value: Decimal, normalized_text: str) -> bool:
    """True when the plain-notation form of the positive finite ``value`` occurs as a standalone number in the text.

    RESOURCE SAFETY. The plain form is only built when it is safe and possible to find: (1) a value whose plain
    form would need more than ``MAX_PADDING_ZEROS`` padding zeros (an enormous positive or negative exponent) is
    never formatted; (2) a value whose plain form is longer than the text cannot occur in it, so it is not
    formatted either. Both are decided from the digits and exponent alone (``quantity.plain_padding_zeros`` and
    ``plain_notation_length``), so no string larger than the source text is ever allocated. Both answer "not
    found", which is a WARNING here: validation stays a business check and never raises a safety error. A value
    beyond the padding bound is then refused by canonicalization (``SerializationSafetyError``), the serialization
    safety layer, so it can never become a revision.
    """
    if plain_padding_zeros(value) > MAX_PADDING_ZEROS or plain_notation_length(value) > len(normalized_text):
        return False
    pattern = r"(?<![0-9.])" + re.escape(format(value, "f")) + r"(?![0-9]|\.[0-9])"
    return re.search(pattern, normalized_text) is not None


# --- source references -------------------------------------------------------


def _check_source_ref_types(source_refs, findings):
    if not isinstance(source_refs, (tuple, list)):
        findings.append(_finding(FindingCode.INVALID_TYPE, "source_refs", "Source references must be a list."))
        return
    for index, ref in enumerate(source_refs):
        if not isinstance(ref, SourceRef):
            findings.append(_finding(FindingCode.INVALID_TYPE, f"source_refs[{index}]", "Reference must be a SourceRef."))


def _check_source_refs(request, source_text, findings):
    """Validate ``source_refs`` against the actual source text.

    Only the fixture coordinate convention is checked (see ``SourceRef``): a known value
    path, an existing item, a 1-based ``str.splitlines()`` line number within range, and an
    excerpt equal to ``make_excerpt`` of that line. A reference without a line is only
    required to appear verbatim in the text. Nothing more general (for example fuzzy
    matching of quotes produced by a language model) is claimed or checked.
    """
    refs = request.source_refs
    if not isinstance(refs, (tuple, list)):
        return
    lines = source_text.splitlines()
    items = request.items if isinstance(request.items, (tuple, list)) else None

    for index, ref in enumerate(refs):
        if not isinstance(ref, SourceRef):
            continue  # already reported as invalid_type
        where = f"source_refs[{index}]"
        parsed = parse_field_path(ref.field_path)
        if parsed is None or not parsed.is_field:
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference does not name a known value field."))
            continue
        if parsed.item_index is not None and (items is None or parsed.item_index >= len(items)):
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference names an item that does not exist."))
            continue
        if not isinstance(ref.excerpt, str):
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference excerpt must be text."))
            continue
        if ref.line is None:
            if ref.excerpt not in source_text:
                findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference excerpt does not appear in the source text."))
            continue
        if isinstance(ref.line, bool) or not isinstance(ref.line, int):
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference line must be an integer."))
            continue
        if not 1 <= ref.line <= len(lines):
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference line is outside the source text."))
            continue
        if ref.excerpt != make_excerpt(lines[ref.line - 1]):
            findings.append(_finding(FindingCode.INVALID_SOURCE_REF, where, "Reference excerpt does not match the cited line."))


# --- ambiguities reported by the extractor -----------------------------------


def _add_ambiguity_findings(ambiguities, findings):
    """Blocking for required fields, a warning for optional ones (one code per severity)."""
    for ambiguity in ambiguities:
        if not isinstance(ambiguity, Ambiguity):
            raise TypeError("ambiguities must contain Ambiguity objects")
        parsed = parse_field_path(ambiguity.field_path)
        if parsed is None:
            raise ValueError("an Ambiguity must name a known field path")
        if parsed.required:
            code = FindingCode.AMBIGUOUS_REQUIRED_VALUE
            message = "A required value in the source could not be read unambiguously."
        else:
            code = FindingCode.AMBIGUOUS_OPTIONAL_VALUE
            message = "An optional value in the source could not be read unambiguously."
        findings.append(Finding(code=code, field_path=ambiguity.field_path, message=message, excerpt=ambiguity.excerpt))


# --- helpers -----------------------------------------------------------------


def _finding(code, path, message):
    return Finding(code=code, field_path=path, message=message)


def _missing(path, message):
    return Finding(code=FindingCode.MISSING_REQUIRED, field_path=path, message=message)
