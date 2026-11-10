"""Pure-domain tests (standard-library unittest, no Django) for the canonical quantity.

Decisions under test: Decimal is the canonical type; fractions are allowed; booleans, NaN,
infinity, zero, and negative values are rejected; nothing is silently rounded; no new
precision limit is introduced; the conversion rules are documented in ``quantity.py``.
"""

import decimal
import unittest
from decimal import Decimal

from tests.intake_support import make_request, summary
from workflows.request_intake import RequestedItem, Requester, StructuredRequest, ValidationLimits, to_decimal, validate_request


class ConversionTests(unittest.TestCase):
    def test_decimal_is_returned_unchanged(self):
        for value in (Decimal("2.50"), Decimal("1E+3"), Decimal("0.0010"), Decimal("-7"), Decimal("100")):
            with self.subTest(value=value):
                converted = to_decimal(value)
                self.assertIs(converted, value)
                self.assertEqual(str(converted), str(value))  # digits and exponent preserved

    def test_int_is_converted_exactly_at_any_size(self):
        self.assertEqual(to_decimal(10), Decimal("10"))
        huge = 10**60 + 7
        self.assertEqual(to_decimal(huge), Decimal(huge))
        self.assertEqual(str(to_decimal(huge)), str(huge))

    def test_float_uses_the_shortest_round_trip_digits(self):
        self.assertEqual(to_decimal(0.1), Decimal("0.1"))
        self.assertEqual(str(to_decimal(2.5)), "2.5")
        self.assertEqual(to_decimal(0.1 + 0.2), Decimal("0.30000000000000004"))
        self.assertEqual(to_decimal(1e-05), Decimal("0.00001"))
        self.assertEqual(to_decimal(123456789.125), Decimal("123456789.125"))
        # Not the exact binary expansion:
        self.assertNotEqual(to_decimal(0.1), Decimal(0.1))

    def test_float_conversion_never_changes_which_float_it_is(self):
        for value in (0.1, 0.2, 0.3, 1.1, 2.675, 1e-300, 1e300, 5e-324, 1.7976931348623157e308, 123456.789, 0.30000000000000004):
            with self.subTest(value=value):
                self.assertEqual(float(to_decimal(value)), value)

    def test_booleans_are_not_quantities(self):
        for value in (True, False):
            with self.subTest(value=value), self.assertRaises(TypeError):
                to_decimal(value)

    def test_text_and_other_types_are_not_converted(self):
        for value in ("5", "2.5", b"5", None, [1], (1,), {"n": 1}, object(), 1 + 2j):
            with self.subTest(value=repr(value)), self.assertRaises(TypeError):
                to_decimal(value)

    def test_non_finite_values_cannot_be_converted(self):
        for value in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                to_decimal(value)

    def test_conversion_is_deterministic(self):
        self.assertEqual([to_decimal(2.5) for _ in range(3)], [Decimal("2.5")] * 3)


class NoSilentRoundingTests(unittest.TestCase):
    LONG = "0.1234567890123456789012345678901234567890123456789"  # 49 significant digits, beyond the default context

    def test_long_decimals_are_kept_digit_for_digit(self):
        item = RequestedItem(description="x", quantity=Decimal(self.LONG))
        self.assertEqual(str(item.quantity), self.LONG)

    def test_precision_is_not_limited(self):
        for text in ("1" * 500, "0." + "0" * 400 + "1", "9" * 100 + "." + "9" * 100, "1E+1000", "1E-1000"):
            with self.subTest(length=len(text)):
                value = Decimal(text)
                self.assertIs(to_decimal(value), value)
                request = make_request(items=(RequestedItem(description="x", quantity=value),))
                self.assertEqual(validate_request(request).findings, ())

    def test_results_do_not_depend_on_the_ambient_decimal_context(self):
        long_value = Decimal(self.LONG)
        with decimal.localcontext() as context:
            context.prec = 3
            self.assertEqual(str(to_decimal(long_value)), self.LONG)
            self.assertEqual(to_decimal(1234567), Decimal(1234567))
            self.assertEqual(str(to_decimal(10**40 + 1)), str(10**40 + 1))
            request = make_request(items=(RequestedItem(description="x", quantity=long_value),))
            limits = ValidationLimits(max_quantity=Decimal("0.1234567890123456789012345678901234567890123456788"))
            # Exactly one unit in the last place above the maximum is still reported.
            self.assertEqual(summary(validate_request(request, limits=limits)), [("exceeds_maximum", "items[0].quantity")])

    def test_neighbouring_values_are_distinguished_exactly(self):
        limits = ValidationLimits(max_quantity=Decimal("5"))
        at = make_request(items=(RequestedItem(description="x", quantity=Decimal("5.0000000000000000000000000000")),))
        over = make_request(items=(RequestedItem(description="x", quantity=Decimal("5.0000000000000000000000000001")),))
        self.assertEqual(validate_request(at, limits=limits).findings, ())
        self.assertEqual(summary(validate_request(over, limits=limits)), [("exceeds_maximum", "items[0].quantity")])


class CanonicalTypeTests(unittest.TestCase):
    """RequestedItem converts numbers to Decimal on construction; validation uses Decimal."""

    def test_numbers_become_decimal_on_construction(self):
        self.assertEqual(RequestedItem(quantity=10).quantity, Decimal("10"))
        self.assertIsInstance(RequestedItem(quantity=10).quantity, Decimal)
        self.assertIsInstance(RequestedItem(quantity=2.5).quantity, Decimal)
        self.assertEqual(str(RequestedItem(quantity=2.5).quantity), "2.5")
        self.assertEqual(str(RequestedItem(quantity=Decimal("2.50")).quantity), "2.50")

    def test_values_that_cannot_be_converted_are_left_for_validation_to_report(self):
        for value in (True, "5", float("nan"), float("inf"), Decimal("NaN"), [1]):
            with self.subTest(value=repr(value)):
                kept = RequestedItem(quantity=value).quantity
                self.assertTrue(kept is value or repr(kept) == repr(value))

    def test_equal_numbers_compare_equal_across_input_types(self):
        self.assertEqual(RequestedItem(quantity=10), RequestedItem(quantity=Decimal("10")))
        self.assertEqual(RequestedItem(quantity=2.5), RequestedItem(quantity=Decimal("2.5")))

    def test_no_float_remains_in_a_constructed_item(self):
        self.assertNotIsInstance(RequestedItem(quantity=0.1).quantity, float)

    def test_the_configured_maximum_is_stored_as_decimal(self):
        self.assertIsInstance(ValidationLimits(max_quantity=100).max_quantity, Decimal)
        self.assertIsInstance(ValidationLimits(max_quantity=2.5).max_quantity, Decimal)
        self.assertEqual(ValidationLimits(max_quantity=2.5).max_quantity, Decimal("2.5"))

    def test_validation_does_not_change_the_stored_quantity(self):
        request = StructuredRequest(requester=Requester(name="J"), items=(RequestedItem(description="x", quantity=Decimal("2.50")),))
        validate_request(request)
        self.assertEqual(str(request.items[0].quantity), "2.50")


class DeferredRestrictionTests(unittest.TestCase):
    def test_unit_specific_integer_restrictions_are_not_applied(self):
        # Deferred decision: even a "countable" unit accepts a fraction in this generic schema.
        request = make_request(items=(RequestedItem(description="Staplers", quantity=Decimal("1.5"), unit="pieces"),))
        self.assertEqual(validate_request(request).findings, ())


if __name__ == "__main__":
    unittest.main()
