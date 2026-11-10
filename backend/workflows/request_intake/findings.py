"""Validation findings with stable codes, field paths, and a fixed severity.

THE INITIAL STABLE CONTRACT. Codes, their severities, and the field-path grammar
(``paths.py``) are what stored revisions, tests, and the review UI will refer to. They
are versioned by ``FINDINGS_CONTRACT_VERSION``. Adding a new code is compatible. Renaming
or removing a code, changing a code's severity, or changing the path grammar is
INCOMPATIBLE and requires bumping ``FINDINGS_CONTRACT_VERSION``, documenting the change,
and deciding what happens to findings already stored under the old version. A test pins
the whole table so such a change cannot happen silently.

Each code has exactly one severity (``SEVERITY_BY_CODE``), so a finding can never
disagree with its code. That is why blocking and warning ambiguity use different codes.

Field paths use dotted names and zero-based list indexes, for example
``requester.name``, ``items``, ``items[0].quantity``, ``notes``. Source-reference
findings use the path of the reference itself: ``source_refs[N]``.
"""

from dataclasses import dataclass
from enum import Enum, StrEnum

FINDINGS_CONTRACT_VERSION = 1


class Severity(Enum):
    BLOCKING = "blocking"  # prevents approval
    WARNING = "warning"  # shown to the reviewer; does not prevent approval


class FindingCode(StrEnum):
    # --- missing required data (blocking) ---
    MISSING_REQUIRED = "missing_required"
    # --- invalid values (blocking) ---
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    EXCEEDS_MAXIMUM = "exceeds_maximum"
    EXCEEDS_LENGTH = "exceeds_length"
    INVALID_SOURCE_REF = "invalid_source_ref"
    # --- ambiguity (one code per severity) ---
    AMBIGUOUS_REQUIRED_VALUE = "ambiguous_required_value"  # blocking
    AMBIGUOUS_OPTIONAL_VALUE = "ambiguous_optional_value"  # warning
    # --- advisory (warning) ---
    DUPLICATE_ITEM = "duplicate_item"
    VALUE_NOT_IN_SOURCE = "value_not_in_source"


SEVERITY_BY_CODE: dict[FindingCode, Severity] = {
    FindingCode.MISSING_REQUIRED: Severity.BLOCKING,
    FindingCode.INVALID_TYPE: Severity.BLOCKING,
    FindingCode.INVALID_VALUE: Severity.BLOCKING,
    FindingCode.EXCEEDS_MAXIMUM: Severity.BLOCKING,
    FindingCode.EXCEEDS_LENGTH: Severity.BLOCKING,
    FindingCode.INVALID_SOURCE_REF: Severity.BLOCKING,
    FindingCode.AMBIGUOUS_REQUIRED_VALUE: Severity.BLOCKING,
    FindingCode.AMBIGUOUS_OPTIONAL_VALUE: Severity.WARNING,
    FindingCode.DUPLICATE_ITEM: Severity.WARNING,
    FindingCode.VALUE_NOT_IN_SOURCE: Severity.WARNING,
}

MISSING_DATA_CODES = frozenset({FindingCode.MISSING_REQUIRED})
INVALID_VALUE_CODES = frozenset(
    {
        FindingCode.INVALID_TYPE,
        FindingCode.INVALID_VALUE,
        FindingCode.EXCEEDS_MAXIMUM,
        FindingCode.EXCEEDS_LENGTH,
        FindingCode.INVALID_SOURCE_REF,
    }
)
AMBIGUITY_CODES = frozenset({FindingCode.AMBIGUOUS_REQUIRED_VALUE, FindingCode.AMBIGUOUS_OPTIONAL_VALUE})


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    field_path: str
    message: str  # fixed explanatory text; never echoes the offending value
    excerpt: str | None = None  # only for source excerpts quoted by the extractor

    @property
    def severity(self) -> Severity:
        return SEVERITY_BY_CODE[self.code]

    @property
    def is_blocking(self) -> bool:
        return self.severity is Severity.BLOCKING


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.is_blocking)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.is_blocking)

    @property
    def is_valid(self) -> bool:
        """Valid means no blocking findings (first-workflow.md, section 7)."""
        return not self.blocking

    @property
    def missing_required(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.code in MISSING_DATA_CODES)

    @property
    def invalid_values(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.code in INVALID_VALUE_CODES)

    @property
    def ambiguities(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.code in AMBIGUITY_CODES)
