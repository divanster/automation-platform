"""Canonical quantity: ``Decimal``, converted without rounding.

Decisions (docs/development/request-intake-slice.md):

* The canonical quantity type is ``decimal.Decimal``.
* Fractions are allowed in this generic schema. Unit-specific integer restrictions
  (for example "whole boxes only") are deferred.
* Booleans, NaN, infinity, zero, and negative values are not valid quantities
  (zero and negative values are rejected by validation; the others cannot be converted).
* Nothing is rounded, quantized, normalized, or limited in precision. A ``Decimal``
  keeps exactly the digits and exponent it was given (``Decimal("2.50")`` stays
  ``2.50``), and an ``int`` is converted exactly at any size. Only construction,
  comparison and formatting are used, none of which depends on the ambient decimal
  context's precision; no arithmetic is performed anywhere in this package.

Conversion behavior of ``to_decimal``:

=============  =====================================================================
Input          Result
=============  =====================================================================
``Decimal``    returned unchanged (finite values only)
``int``        exact ``Decimal`` of the same value (``bool`` is not accepted)
``float``      ``Decimal(repr(value))``: the shortest decimal string that converts
               back to the same float, e.g. ``0.1`` -> ``Decimal("0.1")``
anything else  ``TypeError`` (text such as ``"5"`` is NOT converted here; reading
               numbers from text is an extractor's job)
non-finite     ``ValueError`` (NaN, infinity)
=============  =====================================================================

A ``float`` is a binary approximation, so converting it needs a convention. The
shortest-round-trip convention recovers the digits a person almost certainly meant
(``0.1`` not ``0.1000000000000000055511151231257827...``), never changes which float
the result converts back to, and avoids exporting spurious digits. Callers who need
exact digits should supply a ``Decimal`` or an ``int``. Whether ``2.5`` and ``2.50``
are one canonical value for hashing is an open question; both are kept as given.
"""

import math
from decimal import Decimal

NUMBER_TYPES = (int, float, Decimal)

# SERIALIZATION SAFETY GUARD, NOT A BUSINESS LIMIT. A quantity whose plain decimal form would need more than this
# many padding zeros (from a very large exponent, for example 1E+1000000, or a very small one, for example
# 1E-1000000) must never be written out in plain notation: doing so allocates a string as long as the exponent, and
# an exponent can be about 10**9 in either direction (``Decimal("1e-999999999")`` is a valid ``Decimal``). The value
# mirrors the default decimal context's exponent range (Emax 999999). It does not limit the number of digits of the
# coefficient and is not a rule about how large or small a quantity may be.
#
# ONE RULE, TWO CALLERS. ``canonical.canonical_decimal_string`` refuses such a value (``SerializationSafetyError``).
# ``validation`` never formats it and treats it as a value that cannot be located in the source text (a warning).
# Both decide with ``plain_padding_zeros``, which reads only the Decimal's digits and exponent.
MAX_PADDING_ZEROS = 999_999


def is_number(value) -> bool:
    """True for int, float, and Decimal, but never for bool."""
    return isinstance(value, NUMBER_TYPES) and not isinstance(value, bool)


def is_finite(value) -> bool:
    """True unless ``value`` is a float or Decimal that is NaN or infinite."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return True


def plain_padding_zeros(value: Decimal) -> int:
    """The padding zeros the plain-notation form of the finite ``value`` needs, WITHOUT building any string.

    Computed from ``as_tuple()`` alone (digits and exponent), so it costs time proportional to the number of
    digits and allocates nothing proportional to the exponent, and it does not depend on the decimal context.
    Trailing zeros of the coefficient are counted as part of the exponent first (they do not change the value),
    exactly as ``canonical_decimal_string`` writes the number. Zero needs none. ``Decimal("1E+5")`` needs 5,
    ``Decimal("1E-5")`` needs 4 (``0.00001``), ``Decimal("1e-999999999")`` needs 999999998.
    """
    if not isinstance(value, Decimal):
        raise TypeError("plain_padding_zeros requires a Decimal")
    if not value.is_finite():
        raise ValueError("a non-finite Decimal has no plain form")
    _, digits, exponent = value.as_tuple()
    if not any(digits):
        return 0
    significant = len(digits)
    while digits[significant - 1] == 0:  # trailing zeros of the coefficient: value-preserving
        significant -= 1
        exponent += 1
    if exponent >= 0:
        return exponent
    return max(0, -exponent - significant)


def plain_notation_length(value: Decimal) -> int:
    """How many characters ``format(value, "f")`` produces for the finite ``value``, WITHOUT building the string.

    Computed from the digit count and exponent only. Used to decide that a number cannot occur in a text shorter
    than it, and by the REST API's transport bound.
    """
    if not isinstance(value, Decimal):
        raise TypeError("plain_notation_length requires a Decimal")
    if not value.is_finite():
        raise ValueError("a non-finite Decimal has no plain form")
    sign, digits, exponent = value.as_tuple()
    count = len(digits)
    negative = 1 if sign else 0
    if exponent >= 0:
        if not any(digits):
            return 1 + negative  # zero with a positive exponent prints as "0": the exponent adds no characters
        return count + exponent + negative
    return max(count, -exponent + 1) + 1 + negative  # a leading "0", the point, and any padding zeros


def to_decimal(value) -> Decimal:
    """Convert a number to the canonical ``Decimal`` quantity. See the module docstring."""
    if not is_number(value):
        raise TypeError("a quantity must be an int, float, or Decimal (not a bool or text)")
    if not is_finite(value):
        raise ValueError("a quantity must be finite (not NaN or infinity)")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(float(value)))
    return Decimal(int(value))
