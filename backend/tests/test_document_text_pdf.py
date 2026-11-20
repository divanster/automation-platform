"""DB-free tests of the text extraction itself (DOC-2): ``apps.document_text.pdf_text`` with the REAL library (pypdf) on small synthetic PDFs built in memory (``tests/pdf_support.py``). Nothing is read from disk, sent
anywhere or taken from a real document. The same function runs in the isolated child process in production; the process boundary is tested in ``tests/test_document_text_isolation.py``."""

import hashlib

from django.test import SimpleTestCase

from apps.document_text import pdf_text, rules
from apps.document_text.rules import ExtractionLimits
from tests.pdf_support import build_pdf

LIMITS = ExtractionLimits()


def extract(data, limits=LIMITS):
    return pdf_text.extract_pdf_text(data, limits)


def texts(result):
    return [page.text for page in result.pages]


class ValidDocumentTests(SimpleTestCase):
    def test_a_single_text_page(self):
        result = extract(build_pdf([("Hello world",)]))
        self.assertEqual((result.status, result.error_code, result.page_count), (rules.RESULT_EXTRACTED, "", 1))
        self.assertEqual(texts(result), ["Hello world"])
        self.assertEqual((result.pages[0].page_number, result.pages[0].status, result.pages[0].error_code), (1, rules.PAGE_TEXT, ""))
        self.assertEqual(result.extractor_name, "pypdf")
        self.assertEqual(result.extractor_version, pdf_text.library_version())

    def test_several_pages_come_back_in_page_order_with_their_numbers(self):
        result = extract(build_pdf([("First page", "second line"), ("Second page",), ("Third page",)]))
        self.assertEqual(result.page_count, 3)
        self.assertEqual([(p.page_number, p.status) for p in result.pages], [(1, "text"), (2, "text"), (3, "text")])
        self.assertEqual(texts(result), ["First page\nsecond line", "Second page", "Third page"])
        self.assertEqual(result.char_count, sum(len(t) for t in texts(result)))

    def test_lines_keep_the_order_the_page_draws_them_in(self):
        lines = [f"line {number}" for number in range(1, 21)]
        self.assertEqual(texts(extract(build_pdf([lines])))[0].split("\n"), lines)

    def test_a_compressed_content_stream_is_read(self):
        self.assertEqual(texts(extract(build_pdf([("Compressed text",)], compress=True))), ["Compressed text"])

    def test_non_ascii_text_is_decoded_not_guessed(self):
        result = extract(build_pdf([("Caf\u00e9 \u20ac 100",)]))
        self.assertEqual(texts(result), ["Caf\u00e9 \u20ac 100"])

    def test_parentheses_and_backslashes_in_the_text_survive(self):
        self.assertEqual(texts(extract(build_pdf([("a (b) c \\ d",)]))), ["a (b) c \\ d"])

    def test_the_result_is_deterministic(self):
        data = build_pdf([("Alpha", "beta"), ("Gamma",), ()])
        first, second = extract(data), extract(data)
        self.assertEqual(first, second)
        self.assertEqual(first.text_sha256, second.text_sha256)
        self.assertEqual(extract(build_pdf([("Alpha", "beta"), ("Gamma",), ()], compress=True)).text_sha256, first.text_sha256)  # the same text, stored differently

    def test_the_input_is_never_modified_and_not_retained(self):
        data = bytearray(build_pdf([("Hello",)]))
        before = hashlib.sha256(data).hexdigest()
        extract(bytes(data))
        extract(data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), before)

    def test_a_pdf_two_header_and_other_supported_versions_are_read(self):
        for version in ("1.0", "1.4", "1.7", "2.0"):
            with self.subTest(version=version):
                self.assertEqual(texts(extract(build_pdf([("Versioned",)], version=version))), ["Versioned"])


class DocumentsWithoutTextTests(SimpleTestCase):
    def test_a_page_with_no_text_layer_is_empty_not_an_error_and_nothing_is_invented(self):
        result = extract(build_pdf([()]))
        self.assertEqual((result.status, result.error_code, result.page_count), (rules.RESULT_EMPTY, rules.INFO_NO_TEXT, 1))
        self.assertEqual([(p.status, p.text) for p in result.pages], [(rules.PAGE_EMPTY, "")])

    def test_a_document_with_no_pages_is_empty(self):
        result = extract(build_pdf([]))
        self.assertEqual((result.status, result.error_code, result.page_count, result.pages), (rules.RESULT_EMPTY, rules.INFO_NO_PAGES, 0, ()))

    def test_text_and_empty_pages_mix_without_failing(self):
        result = extract(build_pdf([("Text",), (), ("More",)]))
        self.assertEqual((result.status, [p.status for p in result.pages]), (rules.RESULT_EXTRACTED, ["text", "empty", "text"]))
        self.assertEqual(result.pages[1].page_number, 2)

    def test_there_is_no_ocr_a_drawn_rectangle_gives_nothing(self):
        self.assertEqual(texts(extract(build_pdf([(), ()]))), ["", ""])


class RefusedDocumentTests(SimpleTestCase):
    def failed(self, data, code, limits=LIMITS):
        result = extract(data, limits)
        self.assertEqual((result.status, result.error_code, result.pages), (rules.RESULT_FAILED, code, ()))
        return result

    def test_an_encrypted_document_is_refused_and_no_text_is_read(self):
        self.failed(build_pdf([("Secret content",)], encrypt=True), rules.ERR_ENCRYPTED)

    def test_a_really_encrypted_document_that_opens_with_an_empty_password_is_still_refused(self):
        import io

        from pypdf import PdfReader, PdfWriter

        for algorithm in ("RC4-40", "RC4-128"):
            with self.subTest(algorithm=algorithm):
                writer = PdfWriter()
                writer.add_blank_page(200, 200)
                writer.encrypt("", "owner-password", algorithm=algorithm)
                buffer = io.BytesIO()
                writer.write(buffer)
                data = buffer.getvalue()
                self.assertEqual(len(PdfReader(io.BytesIO(data)).pages), 1)  # the library alone WOULD open it: refusing is this slice's decision, not the library's
                self.failed(data, rules.ERR_ENCRYPTED)

    def test_a_truncated_document_is_malformed_and_yields_no_text(self):
        data = build_pdf([("Hello",), ("World",)])
        for cut in (10, 60, len(data) // 2, len(data) - 40, len(data) - 10):
            with self.subTest(cut=cut):
                result = extract(data[:cut])
                self.assertEqual((result.status, result.error_code, result.pages), (rules.RESULT_FAILED, rules.ERR_MALFORMED, ()))

    def test_garbage_is_malformed_and_never_raises(self):
        for data in (b"", b"%PDF-1.4", b"%PDF-1.4\n%%EOF", b"not a pdf at all", b"\x00" * 1000, b"%PDF-1.4\n1 0 obj\n<<" + b"<<" * 5000, bytes(range(256)) * 40):
            with self.subTest(size=len(data), start=data[:12]):
                result = extract(data)
                self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_MALFORMED))

    def test_a_catalog_that_points_nowhere_is_malformed(self):
        data = build_pdf([("Hello",)]).replace(b"/Pages 2 0 R", b"/Pages 99 0 R")
        self.assertEqual(extract(data).status, rules.RESULT_FAILED)

    def test_too_many_pages_are_refused_before_any_page_is_read(self):
        data = build_pdf([("x",)] * 6)
        result = self.failed(data, rules.ERR_TOO_MANY_PAGES, ExtractionLimits(max_pages=5))
        self.assertEqual(result.page_count, 6)
        self.assertEqual(extract(data, ExtractionLimits(max_pages=6)).page_count, 6)  # exactly at the limit is fine

    def test_a_lying_page_count_is_not_believed(self):
        data = build_pdf([("one",), ("two",)], page_count_override=1000000)
        result = extract(data, ExtractionLimits(max_pages=5))
        self.assertEqual((result.status, result.page_count, texts(result)), (rules.RESULT_EXTRACTED, 2, ["one", "two"]))  # the real tree, not /Count, decides

    def test_the_library_is_told_to_call_no_external_program(self):
        from unittest import mock

        configuration = pdf_text._configuration(LIMITS)
        self.assertIsNone(configuration.jbig2dec_binary)
        found = pdf_text.pypdf.get_configuration().with_overwrites(jbig2dec_binary="/usr/bin/jbig2dec")  # as if the program were installed on this machine
        with mock.patch.object(pdf_text.pypdf, "get_configuration", return_value=found):
            self.assertIsNone(pdf_text._configuration(LIMITS).jbig2dec_binary)
        for name in ("maximum_declared_stream_length", "zlib_maximum_output_length", "lzw_maximum_output_length", "run_length_maximum_output_length", "image_maximum_buffer_size"):
            self.assertLessEqual(getattr(configuration, name), LIMITS.memory_bytes // 8)
        small = pdf_text._configuration(ExtractionLimits(memory_bytes=128 * rules.MIB))
        self.assertEqual(small.zlib_maximum_output_length, 16 * rules.MIB)

    def test_a_decompression_bomb_is_stopped_by_the_stream_cap_and_never_becomes_text(self):
        import time
        import zlib

        bomb = zlib.compress(b"A" * (64 * 1024 * 1024))  # 64 MiB of output from about 64 KiB
        data = build_pdf([("good page",), ("x",)], flate_streams={1: bomb})
        started = time.monotonic()
        result = extract(data, ExtractionLimits(memory_bytes=128 * rules.MIB))  # the stream cap is 16 MiB
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(texts(result)[0], "good page")
        self.assertEqual((result.pages[1].status, result.pages[1].error_code, result.pages[1].text), ("failed", rules.PAGE_ERR_FAILED, ""))  # the bomb is a failed page, never text
        self.assertEqual((result.status, result.error_code), (rules.RESULT_PARTIAL, rules.INFO_SOME_PAGES_FAILED))


class TextLimitTests(SimpleTestCase):
    def test_a_page_over_the_page_limit_fails_and_is_never_cut_short(self):
        data = build_pdf([("short",), ("x" * 120,), ("also short",)])
        result = extract(data, ExtractionLimits(max_page_chars=100, max_total_chars=1000))
        self.assertEqual([(p.status, p.error_code) for p in result.pages], [("text", ""), ("failed", rules.PAGE_ERR_TOO_LONG), ("text", "")])
        self.assertEqual((result.status, result.error_code), (rules.RESULT_PARTIAL, rules.INFO_SOME_PAGES_FAILED))
        self.assertEqual(result.pages[1].text, "")  # no partial text is stored as if it were whole
        self.assertEqual(result.char_count, len("short") + len("also short"))

    def test_the_total_limit_stops_the_text_and_marks_every_later_page(self):
        data = build_pdf([("a" * 40,), ("b" * 40,), ("c" * 40,), ("d" * 10,)])
        result = extract(data, ExtractionLimits(max_page_chars=100, max_total_chars=100))
        self.assertEqual([(p.status, p.error_code) for p in result.pages], [("text", ""), ("text", ""), ("failed", rules.PAGE_ERR_TEXT_LIMIT), ("failed", rules.PAGE_ERR_TEXT_LIMIT)])
        self.assertEqual((result.status, result.char_count), (rules.RESULT_PARTIAL, 80))
        self.assertLessEqual(result.char_count, 100)

    def test_a_page_exactly_at_the_limits_is_kept(self):
        result = extract(build_pdf([("a" * 50,), ("b" * 50,)]), ExtractionLimits(max_page_chars=50, max_total_chars=100))
        self.assertEqual((result.status, result.char_count), (rules.RESULT_EXTRACTED, 100))

    def test_when_every_page_is_over_the_limit_nothing_is_usable(self):
        result = extract(build_pdf([("x" * 60,), ("y" * 60,)]), ExtractionLimits(max_page_chars=50, max_total_chars=100))
        self.assertEqual((result.status, result.error_code, result.pages, result.page_count), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED, (), 2))


class FailedPageTests(SimpleTestCase):
    def test_one_unreadable_page_does_not_hide_the_others(self):
        data = build_pdf([("good one",), ("bad page",), ("good two",)])
        original = pdf_text.PdfReader

        class Broken(original):
            @property
            def pages(self):
                real = super().pages

                class Pages:
                    def __len__(self_inner):
                        return len(real)

                    def __getitem__(self_inner, index):
                        page = real[index]
                        if index == 1:
                            page.extract_text = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
                        return page

                return Pages()

        pdf_text.PdfReader = Broken
        try:
            result = extract(data)
        finally:
            pdf_text.PdfReader = original
        self.assertEqual([(p.status, p.error_code) for p in result.pages], [("text", ""), ("failed", rules.PAGE_ERR_FAILED), ("text", "")])
        self.assertEqual((result.status, result.error_code), (rules.RESULT_PARTIAL, rules.INFO_SOME_PAGES_FAILED))
        self.assertEqual(texts(result), ["good one", "", "good two"])

    def test_a_memory_error_is_not_swallowed(self):
        original = pdf_text.PdfReader

        class Exhausted:
            def __init__(self, *args, **kwargs):
                raise MemoryError

        pdf_text.PdfReader = Exhausted
        try:
            with self.assertRaises(MemoryError):
                extract(build_pdf([("x",)]))
        finally:
            pdf_text.PdfReader = original


class SideEffectTests(SimpleTestCase):
    def test_logging_is_restored_after_an_extraction_even_a_failing_one(self):
        import logging

        before = logging.root.manager.disable
        extract(b"garbage")
        extract(build_pdf([("x",)]))
        self.assertEqual(logging.root.manager.disable, before)

    def test_nothing_is_printed(self):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            extract(b"%PDF-1.4\ngarbage")
            extract(build_pdf([("x",)], encrypt=True))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
