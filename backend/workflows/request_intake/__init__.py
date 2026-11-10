"""Request intake: the first workflow's schema, validation, and extraction contract.

Plain Python, independent of Django, the database, the filesystem, the network, and
any AI provider. See docs/development/request-intake-slice.md for what exists and
what is deferred.
"""

from .extraction import DeterministicTestExtractor, ExtractionResult, RequestExtractor
from .findings import (
    AMBIGUITY_CODES,
    FINDINGS_CONTRACT_VERSION,
    INVALID_VALUE_CODES,
    MISSING_DATA_CODES,
    SEVERITY_BY_CODE,
    Finding,
    FindingCode,
    Severity,
    ValidationResult,
)
from .paths import FieldPath, parse_field_path
from .quantity import to_decimal
from .schema import (
    SOURCE_REF_EXCERPT_MAX,
    Ambiguity,
    CanonicalQuantity,
    Quantity,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    make_excerpt,
)
from .validation import ValidationLimits, validate_request

__all__ = [
    "AMBIGUITY_CODES",
    "Ambiguity",
    "CanonicalQuantity",
    "DeterministicTestExtractor",
    "ExtractionResult",
    "FINDINGS_CONTRACT_VERSION",
    "FieldPath",
    "Finding",
    "FindingCode",
    "INVALID_VALUE_CODES",
    "MISSING_DATA_CODES",
    "Quantity",
    "RequestExtractor",
    "RequestedItem",
    "Requester",
    "SEVERITY_BY_CODE",
    "SOURCE_REF_EXCERPT_MAX",
    "Severity",
    "SourceRef",
    "StructuredRequest",
    "ValidationLimits",
    "ValidationResult",
    "make_excerpt",
    "parse_field_path",
    "to_decimal",
    "validate_request",
]
