"""Canonical serialization and content hashing of a request-intake draft: FORMAT VERSION 1.

This is an INTERNAL PREPARATION component, not an export feature. It defines the exact
bytes that a content hash covers, so that later phases (stored revisions, approvals) can
bind a decision to one specific reviewed content. Nothing here stores, signs, authorizes,
or exports anything.

This is the project's own, explicitly defined version-1 format. It is NOT claimed to be
compatible with RFC 8785 (JCS) or any other external canonical-JSON standard.

BUSINESS CONTENT (hashed): requester (name, contact), items in order (description,
quantity, unit, notes), notes, and source references in order (field_path, excerpt, line).

NOT hashed (future revision or audit metadata): validation findings, extractor-reported
ambiguities, the validation limits, the source text, extractor or provider identity and
version, timestamps, authors, and identifiers. Warnings stay available on
``CanonicalContent.validation`` and are never removed or treated as resolved.

SOURCE REFERENCES AND THE SOURCE TEXT. Source references are part of the hashed business
content, so they must be verified. If a draft has any source reference, ``source_text`` is
REQUIRED and every reference is validated against it; omitting it is refused with
``SourceTextRequiredError`` and verification is never silently skipped. A draft with no
source references may omit ``source_text``. The source text itself is NEVER added to the
payload. Consequently the hash fingerprints the included content only; it does not prove
the identity or authenticity of the original document. The identity and hash of the
original document remain future revision metadata.

THE BYTES. UTF-8 (no BOM) of one compact JSON object:

    {"canonical_format_version":1,"payload":{"items":[...],"notes":...,"requester":{...},"source_refs":[...]}}

* Fixed field names. Every schema-defined field is always present: an absent optional
  value is ``null``; nothing is omitted.
* ``null`` and the empty string ``""`` are different values and stay different.
* Text is preserved exactly: no trimming, case change, or Unicode normalization (NFC and
  NFD spellings are different content). Text must be encodable as UTF-8; a string with an
  unpaired surrogate is refused with ``SerializationSafetyError``, never repaired.
* Arrays keep their order (items, source references). Object keys are sorted by Unicode
  code point (all keys are fixed ASCII names), written with the separators "," and ":"
  and no whitespace.
* String escaping is that of Python's ``json`` module with ``ensure_ascii=False``: the
  quote, the backslash, and the control characters below U+0020 are escaped (the short
  forms for backspace, form feed, newline, carriage return and tab; otherwise backslash-u
  followed by four lowercase hex digits). Every other character, including U+007F,
  U+2028, U+2029 and all non-ASCII text, is written as raw UTF-8. Tests pin this.
* Quantities are strings in canonical plain decimal notation (see ``canonical_decimal_string``).
* ``line`` in a source reference is a JSON integer or ``null``.
* No floats, NaN, infinity, timestamps, or random values appear anywhere.

The hash is SHA-256 over exactly those bytes, as 64 lowercase hex characters. A hash is a
fingerprint of content. It is NOT a signature, a MAC, proof of authorship, or an
authorization; anyone can compute it.

VERSIONING. Version 1 fixes BOTH the canonicalization rules above AND the current payload
field structure (which fields exist, their names, and which are included). An incompatible
change to either, that is, to the canonicalization rules, the included fields, the payload
or schema structure, or the quantity representation, requires a new
``CANONICAL_FORMAT_VERSION``. The version is inside the hashed bytes, so a version-1 hash
can never equal a version-2 hash by accident. Historical bytes and hashes must never be
silently recomputed under new rules; a hash recorded under version 1 keeps its version.
There is deliberately no separate schema-version field in the version-1 envelope.

VALIDATION VERSUS SERIALIZATION SAFETY. A draft can pass business validation and still fail
canonical serialization: validation has no business limit on quantity magnitude or on
text encodability, but serialization refuses a quantity whose plain decimal form would need
an excessive allocation, and text that cannot be encoded as UTF-8. Those refusals are
``SerializationSafetyError``; they are not business rules and validation was not changed.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from .findings import ValidationResult
from .schema import StructuredRequest
from .validation import ValidationLimits, validate_request

CANONICAL_FORMAT_VERSION = 1
HASH_ALGORITHM = "sha256"

# SERIALIZATION SAFETY GUARD, NOT A BUSINESS LIMIT. A quantity whose plain decimal form
# would need more than this many padding zeros (from a very large exponent, for example
# 1E+1000000) is refused BEFORE any large string is built, instead of allocating it. The
# value mirrors the default decimal context's exponent range (Emax 999999). It does not
# limit the number of digits of the coefficient and is not a rule about how large a
# quantity may be: validation accepts such quantities and is not changed.
MAX_PADDING_ZEROS = 999_999

# Stable reasons carried by SerializationSafetyError.reason.
REASON_DECIMAL_EXPANSION = "decimal_expansion"
REASON_UNENCODABLE_TEXT = "unencodable_text"


def _safety_message(reason: str) -> str:
    if reason == REASON_DECIMAL_EXPANSION:
        return "a quantity's plain decimal form would be too large to serialize safely"
    if reason == REASON_UNENCODABLE_TEXT:
        return "text contains characters that cannot be encoded as UTF-8 (an unpaired surrogate)"
    raise ValueError("unknown serialization safety reason")


class CanonicalizationError(ValueError):
    """A draft cannot be given a canonical form. Messages never echo content."""


class DraftNotValidError(CanonicalizationError):
    """The draft has blocking validation findings. ``validation`` holds the full result."""

    def __init__(self, validation: ValidationResult):
        self.validation = validation
        super().__init__(f"The draft has {len(validation.blocking)} blocking validation finding(s) and cannot be canonicalized.")


class SourceTextRequiredError(CanonicalizationError):
    """The draft has source references but no ``source_text`` was supplied to verify them."""

    def __init__(self):
        super().__init__("The draft has source references, so source_text is required to verify them; it cannot be canonicalized without it.")


class SerializationSafetyError(CanonicalizationError):
    """A draft that may pass business validation fails a canonical-serialization safety check.

    ``reason`` is ``"decimal_expansion"`` (plain notation would need more than
    ``MAX_PADDING_ZEROS`` padding zeros) or ``"unencodable_text"`` (an unpaired surrogate).
    ``path`` is the field path when known. Nothing is rounded, truncated, replaced, or
    switched to exponent notation; the message never echoes content.
    """

    def __init__(self, reason: str, path: str | None = None):
        self.reason = reason
        self.path = path
        where = f" at {path}" if path else ""
        super().__init__(_safety_message(reason) + where)


@dataclass(frozen=True)
class CanonicalContent:
    """The result of canonicalization: version, exact bytes, and the hash OF THOSE BYTES.

    All three always come from one operation, and the hash is re-checked against the bytes on
    construction, so the hash cannot describe a different representation. ``validation``
    carries the validation result (including warnings) for convenience; it is not part of the
    bytes or the hash.
    """

    canonical_format_version: int
    canonical_bytes: bytes
    content_hash: str
    hash_algorithm: str
    validation: ValidationResult

    def __post_init__(self):
        if not isinstance(self.canonical_bytes, bytes):
            raise TypeError("canonical_bytes must be bytes")
        if self.hash_algorithm != HASH_ALGORITHM:
            raise ValueError("unsupported hash algorithm")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != self.content_hash:
            raise ValueError("content_hash does not match canonical_bytes")


def canonicalize_request(
    request: StructuredRequest,
    *,
    limits: ValidationLimits | None = None,
    source_text: str | None = None,
    ambiguities=(),
) -> CanonicalContent:
    """Validate ``request`` and return its canonical bytes and content hash.

    ``limits`` and ``ambiguities`` are passed to ``validate_request`` and decide only whether
    the draft may be canonicalized; they do not change the bytes.

    ``source_text`` is REQUIRED when the draft has any source reference (otherwise
    ``SourceTextRequiredError``) and is then used to validate every reference. It may be
    omitted when there are none. It never appears in the bytes.

    Raises ``DraftNotValidError`` if there is any blocking finding (including a blocking
    ambiguity or an invalid source reference), ``SerializationSafetyError`` if a value that
    validation accepts cannot be serialized safely, and ``CanonicalizationError`` for other
    representation failures. The draft is never modified.
    """
    references = request.source_refs if isinstance(request, StructuredRequest) else ()
    if isinstance(references, (tuple, list)) and len(references) > 0 and source_text is None:
        raise SourceTextRequiredError()

    validation = validate_request(request, limits=limits, source_text=source_text, ambiguities=ambiguities)
    if validation.blocking:
        raise DraftNotValidError(validation)

    document = {"canonical_format_version": CANONICAL_FORMAT_VERSION, "payload": _payload(request)}
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    canonical_bytes = text.encode("utf-8")  # every string was checked to be encodable, so this cannot fail
    return CanonicalContent(
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        canonical_bytes=canonical_bytes,
        content_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        hash_algorithm=HASH_ALGORITHM,
        validation=validation,
    )


def canonical_decimal_string(value: Decimal) -> str:
    """Canonical plain decimal string for a finite ``Decimal``.

    * Numerically equal values give the same string: ``1``, ``1.0``, ``1.00`` and ``1E+0``
      are all ``"1"``; ``2.50`` is ``"2.5"``; ``1E+3`` is ``"1000"``.
    * Plain notation only: never an exponent. No leading zeros except the single ``0``
      before a decimal point; no trailing zeros after the point; no ``+``; no trailing ``.``.
    * Zero (including negative zero) is ``"0"``; a negative value has a leading ``-``.
    * Exact: only digits of the coefficient are removed when they are trailing zeros of
      the value (which cannot change its value) and the exponent is applied as written. No
      rounding, no ``normalize`` or ``quantize``, no float, and no use of the active
      decimal context (the digits come from ``Decimal.as_tuple``), so the result does not
      depend on context precision or rounding mode.
    * Refused: non-finite values (``CanonicalizationError``), and values whose plain form
      would need more than ``MAX_PADDING_ZEROS`` padding zeros (``SerializationSafetyError``,
      reason ``"decimal_expansion"``). The size check happens BEFORE any padded string is
      built, so a refusal allocates essentially nothing.
    """
    if not isinstance(value, Decimal):
        raise TypeError("canonical_decimal_string requires a Decimal")
    if not value.is_finite():
        raise CanonicalizationError("a non-finite quantity cannot be serialized")

    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return "0"

    coefficient = list(digits)
    while coefficient[-1] == 0:  # trailing zeros of the coefficient: value-preserving
        coefficient.pop()
        exponent += 1

    # Size check from the digit count and exponent alone, before anything padded exists.
    if exponent >= 0:
        padding = exponent
    else:
        padding = max(0, -exponent - len(coefficient))
    if padding > MAX_PADDING_ZEROS:
        raise SerializationSafetyError(REASON_DECIMAL_EXPANSION)

    text = "".join(str(digit) for digit in coefficient)
    if exponent >= 0:
        body = text + "0" * exponent
    else:
        point = len(text) + exponent  # digits before the decimal point
        if point > 0:
            body = text[:point] + "." + text[point:]
        else:
            body = "0." + "0" * (-point) + text
    return "-" + body if sign else body


# --- payload -----------------------------------------------------------------


def _payload(request):
    requester = request.requester
    return {
        "requester": {
            "name": _text(requester.name, "requester.name"),
            "contact": _text(requester.contact, "requester.contact"),
        },
        "items": [_item(index, item) for index, item in enumerate(request.items)],
        "notes": _text(request.notes, "notes"),
        "source_refs": [_source_ref(index, ref) for index, ref in enumerate(request.source_refs)],
    }


def _item(index, item):
    path = f"items[{index}]"
    quantity = item.quantity
    if not isinstance(quantity, Decimal):
        # RequestedItem converts numbers to Decimal when it is constructed, so a valid draft
        # always has a Decimal here. Nothing is converted during canonicalization.
        raise CanonicalizationError(f"{path}.quantity must be a Decimal")
    try:
        quantity_text = canonical_decimal_string(quantity)
    except SerializationSafetyError as error:
        raise SerializationSafetyError(error.reason, f"{path}.quantity") from None
    return {
        "description": _text(item.description, f"{path}.description"),
        "quantity": quantity_text,
        "unit": _text(item.unit, f"{path}.unit"),
        "notes": _text(item.notes, f"{path}.notes"),
    }


def _source_ref(index, ref):
    # Validation (with the required source_text) already rejects mistyped references; these
    # checks are defense in depth so a regression there can never produce silent output.
    path = f"source_refs[{index}]"
    line = ref.line
    if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
        raise CanonicalizationError(f"{path}.line must be an integer or None")
    return {
        "field_path": _required_text(ref.field_path, f"{path}.field_path"),
        "excerpt": _required_text(ref.excerpt, f"{path}.excerpt"),
        "line": line,
    }


def _text(value, path):
    """A text value or None, preserved exactly; refused (not repaired) if not UTF-8 encodable."""
    if value is None:
        return None
    return _required_text(value, path)


def _required_text(value, path):
    if not isinstance(value, str):
        raise CanonicalizationError(f"{path} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SerializationSafetyError(REASON_UNENCODABLE_TEXT, path) from None
    return value
