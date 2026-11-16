"""Pure-domain regression tests: a Decimal with an enormous exponent must never cause an enormous allocation.

THE DEFECT THEY GUARD. ``validate_request`` (given a source text) looked each positive quantity up in the text as a
plain-notation number and built it with ``format(value, "f")``. A ``Decimal`` can carry an exponent of about 10**9
in either direction (``Decimal("1e-999999999")`` is valid), so that call tried to build a string of about a
gigabyte, and canonicalization's ``MAX_PADDING_ZEROS`` guard only ran afterwards. The rule now lives in
``workflows.request_intake.quantity`` and both layers use it: validation never formats such a value (it is "not
found in the source", a warning) and canonicalization refuses it (``SerializationSafetyError``).

BOUNDED BY DESIGN. No test builds a string longer than a few kilobytes: the enormous values are exercised through
the arithmetic helpers and through ``validate_request`` (which must now NOT allocate), the safety boundary is
exercised by patching the constant down to a few hundred, and memory is measured with ``tracemalloc``.
Standard library only (no Django), so it runs with ``python -m unittest`` and through ``manage.py test``.
"""

import decimal
import time
import tracemalloc
import unittest
from decimal import Decimal
from unittest import mock

from tests.intake_support import make_request
from workflows.request_intake import (
    RequestedItem,
    Requester,
    SerializationSafetyError,
    StructuredRequest,
    ValidationLimits,
    canonical_decimal_string,
    canonicalize_request,
    validate_request,
)
from workflows.request_intake import canonical, quantity, validation

HUGE = ("1e-999999999", "1E+999999999", "1E+1000000", "1E-1000001", "9.99E+999999990", "123456789E-999999000")
SOURCE = "Requester: Jane Doe\nItem: 10 | boxes | A4 paper\n"
PATH = "items[0].quantity"


def request_with(value):
    return StructuredRequest(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="A4 paper", quantity=value),))


def quantity_findings(result):
    return [(f.code.value, f.field_path) for f in result.findings if f.field_path == PATH]


def independent_padding(value):
    """The padding zeros counted from the canonical TEXT (only usable for values with a small exponent)."""
    text = canonical_decimal_string(value).lstrip("-")
    if "." not in text:
        return len(text) - len(text.rstrip("0")) if text != "0" else 0
    whole, fraction = text.split(".")
    return len(fraction) - len(fraction.lstrip("0")) if whole == "0" else 0


GRID = [
    Decimal(f"{sign}{digits}E{exponent:+d}")
    for sign in ("", "-")
    for digits in ("0", "1", "5", "10", "100", "12345", "120", "1050", "999", "100000")
    for exponent in range(-25, 26)
]


class HelperArithmeticTests(unittest.TestCase):
    def test_padding_matches_the_text_the_canonical_form_actually_writes(self):
        for value in GRID:
            with self.subTest(value=str(value)):
                self.assertEqual(quantity.plain_padding_zeros(value), independent_padding(value))

    def test_plain_length_matches_the_real_formatting(self):
        for value in GRID:
            with self.subTest(value=str(value)):
                self.assertEqual(quantity.plain_notation_length(value), len(format(value, "f")))

    def test_named_examples(self):
        expected = {"1E+5": 5, "1E-5": 4, "0.00001": 4, "100": 2, "1E+0": 0, "123.456": 0, "0": 0, "-0": 0, "0.000": 0, "1.10": 0, "5E+3": 3, "1e-999999999": 999999998, "1E+999999999": 999999999}
        for text, padding in expected.items():
            with self.subTest(value=text):
                self.assertEqual(quantity.plain_padding_zeros(Decimal(text)), padding)

    def test_the_helpers_build_no_large_string_and_are_fast_for_huge_exponents(self):
        started = time.monotonic()
        tracemalloc.start()
        try:
            for text in HUGE:
                quantity.plain_padding_zeros(Decimal(text))
                quantity.plain_notation_length(Decimal(text))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 200_000)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_inputs_must_be_finite_decimals(self):
        for bad in (5, 1.5, "1", None, True):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                quantity.plain_padding_zeros(bad)
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                quantity.plain_notation_length(bad)
        for text in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=text), self.assertRaises(ValueError):
                quantity.plain_padding_zeros(Decimal(text))
            with self.subTest(value=text), self.assertRaises(ValueError):
                quantity.plain_notation_length(Decimal(text))

    def test_the_bound_is_the_existing_canonicalization_boundary_and_is_shared(self):
        self.assertEqual(quantity.MAX_PADDING_ZEROS, 999_999)
        self.assertIs(canonical.MAX_PADDING_ZEROS, quantity.MAX_PADDING_ZEROS)
        self.assertIs(validation.MAX_PADDING_ZEROS, quantity.MAX_PADDING_ZEROS)


class ValidationDoesNotAllocateTests(unittest.TestCase):
    def test_a_huge_exponent_is_a_stable_warning_not_a_hang_and_never_formatted(self):
        for text in HUGE:
            value = Decimal(text)
            with self.subTest(value=text), mock.patch.object(validation, "format", create=True, side_effect=AssertionError("format() must not be called")) as fmt:
                result = validate_request(request_with(value), source_text=SOURCE)
                self.assertEqual(quantity_findings(result), [("value_not_in_source", PATH)])
                self.assertTrue(result.is_valid)  # a warning, not a business refusal
                fmt.assert_not_called()

    def test_peak_memory_and_time_stay_small(self):
        started = time.monotonic()
        tracemalloc.start()
        try:
            for text in HUGE:
                validate_request(request_with(Decimal(text)), source_text=SOURCE)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2_000_000)  # the old code needed about a gigabyte for one of these
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_findings_are_stable_across_calls_and_identical_for_equal_inputs(self):
        first = validate_request(request_with(Decimal("1e-999999999")), source_text=SOURCE)
        second = validate_request(request_with(Decimal("1e-999999999")), source_text=SOURCE)
        self.assertEqual(first, second)

    def test_without_a_source_text_nothing_is_formatted_at_all(self):
        result = validate_request(request_with(Decimal("1e-999999999")))
        self.assertEqual(quantity_findings(result), [])

    def test_other_business_findings_still_come_first_and_unchanged(self):
        value = Decimal("1e-999999999")
        result = validate_request(request_with(value), source_text=SOURCE, limits=ValidationLimits(max_quantity=5))
        self.assertEqual([f.code.value for f in result.findings if f.field_path == PATH], ["value_not_in_source"])
        big = validate_request(request_with(Decimal("1E+999999999")), source_text=SOURCE, limits=ValidationLimits(max_quantity=5))
        self.assertEqual([f.code.value for f in big.findings if f.field_path == PATH], ["exceeds_maximum", "value_not_in_source"])

    def test_the_inputs_are_not_modified(self):
        value = Decimal("1E+999999999")
        draft = request_with(value)
        before = repr(draft)
        source = SOURCE
        validate_request(draft, source_text=source)
        self.assertEqual(repr(draft), before)
        self.assertEqual(draft.items[0].quantity, value)
        self.assertEqual(source, SOURCE)
        self.assertEqual(value.as_tuple(), Decimal("1E+999999999").as_tuple())

    def test_a_value_longer_than_the_text_is_not_formatted_even_within_the_bound(self):
        # 1E+30 is far inside the bound, but its 31-character plain form cannot occur in a 10-character text.
        with mock.patch.object(validation, "format", create=True, side_effect=AssertionError("format() must not be called")):
            result = validate_request(request_with(Decimal("1E+30")), source_text="need 5 pen")
        self.assertEqual(quantity_findings(result), [("value_not_in_source", PATH)])

    def test_the_safety_boundary_applies_exactly_at_the_constant(self):
        # Controlled boundary: the constant is lowered to 100 so no test string exceeds a few hundred characters.
        text = "x" * 400 + " 1" + "0" * 100 + " x"
        with mock.patch.object(validation, "MAX_PADDING_ZEROS", 100):
            at_bound = validate_request(request_with(Decimal("1E+100")), source_text=text)
            over_bound = validate_request(request_with(Decimal("1E+101")), source_text=text + "0")
        self.assertEqual(quantity_findings(at_bound), [])  # padding 100 is allowed, formatted, and found
        self.assertEqual(quantity_findings(over_bound), [("value_not_in_source", PATH)])  # padding 101: never formatted, though the text has it

    def test_the_negative_exponent_boundary(self):
        text = "x" * 300 + " 0." + "0" * 99 + "1 x"  # 1E-100: padding 99
        with mock.patch.object(validation, "MAX_PADDING_ZEROS", 99):
            self.assertEqual(quantity_findings(validate_request(request_with(Decimal("1E-100")), source_text=text)), [])
        with mock.patch.object(validation, "MAX_PADDING_ZEROS", 98):
            self.assertEqual(quantity_findings(validate_request(request_with(Decimal("1E-100")), source_text=text)), [("value_not_in_source", PATH)])


class OrdinaryQuantitiesAreUnchangedTests(unittest.TestCase):
    def found(self, value, text):
        return quantity_findings(validate_request(request_with(value), source_text=text)) == []

    def test_ordinary_values_are_found_exactly_as_before(self):
        cases = [
            (Decimal("10"), "Item: 10 | boxes", True),
            (Decimal("2.50"), "price 2.50 each", True),
            (Decimal("2.5"), "price 2.50 each", False),  # written exactly as the canonical Decimal prints
            (Decimal("0.000001"), "need 0.000001 grams", True),
            (Decimal("1E+20"), "need 100000000000000000000 units", True),
            (Decimal("1E+3"), "need 1000 units", True),
            (5, "need 5 pens", True),
            (5, "need 15 pens", False),
            (Decimal("5"), "need 5.5 pens", False),
            (0.25, "need 0.25 l", True),
            (Decimal("12345678901234567890.123456789"), "x 12345678901234567890.123456789 y", True),
        ]
        for value, text, expected in cases:
            with self.subTest(value=str(value), text=text):
                self.assertEqual(self.found(value, text), expected)

    def test_floats_across_their_whole_range_are_handled_without_allocation(self):
        for value in (1e300, 1e-300, 5e-324, 1.7976931348623157e308, 2.5, 1e22):
            draft = request_with(value)
            tracemalloc.start()
            try:
                result = validate_request(draft, source_text=SOURCE)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            with self.subTest(value=repr(value)):
                self.assertLess(peak, 500_000)
                self.assertTrue(result.is_valid)
                canonicalize_request(draft, source_text=SOURCE)  # and it can still be canonicalized

    def test_huge_integers_are_not_an_exponent_problem(self):
        value = 10**4000
        result = validate_request(request_with(value), source_text=SOURCE)
        self.assertTrue(result.is_valid)
        self.assertEqual(quantity.plain_padding_zeros(Decimal(value)), 4000)


class ContextIndependenceTests(unittest.TestCase):
    def test_results_do_not_depend_on_the_ambient_decimal_context(self):
        def observe():
            observed = []
            for text in HUGE + ("1E+30", "2.50", "0.000001"):
                value = Decimal(text)
                observed.append((quantity.plain_padding_zeros(value), quantity.plain_notation_length(value), quantity_findings(validate_request(request_with(value), source_text=SOURCE))))
            return observed

        default = observe()
        for prec, emax, emin, rounding in ((1, 5, -5, decimal.ROUND_DOWN), (3, 1, -1, decimal.ROUND_UP), (999999999, 999999999, -999999999, decimal.ROUND_HALF_EVEN)):
            with self.subTest(prec=prec, emax=emax), decimal.localcontext() as context:
                context.prec, context.Emax, context.Emin, context.rounding = prec, emax, emin, rounding
                context.traps[decimal.Inexact] = context.traps[decimal.Rounded] = context.traps[decimal.Overflow] = context.traps[decimal.Underflow] = True
                self.assertEqual(observe(), default)


class CanonicalizationIsUnchangedTests(unittest.TestCase):
    def test_the_canonical_forms_are_exactly_what_they_were(self):
        expected = {
            "1": "1", "1.0": "1", "1.00": "1", "1E+0": "1", "2.50": "2.5", "1E+3": "1000", "1.5E+3": "1500", "0": "0", "-0": "0",
            "0.5": "0.5", "1E-5": "0.00001", "-1.25": "-1.25", "123.4500": "123.45", "100": "100", "12E-3": "0.012",
        }  # fmt: skip
        for text, want in expected.items():
            with self.subTest(value=text):
                self.assertEqual(canonical_decimal_string(Decimal(text)), want)

    def test_values_beyond_the_bound_are_refused_before_any_string_is_built(self):
        tracemalloc.start()
        try:
            for text in HUGE:
                with self.assertRaises(SerializationSafetyError) as caught:
                    canonical_decimal_string(Decimal(text))
                self.assertEqual(caught.exception.reason, "decimal_expansion")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 200_000)

    def test_canonicalize_request_refuses_with_the_stable_reason_and_path_after_validation_passes(self):
        for text in HUGE:
            with self.subTest(value=text), self.assertRaises(SerializationSafetyError) as caught:
                canonicalize_request(request_with(Decimal(text)), source_text=SOURCE)
            self.assertEqual((caught.exception.reason, caught.exception.path), ("decimal_expansion", PATH))
            self.assertNotIn(text, str(caught.exception))  # the message never echoes the value

    def test_the_guard_is_the_same_rule_as_before_the_refactoring(self):
        # The old inline computation, reproduced here as an independent oracle on a grid inside the bound.
        def old_padding(value):
            _, digits, exponent = value.as_tuple()
            if not any(digits):
                return 0
            coefficient = list(digits)
            while coefficient[-1] == 0:
                coefficient.pop()
                exponent += 1
            return exponent if exponent >= 0 else max(0, -exponent - len(coefficient))

        for value in GRID + [Decimal(t) for t in HUGE]:
            with self.subTest(value=str(value)):
                self.assertEqual(quantity.plain_padding_zeros(value), old_padding(value))

    def test_the_documented_distinction_between_validation_and_serialization_safety_holds(self):
        draft = make_request(items=(RequestedItem(description="A4 paper", quantity=Decimal("1E+1000000")),), source_refs=())
        result = validate_request(draft, source_text=SOURCE)
        self.assertTrue(result.is_valid)  # business validation does not refuse it ...
        with self.assertRaises(SerializationSafetyError):  # ... the serialization layer does
            canonicalize_request(draft, source_text=SOURCE)


if __name__ == "__main__":
    unittest.main()
