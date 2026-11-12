"""A generic, workflow-independent finding carried on a revision.

The revision, approval, and eligibility modules must not depend on any workflow's own types
(for example the request-intake validation result). A workflow integration module converts
its findings into these plain records. They are kept on the revision for review and are NOT
part of the hashed content.
"""

from dataclasses import dataclass

from .errors import PolicyInputError


@dataclass(frozen=True)
class RevisionFinding:
    """One finding: a stable code, the field it concerns, and whether it blocks approval.

    ``message`` is fixed explanatory text. ``excerpt`` is optional source text quoted by an
    extractor; it is review material and is never hashed.
    """

    code: str
    field_path: str
    blocking: bool
    message: str = ""
    excerpt: str | None = None

    def __post_init__(self):
        if not isinstance(self.code, str) or self.code == "":
            raise PolicyInputError("a finding code must be a non-empty string")
        if not isinstance(self.field_path, str):
            raise PolicyInputError("a finding field_path must be a string")
        if not isinstance(self.blocking, bool):
            raise PolicyInputError("a finding's blocking flag must be a bool")
        if not isinstance(self.message, str):
            raise PolicyInputError("a finding message must be a string")
        if self.excerpt is not None and not isinstance(self.excerpt, str):
            raise PolicyInputError("a finding excerpt must be a string or None")
