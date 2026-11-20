"""Pure rules for an uploaded document: names, declared type and length, limits, the storage key and the processing states. Standard library only; no Django, no database, no I/O.

THE FILE NAME IS A LABEL. It is user-provided data that is shown back and used in the download's ``Content-Disposition``; it is NEVER used to build a filesystem path (the storage key is random,
``new_storage_key``). A name that contains a path keeps only its last component, so ``..\\..\\x.pdf`` is the label ``x.pdf``; a name with a control, format, separator, surrogate, private-use
or unassigned character, an empty name, ``.`` or ``..`` is refused rather than altered, and so is a name that is too long or does not end in ``.pdf``.

THE TYPE IS CHECKED THREE WAYS (this module checks two; ``pdf.py`` and ``storage.py`` check the content): the declared ``Content-Type`` must be ``application/pdf``, the name must end in ``.pdf``,
and the bytes must be a structurally complete PDF. The declared type and the name are the client's claim; only the content check is evidence.

LIMITS. The size limit is explicit (``DEFAULT_MAX_BYTES``, configurable between ``MIN_MAX_BYTES`` and ``HARD_MAX_BYTES``); the hard maximum cannot be configured away.
"""

import re
import secrets
import unicodedata
from urllib.parse import unquote

from .errors import (
    REASON_EMPTY,
    REASON_INVALID_FILENAME,
    REASON_LENGTH_REQUIRED,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED_TYPE,
    DocumentRejectedError,
)

CONTENT_TYPE_PDF = "application/pdf"
EXTENSION_PDF = ".pdf"

STATE_UPLOADED = "uploaded"
STATE_PROCESSING = "processing"
STATE_PROCESSED = "processed"
STATE_FAILED = "failed"
# DOC-1 only ever sets ``uploaded``: nothing processes a document yet. The other states are the closed vocabulary later slices will use, so the database constraint does not have to change then.
PROCESSING_STATES = (STATE_UPLOADED, STATE_PROCESSING, STATE_PROCESSED, STATE_FAILED)

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MIN_MAX_BYTES = 1024
HARD_MAX_BYTES = 100 * 1024 * 1024

MAX_FILENAME_CHARS = 255
MAX_FILENAME_HEADER_CHARS = 4096  # a percent-encoded UTF-8 name: at most 4 bytes of 3 characters per character, with room to spare
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

STORAGE_KEY_PATTERN = r"[0-9a-f]{2}/[0-9a-f]{32}"
_STORAGE_KEY = re.compile(STORAGE_KEY_PATTERN)
SHA256_PATTERN = r"[0-9a-f]{64}"
_LENGTH = re.compile(r"[0-9]{1,12}")


def normalize_filename(raw) -> str:
    """The label to store for ``raw``, or ``invalid_filename`` / ``unsupported_type`` (the name does not end in ``.pdf``)."""
    if not isinstance(raw, str):
        raise DocumentRejectedError(REASON_INVALID_FILENAME)
    text = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not text or text in {".", ".."} or len(text) > MAX_FILENAME_CHARS:
        raise DocumentRejectedError(REASON_INVALID_FILENAME)
    if any(unicodedata.category(character) in _REFUSED_CATEGORIES for character in text):
        raise DocumentRejectedError(REASON_INVALID_FILENAME)
    if not text.lower().endswith(EXTENSION_PDF) or len(text) == len(EXTENSION_PDF):
        raise DocumentRejectedError(REASON_UNSUPPORTED_TYPE)
    return text


def filename_from_header(value) -> str:
    """The label for the percent-encoded UTF-8 name of the ``X-Document-Filename`` header (HTTP headers carry no portable non-ASCII text). A missing, over-long or undecodable value is ``invalid_filename``."""
    if not isinstance(value, str) or not value or len(value) > MAX_FILENAME_HEADER_CHARS:
        raise DocumentRejectedError(REASON_INVALID_FILENAME)
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DocumentRejectedError(REASON_INVALID_FILENAME) from None
    return normalize_filename(decoded)


def check_content_type(value) -> str:
    """``application/pdf`` (parameters are ignored); anything else, or nothing, is ``unsupported_type``."""
    if isinstance(value, str) and value.split(";", 1)[0].strip().lower() == CONTENT_TYPE_PDF:
        return CONTENT_TYPE_PDF
    raise DocumentRejectedError(REASON_UNSUPPORTED_TYPE)


def parse_content_length(value, max_bytes: int) -> int:
    """The declared body length as an integer, checked BEFORE a byte of the body is read: ``length_required`` when it is missing or not a plain number, ``empty`` for 0, ``too_large`` above ``max_bytes``."""
    if not isinstance(value, str) or _LENGTH.fullmatch(value.strip()) is None:
        raise DocumentRejectedError(REASON_LENGTH_REQUIRED)
    length = int(value.strip())
    if length == 0:
        raise DocumentRejectedError(REASON_EMPTY)
    if length > max_bytes:
        raise DocumentRejectedError(REASON_TOO_LARGE)
    return length


def is_valid_max_bytes(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_MAX_BYTES <= value <= HARD_MAX_BYTES


def new_storage_key() -> str:
    """A fresh random storage key, ``<2 hex>/<32 hex>`` (128 random bits): never derived from the name, the content or the owner."""
    token = secrets.token_hex(16)
    return f"{token[:2]}/{token}"


def is_storage_key(value) -> bool:
    return isinstance(value, str) and _STORAGE_KEY.fullmatch(value) is not None
