"""A structural ENVELOPE check for a PDF: pure, standard library only, bounded work, no dependency.

WHAT IT PROVES. Only that the file is shaped like a complete PDF and not obviously cut off: it starts with a ``%PDF-1.0`` to ``%PDF-1.7`` or ``%PDF-2.0`` header at byte 0 (so a polyglot with
leading bytes is refused), ends with ``%%EOF`` within the last 1 KiB, has a ``startxref`` offset before it that lies inside the file and points at a cross-reference table (``xref`` followed by a
subsection header) or a cross-reference stream (``N G obj`` whose dictionary names ``/XRef`` and ``/Size``), and, for a classic table, a ``trailer`` dictionary with ``/Size`` before the
``startxref``. A truncated upload (no end marker) and a file that merely begins with ``%PDF-`` fail it.

WHAT IT DOES NOT PROVE. It does not parse objects, decompress streams, check the page tree, detect encryption, scripts, embedded files or malware, or guarantee that any reader can open the file.
A file can pass and still be a hostile or damaged PDF. Whatever later reads the content (OCR, extraction) must treat it as untrusted and parse it in its own, isolated step. No claim of
safe content is made anywhere in the product.

BOUNDED. It reads the 9-byte head, at most the last 64 KiB and at most 4 KiB at the ``startxref`` target, with ``bytes.find`` and a few anchored regular expressions with no nested quantifiers.
"""

import re

from .errors import REASON_MALFORMED_PDF, REASON_NOT_A_PDF, REASON_TRUNCATED_PDF, DocumentRejectedError

HEADER_BYTES = 9
TAIL_BYTES = 64 * 1024
EOF_WINDOW = 1024
TARGET_BYTES = 4096
_WHITESPACE = b"\x00\t\n\x0c\r "
_HEADER = re.compile(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n\t ]")
_STARTXREF_VALUE = re.compile(rb"[\x00\t\n\x0c\r ]*([0-9]{1,12})[\x00\t\n\x0c\r ]*")
_XREF_TABLE = re.compile(rb"xref[\x00\t\n\x0c\r ]+[0-9]{1,10}[ \t]+[0-9]{1,10}")
_XREF_STREAM = re.compile(rb"[0-9]{1,10}[\x00\t\n\x0c\r ]+[0-9]{1,5}[\x00\t\n\x0c\r ]+obj")


def check_header(prefix: bytes) -> None:
    """``not_a_pdf`` unless ``prefix`` (at least the first 9 bytes of the file) starts with a PDF header. Called on the first bytes received, so garbage is refused before it is written."""
    if _HEADER.match(prefix[:HEADER_BYTES]) is None:
        raise DocumentRejectedError(REASON_NOT_A_PDF)


def validate_pdf(read_at, size: int) -> None:
    """Check the envelope of a stored file. ``read_at(offset, length)`` returns up to ``length`` bytes at ``offset`` (fewer only at the end of the file); ``size`` is the file's length.

    Raises ``DocumentRejectedError`` with ``not_a_pdf`` (no header), ``truncated_pdf`` (no end marker or ``startxref``) or ``malformed_pdf`` (the offset or the structure it points at is wrong).
    """
    if size < HEADER_BYTES:
        raise DocumentRejectedError(REASON_NOT_A_PDF)
    check_header(read_at(0, HEADER_BYTES))
    window = min(size, TAIL_BYTES)
    tail = read_at(size - window, window)
    if len(tail) != window:
        raise DocumentRejectedError(REASON_TRUNCATED_PDF)
    eof = tail.rfind(b"%%EOF")
    if eof < 0 or window - eof > EOF_WINDOW + len(b"%%EOF"):
        raise DocumentRejectedError(REASON_TRUNCATED_PDF)
    start = tail.rfind(b"startxref", 0, eof)
    if start < 0:
        raise DocumentRejectedError(REASON_TRUNCATED_PDF)
    value = _STARTXREF_VALUE.fullmatch(tail[start + len(b"startxref") : eof])
    if value is None:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)
    offset = int(value.group(1))
    if offset >= size:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)
    target = read_at(offset, TARGET_BYTES).lstrip(_WHITESPACE)
    if target.startswith(b"xref"):
        _check_table(target, tail[:start])
    elif _XREF_STREAM.match(target):
        _check_stream(target)
    else:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)


def _check_table(target: bytes, before_startxref: bytes) -> None:
    if _XREF_TABLE.match(target) is None:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)
    trailer = before_startxref.rfind(b"trailer")
    if trailer < 0 or before_startxref.find(b"/Size", trailer) < 0:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)


def _check_stream(target: bytes) -> None:
    body = target.split(b"stream", 1)[0]
    if b"/XRef" not in body or b"/Size" not in body:
        raise DocumentRejectedError(REASON_MALFORMED_PDF)
