"""DB-free tests for the pure queue policy and the test handler (``apps.tasks.policy`` / ``handlers``).

Standard-library unittest: nothing here imports Django or opens a database connection. Every
time-dependent rule is tested with fixed instants; there are no sleeps. This file is ASCII only.
"""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from apps.tasks import handlers, policy
from apps.tasks.policy import (
    AttemptOutcome,
    Failure,
    FailureCode,
    InvalidPayloadError,
    QueuePolicyError,
    TaskPermanentError,
    TaskRetryableError,
    TaskState,
)

T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
ALL_STATES = list(TaskState)


class StateTransitionTests(unittest.TestCase):
    def test_the_states_are_exactly_the_documented_ones(self):
        self.assertEqual({s.value for s in TaskState}, {"queued", "running", "succeeded", "failed", "cancelled"})

    def test_the_allowed_transitions_are_exactly_the_documented_ones(self):
        allowed = {(a.value, b.value) for a in ALL_STATES for b in ALL_STATES if policy.can_transition(a, b)}
        self.assertEqual(
            allowed,
            {
                ("queued", "running"),
                ("queued", "cancelled"),
                ("running", "succeeded"),
                ("running", "failed"),
                ("running", "queued"),
            },
        )

    def test_terminal_states_have_no_outgoing_transition(self):
        for state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            with self.subTest(state=state):
                self.assertTrue(policy.is_terminal(state))
                self.assertFalse(any(policy.can_transition(state, other) for other in ALL_STATES))

    def test_a_finished_task_cannot_be_restarted(self):
        for state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            self.assertFalse(policy.can_transition(state, TaskState.QUEUED))
            self.assertFalse(policy.can_transition(state, TaskState.RUNNING))

    def test_queued_and_running_are_not_terminal(self):
        self.assertFalse(policy.is_terminal("queued"))
        self.assertFalse(policy.is_terminal("running"))

    def test_task_states_are_not_workflow_run_states_or_the_old_ready_name(self):
        self.assertNotIn("ready", {s.value for s in TaskState})

    def test_unknown_states_are_rejected(self):
        with self.assertRaises(ValueError):
            policy.can_transition("ready", "running")

    def test_attempt_outcomes(self):
        self.assertEqual({o.value for o in AttemptOutcome}, {"running", "succeeded", "failed", "abandoned"})


class AvailabilityTests(unittest.TestCase):
    def test_a_queued_task_is_available_exactly_when_its_time_is_reached(self):
        self.assertFalse(policy.is_available(state="queued", available_at=T0, now=T0 - timedelta(microseconds=1)))
        self.assertTrue(policy.is_available(state="queued", available_at=T0, now=T0))
        self.assertTrue(policy.is_available(state="queued", available_at=T0, now=T0 + timedelta(seconds=1)))

    def test_other_states_are_never_available(self):
        for state in ("running", "succeeded", "failed", "cancelled"):
            self.assertFalse(policy.is_available(state=state, available_at=T0, now=T0 + timedelta(days=1)))


class LeaseTests(unittest.TestCase):
    TOKEN = "token-a"

    def problem(self, *, state="running", expected="token-a", actual="token-a", expires=T0 + timedelta(seconds=60), now=T0):
        return policy.lease_problem(state=state, expected_token=expected, actual_token=actual, lease_expires_at=expires, now=now)

    def test_a_held_unexpired_lease_is_fine(self):
        self.assertIsNone(self.problem())
        self.assertIsNone(self.problem(now=T0 + timedelta(seconds=60) - timedelta(microseconds=1)))

    def test_the_lease_is_expired_at_the_expiry_instant_itself(self):
        self.assertEqual(self.problem(now=T0 + timedelta(seconds=60)), "lease_expired")
        self.assertEqual(self.problem(now=T0 + timedelta(hours=1)), "lease_expired")

    def test_a_different_token_is_refused_even_when_unexpired(self):
        self.assertEqual(self.problem(actual="token-b"), "token_mismatch")
        self.assertEqual(self.problem(actual=None), "token_mismatch")

    def test_a_task_that_is_not_running_is_refused(self):
        for state in ("queued", "succeeded", "failed", "cancelled"):
            self.assertEqual(self.problem(state=state, actual=None, expires=None), "not_running")

    def test_a_running_task_without_an_expiry_is_refused(self):
        self.assertEqual(self.problem(expires=None), "lease_expired")

    def test_the_state_is_checked_before_the_token_and_the_token_before_the_expiry(self):
        self.assertEqual(self.problem(state="queued", actual="x", now=T0 + timedelta(days=1)), "not_running")
        self.assertEqual(self.problem(actual="x", now=T0 + timedelta(days=1)), "token_mismatch")

    def test_lease_expiry_is_now_plus_the_lease(self):
        self.assertEqual(policy.lease_expiry(T0, 60), T0 + timedelta(seconds=60))

    def test_lease_bounds(self):
        for good in (5, 60, 3600):
            self.assertEqual(policy.validate_lease_seconds(good), good)
        for bad in (0, 4, 3601, -1, 1.5, "60", None, True):
            with self.subTest(bad=bad), self.assertRaises(QueuePolicyError):
                policy.validate_lease_seconds(bad)


class BackoffTests(unittest.TestCase):
    def delays(self, rng, count=9):
        return [policy.backoff_seconds(n, rng=rng) for n in range(1, count + 1)]

    def test_the_delay_doubles_from_the_base_up_to_the_cap(self):
        self.assertEqual(self.delays(lambda: 0.0), [5, 10, 20, 40, 80, 160, 300, 300, 300])

    def test_jitter_only_shortens_the_delay_by_at_most_the_ratio(self):
        for n in range(1, 10):
            full = policy.backoff_seconds(n, rng=lambda: 0.0)
            for r in (0.0, 0.25, 0.5, 0.999999):
                delay = policy.backoff_seconds(n, rng=lambda r=r: r)
                self.assertLessEqual(delay, full)
                self.assertGreaterEqual(delay, full * (1 - policy.BACKOFF_JITTER_RATIO) - 1e-9)

    def test_the_delay_never_exceeds_the_cap_and_is_positive(self):
        for n in (1, 5, 50, 1000):
            delay = policy.backoff_seconds(n)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, policy.BACKOFF_CAP_SECONDS)

    def test_the_default_rng_stays_in_the_jitter_window(self):
        for _ in range(200):
            self.assertTrue(4.0 <= policy.backoff_seconds(1) <= 5.0)

    def test_invalid_attempt_counts_are_refused(self):
        for bad in (0, -1, 1.0, "1", None, True):
            with self.subTest(bad=bad), self.assertRaises(QueuePolicyError):
                policy.backoff_seconds(bad)


class RetryDecisionTests(unittest.TestCase):
    RETRYABLE = Failure(FailureCode.TEMPORARY_FAILURE, retryable=True)
    PERMANENT = Failure(FailureCode.REJECTED, retryable=False)

    def decide(self, failure, attempts, maximum=3, now=T0):
        return policy.decide_retry(failure=failure, attempts_made=attempts, max_attempts=maximum, now=now, rng=lambda: 0.0)

    def test_a_retryable_failure_with_attempts_left_is_requeued_at_a_future_time(self):
        decision = self.decide(self.RETRYABLE, 1)
        self.assertTrue(decision.requeue)
        self.assertEqual(decision.available_at, T0 + timedelta(seconds=5))
        self.assertEqual(self.decide(self.RETRYABLE, 2).available_at, T0 + timedelta(seconds=10))

    def test_the_last_attempt_is_not_retried(self):
        decision = self.decide(self.RETRYABLE, 3)
        self.assertEqual((decision.requeue, decision.available_at), (False, None))

    def test_attempts_are_bounded_for_every_limit(self):
        for maximum in range(1, policy.MAX_ATTEMPTS_LIMIT + 1):
            requeues = [n for n in range(1, maximum + 1) if self.decide(self.RETRYABLE, n, maximum).requeue]
            self.assertEqual(requeues, list(range(1, maximum)))  # never after the last attempt

    def test_a_single_allowed_attempt_is_never_retried(self):
        self.assertFalse(self.decide(self.RETRYABLE, 1, maximum=1).requeue)

    def test_a_permanent_failure_is_never_retried_even_with_attempts_left(self):
        self.assertFalse(self.decide(self.PERMANENT, 1).requeue)

    def test_recovery_requeues_within_the_limit_and_fails_beyond_it(self):
        again = policy.decide_recovery(attempts_made=1, max_attempts=3, now=T0, rng=lambda: 0.0)
        self.assertEqual((again.requeue, again.available_at), (True, T0 + timedelta(seconds=5)))
        last = policy.decide_recovery(attempts_made=3, max_attempts=3, now=T0, rng=lambda: 0.0)
        self.assertEqual((last.requeue, last.available_at), (False, None))

    def test_the_decision_depends_only_on_its_arguments(self):
        later = T0 + timedelta(hours=5)
        self.assertEqual(self.decide(self.RETRYABLE, 1, now=later).available_at, later + timedelta(seconds=5))


class FailureClassificationTests(unittest.TestCase):
    def test_a_retryable_error_is_retryable_with_its_code(self):
        failure = policy.classify_exception(TaskRetryableError())
        self.assertEqual(failure, Failure(FailureCode.TEMPORARY_FAILURE, True))

    def test_a_permanent_error_is_not_retryable(self):
        self.assertEqual(policy.classify_exception(TaskPermanentError()), Failure(FailureCode.REJECTED, False))
        self.assertEqual(policy.classify_exception(TaskPermanentError(FailureCode.INVALID_RESULT)), Failure(FailureCode.INVALID_RESULT, False))

    def test_an_invalid_payload_is_permanent(self):
        self.assertEqual(policy.classify_exception(InvalidPayloadError("values must be a list")), Failure(FailureCode.INVALID_PAYLOAD, False))

    def test_an_unexpected_exception_is_a_handler_error_retried_within_the_limit(self):
        for error in (RuntimeError("boom"), KeyError("k"), ZeroDivisionError(), QueuePolicyError("x"), Exception()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(policy.classify_exception(error), Failure(FailureCode.HANDLER_ERROR, True))

    def test_the_message_never_reaches_the_failure(self):
        secret = "password=hunter2 /home/user/secret.txt"
        for error in (RuntimeError(secret), TaskRetryableError(), InvalidPayloadError(secret), ValueError(secret)):
            failure = policy.classify_exception(error)
            self.assertNotIn("hunter2", repr(failure))
            self.assertNotIn("secret.txt", repr(failure))
            self.assertIsInstance(failure.code, FailureCode)

    def test_every_stored_code_is_from_the_closed_safe_set(self):
        self.assertEqual(
            {c.value for c in FailureCode},
            {"invalid_payload", "unknown_kind", "invalid_result", "rejected", "temporary_failure", "handler_error", "lease_expired"},
        )

    def test_a_handler_cannot_attach_an_arbitrary_code_or_message(self):
        with self.assertRaises(ValueError):
            TaskRetryableError("free text with secret")
        with self.assertRaises(ValueError):
            TaskPermanentError("free text with secret")

    def test_the_classes_do_not_carry_a_message_beyond_the_code(self):
        self.assertEqual(str(TaskRetryableError()), "temporary_failure")
        self.assertEqual(str(TaskPermanentError()), "rejected")


class CancellationRuleTests(unittest.TestCase):
    def test_queued_is_cancellable(self):
        decision = policy.check_cancellation("queued")
        self.assertEqual((decision.allowed, decision.already_cancelled, decision.reason), (True, False, "cancellable"))

    def test_running_is_explicitly_unsupported(self):
        decision = policy.check_cancellation(TaskState.RUNNING)
        self.assertEqual((decision.allowed, decision.already_cancelled, decision.reason), (False, False, "running_unsupported"))

    def test_cancelled_is_a_no_op(self):
        decision = policy.check_cancellation("cancelled")
        self.assertEqual((decision.allowed, decision.already_cancelled, decision.reason), (False, True, "already_cancelled"))

    def test_succeeded_and_failed_are_refused(self):
        for state in ("succeeded", "failed"):
            decision = policy.check_cancellation(state)
            self.assertEqual((decision.allowed, decision.already_cancelled, decision.reason), (False, False, "terminal"))


class RequestValidationTests(unittest.TestCase):
    def test_max_attempts_bounds(self):
        for good in (1, 3, 10):
            self.assertEqual(policy.validate_max_attempts(good), good)
        for bad in (0, 11, -1, 2.0, "3", None, True):
            with self.subTest(bad=bad), self.assertRaises(QueuePolicyError):
                policy.validate_max_attempts(bad)

    def test_poll_bounds_are_bounded_and_never_zero(self):
        for good in (1, 2, 60):
            self.assertEqual(policy.validate_poll_seconds(good), good)
        for bad in (0, -5, 61, 0.5, "2", None, True):
            with self.subTest(bad=bad), self.assertRaises(QueuePolicyError):
                policy.validate_poll_seconds(bad)

    def test_idempotency_keys(self):
        self.assertIsNone(policy.validate_idempotency_key(None))
        for good in ("a", "intake:run-1:extract:1", "A.b_c-d:9", "x" * 200):
            self.assertEqual(policy.validate_idempotency_key(good), good)
        for bad in ("", "x" * 201, "has space", "new\nline", "caf\u00e9", "a/b", b"bytes", 5, ["k"]):
            with self.subTest(bad=bad), self.assertRaises(QueuePolicyError):
                policy.validate_idempotency_key(bad)

    def test_instants_must_be_timezone_aware(self):
        self.assertEqual(policy.validate_aware(T0, "now"), T0)
        with self.assertRaises(QueuePolicyError):
            policy.validate_aware(datetime(2030, 1, 1), "now")
        with self.assertRaises(QueuePolicyError):
            policy.validate_aware("2030-01-01T00:00:00Z", "now")


class CanonicalJsonAndFingerprintTests(unittest.TestCase):
    def canon(self, value, limit=4096):
        return policy.canonical_json(value, max_bytes=limit, what="payload")

    def test_keys_are_sorted_and_compact(self):
        self.assertEqual(self.canon({"b": [1, 2], "a": None, "c": {"y": True, "x": "s"}}), '{"a":null,"b":[1,2],"c":{"x":"s","y":true}}')

    def test_non_ascii_is_escaped_so_the_size_is_in_bytes(self):
        self.assertEqual(self.canon({"a": "\u00e9"}), '{"a":"\\u00e9"}')

    def test_unsupported_values_are_refused(self):
        for bad in (1.5, float("nan"), {1: "a"}, {"a": {1, 2}}, {"a": b"x"}, {"a": object()}, [complex(1, 1)]):
            with self.subTest(bad=repr(bad)), self.assertRaises(QueuePolicyError):
                self.canon(bad)

    def test_depth_is_bounded(self):
        value = "leaf"
        for _ in range(policy.MAX_JSON_DEPTH - 1):
            value = [value]
        self.canon(value)  # exactly at the limit
        with self.assertRaises(QueuePolicyError):
            self.canon([value])

    def test_size_is_bounded(self):
        self.canon({"a": "x" * 4000})
        with self.assertRaises(QueuePolicyError):
            self.canon({"a": "x" * 4100})
        with self.assertRaises(QueuePolicyError):
            self.canon({"a": "xxxxxxxxxx"}, limit=10)

    def test_the_fingerprint_ignores_key_order_and_is_sha256_hex(self):
        first = policy.request_fingerprint("k", {"a": 1, "b": [1, 2]})
        self.assertEqual(first, policy.request_fingerprint("k", {"b": [1, 2], "a": 1}))
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_the_fingerprint_depends_on_kind_and_payload(self):
        base = policy.request_fingerprint("k", {"a": 1})
        self.assertNotEqual(base, policy.request_fingerprint("other", {"a": 1}))
        self.assertNotEqual(base, policy.request_fingerprint("k", {"a": 2}))
        self.assertNotEqual(base, policy.request_fingerprint("k", {"a": 1, "b": None}))
        self.assertNotEqual(policy.request_fingerprint("k", [1, 2]), policy.request_fingerprint("k", [2, 1]))

    def test_the_fingerprint_enforces_the_payload_bound(self):
        with self.assertRaises(QueuePolicyError):
            policy.request_fingerprint("k", {"a": "x" * 5000})


class AddNumbersValidationTests(unittest.TestCase):
    def valid(self, **changes):
        payload = {"values": [1, 2, 3], "label": "demo"}
        payload.update(changes)
        return payload

    def test_a_valid_payload_is_returned_as_a_clean_copy(self):
        payload = self.valid()
        clean = handlers.validate_add_numbers_payload(payload)
        self.assertEqual(clean, payload)
        self.assertIsNot(clean, payload)
        self.assertIsNot(clean["values"], payload["values"])

    def test_the_label_is_optional(self):
        self.assertEqual(handlers.validate_add_numbers_payload({"values": [5]}), {"values": [5]})

    def test_boundaries_are_accepted(self):
        handlers.validate_add_numbers_payload({"values": [1_000_000, -1_000_000] * 10, "label": "x" * 40})
        handlers.validate_add_numbers_payload({"values": [0]})

    def test_invalid_payloads_are_refused(self):
        bad_payloads = [
            None,
            [],
            "text",
            {},
            {"values": []},
            {"values": "123"},
            {"values": [1] * 21},
            {"values": [1.5]},
            {"values": [True]},
            {"values": ["1"]},
            {"values": [None]},
            {"values": [1_000_001]},
            {"values": [-1_000_001]},
            {"values": [1], "label": 5},
            {"values": [1], "label": "x" * 41},
            {"values": [1], "extra": 1},
            {"label": "no values"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=repr(payload)[:50]), self.assertRaises(InvalidPayloadError):
                handlers.validate_add_numbers_payload(payload)

    def test_error_messages_do_not_echo_the_offending_value(self):
        for payload in ({"values": ["SECRET-VALUE"]}, {"values": [1], "label": "SECRET-" * 20}, {"values": [1], "SECRET-FIELD": 1}):
            with self.assertRaises(InvalidPayloadError) as caught:
                handlers.validate_add_numbers_payload(payload)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_an_invalid_payload_error_is_a_queue_policy_error(self):
        self.assertTrue(issubclass(InvalidPayloadError, QueuePolicyError))


class AddNumbersHandlerTests(unittest.TestCase):
    def test_the_result_is_small_and_deterministic(self):
        payload = {"values": [3, -1, 10], "label": "demo"}
        results = [handlers.run_add_numbers(copy.deepcopy(payload)) for _ in range(3)]
        self.assertEqual(results[0], {"count": 3, "sum": 12, "label": "demo"})
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_without_a_label_the_result_has_none(self):
        self.assertEqual(handlers.run_add_numbers({"values": [4, 5]}), {"count": 2, "sum": 9})

    def test_the_input_is_not_modified(self):
        payload = {"values": [1, 2], "label": "x"}
        before = copy.deepcopy(payload)
        handlers.run_add_numbers(payload)
        self.assertEqual(payload, before)

    def test_the_result_satisfies_the_result_bound(self):
        largest = handlers.run_add_numbers({"values": [1_000_000] * 20, "label": "x" * 40})
        policy.canonical_json(largest, max_bytes=policy.MAX_RESULT_BYTES, what="result")


class HandlerRegistryTests(unittest.TestCase):
    def test_the_allowlist_contains_exactly_the_test_handler(self):
        self.assertEqual(set(handlers.HANDLERS), {"test.add_numbers"})
        self.assertEqual(handlers.TEST_ADD_NUMBERS, "test.add_numbers")

    def test_the_test_handler_is_identified_as_a_test_handler(self):
        handler = handlers.HANDLERS[handlers.TEST_ADD_NUMBERS]
        self.assertTrue(handler.is_test_handler)
        self.assertTrue(handler.kind.startswith("test."))

    def test_the_registry_cannot_be_modified_at_runtime(self):
        self.assertIsInstance(handlers.HANDLERS, MappingProxyType)
        with self.assertRaises(TypeError):
            handlers.HANDLERS["evil"] = handlers.HANDLERS[handlers.TEST_ADD_NUMBERS]  # type: ignore[index]
        with self.assertRaises(AttributeError):
            handlers.HANDLERS.update({})  # type: ignore[attr-defined]

    def test_unknown_kinds_have_no_handler(self):
        for kind in ("os.system", "subprocess.run", "builtins.eval", "test.add_numbers ", "TEST.ADD_NUMBERS", "", "../x", "test.add_numbers\n"):
            with self.subTest(kind=kind):
                self.assertIsNone(handlers.get_handler(kind))

    def test_non_string_kinds_have_no_handler(self):
        for kind in (None, 5, b"test.add_numbers", ["test.add_numbers"], print):
            with self.subTest(kind=repr(kind)):
                self.assertIsNone(handlers.get_handler(kind))

    def test_an_injected_registry_replaces_the_default_for_tests_only(self):
        fake = {"fake.kind": handlers.HANDLERS[handlers.TEST_ADD_NUMBERS]}
        self.assertIsNotNone(handlers.get_handler("fake.kind", fake))
        self.assertIsNone(handlers.get_handler(handlers.TEST_ADD_NUMBERS, fake))


if __name__ == "__main__":
    unittest.main()
