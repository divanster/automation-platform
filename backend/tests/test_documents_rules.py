"""DB-free tests of the pure document rules (DOC-1): file names, the declared type and length, limits, the storage key, the processing states and the PDF envelope check."""

import unicodedata

from django.test import SimpleTestCase

from apps.documents import pdf, rules
from apps.documents.errors import (
    REASON_EMPTY,
    REASON_INVALID_FILENAME,
    REASON_LENGTH_REQUIRED,
    REASON_MALFORMED_PDF,
    REASON_NOT_A_PDF,
    REASON_TOO_LARGE,
    REASON_TRUNCATED_PDF,
    REASON_UNSUPPORTED_TYPE,
    REJECTION_REASONS,
    DocumentRejectedError,
)
from tests.documents_support import VALID_PDF, make_pdf, make_xref_stream_pdf


def reason_of(function, *args):
    try:
        function(*args)
    except DocumentRejectedError as error:
        return error.reason
    return None


def validate(data: bytes):
    def read_at(offset, length):
        return data[offset : offset + length]

    return pdf.validate_pdf(read_at, len(data))


class FilenameTests(SimpleTestCase):
    def test_a_plain_pdf_name_is_kept(self):
        for name in ("invoice.pdf", "My Invoice (2).PDF", "a.b.c.pdf", "x" * 251 + ".pdf", "résumé.pdf", "請求書.pdf"):
            with self.subTest(name=name):
                self.assertEqual(rules.normalize_filename(name), unicodedata.normalize("NFC", name))

    def test_a_path_keeps_only_its_last_component(self):
        for raw, label in (
            ("../../etc/passwd.pdf", "passwd.pdf"),
            ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
            ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
            ("/var/data/x.pdf", "x.pdf"),
            ("a/b\\c/d.pdf", "d.pdf"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(rules.normalize_filename(raw), label)

    def test_a_name_that_is_not_a_name_is_refused(self):
        for raw in ("", "   ", ".", "..", "dir/", "dir\\", "/", "a\x00.pdf", "a\n.pdf", "a\t.pdf", "a\x7f.pdf", "\u202etxt.pdf", "a\u2028b.pdf", "a\ud800.pdf", "a\ue000.pdf", "x" * 252 + ".pdf", None, 5, b"a.pdf"):
            with self.subTest(raw=raw):
                self.assertEqual(reason_of(rules.normalize_filename, raw), REASON_INVALID_FILENAME)

    def test_a_name_that_does_not_end_in_pdf_is_an_unsupported_type(self):
        for raw in ("a.txt", "a.pdf.exe", "a", "pdf", ".pdf", "a.pdf ", "a.docx"):
            with self.subTest(raw=raw):
                expected = REASON_UNSUPPORTED_TYPE if raw != "a.pdf " else None
                self.assertEqual(reason_of(rules.normalize_filename, raw), expected)  # surrounding space is trimmed, so "a.pdf " is "a.pdf"

    def test_the_name_is_normalized_to_nfc(self):
        self.assertEqual(rules.normalize_filename("e\u0301.pdf"), "\u00e9.pdf")


class HeaderFilenameTests(SimpleTestCase):
    def test_a_percent_encoded_utf8_name_is_decoded(self):
        self.assertEqual(rules.filename_from_header("caf%C3%A9%20menu.pdf"), "caf\u00e9 menu.pdf")
        self.assertEqual(rules.filename_from_header("plain.pdf"), "plain.pdf")

    def test_an_encoded_path_or_control_character_gets_the_same_treatment_as_a_plain_one(self):
        self.assertEqual(rules.filename_from_header("..%2F..%2Fx.pdf"), "x.pdf")
        self.assertEqual(rules.filename_from_header("..%5C..%5Cx.pdf"), "x.pdf")
        for value in ("a%00.pdf", "a%0A.pdf", "a%E2%80%AE.pdf"):
            with self.subTest(value=value):
                self.assertEqual(reason_of(rules.filename_from_header, value), REASON_INVALID_FILENAME)

    def test_a_missing_overlong_or_undecodable_value_is_refused(self):
        for value in (None, "", "%FF%FE.pdf", "%C3.pdf", "x" * (rules.MAX_FILENAME_HEADER_CHARS + 1)):
            with self.subTest(value=value if value is None or len(value) < 20 else "long"):
                self.assertEqual(reason_of(rules.filename_from_header, value), REASON_INVALID_FILENAME)


class ContentTypeAndLengthTests(SimpleTestCase):
    def test_only_application_pdf_is_accepted_whatever_the_case_or_parameters(self):
        for value in ("application/pdf", "Application/PDF", " application/pdf ", "application/pdf; charset=binary"):
            self.assertEqual(rules.check_content_type(value), "application/pdf")

    def test_every_other_type_is_refused(self):
        for value in (None, "", "text/plain", "application/octet-stream", "application/x-pdf", "image/png", "application/pdfx", "multipart/form-data; boundary=x", "application/json", 5):
            with self.subTest(value=value):
                self.assertEqual(reason_of(rules.check_content_type, value), REASON_UNSUPPORTED_TYPE)

    def test_a_declared_length_is_parsed_and_bounded_before_any_byte_is_read(self):
        self.assertEqual(rules.parse_content_length("1", 100), 1)
        self.assertEqual(rules.parse_content_length(" 100 ", 100), 100)
        self.assertEqual(reason_of(rules.parse_content_length, "101", 100), REASON_TOO_LARGE)
        self.assertEqual(reason_of(rules.parse_content_length, "0", 100), REASON_EMPTY)
        self.assertEqual(reason_of(rules.parse_content_length, "000", 100), REASON_EMPTY)
        for value in (None, "", "abc", "-1", "1.5", "1e3", "0x10", "+5", "9" * 13, 5, " "):
            with self.subTest(value=value):
                self.assertEqual(reason_of(rules.parse_content_length, value, 100), REASON_LENGTH_REQUIRED)

    def test_the_limit_is_explicit_and_bounded_and_the_hard_maximum_cannot_be_configured_away(self):
        self.assertEqual(rules.DEFAULT_MAX_BYTES, 20 * 1024 * 1024)
        self.assertTrue(rules.is_valid_max_bytes(rules.DEFAULT_MAX_BYTES))
        for value in (rules.MIN_MAX_BYTES, rules.HARD_MAX_BYTES):
            self.assertTrue(rules.is_valid_max_bytes(value))
        for value in (0, 1, rules.MIN_MAX_BYTES - 1, rules.HARD_MAX_BYTES + 1, -5, True, None, "100", 1.5):
            with self.subTest(value=value):
                self.assertFalse(rules.is_valid_max_bytes(value))
        self.assertEqual(rules.HARD_MAX_BYTES, 100 * 1024 * 1024)


class StorageKeyTests(SimpleTestCase):
    def test_a_new_key_has_the_exact_shape_and_is_random(self):
        keys = {rules.new_storage_key() for _ in range(200)}
        self.assertEqual(len(keys), 200)
        for key in keys:
            self.assertTrue(rules.is_storage_key(key))
            shard, name = key.split("/")
            self.assertEqual((len(shard), len(name), name[:2]), (2, 32, shard))

    def test_anything_that_is_not_exactly_a_key_is_refused(self):
        good = rules.new_storage_key()
        for value in (
            None, "", good + "\n", "\n" + good, " " + good, good.upper(), good + "/x", "../" + good, good.replace("/", "\\"), good.replace("/", "//"), "ab/" + "c" * 31, "ab/" + "c" * 33,
            "a/" + "c" * 32, "abc/" + "c" * 32, "zz/" + "g" * 32, "..", "ab/..", "../..", "ab/" + "c" * 32 + ".pdf", 5, b"ab/" + b"c" * 32, "/etc/passwd", "C:\\x",
        ):
            with self.subTest(value=value):
                self.assertFalse(rules.is_storage_key(value))

    def test_the_key_pattern_is_the_one_the_database_constraint_uses(self):
        from apps.documents.models import Document

        constraints = {c.name: c for c in Document._meta.constraints}
        self.assertEqual(constraints["documents_storage_key_shape"].condition.children[0], ("storage_key__regex", f"^{rules.STORAGE_KEY_PATTERN}$"))
        self.assertEqual(constraints["documents_sha256_shape"].condition.children[0], ("sha256__regex", f"^{rules.SHA256_PATTERN}$"))


class StatesAndReasonsTests(SimpleTestCase):
    def test_the_processing_states_are_a_closed_vocabulary_and_uploaded_is_first(self):
        self.assertEqual(rules.PROCESSING_STATES, ("uploaded", "processing", "processed", "failed"))
        self.assertEqual(rules.STATE_UPLOADED, "uploaded")

    def test_a_rejection_carries_only_a_known_reason_and_a_fixed_message(self):
        for reason in REJECTION_REASONS:
            error = DocumentRejectedError(reason)
            self.assertEqual((error.reason, str(error)), (reason, f"The document was rejected ({reason})."))
        with self.assertRaises(ValueError):
            DocumentRejectedError("because /etc/passwd")


class PdfEnvelopeTests(SimpleTestCase):
    def test_a_complete_pdf_with_a_classic_table_passes(self):
        validate(VALID_PDF)
        validate(make_pdf(padding=100_000))
        validate(make_pdf(marker=b"%%EOF startxref 5 %%EOF"))  # content that merely looks like markers

    def test_a_cross_reference_stream_pdf_passes(self):
        validate(make_xref_stream_pdf())

    def test_every_supported_header_version_passes_and_others_do_not(self):
        for version in (b"1.0", b"1.1", b"1.4", b"1.7", b"2.0"):
            validate(VALID_PDF.replace(b"%PDF-1.4", b"%PDF-" + version, 1))
        for version in (b"1.8", b"1.9", b"2.1", b"3.0", b"0.9"):
            with self.subTest(version=version):
                self.assertEqual(reason_of(validate, VALID_PDF.replace(b"%PDF-1.4", b"%PDF-" + version, 1)), REASON_NOT_A_PDF)

    def test_a_file_that_does_not_start_with_the_header_is_not_a_pdf(self):
        for data in (b"", b"%PDF", b"%PDF-1.4", b"hello world, this is text\n", b" " + VALID_PDF, b"\xef\xbb\xbf" + VALID_PDF, b"GIF89a" + VALID_PDF, b"PK\x03\x04" + b"\x00" * 100, b"%PDF-1.4x" + VALID_PDF[9:], b"MZ" + b"\x00" * 100):
            with self.subTest(prefix=data[:12]):
                self.assertEqual(reason_of(validate, data), REASON_NOT_A_PDF)

    def test_a_truncated_pdf_is_refused_at_every_cut_point(self):
        for cut in range(9, len(VALID_PDF) - 1):
            with self.subTest(cut=cut):
                self.assertIn(reason_of(validate, VALID_PDF[:cut]), (REASON_TRUNCATED_PDF, REASON_MALFORMED_PDF))
        validate(VALID_PDF[:-1])  # only the final newline is missing: the end marker is intact, so the file is complete
        self.assertIsNotNone(reason_of(validate, VALID_PDF[:-6]))  # "%%EOF" and its newline are gone

    def test_a_missing_end_marker_or_startxref_is_truncated(self):
        self.assertEqual(reason_of(validate, VALID_PDF.replace(b"%%EOF", b"")), REASON_TRUNCATED_PDF)
        self.assertEqual(reason_of(validate, VALID_PDF.replace(b"startxref", b"startxxxx")), REASON_TRUNCATED_PDF)

    def test_an_end_marker_only_far_from_the_end_is_truncated(self):
        self.assertEqual(reason_of(validate, VALID_PDF + b"x" * 2000), REASON_TRUNCATED_PDF)
        validate(VALID_PDF + b"\n" * 1000)  # trailing whitespace within the allowed window is fine

    def test_a_wrong_startxref_offset_is_malformed(self):
        start = VALID_PDF.rfind(b"startxref\n") + len(b"startxref\n")
        number = VALID_PDF[start : VALID_PDF.rfind(b"\n%%EOF")]
        for replacement in (b"0", b"9", str(len(VALID_PDF)).encode(), b"99999999", b"12abc", b""):
            with self.subTest(replacement=replacement):
                mutated = VALID_PDF[:start] + replacement + VALID_PDF[start + len(number) :]
                self.assertIn(reason_of(validate, mutated), (REASON_MALFORMED_PDF,))

    def test_a_table_without_a_trailer_or_size_is_malformed(self):
        self.assertEqual(reason_of(validate, VALID_PDF.replace(b"trailer", b"trailxx")), REASON_MALFORMED_PDF)
        self.assertEqual(reason_of(validate, VALID_PDF.replace(b"/Size", b"/Sizz")), REASON_MALFORMED_PDF)

    def test_a_stream_cross_reference_without_its_markers_is_malformed(self):
        stream = make_xref_stream_pdf()
        self.assertEqual(reason_of(validate, stream.replace(b"/XRef", b"/XRxf")), REASON_MALFORMED_PDF)
        self.assertEqual(reason_of(validate, stream.replace(b"/Size", b"/Sizz")), REASON_MALFORMED_PDF)

    def test_it_reads_a_bounded_amount_and_never_the_whole_file(self):
        big = make_pdf(padding=5_000_000)
        reads = []

        def read_at(offset, length):
            reads.append(length)
            return big[offset : offset + length]

        pdf.validate_pdf(read_at, len(big))
        self.assertLessEqual(max(reads), pdf.TAIL_BYTES)
        self.assertLessEqual(sum(reads), pdf.HEADER_BYTES + pdf.TAIL_BYTES + pdf.TARGET_BYTES)

    def test_hostile_input_cannot_make_the_check_slow(self):
        import time

        for data in (b"%PDF-1.4\n" + b"startxref\n" * 5000 + b"%%EOF", b"%PDF-1.4\n" + b" " * 70_000 + b"startxref " + b"1" * 5000 + b"\n%%EOF", b"%PDF-1.4\n" + b"xref\n0 1\n" * 9000 + b"startxref\n9\n%%EOF"):
            started = time.monotonic()
            reason_of(validate, data)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_the_header_check_alone_needs_the_first_nine_bytes(self):
        pdf.check_header(b"%PDF-1.7\n")
        pdf.check_header(b"%PDF-2.0\r\nmore")
        for prefix in (b"%PDF-1.7", b"", b"%PDF-1.7X", b"%pdf-1.7\n"):
            with self.subTest(prefix=prefix):
                self.assertEqual(reason_of(pdf.check_header, prefix), REASON_NOT_A_PDF)
