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
