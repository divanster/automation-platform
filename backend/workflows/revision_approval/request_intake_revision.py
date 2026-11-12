"""Create a ``ResultRevision`` from a request-intake draft.

THIS IS THE ONLY MODULE of the revision-approval package that depends on the request-intake
package. The generic revision, approval, and eligibility modules know nothing about request
intake; this integration module translates request-intake results into the generic
``RevisionFinding`` records they use. It is deliberately NOT re-exported from the package's
``__init__``: import it explicitly from ``workflows.revision_approval.request_intake_revision``.

The work goes through the existing ``canonicalize_request`` operation, so blocking validation
findings, the required source text for source references, and the serialization safety checks
cannot be skipped. Nothing is formatted or hashed here: this module neither duplicates JSON or
Decimal canonicalization nor invents another content-hash format.
"""

from uuid import UUID

from workflows.request_intake import StructuredRequest, ValidationLimits, ValidationResult, canonicalize_request

from .findings import RevisionFinding
from .identifiers import validate_revision_number, validate_uuid
from .revision import ResultRevision


def findings_from_validation(validation: ValidationResult) -> tuple[RevisionFinding, ...]:
    """Convert a request-intake validation result into generic revision findings, in order."""
    return tuple(
        RevisionFinding(
            code=finding.code.value,
            field_path=finding.field_path,
            blocking=finding.is_blocking,
            message=finding.message,
            excerpt=finding.excerpt,
        )
        for finding in validation.findings
    )


def create_request_intake_revision(
    request: StructuredRequest,
    *,
    case_id: UUID,
    run_id: UUID,
    revision_id: UUID,
    revision_number: int,
    limits: ValidationLimits | None = None,
    source_text: str | None = None,
    ambiguities=(),
) -> ResultRevision:
    """Canonicalize ``request`` and wrap the result as an immutable revision of one run.

    The caller supplies the UUIDs and the revision number; none is generated here. Exceptions
    from ``canonicalize_request`` propagate unchanged: ``DraftNotValidError`` for blocking
    findings, ``SourceTextRequiredError`` when source references have no source text, and
    ``SerializationSafetyError`` for values that cannot be serialized safely. The returned
    revision keeps the findings (including warnings) outside the hashed content.
    """
    validate_uuid(case_id, "case_id")
    validate_uuid(run_id, "run_id")
    validate_uuid(revision_id, "revision_id")
    validate_revision_number(revision_number)

    content = canonicalize_request(request, limits=limits, source_text=source_text, ambiguities=ambiguities)
    return ResultRevision(
        case_id=case_id,
        run_id=run_id,
        revision_id=revision_id,
        revision_number=revision_number,
        canonical_format_version=content.canonical_format_version,
        canonical_bytes=content.canonical_bytes,
        content_hash=content.content_hash,
        findings=findings_from_validation(content.validation),
    )
