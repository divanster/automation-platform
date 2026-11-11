"""Pure-domain tests (standard-library unittest, no Django) for canonical format version 1.

Covers canonical serialization, deterministic bytes, and SHA-256 content hashing of a
request-intake draft (workflows/request_intake/canonical.py). This file is ASCII only:
non-ASCII text is built with chr() and expected UTF-8 bytes are written as hex escapes.
"""

import copy
import dataclasses
import decimal
import hashlib
import json
import os
import subprocess
import sys
import tracemalloc
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import workflows.request_intake.canonical as canonical_module
from tests.intake_support import VALID_TEXT, find_mutable_state
from workflows.request_intake import (
    CANONICAL_FORMAT_VERSION,
    HASH_ALGORITHM,
    Ambiguity,
    CanonicalContent,
    CanonicalizationError,
    DeterministicTestExtractor,
    DraftNotValidError,
    RequestedItem,
    Requester,
    SerializationSafetyError,
    SourceRef,
    SourceTextRequiredError,
    StructuredRequest,
    ValidationLimits,
    canonical_decimal_string,
    canonicalize_request,
    validate_request,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

E_ACUTE = chr(0xE9)  # e with acute accent, precomposed
COMBINING_ACUTE = chr(0x301)


def D(text):
    return Decimal(text)


def simple(**overrides):
    """A minimal valid draft: one requester, one item."""
    fields = {
        "requester": Requester(name="Jane Doe"),
        "items": (RequestedItem(description="Pens", quantity=1),),
    }
    fields.update(overrides)
    return StructuredRequest(**fields)


def source_for(request):
    """A source text consistent with the draft's own source references.

    Source references now REQUIRE a source text that verifies them. Numbered references are
    placed on their lines (other lines are filler); references without a line are appended.
    """
    refs = [ref for ref in request.source_refs if isinstance(ref, SourceRef) and isinstance(ref.excerpt, str)]
    numbered = {}
    for ref in refs:
        if isinstance(ref.line, int) and not isinstance(ref.line, bool) and ref.line >= 1:
            numbered.setdefault(ref.line, ref.excerpt)
    lines = [numbered.get(n, "filler line") for n in range(1, max(numbered, default=0) + 1)]
    lines += [ref.excerpt for ref in refs if ref.line is None]
    return "\n".join(lines) + "\n"


def canon(request, **kwargs):
    """Canonicalize, supplying a consistent source text when the draft has references.

    Tests of the source-text gate itself call ``canonicalize_request`` directly.
    """
    refs = getattr(request, "source_refs", ())
    if isinstance(refs, (tuple, list)) and refs and "source_text" not in kwargs:
        kwargs["source_text"] = source_for(request)
    return canonicalize_request(request, **kwargs)


# --- the golden fixture --------------------------------------------------------------
# The expected bytes below were typed by hand (not produced by the code under test). The
# expected hash was calculated independently over exactly those bytes with three tools:
# GNU coreutils sha256sum, PowerShell Get-FileHash, and openssl dgst -sha256. All three agreed.

GOLDEN_BYTES = (
    b'{"canonical_format_version":1,"payload":{"items":[{"description":"A4 paper","notes":"","quantity":"10","unit":"boxes"},{"description":"Caf'
    b"\xc3\xa9"
    b' beans","notes":null,"quantity":"2.5","unit":null}],"notes":"Deliver to \\"Room 5\\"","requester":{"contact":null,"name":"Jane Doe"},"source_refs":[{"excerpt":"Requester: Jane Doe","field_path":"requester.name","line":1},{"excerpt":"Item: 2.50 | | Caf'
    b"\xc3\xa9"
    b' beans","field_path":"items[1].description","line":null}]}}'
)
GOLDEN_SHA256 = "b161c35b51484a3ff92b96e44ccfb7e1f62e1c37c6acaeec4a8d692c9e0a9c67"
GOLDEN_LENGTH = 450


def golden_request():
    return StructuredRequest(
        requester=Requester(name="Jane Doe", contact=None),
        items=(
            RequestedItem(description="A4 paper", quantity=D("10.0"), unit="boxes", notes=""),
            RequestedItem(description="Caf" + E_ACUTE + " beans", quantity=D("2.50"), unit=None, notes=None),
        ),
        notes='Deliver to "Room 5"',
        source_refs=(
            SourceRef("requester.name", "Requester: Jane Doe", 1),
            SourceRef("items[1].description", "Item: 2.50 | | Caf" + E_ACUTE + " beans", None),
        ),
    )


# The source text that verifies the golden draft's references. It contains an UNREFERENCED line;
# the text itself is never part of the canonical bytes.
GOLDEN_SOURCE_TEXT = "Requester: Jane Doe\nAn unreferenced line that is not part of the content.\nItem: 2.50 | | Caf" + E_ACUTE + " beans\n"


class GoldenFixtureTests(unittest.TestCase):
    def test_the_golden_bytes_are_exactly_the_fixed_expectation(self):
        content = canon(golden_request(), source_text=GOLDEN_SOURCE_TEXT)
        self.assertEqual(content.canonical_bytes, GOLDEN_BYTES)
        self.assertEqual(len(content.canonical_bytes), GOLDEN_LENGTH)

    def test_the_golden_hash_is_the_independently_calculated_one(self):
        content = canon(golden_request(), source_text=GOLDEN_SOURCE_TEXT)
        self.assertEqual(content.content_hash, GOLDEN_SHA256)

    def test_the_golden_source_text_is_not_part_of_the_bytes(self):
        content = canon(golden_request(), source_text=GOLDEN_SOURCE_TEXT)
        self.assertNotIn(b"unreferenced", content.canonical_bytes)

    def test_the_fixed_literals_are_self_consistent(self):
        # The literal bytes really hash to the independently calculated value.
        self.assertEqual(len(GOLDEN_BYTES), GOLDEN_LENGTH)
        self.assertEqual(hashlib.sha256(GOLDEN_BYTES).hexdigest(), GOLDEN_SHA256)

    def test_the_golden_bytes_are_valid_utf8_without_a_bom_and_parse_as_json(self):
        self.assertFalse(GOLDEN_BYTES.startswith(b"\xef\xbb\xbf"))
        parsed = json.loads(GOLDEN_BYTES.decode("utf-8"))
        self.assertEqual(parsed["canonical_format_version"], 1)
        self.assertEqual(parsed["payload"]["items"][1]["description"], "Caf" + E_ACUTE + " beans")

    def test_equivalent_decimal_spellings_of_the_golden_draft_hash_identically(self):
        variant = dataclasses.replace(
            golden_request(),
            items=(
                RequestedItem(description="A4 paper", quantity=10, unit="boxes", notes=""),
                RequestedItem(description="Caf" + E_ACUTE + " beans", quantity=D("2.5000"), unit=None, notes=None),
            ),
        )
        self.assertEqual(canon(variant, source_text=GOLDEN_SOURCE_TEXT).content_hash, GOLDEN_SHA256)


class ResultShapeTests(unittest.TestCase):
    def test_one_operation_returns_version_bytes_and_hash_together(self):
        content = canon(simple())
        self.assertIsInstance(content, CanonicalContent)
        self.assertEqual(content.canonical_format_version, CANONICAL_FORMAT_VERSION)
        self.assertEqual(CANONICAL_FORMAT_VERSION, 1)
        self.assertIsInstance(content.canonical_bytes, bytes)
        self.assertEqual(content.hash_algorithm, HASH_ALGORITHM)
        self.assertEqual(HASH_ALGORITHM, "sha256")
        self.assertRegex(content.content_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(content.content_hash, hashlib.sha256(content.canonical_bytes).hexdigest())

    def test_the_hash_cannot_describe_different_bytes(self):
        good = canon(simple())
        with self.assertRaises(ValueError):
            CanonicalContent(1, good.canonical_bytes + b" ", good.content_hash, "sha256", good.validation)
        with self.assertRaises(ValueError):
            CanonicalContent(1, good.canonical_bytes, "0" * 64, "sha256", good.validation)
        with self.assertRaises(ValueError):
            CanonicalContent(1, good.canonical_bytes, good.content_hash, "md5", good.validation)
        with self.assertRaises(TypeError):
            CanonicalContent(1, good.canonical_bytes.decode("utf-8"), good.content_hash, "sha256", good.validation)

    def test_the_version_is_inside_the_hashed_bytes(self):
        content = canon(simple())
        parsed = json.loads(content.canonical_bytes.decode("utf-8"))
        self.assertEqual(parsed["canonical_format_version"], content.canonical_format_version)
        self.assertEqual(set(parsed), {"canonical_format_version", "payload"})
        self.assertTrue(content.canonical_bytes.startswith(b'{"canonical_format_version":1,"payload":{'))


class FormatRulesTests(unittest.TestCase):
    def setUp(self):
        self.content = canon(golden_request())
        self.text = self.content.canonical_bytes.decode("utf-8")
        self.parsed = json.loads(self.text)

    def test_the_payload_has_fixed_field_names_and_every_optional_field_is_present(self):
        payload = self.parsed["payload"]
        self.assertEqual(sorted(payload), ["items", "notes", "requester", "source_refs"])
        self.assertEqual(sorted(payload["requester"]), ["contact", "name"])
        for item in payload["items"]:
            self.assertEqual(sorted(item), ["description", "notes", "quantity", "unit"])
        for ref in payload["source_refs"]:
            self.assertEqual(sorted(ref), ["excerpt", "field_path", "line"])

    def test_a_minimal_draft_still_writes_every_optional_field_as_null(self):
        parsed = json.loads(canon(simple()).canonical_bytes.decode("utf-8"))
        self.assertEqual(
            parsed["payload"],
            {
                "requester": {"name": "Jane Doe", "contact": None},
                "items": [{"description": "Pens", "quantity": "1", "unit": None, "notes": None}],
                "notes": None,
                "source_refs": [],
            },
        )

    def test_the_text_is_exactly_the_compact_sorted_form(self):
        again = json.dumps(self.parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(again.encode("utf-8"), self.content.canonical_bytes)

    def test_there_is_no_whitespace_between_tokens_and_no_trailing_newline(self):
        outside_strings = []
        in_string = escaped = False
        for ch in self.text:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            else:
                outside_strings.append(ch)
        self.assertNotIn(" ", outside_strings)
        self.assertNotIn("\n", outside_strings)
        self.assertFalse(self.content.canonical_bytes.endswith(b"\n"))

    def test_it_is_utf8_without_a_bom(self):
        self.assertFalse(self.content.canonical_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.content.canonical_bytes.decode("utf-8").encode("utf-8"), self.content.canonical_bytes)

    def test_quantities_are_strings_not_numbers(self):
        for item in self.parsed["payload"]["items"]:
            self.assertIsInstance(item["quantity"], str)

    def test_no_floats_nan_or_infinity_appear(self):
        for item in self.parsed["payload"]["items"]:
            self.assertRegex(item["quantity"], r"^-?[0-9]+(\.[0-9]+)?$")  # plain digits only, never an exponent
        for ref in self.parsed["payload"]["source_refs"]:
            self.assertIsInstance(ref["line"], (int, type(None)))
            self.assertNotIsInstance(ref["line"], bool)
        self.assertNotIn(b"NaN", self.content.canonical_bytes)
        self.assertNotIn(b"Infinity", self.content.canonical_bytes)

    def test_keys_are_sorted_at_every_level(self):
        def check(node):
            if isinstance(node, dict):
                self.assertEqual(list(node), sorted(node))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(json.loads(self.text, object_pairs_hook=lambda pairs: dict(pairs)))


class RepeatabilityTests(unittest.TestCase):
    def test_repeated_calls_give_identical_bytes_and_hash(self):
        results = [canon(golden_request()) for _ in range(5)]
        for result in results[1:]:
            self.assertEqual(result.canonical_bytes, results[0].canonical_bytes)
            self.assertEqual(result.content_hash, results[0].content_hash)

    def test_equal_drafts_built_separately_are_identical(self):
        self.assertEqual(canon(golden_request()).content_hash, canon(golden_request()).content_hash)

    def test_results_are_independent_objects(self):
        a, b = canon(golden_request()), canon(golden_request())
        self.assertIsNot(a, b)
        self.assertEqual(a, b)

    def test_the_result_does_not_depend_on_python_hash_randomization(self):
        # No dict/set ordering, timestamps, or random values leak in: run in fresh processes
        # with different hash seeds and compare.
        program = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from decimal import Decimal\n"
            "from workflows.request_intake import *\n"
            "r = StructuredRequest(requester=Requester(name='J', contact='c'),\n"
            "    items=(RequestedItem(description='a', quantity=Decimal('1.50'), unit='u', notes='n'),\n"
            "           RequestedItem(description='b', quantity=3)), notes='z',\n"
            "    source_refs=(SourceRef('notes', 'Notes: z', 2),))\n"
            "print(canonicalize_request(r, source_text='filler line\\nNotes: z\\n').content_hash)\n"
        )
        hashes = set()
        for seed in ("0", "1", "4242", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            completed = subprocess.run([sys.executable, "-c", program, str(BACKEND_DIR)], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
            hashes.add(completed.stdout.strip())
        self.assertEqual(len(hashes), 1, hashes)
        self.assertRegex(hashes.pop(), r"^[0-9a-f]{64}$")

    def test_extraction_then_canonicalization_is_stable(self):
        outcomes = []
        for _ in range(3):
            extraction = DeterministicTestExtractor().extract(VALID_TEXT)
            outcomes.append(canon(extraction.candidate, source_text=VALID_TEXT, ambiguities=extraction.ambiguities))
        self.assertEqual({o.content_hash for o in outcomes}, {outcomes[0].content_hash})
        self.assertEqual({o.canonical_bytes for o in outcomes}, {outcomes[0].canonical_bytes})


class DecimalStringTests(unittest.TestCase):
    TABLE = [
        ("1", "1"), ("1.0", "1"), ("1.00", "1"), ("1E+0", "1"), ("10E-1", "1"), ("0.1E+1", "1"),
        ("2.50", "2.5"), ("2.5", "2.5"), ("25E-1", "2.5"), ("0.25E+1", "2.5"),
        ("100", "100"), ("1E+2", "100"), ("100.0", "100"), ("1.00E+2", "100"), ("10E+1", "100"),
        ("1E+3", "1000"), ("0.001", "0.001"), ("0.0010", "0.001"), ("1E-3", "0.001"), ("10E-4", "0.001"),
        ("123.45", "123.45"), ("123.4500", "123.45"), ("12345E-2", "123.45"), ("0.5", "0.5"), ("0.50", "0.5"),
        ("007", "7"), ("1E-5", "0.00001"), ("0", "0"), ("0.000", "0"), ("0E+5", "0"), ("-0", "0"), ("-0.00", "0"),
        ("-2.5", "-2.5"), ("-2.50", "-2.5"), ("-100", "-100"), ("-1E+3", "-1000"), ("-0.001", "-0.001"),
        ("5", "5"), ("10", "10"), ("1000000", "1000000"), ("12.0", "12"), ("1.1", "1.1"), ("99999999999999999999", "99999999999999999999"),
    ]

    def test_the_documented_table(self):
        for text, expected in self.TABLE:
            with self.subTest(value=text):
                self.assertEqual(canonical_decimal_string(D(text)), expected)

    def test_numerically_equal_values_give_the_same_string(self):
        for group in (
            ["1", "1.0", "1.00", "1.000000", "1E+0", "10E-1", "0.1E+1", "0.010E+2"],
            ["2.5", "2.50", "2.500", "25E-1", "250E-2"],
            ["0.001", "0.0010", "1E-3", "10E-4", "1000E-6"],
            ["100", "1E+2", "100.0", "10E+1", "1.00E+2", "0.1E+3"],
        ):
            with self.subTest(group=group):
                self.assertEqual({canonical_decimal_string(D(t)) for t in group}, {canonical_decimal_string(D(group[0]))})
                for a in group:
                    self.assertEqual(D(a), D(group[0]))  # they really are numerically equal

    def test_different_values_give_different_strings(self):
        values = ["1", "1.01", "1.001", "10", "0.1", "0.01", "100", "1.1", "-1", "11", "101", "0"]
        strings = [canonical_decimal_string(D(v)) for v in values]
        self.assertEqual(len(set(strings)), len(strings))

    def test_plain_notation_never_uses_an_exponent(self):
        for text in ("1E+30", "1E-30", "1.5E+20", "9.99E-15", "123E+10", "1E+1000", "1E-1000"):
            with self.subTest(value=text):
                self.assertNotIn("E", canonical_decimal_string(D(text)).upper())

    def test_no_leading_or_trailing_zero_noise(self):
        for text in ("1.0", "10.50", "0.10", "00012.3400", "1E+2", "0.0100"):
            result = canonical_decimal_string(D(text))
            self.assertFalse(result.startswith("00"), result)
            if "." in result:
                self.assertFalse(result.endswith("0"), result)
                self.assertFalse(result.endswith("."), result)
            self.assertFalse(result.startswith("+"), result)

    def test_large_and_small_finite_values_are_exact(self):
        self.assertEqual(canonical_decimal_string(D("1E+1000")), "1" + "0" * 1000)
        self.assertEqual(canonical_decimal_string(D("1E-1000")), "0." + "0" * 999 + "1")
        self.assertEqual(canonical_decimal_string(D(10**60)), "1" + "0" * 60)
        digits = "9" * 500
        self.assertEqual(canonical_decimal_string(D(digits)), digits)
        mixed = "9" * 100 + "." + "9" * 100
        self.assertEqual(canonical_decimal_string(D(mixed)), mixed)
        fifty = "0." + "1234567890" * 4 + "123456789"  # 49 digits, ends in a nonzero digit
        self.assertEqual(canonical_decimal_string(D(fifty)), fifty)

    def test_trailing_zeros_of_a_huge_coefficient_are_removed_without_rounding(self):
        value = D("1" + "0" * 5000 + "." + "0" * 5000)
        self.assertEqual(canonical_decimal_string(value), "1" + "0" * 5000)
        self.assertEqual(canonical_decimal_string(D("12345.678900000000000000000000000000000000000000")), "12345.6789")

    def test_the_result_does_not_depend_on_the_decimal_context(self):
        values = ["1", "1.00", "2.50", "123.4567890123456789012345678901234567890", "1E+30", "1E-30", "0.001", "-7.0", "9" * 60]
        baseline = {v: canonical_decimal_string(D(v)) for v in values}
        contexts = [
            dict(prec=1, rounding=decimal.ROUND_DOWN),
            dict(prec=2, rounding=decimal.ROUND_UP, Emax=3, Emin=-3),
            dict(prec=3, rounding=decimal.ROUND_HALF_EVEN, Emax=5, Emin=-5),
            dict(prec=100000, rounding=decimal.ROUND_CEILING),
        ]
        for settings in contexts:
            with self.subTest(context=settings):
                with decimal.localcontext() as context:
                    for key, setting in settings.items():
                        setattr(context, key, setting)
                    for value in values:
                        self.assertEqual(canonical_decimal_string(D(value)), baseline[value], value)

    def test_nothing_goes_through_float(self):
        # 0.1 + 0.2 is not 0.3 as a float; as a Decimal it is exactly what was written.
        self.assertEqual(canonical_decimal_string(D("0.3")), "0.3")
        self.assertEqual(canonical_decimal_string(D("0.30000000000000004")), "0.30000000000000004")
        self.assertEqual(canonical_decimal_string(D("1E+400")), "1" + "0" * 400)  # beyond any float
        self.assertEqual(canonical_decimal_string(D("123456789012345678901234567890.123456789012345678901234567890")), "123456789012345678901234567890.12345678901234567890123456789")

    def test_non_finite_values_are_refused(self):
        for text in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=text), self.assertRaises(CanonicalizationError):
                canonical_decimal_string(D(text))

    def test_only_decimals_are_accepted(self):
        for bad in (1, 1.0, "1", None, True):
            with self.subTest(value=repr(bad)), self.assertRaises(TypeError):
                canonical_decimal_string(bad)

    def test_the_input_decimal_is_not_changed(self):
        value = D("2.500")
        canonical_decimal_string(value)
        self.assertEqual(str(value), "2.500")
        self.assertEqual(value.as_tuple(), D("2.500").as_tuple())


class DecimalInDraftsTests(unittest.TestCase):
    def quantity_hash(self, quantity):
        return canon(simple(items=(RequestedItem(description="Pens", quantity=quantity),))).content_hash

    def test_numerically_equal_quantities_give_equal_bytes_and_hash(self):
        variants = [D("1"), D("1.0"), D("1.00"), D("1E+0"), 1, 1.0, D("10E-1")]
        contents = [canon(simple(items=(RequestedItem(description="Pens", quantity=q),))) for q in variants]
        self.assertEqual({c.canonical_bytes for c in contents}, {contents[0].canonical_bytes})
        self.assertEqual({c.content_hash for c in contents}, {contents[0].content_hash})

    def test_numerically_different_quantities_change_the_hash(self):
        hashes = {self.quantity_hash(q) for q in (D("1"), D("1.01"), D("1.001"), D("2"), D("0.1"), D("10"))}
        self.assertEqual(len(hashes), 6)

    def test_a_float_goes_through_its_shortest_digits_not_binary_expansion(self):
        content = canon(simple(items=(RequestedItem(description="Pens", quantity=0.1),)))
        self.assertIn(b'"quantity":"0.1"', content.canonical_bytes)

    def test_large_and_small_quantities_accepted_by_validation_are_serialized_exactly(self):
        for quantity, expected in (
            (D("1E+1000"), "1" + "0" * 1000),
            (D("1E-1000"), "0." + "0" * 999 + "1"),
            (10**60 + 1, str(10**60 + 1)),
            (D("0." + "1234567890" * 4 + "123456789"), "0." + "1234567890" * 4 + "123456789"),
        ):
            with self.subTest(quantity=repr(quantity)[:30]):
                request = simple(items=(RequestedItem(description="Pens", quantity=quantity),))
                self.assertTrue(validate_request(request).is_valid)
                parsed = json.loads(canon(request).canonical_bytes.decode("utf-8"))
                self.assertEqual(parsed["payload"]["items"][0]["quantity"], expected)

    def test_the_active_decimal_context_does_not_change_the_draft_bytes(self):
        request = simple(items=(RequestedItem(description="Pens", quantity=D("123.456789012345678901234567890123")),))
        baseline = canon(request)
        for settings in (dict(prec=1, rounding=decimal.ROUND_DOWN), dict(prec=5, Emax=9, Emin=-9), dict(prec=50, rounding=decimal.ROUND_UP)):
            with self.subTest(context=settings), decimal.localcontext() as context:
                for key, setting in settings.items():
                    setattr(context, key, setting)
                again = canon(request)
                rebuilt = canon(simple(items=(RequestedItem(description="Pens", quantity=D("123.456789012345678901234567890123")),)))
            self.assertEqual(again.canonical_bytes, baseline.canonical_bytes)
            self.assertEqual(rebuilt.content_hash, baseline.content_hash)

    def test_a_configured_maximum_is_compared_exactly_and_does_not_change_the_bytes(self):
        request = simple(items=(RequestedItem(description="Pens", quantity=D("100.000")),))
        with_limit = canon(request, limits=ValidationLimits(max_quantity=100))
        self.assertEqual(with_limit.canonical_bytes, canon(request).canonical_bytes)
        over = simple(items=(RequestedItem(description="Pens", quantity=D("100.0000000000000000000000000001")),))
        with self.assertRaises(DraftNotValidError):
            canon(over, limits=ValidationLimits(max_quantity=100))

    def test_invalid_quantities_are_refused_by_the_existing_rules(self):
        for quantity in (0, -1, D("0.00"), True, False, "5", None, float("nan"), float("inf"), D("NaN"), D("Infinity")):
            with self.subTest(quantity=repr(quantity)), self.assertRaises(DraftNotValidError):
                canon(simple(items=(RequestedItem(description="Pens", quantity=quantity),)))


class PaddingGuardTests(unittest.TestCase):
    """The serialization safety guard: a documented refusal, not a business quantity limit.

    Boundary behavior is tested with a SMALL patched limit so no test builds a huge string;
    the real limit is tested only through refusals, which allocate almost nothing.
    """

    def test_the_real_limit_is_documented_and_unchanged(self):
        self.assertEqual(canonical_module.MAX_PADDING_ZEROS, 999_999)

    def test_the_boundary_for_large_exponents(self):
        with mock.patch.object(canonical_module, "MAX_PADDING_ZEROS", 10):
            self.assertEqual(canonical_decimal_string(D("1E+10")), "1" + "0" * 10)  # exactly at the limit
            self.assertEqual(canonical_decimal_string(D("123E+10")), "123" + "0" * 10)
            for text in ("1E+11", "123E+11", "9E+1000"):
                with self.subTest(value=text), self.assertRaises(SerializationSafetyError):
                    canonical_decimal_string(D(text))

    def test_the_boundary_for_small_values_counts_the_zeros_after_the_point(self):
        with mock.patch.object(canonical_module, "MAX_PADDING_ZEROS", 10):
            self.assertEqual(canonical_decimal_string(D("1E-11")), "0." + "0" * 10 + "1")  # 10 zeros: at the limit
            self.assertEqual(canonical_decimal_string(D("1E-5")), "0.00001")
            self.assertEqual(canonical_decimal_string(D("12345E-15")), "0." + "0" * 10 + "12345")
            for text in ("1E-12", "12345E-16", "1E-1000"):
                with self.subTest(value=text), self.assertRaises(SerializationSafetyError):
                    canonical_decimal_string(D(text))

    def test_the_coefficient_digits_never_count_toward_the_guard(self):
        with mock.patch.object(canonical_module, "MAX_PADDING_ZEROS", 10):
            digits = "7" * 200
            self.assertEqual(canonical_decimal_string(D(digits)), digits)
            self.assertEqual(canonical_decimal_string(D("0." + digits)), "0." + digits)

    def test_the_real_limit_refuses_without_building_anything_large(self):
        for text in ("1E+1000000", "9E+1000000", "1E+999999999", "1E+999999999999999999", "1E-1000001", "1E-999999999999999999"):
            with self.subTest(value=text):
                tracemalloc.start()
                try:
                    with self.assertRaises(SerializationSafetyError):
                        canonical_decimal_string(D(text))
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 64 * 1024, f"refusal allocated {peak} bytes: the guard must run before any large string exists")

    def test_the_refusal_is_a_documented_exception_with_a_stable_reason(self):
        with self.assertRaises(SerializationSafetyError) as caught:
            canonical_decimal_string(D("1E+1000000"))
        error = caught.exception
        self.assertEqual(error.reason, "decimal_expansion")
        self.assertIsNone(error.path)
        self.assertIsInstance(error, CanonicalizationError)
        self.assertIsInstance(error, ValueError)
        self.assertNotIsInstance(error, DraftNotValidError)
        self.assertIn("too large", str(error))
        self.assertNotIn("1000000", str(error))  # the message never echoes the value
        self.assertIn("SerializationSafetyError", canonical_module.__doc__)

    def test_a_refusal_never_rounds_truncates_or_switches_to_exponent_notation(self):
        with mock.patch.object(canonical_module, "MAX_PADDING_ZEROS", 10):
            for text in ("1E+11", "1E-12", "5E+50"):
                with self.subTest(value=text):
                    with self.assertRaises(SerializationSafetyError):
                        canonical_decimal_string(D(text))  # raises; there is no returned fallback string
            self.assertEqual(canonical_decimal_string(D("1E+10")).count("E"), 0)

    def test_in_a_draft_the_error_names_the_quantity_path(self):
        request = simple(items=(RequestedItem(description="a", quantity=1), RequestedItem(description="b", quantity=D("1E+1000000"))))
        self.assertEqual(validate_request(request).findings, ())  # validation is unchanged and accepts it
        with self.assertRaises(SerializationSafetyError) as caught:
            canon(request)
        self.assertEqual((caught.exception.reason, caught.exception.path), ("decimal_expansion", "items[1].quantity"))
        self.assertIn("items[1].quantity", str(caught.exception))
        self.assertNotIsInstance(caught.exception, DraftNotValidError)  # a safety refusal, not a validity failure

    def test_a_huge_coefficient_is_not_limited(self):
        value = D("7" * 20000)
        self.assertEqual(canonical_decimal_string(value), "7" * 20000)

    def test_the_guard_is_not_a_business_limit(self):
        # Large but representable quantities are accepted by validation AND serialized.
        request = simple(items=(RequestedItem(description="Pens", quantity=D("1E+1000")),))
        self.assertTrue(validate_request(request).is_valid)
        self.assertEqual(json.loads(canon(request).canonical_bytes.decode("utf-8"))["payload"]["items"][0]["quantity"], "1" + "0" * 1000)


class TextTests(unittest.TestCase):
    def notes_bytes(self, text):
        content = canon(simple(notes=text))
        marker = b'}],"notes":'  # the items array closes, then the top-level notes
        start = content.canonical_bytes.index(marker) + len(marker)
        end = content.canonical_bytes.index(b',"requester"')
        return content.canonical_bytes[start:end]

    def test_ascii_special_characters_are_escaped_exactly(self):
        text = 'q"b\\s/\n\r\t\x08\x0c\x00\x1f\x7f'
        expected = b'"q\\"b\\\\s/\\n\\r\\t\\b\\f\\u0000\\u001f\x7f"'
        self.assertEqual(self.notes_bytes(text), expected)

    def test_other_control_characters_use_lowercase_four_digit_escapes(self):
        for code in (0x01, 0x0B, 0x0E, 0x1A, 0x1B, 0x1F):
            with self.subTest(code=code):
                expected = b'"\\u%04x"' % code
                self.assertEqual(self.notes_bytes(chr(code)), expected)

    def test_non_ascii_text_is_written_as_raw_utf8_not_escaped(self):
        text = chr(0x6F22) + chr(0xE9) + chr(0x1F600) + chr(0x2028) + chr(0x2029)
        expected = b'"' + b"\xe6\xbc\xa2" + b"\xc3\xa9" + b"\xf0\x9f\x98\x80" + b"\xe2\x80\xa8" + b"\xe2\x80\xa9" + b'"'
        self.assertEqual(self.notes_bytes(text), expected)
        self.assertNotIn(b"\\u6f22", canon(simple(notes=text)).canonical_bytes)

    def test_text_is_preserved_exactly_without_trimming_or_case_change(self):
        for text in ("  padded  ", "MiXeD Case", "\ttabbed", "trailing\n", "a  b   c"):
            with self.subTest(text=repr(text)):
                parsed = json.loads(canon(simple(notes=text)).canonical_bytes.decode("utf-8"))
                self.assertEqual(parsed["payload"]["notes"], text)

    def test_whitespace_differences_change_the_hash(self):
        hashes = {canon(simple(requester=Requester(name=name))).content_hash for name in ("Jane Doe", " Jane Doe", "Jane Doe ", "Jane  Doe", "jane doe")}
        self.assertEqual(len(hashes), 5)

    def test_unicode_is_not_normalized(self):
        composed = "Caf" + E_ACUTE
        decomposed = "Cafe" + COMBINING_ACUTE
        self.assertNotEqual(composed, decomposed)
        a = canon(simple(items=(RequestedItem(description=composed, quantity=1),)))
        b = canon(simple(items=(RequestedItem(description=decomposed, quantity=1),)))
        self.assertNotEqual(a.canonical_bytes, b.canonical_bytes)
        self.assertNotEqual(a.content_hash, b.content_hash)
        self.assertIn(b"Caf\xc3\xa9", a.canonical_bytes)
        self.assertIn(b"Cafe\xcc\x81", b.canonical_bytes)

    def test_case_differences_change_the_hash(self):
        a = canon(simple(items=(RequestedItem(description="pens", quantity=1),)))
        b = canon(simple(items=(RequestedItem(description="Pens", quantity=1),)))
        self.assertNotEqual(a.content_hash, b.content_hash)

    def test_text_that_cannot_be_utf8_is_refused_without_echoing_it(self):
        lone = "bad" + chr(0xD800) + "text"
        for request, path in (
            (simple(notes=lone), "notes"),
            (simple(requester=Requester(name=lone)), "requester.name"),
            (simple(items=(RequestedItem(description=lone, quantity=1),)), "items[0].description"),
            (simple(items=(RequestedItem(description="ok", quantity=1, unit=lone),)), "items[0].unit"),
            (simple(source_refs=(SourceRef("notes", lone, 1),)), "source_refs[0].excerpt"),
        ):
            with self.subTest(path=path), self.assertRaises(SerializationSafetyError) as caught:
                canon(request)
            self.assertEqual((caught.exception.reason, caught.exception.path), ("unencodable_text", path))
            self.assertIn(path, str(caught.exception))
            self.assertNotIn("bad", str(caught.exception))
            self.assertIsInstance(caught.exception, CanonicalizationError)
            self.assertNotIsInstance(caught.exception, DraftNotValidError)  # a safety refusal, not a validity failure

    def test_unpaired_surrogates_are_refused_never_replaced_or_dropped(self):
        for lone in (chr(0xD800), chr(0xDBFF), chr(0xDC00), chr(0xDFFF)):
            with self.subTest(code=hex(ord(lone))), self.assertRaises(SerializationSafetyError):
                canon(simple(notes="a" + lone + "b"))
        # A valid surrogate pair is just one non-BMP character and is written as raw UTF-8.
        pair = (chr(0xD83D) + chr(0xDE00)).encode("utf-16", "surrogatepass").decode("utf-16")
        self.assertEqual(pair, chr(0x1F600))
        self.assertIn(b"\xf0\x9f\x98\x80", canon(simple(notes=pair)).canonical_bytes)

    def test_validation_accepts_such_text_so_the_refusal_is_a_serialization_limit(self):
        self.assertEqual(validate_request(simple(notes="bad" + chr(0xD800))).findings, ())


class NullVersusEmptyTests(unittest.TestCase):
    def test_none_and_empty_string_are_different_for_every_optional_field(self):
        builders = {
            "requester.contact": lambda v: simple(requester=Requester(name="Jane Doe", contact=v)),
            "items[].unit": lambda v: simple(items=(RequestedItem(description="Pens", quantity=1, unit=v),)),
            "items[].notes": lambda v: simple(items=(RequestedItem(description="Pens", quantity=1, notes=v),)),
            "notes": lambda v: simple(notes=v),
        }
        for name, build in builders.items():
            with self.subTest(field=name):
                none, empty = canon(build(None)), canon(build(""))
                self.assertNotEqual(none.canonical_bytes, empty.canonical_bytes)
                self.assertNotEqual(none.content_hash, empty.content_hash)
                self.assertIn(b":null", none.canonical_bytes)
                self.assertIn(b':""', empty.canonical_bytes)

    def test_a_source_ref_line_of_none_differs_from_a_numbered_line(self):
        # (Line 0 can no longer be canonicalized: with the required source text it is outside the text.)
        a = canon(simple(source_refs=(SourceRef("notes", "x", None),)))
        b = canon(simple(source_refs=(SourceRef("notes", "x", 1),)))
        self.assertIn(b'"line":null', a.canonical_bytes)
        self.assertIn(b'"line":1', b.canonical_bytes)
        self.assertNotEqual(a.content_hash, b.content_hash)


class OrderTests(unittest.TestCase):
    def test_item_order_is_preserved_and_matters(self):
        a = RequestedItem(description="Alpha", quantity=1)
        b = RequestedItem(description="Beta", quantity=2)
        ab, ba = canon(simple(items=(a, b))), canon(simple(items=(b, a)))
        self.assertLess(ab.canonical_bytes.index(b"Alpha"), ab.canonical_bytes.index(b"Beta"))
        self.assertLess(ba.canonical_bytes.index(b"Beta"), ba.canonical_bytes.index(b"Alpha"))
        self.assertNotEqual(ab.content_hash, ba.content_hash)

    def test_source_reference_order_is_preserved_and_matters(self):
        r1, r2 = SourceRef("notes", "first", 1), SourceRef("notes", "second", 2)
        one, two = canon(simple(source_refs=(r1, r2))), canon(simple(source_refs=(r2, r1)))
        self.assertLess(one.canonical_bytes.index(b"first"), one.canonical_bytes.index(b"second"))
        self.assertLess(two.canonical_bytes.index(b"second"), two.canonical_bytes.index(b"first"))
        self.assertNotEqual(one.content_hash, two.content_hash)

    def test_equal_items_are_not_merged_and_quantities_are_not_summed(self):
        items = (RequestedItem(description="Pens", quantity=1), RequestedItem(description="pens", quantity=2))
        content = canon(simple(items=items))
        parsed = json.loads(content.canonical_bytes.decode("utf-8"))
        self.assertEqual([i["quantity"] for i in parsed["payload"]["items"]], ["1", "2"])
        self.assertEqual(len(parsed["payload"]["items"]), 2)


class ChangeSensitivityTests(unittest.TestCase):
    def test_changing_any_business_field_changes_bytes_and_hash(self):
        base = golden_request()
        item0, item1 = base.items
        variants = {
            "requester.name": dataclasses.replace(base, requester=Requester(name="Jane Roe", contact=None)),
            "requester.contact": dataclasses.replace(base, requester=Requester(name="Jane Doe", contact="x@example.invalid")),
            "items[0].description": dataclasses.replace(base, items=(dataclasses.replace(item0, description="A4 papers"), item1)),
            "items[0].quantity": dataclasses.replace(base, items=(dataclasses.replace(item0, quantity=D("10.5")), item1)),
            "items[0].unit": dataclasses.replace(base, items=(dataclasses.replace(item0, unit="packs"), item1)),
            "items[0].notes": dataclasses.replace(base, items=(dataclasses.replace(item0, notes=None), item1)),
            "items[1].unit": dataclasses.replace(base, items=(item0, dataclasses.replace(item1, unit="kg"))),
            "an extra item": dataclasses.replace(base, items=base.items + (RequestedItem(description="Z", quantity=1),)),
            "notes": dataclasses.replace(base, notes="Deliver to Room 6"),
            "source_ref field_path": dataclasses.replace(base, source_refs=(SourceRef("requester.contact", "Requester: Jane Doe", 1), base.source_refs[1])),
            "source_ref excerpt": dataclasses.replace(base, source_refs=(SourceRef("requester.name", "Requester: Jane Roe", 1), base.source_refs[1])),
            "source_ref line": dataclasses.replace(base, source_refs=(SourceRef("requester.name", "Requester: Jane Doe", 2), base.source_refs[1])),
            "a removed source ref": dataclasses.replace(base, source_refs=base.source_refs[:1]),
        }
        reference = canon(base)
        seen = {reference.content_hash}
        for name, variant in variants.items():
            with self.subTest(change=name):
                changed = canon(variant)
                self.assertNotEqual(changed.canonical_bytes, reference.canonical_bytes)
                self.assertNotEqual(changed.content_hash, reference.content_hash)
                seen.add(changed.content_hash)
        self.assertEqual(len(seen), len(variants) + 1)  # and every variant differs from every other


class BoundaryOfHashedContentTests(unittest.TestCase):
    """Validation findings, ambiguities, limits, and metadata are not part of the hashed content."""

    def test_warnings_do_not_change_the_bytes_and_remain_available(self):
        clean = simple(items=(RequestedItem(description="Pens", quantity=1), RequestedItem(description="Ink", quantity=2)))
        duplicated = simple(items=(RequestedItem(description="Pens", quantity=1), RequestedItem(description="pens", quantity=1)))
        content = canon(duplicated)
        self.assertEqual([f.code.value for f in content.validation.warnings], ["duplicate_item"])
        self.assertEqual(content.validation, validate_request(duplicated))
        self.assertTrue(content.validation.is_valid)
        self.assertNotIn(b"duplicate", content.canonical_bytes)
        self.assertNotIn(b"warning", content.canonical_bytes)
        self.assertEqual(len(json.loads(content.canonical_bytes.decode("utf-8"))["payload"]["items"]), 2)
        self.assertNotEqual(canon(clean).content_hash, content.content_hash)  # different content, not different warnings

    def test_the_same_content_hashes_the_same_whatever_the_validation_context(self):
        request = simple(items=(RequestedItem(description="Pens", quantity=D("2.5")),), notes="note")
        plain = canon(request)
        contexts = [
            {"limits": ValidationLimits(max_quantity=100, max_text_length=500)},
            {"source_text": "Pens 2.5 note Jane Doe"},
            {"source_text": "something else entirely"},  # only adds value_not_in_source warnings
            {"ambiguities": [Ambiguity("items[0].unit", "?"), Ambiguity("notes", "?")]},  # optional: warnings
        ]
        for kwargs in contexts:
            with self.subTest(context=list(kwargs)):
                content = canon(request, **kwargs)
                self.assertEqual(content.canonical_bytes, plain.canonical_bytes)
                self.assertEqual(content.content_hash, plain.content_hash)
        self.assertTrue(canon(request, source_text="something else entirely").validation.warnings)

    def test_findings_ambiguities_and_metadata_never_appear_in_the_bytes(self):
        extraction = DeterministicTestExtractor().extract(VALID_TEXT)
        content = canon(extraction.candidate, source_text=VALID_TEXT, ambiguities=extraction.ambiguities)
        text = content.canonical_bytes.decode("utf-8")
        for token in ("fixture", "extractor", "severity", "finding", "ambiguit", "blocking", "warning", "timestamp", "created", "author", "version\":\"", "provider"):
            self.assertNotIn(token, text)
        self.assertEqual(set(json.loads(text)), {"canonical_format_version", "payload"})
        self.assertEqual(set(json.loads(text)["payload"]), {"requester", "items", "notes", "source_refs"})

    def test_source_references_are_part_of_the_hashed_content(self):
        with_refs = canon(golden_request())
        without = canon(dataclasses.replace(golden_request(), source_refs=()))
        self.assertNotEqual(with_refs.content_hash, without.content_hash)


class ValidationGateTests(unittest.TestCase):
    def test_blocking_findings_prevent_serialization(self):
        invalid_drafts = {
            "missing requester name": simple(requester=Requester(name=None)),
            "blank requester name": simple(requester=Requester(name="  ")),
            "no items": simple(items=()),
            "missing description": simple(items=(RequestedItem(description=None, quantity=1),)),
            "missing quantity": simple(items=(RequestedItem(description="Pens", quantity=None),)),
            "zero quantity": simple(items=(RequestedItem(description="Pens", quantity=0),)),
            "wrong type": simple(notes=7),
            "bad source ref container": simple(source_refs=("x",)),
        }
        for name, draft in invalid_drafts.items():
            with self.subTest(draft=name):
                with self.assertRaises(DraftNotValidError) as caught:
                    canon(draft)
                self.assertTrue(caught.exception.validation.blocking)
                self.assertFalse(caught.exception.validation.is_valid)
                self.assertIsInstance(caught.exception, CanonicalizationError)

    def test_a_blocking_ambiguity_prevents_serialization_even_if_the_value_is_present(self):
        request = simple(items=(RequestedItem(description="Pens", quantity=5),))
        with self.assertRaises(DraftNotValidError) as caught:
            canon(request, ambiguities=[Ambiguity("items[0].quantity", "maybe five")])
        self.assertEqual([f.code.value for f in caught.exception.validation.blocking], ["ambiguous_required_value"])

    def test_an_invalid_source_reference_prevents_serialization_when_a_source_text_is_given(self):
        request = simple(source_refs=(SourceRef("notes", "wrong", 1),))
        with self.assertRaises(DraftNotValidError) as caught:
            canon(request, source_text="Notes: real text")
        self.assertEqual([f.code.value for f in caught.exception.validation.blocking], ["invalid_source_ref"])

    def test_mistyped_source_reference_fields_are_blocking_findings_never_coerced(self):
        for ref in (SourceRef("notes", "x", "1"), SourceRef("notes", "x", True), SourceRef("notes", "x", 1.0), SourceRef(5, "x", 1), SourceRef("notes", None, 1), SourceRef("notes", b"x", 1)):
            with self.subTest(ref=repr(ref)):
                with self.assertRaises(DraftNotValidError) as caught:
                    canonicalize_request(simple(source_refs=(ref,)), source_text="x\n")
                self.assertEqual([f.code.value for f in caught.exception.validation.blocking], ["invalid_source_ref"])

    def test_the_serializers_own_type_checks_are_defense_in_depth(self):
        # Validation normally rejects these first; the serializer still refuses, never coerces.
        for ref in (SourceRef("notes", "x", "1"), SourceRef("notes", "x", True), SourceRef("notes", "x", 1.0), SourceRef(5, "x", 1), SourceRef("notes", None, 1)):
            with self.subTest(ref=repr(ref)), self.assertRaises(CanonicalizationError):
                canonical_module._source_ref(0, ref)

    def test_the_error_names_codes_and_paths_but_never_echoes_content(self):
        sentinel = "SENTINEL-CONTENT-123"
        request = simple(requester=Requester(name=sentinel), items=(RequestedItem(description=sentinel, quantity=-1),), notes=sentinel)
        with self.assertRaises(DraftNotValidError) as caught:
            canon(request)
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertIn("1 blocking", str(caught.exception))

    def test_a_valid_draft_with_only_warnings_is_serialized(self):
        request = simple(items=(RequestedItem(description="Pens", quantity=1), RequestedItem(description="PENS", quantity=1)))
        content = canon(request, source_text="unrelated", ambiguities=[Ambiguity("notes", "?")])
        self.assertTrue(content.validation.warnings)
        self.assertFalse(content.validation.blocking)

    def test_non_requests_are_a_programming_error(self):
        with self.assertRaises(TypeError):
            canon({"requester": {"name": "x"}})


class SourceReferenceGateTests(unittest.TestCase):
    """Source references are canonical business content, so they must be verified.

    With any source reference, a source text is REQUIRED and every reference is validated
    against it; omitting it is refused, never silently skipped. Without references the source
    text may be omitted. The source text is never part of the bytes.
    """

    TEXT = "Requester: Jane Doe\nNotes: soon\n"

    def draft(self, *refs):
        return StructuredRequest(
            requester=Requester(name="Jane Doe"),
            items=(RequestedItem(description="Pens", quantity=1),),
            notes="soon",
            source_refs=tuple(refs),
        )

    def good_refs(self):
        return (SourceRef("requester.name", "Requester: Jane Doe", 1), SourceRef("notes", "Notes: soon", 2))

    def test_references_without_a_source_text_are_refused_clearly(self):
        draft = self.draft(*self.good_refs())
        for kwargs in ({}, {"source_text": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceTextRequiredError) as caught:
                canonicalize_request(draft, **kwargs)
            self.assertIn("source_text is required", str(caught.exception))
            self.assertIsInstance(caught.exception, CanonicalizationError)
            self.assertNotIsInstance(caught.exception, DraftNotValidError)

    def test_the_refusal_applies_even_to_references_that_look_harmless(self):
        # A single reference, a reference without a line, and a reference that would be wrong
        # are all refused the same way: verification is never silently skipped.
        for refs in ((SourceRef("notes", "Notes: soon", 2),), (SourceRef("notes", "Notes: soon", None),), (SourceRef("price", "nowhere", 999),)):
            with self.subTest(refs=refs), self.assertRaises(SourceTextRequiredError):
                canonicalize_request(self.draft(*refs))

    def test_matching_source_text_allows_canonicalization(self):
        content = canonicalize_request(self.draft(*self.good_refs()), source_text=self.TEXT)
        self.assertTrue(content.validation.is_valid)
        parsed = json.loads(content.canonical_bytes.decode("utf-8"))
        self.assertEqual([r["line"] for r in parsed["payload"]["source_refs"]], [1, 2])

    def test_references_are_validated_against_the_supplied_source(self):
        bad = (
            SourceRef("requester.name", "Requester: Someone Else", 1),  # wrong excerpt
            SourceRef("notes", "Notes: soon", 9),  # line outside the text
            SourceRef("price", "x", 1),  # unknown field
            SourceRef("items[5].quantity", "Requester: Jane Doe", 1),  # no such item
        )
        for ref in bad:
            with self.subTest(ref=ref), self.assertRaises(DraftNotValidError) as caught:
                canonicalize_request(self.draft(ref), source_text=self.TEXT)
            self.assertEqual([f.code.value for f in caught.exception.validation.blocking], ["invalid_source_ref"])

    def test_an_empty_source_text_is_supplied_not_missing(self):
        # "" is a source text (so no SourceTextRequiredError); every reference then fails verification.
        with self.assertRaises(DraftNotValidError) as caught:
            canonicalize_request(self.draft(*self.good_refs()), source_text="")
        self.assertNotIsInstance(caught.exception, SourceTextRequiredError)

    def test_without_references_the_source_text_may_be_omitted(self):
        draft = self.draft()
        content = canonicalize_request(draft)
        self.assertEqual(json.loads(content.canonical_bytes.decode("utf-8"))["payload"]["source_refs"], [])
        self.assertTrue(content.content_hash)

    def test_without_references_a_source_text_may_still_be_supplied_and_changes_nothing(self):
        draft = self.draft()
        plain = canonicalize_request(draft)
        for text in ("", "anything at all", self.TEXT):
            with self.subTest(text=text):
                self.assertEqual(canonicalize_request(draft, source_text=text).canonical_bytes, plain.canonical_bytes)

    def test_other_validation_rules_still_apply_without_references(self):
        for bad in (simple(requester=Requester(name=None)), simple(items=()), simple(items=(RequestedItem(description="x", quantity=0),)), simple(notes=7)):
            with self.subTest(draft=repr(bad)[:40]), self.assertRaises(DraftNotValidError):
                canonicalize_request(bad)

    def test_the_missing_source_text_is_reported_before_other_findings(self):
        # A caller who forgot the argument is told so first, instead of a misleading list of findings.
        invalid_with_refs = StructuredRequest(requester=Requester(name=None), items=(), source_refs=self.good_refs())
        with self.assertRaises(SourceTextRequiredError):
            canonicalize_request(invalid_with_refs)
        with self.assertRaises(DraftNotValidError):  # and once the text is supplied, the real findings appear
            canonicalize_request(invalid_with_refs, source_text=self.TEXT)

    def test_the_source_text_is_never_part_of_the_canonical_content(self):
        text = self.TEXT + "A sentinel sentence that no reference points at.\n"
        content = canonicalize_request(self.draft(*self.good_refs()), source_text=text)
        self.assertNotIn(b"sentinel", content.canonical_bytes)
        self.assertNotIn(text.encode("utf-8"), content.canonical_bytes)
        self.assertEqual(set(json.loads(content.canonical_bytes.decode("utf-8"))["payload"]), {"requester", "items", "notes", "source_refs"})

    def test_the_hash_fingerprints_the_included_content_not_the_document(self):
        # Two different documents that contain the same referenced lines give the same hash:
        # the hash does not identify or authenticate the original document.
        draft = self.draft(*self.good_refs())
        a = canonicalize_request(draft, source_text=self.TEXT)
        b = canonicalize_request(draft, source_text=self.TEXT + "Completely different trailing content.\n")
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertEqual(a.canonical_bytes, b.canonical_bytes)

    def test_invalid_source_text_types_are_a_programming_error(self):
        with self.assertRaises(TypeError):
            canonicalize_request(self.draft(*self.good_refs()), source_text=b"bytes")

    def test_a_non_sequence_reference_container_is_a_validation_failure_not_a_missing_argument(self):
        with self.assertRaises(DraftNotValidError):
            canonicalize_request(simple(source_refs="x"))


class NoConversionTests(unittest.TestCase):
    """Canonicalization converts nothing: a quantity must already be a Decimal."""

    def test_a_float_that_bypassed_construction_is_refused_not_converted(self):
        item = RequestedItem(description="x", quantity=1)
        object.__setattr__(item, "quantity", 0.1)  # bypasses the conversion done at construction
        request = StructuredRequest(requester=Requester(name="J"), items=(item,))
        self.assertTrue(validate_request(request).is_valid)  # validation alone would let it through
        with self.assertRaises(CanonicalizationError) as caught:
            canon(request)
        self.assertIn("items[0].quantity must be a Decimal", str(caught.exception))

    def test_an_int_that_bypassed_construction_is_refused_too(self):
        item = RequestedItem(description="x", quantity=1)
        object.__setattr__(item, "quantity", 5)
        with self.assertRaises(CanonicalizationError):
            canon(StructuredRequest(requester=Requester(name="J"), items=(item,)))

    def test_the_module_never_uses_float_rounding_or_conversion_helpers(self):
        # Checked on real names and attributes (not docstrings): no float(), round(), to_decimal,
        # quantize, normalize, or Decimal construction from a repr.
        import ast

        tree = ast.parse(Path(canonical_module.__file__).read_text(encoding="utf-8"))
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(used & {"float", "round", "to_decimal", "quantize", "normalize", "repr", "create_decimal", "getcontext", "localcontext"}, set())


class NoMutationTests(unittest.TestCase):
    def test_canonicalization_does_not_change_the_draft(self):
        request = golden_request()
        before = copy.deepcopy(request)
        identities = (id(request.requester), id(request.items), id(request.source_refs), [id(i) for i in request.items])
        canon(request, limits=ValidationLimits(max_quantity=100, max_text_length=500))
        self.assertEqual(request, before)
        self.assertEqual(identities, (id(request.requester), id(request.items), id(request.source_refs), [id(i) for i in request.items]))
        self.assertEqual([str(i.quantity) for i in request.items], ["10.0", "2.50"])  # as given; only the canonical form differs

    def test_ambiguities_and_limits_are_not_changed(self):
        ambiguities = [Ambiguity("notes", "?")]
        limits = ValidationLimits(max_quantity=D("100"), max_text_length=50)
        ambiguities_before, limits_before = copy.deepcopy(ambiguities), dataclasses.astuple(limits)
        canon(simple(), limits=limits, ambiguities=ambiguities)
        self.assertEqual(ambiguities, ambiguities_before)
        self.assertEqual(dataclasses.astuple(limits), limits_before)

    def test_the_result_is_immutable_and_holds_no_mutable_containers(self):
        content = canon(golden_request())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            content.content_hash = "0" * 64
        with self.assertRaises(dataclasses.FrozenInstanceError):
            content.canonical_bytes = b"x"
        self.assertIsInstance(content.canonical_bytes, bytes)
        self.assertEqual(find_mutable_state(content), [])

    def test_a_list_given_to_the_draft_cannot_alter_a_later_canonical_form(self):
        items = [RequestedItem(description="Pens", quantity=1)]
        request = StructuredRequest(requester=Requester(name="Jane Doe"), items=items)
        first = canon(request)
        items.append(RequestedItem(description="Ink", quantity=2))
        items[0] = RequestedItem(description="Changed", quantity=9)
        self.assertEqual(canon(request).canonical_bytes, first.canonical_bytes)

    def test_calls_do_not_share_state(self):
        a = canon(golden_request())
        canon(simple(notes="something different"))
        b = canon(golden_request())
        self.assertEqual(a, b)

    def test_the_serializer_keeps_no_module_level_mutable_state(self):
        import workflows.request_intake.canonical as module

        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            self.assertNotIsInstance(value, (list, dict, set, bytearray), name)


if __name__ == "__main__":
    unittest.main()
