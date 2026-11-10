"""Pure-domain tests (standard-library unittest, no Django) for the fixture extractor.

``DeterministicTestExtractor`` is a fixture parser for one labelled-line template. These
tests check its documented behavior (module docstring of workflows/request_intake/
extraction.py) and that its output is candidate data which validation then checks.
"""

import dataclasses
import unittest
from decimal import Decimal

from tests.intake_support import VALID_TEXT, find_mutable_state, summary
from workflows.request_intake import extraction as extraction_module
from workflows.request_intake import (
    Ambiguity,
    DeterministicTestExtractor,
    ExtractionResult,
    FindingCode,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    make_excerpt,
    validate_request,
)


class FixtureBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.extractor = DeterministicTestExtractor()

    def test_extracts_the_documented_template(self):
        result = self.extractor.extract(VALID_TEXT)
        self.assertEqual(
            result.candidate,
            StructuredRequest(
                requester=Requester(name="Jane Doe", contact="jane@example.invalid"),
                items=(
                    RequestedItem(description="A4 paper", quantity=Decimal("10"), unit="boxes", notes="for the print room"),
                    RequestedItem(description="Stapler", quantity=Decimal("2"), unit=None, notes=None),
                ),
                notes="Needed this week.",
                source_refs=result.candidate.source_refs,
            ),
        )
        self.assertEqual(result.ambiguities, ())

    def test_labels_are_case_insensitive_and_tolerate_spacing_and_crlf(self):
        result = self.extractor.extract("  REQUESTER :  Jane Doe  \r\nitem:3|pcs|Pens\r\n")
        self.assertEqual(result.candidate.requester.name, "Jane Doe")
        self.assertEqual(result.candidate.items, (RequestedItem(description="Pens", quantity=Decimal("3"), unit="pcs"),))

    def test_unlabelled_and_unknown_lines_are_ignored(self):
        text = "Dear team,\nPlease send pens.\nPrice: 5 euro\nRequester: Jane Doe\nThanks!\n"
        result = self.extractor.extract(text)
        self.assertEqual(result.candidate.requester, Requester(name="Jane Doe"))
        self.assertEqual(result.candidate.items, ())
        self.assertIsNone(result.candidate.notes)

    def test_first_occurrence_of_single_valued_labels_wins(self):
        text = "Requester: First\nRequester: Second\nContact: a@example.invalid\nContact: b@example.invalid\nNotes: one\nNotes: two\n"
        candidate = self.extractor.extract(text).candidate
        self.assertEqual((candidate.requester.name, candidate.requester.contact, candidate.notes), ("First", "a@example.invalid", "one"))

    def test_empty_values_are_absent_not_defaulted(self):
        self.assertEqual(self.extractor.extract("Requester:\nContact:   \nNotes:\n").candidate, StructuredRequest())

    def test_every_item_line_yields_an_item_in_order(self):
        items = self.extractor.extract("Item: 1 | | A\nItem: 2 | | B\nItem: 3 | | C\n").candidate.items
        self.assertEqual([i.description for i in items], ["A", "B", "C"])

    def test_empty_unit_description_and_notes_fields_become_none(self):
        item = self.extractor.extract("Item: 4 | | |").candidate.items[0]
        self.assertEqual((item.description, item.quantity, item.unit, item.notes), (None, Decimal("4"), None, None))

    def test_empty_text_gives_an_empty_candidate(self):
        result = self.extractor.extract("")
        self.assertEqual(result.candidate, StructuredRequest())
        self.assertEqual(result.ambiguities, ())

    def test_text_must_be_a_string(self):
        for bad in (None, b"Requester: x", 5, ["Requester: x"]):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                self.extractor.extract(bad)


class QuantityReadingTests(unittest.TestCase):
    def setUp(self):
        self.extractor = DeterministicTestExtractor()

    def quantity_of(self, quantity_text):
        result = self.extractor.extract(f"Item: {quantity_text} | | Pens")
        return result.candidate.items[0].quantity, result.ambiguities

    def test_numbers_become_decimal_with_the_digits_as_written(self):
        for text, expected_text in (("10", "10"), ("2.50", "2.50"), ("+7", "7"), ("0.125", "0.125"), ("-3", "-3"), ("007", "7")):
            with self.subTest(text=text):
                quantity, ambiguities = self.quantity_of(text)
                self.assertIsInstance(quantity, Decimal)
                self.assertEqual(str(quantity), expected_text)
                self.assertEqual(ambiguities, ())

    def test_no_rounding_and_no_float(self):
        digits = "0.1234567890123456789012345678901234567890123456789"
        quantity, _ = self.quantity_of(digits)
        self.assertEqual(str(quantity), digits)
        self.assertNotIsInstance(quantity, float)

    def test_zero_and_negative_are_passed_through_for_validation_to_reject(self):
        self.assertEqual(self.quantity_of("0"), (Decimal("0"), ()))
        self.assertEqual(self.quantity_of("-3"), (Decimal("-3"), ()))

    def test_words_and_other_formats_are_ambiguous_and_never_guessed(self):
        for text in ("a few", "ten", "10-12", "1,5", "about 10", "5x", "1e3", ".5", "5.", "0x10", "1 000"):
            with self.subTest(text=text):
                quantity, ambiguities = self.quantity_of(text)
                self.assertIsNone(quantity)
                self.assertEqual(ambiguities, (Ambiguity("items[0].quantity", text),))

    def test_digits_from_other_scripts_are_not_numbers(self):
        # "\d" would accept these; the fixture grammar is ASCII digits only.
        for text in ("\u0663", "\u0661\u0660", "\uff11\uff10", "1\u0660"):
            with self.subTest(text=ascii(text)):
                quantity, ambiguities = self.quantity_of(text)
                self.assertIsNone(quantity)
                self.assertEqual(ambiguities, (Ambiguity("items[0].quantity", text),))

    def test_empty_quantity_is_missing_without_an_ambiguity(self):
        self.assertEqual(self.quantity_of(""), (None, ()))

    def test_malformed_item_line_is_not_guessed(self):
        for line in ("Item: Pens", "Item: 5 | Pens", "Item: 5 | | Pens | note | extra"):
            with self.subTest(line=line):
                result = self.extractor.extract(line)
                self.assertEqual(result.candidate.items, (RequestedItem(),))
                self.assertEqual(result.ambiguities, (Ambiguity("items[0]", line),))

    def test_ambiguity_indexes_follow_item_order(self):
        result = self.extractor.extract("Item: 1 | | A\nItem: some | | B\nItem: 3 | | C\nItem: lots | | D\n")
        self.assertEqual([a.field_path for a in result.ambiguities], ["items[1].quantity", "items[3].quantity"])


class SourceReferenceCoordinateTests(unittest.TestCase):
    """Source references follow the documented convention and validate against the real input."""

    def refs(self, text):
        candidate = DeterministicTestExtractor().extract(text).candidate
        return {ref.field_path: (ref.excerpt, ref.line) for ref in candidate.source_refs}

    def test_lines_are_one_based_and_excerpts_are_the_stripped_line(self):
        refs = self.refs(VALID_TEXT)
        self.assertEqual(refs["requester.name"], ("Requester: Jane Doe", 1))
        self.assertEqual(refs["requester.contact"], ("Contact: jane@example.invalid", 2))
        self.assertEqual(refs["items[0].quantity"][1], 3)
        self.assertEqual(refs["items[1].description"][1], 4)
        self.assertEqual(refs["notes"], ("Notes: Needed this week.", 5))
        self.assertNotIn("items[1].unit", refs)  # empty value: no ref, nothing invented

    def test_surrounding_whitespace_is_not_part_of_the_excerpt(self):
        self.assertEqual(self.refs("   Requester:   Jane Doe   \n")["requester.name"], ("Requester:   Jane Doe", 1))

    def test_line_numbers_follow_str_splitlines_for_every_separator(self):
        separators = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
        for separator in separators:
            with self.subTest(separator=ascii(separator)):
                text = f"unlabelled line{separator}Requester: Jane{separator}Notes: soon{separator}"
                result = DeterministicTestExtractor().extract(text)
                refs = {r.field_path: r.line for r in result.candidate.source_refs}
                self.assertEqual(refs, {"requester.name": 2, "notes": 3})
                validation = validate_request(result.candidate, source_text=text)
                self.assertEqual([f for f in validation.findings if f.code is FindingCode.INVALID_SOURCE_REF], [])

    def test_a_trailing_line_break_does_not_add_a_line(self):
        self.assertEqual(len("a\nb\n".splitlines()), 2)
        text = "Requester: Jane\n"
        result = DeterministicTestExtractor().extract(text)
        self.assertEqual(result.candidate.source_refs, (SourceRef("requester.name", "Requester: Jane", 1),))

    def test_long_excerpts_are_shortened_by_the_documented_convention(self):
        text = "Requester: " + "x" * 500
        result = DeterministicTestExtractor().extract(text)
        excerpt = result.candidate.source_refs[0].excerpt
        self.assertEqual(excerpt, make_excerpt(text))
        self.assertEqual(len(excerpt), 200)
        self.assertTrue(excerpt.endswith("..."))
        self.assertEqual(len(result.candidate.requester.name), 500)  # the value itself is never truncated

    def test_every_reference_validates_against_the_actual_input(self):
        for text in (VALID_TEXT, "Requester: A\nItem: 1 | u | X | n\nItem: 2 | | Y\nContact: c\nNotes: z\n", "Requester: " + "y" * 500):
            with self.subTest(text=text[:30]):
                result = DeterministicTestExtractor().extract(text)
                validation = validate_request(result.candidate, source_text=text, ambiguities=result.ambiguities)
                self.assertEqual([f for f in validation.findings if f.code is FindingCode.INVALID_SOURCE_REF], [])

    def test_references_are_checked_against_the_input_actually_supplied(self):
        result = DeterministicTestExtractor().extract(VALID_TEXT)
        edited = VALID_TEXT.replace("Jane Doe", "John Roe")
        findings = validate_request(result.candidate, source_text=edited).findings
        self.assertIn(FindingCode.INVALID_SOURCE_REF, [f.code for f in findings])

    def test_absent_values_have_no_refs(self):
        self.assertEqual(DeterministicTestExtractor().extract("nothing useful").candidate.source_refs, ())


class ExtractionIsSeparateFromValidationTests(unittest.TestCase):
    """The extractor produces candidate data; validation is a different step."""

    def validate(self, text):
        result = DeterministicTestExtractor().extract(text)
        return result, validate_request(result.candidate, source_text=text, ambiguities=result.ambiguities)

    def test_valid_text_produces_a_valid_candidate(self):
        _, validation = self.validate(VALID_TEXT)
        self.assertEqual(validation.findings, ())
        self.assertTrue(validation.is_valid)

    def test_extractor_does_not_validate(self):
        result = DeterministicTestExtractor().extract("Item: -3 | | Pens\nItem: 0 | | Ink\n")
        self.assertEqual([i.quantity for i in result.candidate.items], [Decimal("-3"), Decimal("0")])
        self.assertIsNone(result.candidate.requester.name)
        self.assertEqual(result.ambiguities, ())

    def test_validation_reports_missing_requester_and_items(self):
        _, validation = self.validate("Notes: nothing else\n")
        self.assertEqual(summary(validation), [("missing_required", "requester.name"), ("missing_required", "items")])

    def test_validation_rejects_extracted_invalid_quantities(self):
        _, validation = self.validate("Requester: Jane\nItem: -3 | | Pens\nItem: 0 | | Ink\n")
        self.assertEqual(summary(validation), [("invalid_value", "items[0].quantity"), ("invalid_value", "items[1].quantity")])

    def test_ambiguous_quantity_is_missing_and_blocking_ambiguous(self):
        result, validation = self.validate("Requester: Jane\nItem: a few | | Pens\n")
        self.assertEqual(
            summary(validation),
            [("missing_required", "items[0].quantity"), ("ambiguous_required_value", "items[0].quantity")],
        )
        self.assertEqual(validation.findings[1].excerpt, "a few")
        self.assertIs(validation.findings[1].code, FindingCode.AMBIGUOUS_REQUIRED_VALUE)
        self.assertFalse(validation.is_valid)
        self.assertEqual(result.candidate.items[0].description, "Pens")  # nothing was invented to fill the gap

    def test_malformed_item_line_blocks_approval_for_a_human_to_complete(self):
        _, validation = self.validate("Requester: Jane\nItem: Pens\n")
        self.assertEqual(
            summary(validation),
            [("missing_required", "items[0].description"), ("missing_required", "items[0].quantity"), ("ambiguous_required_value", "items[0]")],
        )
        self.assertFalse(validation.is_valid)

    def test_extracted_values_are_located_in_their_source(self):
        # The fixture copies values from the text, so the source-location warning never fires.
        for text in (VALID_TEXT, "Requester:   Jane   Doe \nItem: 2.50 | kg | Coffee beans\n"):
            with self.subTest(text=text):
                _, validation = self.validate(text)
                self.assertNotIn(FindingCode.VALUE_NOT_IN_SOURCE, [f.code for f in validation.findings])


class FixtureIdentityTests(unittest.TestCase):
    """The extractor is a fixture parser, not document understanding and not an AI provider."""

    def test_it_identifies_itself_as_a_fixture_parser(self):
        extractor = DeterministicTestExtractor()
        self.assertEqual(extractor.name, "deterministic-test-fixture-parser")
        self.assertEqual(extractor.kind, "fixture_parser")
        self.assertIs(extractor.understands_arbitrary_documents, False)
        result = extractor.extract(VALID_TEXT)
        self.assertIsInstance(result, ExtractionResult)
        self.assertEqual((result.extractor_name, result.extractor_version), (extractor.name, extractor.version))
        for word in ("model", "ai", "llm", "gpt", "claude"):
            self.assertNotIn(word, extractor.name.split("-"))

    def test_the_documentation_states_it_is_not_a_provider(self):
        doc = (DeterministicTestExtractor.__doc__ or "") + (extraction_module.__doc__ or "")
        self.assertIn("NOT an AI model", doc)
        self.assertIn("FIXTURE PARSER", doc)

    def test_it_does_not_understand_arbitrary_documents(self):
        prose = (
            "Hi, this is Jane Doe. Could you please send ten boxes of A4 paper and "
            "two staplers? I need them by Friday. Thanks, Jane"
        )
        result = DeterministicTestExtractor().extract(prose)
        self.assertEqual(result.candidate, StructuredRequest())
        self.assertEqual(result.ambiguities, ())

    def test_near_miss_templates_are_not_interpreted(self):
        for text in (
            "Name: Jane Doe\nQty: 5\nProduct: Pens",
            "From: Jane Doe\nItems: 5 | | Pens",
            "Requester - Jane Doe",
            "Item 5 | | Pens",
            "Request: Jane\nItemx: 5 | | Pens",
        ):
            with self.subTest(text=text):
                self.assertEqual(DeterministicTestExtractor().extract(text).candidate, StructuredRequest())

    def test_it_never_produces_price_or_availability_data(self):
        text = "Requester: Jane\nItem: 5 | | Pens\nPrice: 12.50\nStock: 300\nDelivery: Friday\n"
        candidate = DeterministicTestExtractor().extract(text).candidate
        flat = repr(candidate).lower()
        for forbidden in ("12.50", "300", "friday", "price", "stock"):
            self.assertNotIn(forbidden, flat)


class RepeatabilityAndIsolationTests(unittest.TestCase):
    def test_same_input_gives_equal_output(self):
        extractor = DeterministicTestExtractor()
        self.assertEqual(extractor.extract(VALID_TEXT), extractor.extract(VALID_TEXT))

    def test_separate_instances_agree(self):
        self.assertEqual(DeterministicTestExtractor().extract(VALID_TEXT), DeterministicTestExtractor().extract(VALID_TEXT))

    def test_an_intervening_call_does_not_change_a_result(self):
        extractor = DeterministicTestExtractor()
        first = extractor.extract(VALID_TEXT)
        snapshot = dataclasses.astuple(first)
        extractor.extract("Requester: Someone Else\nItem: 99 | | Other thing\nItem: lots | | Mystery\n")
        self.assertEqual(dataclasses.astuple(first), snapshot)
        self.assertEqual(extractor.extract(VALID_TEXT), first)

    def test_results_are_distinct_objects_each_call(self):
        extractor = DeterministicTestExtractor()
        a, b = extractor.extract(VALID_TEXT), extractor.extract(VALID_TEXT)
        self.assertIsNot(a, b)
        self.assertIsNot(a.candidate, b.candidate)
        self.assertIsNot(a.candidate.items, b.candidate.items)
        self.assertIsNot(a.candidate.items[0], b.candidate.items[0])
        self.assertIsNot(a.candidate.requester, b.candidate.requester)

    def test_results_contain_no_mutable_containers_to_share(self):
        text = "Requester: A\nContact: c\nItem: 1 | u | X | n\nItem: lots | | Y\nItem: broken\nNotes: z\n"
        result = DeterministicTestExtractor().extract(text)
        self.assertEqual(find_mutable_state(result), [])

    def test_results_are_immutable(self):
        result = DeterministicTestExtractor().extract(VALID_TEXT)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.candidate = StructuredRequest()
        self.assertIsInstance(result.candidate.items, tuple)
        self.assertIsInstance(result.ambiguities, tuple)
        self.assertIsInstance(result.candidate.source_refs, tuple)

    def test_the_extractor_keeps_no_instance_state(self):
        extractor = DeterministicTestExtractor()
        self.assertEqual(vars(extractor), {})
        extractor.extract(VALID_TEXT)
        self.assertEqual(vars(extractor), {})

    def test_many_interleaved_calls_are_each_repeatable(self):
        extractor = DeterministicTestExtractor()
        texts = [VALID_TEXT, "Requester: A\nItem: 1 | | X", "", "Item: a few | | Z"]
        expected = [extractor.extract(t) for t in texts]
        for _ in range(3):
            for text, want in zip(reversed(texts), reversed(expected)):
                self.assertEqual(extractor.extract(text), want)

    def test_extraction_result_copies_a_list_of_ambiguities(self):
        ambiguities = [Ambiguity("notes", "?")]
        result = ExtractionResult(candidate=StructuredRequest(), ambiguities=ambiguities)
        ambiguities.append(Ambiguity("notes", "later"))
        self.assertEqual(result.ambiguities, (Ambiguity("notes", "?"),))


if __name__ == "__main__":
    unittest.main()
