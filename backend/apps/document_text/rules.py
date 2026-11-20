"""The pure rules of PDF text extraction (DOC-2): vocabularies, limits, the result shape, text sanitizing, canonical hashing and the strict validation of what the isolated child process reports.
Standard library only: no Django, no database, no I/O, no PDF library. The child process (``child.py``) and the worker both import it.

WHAT AN EXTRACTION IS. A deterministic, page-by-page read of the text layer that a PDF stores. There is no OCR, no AI and no guess: text that is not in the file is never produced. A page without a text layer
is ``empty`` (not an error); a page the library could not read is ``failed`` with a code and NO text; text that exceeds a limit is never cut short and stored as if it were whole: the page is ``failed`` with
``page_text_too_long`` or ``text_limit_exceeded``.

STATUSES. A page is ``text``, ``empty`` or ``failed``. The result as a whole is ``extracted`` (every page read, at least one with text), ``partial`` (some pages text, some failed), ``empty`` (the file has no
pages or no page has text) or ``failed`` (nothing usable: a document-level ``error_code``, and then NO page rows). ``error_code`` is empty exactly when the result is ``extracted``; ``empty`` and ``partial`` carry an
informational code (``no_pages``, ``no_text``, ``some_pages_failed``) so a reader never has to infer why.

TEXT FACTS. Extraction order is the order the PDF's content stream draws the text in, as the library reports it (``extract_text()`` in its default mode). That is the reading order only where the producer drew
the page in reading order; columns, tables, text boxes and rotated text may come out in another order. Line ends are normalized to ``\\n``; NUL and the other control characters (except ``\\n`` and ``\\t``) and
lone surrogates are REMOVED (PostgreSQL cannot store NUL and a lone surrogate is not text); nothing else is changed: no Unicode normalization, no whitespace trimming, no de-hyphenation. Bidirectional control
characters are kept: a display that shows extracted text must neutralize them.

DETERMINISM. The same bytes, the same limits and the same library version give the same pages and the same ``text_sha256`` (SHA-256 of the canonical JSON of the pages). A different library version may differ.

PROVENANCE. A page's source is ``document:<document id>:sha256:<hash of the stored bytes>:page:<number>``: it names the exact bytes the page was read from.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass

EXTRACTOR_NAME = "pypdf"
KIND_EXTRACT_TEXT = "documents.extract_text"

RESULT_EXTRACTED = "extracted"
RESULT_PARTIAL = "partial"
RESULT_EMPTY = "empty"
RESULT_FAILED = "failed"
RESULT_STATUSES = (RESULT_EXTRACTED, RESULT_PARTIAL, RESULT_EMPTY, RESULT_FAILED)

PAGE_TEXT = "text"
PAGE_EMPTY = "empty"
PAGE_FAILED = "failed"
PAGE_STATUSES = (PAGE_TEXT, PAGE_EMPTY, PAGE_FAILED)

# What the API reports before there is a result.
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATES = (STATE_QUEUED, STATE_RUNNING, *RESULT_STATUSES)

# Document-level failure codes (a ``failed`` result).
ERR_NOT_AUTHORIZED = "not_authorized"  # the requester may no longer read the document when the worker runs
ERR_DOCUMENT_MISSING = "document_missing"  # the metadata exists but the stored object does not (or does not match its recorded size)
ERR_INTEGRITY_MISMATCH = "integrity_mismatch"  # the stored bytes do not hash to the recorded SHA-256
ERR_DOCUMENT_TOO_LARGE = "document_too_large"  # larger than the extraction's own size limit
ERR_ENCRYPTED = "encrypted_pdf"  # encrypted: no password or key is supported, so it is never read
ERR_MALFORMED = "malformed_pdf"  # the library could not parse the file (damaged, truncated or hostile)
ERR_TOO_MANY_PAGES = "too_many_pages"
ERR_TIMEOUT = "extraction_timeout"
ERR_RESOURCE_LIMIT = "resource_limit"  # the isolated process hit its memory or CPU limit, or the library's own safety limit
ERR_EXTRACTOR_FAILED = "extractor_failed"  # the isolated process died or answered something invalid, or no page could be read
ERR_UNAVAILABLE = "extraction_unavailable"  # every attempt failed for a reason outside the document (storage, the isolated process could not start)
DOCUMENT_ERRORS = (
    ERR_NOT_AUTHORIZED,
    ERR_DOCUMENT_MISSING,
    ERR_INTEGRITY_MISMATCH,
    ERR_DOCUMENT_TOO_LARGE,
    ERR_ENCRYPTED,
    ERR_MALFORMED,
    ERR_TOO_MANY_PAGES,
    ERR_TIMEOUT,
    ERR_RESOURCE_LIMIT,
    ERR_EXTRACTOR_FAILED,
    ERR_UNAVAILABLE,
)
# Informational codes of a result that is not ``failed`` but not complete text either.
INFO_NO_PAGES = "no_pages"
INFO_NO_TEXT = "no_text"
INFO_SOME_PAGES_FAILED = "some_pages_failed"
RESULT_INFO = (INFO_NO_PAGES, INFO_NO_TEXT, INFO_SOME_PAGES_FAILED)
# Page-level failure codes.
PAGE_ERR_FAILED = "page_extraction_failed"
PAGE_ERR_TOO_LONG = "page_text_too_long"
PAGE_ERR_TEXT_LIMIT = "text_limit_exceeded"
PAGE_ERRORS = (PAGE_ERR_FAILED, PAGE_ERR_TOO_LONG, PAGE_ERR_TEXT_LIMIT)
RESULT_ERROR_CODES = (*DOCUMENT_ERRORS, *RESULT_INFO)
ERROR_CODE_SHAPE = r"^[a-z][a-z0-9_]{0,39}$"  # what the database checks; which codes exist is this module's rule

# A child may report only these in place of a result.
FATAL_RESOURCE_LIMIT = "resource_limit"
FATAL_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
FATAL_CODES = (FATAL_RESOURCE_LIMIT, FATAL_SANDBOX_UNAVAILABLE)

SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
_KEPT_CONTROLS = "\n\t"
_REMOVED_CATEGORIES = frozenset({"Cc", "Cs"})

MIB = 1024 * 1024


@dataclass(frozen=True)
class ExtractionLimits:
    """Every bound of one extraction. The defaults are provisional (not measured on real documents); a bound outside its range is a configuration error, never clamped."""

    max_bytes: int = 20 * MIB  # the size of the stored PDF
    max_pages: int = 200
    max_page_chars: int = 100_000
    max_total_chars: int = 500_000
    timeout_seconds: int = 20  # wall clock for the whole isolated process, start-up included; its CPU time is limited to the same number of seconds
    memory_bytes: int = 1024 * MIB  # address space (POSIX) or committed memory (Windows) of the isolated process

    RANGES = {
        "max_bytes": (1024, 100 * MIB),
        "max_pages": (1, 1000),
        "max_page_chars": (1, 1_000_000),
        "max_total_chars": (1, 5_000_000),
        "timeout_seconds": (1, 120),
        "memory_bytes": (128 * MIB, 4096 * MIB),
    }

    def __post_init__(self):
        for name, (low, high) in self.RANGES.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        if self.max_page_chars > self.max_total_chars:
            raise ValueError("max_page_chars must not exceed max_total_chars")

    @property
    def max_output_bytes(self) -> int:
        """The most the isolated process may write back: four bytes per character plus a fixed allowance per page and for the envelope."""
        return 4 * self.max_total_chars + 256 * self.max_pages + 64 * 1024


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based, as the reader counts pages
    status: str
    error_code: str
    text: str

    def __post_init__(self):
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number < 1:
            raise ValueError("page_number must be an integer of at least 1")
        if self.status not in PAGE_STATUSES or not isinstance(self.text, str) or not isinstance(self.error_code, str):
            raise ValueError("invalid page")
        if self.status == PAGE_FAILED:
            if self.error_code not in PAGE_ERRORS or self.text != "":
                raise ValueError("a failed page has a page error code and no text")
        elif self.error_code != "" or (self.status == PAGE_TEXT) != (self.text.strip() != ""):
            raise ValueError("a text page has text and an empty page has none; neither has an error code")
        if sanitize_text(self.text) != self.text:
            raise ValueError("page text is not sanitized")

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class ExtractionOutput:
    """What one extraction produced. ``pages`` is empty for a ``failed`` result; ``page_count`` is None when the page count was never established."""

    status: str
    error_code: str
    extractor_name: str
    extractor_version: str
    page_count: int | None
    pages: tuple

    def __post_init__(self):
        if self.status not in RESULT_STATUSES:
            raise ValueError("unknown result status")
        if (self.status == RESULT_EXTRACTED) != (self.error_code == ""):
            raise ValueError("error_code is empty exactly when the result is extracted")
        if self.status == RESULT_FAILED and (self.error_code not in DOCUMENT_ERRORS or self.pages):
            raise ValueError("a failed result has a document error code and no pages")
        if self.status in (RESULT_EMPTY, RESULT_PARTIAL) and self.error_code not in RESULT_INFO:
            raise ValueError("an empty or partial result has an informational code")
        if self.extractor_name != EXTRACTOR_NAME or _VERSION.fullmatch(self.extractor_version or "") is None:
            raise ValueError("invalid extractor identity")
        if self.status != RESULT_FAILED:
            if self.page_count != len(self.pages):
                raise ValueError("the page count must equal the number of pages")
            if tuple(page.page_number for page in self.pages) != tuple(range(1, len(self.pages) + 1)):
                raise ValueError("pages must be numbered 1..n in order")
            if derive_status(self.pages) != (self.status, self.error_code):
                raise ValueError("the result status does not follow from its pages")

    @property
    def char_count(self) -> int:
        return sum(page.char_count for page in self.pages)

    @property
    def text_sha256(self) -> str:
        return pages_sha256(self.pages)


def failed_output(error_code: str, *, extractor_version: str, page_count: int | None = None) -> ExtractionOutput:
    return ExtractionOutput(RESULT_FAILED, error_code, EXTRACTOR_NAME, extractor_version, page_count, ())


def derive_status(pages) -> tuple:
    """``(status, error_code)`` that follows from the pages of a result that was read. All pages failed counts as a failed document, which carries no pages: it is reported by ``extractor_failed`` instead."""
    if not pages:
        return RESULT_EMPTY, INFO_NO_PAGES
    with_text = any(page.status == PAGE_TEXT for page in pages)
    failed = any(page.status == PAGE_FAILED for page in pages)
    if failed:
        return (RESULT_PARTIAL, INFO_SOME_PAGES_FAILED) if with_text else (RESULT_FAILED, ERR_EXTRACTOR_FAILED)
    return (RESULT_EXTRACTED, "") if with_text else (RESULT_EMPTY, INFO_NO_TEXT)


def output_from_pages(pages, *, extractor_version: str) -> ExtractionOutput:
    """The result for pages that were read: its status and code follow from the pages (never chosen by a caller)."""
    pages = tuple(pages)
    status, code = derive_status(pages)
    if status == RESULT_FAILED:
        return failed_output(code, extractor_version=extractor_version, page_count=len(pages))
    return ExtractionOutput(status, code, EXTRACTOR_NAME, extractor_version, len(pages), pages)


def sanitize_text(text: str) -> str:
    """Normalize line ends to ``\\n`` and remove NUL, the other control characters except ``\\n`` and ``\\t``, and lone surrogates. Nothing else is touched. Idempotent."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _REMOVED_CATEGORIES)


def pages_sha256(pages) -> str:
    """SHA-256 of the canonical JSON of the pages (number, status, code, text): the same pages always give the same value."""
    document = [{"page_number": p.page_number, "status": p.status, "error_code": p.error_code, "text": p.text} for p in pages]
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_reference(document_id, sha256: str, page_number: int) -> str:
    """A stable name for one page of one exact stored file: ``document:<id>:sha256:<hash>:page:<n>``."""
    if not isinstance(document_id, uuid.UUID) or not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        raise ValueError("invalid source")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be an integer of at least 1")
    return f"document:{document_id}:sha256:{sha256}:page:{page_number}"


class ChildOutputError(ValueError):
    """What the isolated process wrote is not a valid answer. The message names the rule, never the content."""


class ChildFatal(Exception):
    """The isolated process reported that it cannot run (``code`` is one of ``FATAL_CODES``)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _expect(condition, rule: str) -> None:
    if not condition:
        raise ChildOutputError(rule)


def parse_child_output(raw: bytes, limits: ExtractionLimits) -> ExtractionOutput:
    """Strictly validate what the child wrote. The child parses hostile input, so its answer is untrusted too: shape, types, closed vocabularies, bounds and consistency are all checked here, and anything else is
    ``ChildOutputError``. ``ChildFatal`` for a ``fatal`` answer."""
    _expect(isinstance(raw, (bytes, bytearray)) and 0 < len(raw) <= limits.max_output_bytes, "size")
    try:
        document = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ChildOutputError("json") from None
    _expect(isinstance(document, dict) and document.get("version") == 1, "envelope")
    if "fatal" in document:
        _expect(set(document) == {"version", "fatal"} and document["fatal"] in FATAL_CODES, "fatal")
        raise ChildFatal(document["fatal"])
    _expect(set(document) == {"version", "status", "error_code", "extractor_name", "extractor_version", "page_count", "pages"}, "keys")
    status, code, pages_raw, page_count = document["status"], document["error_code"], document["pages"], document["page_count"]
    _expect(isinstance(status, str) and isinstance(code, str) and isinstance(pages_raw, list), "types")
    _expect(isinstance(document["extractor_name"], str) and isinstance(document["extractor_version"], str), "types")
    _expect(page_count is None or (isinstance(page_count, int) and not isinstance(page_count, bool) and 0 <= page_count <= 100_000_000), "page_count")
    _expect(len(pages_raw) <= limits.max_pages, "pages")
    pages, total = [], 0
    for index, item in enumerate(pages_raw, start=1):
        _expect(isinstance(item, dict) and set(item) == {"page_number", "status", "error_code", "text"}, "page")
        _expect(item["page_number"] == index and isinstance(item["page_number"], int) and not isinstance(item["page_number"], bool), "page_number")
        _expect(all(isinstance(item[key], str) for key in ("status", "error_code", "text")), "page types")
        _expect(len(item["text"]) <= limits.max_page_chars, "page text limit")
        total += len(item["text"])
        _expect(total <= limits.max_total_chars, "total text limit")
        try:
            pages.append(PageText(item["page_number"], item["status"], item["error_code"], item["text"]))
        except ValueError:
            raise ChildOutputError("page") from None
    try:
        return ExtractionOutput(status, code, document["extractor_name"], document["extractor_version"], page_count, tuple(pages))
    except ValueError:
        raise ChildOutputError("result") from None


def encode_child_output(output: ExtractionOutput) -> bytes:
    """What the child writes for ``output`` (UTF-8 JSON; the inverse of ``parse_child_output``)."""
    document = {
        "version": 1,
        "status": output.status,
        "error_code": output.error_code,
        "extractor_name": output.extractor_name,
        "extractor_version": output.extractor_version,
        "page_count": output.page_count,
        "pages": [{"page_number": p.page_number, "status": p.status, "error_code": p.error_code, "text": p.text} for p in output.pages],
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_fatal(code: str) -> bytes:
    if code not in FATAL_CODES:
        raise ValueError("unknown fatal code")
    return json.dumps({"version": 1, "fatal": code}, separators=(",", ":")).encode("ascii")
