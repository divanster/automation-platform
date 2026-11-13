"""Pure rules for the request-intake orchestration: text limits, identifiers, task payload.

Standard library only: no Django, no database, no workflow package. The database operations in
``apps.intake.submission`` and ``apps.intake.services`` apply these rules; the PostgreSQL tests check
the result.

PROVISIONAL LIMIT, NOT MEASURED. ``MAX_REQUEST_TEXT_BYTES`` is a guess that keeps one request small
enough to read and review by hand and to store in a ``TextField``; it is not derived from real
documents (open question 2.2 asks for a measured value) and it is not a business rule. It applies to
the UTF-8 encoding of the text, and it is the only size bound on stored source text.
"""

import re
import uuid
from datetime import datetime

MAX_REQUEST_TEXT_BYTES = 65_536  # provisional

KIND_EXTRACT = "request_intake.extract"  # the task kind (handler allowlist key)
STEP_EXTRACT = "extract"  # the step name used in StepTask and in the idempotency key
FIRST_GENERATION = 1

OUTCOME_VALID = "valid"
OUTCOME_INVALID = "invalid"
OUTCOME_STATUSES = (OUTCOME_VALID, OUTCOME_INVALID)

# Task results are not business content: a status and identifiers only.
RESULT_DISCARDED = "discarded"

_CANONICAL_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class RequestTextError(ValueError):
    """The submitted text is refused. ``reason`` is a stable code; the message never echoes the text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The request text is not acceptable ({reason}).")


class InvalidTaskPayloadError(ValueError):
    """A task payload is not ``{"run_id": "<canonical lowercase uuid>"}``. Names the rule, never the value."""


def validate_request_text(text) -> str:
    """Return ``text`` unchanged if it may be stored as a request source, else raise ``RequestTextError``.

    Reasons: ``not_text`` (not a ``str``), ``blank`` (empty or whitespace only), ``contains_nul``
    (PostgreSQL text cannot hold NUL), ``unencodable`` (a lone surrogate: not UTF-8), and
    ``too_large`` (more than ``MAX_REQUEST_TEXT_BYTES`` bytes as UTF-8). The text is NOT changed:
    no trimming, no newline normalization. Source references of the extractor count lines in
    exactly this text.
    """
    if not isinstance(text, str):
        raise RequestTextError("not_text")
    if not text.strip():
        raise RequestTextError("blank")
    if "\x00" in text:
        raise RequestTextError("contains_nul")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise RequestTextError("unencodable") from None
    if len(encoded) > MAX_REQUEST_TEXT_BYTES:
        raise RequestTextError("too_large")
    return text


def extraction_idempotency_key(run_id: uuid.UUID, generation: int = FIRST_GENERATION) -> str:
    """The queue idempotency key of one extraction step: ``request_intake.extract:<run uuid>:<generation>``.

    Run, step, and retry generation, as docs/architecture/data-model.md describes. The queue's
    key alphabet allows ``.``, ``:``, ``-``, and hexadecimal digits.
    """
    if not isinstance(run_id, uuid.UUID):
        raise TypeError("run_id must be a uuid.UUID")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be an integer of at least 1")
    return f"{KIND_EXTRACT}:{run_id}:{generation}"


def extraction_payload(run_id: uuid.UUID) -> dict:
    """The task payload: the run id and nothing else. Business text never enters the queue."""
    if not isinstance(run_id, uuid.UUID):
        raise TypeError("run_id must be a uuid.UUID")
    return {"run_id": str(run_id)}


def validate_extraction_payload(payload) -> dict:
    """A clean copy of a valid extraction payload, or ``InvalidTaskPayloadError``.

    Exactly one field, ``run_id``, in canonical lowercase UUID text. The stored payload is not
    trusted: the worker validates it again before the handler runs.
    """
    if not isinstance(payload, dict):
        raise InvalidTaskPayloadError("payload must be an object")
    if set(payload) != {"run_id"}:
        raise InvalidTaskPayloadError("payload must have exactly the field run_id")
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not _CANONICAL_UUID_TEXT.fullmatch(run_id):
        raise InvalidTaskPayloadError("run_id must be a lowercase UUID in 8-4-4-4-12 form")
    return {"run_id": run_id}


def new_case_reference(now: datetime, token: str) -> str:
    """``REQ-YYYYMMDD-XXXXXXXX``: the UTC date plus eight uppercase hexadecimal characters of ``token``.

    Unique in the installation only by chance (32 random bits per day); the database unique
    constraint is the guarantee and the caller retries on a collision.
    """
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{8,}", token):
        raise ValueError("token must be at least eight hexadecimal characters")
    return f"REQ-{now:%Y%m%d}-{token[:8].upper()}"
