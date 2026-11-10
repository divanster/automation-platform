"""Extraction contract and a deterministic FIXTURE PARSER for tests.

The contract is the smallest provider-neutral interface this slice needs: text in,
*candidate* structured data out. It does not validate (see ``validation.py``), does
not know about databases, storage, tasks, or approvals, and does not depend on any
AI provider SDK. Real local and external adapters, data-policy enforcement,
capability checks, and the AI gateway (docs/architecture/ai-policy.md) are deferred.

``DeterministicTestExtractor`` is a FIXTURE PARSER. It is NOT an AI model and NOT a
document-understanding implementation: it recognizes one fixed, labelled-line template
and ignores everything else. It exists so the rest of the workflow can be tested with
repeatable input and output, and it must never be presented as, or substituted for, a
real extraction provider.

Fixture template (labels are case-insensitive; one label per line):

    Requester: <name>
    Contact: <contact>                                   (optional)
    Item: <quantity> | <unit> | <description> [| <item notes>]
    Notes: <request-level notes>                         (optional)

Behavior, exactly:

* ``Requester``, ``Contact`` and ``Notes``: the FIRST such line is used; later
  duplicates are ignored. An empty value is treated as absent (``None``).
* ``Item``: split on ``|`` into 3 or 4 fields (quantity, unit, description[, notes]).
  Every ``Item`` line yields one item, in order. An empty ``unit``, ``description`` or
  ``notes`` field becomes ``None``.
* A quantity written as an optionally signed decimal number in ASCII digits (``10``,
  ``-3``, ``2.50``) becomes a ``Decimal`` with exactly the digits as written (no
  rounding, no float). It is NOT checked here: ``0`` and ``-3`` are passed through for
  validation to reject. Any other non-empty quantity text (for example ``a few``,
  ``1,5``, or digits from another script) becomes ``None`` plus an ``Ambiguity``
  quoting it. An empty quantity is ``None``.
* An ``Item`` line without 3 or 4 fields yields an item with every field ``None`` plus
  an ``Ambiguity`` on ``items[i]`` quoting the line.
* Lines with any other label, and lines with no label, are ignored.
* Every extracted value gets a ``SourceRef`` following the convention documented on
  ``SourceRef``: 1-based ``str.splitlines()`` line numbers and ``make_excerpt`` excerpts.

The extractor holds no state: each call builds new, immutable results, so calls are
independent and repeatable. It performs no I/O and makes no network calls.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .schema import Ambiguity, RequestedItem, Requester, SourceRef, StructuredRequest, make_excerpt

_LABELLED_LINE = re.compile(r"\s*(requester|contact|item|notes)\s*:(.*)", re.IGNORECASE)
# ASCII digits only: "[0-9]" (unlike "\d") does not accept digits from other scripts.
_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class ExtractionResult:
    """Candidate data produced by an extractor. Not yet validated."""

    candidate: StructuredRequest
    ambiguities: tuple[Ambiguity, ...] = ()
    extractor_name: str = ""
    extractor_version: str = ""

    def __post_init__(self):
        if isinstance(self.ambiguities, list):
            object.__setattr__(self, "ambiguities", tuple(self.ambiguities))


class RequestExtractor(Protocol):
    """Provider-neutral extraction contract for the request workflow."""

    name: str
    version: str

    def extract(self, text: str) -> ExtractionResult:
        """Return candidate data for ``text``. Must not invent missing values."""
        ...


class DeterministicTestExtractor:
    """Fixture parser for one labelled-line test template. Not an AI model.

    See the module docstring for the exact template and behavior.
    """

    name = "deterministic-test-fixture-parser"
    version = "1"  # version of this fixture grammar, not of any model
    kind = "fixture_parser"
    understands_arbitrary_documents = False

    def extract(self, text: str) -> ExtractionResult:
        if not isinstance(text, str):
            raise TypeError("text must be a str")

        requester_name = contact = notes = None
        refs: list[SourceRef] = []
        ambiguities: list[Ambiguity] = []
        items: list[RequestedItem] = []

        for line_number, line in enumerate(text.splitlines(), start=1):
            match = _LABELLED_LINE.fullmatch(line)
            if match is None:
                continue
            label = match.group(1).lower()
            value = match.group(2).strip()
            excerpt = make_excerpt(line)

            if label == "requester":
                if requester_name is None and value:
                    requester_name = value
                    refs.append(SourceRef("requester.name", excerpt, line_number))
            elif label == "contact":
                if contact is None and value:
                    contact = value
                    refs.append(SourceRef("requester.contact", excerpt, line_number))
            elif label == "notes":
                if notes is None and value:
                    notes = value
                    refs.append(SourceRef("notes", excerpt, line_number))
            else:  # "item"
                items.append(self._read_item(len(items), value, line_number, excerpt, refs, ambiguities))

        candidate = StructuredRequest(
            requester=Requester(name=requester_name, contact=contact),
            items=tuple(items),
            notes=notes,
            source_refs=tuple(refs),
        )
        return ExtractionResult(
            candidate=candidate,
            ambiguities=tuple(ambiguities),
            extractor_name=self.name,
            extractor_version=self.version,
        )

    @staticmethod
    def _read_item(index, value, line_number, excerpt, refs, ambiguities):
        path = f"items[{index}]"
        fields = [field.strip() for field in value.split("|")]
        if len(fields) not in (3, 4):
            ambiguities.append(Ambiguity(path, excerpt))
            return RequestedItem()

        quantity_text, unit, description = fields[0], fields[1], fields[2]
        item_notes = fields[3] if len(fields) == 4 else ""

        quantity = None
        if _NUMBER.fullmatch(quantity_text):
            quantity = Decimal(quantity_text)  # exactly the digits as written
            refs.append(SourceRef(f"{path}.quantity", excerpt, line_number))
        elif quantity_text:
            ambiguities.append(Ambiguity(f"{path}.quantity", make_excerpt(quantity_text)))

        for suffix, text in (("unit", unit), ("description", description), ("notes", item_notes)):
            if text:
                refs.append(SourceRef(f"{path}.{suffix}", excerpt, line_number))

        return RequestedItem(
            description=description or None,
            quantity=quantity,
            unit=unit or None,
            notes=item_notes or None,
        )
