"""The text extraction itself: bytes of an untrusted PDF in, an ``ExtractionOutput`` out. It runs ONLY inside the isolated child process (``child.py``), never in the web or worker process, because it parses
hostile input with a third-party library. It is a pure function of the bytes and the limits: no file, network, environment, clock or database access.

THE LIBRARY. ``pypdf`` (pure Python, BSD-3-Clause, pinned in ``requirements/runtime.in``). It reads the PDF's own text layer; it does not render, run or follow anything in the file: no JavaScript, no launch or URI
action, no embedded file, no form logic, no external program (``jbig2dec`` is switched off explicitly) and no network. Its own safety limits (decompressed stream sizes, page tree depth and size, parsing work)
are kept at their defaults or tightened here to a fraction of the memory limit.

ENCRYPTION. Any file with an ``/Encrypt`` entry is refused with ``encrypted_pdf``, even one the library could open with an empty password: no password or key is supported, and no cryptography package is installed.

WHAT IT DOES NOT DO. No OCR (a scanned page is ``empty``), no layout analysis, no table or column detection, no repair beyond what the library does by itself, no text from images or annotations or form fields.
"""

import io
import logging

import pypdf
from pypdf import Configuration, PdfReader, apply_configuration
from pypdf import errors as pypdf_errors

from .rules import (
    ERR_ENCRYPTED,
    ERR_MALFORMED,
    ERR_RESOURCE_LIMIT,
    ERR_TOO_MANY_PAGES,
    PAGE_EMPTY,
    PAGE_ERR_FAILED,
    PAGE_ERR_TEXT_LIMIT,
    PAGE_ERR_TOO_LONG,
    PAGE_FAILED,
    PAGE_TEXT,
    ExtractionLimits,
    ExtractionOutput,
    PageText,
    failed_output,
    output_from_pages,
    sanitize_text,
)

_STREAM_CAP_MAX = 75_000_000  # the library's own default


def _configuration(limits: ExtractionLimits) -> Configuration:
    """The library's limits for one extraction: every decompression and buffer cap is at most an eighth of the memory limit, and no external binary is ever called."""
    cap = min(_STREAM_CAP_MAX, limits.memory_bytes // 8)
    return pypdf.get_configuration().with_overwrites(
        maximum_declared_stream_length=cap,
        array_based_stream_maximum_output_length=cap,
        jbig2_maximum_output_length=cap,
        lzw_maximum_output_length=cap,
        run_length_maximum_output_length=cap,
        zlib_maximum_output_length=cap,
        brotli_maximum_output_length=cap,
        image_maximum_buffer_size=cap,
        jbig2dec_binary=None,
    )


def library_version() -> str:
    return pypdf.__version__


def _open(data: bytes, version: str, max_pages: int):
    """The reader and its page count, or the failed output that describes why neither is available."""
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return None, failed_output(ERR_ENCRYPTED, extractor_version=version)
        count = reader.get_num_pages()
    except MemoryError:
        raise
    except (pypdf_errors.DependencyError, pypdf_errors.FileNotDecryptedError, pypdf_errors.WrongPasswordError):
        return None, failed_output(ERR_ENCRYPTED, extractor_version=version)
    except pypdf_errors.LimitReachedError:
        return None, failed_output(ERR_RESOURCE_LIMIT, extractor_version=version)
    except Exception:  # noqa: BLE001 - a malformed or hostile file can make the library raise anything; only the fact matters
        return None, failed_output(ERR_MALFORMED, extractor_version=version)
    if not isinstance(count, int) or count < 0:
        return None, failed_output(ERR_MALFORMED, extractor_version=version)
    if count > max_pages:
        return None, failed_output(ERR_TOO_MANY_PAGES, extractor_version=version, page_count=count)
    return (reader, count), None


def extract_pdf_text(data: bytes, limits: ExtractionLimits) -> ExtractionOutput:
    """Extract the text of every page, in page order. ``MemoryError`` is not caught: the child reports it as a resource limit."""
    previous = logging.root.manager.disable  # the library logs warnings about damaged files; nothing here may be written anywhere (restored afterwards: the tests run this in-process)
    logging.disable(logging.CRITICAL)
    try:
        return _extract(data, limits)
    finally:
        logging.disable(previous)


def _extract(data: bytes, limits: ExtractionLimits) -> ExtractionOutput:
    version = library_version()
    with apply_configuration(_configuration(limits)):
        opened, failure = _open(data, version, limits.max_pages)
        if failure is not None:
            return failure
        reader, count = opened
        pages, total, exhausted = [], 0, False
        for number in range(1, count + 1):
            if exhausted:
                pages.append(PageText(number, PAGE_FAILED, PAGE_ERR_TEXT_LIMIT, ""))
                continue
            try:
                text = sanitize_text(reader.pages[number - 1].extract_text())
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001
                pages.append(PageText(number, PAGE_FAILED, PAGE_ERR_FAILED, ""))
                continue
            if len(text) > limits.max_page_chars:
                pages.append(PageText(number, PAGE_FAILED, PAGE_ERR_TOO_LONG, ""))
            elif total + len(text) > limits.max_total_chars:
                exhausted = True  # this page and every later one is reported, not cut
                pages.append(PageText(number, PAGE_FAILED, PAGE_ERR_TEXT_LIMIT, ""))
            else:
                total += len(text)
                pages.append(PageText(number, PAGE_TEXT if text.strip() else PAGE_EMPTY, "", text))
        return output_from_pages(pages, extractor_version=version)
