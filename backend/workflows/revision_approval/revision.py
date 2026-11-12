"""An immutable result revision: identity plus the exact canonical bytes and their hash.

A ``ResultRevision`` ties the identifiers a caller supplies (case, workflow run, revision, and
revision number) to one specific piece of canonical content. It does not define or recompute
the canonical format: the bytes and hash come from a workflow's canonicalization operation
(for request intake, see ``request_intake_revision.py``), and this module only CHECKS that the
pieces agree with each other. It is generic: it imports no workflow-specific types.

All identifiers are ``uuid.UUID`` values supplied by the caller.

What this pure module does NOT do:

* It does not enforce database uniqueness (for example one revision number per run), durable
  immutability, ordering, or access control. A frozen dataclass cannot be modified through its
  public attributes, but copies can be made and a determined caller can bypass it, which is why
  ``consistency_problems`` can be re-run later (the export policy does).
* It does not generate identifiers, UUIDs, timestamps, or sequence numbers.
* It does not know which revision is the current one, or which run is current; callers
  supply that.

Findings are carried on the revision for review but are NOT part of the hashed business
content (they are not inside ``canonical_bytes``).

The only assumption about the bytes is the versioned-envelope convention: a JSON object that
carries an integer ``canonical_format_version``.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from .errors import InvalidRevisionError
from .findings import RevisionFinding
from .identifiers import is_valid_revision_number, is_valid_uuid

_HASH = re.compile(r"[0-9a-f]{64}")


class RevisionProblem(StrEnum):
    """Stable codes describing why a revision is not internally consistent."""

    INVALID_CASE_ID = "invalid_case_id"
    INVALID_RUN_ID = "invalid_run_id"
    INVALID_REVISION_ID = "invalid_revision_id"
    INVALID_REVISION_NUMBER = "invalid_revision_number"
    INVALID_FORMAT_VERSION = "invalid_format_version"
    INVALID_BYTES = "invalid_bytes"
    FORMAT_VERSION_MISMATCH = "format_version_mismatch"
    INVALID_HASH = "invalid_hash"
    HASH_MISMATCH = "hash_mismatch"
    INVALID_FINDINGS = "invalid_findings"
    BLOCKING_FINDINGS = "blocking_findings"


def _envelope_version(canonical_bytes):
    """The ``canonical_format_version`` written inside the bytes, or None if unreadable."""
    try:
        document = json.loads(canonical_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    version = document.get("canonical_format_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def _problems(revision):
    """Stable problem codes for ``revision``, in a fixed order (empty if consistent)."""
    problems = []
    if not is_valid_uuid(revision.case_id):
        problems.append(RevisionProblem.INVALID_CASE_ID)
    if not is_valid_uuid(revision.run_id):
        problems.append(RevisionProblem.INVALID_RUN_ID)
    if not is_valid_uuid(revision.revision_id):
        problems.append(RevisionProblem.INVALID_REVISION_ID)
    if not is_valid_revision_number(revision.revision_number):
        problems.append(RevisionProblem.INVALID_REVISION_NUMBER)

    version = revision.canonical_format_version
    version_ok = isinstance(version, int) and not isinstance(version, bool) and version >= 1
    if not version_ok:
        problems.append(RevisionProblem.INVALID_FORMAT_VERSION)

    content = revision.canonical_bytes
    bytes_ok = isinstance(content, bytes) and len(content) > 0
    if not bytes_ok:
        problems.append(RevisionProblem.INVALID_BYTES)
    else:
        written = _envelope_version(content)
        if written is None:
            problems.append(RevisionProblem.INVALID_BYTES)
        elif version_ok and written != version:
            problems.append(RevisionProblem.FORMAT_VERSION_MISMATCH)

    digest = revision.content_hash
    if not (isinstance(digest, str) and _HASH.fullmatch(digest)):
        problems.append(RevisionProblem.INVALID_HASH)
    elif bytes_ok and hashlib.sha256(content).hexdigest() != digest:
        problems.append(RevisionProblem.HASH_MISMATCH)

    findings = revision.findings
    if not (isinstance(findings, tuple) and all(isinstance(f, RevisionFinding) for f in findings)):
        problems.append(RevisionProblem.INVALID_FINDINGS)
    elif any(f.blocking for f in findings):
        problems.append(RevisionProblem.BLOCKING_FINDINGS)
    return tuple(problems)


@dataclass(frozen=True)
class ResultRevision:
    """One immutable result revision, scoped to a case and a workflow run.

    Create request-intake revisions with ``create_request_intake_revision`` so
    canonicalization and validation are not skipped. ``__post_init__`` refuses an inconsistent
    revision: identifiers that are not ``uuid.UUID``, an invalid revision number, a hash that
    is not the SHA-256 of ``canonical_bytes``, a format version that disagrees with the one
    written inside the bytes, or any blocking finding. A list of findings is copied into a tuple.
    """

    case_id: UUID
    run_id: UUID
    revision_id: UUID
    revision_number: int
    canonical_format_version: int
    canonical_bytes: bytes
    content_hash: str
    findings: tuple[RevisionFinding, ...] = ()  # carried for review; never part of the hashed bytes

    def __post_init__(self):
        if isinstance(self.findings, list):
            object.__setattr__(self, "findings", tuple(self.findings))
        problems = _problems(self)
        if problems:
            raise InvalidRevisionError(problems)

    def consistency_problems(self):
        """Re-check internal consistency now (the object could have been tampered with)."""
        return _problems(self)

    @property
    def blocking_findings(self):
        return tuple(f for f in self.findings if f.blocking)

    @property
    def warnings(self):
        """The non-blocking findings available for review (never removed or resolved)."""
        return tuple(f for f in self.findings if not f.blocking)
