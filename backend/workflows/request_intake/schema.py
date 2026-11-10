"""Plain-Python representation of a structured request draft.

Mirrors the result schema in docs/architecture/first-workflow.md, section 4:

    requester.name        required
    requester.contact     optional
    items                 required, at least one
    items[].description   required
    items[].quantity      required, a positive number (canonical type: Decimal)
    items[].unit          optional
    items[].notes         optional
    notes                 optional
    source_refs           optional

There is deliberately no price, currency, stock, availability, or delivery field.

Immutability. Every class is a frozen dataclass and every collection is a tuple. Because
a frozen dataclass alone would still alias a ``list`` a caller passes in, the
constructors COPY lists into tuples, so later changes to the caller's list cannot reach
the draft. There are no shared default instances. A missing value is ``None`` (or an
empty tuple); nothing is ever defaulted or guessed.

Quantities. ``RequestedItem`` converts ``int`` and ``float`` quantities to ``Decimal``
when it is constructed (see ``quantity.py``), so the canonical type is the only type that
flows through the domain. Values that cannot be converted (``bool``, text, NaN, infinity)
are left as given so validation can report them.

Dataclasses do not enforce types at runtime, so validation re-checks them
(see ``validation.py``).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from .quantity import is_finite, is_number, to_decimal

# Accepted as input; converted to Decimal on construction.
Quantity = int | float | Decimal
CanonicalQuantity = Decimal

# Source-reference convention (fixture format): an excerpt is the cited line with
# surrounding whitespace removed, cut to this many characters, ending in "..." if cut.
SOURCE_REF_EXCERPT_MAX = 200


def make_excerpt(line: str) -> str:
    """The excerpt convention for ``SourceRef`` (see ``SourceRef``)."""
    stripped = line.strip()
    if len(stripped) <= SOURCE_REF_EXCERPT_MAX:
        return stripped
    return stripped[: SOURCE_REF_EXCERPT_MAX - 3] + "..."


@dataclass(frozen=True)
class Requester:
    name: str | None = None
    contact: str | None = None


@dataclass(frozen=True)
class RequestedItem:
    description: str | None = None
    quantity: Quantity | None = None
    unit: str | None = None
    notes: str | None = None

    def __post_init__(self):
        quantity = self.quantity
        if is_number(quantity) and is_finite(quantity) and not isinstance(quantity, Decimal):
            object.__setattr__(self, "quantity", to_decimal(quantity))


@dataclass(frozen=True)
class SourceRef:
    """Where in the source text a value came from.

    Coordinate convention (the fixture format; nothing more general is claimed):

    * ``line`` is **1-based** and counts lines exactly as Python's ``str.splitlines()``
      does on the exact text given to the extractor. That splits on ``\\n``, ``\\r\\n``,
      ``\\r`` and also on ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\x85``, ``\\u2028`` and
      ``\\u2029``; a trailing line break does not start a new line.
    * ``excerpt`` is ``make_excerpt(<that line>)``: the line stripped of surrounding
      whitespace and cut to ``SOURCE_REF_EXCERPT_MAX`` characters with a trailing "...".
    * There are no character columns or offsets.
    * ``line`` may be ``None``; the reference is then only checked for appearing verbatim
      somewhere in the source text.
    """

    field_path: str
    excerpt: str
    line: int | None = None


@dataclass(frozen=True)
class Ambiguity:
    """A value the source mentions but that could not be read unambiguously.

    The affected field is stored as ``None`` and this records the source excerpt
    (first-workflow.md, section 5). Validation reports it as blocking when the field is
    required and as a warning when it is optional.
    """

    field_path: str
    excerpt: str


@dataclass(frozen=True)
class StructuredRequest:
    requester: Requester = field(default_factory=Requester)
    items: tuple[RequestedItem, ...] = ()
    notes: str | None = None
    source_refs: tuple[SourceRef, ...] = ()

    def __post_init__(self):
        if isinstance(self.items, list):
            object.__setattr__(self, "items", tuple(self.items))
        if isinstance(self.source_refs, list):
            object.__setattr__(self, "source_refs", tuple(self.source_refs))
