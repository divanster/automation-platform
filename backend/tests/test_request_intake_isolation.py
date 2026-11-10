"""Pure-domain tests (standard-library unittest, no Django) for mutation and shared state.

Verifies that validation does not mutate its input, that frozen drafts do not alias
caller-owned mutable objects or expose shared mutable nested state, and that repeated
calls return independent results.
"""

import copy
import dataclasses
import unittest
from decimal import Decimal

from tests.intake_support import VALID_TEXT, find_mutable_state, make_request
from workflows.request_intake import (
    Ambiguity,
    DeterministicTestExtractor,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    ValidationLimits,
    validate_request,
)


class ValidationDoesNotMutateTests(unittest.TestCase):
    def requests(self):
        text = VALID_TEXT
        yield make_request(), text
        yield make_request(requester=Requester(name=None), items=(RequestedItem(description="x", quantity=0),)), text
        yield make_request(items=[RequestedItem(description="a", quantity=1), RequestedItem(description="A", quantity=2)]), "a A"
        yield DeterministicTestExtractor().extract(text).candidate, text
        yield make_request(source_refs=(SourceRef("notes", "wrong", 9),)), text

    def test_the_request_is_unchanged_after_validation(self):
        for request, text in self.requests():
            with self.subTest(request=repr(request)[:60]):
                before = copy.deepcopy(request)
                identity_before = (id(request.requester), id(request.items), id(request.source_refs), [id(i) for i in request.items])
                validate_request(request, source_text=text, limits=ValidationLimits(max_quantity=5, max_text_length=50))
                self.assertEqual(request, before)
                self.assertEqual(identity_before, (id(request.requester), id(request.items), id(request.source_refs), [id(i) for i in request.items]))

    def test_ambiguities_and_limits_are_unchanged_after_validation(self):
        ambiguities = [Ambiguity("notes", "?"), Ambiguity("items[0].quantity", "a few")]
        before = copy.deepcopy(ambiguities)
        limits = ValidationLimits(max_quantity=Decimal("2.5"), max_text_length=10)
        limits_before = dataclasses.astuple(limits)
        validate_request(make_request(), limits=limits, ambiguities=ambiguities)
        self.assertEqual(ambiguities, before)
        self.assertEqual(dataclasses.astuple(limits), limits_before)

    def test_the_source_text_is_a_str_and_cannot_be_changed(self):
        text = "Requester: Jane"
        validate_request(make_request(), source_text=text)
        self.assertEqual(text, "Requester: Jane")

    def test_repeated_validation_of_one_request_is_identical(self):
        request = make_request(requester=Requester(name=None))
        results = [validate_request(request, source_text="x") for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertIsNot(results[0], results[1])
        self.assertTrue(results[0].findings)  # a non-trivial case: there are findings to compare
        self.assertIsNot(results[0].findings[0], results[1].findings[0])


class NoAliasingOfCallerObjectsTests(unittest.TestCase):
    def test_a_list_of_items_is_copied_into_a_tuple(self):
        items = [RequestedItem(description="Pens", quantity=1)]
        request = StructuredRequest(requester=Requester(name="Jane"), items=items)
        self.assertIsInstance(request.items, tuple)
        self.assertIsNot(request.items, items)
        items.append(RequestedItem(description="Ink", quantity=2))
        items[0] = RequestedItem(description="Changed", quantity=9)
        self.assertEqual(len(request.items), 1)
        self.assertEqual(request.items[0].description, "Pens")

    def test_a_list_of_source_refs_is_copied_into_a_tuple(self):
        refs = [SourceRef("notes", "Notes: x", 1)]
        request = StructuredRequest(source_refs=refs)
        self.assertIsInstance(request.source_refs, tuple)
        refs.append(SourceRef("notes", "later", 2))
        self.assertEqual(len(request.source_refs), 1)

    def test_replace_also_copies_lists(self):
        base = make_request()
        items = [RequestedItem(description="Pens", quantity=1)]
        changed = dataclasses.replace(base, items=items)
        items.clear()
        self.assertEqual(len(changed.items), 1)
        self.assertEqual(len(base.items), 2)

    def test_there_are_no_shared_default_instances(self):
        a, b = StructuredRequest(), StructuredRequest()
        self.assertIsNot(a.requester, b.requester)
        self.assertEqual(a.requester, b.requester)

    def test_other_mutable_container_values_are_rejected_by_validation_not_shared_silently(self):
        # Dataclasses cannot forbid a caller from passing a dict; validation reports it.
        request = StructuredRequest(requester=Requester(name="Jane"), items=(RequestedItem(description="x", quantity={"n": 1}),))
        self.assertEqual([f.code.value for f in validate_request(request).findings], ["invalid_type"])


class NoSharedMutableNestedStateTests(unittest.TestCase):
    def test_a_constructed_draft_has_no_mutable_containers(self):
        request = make_request(items=[RequestedItem(description="x", quantity=1.5)], source_refs=[SourceRef("notes", "n", 1)])
        self.assertEqual(find_mutable_state(request), [])

    def test_extractor_output_has_no_mutable_containers(self):
        text = "Requester: A\nContact: c\nItem: 1 | u | X | n\nItem: lots | | Y\nItem: broken\nNotes: z\n"
        self.assertEqual(find_mutable_state(DeterministicTestExtractor().extract(text)), [])

    def test_validation_results_have_no_mutable_containers(self):
        result = validate_request(make_request(requester=Requester(name=None)), source_text="x", ambiguities=[Ambiguity("notes", "?")])
        self.assertEqual(find_mutable_state(result), [])

    def test_the_checker_itself_detects_mutable_state(self):
        leaky = dataclasses.replace(make_request())
        object.__setattr__(leaky, "items", [RequestedItem(description="x", quantity=1)])  # bypass the copy on purpose
        self.assertEqual(len(find_mutable_state(leaky)), 1)


class IndependentResultsTests(unittest.TestCase):
    def test_validating_equal_requests_gives_independent_equal_results(self):
        a = make_request(requester=Requester(name=None))
        b = make_request(requester=Requester(name=None))
        ra, rb = validate_request(a), validate_request(b)
        self.assertEqual(ra, rb)
        self.assertIsNot(ra, rb)
        self.assertIsNot(ra.findings[0], rb.findings[0])

    def test_one_validation_does_not_influence_the_next(self):
        bad = make_request(requester=Requester(name=None))
        good = make_request()
        first = validate_request(bad, ambiguities=[Ambiguity("notes", "?")])
        second = validate_request(good)
        third = validate_request(bad, ambiguities=[Ambiguity("notes", "?")])
        self.assertEqual(second.findings, ())
        self.assertEqual(first, third)

    def test_extracting_then_validating_is_repeatable(self):
        extractor = DeterministicTestExtractor()
        outcomes = []
        for _ in range(3):
            extraction = extractor.extract(VALID_TEXT)
            outcomes.append(validate_request(extraction.candidate, source_text=VALID_TEXT, ambiguities=extraction.ambiguities))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[1], outcomes[2])
        self.assertTrue(all(o.is_valid for o in outcomes))


if __name__ == "__main__":
    unittest.main()
