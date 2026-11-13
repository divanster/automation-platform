"""DB-free tests (standard-library unittest, no Django) for the intake rules and the run-state table.

Covers ``apps.intake.policy`` (text validation, identifiers, the task payload, the case reference) and
``apps.cases.run_state`` (states and transitions). Nothing here opens a database connection.
"""

import unittest
import uuid
from datetime import datetime, timezone

from apps.cases import run_state
from apps.cases.run_state import FailedStep, RunState, can_transition, transition_problem
from apps.intake import policy
from apps.intake.policy import (
    MAX_REQUEST_TEXT_BYTES,
    InvalidTaskPayloadError,
    RequestTextError,
    extraction_idempotency_key,
    extraction_payload,
    new_case_reference,
    validate_extraction_payload,
    validate_request_text,
)
from apps.tasks import policy as task_policy

RUN_ID = uuid.UUID("1234abcd-12ab-56cd-92ef-5678abcd1234")


class RequestTextTests(unittest.TestCase):
    def reason(self, value):
        with self.assertRaises(RequestTextError) as caught:
            validate_request_text(value)
        return caught.exception.reason

    def test_ordinary_text_is_returned_unchanged(self):
        text = "  Requester: Jane\r\nItem: 1 | x | y\n\n\tNotes: z  \n"
        self.assertIs(validate_request_text(text), text)

    def test_non_ascii_text_is_accepted(self):
        text = "Requester: Zoë 中文 \U0001f600"
        self.assertEqual(validate_request_text(text), text)

    def test_refusals_have_stable_reasons(self):
        self.assertEqual(self.reason(None), "not_text")
        self.assertEqual(self.reason(b"bytes"), "not_text")
        self.assertEqual(self.reason(12), "not_text")
        self.assertEqual(self.reason(""), "blank")
        self.assertEqual(self.reason(" \n\t "), "blank")
        self.assertEqual(self.reason("a\x00b"), "contains_nul")
        self.assertEqual(self.reason("a\ud800b"), "unencodable")

    def test_the_size_limit_counts_utf8_bytes_not_characters(self):
        self.assertEqual(len(validate_request_text("a" * MAX_REQUEST_TEXT_BYTES)), MAX_REQUEST_TEXT_BYTES)
        self.assertEqual(self.reason("a" * (MAX_REQUEST_TEXT_BYTES + 1)), "too_large")
        two_bytes = "é" * (MAX_REQUEST_TEXT_BYTES // 2)  # exactly at the limit in bytes
        self.assertEqual(validate_request_text(two_bytes), two_bytes)
        self.assertEqual(self.reason(two_bytes + "é"), "too_large")

    def test_the_limit_is_documented_as_provisional(self):
        self.assertIn("PROVISIONAL", policy.__doc__)
        self.assertIn("NOT MEASURED", policy.__doc__)

    def test_error_messages_never_echo_the_text(self):
        secret = "SECRET-TEXT-" + "x" * MAX_REQUEST_TEXT_BYTES
        with self.assertRaises(RequestTextError) as caught:
            validate_request_text(secret)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.reason("SECRET\x00"), "contains_nul")


class IdentifierTests(unittest.TestCase):
    def test_the_idempotency_key_is_kind_run_and_generation(self):
        key = extraction_idempotency_key(RUN_ID)
        self.assertEqual(key, f"request_intake.extract:{RUN_ID}:1")
        self.assertEqual(extraction_idempotency_key(RUN_ID, 2), f"request_intake.extract:{RUN_ID}:2")

    def test_the_key_is_accepted_by_the_queue_rules(self):
        self.assertEqual(task_policy.validate_idempotency_key(extraction_idempotency_key(RUN_ID, 9)), extraction_idempotency_key(RUN_ID, 9))

    def test_the_key_refuses_bad_arguments(self):
        for run_id, generation in ((str(RUN_ID), 1), (RUN_ID, 0), (RUN_ID, True), (RUN_ID, "1"), (RUN_ID, -1)):
            with self.subTest(run=run_id, generation=generation), self.assertRaises((TypeError, ValueError)):
                extraction_idempotency_key(run_id, generation)

    def test_the_payload_is_the_run_id_only(self):
        payload = extraction_payload(RUN_ID)
        self.assertEqual(payload, {"run_id": str(RUN_ID)})
        task_policy.canonical_json(payload, max_bytes=task_policy.MAX_PAYLOAD_BYTES, what="payload")  # fits the queue rules

    def test_the_payload_builder_refuses_a_string(self):
        with self.assertRaises(TypeError):
            extraction_payload(str(RUN_ID))


class PayloadValidationTests(unittest.TestCase):
    def test_a_valid_payload_is_copied(self):
        payload = {"run_id": str(RUN_ID)}
        clean = validate_extraction_payload(payload)
        self.assertEqual(clean, payload)
        self.assertIsNot(clean, payload)

    def test_everything_else_is_refused(self):
        bad = [
            None,
            [],
            "x",
            {},
            {"run_id": str(RUN_ID), "text": "Requester: Jane"},
            {"run_id": str(RUN_ID), "extra": 1},
            {"text": "Requester: Jane"},
            {"run_id": RUN_ID},
            {"run_id": 5},
            {"run_id": None},
            {"run_id": str(RUN_ID).upper()},
            {"run_id": str(RUN_ID).replace("-", "")},
            {"run_id": " " + str(RUN_ID)},
            {"run_id": "{" + str(RUN_ID) + "}"},
            {"run_id": "not-a-uuid"},
        ]
        for payload in bad:
            with self.subTest(payload=repr(payload)[:40]), self.assertRaises(InvalidTaskPayloadError):
                validate_extraction_payload(payload)

    def test_the_messages_do_not_echo_the_value(self):
        with self.assertRaises(InvalidTaskPayloadError) as caught:
            validate_extraction_payload({"run_id": "SECRET"})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_no_business_text_fits_the_payload_contract(self):
        self.assertEqual(set(extraction_payload(RUN_ID)), {"run_id"})


class CaseReferenceTests(unittest.TestCase):
    def test_format(self):
        now = datetime(2030, 2, 3, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(new_case_reference(now, "abcdef0123456789"), "REQ-20300203-ABCDEF01")

    def test_refusals(self):
        now = datetime(2030, 2, 3, tzinfo=timezone.utc)
        for token in ("short", "zzzzzzzz", 5, None):
            with self.subTest(token=token), self.assertRaises((TypeError, ValueError)):
                new_case_reference(now, token)
        with self.assertRaises(TypeError):
            new_case_reference("2030-02-03", "abcdef01")

    def test_the_reference_fits_the_case_column(self):
        self.assertLessEqual(len(new_case_reference(datetime(2030, 1, 1, tzinfo=timezone.utc), uuid.uuid4().hex)), 100)


class RunStateVocabularyTests(unittest.TestCase):
    def test_the_states_are_the_documented_ones_in_order(self):
        self.assertEqual(
            run_state.STATES,
            ("queued", "extracting", "needs_review", "approved", "exporting", "exported", "failed", "cancelled"),
        )
        self.assertEqual(run_state.FAILED_STEPS, ("extract", "export"))

    def test_every_state_has_a_row_in_the_table(self):
        self.assertEqual(set(run_state.ALLOWED_TRANSITIONS), set(RunState))

    def test_terminal_states_have_no_exit(self):
        for state in run_state.TERMINAL_STATES:
            self.assertEqual(run_state.ALLOWED_TRANSITIONS[state], frozenset())

    def test_the_module_is_standard_library_only(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(run_state.__file__).read_text(encoding="utf-8"))
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imported, {"enum"})


class RunTransitionTests(unittest.TestCase):
    ALLOWED = [
        ("queued", "extracting"),
        ("queued", "cancelled"),
        ("extracting", "extracting"),  # a claimed-again task finds the run already here
        ("extracting", "needs_review"),
        ("extracting", "cancelled"),
        ("needs_review", "needs_review"),  # a human edit
        ("needs_review", "approved"),
        ("needs_review", "cancelled"),
        ("approved", "needs_review"),
        ("approved", "exporting"),
        ("approved", "cancelled"),
        ("exporting", "exported"),
        ("exporting", "needs_review"),
        ("exporting", "cancelled"),
    ]

    def test_every_listed_transition_is_allowed(self):
        for current, target in self.ALLOWED:
            with self.subTest(transition=f"{current}->{target}"):
                self.assertTrue(can_transition(current, target))
                self.assertIsNone(transition_problem(current, target))

    def test_failure_transitions_need_a_failed_step(self):
        for current in ("queued", "extracting", "exporting"):
            self.assertIsNone(transition_problem(current, "failed", new_failed_step="extract"))
            self.assertEqual(transition_problem(current, "failed"), "failed_step_required")
            self.assertEqual(transition_problem(current, "failed", new_failed_step="other"), "failed_step_required")

    def test_a_failed_step_is_refused_on_any_other_target(self):
        self.assertEqual(transition_problem("extracting", "needs_review", new_failed_step="extract"), "failed_step_unexpected")

    def test_a_failed_run_leaves_only_by_retrying_the_step_that_failed(self):
        self.assertIsNone(transition_problem("failed", "queued", current_failed_step="extract"))
        self.assertIsNone(transition_problem("failed", "approved", current_failed_step="export"))
        self.assertEqual(transition_problem("failed", "queued", current_failed_step="export"), "wrong_retry_target")
        self.assertEqual(transition_problem("failed", "approved", current_failed_step="extract"), "wrong_retry_target")
        self.assertEqual(transition_problem("failed", "queued", current_failed_step=""), "wrong_retry_target")
        self.assertIsNone(transition_problem("failed", "cancelled", current_failed_step="extract"))

    def test_every_other_transition_is_refused(self):
        allowed = {tuple(pair) for pair in self.ALLOWED} | {("queued", "failed"), ("extracting", "failed"), ("exporting", "failed"), ("failed", "queued"), ("failed", "approved"), ("failed", "cancelled")}
        for current in run_state.STATES:
            for target in run_state.STATES:
                if (current, target) in allowed:
                    continue
                with self.subTest(transition=f"{current}->{target}"):
                    self.assertFalse(can_transition(current, target))
                    self.assertEqual(transition_problem(current, target), "not_allowed")

    def test_the_first_extraction_path_is_allowed_step_by_step(self):
        path = ["queued", "extracting", "needs_review"]
        for current, target in zip(path, path[1:]):
            self.assertIsNone(transition_problem(current, target))

    def test_a_queued_run_cannot_jump_to_needs_review_but_can_fail_before_it_was_ever_claimed(self):
        self.assertEqual(transition_problem("queued", "needs_review"), "not_allowed")
        self.assertEqual(transition_problem("queued", "approved"), "not_allowed")
        self.assertIsNone(transition_problem("queued", "failed", new_failed_step="extract"))  # e.g. unknown_kind refused by the worker

    def test_unknown_states_are_refused(self):
        self.assertEqual(transition_problem("queued", "bogus"), "unknown_state")
        self.assertEqual(transition_problem("bogus", "queued"), "unknown_state")

    def test_enums_accept_their_text_values(self):
        self.assertIsNone(transition_problem(RunState.QUEUED, RunState.EXTRACTING))
        self.assertIsNone(transition_problem(RunState.EXTRACTING, RunState.FAILED, new_failed_step=FailedStep.EXTRACT.value))


if __name__ == "__main__":
    unittest.main()
