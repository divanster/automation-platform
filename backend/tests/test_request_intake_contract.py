"""Pure-domain tests (standard-library unittest, no Django) pinning the findings contract.

Codes, severities, and field paths are the INITIAL STABLE CONTRACT (findings.py). These
tests pin the whole table. If one fails because the contract is being changed on purpose,
that change is incompatible: bump ``FINDINGS_CONTRACT_VERSION``, document it, and decide
what happens to findings stored under the old version.
"""

import re
import unittest

from tests.intake_support import make_request, summary
from workflows.request_intake import (
    FINDINGS_CONTRACT_VERSION,
    SEVERITY_BY_CODE,
    Ambiguity,
    Finding,
    FindingCode,
    RequestedItem,
    Requester,
    Severity,
    SourceRef,
    StructuredRequest,
    ValidationLimits,
    parse_field_path,
    validate_request,
)
from workflows.request_intake.findings import AMBIGUITY_CODES, INVALID_VALUE_CODES, MISSING_DATA_CODES

# The golden table. Any difference is an incompatible change.
GOLDEN_SEVERITIES = {
    "missing_required": "blocking",
    "invalid_type": "blocking",
    "invalid_value": "blocking",
    "exceeds_maximum": "blocking",
    "exceeds_length": "blocking",
    "invalid_source_ref": "blocking",
    "ambiguous_required_value": "blocking",
    "ambiguous_optional_value": "warning",
    "duplicate_item": "warning",
    "value_not_in_source": "warning",
}

# Every path a finding can carry: value paths, containers, and source-reference entries.
FINDING_PATH = re.compile(
    r"requester(\.(name|contact))?|notes|items(\[(0|[1-9][0-9]*)\](\.(description|quantity|unit|notes))?)?|source_refs(\[(0|[1-9][0-9]*)\])?"
)


class ContractVersionTests(unittest.TestCase):
    def test_the_initial_contract_is_version_one(self):
        self.assertEqual(FINDINGS_CONTRACT_VERSION, 1)

    def test_the_golden_table_is_unchanged(self):
        actual = {code.value: severity.value for code, severity in SEVERITY_BY_CODE.items()}
        self.assertEqual(actual, GOLDEN_SEVERITIES, "incompatible change: bump FINDINGS_CONTRACT_VERSION")

    def test_every_code_has_exactly_one_severity(self):
        self.assertEqual(set(SEVERITY_BY_CODE), set(FindingCode))
        for code in FindingCode:
            self.assertIsInstance(SEVERITY_BY_CODE[code], Severity)

    def test_code_values_are_lowercase_snake_case_strings(self):
        for code in FindingCode:
            self.assertRegex(code.value, r"^[a-z]+(_[a-z]+)*$")
            self.assertEqual(str(code), code.value)

    def test_blocking_and_warning_ambiguity_use_different_codes(self):
        self.assertIs(SEVERITY_BY_CODE[FindingCode.AMBIGUOUS_REQUIRED_VALUE], Severity.BLOCKING)
        self.assertIs(SEVERITY_BY_CODE[FindingCode.AMBIGUOUS_OPTIONAL_VALUE], Severity.WARNING)
        self.assertEqual(AMBIGUITY_CODES, {FindingCode.AMBIGUOUS_REQUIRED_VALUE, FindingCode.AMBIGUOUS_OPTIONAL_VALUE})
        self.assertNotIn("ambiguous_value", {c.value for c in FindingCode})

    def test_missing_and_invalid_groups_do_not_overlap_and_are_blocking(self):
        self.assertFalse(MISSING_DATA_CODES & INVALID_VALUE_CODES)
        for code in MISSING_DATA_CODES | INVALID_VALUE_CODES:
            self.assertIs(SEVERITY_BY_CODE[code], Severity.BLOCKING)

    def test_severity_follows_the_code(self):
        self.assertTrue(Finding(FindingCode.MISSING_REQUIRED, "x", "m").is_blocking)
        self.assertIs(Finding(FindingCode.DUPLICATE_ITEM, "x", "m").severity, Severity.WARNING)


class PathGrammarTests(unittest.TestCase):
    VALID = [
        ("requester.name", True),
        ("requester.contact", False),
        ("items", True),
        ("items[0]", True),
        ("items[0].description", True),
        ("items[0].quantity", True),
        ("items[0].unit", False),
        ("items[0].notes", False),
        ("items[10].quantity", True),
        ("notes", False),
    ]
    INVALID = ["", "requester", "item[0]", "items[00]", "items[01].quantity", "items[-1]", "items[0].price", "Items[0]", "items[0]quantity", "items[ 0 ]", "price", "items[1.5]", "items[\u0663]", None, 5]

    def test_valid_paths_and_their_required_flag(self):
        for path, required in self.VALID:
            with self.subTest(path=path):
                parsed = parse_field_path(path)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.required, required)

    def test_invalid_paths_are_rejected(self):
        for path in self.INVALID:
            with self.subTest(path=repr(path)):
                self.assertIsNone(parse_field_path(path))

    def test_indexes_are_zero_based_decimal_without_leading_zeros(self):
        self.assertEqual(parse_field_path("items[7].notes").item_index, 7)
        self.assertEqual(parse_field_path("items[0]").item_index, 0)
        self.assertIsNone(parse_field_path("items[007]"))

    def test_only_value_fields_are_fields(self):
        self.assertTrue(parse_field_path("notes").is_field)
        self.assertFalse(parse_field_path("items").is_field)
        self.assertFalse(parse_field_path("items[0]").is_field)

    def test_every_path_in_every_finding_follows_the_grammar(self):
        requests = [
            make_request(requester=None, items=None, source_refs="x"),
            make_request(requester="x", items="y", notes=3),
            make_request(requester=Requester(name=1, contact=2), items=(1, RequestedItem(), RequestedItem(description=5, quantity="q", unit=7, notes=[1])), source_refs=(1,)),
            make_request(items=(RequestedItem(description="A", quantity=0), RequestedItem(description="a", quantity=-1)), source_refs=(SourceRef("price", "x", 1), SourceRef("notes", "y", 99))),
        ]
        seen = set()
        for request in requests:
            result = validate_request(request, limits=ValidationLimits(max_text_length=1, max_quantity=1), source_text="nothing here", ambiguities=[Ambiguity("items[0]", "?"), Ambiguity("notes", "?")])
            for finding in result.findings:
                seen.add(finding.field_path)
                self.assertRegex(finding.field_path, FINDING_PATH)
                self.assertTrue(FINDING_PATH.fullmatch(finding.field_path), finding.field_path)
        self.assertGreater(len(seen), 8)

    def test_every_code_can_be_produced(self):
        produced = set()
        batteries = [
            (make_request(requester=Requester(name=None)), {}),
            (make_request(items=(RequestedItem(description="A", quantity="x", unit=1),)), {}),
            (make_request(items=(RequestedItem(description="A", quantity=0),)), {}),
            (make_request(items=(RequestedItem(description="A", quantity=9),)), {"limits": ValidationLimits(max_quantity=1)}),
            (make_request(notes="long"), {"limits": ValidationLimits(max_text_length=1)}),
            (make_request(source_refs=(SourceRef("price", "x", 1),)), {"source_text": "t"}),
            (make_request(), {"ambiguities": [Ambiguity("items[0].quantity", "?"), Ambiguity("notes", "?")]}),
            (make_request(items=(RequestedItem(description="A", quantity=1), RequestedItem(description="a", quantity=1))), {}),
            (make_request(), {"source_text": "nothing relevant"}),
        ]
        for request, kwargs in batteries:
            produced |= {finding.code for finding in validate_request(request, **kwargs).findings}
        self.assertEqual(produced, set(FindingCode))


class FindingBehaviorContractTests(unittest.TestCase):
    def test_missing_data_is_distinguished_from_invalid_values(self):
        request = make_request(
            requester=Requester(name=None),
            items=(RequestedItem(description="Pens", quantity=-1), RequestedItem(description="Ink", quantity="x")),
        )
        result = validate_request(request)
        self.assertEqual(summary(result), [("missing_required", "requester.name"), ("invalid_value", "items[0].quantity"), ("invalid_type", "items[1].quantity")])
        self.assertEqual([f.field_path for f in result.missing_required], ["requester.name"])
        self.assertEqual([f.field_path for f in result.invalid_values], ["items[0].quantity", "items[1].quantity"])
        self.assertFalse(result.is_valid)

    def test_messages_never_echo_the_offending_value(self):
        sentinel = "SENTINEL-VALUE-123"
        request = StructuredRequest(
            requester=Requester(name=sentinel * 5),
            items=(RequestedItem(description=sentinel, quantity=sentinel),),
            notes=sentinel * 5,
            source_refs=(SourceRef(sentinel, sentinel, 1),),
        )
        result = validate_request(request, limits=ValidationLimits(max_text_length=5), source_text="unrelated")
        self.assertTrue(result.findings)
        for finding in result.findings:
            self.assertNotIn(sentinel, finding.message)
            self.assertNotIn(sentinel, finding.field_path)

    def test_field_paths_use_zero_based_indexes(self):
        request = make_request(items=(RequestedItem(description="a", quantity=1), RequestedItem(description="b", quantity=0)))
        self.assertEqual(summary(validate_request(request)), [("invalid_value", "items[1].quantity")])

    def test_findings_are_ordered_deterministically(self):
        request = make_request(
            requester=Requester(name=None, contact=5),
            items=(RequestedItem(description="Pens", quantity=0), RequestedItem(description="pens", quantity=1)),
            notes=3,
            source_refs=(SourceRef("notes", "wrong", 1),),
        )
        kwargs = {"source_text": "x", "ambiguities": (Ambiguity("items[0].unit", "?"),)}
        result = validate_request(request, **kwargs)
        self.assertEqual(
            summary(result),
            [
                ("missing_required", "requester.name"),
                ("invalid_type", "requester.contact"),
                ("invalid_value", "items[0].quantity"),
                ("invalid_type", "notes"),
                ("duplicate_item", "items[1].description"),
                ("value_not_in_source", "items[0].description"),
                ("value_not_in_source", "items[1].description"),
                ("value_not_in_source", "items[1].quantity"),
                ("invalid_source_ref", "source_refs[0]"),
                ("ambiguous_optional_value", "items[0].unit"),
            ],
        )
        self.assertEqual(summary(result), summary(validate_request(request, **kwargs)))

    def test_blocking_findings_are_what_make_a_request_invalid(self):
        warnings_only = validate_request(
            make_request(items=(RequestedItem(description="A", quantity=1), RequestedItem(description="a", quantity=1))),
            source_text="unrelated",
            ambiguities=[Ambiguity("notes", "?")],
        )
        self.assertTrue(warnings_only.warnings)
        self.assertFalse(warnings_only.blocking)
        self.assertTrue(warnings_only.is_valid)


if __name__ == "__main__":
    unittest.main()
