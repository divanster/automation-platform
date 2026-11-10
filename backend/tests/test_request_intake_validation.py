"""Pure-domain tests (standard-library unittest, no Django) for request-intake validation.

Rules under test come from docs/architecture/first-workflow.md, sections 4 to 6, and the
decisions recorded in docs/development/request-intake-slice.md.
"""

import dataclasses
import unittest
from decimal import Decimal

from tests.intake_support import make_request, summary
from workflows.request_intake import (
    Ambiguity,
    DeterministicTestExtractor,
    FindingCode,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    ValidationLimits,
    validate_request,
)


class ValidRequestTests(unittest.TestCase):
    def test_a_valid_request_has_no_findings(self):
        result = validate_request(make_request())
        self.assertEqual(result.findings, ())
        self.assertTrue(result.is_valid)
        self.assertEqual((result.blocking, result.warnings), ((), ()))

    def test_only_required_fields_are_needed(self):
        request = StructuredRequest(
            requester=Requester(name="Jane Doe"),
            items=(RequestedItem(description="Pens", quantity=1),),
        )
        self.assertEqual(validate_request(request).findings, ())

    def test_validation_is_pure_and_repeatable(self):
        request = make_request(requester=Requester(name=None))
        self.assertEqual(validate_request(request), validate_request(request))

    def test_non_request_input_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            validate_request({"requester": {"name": "x"}})


class SchemaShapeTests(unittest.TestCase):
    def test_schema_has_exactly_the_documented_fields(self):
        names = lambda cls: {f.name for f in dataclasses.fields(cls)}  # noqa: E731
        self.assertEqual(names(StructuredRequest), {"requester", "items", "notes", "source_refs"})
        self.assertEqual(names(Requester), {"name", "contact"})
        self.assertEqual(names(RequestedItem), {"description", "quantity", "unit", "notes"})

    def test_schema_has_no_price_or_availability_fields(self):
        all_names = set()
        for cls in (StructuredRequest, Requester, RequestedItem):
            all_names |= {f.name for f in dataclasses.fields(cls)}
        for forbidden in ("price", "cost", "currency", "total", "stock", "inventory", "availability", "delivery"):
            self.assertFalse(any(forbidden in name for name in all_names), forbidden)

    def test_drafts_are_immutable(self):
        request = make_request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.notes = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.requester.name = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.items[0].quantity = 99

    def test_nothing_is_defaulted_or_invented(self):
        empty = StructuredRequest()
        self.assertEqual((empty.requester.name, empty.requester.contact), (None, None))
        self.assertEqual((empty.items, empty.notes, empty.source_refs), ((), None, ()))
        item = RequestedItem()
        self.assertEqual((item.description, item.quantity, item.unit, item.notes), (None, None, None, None))


class MissingRequesterTests(unittest.TestCase):
    def test_missing_requester_name(self):
        result = validate_request(make_request(requester=Requester(name=None)))
        self.assertEqual(summary(result), [("missing_required", "requester.name")])
        self.assertFalse(result.is_valid)

    def test_blank_requester_name_counts_as_missing(self):
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=repr(blank)):
                result = validate_request(make_request(requester=Requester(name=blank)))
                self.assertEqual(summary(result), [("missing_required", "requester.name")])

    def test_absent_requester_object(self):
        result = validate_request(make_request(requester=None))
        self.assertEqual(summary(result), [("missing_required", "requester.name")])

    def test_requester_contact_is_optional(self):
        result = validate_request(make_request(requester=Requester(name="Jane Doe", contact=None)))
        self.assertEqual(result.findings, ())

    def test_wrong_requester_type_is_invalid_not_missing(self):
        result = validate_request(make_request(requester="Jane Doe"))
        self.assertEqual(summary(result), [("invalid_type", "requester")])


class MissingItemsTests(unittest.TestCase):
    def test_no_items(self):
        for items in ((), []):
            with self.subTest(items=items):
                result = validate_request(make_request(items=items))
                self.assertEqual(summary(result), [("missing_required", "items")])

    def test_items_is_none(self):
        result = validate_request(make_request(items=None))
        self.assertEqual(summary(result), [("missing_required", "items")])

    def test_items_of_the_wrong_container_type(self):
        result = validate_request(make_request(items="pens"))
        self.assertEqual(summary(result), [("invalid_type", "items")])

    def test_item_of_the_wrong_type(self):
        result = validate_request(make_request(items=(RequestedItem(description="Pens", quantity=1), "stapler")))
        self.assertEqual(summary(result), [("invalid_type", "items[1]")])

    def test_item_missing_description(self):
        for description in (None, "", "  "):
            with self.subTest(description=description):
                request = make_request(items=(RequestedItem(description=description, quantity=1),))
                self.assertEqual(summary(validate_request(request)), [("missing_required", "items[0].description")])

    def test_item_missing_quantity(self):
        request = make_request(items=(RequestedItem(description="Pens", quantity=None),))
        self.assertEqual(summary(validate_request(request)), [("missing_required", "items[0].quantity")])

    def test_every_missing_value_is_reported_for_a_blank_item(self):
        request = make_request(items=(RequestedItem(),))
        self.assertEqual(
            summary(validate_request(request)),
            [("missing_required", "items[0].description"), ("missing_required", "items[0].quantity")],
        )


class QuantityTests(unittest.TestCase):
    def check(self, quantity, **kwargs):
        request = make_request(items=(RequestedItem(description="Pens", quantity=quantity),))
        return validate_request(request, **kwargs)

    def test_positive_numbers_including_fractions_are_valid(self):
        for quantity in (1, 10, 0.5, 2.5, Decimal("2.50"), Decimal("0.001"), 10**12, Decimal("0.0000000000000000000000000001")):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.check(quantity).findings, ())

    def test_zero_and_negative_are_invalid_values(self):
        for quantity in (0, -1, -0.5, 0.0, -0.0, Decimal("0"), Decimal("-0"), Decimal("-2"), Decimal("0.00")):
            with self.subTest(quantity=quantity):
                self.assertEqual(summary(self.check(quantity)), [("invalid_value", "items[0].quantity")])

    def test_non_finite_numbers_are_invalid_values(self):
        for quantity in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(quantity=quantity):
                self.assertEqual(summary(self.check(quantity)), [("invalid_value", "items[0].quantity")])

    def test_booleans_and_non_numbers_are_invalid_types_not_missing(self):
        for quantity in ("5", "a few", True, False, [1], (1,), {"n": 1}, b"5", object()):
            with self.subTest(quantity=quantity):
                self.assertEqual(summary(self.check(quantity)), [("invalid_type", "items[0].quantity")])

    def test_maximum_is_enforced_only_when_configured(self):
        self.assertEqual(self.check(10**9).findings, ())
        limits = ValidationLimits(max_quantity=100)
        self.assertEqual(self.check(100, limits=limits).findings, ())
        self.assertEqual(summary(self.check(101, limits=limits)), [("exceeds_maximum", "items[0].quantity")])

    def test_the_maximum_comparison_is_exact_and_never_rounds(self):
        limits = ValidationLimits(max_quantity=Decimal("100"))
        for just_over in (Decimal("100.0000000000000000000000000001"), Decimal("100.00000000000000000000000000000000000001")):
            with self.subTest(quantity=just_over):
                self.assertEqual(summary(self.check(just_over, limits=limits)), [("exceeds_maximum", "items[0].quantity")])
        self.assertEqual(self.check(Decimal("100.000000000000000000000000000000000"), limits=limits).findings, ())
        # A float is compared through its canonical Decimal (the shortest round-trip digits).
        self.assertEqual(self.check(0.3, limits=ValidationLimits(max_quantity=Decimal("0.3"))).findings, ())
        self.assertEqual(
            summary(self.check(0.1 + 0.2, limits=ValidationLimits(max_quantity=Decimal("0.3")))),
            [("exceeds_maximum", "items[0].quantity")],
        )

    def test_maximum_never_replaces_the_greater_than_zero_rule(self):
        limits = ValidationLimits(max_quantity=100)
        self.assertEqual(summary(self.check(-5, limits=limits)), [("invalid_value", "items[0].quantity")])

    def test_fractional_quantities_are_allowed(self):
        # Decided: the initial generic schema allows fractions. Unit-specific integer
        # restrictions are deferred (docs/development/request-intake-slice.md).
        for quantity in (2.5, Decimal("0.25"), Decimal("1.5")):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.check(quantity).findings, ())

    def test_invalid_limit_configuration_is_rejected(self):
        for bad in (0, -1, True, "10", float("nan"), float("inf"), Decimal("NaN")):
            with self.subTest(max_quantity=bad), self.assertRaises(ValueError):
                ValidationLimits(max_quantity=bad)
        for bad in (0, -1, True, 1.5, "10"):
            with self.subTest(max_text_length=bad), self.assertRaises(ValueError):
                ValidationLimits(max_text_length=bad)

    def test_no_limits_are_invented(self):
        limits = ValidationLimits()
        self.assertEqual((limits.max_quantity, limits.max_text_length), (None, None))
        huge = make_request(
            requester=Requester(name="n" * 100_000),
            items=(RequestedItem(description="d" * 100_000, quantity=Decimal("1E+100")),),
        )
        self.assertEqual(validate_request(huge).findings, ())


class OptionalFieldTests(unittest.TestCase):
    def test_absent_optional_fields_produce_no_findings(self):
        request = StructuredRequest(
            requester=Requester(name="Jane Doe", contact=None),
            items=(RequestedItem(description="Pens", quantity=1, unit=None, notes=None),),
            notes=None,
        )
        self.assertEqual(validate_request(request).findings, ())

    def test_present_optional_notes_are_valid(self):
        self.assertEqual(validate_request(make_request(notes="Please deliver to reception.")).findings, ())

    def test_empty_optional_text_is_allowed(self):
        request = make_request(notes="", requester=Requester(name="Jane Doe", contact=""))
        self.assertEqual(validate_request(request).findings, ())

    def test_optional_text_of_the_wrong_type_is_invalid(self):
        request = StructuredRequest(
            requester=Requester(name="Jane Doe", contact=123),
            items=(RequestedItem(description="Pens", quantity=1, unit=5, notes=["x"]),),
            notes=7,
        )
        self.assertEqual(
            summary(validate_request(request)),
            [
                ("invalid_type", "requester.contact"),
                ("invalid_type", "items[0].unit"),
                ("invalid_type", "items[0].notes"),
                ("invalid_type", "notes"),
            ],
        )

    def test_text_length_limits_are_applied_only_when_explicitly_configured(self):
        request = make_request(
            requester=Requester(name="x" * 11, contact="y" * 11),
            items=(RequestedItem(description="d" * 11, quantity=1, unit="u" * 11, notes="n" * 11),),
            notes="z" * 11,
        )
        self.assertEqual(validate_request(request).findings, ())
        result = validate_request(request, limits=ValidationLimits(max_text_length=10))
        self.assertEqual(
            summary(result),
            [
                ("exceeds_length", "requester.name"),
                ("exceeds_length", "requester.contact"),
                ("exceeds_length", "items[0].description"),
                ("exceeds_length", "items[0].unit"),
                ("exceeds_length", "items[0].notes"),
                ("exceeds_length", "notes"),
            ],
        )
        at_limit = StructuredRequest(
            requester=Requester(name="x" * 10, contact="y" * 10),
            items=(RequestedItem(description="d" * 10, quantity=1, unit="u" * 10, notes="n" * 10),),
            notes="z" * 10,
        )
        self.assertEqual(validate_request(at_limit, limits=ValidationLimits(max_text_length=10)).findings, ())


class DuplicateTests(unittest.TestCase):
    """Warning only; a conservative comparison; never merged, deleted, or summed."""

    def test_duplicate_descriptions_warn_but_stay_valid(self):
        request = make_request(
            items=(
                RequestedItem(description="A4 paper", quantity=1),
                RequestedItem(description="  a4   PAPER ", quantity=2),
                RequestedItem(description="Stapler", quantity=1),
                RequestedItem(description="a4 paper", quantity=3),
            )
        )
        result = validate_request(request)
        self.assertEqual(summary(result), [("duplicate_item", "items[1].description"), ("duplicate_item", "items[3].description")])
        self.assertTrue(result.is_valid)
        self.assertEqual(result.warnings, result.findings)

    def test_the_comparison_is_conservative(self):
        # Only trim + collapse whitespace + casefold. Near-misses are NOT duplicates.
        near_misses = ["A4 paper", "A4-paper", "A4 papers", "A4 paper.", "A 4 paper", "Paper A4", "Papier A4", "a4paper"]
        request = make_request(items=tuple(RequestedItem(description=d, quantity=1) for d in near_misses))
        self.assertEqual(validate_request(request).findings, ())

    def test_similar_meaning_is_never_proof_of_duplication(self):
        request = make_request(
            items=(RequestedItem(description="Notebook", quantity=1), RequestedItem(description="Laptop", quantity=1), RequestedItem(description="Jotter", quantity=1))
        )
        self.assertEqual(validate_request(request).findings, ())

    def test_duplicates_are_never_merged_deleted_or_summed(self):
        items = (RequestedItem(description="Pens", quantity=1), RequestedItem(description="pens", quantity=2))
        request = make_request(items=items)
        result = validate_request(request)
        self.assertEqual(summary(result), [("duplicate_item", "items[1].description")])
        # The draft is untouched: both items and both quantities remain.
        self.assertEqual(request.items, items)
        self.assertEqual([item.quantity for item in request.items], [Decimal(1), Decimal(2)])

    def test_blank_descriptions_are_not_reported_as_duplicates(self):
        request = make_request(items=(RequestedItem(description="", quantity=1), RequestedItem(description="", quantity=1)))
        self.assertEqual([c for c, _ in summary(validate_request(request))], ["missing_required", "missing_required"])

    def test_the_unit_and_quantity_do_not_affect_the_comparison(self):
        request = make_request(items=(RequestedItem(description="Pens", quantity=1, unit="box"), RequestedItem(description="Pens", quantity=50, unit="pcs")))
        self.assertEqual(summary(validate_request(request)), [("duplicate_item", "items[1].description")])


class ValueInSourceTests(unittest.TestCase):
    def test_value_not_in_source_is_a_warning(self):
        source = "Requester: Jane Doe\nItem: 10 | boxes | A4 paper"
        request = make_request(
            requester=Requester(name="Jane Doe", contact="not in the text"),
            items=(RequestedItem(description="A4 paper", quantity=10, unit="boxes"),),
            notes=None,
        )
        result = validate_request(request, source_text=source)
        self.assertEqual(summary(result), [("value_not_in_source", "requester.contact")])
        self.assertTrue(result.is_valid)

    def test_source_matching_ignores_case_and_whitespace(self):
        source = "REQUESTER:   jane\n   doe"
        request = make_request(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="Pens", quantity=1),), notes=None)
        findings = [f.field_path for f in validate_request(request, source_text=source).findings]
        self.assertNotIn("requester.name", findings)

    def test_quantity_is_looked_up_as_a_standalone_number(self):
        def item(q):
            return make_request(items=(RequestedItem(description="Pens", quantity=q),), notes=None, requester=Requester(name="Jane"))

        source = "Jane wants Pens, 10 of them. About 100 would be too many."
        self.assertEqual(validate_request(item(10), source_text=source).findings, ())
        self.assertEqual(summary(validate_request(item(1), source_text=source)), [("value_not_in_source", "items[0].quantity")])
        self.assertEqual(summary(validate_request(item(0.5), source_text="Jane Pens 10.5")), [("value_not_in_source", "items[0].quantity")])
        self.assertEqual(validate_request(item(Decimal("2.50")), source_text="Jane Pens 2.50").findings, ())

    def test_source_check_is_skipped_without_source_text(self):
        request = make_request(requester=Requester(name="Nobody In Particular"))
        self.assertEqual(validate_request(request).findings, ())

    def test_source_text_must_be_text(self):
        with self.assertRaises(TypeError):
            validate_request(make_request(), source_text=b"bytes")


class AmbiguityTests(unittest.TestCase):
    """Blocking for required fields, a warning for optional ones; one code per severity."""

    def test_ambiguity_on_a_required_field_blocks_even_when_a_value_is_present(self):
        request = make_request(items=(RequestedItem(description="Pens", quantity=5),))
        result = validate_request(request, ambiguities=[Ambiguity("items[0].quantity", "maybe five?")])
        self.assertEqual(summary(result), [("ambiguous_required_value", "items[0].quantity")])
        self.assertFalse(result.is_valid)
        self.assertEqual(result.findings[0].excerpt, "maybe five?")
        self.assertTrue(result.findings[0].is_blocking)

    def test_ambiguity_on_an_optional_field_is_a_warning(self):
        request = make_request(items=(RequestedItem(description="Pens", quantity=1),))
        result = validate_request(request, ambiguities=[Ambiguity("items[0].unit", "a couple of boxes")])
        self.assertEqual(summary(result), [("ambiguous_optional_value", "items[0].unit")])
        self.assertEqual(result.findings[0].excerpt, "a couple of boxes")
        self.assertTrue(result.is_valid)
        self.assertFalse(result.findings[0].is_blocking)

    def test_required_versus_optional_paths(self):
        required = ["requester.name", "items", "items[0]", "items[3].description", "items[12].quantity"]
        optional = ["requester.contact", "items[0].unit", "items[2].notes", "notes"]
        request = make_request()
        for path in required:
            with self.subTest(required=path):
                finding = validate_request(request, ambiguities=[Ambiguity(path, "?")]).findings[-1]
                self.assertIs(finding.code, FindingCode.AMBIGUOUS_REQUIRED_VALUE)
                self.assertTrue(finding.is_blocking)
        for path in optional:
            with self.subTest(optional=path):
                finding = validate_request(request, ambiguities=[Ambiguity(path, "?")]).findings[-1]
                self.assertIs(finding.code, FindingCode.AMBIGUOUS_OPTIONAL_VALUE)
                self.assertFalse(finding.is_blocking)

    def test_missing_required_is_not_weakened_by_an_ambiguity(self):
        request = make_request(items=(RequestedItem(description="Pens", quantity=None),))
        result = validate_request(request, ambiguities=(Ambiguity("items[0].quantity", "a few"),))
        self.assertEqual(
            summary(result),
            [("missing_required", "items[0].quantity"), ("ambiguous_required_value", "items[0].quantity")],
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(len(result.blocking), 2)

    def test_the_two_ambiguity_codes_have_different_severities(self):
        request = make_request()
        result = validate_request(request, ambiguities=[Ambiguity("notes", "?"), Ambiguity("requester.name", "?")])
        by_code = {f.code: f.severity for f in result.findings}
        self.assertEqual(len(by_code), 2)
        self.assertEqual(len(set(by_code.values())), 2)
        self.assertEqual({f.field_path for f in result.ambiguities}, {"notes", "requester.name"})

    def test_ambiguity_inputs_are_checked(self):
        with self.assertRaises(TypeError):
            validate_request(make_request(), ambiguities=["a few"])
        for bad_path in ("price", "items[01].quantity", "items[-1].quantity", "items[0].colour", "", "requester", "Items[0]"):
            with self.subTest(path=bad_path), self.assertRaises(ValueError):
                validate_request(make_request(), ambiguities=[Ambiguity(bad_path, "?")])

    def test_ambiguities_may_be_any_iterable_and_are_read_once(self):
        request = make_request()
        result = validate_request(request, ambiguities=(a for a in [Ambiguity("notes", "?")]))
        self.assertEqual(summary(result), [("ambiguous_optional_value", "notes")])


class SourceReferenceTests(unittest.TestCase):
    """source_refs are validated against the actual text, using the fixture coordinate convention."""

    TEXT = "Requester: Jane Doe\nItem: 10 | boxes | A4 paper\nNotes: soon"

    def request_with(self, *refs):
        return StructuredRequest(
            requester=Requester(name="Jane Doe"),
            items=(RequestedItem(description="A4 paper", quantity=10, unit="boxes"),),
            notes="soon",
            source_refs=tuple(refs),
        )

    def check(self, *refs, text=None):
        return validate_request(self.request_with(*refs), source_text=self.TEXT if text is None else text)

    def test_references_produced_by_the_fixture_extractor_are_all_valid(self):
        text = "Requester: Jane Doe\nContact: j@example.invalid\nItem: 10 | boxes | A4 paper | note\nItem: a few | | Staplers\nNotes: soon\n"
        extraction = DeterministicTestExtractor().extract(text)
        result = validate_request(extraction.candidate, source_text=text, ambiguities=extraction.ambiguities)
        self.assertEqual([f for f in result.findings if f.code is FindingCode.INVALID_SOURCE_REF], [])

    def test_a_correct_reference_is_accepted(self):
        self.assertEqual(self.check(SourceRef("requester.name", "Requester: Jane Doe", 1)).findings, ())

    def test_line_numbers_are_one_based(self):
        self.assertEqual(summary(self.check(SourceRef("requester.name", "Requester: Jane Doe", 0))), [("invalid_source_ref", "source_refs[0]")])
        self.assertEqual(self.check(SourceRef("notes", "Notes: soon", 3)).findings, ())

    def test_line_outside_the_source_is_invalid(self):
        for line in (4, 500, -1):
            with self.subTest(line=line):
                self.assertEqual(summary(self.check(SourceRef("notes", "Notes: soon", line))), [("invalid_source_ref", "source_refs[0]")])

    def test_excerpt_must_match_the_cited_line(self):
        self.assertEqual(summary(self.check(SourceRef("requester.name", "Requester: Someone Else", 1))), [("invalid_source_ref", "source_refs[0]")])
        self.assertEqual(summary(self.check(SourceRef("requester.name", "Requester: Jane Doe", 2))), [("invalid_source_ref", "source_refs[0]")])

    def test_unknown_or_non_value_paths_are_invalid(self):
        for path in ("price", "items[0]", "items", "requester", "items[01].quantity", "", None):
            with self.subTest(path=path):
                self.assertEqual(summary(self.check(SourceRef(path, "Notes: soon", 3))), [("invalid_source_ref", "source_refs[0]")])

    def test_reference_to_a_nonexistent_item_is_invalid(self):
        self.assertEqual(summary(self.check(SourceRef("items[1].quantity", "Item: 10 | boxes | A4 paper", 2))), [("invalid_source_ref", "source_refs[0]")])
        self.assertEqual(summary(self.check(SourceRef("items[99].quantity", "Item: 10 | boxes | A4 paper", 2))), [("invalid_source_ref", "source_refs[0]")])

    def test_bad_line_types_are_invalid(self):
        for line in (True, 1.0, "1"):
            with self.subTest(line=line):
                self.assertEqual(summary(self.check(SourceRef("requester.name", "Requester: Jane Doe", line))), [("invalid_source_ref", "source_refs[0]")])

    def test_a_reference_without_a_line_must_appear_verbatim(self):
        self.assertEqual(self.check(SourceRef("notes", "Notes: soon")).findings, ())
        self.assertEqual(summary(self.check(SourceRef("notes", "absent text"))), [("invalid_source_ref", "source_refs[0]")])

    def test_long_excerpts_follow_the_shortening_convention(self):
        long_line = "Notes: " + "x" * 400
        request = StructuredRequest(
            requester=Requester(name="Jane Doe"),
            items=(RequestedItem(description="A4 paper", quantity=1),),
            notes="x" * 400,
            source_refs=(SourceRef("notes", (long_line)[:197] + "...", 1),),
        )
        self.assertEqual([f for f in validate_request(request, source_text=long_line).findings if f.code is FindingCode.INVALID_SOURCE_REF], [])
        request_bad = dataclasses.replace(request, source_refs=(SourceRef("notes", long_line, 1),))
        self.assertEqual([f.field_path for f in validate_request(request_bad, source_text=long_line).findings if f.code is FindingCode.INVALID_SOURCE_REF], ["source_refs[0]"])

    def test_each_bad_reference_is_reported_by_its_own_index(self):
        result = self.check(
            SourceRef("requester.name", "Requester: Jane Doe", 1),
            SourceRef("notes", "wrong", 3),
            SourceRef("price", "x", 1),
        )
        self.assertEqual(summary(result), [("invalid_source_ref", "source_refs[1]"), ("invalid_source_ref", "source_refs[2]")])
        self.assertFalse(result.is_valid)

    def test_references_are_only_checked_when_source_text_is_supplied(self):
        bad = self.request_with(SourceRef("price", "nowhere", 999))
        self.assertEqual(validate_request(bad).findings, ())  # not checked, and nothing is claimed about them

    def test_malformed_reference_containers_are_invalid_types(self):
        self.assertEqual(summary(validate_request(make_request(source_refs="x"))), [("invalid_type", "source_refs")])
        self.assertEqual(summary(validate_request(make_request(source_refs=("x",)))), [("invalid_type", "source_refs[0]")])


if __name__ == "__main__":
    unittest.main()
