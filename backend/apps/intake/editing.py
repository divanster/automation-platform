"""The pure edit step: an edited draft in, a revision out (or a refusal). No database, no clock, no randomness.

Exactly like ``apps.intake.extraction``, nothing here validates, canonicalizes, hashes, or builds a revision
itself: ONE call of ``workflows.revision_approval.request_intake_revision.create_request_intake_revision``
does validation (against the stored source text, so source references are verified exactly as they are for
an extraction), canonicalization and hashing. Its refusals become ``DraftInvalidError``:

* ``blocking_findings`` (``DraftNotValidError``): the draft is invalid. It is NOT saved as a revision: a
  revision cannot hold blocking findings (open question 2.17), and the caller gets every finding back so
  they can fix the draft and submit it again.
* ``not_serializable`` (``SerializationSafetyError``): a value that passes validation but cannot be
  serialized safely (open question 2.13).

AMBIGUITIES ARE NOT CARRIED OVER. The extractor's ambiguities describe how it read the source text; a human
edit replaces that reading, so the edited revision's findings are exactly what validation reports for the
edited draft (duplicate items, values not found in the source, invalid source references, and so on).
Warnings are never removed by this module: each one validation reports is stored on the revision and has
to be acknowledged at approval.
"""

from uuid import UUID

from workflows.request_intake import DraftNotValidError, SerializationSafetyError, StructuredRequest, ValidationLimits
from workflows.revision_approval import ResultRevision
from workflows.revision_approval.request_intake_revision import create_request_intake_revision, findings_from_validation

from .errors import DraftInvalidError


def build_edit_revision(
    draft: StructuredRequest,
    *,
    source_text: str,
    case_id: UUID,
    run_id: UUID,
    revision_id: UUID,
    revision_number: int,
    limits: ValidationLimits | None = None,
) -> ResultRevision:
    """Validate and canonicalize ``draft`` as revision ``revision_number`` of the run, or raise ``DraftInvalidError``."""
    if not isinstance(draft, StructuredRequest):
        raise TypeError("draft must be a workflows.request_intake.StructuredRequest")
    try:
        return create_request_intake_revision(
            draft,
            case_id=case_id,
            run_id=run_id,
            revision_id=revision_id,
            revision_number=revision_number,
            limits=limits,
            source_text=source_text,
        )
    except DraftNotValidError as error:
        raise DraftInvalidError("blocking_findings", findings=findings_from_validation(error.validation)) from None
    except SerializationSafetyError as error:
        raise DraftInvalidError("not_serializable", path=error.path) from None
