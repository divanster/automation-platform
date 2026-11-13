"""DB-free tests: the pure review rules (edit, approval, rejection, self-approval, warnings) and the edit builder.

The builder tests use the real domain (validation, canonicalization, hashing); they prove that an edited
draft goes through the same path as an extraction and that nothing in ``apps.intake`` reimplements it.
"""

import uuid
from decimal import Decimal

from django.test import SimpleTestCase

from apps.cases.models import REASON_CODE_PATTERN
from apps.cases.run_state import STATES, RunState
from apps.intake import review_rules as rules
from apps.intake.editing import build_edit_revision
from apps.intake.errors import (
    DraftInvalidError,
    NoChangeError,
    ReviewRefusedError,
    SelfApprovalError,
    StaleRevisionError,
    WarningsNotAcknowledgedError,
    refusal,
)
from apps.intake.policy import REJECTION_REASONS
from tests.intake_support import make_request
from workflows.request_intake import (
    Ambiguity,
    RequestedItem,
    Requester,
    SourceRef,
    StructuredRequest,
    canonicalize_request,
    make_excerpt,
)

CASE, RUN = uuid.uuid4(), uuid.uuid4()
REV_A, REV_B = uuid.uuid4(), uuid.uuid4()
HASH_A, HASH_B = "a" * 64, "b" * 64
WARNING = ("duplicate_item", "items[1]")
NEEDS_REVIEW, APPROVED = RunState.NEEDS_REVIEW.value, RunState.APPROVED.value


def approval(**overrides):
    facts = {
        "run_state": NEEDS_REVIEW,
        "current_revision_id": REV_A,
        "revision_id": REV_A,
        "current_hash": HASH_A,
        "submitted_hash": HASH_A,
        "self_approval": False,
        "revision_rejected": False,
        "current_warnings": (),
        "acknowledged_warnings": frozenset(),
    }
    facts.update(overrides)
    return rules.approval_problem(**facts)


def rejection(**overrides):
    facts = {
        "run_state": NEEDS_REVIEW,
        "current_revision_id": REV_A,
        "revision_id": REV_A,
        "current_hash": HASH_A,
        "submitted_hash": HASH_A,
        "reason_code": "incorrect_content",
    }
    facts.update(overrides)
    return rules.rejection_problem(**facts)


def edit(**overrides):
    facts = {"run_state": NEEDS_REVIEW, "current_revision_id": REV_A, "base_revision_id": REV_A, "has_invalid_outcome": False}
    facts.update(overrides)
    return rules.edit_problem(**facts)


class EditRuleTests(SimpleTestCase):
    def test_an_edit_of_the_current_revision_is_allowed_in_review_and_approved(self):
        for state in (NEEDS_REVIEW, APPROVED):
            with self.subTest(state=state):
                self.assertIsNone(edit(run_state=state))

    def test_every_other_state_is_refused(self):
        for state in STATES:
            if state in (NEEDS_REVIEW, APPROVED):
                continue
            with self.subTest(state=state):
                self.assertEqual(edit(run_state=state), "run_state_not_editable")

    def test_exporting_is_refused_because_export_cancellation_does_not_exist_yet(self):
        self.assertEqual(edit(run_state="exporting"), "run_state_not_editable")

    def test_a_stale_base_is_refused(self):
        self.assertEqual(edit(base_revision_id=REV_B), "stale_base_revision")
        self.assertEqual(edit(base_revision_id=None), "stale_base_revision")  # claims "no revision yet" but one exists

    def test_the_first_edit_of_an_invalid_outcome_has_no_base(self):
        self.assertIsNone(edit(current_revision_id=None, base_revision_id=None, has_invalid_outcome=True))

    def test_the_first_edit_cannot_name_a_base_that_does_not_exist(self):
        self.assertEqual(edit(current_revision_id=None, base_revision_id=REV_A, has_invalid_outcome=True), "stale_base_revision")

    def test_no_revision_and_no_invalid_outcome_leaves_nothing_to_edit(self):
        self.assertEqual(edit(current_revision_id=None, base_revision_id=None, has_invalid_outcome=False), "nothing_to_edit")

    def test_the_state_is_checked_before_the_base(self):
        self.assertEqual(edit(run_state="queued", base_revision_id=REV_B), "run_state_not_editable")


class SelfApprovalTests(SimpleTestCase):
    def test_the_submitter_may_not_approve(self):
        self.assertTrue(rules.is_self_approval(actor_pk=1, submitter_pk=1, author_pks=[]))

    def test_an_author_of_any_revision_may_not_approve(self):
        self.assertTrue(rules.is_self_approval(actor_pk=3, submitter_pk=1, author_pks=[2, 3]))
        self.assertTrue(rules.is_self_approval(actor_pk=2, submitter_pk=1, author_pks=iter([2, 2])))

    def test_somebody_else_may(self):
        self.assertFalse(rules.is_self_approval(actor_pk=9, submitter_pk=1, author_pks=[2, 3]))
        self.assertFalse(rules.is_self_approval(actor_pk=9, submitter_pk=1, author_pks=[]))


class WarningAcknowledgementTests(SimpleTestCase):
    def test_pairs_become_a_set_of_tuples(self):
        self.assertEqual(rules.normalize_warning_pairs([["a", "b"], ("a", "b"), ("c", "d")]), frozenset({("a", "b"), ("c", "d")}))
        self.assertEqual(rules.normalize_warning_pairs(()), frozenset())

    def test_malformed_input_is_refused(self):
        for bad in ("ab", b"ab", 5, None, [("a",)], [("a", "b", "c")], [("a", 1)], ["ab"], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rules.normalize_warning_pairs(bad)

    def test_no_warnings_need_no_acknowledgement(self):
        self.assertIsNone(approval(current_warnings=(), acknowledged_warnings=frozenset()))

    def test_a_missing_acknowledgement_is_refused_with_the_missing_pairs(self):
        reason, details = approval(current_warnings=(WARNING,), acknowledged_warnings=frozenset())
        self.assertEqual(reason, "warnings_not_acknowledged")
        self.assertEqual(details["missing"], (WARNING,))
        self.assertEqual(details["unexpected"], ())

    def test_a_partial_acknowledgement_is_refused(self):
        second = ("value_not_in_source", "items[0].description")
        reason, details = approval(current_warnings=(WARNING, second), acknowledged_warnings=frozenset({WARNING}))
        self.assertEqual((reason, details["missing"]), ("warnings_not_acknowledged", (second,)))

    def test_an_exact_acknowledgement_passes(self):
        self.assertIsNone(approval(current_warnings=(WARNING,), acknowledged_warnings=frozenset({WARNING})))

    def test_an_acknowledgement_of_a_warning_the_revision_does_not_have_is_refused(self):
        reason, details = approval(current_warnings=(), acknowledged_warnings=frozenset({WARNING}))
        self.assertEqual((reason, details["unexpected"]), ("unknown_warning_acknowledged", (WARNING,)))

    def test_a_stale_acknowledgement_for_a_changed_revision_is_refused(self):
        # Acknowledged the warnings of the previous revision; the current one has different ones.
        reason, details = approval(current_warnings=(("duplicate_item", "items[2]"),), acknowledged_warnings=frozenset({WARNING}))
        self.assertEqual(reason, "warnings_not_acknowledged")
        self.assertEqual(details["unexpected"], (WARNING,))


class ApprovalRuleTests(SimpleTestCase):
    def test_the_plain_case_passes(self):
        self.assertIsNone(approval())

    def test_only_needs_review_may_be_approved(self):
        for state in STATES:
            if state == NEEDS_REVIEW:
                continue
            with self.subTest(state=state):
                self.assertEqual(approval(run_state=state)[0], "run_not_in_review")

    def test_no_current_revision(self):
        self.assertEqual(approval(current_revision_id=None, current_hash=None)[0], "no_current_revision")

    def test_self_approval_is_forbidden(self):
        self.assertEqual(approval(self_approval=True)[0], "self_approval_forbidden")

    def test_a_non_current_revision_is_stale(self):
        self.assertEqual(approval(revision_id=REV_B)[0], "stale_revision")

    def test_a_wrong_hash_is_refused(self):
        self.assertEqual(approval(submitted_hash=HASH_B)[0], "content_hash_mismatch")
        self.assertEqual(approval(submitted_hash="")[0], "content_hash_mismatch")

    def test_the_right_hash_for_the_wrong_revision_is_still_stale(self):
        self.assertEqual(approval(revision_id=REV_B, submitted_hash=HASH_A)[0], "stale_revision")

    def test_a_rejected_revision_cannot_be_approved(self):
        self.assertEqual(approval(revision_rejected=True)[0], "revision_rejected")

    def test_the_documented_order_of_refusals(self):
        # Start with every fact wrong; fix the reported problem; the next one in the documented order appears.
        facts = {
            "run_state": "queued",
            "current_revision_id": None,
            "self_approval": True,
            "revision_id": REV_B,
            "submitted_hash": HASH_B,
            "revision_rejected": True,
            "current_warnings": (WARNING,),
        }
        fixes = [
            ("run_not_in_review", "run_state", NEEDS_REVIEW),
            ("no_current_revision", "current_revision_id", REV_A),
            ("self_approval_forbidden", "self_approval", False),
            ("stale_revision", "revision_id", REV_A),
            ("content_hash_mismatch", "submitted_hash", HASH_A),
            ("revision_rejected", "revision_rejected", False),
            ("warnings_not_acknowledged", "current_warnings", ()),
        ]
        for reason, key, fixed in fixes:
            with self.subTest(reason=reason):
                self.assertEqual(approval(**facts)[0], reason)
            facts[key] = fixed
        self.assertIsNone(approval(**facts))

    def test_a_superuser_input_does_not_exist(self):
        # The rules take no notion of who the actor is beyond the precomputed self_approval flag.
        import inspect

        self.assertNotIn("superuser", " ".join(inspect.signature(rules.approval_problem).parameters))


class RejectionRuleTests(SimpleTestCase):
    def test_the_plain_case_passes_for_every_reason(self):
        for reason in REJECTION_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNone(rejection(reason_code=reason))

    def test_unknown_or_free_text_reasons_are_refused(self):
        for reason in ("", "Incorrect content", "incorrect_content ", "please fix the totals", "other\n", "OTHER"):
            with self.subTest(reason=reason):
                self.assertEqual(rejection(reason_code=reason), "unknown_reason_code")

    def test_only_needs_review_may_be_rejected(self):
        for state in STATES:
            if state != NEEDS_REVIEW:
                with self.subTest(state=state):
                    self.assertEqual(rejection(run_state=state), "run_not_in_review")

    def test_binding_checks(self):
        self.assertEqual(rejection(current_revision_id=None, current_hash=None), "no_current_revision")
        self.assertEqual(rejection(revision_id=REV_B), "stale_revision")
        self.assertEqual(rejection(submitted_hash=HASH_B), "content_hash_mismatch")

    def test_the_reason_codes_have_the_shape_the_database_checks(self):
        import re

        for reason in REJECTION_REASONS:
            self.assertRegex(reason, REASON_CODE_PATTERN)
        self.assertEqual(len(set(REJECTION_REASONS)), len(REJECTION_REASONS))
        self.assertTrue(re.fullmatch(REASON_CODE_PATTERN, "a_b_1"))


class RefusalErrorTests(SimpleTestCase):
    def test_reasons_map_to_specific_errors(self):
        self.assertIsInstance(refusal("stale_base_revision"), StaleRevisionError)
        self.assertIsInstance(refusal("stale_revision"), StaleRevisionError)
        self.assertIsInstance(refusal("self_approval_forbidden"), SelfApprovalError)
        self.assertIsInstance(refusal("unchanged_content"), NoChangeError)
        self.assertIsInstance(refusal("warnings_not_acknowledged", missing=(WARNING,)), WarningsNotAcknowledgedError)
        self.assertIs(type(refusal("run_not_in_review")), ReviewRefusedError)

    def test_every_specific_error_is_a_review_refusal_with_a_reason(self):
        for error in (refusal("stale_revision"), refusal("self_approval_forbidden"), refusal("unchanged_content"), refusal("other_reason")):
            self.assertIsInstance(error, ReviewRefusedError)
            self.assertTrue(error.reason)

    def test_messages_name_the_code_and_nothing_else(self):
        self.assertEqual(str(refusal("stale_revision")), "The review operation is refused (stale_revision).")
        error = refusal("warnings_not_acknowledged", missing=(("a", "b"),), unexpected=())
        self.assertNotIn("items", str(error))

    def test_raise_if_refused(self):
        rules.raise_if_refused(None)
        with self.assertRaises(SelfApprovalError):
            rules.raise_if_refused(("self_approval_forbidden", {}))
        with self.assertRaises(ReviewRefusedError) as caught:
            rules.raise_if_refused("run_state_not_editable")
        self.assertEqual(caught.exception.reason, "run_state_not_editable")
        with self.assertRaises(WarningsNotAcknowledgedError) as caught:
            rules.raise_if_refused(("warnings_not_acknowledged", {"missing": (WARNING,), "unexpected": ()}))
        self.assertEqual(caught.exception.missing, (WARNING,))


# --- the edit builder (real domain code) ----------------------------------------------------------------

SOURCE = "Requester: Jane Doe\nItem: 10 | boxes | A4 paper\nItem: 2 | | Stapler\n"


def build(draft, source=SOURCE, number=2, **kwargs):
    return build_edit_revision(draft, source_text=source, case_id=CASE, run_id=RUN, revision_id=uuid.uuid4(), revision_number=number, **kwargs)


class BuildEditRevisionTests(SimpleTestCase):
    def test_a_valid_draft_becomes_a_revision_with_the_requested_number_and_ids(self):
        revision = build(make_request(), number=3)
        self.assertEqual((revision.case_id, revision.run_id, revision.revision_number), (CASE, RUN, 3))
        self.assertEqual(revision.consistency_problems(), ())

    def test_the_bytes_and_hash_are_exactly_the_domains_canonicalization(self):
        draft = make_request()
        revision = build(draft)
        content = canonicalize_request(draft, source_text=SOURCE)
        self.assertEqual(revision.canonical_bytes, content.canonical_bytes)
        self.assertEqual(revision.content_hash, content.content_hash)
        self.assertEqual(revision.canonical_format_version, content.canonical_format_version)

    def test_the_same_draft_gives_the_same_hash_and_a_different_revision_id(self):
        first, second = build(make_request()), build(make_request())
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.revision_id, second.revision_id)

    def test_a_changed_draft_gives_a_different_hash(self):
        changed = make_request(notes="Needed next week.")
        self.assertNotEqual(build(make_request()).content_hash, build(changed).content_hash)

    def test_a_draft_with_blocking_findings_is_never_a_revision(self):
        draft = make_request(requester=Requester(name=None), items=(RequestedItem(description="Pens", quantity=0),))
        with self.assertRaises(DraftInvalidError) as caught:
            build(draft)
        error = caught.exception
        self.assertEqual(error.reason, "blocking_findings")
        codes = {(f.code, f.field_path) for f in error.findings}
        self.assertIn(("missing_required", "requester.name"), codes)
        self.assertTrue(any(f.blocking for f in error.findings))

    def test_the_error_never_echoes_content(self):
        draft = make_request(requester=Requester(name=None), notes="SECRET-NOTE")
        with self.assertRaises(DraftInvalidError) as caught:
            build(draft)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(str(caught.exception), "The review operation is refused (blocking_findings).")

    def test_source_references_are_verified_against_the_stored_source_text(self):
        good = SourceRef(field_path="items[0].description", excerpt=make_excerpt("Item: 10 | boxes | A4 paper"), line=2)
        self.assertEqual(build(make_request(source_refs=(good,))).consistency_problems(), ())
        wrong_line = SourceRef(field_path="items[0].description", excerpt=make_excerpt("Item: 10 | boxes | A4 paper"), line=1)
        invented = SourceRef(field_path="items[0].description", excerpt="a line that is not in the source", line=None)
        for bad in (wrong_line, invented):
            with self.subTest(ref=bad), self.assertRaises(DraftInvalidError) as caught:
                build(make_request(source_refs=(bad,)))
            self.assertIn("invalid_source_ref", {f.code for f in caught.exception.findings})

    def test_warnings_are_computed_by_validation_and_kept_on_the_revision(self):
        duplicate = make_request(items=(RequestedItem(description="Stapler", quantity=1), RequestedItem(description="Stapler", quantity=1)))
        revision = build(duplicate)
        self.assertIn(("duplicate_item", "items[1].description"), [(w.code, w.field_path) for w in revision.warnings])
        self.assertTrue(all(not f.blocking for f in revision.findings))

    def test_a_value_that_is_not_in_the_source_is_a_warning_not_a_refusal(self):
        revision = build(make_request(items=(RequestedItem(description="Gold bars", quantity=1),)))
        self.assertIn("value_not_in_source", {w.code for w in revision.warnings})

    def test_extractor_ambiguities_are_not_carried_into_an_edit(self):
        # Documented decision: the edited draft is judged on its own (see apps.intake.editing).
        revision = build(make_request())
        self.assertNotIn("ambiguous_optional_value", {f.code for f in revision.findings})
        self.assertEqual(Ambiguity.__name__, "Ambiguity")

    def test_a_value_that_validates_but_cannot_be_serialized_is_a_stable_refusal(self):
        draft = make_request(items=(RequestedItem(description="Paper", quantity=Decimal("1E+1000000")),))
        with self.assertRaises(DraftInvalidError) as caught:
            build(draft)
        self.assertEqual(caught.exception.reason, "not_serializable")
        self.assertEqual(caught.exception.path, "items[0].quantity")
        self.assertEqual(caught.exception.findings, ())

    def test_wrong_types_are_programming_errors(self):
        with self.assertRaises(TypeError):
            build({"requester": {"name": "x"}})
        with self.assertRaises(ValueError):
            build(make_request(), number=0)
        with self.assertRaises(Exception):
            build_edit_revision(make_request(), source_text=SOURCE, case_id="not-a-uuid", run_id=RUN, revision_id=uuid.uuid4(), revision_number=1)

    def test_the_draft_is_not_modified(self):
        draft = make_request()
        before = repr(draft)
        build(draft)
        self.assertEqual(repr(draft), before)
        self.assertIsInstance(draft, StructuredRequest)
