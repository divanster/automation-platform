"""DB-free tests of the pure rules of PDF text extraction (DOC-2): limits, text sanitizing, the result shape and its consistency, canonical hashing, provenance and the strict validation of what the
isolated child process reports. No PDF library, no database, no process."""

import json
import uuid

from django.test import SimpleTestCase

from apps.document_text import rules
from apps.document_text.rules import (
    ChildFatal,
    ChildOutputError,
    ExtractionLimits,
    ExtractionOutput,
    PageText,
)

VERSION = "7.1.1"
DOCUMENT = uuid.UUID("11111111-1111-4111-8111-111111111111")
SHA = "ab" * 32


def page(number, text="Hello", status=None, error=""):
    status = status or (rules.PAGE_TEXT if text.strip() else rules.PAGE_EMPTY)
    return PageText(number, status, error, text)


def failed_page(number, code=rules.PAGE_ERR_FAILED):
    return PageText(number, rules.PAGE_FAILED, code, "")


def output(*pages):
    return rules.output_from_pages(pages, extractor_version=VERSION)


class LimitsTests(SimpleTestCase):
    def test_the_defaults_are_inside_every_range_and_documented(self):
        limits = ExtractionLimits()
        self.assertEqual((limits.max_bytes, limits.max_pages, limits.max_page_chars, limits.max_total_chars, limits.timeout_seconds), (20 * rules.MIB, 200, 100_000, 500_000, 20))
        self.assertEqual(limits.memory_bytes, 1024 * rules.MIB)

    def test_every_value_outside_its_range_is_refused_not_clamped(self):
        for name, (low, high) in ExtractionLimits.RANGES.items():
            for value in (low - 1, high + 1, 0, -1, True, "5", 1.5, None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    ExtractionLimits(**{name: value})

    def test_the_range_boundaries_themselves_are_accepted(self):
        for name, (low, high) in ExtractionLimits.RANGES.items():
            partner = {"max_page_chars": 1} if name == "max_total_chars" else {"max_total_chars": 5_000_000} if name == "max_page_chars" else {}
            for value in (low, high):
                with self.subTest(name=name, value=value):
                    self.assertEqual(getattr(ExtractionLimits(**{**partner, name: value}), name), value)

    def test_a_page_limit_above_the_total_limit_is_refused(self):
        with self.assertRaises(ValueError):
            ExtractionLimits(max_page_chars=600, max_total_chars=500)

    def test_the_output_cap_follows_the_limits(self):
        small = ExtractionLimits(max_pages=1, max_page_chars=10, max_total_chars=10)
        self.assertEqual(small.max_output_bytes, 4 * 10 + 256 + 64 * 1024)
        self.assertGreater(ExtractionLimits().max_output_bytes, 4 * 500_000)

    def test_the_limits_are_immutable(self):
        with self.assertRaises(Exception):
            ExtractionLimits().max_pages = 5


class SanitizeTests(SimpleTestCase):
    def test_line_ends_are_normalized_and_controls_removed(self):
        self.assertEqual(rules.sanitize_text("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(rules.sanitize_text("a\x00b\x01c\x0bd\x0ce\x7f\x85f"), "abcdef")
        self.assertEqual(rules.sanitize_text("tab\there\nnew"), "tab\there\nnew")

    def test_a_lone_surrogate_is_removed_and_the_result_encodes_as_utf8(self):
        cleaned = rules.sanitize_text("ok\ud800\udfffend")
        self.assertEqual(cleaned, "okend")
        cleaned.encode("utf-8")

    def test_everything_else_is_kept_exactly(self):
        for text in ("Caf\u00e9 \u20ac \u4e2d\u6587", "  leading and trailing  ", "a\u200bb", "\u202ertl\u202c", "fi\ufb01", "x\u2028y", "\U0001f600"):
            with self.subTest(text=text):
                self.assertEqual(rules.sanitize_text(text), text)

    def test_it_is_idempotent(self):
        once = rules.sanitize_text("a\r\n\x00b\ud800c")
        self.assertEqual(rules.sanitize_text(once), once)


class PageAndOutputTests(SimpleTestCase):
    def test_a_page_has_the_status_its_text_implies(self):
        self.assertEqual(page(1, "Hello").status, rules.PAGE_TEXT)
        self.assertEqual(page(1, "").status, rules.PAGE_EMPTY)
        self.assertEqual(page(1, " \n\t ").status, rules.PAGE_EMPTY)  # whitespace is not text; it is kept as extracted, not invented away
        with self.assertRaises(ValueError):
            PageText(1, rules.PAGE_TEXT, "", "   ")
        with self.assertRaises(ValueError):
            PageText(1, rules.PAGE_EMPTY, "", "text")

    def test_a_failed_page_has_a_page_error_code_and_no_text(self):
        self.assertEqual(failed_page(2).char_count, 0)
        for args in ((1, rules.PAGE_FAILED, "", ""), (1, rules.PAGE_FAILED, "encrypted_pdf", ""), (1, rules.PAGE_FAILED, rules.PAGE_ERR_FAILED, "text"), (1, rules.PAGE_TEXT, rules.PAGE_ERR_FAILED, "x")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                PageText(*args)

    def test_page_numbers_and_unsanitized_text_are_refused(self):
        for args in ((0, rules.PAGE_TEXT, "", "x"), (-1, rules.PAGE_TEXT, "", "x"), (True, rules.PAGE_TEXT, "", "x"), ("1", rules.PAGE_TEXT, "", "x"), (1, rules.PAGE_TEXT, "", "a\x00b"), (1, rules.PAGE_TEXT, "", "a\r\nb"), (1, "other", "", "x")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                PageText(*args)

    def test_the_result_status_follows_from_the_pages(self):
        self.assertEqual(output(page(1), page(2)).status, rules.RESULT_EXTRACTED)
        self.assertEqual(output(page(1), page(2, "")).status, rules.RESULT_EXTRACTED)
        extracted = output(page(1), page(2))
        self.assertEqual((extracted.error_code, extracted.page_count, extracted.char_count), ("", 2, 10))
        empty = output(page(1, ""), page(2, ""))
        self.assertEqual((empty.status, empty.error_code, empty.page_count), (rules.RESULT_EMPTY, rules.INFO_NO_TEXT, 2))
        none = output()
        self.assertEqual((none.status, none.error_code, none.page_count), (rules.RESULT_EMPTY, rules.INFO_NO_PAGES, 0))
        partial = output(page(1), failed_page(2))
        self.assertEqual((partial.status, partial.error_code, len(partial.pages)), (rules.RESULT_PARTIAL, rules.INFO_SOME_PAGES_FAILED, 2))

    def test_when_no_page_could_be_read_the_document_failed_and_carries_no_pages(self):
        everything_failed = output(failed_page(1), failed_page(2))
        self.assertEqual((everything_failed.status, everything_failed.error_code, everything_failed.pages, everything_failed.page_count), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED, (), 2))
        mixed = output(failed_page(1), page(2, ""))
        self.assertEqual(mixed.status, rules.RESULT_FAILED)  # failed pages and no text anywhere: nothing usable

    def test_a_result_cannot_state_a_status_its_pages_do_not_support(self):
        pages = (page(1), page(2))
        for status, code in ((rules.RESULT_EMPTY, rules.INFO_NO_TEXT), (rules.RESULT_PARTIAL, rules.INFO_SOME_PAGES_FAILED), (rules.RESULT_EXTRACTED, "")):
            if (status, code) == (rules.RESULT_EXTRACTED, ""):
                continue
            with self.subTest(status=status), self.assertRaises(ValueError):
                ExtractionOutput(status, code, rules.EXTRACTOR_NAME, VERSION, 2, pages)
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_EXTRACTED, "no_text", rules.EXTRACTOR_NAME, VERSION, 2, pages)  # an extraction with a code
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_EXTRACTED, "", rules.EXTRACTOR_NAME, VERSION, 3, pages)  # a page count that is not the pages
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_EXTRACTED, "", rules.EXTRACTOR_NAME, VERSION, 2, (page(2), page(1)))  # not numbered in order
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_EXTRACTED, "", "other-library", VERSION, 2, pages)
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_EXTRACTED, "", rules.EXTRACTOR_NAME, "bad version!", 2, pages)

    def test_a_failed_result_has_a_document_code_and_no_pages(self):
        failed = rules.failed_output(rules.ERR_ENCRYPTED, extractor_version=VERSION)
        self.assertEqual((failed.status, failed.error_code, failed.pages, failed.page_count, failed.char_count), (rules.RESULT_FAILED, "encrypted_pdf", (), None, 0))
        for code in rules.DOCUMENT_ERRORS:
            rules.failed_output(code, extractor_version=VERSION)
        for code in ("", "no_text", rules.PAGE_ERR_FAILED, "made_up"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                rules.failed_output(code, extractor_version=VERSION)
        with self.assertRaises(ValueError):
            ExtractionOutput(rules.RESULT_FAILED, rules.ERR_MALFORMED, rules.EXTRACTOR_NAME, VERSION, 1, (page(1),))

    def test_every_vocabulary_is_closed_and_has_no_duplicates(self):
        for vocabulary in (rules.RESULT_STATUSES, rules.PAGE_STATUSES, rules.STATES, rules.DOCUMENT_ERRORS, rules.RESULT_INFO, rules.PAGE_ERRORS, rules.FATAL_CODES):
            self.assertEqual(len(set(vocabulary)), len(vocabulary))
        self.assertEqual(set(rules.STATES), {"queued", "running", "extracted", "partial", "empty", "failed"})
        self.assertFalse(set(rules.DOCUMENT_ERRORS) & set(rules.PAGE_ERRORS) | set(rules.DOCUMENT_ERRORS) & set(rules.RESULT_INFO) | set(rules.PAGE_ERRORS) & set(rules.RESULT_INFO))
        import re

        for code in (*rules.RESULT_ERROR_CODES, *rules.PAGE_ERRORS):
            self.assertRegex(code, rules.ERROR_CODE_SHAPE)
            self.assertLessEqual(len(code), 40)
            self.assertIsNotNone(re.fullmatch(r"[a-z][a-z0-9_]*", code))


class HashingAndProvenanceTests(SimpleTestCase):
    def test_the_same_pages_always_give_the_same_hash_and_any_change_changes_it(self):
        base = output(page(1, "Hello"), page(2, "World"))
        again = output(page(1, "Hello"), page(2, "World"))
        self.assertEqual(base.text_sha256, again.text_sha256)
        self.assertRegex(base.text_sha256, r"^[0-9a-f]{64}$")
        for other in (output(page(1, "Hello"), page(2, "World!")), output(page(1, "Hello"), page(2, "Hello")), output(page(1, "Hello")), output(page(1, "Hello"), failed_page(2))):
            self.assertNotEqual(base.text_sha256, other.text_sha256)

    def test_the_hash_is_pinned_to_the_canonical_form(self):
        import hashlib

        expected = hashlib.sha256(b'[{"error_code":"","page_number":1,"status":"text","text":"Caf\xc3\xa9"}]').hexdigest()
        self.assertEqual(output(page(1, "Caf\u00e9")).text_sha256, expected)
        self.assertEqual(rules.pages_sha256(()), hashlib.sha256(b"[]").hexdigest())

    def test_the_source_reference_names_the_exact_bytes_and_page(self):
        self.assertEqual(rules.source_reference(DOCUMENT, SHA, 3), f"document:{DOCUMENT}:sha256:{SHA}:page:3")
        for args in (("not-a-uuid", SHA, 1), (DOCUMENT, "xyz", 1), (DOCUMENT, SHA.upper(), 1), (DOCUMENT, SHA, 0), (DOCUMENT, SHA, True), (DOCUMENT, SHA, "1"), (DOCUMENT, None, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                rules.source_reference(*args)


class ChildOutputTests(SimpleTestCase):
    limits = ExtractionLimits(max_pages=5, max_page_chars=50, max_total_chars=80)

    def encode(self, result=None, **override):
        document = json.loads(rules.encode_child_output(result or output(page(1, "Hello"), page(2, ""))))
        document.update(override)
        return json.dumps(document).encode("utf-8")

    def refused(self, raw, limits=None):
        with self.assertRaises(ChildOutputError):
            rules.parse_child_output(raw, limits or self.limits)

    def test_what_the_child_writes_round_trips(self):
        original = output(page(1, "Caf\u00e9 \u20ac"), page(2, ""), failed_page(3, rules.PAGE_ERR_TOO_LONG))
        self.assertEqual(rules.parse_child_output(rules.encode_child_output(original), self.limits), original)
        failed = rules.failed_output(rules.ERR_MALFORMED, extractor_version=VERSION)
        self.assertEqual(rules.parse_child_output(rules.encode_child_output(failed), self.limits), failed)

    def test_a_fatal_answer_is_reported_by_code(self):
        for code in rules.FATAL_CODES:
            with self.assertRaises(ChildFatal) as caught:
                rules.parse_child_output(rules.encode_fatal(code), self.limits)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ValueError):
            rules.encode_fatal("made_up")
        self.refused(json.dumps({"version": 1, "fatal": "made_up"}).encode())
        self.refused(json.dumps({"version": 1, "fatal": "resource_limit", "extra": 1}).encode())

    def test_anything_that_is_not_a_valid_answer_is_refused(self):
        self.refused(b"")
        self.refused(b"not json")
        self.refused(b"\xff\xfe\x00")
        self.refused(b"[]")
        self.refused(b"null")
        self.refused(b'{"version": 2}')
        self.refused(b"[" * 100000)  # nesting is a refusal, not a crash
        self.refused(b"x" * (self.limits.max_output_bytes + 1))

    def test_unknown_missing_or_mistyped_keys_are_refused(self):
        good = json.loads(rules.encode_child_output(output(page(1, "Hello"))))
        for key in good:
            if key != "version":
                broken = {k: v for k, v in good.items() if k != key}
                with self.subTest(missing=key):
                    self.refused(json.dumps(broken).encode())
        self.refused(self.encode(extra="x"))
        for key, value in (("status", 5), ("error_code", None), ("extractor_name", 1), ("extractor_version", 1), ("page_count", "2"), ("page_count", True), ("page_count", -1), ("pages", {}), ("pages", "x")):
            with self.subTest(key=key, value=value):
                self.refused(self.encode(**{key: value}))

    def test_pages_are_validated_one_by_one(self):
        good = json.loads(rules.encode_child_output(output(page(1, "Hello"), page(2, "World"))))
        for index, change in ((0, {"page_number": 2}), (1, {"page_number": 1}), (0, {"page_number": True}), (0, {"status": "other"}), (0, {"status": "empty"}), (0, {"text": 5}), (0, {"error_code": "x"}), (1, {"extra": 1})):
            pages = [dict(p) for p in good["pages"]]
            pages[index].update(change)
            with self.subTest(index=index, change=change):
                self.refused(self.encode(pages=pages))
        self.refused(self.encode(pages=[{"page_number": 1}]))
        self.refused(self.encode(pages=["x"]))

    def test_the_limits_apply_to_what_the_child_reports(self):
        self.refused(rules.encode_child_output(output(*(page(n, "x") for n in range(1, 7)))), self.limits)  # six pages, five allowed
        self.refused(rules.encode_child_output(output(page(1, "x" * 51))), self.limits)  # a page over the page limit
        self.refused(rules.encode_child_output(output(page(1, "x" * 50), page(2, "x" * 31))), self.limits)  # over the total limit
        rules.parse_child_output(rules.encode_child_output(output(page(1, "x" * 50), page(2, "x" * 30))), self.limits)

    def test_a_status_that_does_not_follow_from_the_pages_is_refused(self):
        self.refused(self.encode(status="empty", error_code="no_text"))
        self.refused(self.encode(status="extracted", error_code="no_text"))
        self.refused(self.encode(status="failed", error_code="malformed_pdf"))  # a failed result carries no pages
        self.refused(self.encode(status="partial", error_code="some_pages_failed"))
        self.refused(self.encode(extractor_name="other"))
        self.refused(self.encode(page_count=3))

    def test_a_forged_unsanitized_text_is_refused(self):
        good = json.loads(rules.encode_child_output(output(page(1, "Hello"))))
        good["pages"][0]["text"] = "Hel\u0000lo"
        self.refused(json.dumps(good).encode())
