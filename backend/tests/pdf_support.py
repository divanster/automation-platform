"""Build small synthetic PDFs for the text extraction tests (DOC-2). Everything here is made up: no real document is ever used. Standard library only.

``build_pdf`` writes a classic cross-reference table with correct offsets, so the file passes DOC-1's envelope check (``apps.documents.pdf.validate_pdf``) and a real reader opens it.
"""

import zlib


def _escape(text: str) -> bytes:
    return text.encode("cp1252").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def page_content(lines) -> bytes:
    """The content stream of a page that draws ``lines`` top to bottom in Helvetica 12 (no lines: a page with no text at all)."""
    if not lines:
        return b"q 0 0 612 792 re S Q"  # a drawn rectangle, no text
    out = [b"BT /F1 12 Tf 72 720 Td 14 TL"]
    for index, line in enumerate(lines):
        out.append((b"" if index == 0 else b"T* ") + b"(" + _escape(line) + b") Tj")
    out.append(b"ET")
    return b"\n".join(out)


def build_pdf(pages=(("Hello world",),), *, compress=False, encrypt=False, version="1.4", extra_trailer=b"", page_count_override=None, flate_streams=None) -> bytes:
    """A PDF with one page per item of ``pages`` (each item is a sequence of text lines, empty for a page without text).

    ``encrypt`` adds an ``/Encrypt`` dictionary to the trailer (the content is not actually encrypted: the file only claims it is, which is all a reader checks first).
    ``page_count_override`` writes a ``/Count`` that is not the real number of pages. ``flate_streams`` maps a page index to bytes that are written as that page's content stream as they are, with a
    ``/FlateDecode`` filter (to build a file whose stream does not inflate to a sensible page).
    """
    flate_streams = flate_streams or {}
    objects = {}
    page_ids = [4 + 2 * index for index in range(len(pages))]
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    count = len(pages) if page_count_override is None else page_count_override
    kids = b" ".join(b"%d 0 R" % number for number in page_ids)
    objects[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    for index, lines in enumerate(pages):
        page_id, content_id = page_ids[index], page_ids[index] + 1
        objects[page_id] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id
        body = page_content(lines)
        if index in flate_streams:
            body = flate_streams[index]
            objects[content_id] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body) + body + b"\nendstream"
        elif compress:
            body = zlib.compress(body)
            objects[content_id] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body) + body + b"\nendstream"
        else:
            objects[content_id] = b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"
    trailer_extra = extra_trailer
    if encrypt:
        encrypt_id = max(objects) + 1
        objects[encrypt_id] = b"<< /Filter /Standard /V 1 /R 2 /O (" + b"0" * 32 + b") /U (" + b"1" * 32 + b") /P -4 >>"
        trailer_extra += b" /Encrypt %d 0 R /ID [<00112233445566778899aabbccddeeff> <00112233445566778899aabbccddeeff>]" % encrypt_id
    output = bytearray(b"%PDF-" + version.encode("ascii") + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    start = len(output)
    size = max(objects) + 1
    output += b"xref\n0 %d\n" % size + b"0000000000 65535 f \n"
    for number in range(1, size):
        output += b"%010d 00000 n \n" % offsets[number]
    output += b"trailer\n<< /Size %d /Root 1 0 R" % size + trailer_extra + b" >>\nstartxref\n%d\n%%%%EOF\n" % start
    return bytes(output)
