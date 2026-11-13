"""DB-free tests for the draft JSON form and the pure extraction step.

``apps.intake.draft_json`` and ``apps.intake.extraction`` import no Django, so these run under plain
``unittest`` too. The extraction step must delegate: its valid result is exactly what
``create_request_intake_revision`` produces, and its invalid result carries the findings of the one
validation that call performs.
"""

import copy
import dataclasses
import json
import unittest
import uuid
from decimal import Decimal

from apps.intake.draft_json import (
    ambiguities_from_json,
    ambiguities_to_json,
    draft_from_json,
    draft_to_json,
)
from apps.intake.errors import UnsupportedDraftError
from apps.intake.extraction import EXTRACTION_REVISION_NUMBER, build_extraction
from tests.intake_support import VALID_TEXT
from workflows.request_intake import (
    Ambiguity,
    CanonicalizationError,
    DeterministicTestExtractor,
    ExtractionResult,
    RequestedItem,
    Requester,
    SerializationSafetyError,
    SourceRef,
    StructuredRequest,
    ValidationLimits,
)
from workflows.request_intake import schema
from workflows.revision_approval import RevisionFinding
from workflows.revision_approval.request_intake_revision import create_request_intake_revision

CASE_ID = uuid.UUID(int=0xC1)
RUN_ID = uuid.UUID(int=0xA1)
REVISION_ID = uuid.UUID(int=0xB1)

INVALID_TEXT = "Contact: nobody@example.invalid\nItem: a few | boxes | Pens\nItem: 0 | | Stapler\n"


def extract(text, **kwargs):
    return build_extraction(text, case_id=CASE_ID, run_id=RUN_ID, revision_id=REVISION_ID, **kwargs)


class FixedExtractor:
    """A test extractor returning a prepared result (stands in for any provider)."""

    name = "fixed"
    version = "9"

    def __init__(self, candidate, ambiguities=()):
        self.result = ExtractionResult(candidate=candidate, ambiguities=tuple(ambiguities), extractor_name=self.name, extractor_version=self.version)
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return self.result


class DraftJsonTests(unittest.TestCase):
    def drafts(self):
        yield DeterministicTestExtractor().extract(VALID_TEXT).candidate
        yield DeterministicTestExtractor().extract(INVALID_TEXT).candidate
        yield StructuredRequest()
        yield StructuredRequest(
            requester=Requester(name="Zoë \U0001f600", contact=""),
            items=(RequestedItem(description="x", quantity=Decimal("2.50"), unit="", notes="n"), RequestedItem(quantity=Decimal("1E+3"))),
            notes="line1\nline2",
            source_refs=(SourceRef("notes", "Notes: x", 4), SourceRef("requester.name", "x", None)),
        )

    def test_the_json_form_round_trips_exactly(self):
        for draft in self.drafts():
            with self.subTest(draft=repr(draft)[:60]):
                data = draft_to_json(draft)
                self.assertEqual(draft_from_json(data), draft)
                self.assertEqual(draft_from_json(json.loads(json.dumps(data))), draft)  # survives a JSON column

    def test_quantities_keep_their_exact_digits(self):
        draft = StructuredRequest(items=(RequestedItem(description="a", quantity=Decimal("2.50")), RequestedItem(description="b", quantity=Decimal("1E+3"))))
        data = draft_to_json(draft)
        self.assertEqual([item["quantity"] for item in data["items"]], ["2.50", "1E+3"])
        restored = draft_from_json(data)
        self.assertEqual([str(item.quantity) for item in restored.items], ["2.50", "1E+3"])  # not 2.5 and not 1000

    def test_the_output_is_plain_json_without_floats(self):
        text = json.dumps(draft_to_json(DeterministicTestExtractor().extract(VALID_TEXT).candidate), allow_nan=False)
        self.assertNotRegex(text, r"\d\.\d+[,}\]]")  # no JSON number with a fraction: quantities are strings

    def test_missing_values_stay_null_and_empty_text_stays_empty(self):
        data = draft_to_json(StructuredRequest(requester=Requester(name="", contact=None)))
        self.assertEqual(data["requester"], {"name": "", "contact": None})
        self.assertEqual(draft_from_json(data).requester, Requester(name="", contact=None))

    def test_to_json_does_not_mutate_or_alias_the_draft(self):
        draft = DeterministicTestExtractor().extract(VALID_TEXT).candidate
        before = copy.deepcopy(draft)
        data = draft_to_json(draft)
        data["items"].append({"x": 1})
        data["requester"]["name"] = "changed"
        self.assertEqual(draft, before)

    def test_a_value_the_form_cannot_store_exactly_is_refused_not_converted(self):
        for item in (RequestedItem(description="a", quantity=True), RequestedItem(description="a", quantity="ten"), RequestedItem(description=5, quantity=Decimal(1))):
            with self.subTest(item=repr(item)), self.assertRaises(UnsupportedDraftError):
                draft_to_json(StructuredRequest(items=(item,)))
        with self.assertRaises(UnsupportedDraftError):
            draft_to_json(StructuredRequest(requester=Requester(name=7)))
        with self.assertRaises(UnsupportedDraftError):
            draft_to_json(StructuredRequest(source_refs=(SourceRef("notes", "x", True),)))

    def test_a_non_draft_is_a_type_error(self):
        with self.assertRaises(TypeError):
            draft_to_json({"requester": {}})

    def test_from_json_is_strict(self):
        good = draft_to_json(DeterministicTestExtractor().extract(VALID_TEXT).candidate)
        broken = []
        for mutate in (
            lambda d: d.pop("notes"),
            lambda d: d.update(extra=1),
            lambda d: d["requester"].pop("contact"),
            lambda d: d["items"][0].update(price="1"),
            lambda d: d["items"][0].update(quantity=1.5),
            lambda d: d["items"][0].update(quantity="abc"),
            lambda d: d["items"][0].update(quantity=3),
            lambda d: d.update(items="x"),
            lambda d: d["source_refs"][0].update(line="1"),
            lambda d: d["source_refs"][0].update(line=True),
        ):
            data = copy.deepcopy(good)
            mutate(data)
            broken.append(data)
        broken += [None, [], "x", {}]
        for data in broken:
            with self.subTest(data=repr(data)[:50]), self.assertRaises(UnsupportedDraftError):
                draft_from_json(data)

    def test_the_stored_field_lists_match_the_schema_so_a_schema_change_cannot_pass_unnoticed(self):
        draft = draft_to_json(StructuredRequest(items=(RequestedItem(),), source_refs=(SourceRef("notes", "x"),)))
        self.assertEqual(set(draft), {f.name for f in dataclasses.fields(schema.StructuredRequest)})
        self.assertEqual(set(draft["requester"]), {f.name for f in dataclasses.fields(schema.Requester)})
        self.assertEqual(set(draft["items"][0]), {f.name for f in dataclasses.fields(schema.RequestedItem)})
        self.assertEqual(set(draft["source_refs"][0]), {f.name for f in dataclasses.fields(schema.SourceRef)})

    def test_ambiguities_round_trip(self):
        ambiguities = (Ambiguity("items[0].quantity", "a few"), Ambiguity("notes", "?"))
        data = ambiguities_to_json(ambiguities)
        self.assertEqual(ambiguities_from_json(json.loads(json.dumps(data))), ambiguities)
        self.assertEqual(set(data[0]), {f.name for f in dataclasses.fields(schema.Ambiguity)})

    def test_stored_ambiguities_are_checked_strictly(self):
        for data in (None, {}, [{"field_path": "notes"}], [{"field_path": "notes", "excerpt": "x", "extra": 1}], ["x"]):
            with self.subTest(data=repr(data)), self.assertRaises(UnsupportedDraftError):
                ambiguities_from_json(data)


class ValidExtractionTests(unittest.TestCase):
    def test_a_valid_text_gives_revision_one_with_the_supplied_identifiers(self):
        decision = extract(VALID_TEXT)
        self.assertTrue(decision.is_valid)
        revision = decision.revision
        self.assertEqual((revision.case_id, revision.run_id, revision.revision_id), (CASE_ID, RUN_ID, REVISION_ID))
        self.assertEqual(revision.revision_number, EXTRACTION_REVISION_NUMBER)
        self.assertEqual(EXTRACTION_REVISION_NUMBER, 1)
        self.assertEqual(revision.blocking_findings, ())

    def test_the_result_is_exactly_what_the_existing_revision_function_produces(self):
        extractor = DeterministicTestExtractor()
        result = extractor.extract(VALID_TEXT)
        expected = create_request_intake_revision(
            result.candidate,
            case_id=CASE_ID,
            run_id=RUN_ID,
            revision_id=REVISION_ID,
            revision_number=1,
            source_text=VALID_TEXT,
            ambiguities=result.ambiguities,
        )
        decision = extract(VALID_TEXT)
        self.assertEqual(decision.revision, expected)  # same bytes, same hash, same findings
        self.assertEqual(decision.findings, expected.findings)
        self.assertEqual(decision.draft, result.candidate)

    def test_provenance_comes_from_the_extractor(self):
        decision = extract(VALID_TEXT)
        self.assertEqual((decision.extractor_name, decision.extractor_version), (DeterministicTestExtractor.name, DeterministicTestExtractor.version))

    def test_it_is_deterministic(self):
        first, second = extract(VALID_TEXT), extract(VALID_TEXT)
        self.assertEqual(first, second)
        self.assertEqual(first.revision.content_hash, second.revision.content_hash)

    def test_warnings_stay_on_a_valid_revision(self):
        text = "Requester: Jane\nItem: 1 | | Pens\nItem: 2 | | pens\n"
        decision = extract(text)
        self.assertTrue(decision.is_valid)
        self.assertIn(("duplicate_item", "items[1].description"), [(f.code, f.field_path) for f in decision.revision.warnings])
        self.assertEqual(decision.findings, decision.revision.findings)

    def test_the_extractor_is_called_once_with_the_exact_text(self):
        extractor = FixedExtractor(DeterministicTestExtractor().extract(VALID_TEXT).candidate)
        extract(VALID_TEXT, extractor=extractor)
        self.assertEqual(extractor.calls, [VALID_TEXT])

    def test_the_text_is_never_changed(self):
        text = VALID_TEXT + "   "
        original = str(text)
        extract(text)
        self.assertEqual(text, original)


class InvalidExtractionTests(unittest.TestCase):
    def test_a_draft_with_blocking_findings_has_no_revision_and_keeps_every_finding(self):
        decision = extract(INVALID_TEXT)
        self.assertFalse(decision.is_valid)
        self.assertIsNone(decision.revision)
        blocking = {(f.code, f.field_path) for f in decision.findings if f.blocking}
        self.assertIn(("missing_required", "requester.name"), blocking)
        self.assertIn(("ambiguous_required_value", "items[0].quantity"), blocking)
        self.assertIn(("invalid_value", "items[1].quantity"), blocking)
        for finding in decision.findings:
            self.assertIsInstance(finding, RevisionFinding)

    def test_the_draft_is_kept_and_nothing_is_invented(self):
        decision = extract(INVALID_TEXT)
        self.assertIsNone(decision.draft.requester.name)
        self.assertIsNone(decision.draft.items[0].quantity)
        self.assertEqual(decision.draft.requester.contact, "nobody@example.invalid")
        self.assertEqual(decision.ambiguities, (Ambiguity("items[0].quantity", "a few"),))

    def test_text_with_no_recognised_lines_is_invalid_not_an_error(self):
        decision = extract("Please send pens.")
        self.assertFalse(decision.is_valid)
        self.assertIn(("missing_required", "items"), [(f.code, f.field_path) for f in decision.findings])

    def test_the_findings_equal_the_existing_validation_result(self):
        from workflows.request_intake import validate_request
        from workflows.revision_approval.request_intake_revision import findings_from_validation

        result = DeterministicTestExtractor().extract(INVALID_TEXT)
        expected = findings_from_validation(validate_request(result.candidate, source_text=INVALID_TEXT, ambiguities=result.ambiguities))
        self.assertEqual(extract(INVALID_TEXT).findings, expected)

    def test_limits_are_passed_to_the_existing_validation(self):
        decision = extract(VALID_TEXT, limits=ValidationLimits(max_quantity=5))
        self.assertFalse(decision.is_valid)
        self.assertIn(("exceeds_maximum", "items[0].quantity"), [(f.code, f.field_path) for f in decision.findings])


class SafetyRefusalTests(unittest.TestCase):
    def test_a_value_that_validates_but_cannot_be_serialized_is_an_error_not_an_invalid_draft(self):
        candidate = StructuredRequest(requester=Requester(name="Jane"), items=(RequestedItem(description="x", quantity=Decimal("1E+1000000")),))
        with self.assertRaises(SerializationSafetyError):
            extract("Requester: Jane", extractor=FixedExtractor(candidate))

    def test_that_error_is_a_canonicalization_error(self):
        self.assertTrue(issubclass(SerializationSafetyError, CanonicalizationError))

    def test_an_extractor_exception_propagates_unchanged(self):
        class Broken:
            name, version = "broken", "1"

            def extract(self, text):
                raise RuntimeError("provider down")

        with self.assertRaises(RuntimeError):
            extract(VALID_TEXT, extractor=Broken())


if __name__ == "__main__":
    unittest.main()
