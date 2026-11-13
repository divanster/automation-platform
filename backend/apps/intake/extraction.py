"""The pure extraction step: text in, a decision out. No database, no clock, no randomness.

Everything business-related is delegated; nothing here validates, canonicalizes, hashes, or builds a
revision itself:

* the extractor is a ``workflows.request_intake.RequestExtractor`` (``DeterministicTestExtractor`` by
  default: a fixture parser, NOT an AI model);
* validation and canonicalization happen inside ONE call of
  ``workflows.revision_approval.request_intake_revision.create_request_intake_revision`` (which calls
  ``canonicalize_request``). A draft with blocking findings raises ``DraftNotValidError``, whose
  ``validation`` carries every finding, so validation is never run a second time here.

Revision number 1 is what an extraction produces. ``SerializationSafetyError`` and the other
``CanonicalizationError`` subclasses are NOT caught: a value that validates but cannot be serialized
is a permanent failure of the step, not an invalid draft (there is no finding code for it).
"""

from dataclasses import dataclass
from uuid import UUID

from workflows.request_intake import (
    Ambiguity,
    DeterministicTestExtractor,
    DraftNotValidError,
    RequestExtractor,
    StructuredRequest,
    ValidationLimits,
)
from workflows.revision_approval import ResultRevision, RevisionFinding
from workflows.revision_approval.request_intake_revision import create_request_intake_revision, findings_from_validation

EXTRACTION_REVISION_NUMBER = 1


@dataclass(frozen=True)
class ExtractionDecision:
    """What one extraction produced. ``revision`` is set exactly when the draft is valid."""

    extractor_name: str
    extractor_version: str
    draft: StructuredRequest
    ambiguities: tuple[Ambiguity, ...]
    findings: tuple[RevisionFinding, ...]  # every finding; the blocking ones only when invalid
    revision: ResultRevision | None

    @property
    def is_valid(self) -> bool:
        return self.revision is not None


def build_extraction(
    text: str,
    *,
    case_id: UUID,
    run_id: UUID,
    revision_id: UUID,
    extractor: RequestExtractor | None = None,
    limits: ValidationLimits | None = None,
) -> ExtractionDecision:
    """Extract, validate, and (when valid) canonicalize ``text`` into revision 1 of the run."""
    extractor = DeterministicTestExtractor() if extractor is None else extractor
    result = extractor.extract(text)
    try:
        revision = create_request_intake_revision(
            result.candidate,
            case_id=case_id,
            run_id=run_id,
            revision_id=revision_id,
            revision_number=EXTRACTION_REVISION_NUMBER,
            limits=limits,
            source_text=text,
            ambiguities=result.ambiguities,
        )
    except DraftNotValidError as error:
        return ExtractionDecision(
            extractor_name=result.extractor_name,
            extractor_version=result.extractor_version,
            draft=result.candidate,
            ambiguities=result.ambiguities,
            findings=findings_from_validation(error.validation),
            revision=None,
        )
    return ExtractionDecision(
        extractor_name=result.extractor_name,
        extractor_version=result.extractor_version,
        draft=result.candidate,
        ambiguities=result.ambiguities,
        findings=revision.findings,
        revision=revision,
    )
