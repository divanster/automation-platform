"""Tests for the persistent task queue that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and
later destroys a separate ``test_<AP_DB_NAME>`` database; the mixin below refuses any other name.

STATUS: written on a development VM that has no PostgreSQL and never run there. The project owner
ran them on the local Windows computer: 95 passed, exit code 0, and all 130 PostgreSQL-required
tests passed (see docs/development/local-validation-record.md). That covers the scenarios written
here only; it does not prove that no other race exists, and no test kills a real worker process.
This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_tasks_postgres --tag requires_postgres

Time: every operation takes an explicit ``now``, so no test sleeps to make a lease expire or a
retry become due. Concurrency tests use separate connections (one per thread), bounded waits
(barrier, lock_timeout, join), daemon threads, and close each thread's connection in ``finally``.
"""

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase, TransactionTestCase, tag

from apps.tasks import errors, handlers, services, worker
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.policy import Failure, FailureCode, TaskPermanentError, TaskRetryableError, TaskState

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

BARRIER_TIMEOUT = 10
JOIN_TIMEOUT = 30
LOCK_TIMEOUT_SECONDS = 10

T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
KIND = "test.add_numbers"
OTHER_KIND = "fake.other"
HEX = "a" * 64
SECRET = "SECRET-MESSAGE-VALUE-7c1"
RETRYABLE = Failure(FailureCode.TEMPORARY_FAILURE, retryable=True)
PERMANENT = Failure(FailureCode.REJECTED, retryable=False)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def zero():
    return 0.0  # a deterministic backoff: no jitter reduction


def registry_with(**runs):
    """The real allowlist plus fake handlers that accept any payload and run the given callables."""
    extra = {
        kind.replace("_", "."): handlers.TaskHandler(kind=kind.replace("_", "."), validate=lambda p: p, run=run, is_test_handler=True)
        for kind, run in runs.items()
    }
    return {**handlers.HANDLERS, **extra}


OTHER_REGISTRY = registry_with(fake_other=lambda payload: {})


def enqueue(**changes):
    changes.setdefault("kind", KIND)
    changes.setdefault("payload", {"values": [1, 2, 3]})
    changes.setdefault("now", T0)
    return services.enqueue_task(**changes).task


def claim(now=T0, lease=60, worker_id="w-1"):
    return services.claim_next_task(worker_id=worker_id, lease_seconds=lease, now=now)


def raw_task(**changes):
    """A valid queued row created directly (bypassing the services) for constraint tests."""
    fields = {
        "kind": KIND,
        "request_fingerprint": HEX,
        "payload": {},
        "state": "queued",
        "available_at": T0,
        "max_attempts": 3,
        "created_at": T0,
    }
    fields.update(changes)
    return Task.objects.create(**fields)


def raw_running_task(**changes):
    fields = {"state": "running", "attempt_count": 1, "lease_token": uuid.uuid4(), "lease_expires_at": at(60)}
    fields.update(changes)
    return raw_task(**fields)


def snapshot(task):
    """Every stored column of the task and of its attempts, for 'nothing changed' assertions."""
    return (
        Task.objects.filter(pk=task.pk).values().get(),
        list(TaskAttempt.objects.filter(task_id=task.pk).order_by("attempt_number").values()),
    )


class DedicatedTestDatabaseMixin:
    """Refuse to run against anything but the Django-created test database (``test_*``)."""

    @classmethod
    def setUpClass(cls):
        name = connection.settings_dict["NAME"]
        if not str(name).startswith("test_"):
            raise AssertionError("PostgreSQL queue tests must run against a dedicated test database (test_*).")
        super().setUpClass()


# --- schema ---------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def test_a_valid_row_is_accepted(self):
        task = raw_task()
        self.assertEqual(Task.objects.get(pk=task.pk).state, "queued")

    def test_a_running_task_without_a_lease_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(state="running", attempt_count=1)

    def test_a_queued_task_holding_a_lease_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(lease_token=uuid.uuid4(), lease_expires_at=at(60))

    def test_more_attempts_than_allowed_are_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_running_task(attempt_count=4)

    def test_a_running_task_with_no_attempt_made_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_running_task(attempt_count=0)

    def test_the_attempt_limit_itself_must_be_between_one_and_ten(self):
        for bad in (0, 11):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                raw_task(max_attempts=bad)

    def test_a_terminal_task_needs_a_finish_time_and_a_queued_one_must_not_have_it(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(state="cancelled")
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(finished_at=at(1))

    def test_a_failed_task_needs_a_failure_code_and_others_must_not_have_one(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(state="failed", attempt_count=1, finished_at=at(1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(failure_code="handler_error")

    def test_an_unknown_state_and_a_malformed_fingerprint_are_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(state="ready")
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(request_fingerprint="not-a-hash")

    def test_an_idempotency_key_is_unique_across_kinds(self):
        raw_task(idempotency_key="k1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_task(idempotency_key="k1", kind=OTHER_KIND)

    def test_many_tasks_may_have_no_key(self):
        raw_task()
        raw_task()
        self.assertEqual(Task.objects.filter(idempotency_key__isnull=True).count(), 2)

    def test_attempt_numbers_are_unique_per_task(self):
        task = raw_running_task()
        TaskAttempt.objects.create(task=task, attempt_number=1, worker_id="w", lease_token=uuid.uuid4(), started_at=T0, outcome="succeeded", ended_at=at(1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(task=task, attempt_number=1, worker_id="w", lease_token=uuid.uuid4(), started_at=T0, outcome="succeeded", ended_at=at(1))

    def test_a_task_cannot_have_two_running_attempts(self):
        task = raw_running_task()
        TaskAttempt.objects.create(task=task, attempt_number=1, worker_id="w", lease_token=uuid.uuid4(), started_at=T0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(task=task, attempt_number=2, worker_id="w", lease_token=uuid.uuid4(), started_at=T0)

    def test_attempt_outcome_rules(self):
        task = raw_running_task()
        base = {"task": task, "attempt_number": 1, "worker_id": "w", "lease_token": uuid.uuid4(), "started_at": T0}
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(**base, outcome="running", ended_at=at(1))  # running but ended
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(**base, outcome="succeeded")  # finished without an end time
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(**base, outcome="failed", ended_at=at(1))  # failed without a code
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttempt.objects.create(**base, outcome="cancelled", ended_at=at(1))  # not an outcome

    def test_a_task_with_attempts_cannot_be_deleted(self):
        task = raw_running_task()
        TaskAttempt.objects.create(task=task, attempt_number=1, worker_id="w", lease_token=task.lease_token, started_at=T0)
        with self.assertRaises(ProtectedError):
            task.delete()


# --- enqueue ---------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class EnqueueTests(DedicatedTestDatabaseMixin, TestCase):
    def test_a_task_is_persisted_queued_and_available_now(self):
        result = services.enqueue_task(kind=KIND, payload={"values": [1, 2], "label": "x"}, now=T0)
        self.assertTrue(result.created)
        stored = Task.objects.get(pk=result.task.pk)
        self.assertEqual(stored.kind, KIND)
        self.assertEqual(stored.state, "queued")
        self.assertEqual(stored.payload, {"values": [1, 2], "label": "x"})
        self.assertEqual(stored.available_at, T0)
        self.assertEqual(stored.created_at, T0)
        self.assertEqual(stored.max_attempts, 3)
        self.assertEqual(stored.attempt_count, 0)
        self.assertIsNone(stored.lease_token)
        self.assertIsNone(stored.lease_expires_at)
        self.assertIsNone(stored.result)
        self.assertEqual(stored.failure_code, "")
        self.assertIsNone(stored.finished_at)
        self.assertIsNone(stored.idempotency_key)
        self.assertRegex(stored.request_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(TaskAttempt.objects.count(), 0)

    def test_scheduling_options_are_stored(self):
        task = services.enqueue_task(kind=KIND, payload={"values": [1]}, max_attempts=5, available_at=at(300), now=T0).task
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.max_attempts, stored.available_at), (5, at(300)))

    def test_without_a_key_every_call_creates_a_new_task(self):
        first, second = enqueue(), enqueue()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Task.objects.count(), 2)

    def test_the_same_key_and_request_returns_the_existing_task(self):
        first = services.enqueue_task(kind=KIND, payload={"values": [1, 2], "label": "x"}, idempotency_key="req-1", now=T0)
        again = services.enqueue_task(kind=KIND, payload={"label": "x", "values": [1, 2]}, idempotency_key="req-1", now=at(50))  # key order differs
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(again.task.pk, first.task.pk)
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(Task.objects.get().created_at, T0)  # the original row is untouched

    def test_the_same_key_with_a_different_payload_is_a_conflict_and_changes_nothing(self):
        services.enqueue_task(kind=KIND, payload={"values": [1, 2]}, idempotency_key="req-2", now=T0)
        before = Task.objects.values().get()
        with self.assertRaises(errors.IdempotencyConflictError):
            services.enqueue_task(kind=KIND, payload={"values": [1, 3]}, idempotency_key="req-2", now=at(5))
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(Task.objects.values().get(), before)

    def test_the_same_key_with_a_different_kind_is_a_conflict(self):
        services.enqueue_task(kind=KIND, payload={"values": [1]}, idempotency_key="req-3", now=T0, registry=OTHER_REGISTRY)
        with self.assertRaises(errors.IdempotencyConflictError):
            services.enqueue_task(kind=OTHER_KIND, payload={"values": [1]}, idempotency_key="req-3", now=T0, registry=OTHER_REGISTRY)
        self.assertEqual(Task.objects.count(), 1)

    def test_the_key_scope_is_the_whole_installation_not_one_kind(self):
        first = services.enqueue_task(kind=OTHER_KIND, payload={"values": [1]}, idempotency_key="shared", now=T0, registry=OTHER_REGISTRY).task
        with self.assertRaises(errors.IdempotencyConflictError):
            services.enqueue_task(kind=KIND, payload={"values": [1]}, idempotency_key="shared", now=T0, registry=OTHER_REGISTRY)
        self.assertEqual(Task.objects.get().pk, first.pk)

    def test_different_keys_make_different_tasks_even_for_the_same_request(self):
        first = enqueue(idempotency_key="a")
        second = enqueue(idempotency_key="b")
        self.assertNotEqual(first.pk, second.pk)

    def test_scheduling_options_are_not_part_of_the_request_identity(self):
        first = services.enqueue_task(kind=KIND, payload={"values": [1]}, idempotency_key="sched", max_attempts=2, now=T0).task
        again = services.enqueue_task(kind=KIND, payload={"values": [1]}, idempotency_key="sched", max_attempts=9, available_at=at(999), now=T0)
        self.assertFalse(again.created)
        self.assertEqual(again.task.pk, first.pk)
        stored = Task.objects.get()
        self.assertEqual((stored.max_attempts, stored.available_at), (2, T0))

    def test_a_repeated_key_returns_a_finished_task_without_restarting_it(self):
        task = enqueue(idempotency_key="done")
        claimed = claim()
        services.complete_task(task_id=task.pk, lease_token=claimed.lease_token, result={"count": 3, "sum": 6}, now=at(1))
        before = snapshot(task)
        again = services.enqueue_task(kind=KIND, payload={"values": [1, 2, 3]}, idempotency_key="done", now=at(100))
        self.assertEqual((again.task.pk, again.created, again.task.state), (task.pk, False, "succeeded"))
        self.assertEqual(snapshot(task), before)

    def test_a_refused_request_stores_nothing(self):
        for kwargs in (
            {"kind": "nope", "payload": {}},
            {"kind": KIND, "payload": {"values": []}},
            {"kind": KIND, "payload": {"values": [1]}, "idempotency_key": "bad key"},
            {"kind": KIND, "payload": {"values": [1]}, "max_attempts": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                services.enqueue_task(now=T0, **kwargs)
        self.assertEqual(Task.objects.count(), 0)

    def test_enqueue_works_inside_the_callers_transaction_and_rolls_back_with_it(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                enqueue(idempotency_key="rolled-back")
                raise RuntimeError("caller fails after enqueue")
        self.assertEqual(Task.objects.count(), 0)
        again = services.enqueue_task(kind=KIND, payload={"values": [1, 2, 3]}, idempotency_key="rolled-back", now=T0)
        self.assertTrue(again.created)


# --- claim ----------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ClaimTests(DedicatedTestDatabaseMixin, TestCase):
    def test_nothing_to_claim_returns_none(self):
        self.assertIsNone(claim())

    def test_a_claim_leases_the_task_and_records_the_attempt_in_one_step(self):
        task = enqueue(payload={"values": [4, 5]})
        claimed = claim(now=at(10), lease=90, worker_id="worker-a")
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((claimed.task_id, claimed.kind, claimed.payload, claimed.attempt_number, claimed.max_attempts), (task.pk, KIND, {"values": [4, 5]}, 1, 3))
        self.assertEqual(stored.state, "running")
        self.assertEqual(stored.attempt_count, 1)
        self.assertEqual(stored.lease_token, claimed.lease_token)
        self.assertEqual(stored.lease_expires_at, at(100))
        self.assertEqual(claimed.lease_expires_at, at(100))
        attempt = TaskAttempt.objects.get()
        self.assertEqual(attempt.pk, claimed.attempt_id)
        self.assertEqual((attempt.task_id, attempt.attempt_number, attempt.worker_id, attempt.lease_token), (task.pk, 1, "worker-a", claimed.lease_token))
        self.assertEqual((attempt.outcome, attempt.started_at, attempt.ended_at, attempt.failure_code), ("running", at(10), None, ""))

    def test_a_claimed_task_is_not_claimed_again(self):
        enqueue()
        self.assertIsNotNone(claim())
        self.assertIsNone(claim(now=at(1)))
        self.assertEqual(TaskAttempt.objects.count(), 1)

    def test_a_task_is_claimable_exactly_when_its_availability_time_is_reached(self):
        enqueue(available_at=at(10))
        self.assertIsNone(claim(now=at(10) - timedelta(microseconds=1)))
        self.assertIsNotNone(claim(now=at(10)))

    def test_eligible_tasks_are_claimed_by_availability_then_creation_time(self):
        late_created = enqueue(available_at=at(0), now=at(10), payload={"values": [1]})
        early_created = enqueue(available_at=at(0), now=at(5), payload={"values": [2]})
        earliest_available = enqueue(available_at=at(-30), now=at(20), payload={"values": [3]})
        not_yet = enqueue(available_at=at(500), now=at(1), payload={"values": [4]})
        order = [claim(now=at(100)).task_id for _ in range(3)]
        self.assertEqual(order, [earliest_available.pk, early_created.pk, late_created.pk])
        self.assertIsNone(claim(now=at(100)))
        self.assertEqual(Task.objects.get(pk=not_yet.pk).state, "queued")

    def test_every_claim_gets_a_new_lease_token_and_the_next_attempt_number(self):
        task = enqueue()
        first = claim()
        services.fail_task(task_id=task.pk, lease_token=first.lease_token, failure=RETRYABLE, now=at(1), rng=zero)
        second = claim(now=at(10))
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertEqual((first.attempt_number, second.attempt_number), (1, 2))
        self.assertEqual(Task.objects.get(pk=task.pk).lease_token, second.lease_token)

    def test_running_and_finished_tasks_are_not_claimed(self):
        running = raw_running_task()
        TaskAttempt.objects.create(task=running, attempt_number=1, worker_id="w", lease_token=running.lease_token, started_at=T0)
        raw_task(state="succeeded", attempt_count=1, finished_at=at(1))
        raw_task(state="failed", attempt_count=1, finished_at=at(1), failure_code="rejected")
        raw_task(state="cancelled", finished_at=at(1))
        self.assertIsNone(claim(now=at(5)))

    def test_a_task_with_no_attempts_left_is_never_claimed(self):
        raw_task(attempt_count=3)  # queued at the limit: possible only by bypassing the services
        self.assertIsNone(claim())

    def test_the_lease_must_be_within_bounds(self):
        enqueue()
        with self.assertRaises(Exception):
            claim(lease=1)
        self.assertEqual(Task.objects.get().state, "queued")


# --- lease renewal, completion, failure --------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class LeaseTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.task = enqueue()
        self.claimed = claim(now=T0, lease=60)

    def refreshed(self):
        return Task.objects.get(pk=self.task.pk), TaskAttempt.objects.get(pk=self.claimed.attempt_id)

    def assert_refused(self, reason, call):
        before = snapshot(self.task)
        with self.assertRaises(errors.LeaseLostError) as caught:
            call()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(snapshot(self.task), before)  # nothing was written

    # renewal

    def test_renewal_extends_the_lease_from_now(self):
        expiry = services.renew_lease(task_id=self.task.pk, lease_token=self.claimed.lease_token, lease_seconds=120, now=at(30))
        task, attempt = self.refreshed()
        self.assertEqual(expiry, at(150))
        self.assertEqual((task.lease_expires_at, task.state, task.lease_token), (at(150), "running", self.claimed.lease_token))
        self.assertEqual((attempt.outcome, attempt.ended_at), ("running", None))

    def test_a_renewed_lease_survives_what_would_have_been_its_original_expiry(self):
        services.renew_lease(task_id=self.task.pk, lease_token=self.claimed.lease_token, lease_seconds=60, now=at(50))
        self.assertEqual(services.recover_expired_leases(now=at(61)), (0, 0))
        services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={"count": 3, "sum": 6}, now=at(70))
        self.assertEqual(Task.objects.get(pk=self.task.pk).state, "succeeded")

    def test_renewal_is_refused_once_the_lease_has_expired(self):
        self.assert_refused(
            "lease_expired",
            lambda: services.renew_lease(task_id=self.task.pk, lease_token=self.claimed.lease_token, lease_seconds=60, now=at(60)),
        )

    def test_renewal_with_the_wrong_token_or_an_unknown_task_is_refused(self):
        self.assert_refused("token_mismatch", lambda: services.renew_lease(task_id=self.task.pk, lease_token=uuid.uuid4(), lease_seconds=60, now=at(1)))
        with self.assertRaises(errors.LeaseLostError) as caught:
            services.renew_lease(task_id=uuid.uuid4(), lease_token=self.claimed.lease_token, lease_seconds=60, now=at(1))
        self.assertEqual(caught.exception.reason, "not_found")

    # completion

    def test_completion_records_the_result_and_closes_the_attempt_together(self):
        services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={"count": 3, "sum": 6}, now=at(5))
        task, attempt = self.refreshed()
        self.assertEqual((task.state, task.result, task.finished_at), ("succeeded", {"count": 3, "sum": 6}, at(5)))
        self.assertEqual((task.lease_token, task.lease_expires_at, task.failure_code), (None, None, ""))
        self.assertEqual((attempt.outcome, attempt.ended_at, attempt.failure_code), ("succeeded", at(5), ""))

    def test_an_expired_lease_cannot_complete_even_before_recovery_has_run(self):
        self.assert_refused(
            "lease_expired",
            lambda: services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={"count": 1, "sum": 1}, now=at(60)),
        )
        task, attempt = self.refreshed()
        self.assertEqual((task.state, task.result), ("running", None))  # still leased; no recovery ran
        self.assertEqual(attempt.outcome, "running")

    def test_a_completion_just_inside_the_lease_is_accepted(self):
        services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={}, now=at(60) - timedelta(microseconds=1))
        self.assertEqual(Task.objects.get(pk=self.task.pk).state, "succeeded")

    def test_the_wrong_token_cannot_complete(self):
        self.assert_refused("token_mismatch", lambda: services.complete_task(task_id=self.task.pk, lease_token=uuid.uuid4(), result={}, now=at(1)))

    def test_the_old_token_cannot_complete_after_recovery_and_reclaim(self):
        self.assertEqual(services.recover_expired_leases(now=at(61), rng=zero), (1, 0))
        second = claim(now=at(70), lease=60)
        self.assertNotEqual(second.lease_token, self.claimed.lease_token)
        before = snapshot(self.task)
        with self.assertRaises(errors.LeaseLostError) as caught:
            services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={"count": 0, "sum": 0}, now=at(71))  # the lease of attempt 2 is valid
        self.assertEqual(caught.exception.reason, "token_mismatch")
        self.assertEqual(snapshot(self.task), before)
        services.complete_task(task_id=self.task.pk, lease_token=second.lease_token, result={"count": 3, "sum": 6}, now=at(72))
        task = Task.objects.get(pk=self.task.pk)
        self.assertEqual((task.state, task.result), ("succeeded", {"count": 3, "sum": 6}))
        outcomes = list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", "failure_code"))
        self.assertEqual(outcomes, [("abandoned", "lease_expired"), ("succeeded", "")])

    def test_the_old_token_cannot_complete_after_recovery_before_any_reclaim(self):
        services.recover_expired_leases(now=at(61), rng=zero)
        self.assert_refused("not_running", lambda: services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={}, now=at(62)))
        self.assertEqual(Task.objects.get(pk=self.task.pk).state, "queued")

    def test_the_old_token_cannot_record_a_failure_either(self):
        services.recover_expired_leases(now=at(61), rng=zero)
        second = claim(now=at(70))
        self.assert_refused("token_mismatch", lambda: services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=PERMANENT, now=at(71)))
        self.assertEqual(Task.objects.get(pk=self.task.pk).lease_token, second.lease_token)

    def test_completion_and_the_attempt_update_are_atomic(self):
        with mock.patch.object(Task, "save", side_effect=RuntimeError("fails after the attempt was updated")):
            with self.assertRaises(RuntimeError):
                services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={"count": 3, "sum": 6}, now=at(5))
        task, attempt = self.refreshed()
        self.assertEqual((task.state, task.result, task.finished_at), ("running", None, None))
        self.assertEqual((attempt.outcome, attempt.ended_at), ("running", None))  # the attempt update was rolled back too

    def test_a_running_task_without_its_running_attempt_is_reported_and_rolled_back(self):
        TaskAttempt.objects.filter(pk=self.claimed.attempt_id).delete()
        with self.assertRaises(errors.StoredQueueDataError):
            services.complete_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, result={}, now=at(5))
        self.assertEqual(Task.objects.get(pk=self.task.pk).state, "running")

    # failure and retry scheduling

    def test_a_retryable_failure_requeues_with_a_backoff_and_closes_the_attempt(self):
        outcome = services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=RETRYABLE, now=at(10), rng=zero)
        task, attempt = self.refreshed()
        self.assertEqual(tuple(outcome), (TaskState.QUEUED, at(15)))
        self.assertEqual((task.state, task.available_at, task.attempt_count), ("queued", at(15), 1))
        self.assertEqual((task.lease_token, task.lease_expires_at, task.failure_code, task.finished_at, task.result), (None, None, "", None, None))
        self.assertEqual((attempt.outcome, attempt.failure_code, attempt.ended_at), ("failed", "temporary_failure", at(10)))

    def test_the_retry_is_not_claimable_before_its_time_and_is_after(self):
        services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=RETRYABLE, now=at(10), rng=zero)
        self.assertIsNone(claim(now=at(15) - timedelta(microseconds=1)))
        self.assertIsNotNone(claim(now=at(15)))

    def test_the_default_jitter_keeps_the_retry_inside_the_backoff_window(self):
        outcome = services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=RETRYABLE, now=at(10))
        self.assertTrue(at(14) <= outcome.available_at <= at(15), outcome.available_at)

    def test_the_backoff_grows_and_attempts_are_exhausted_after_the_limit(self):
        task = self.task
        services.fail_task(task_id=task.pk, lease_token=self.claimed.lease_token, failure=RETRYABLE, now=at(10), rng=zero)  # attempt 1 -> +5 s
        second = claim(now=at(20))
        out2 = services.fail_task(task_id=task.pk, lease_token=second.lease_token, failure=RETRYABLE, now=at(21), rng=zero)  # attempt 2 -> +10 s
        self.assertEqual(out2.available_at, at(31))
        third = claim(now=at(40))
        self.assertEqual(third.attempt_number, 3)
        out3 = services.fail_task(task_id=task.pk, lease_token=third.lease_token, failure=RETRYABLE, now=at(41), rng=zero)  # attempt 3 = the limit
        self.assertEqual(tuple(out3), (TaskState.FAILED, None))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.finished_at, stored.attempt_count), ("failed", "temporary_failure", at(41), 3))
        self.assertEqual((stored.lease_token, stored.lease_expires_at), (None, None))
        outcomes = list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("attempt_number", "outcome", "failure_code"))
        self.assertEqual(outcomes, [(1, "failed", "temporary_failure"), (2, "failed", "temporary_failure"), (3, "failed", "temporary_failure")])
        self.assertIsNone(claim(now=at(10_000)))  # never retried again

    def test_a_permanent_failure_fails_the_task_at_once_with_attempts_left(self):
        outcome = services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=PERMANENT, now=at(3))
        task, attempt = self.refreshed()
        self.assertEqual(tuple(outcome), (TaskState.FAILED, None))
        self.assertEqual((task.state, task.failure_code, task.attempt_count, task.finished_at), ("failed", "rejected", 1, at(3)))
        self.assertEqual((attempt.outcome, attempt.failure_code), ("failed", "rejected"))
        self.assertIsNone(claim(now=at(10_000)))

    def test_with_a_single_allowed_attempt_a_retryable_failure_is_final(self):
        services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=PERMANENT, now=at(1))  # finish the task from setUp
        single = enqueue(max_attempts=1, payload={"values": [9]}, now=at(2))
        claimed = claim(now=at(2))
        self.assertEqual(claimed.task_id, single.pk)
        outcome = services.fail_task(task_id=single.pk, lease_token=claimed.lease_token, failure=RETRYABLE, now=at(3))
        self.assertEqual(tuple(outcome), (TaskState.FAILED, None))
        stored = Task.objects.get(pk=single.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.attempt_count), ("failed", "temporary_failure", 1))

    def test_an_expired_lease_cannot_record_a_failure(self):
        self.assert_refused("lease_expired", lambda: services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=RETRYABLE, now=at(60)))

    def test_failure_and_the_attempt_update_are_atomic(self):
        with mock.patch.object(Task, "save", side_effect=RuntimeError("fails after the attempt was updated")):
            with self.assertRaises(RuntimeError):
                services.fail_task(task_id=self.task.pk, lease_token=self.claimed.lease_token, failure=PERMANENT, now=at(5))
        task, attempt = self.refreshed()
        self.assertEqual((task.state, task.failure_code), ("running", ""))
        self.assertEqual((attempt.outcome, attempt.failure_code), ("running", ""))


# --- recovery ----------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RecoveryTests(DedicatedTestDatabaseMixin, TestCase):
    def leased(self, *, lease=60, now=T0, **enqueue_options):
        task = enqueue(**enqueue_options)
        return task, claim(now=now, lease=lease)

    def test_an_expired_lease_is_recovered_by_requeueing_while_attempts_remain(self):
        task, claimed = self.leased()
        result = services.recover_expired_leases(now=at(61), rng=zero)
        self.assertEqual(tuple(result), (1, 0))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.available_at, stored.attempt_count), ("queued", at(66), 1))
        self.assertEqual((stored.lease_token, stored.lease_expires_at, stored.failure_code, stored.finished_at), (None, None, "", None))
        attempt = TaskAttempt.objects.get(pk=claimed.attempt_id)
        self.assertEqual((attempt.outcome, attempt.failure_code, attempt.ended_at), ("abandoned", "lease_expired", at(61)))

    def test_a_recovered_task_can_be_claimed_again_under_a_new_token(self):
        task, first = self.leased()
        services.recover_expired_leases(now=at(61), rng=zero)
        self.assertIsNone(claim(now=at(65)))
        second = claim(now=at(66))
        self.assertEqual((second.task_id, second.attempt_number), (task.pk, 2))
        self.assertNotEqual(second.lease_token, first.lease_token)

    def test_recovery_marks_the_task_failed_when_attempts_are_exhausted(self):
        task, _ = self.leased(max_attempts=1)
        result = services.recover_expired_leases(now=at(100))
        self.assertEqual(tuple(result), (0, 1))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.finished_at, stored.attempt_count), ("failed", "lease_expired", at(100), 1))
        self.assertEqual((stored.lease_token, stored.lease_expires_at), (None, None))
        self.assertEqual(TaskAttempt.objects.get().outcome, "abandoned")
        self.assertIsNone(claim(now=at(100_000)))

    def test_repeated_lease_expiry_is_bounded_by_the_attempt_limit(self):
        task = enqueue(max_attempts=3)
        clock = 0
        for attempt_number in (1, 2, 3):
            claimed = claim(now=at(clock))
            self.assertEqual(claimed.attempt_number, attempt_number)
            clock += 61
            services.recover_expired_leases(now=at(clock), rng=zero)
            clock += 600  # past any backoff
        self.assertEqual(Task.objects.get(pk=task.pk).state, "failed")
        self.assertIsNone(claim(now=at(clock)))
        self.assertEqual(TaskAttempt.objects.filter(task=task, outcome="abandoned").count(), 3)

    def test_a_lease_is_expired_exactly_at_its_expiry_instant(self):
        task, _ = self.leased()
        self.assertEqual(tuple(services.recover_expired_leases(now=at(60) - timedelta(microseconds=1))), (0, 0))
        self.assertEqual(Task.objects.get(pk=task.pk).state, "running")
        self.assertEqual(tuple(services.recover_expired_leases(now=at(60), rng=zero)), (1, 0))

    def test_queued_and_finished_tasks_are_left_alone(self):
        finished = enqueue(payload={"values": [7]})
        claimed = claim(now=at(1))
        services.complete_task(task_id=finished.pk, lease_token=claimed.lease_token, result={}, now=at(2))
        waiting = enqueue(payload={"values": [8]}, now=at(3))
        before = [snapshot(t) for t in (finished, waiting)]
        self.assertEqual(tuple(services.recover_expired_leases(now=at(10_000))), (0, 0))
        self.assertEqual([snapshot(t) for t in (finished, waiting)], before)

    def test_recovery_handles_at_most_the_limit_oldest_expiries_first(self):
        tasks = []
        for i in range(3):
            tasks.append(enqueue(payload={"values": [i]}, now=at(i)))
        claims = [claim(now=at(i), lease=60 + i * 10) for i in range(3)]  # expiries 60, 70, 80 (+ claim offsets)
        result = services.recover_expired_leases(now=at(1000), limit=2, rng=zero)
        self.assertEqual(tuple(result), (2, 0))
        states = {t.pk: Task.objects.get(pk=t.pk).state for t in tasks}
        self.assertEqual(sorted(states.values()), ["queued", "queued", "running"])
        self.assertEqual(states[claims[2].task_id], "running")  # the latest expiry waits for the next call
        self.assertEqual(tuple(services.recover_expired_leases(now=at(1000), limit=2, rng=zero)), (1, 0))

    def test_recovery_is_idempotent(self):
        self.leased()
        self.assertEqual(tuple(services.recover_expired_leases(now=at(61), rng=zero)), (1, 0))
        before = snapshot(Task.objects.get())
        self.assertEqual(tuple(services.recover_expired_leases(now=at(61), rng=zero)), (0, 0))
        self.assertEqual(snapshot(Task.objects.get()), before)

    def test_recovery_is_atomic_with_the_attempt_update(self):
        task, claimed = self.leased()
        with mock.patch.object(Task, "save", side_effect=RuntimeError("fails after the attempt was closed")):
            with self.assertRaises(RuntimeError):
                services.recover_expired_leases(now=at(61))
        self.assertEqual(Task.objects.get(pk=task.pk).state, "running")
        self.assertEqual(TaskAttempt.objects.get(pk=claimed.attempt_id).outcome, "running")

    def test_a_late_worker_is_fenced_out_after_recovery(self):
        task, claimed = self.leased()
        services.recover_expired_leases(now=at(61), rng=zero)
        with self.assertRaises(errors.LeaseLostError):
            services.complete_task(task_id=task.pk, lease_token=claimed.lease_token, result={"count": 9, "sum": 9}, now=at(62))
        self.assertIsNone(Task.objects.get(pk=task.pk).result)


# --- cancellation and terminal-state protection -----------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class CancellationTests(DedicatedTestDatabaseMixin, TestCase):
    def test_a_queued_task_is_cancelled_and_never_claimed(self):
        task = enqueue()
        self.assertEqual(services.cancel_task(task.pk, now=at(5)), TaskState.CANCELLED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.finished_at, stored.attempt_count), ("cancelled", at(5), 0))
        self.assertIsNone(claim(now=at(100)))
        self.assertEqual(TaskAttempt.objects.count(), 0)

    def test_a_task_waiting_for_a_retry_can_be_cancelled(self):
        task = enqueue()
        claimed = claim()
        services.fail_task(task_id=task.pk, lease_token=claimed.lease_token, failure=RETRYABLE, now=at(1), rng=zero)
        services.cancel_task(task.pk, now=at(2))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("cancelled", 1))
        self.assertIsNone(claim(now=at(100)))

    def test_cancelling_a_running_task_is_refused_and_changes_nothing(self):
        task = enqueue()
        claim()
        before = snapshot(task)
        with self.assertRaises(errors.CancellationNotSupportedError):
            services.cancel_task(task.pk, now=at(5))
        self.assertEqual(snapshot(task), before)

    def test_a_running_task_still_completes_after_a_refused_cancellation(self):
        task = enqueue()
        claimed = claim()
        with self.assertRaises(errors.CancellationNotSupportedError):
            services.cancel_task(task.pk, now=at(5))
        services.complete_task(task_id=task.pk, lease_token=claimed.lease_token, result={}, now=at(6))
        self.assertEqual(Task.objects.get(pk=task.pk).state, "succeeded")

    def test_cancelling_a_finished_task_is_refused(self):
        succeeded, failed = enqueue(payload={"values": [1]}), enqueue(payload={"values": [2]})
        first = claim()
        services.complete_task(task_id=first.task_id, lease_token=first.lease_token, result={}, now=at(1))
        second = claim(now=at(2))
        services.fail_task(task_id=second.task_id, lease_token=second.lease_token, failure=PERMANENT, now=at(3))
        for task in (succeeded, failed):
            before = snapshot(task)
            with self.subTest(task=task.pk), self.assertRaises(errors.TaskTerminalError):
                services.cancel_task(task.pk, now=at(9))
            self.assertEqual(snapshot(task), before)

    def test_cancelling_a_cancelled_task_changes_nothing(self):
        task = enqueue()
        services.cancel_task(task.pk, now=at(5))
        before = snapshot(task)
        self.assertEqual(services.cancel_task(task.pk, now=at(50)), TaskState.CANCELLED)
        self.assertEqual(snapshot(task), before)  # finished_at stays at the first cancellation

    def test_an_unknown_task_is_reported(self):
        with self.assertRaises(errors.TaskNotFoundError):
            services.cancel_task(uuid.uuid4())


@tag("requires_postgres")
@requires_postgres
class TerminalStateProtectionTests(DedicatedTestDatabaseMixin, TestCase):
    """No operation changes a finished task, and nothing restarts it."""

    def finished_tasks(self):
        succeeded = enqueue(payload={"values": [1]}, idempotency_key="t-succeeded")
        a = claim()
        services.complete_task(task_id=succeeded.pk, lease_token=a.lease_token, result={"count": 1, "sum": 1}, now=at(1))
        failed = enqueue(payload={"values": [2]}, idempotency_key="t-failed")
        b = claim(now=at(2))
        services.fail_task(task_id=failed.pk, lease_token=b.lease_token, failure=PERMANENT, now=at(3))
        cancelled = enqueue(payload={"values": [3]}, idempotency_key="t-cancelled")
        services.cancel_task(cancelled.pk, now=at(4))
        return {"succeeded": (succeeded, a.lease_token), "failed": (failed, b.lease_token), "cancelled": (cancelled, uuid.uuid4())}

    def test_no_operation_changes_a_finished_task(self):
        for label, (task, token) in self.finished_tasks().items():
            before = snapshot(task)
            with self.subTest(label=label):
                self.assertIsNone(claim(now=at(10_000)))
                self.assertEqual(tuple(services.recover_expired_leases(now=at(10_000))), (0, 0))
                for call in (
                    lambda: services.renew_lease(task_id=task.pk, lease_token=token, lease_seconds=60, now=at(10)),
                    lambda: services.complete_task(task_id=task.pk, lease_token=token, result={"count": 9, "sum": 9}, now=at(10)),
                    lambda: services.fail_task(task_id=task.pk, lease_token=token, failure=RETRYABLE, now=at(10)),
                ):
                    with self.assertRaises(errors.LeaseLostError) as caught:
                        call()
                    self.assertEqual(caught.exception.reason, "not_running")
                self.assertEqual(snapshot(task), before)

    def test_a_repeated_idempotency_key_returns_the_finished_task_and_does_not_restart_it(self):
        payloads = {"succeeded": [1], "failed": [2], "cancelled": [3]}
        for label, (task, _) in self.finished_tasks().items():
            before = snapshot(task)
            with self.subTest(label=label):
                again = services.enqueue_task(kind=KIND, payload={"values": payloads[label]}, idempotency_key=f"t-{label}", now=at(500))
                self.assertEqual((again.task.pk, again.created, again.task.state), (task.pk, False, label))
                self.assertEqual(snapshot(task), before)

    def test_cancelling_succeeded_or_failed_tasks_is_refused(self):
        tasks = self.finished_tasks()
        for label in ("succeeded", "failed"):
            with self.subTest(label=label), self.assertRaises(errors.TaskTerminalError):
                services.cancel_task(tasks[label][0].pk)


# --- concurrency (separate connections) -----------------------------------------------------------------


class ThreadedMixin:
    """Run callables on separate threads, each with its own connection, with bounded waits."""

    def run_threads(self, targets):
        results = [None] * len(targets)
        barrier = threading.Barrier(len(targets))

        def work(index, target):
            try:
                with connection.cursor() as cursor:  # this thread's own connection
                    cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'")  # a stuck lock becomes an error
                barrier.wait(timeout=BARRIER_TIMEOUT)
                results[index] = ("ok", target())
            except Exception as error:  # recorded and asserted on by the caller
                results[index] = ("error", error)
            finally:
                connection.close()  # always release the connection, success or failure

        # Daemon threads: a thread that somehow never returns cannot keep the test process alive.
        threads = [threading.Thread(target=work, args=(i, t), daemon=True) for i, t in enumerate(targets)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
        stuck = [thread for thread in threads if thread.is_alive()]
        self.assertEqual(stuck, [], "a worker thread did not finish within the bound (possible deadlock)")
        self.assertNotIn(None, results)
        return results


@tag("requires_postgres")
@requires_postgres
class ClaimConcurrencyTests(ThreadedMixin, DedicatedTestDatabaseMixin, TransactionTestCase):
    """Committed data visible across connections, hence TransactionTestCase."""

    def test_competing_workers_claim_a_single_task_only_once(self):
        task = enqueue()
        results = self.run_threads([lambda i=i: claim(now=T0, worker_id=f"w-{i}") for i in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        winners = [value for _, value in results if value is not None]
        self.assertEqual(len(winners), 1, results)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.lease_token), ("running", 1, winners[0].lease_token))
        attempts = list(TaskAttempt.objects.filter(task=task))
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts[0].pk, attempts[0].outcome), (winners[0].attempt_id, "running"))

    def test_competing_workers_never_share_tasks(self):
        tasks = [enqueue(payload={"values": [i]}) for i in range(3)]
        results = self.run_threads([lambda i=i: claim(now=T0, worker_id=f"w-{i}") for i in range(8)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        winners = [value for _, value in results if value is not None]
        self.assertEqual(sorted(w.task_id for w in winners), sorted(t.pk for t in tasks))  # each task claimed once, none twice
        self.assertEqual(len({w.lease_token for w in winners}), 3)
        self.assertEqual(Task.objects.filter(state="running").count(), 3)
        self.assertEqual(TaskAttempt.objects.count(), 3)
        self.assertEqual(TaskAttempt.objects.filter(outcome="running").count(), 3)

    def test_a_row_locked_by_another_transaction_is_skipped_not_waited_for(self):
        task = enqueue()
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=task.pk)  # this connection holds the row lock
            results = self.run_threads([lambda: claim(now=T0, worker_id="other")])
            self.assertEqual(results[0], ("ok", None))  # skipped promptly (SKIP LOCKED), not blocked
        self.assertIsNotNone(claim(now=T0, worker_id="after"))  # claimable once the lock is gone

    def test_two_completions_with_one_token_have_exactly_one_winner(self):
        task = enqueue()
        claimed = claim()
        results = self.run_threads(
            [lambda i=i: services.complete_task(task_id=task.pk, lease_token=claimed.lease_token, result={"count": i, "sum": i}, now=at(1)) for i in range(2)]
        )
        winners = [r for r in results if r[0] == "ok"]
        losers = [r[1] for r in results if r[0] == "error"]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(all(isinstance(e, errors.LeaseLostError) and e.reason == "not_running" for e in losers), losers)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual(stored.state, "succeeded")
        self.assertIn(stored.result["count"], (0, 1))
        self.assertEqual(TaskAttempt.objects.filter(task=task, outcome="succeeded").count(), 1)

    def test_concurrent_recovery_handles_each_expired_task_once(self):
        tasks = [enqueue(payload={"values": [i]}) for i in range(4)]
        for _ in tasks:
            claim(now=T0)
        results = self.run_threads([lambda: services.recover_expired_leases(now=at(1000), rng=zero) for _ in range(3)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(r.requeued + r.failed for _, r in results), 4)
        self.assertEqual(Task.objects.filter(state="queued").count(), 4)
        self.assertEqual(TaskAttempt.objects.filter(outcome="abandoned").count(), 4)
        self.assertEqual(TaskAttempt.objects.filter(outcome="running").count(), 0)

    def test_cancelling_and_claiming_a_queued_task_never_both_win(self):
        task = enqueue()
        results = self.run_threads([lambda: services.cancel_task(task.pk, now=at(1)), lambda: claim(now=T0)])
        cancel_result, claim_result = results
        stored = Task.objects.get(pk=task.pk)
        if cancel_result[0] == "ok":
            self.assertEqual(claim_result, ("ok", None))
            self.assertEqual((stored.state, stored.attempt_count), ("cancelled", 0))
            self.assertEqual(TaskAttempt.objects.count(), 0)
        else:
            self.assertIsInstance(cancel_result[1], errors.CancellationNotSupportedError)
            self.assertEqual(claim_result[0], "ok")
            self.assertIsNotNone(claim_result[1])
            self.assertEqual((stored.state, stored.attempt_count), ("running", 1))

    def test_concurrent_enqueues_of_one_key_create_one_task(self):
        results = self.run_threads([lambda: services.enqueue_task(kind=KIND, payload={"values": [1, 2]}, idempotency_key="race", now=T0) for _ in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(1 for _, r in results if r.created), 1)
        self.assertEqual(len({r.task.pk for _, r in results}), 1)
        self.assertEqual(Task.objects.count(), 1)

    def test_concurrent_enqueues_of_one_key_with_different_payloads_leave_one_consistent_task(self):
        payloads = [[1], [1], [2], [2]]
        results = self.run_threads([lambda p=p: services.enqueue_task(kind=KIND, payload={"values": p}, idempotency_key="race-2", now=T0) for p in payloads])
        stored = Task.objects.get()
        self.assertEqual(sum(1 for kind, r in results if kind == "ok" and r.created), 1)
        for (kind, value), values in zip(results, payloads):
            if kind == "ok":
                self.assertEqual(value.task.pk, stored.pk)
                self.assertEqual(values, stored.payload["values"])  # only the same request can see the existing task
            else:
                self.assertIsInstance(value, errors.IdempotencyConflictError)
                self.assertNotEqual(values, stored.payload["values"])


# --- the worker against the database -----------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class WorkerProcessTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """``process_one`` end to end, on committed data (no wrapping transaction), with an injected clock."""

    def process(self, now, registry=None):
        return worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: now, registry=registry)

    def test_the_test_handler_runs_and_its_result_is_stored(self):
        task = enqueue(payload={"values": [3, -1, 10], "label": "demo"})
        outcome = self.process(T0)
        self.assertEqual((outcome.status, outcome.task_id), (worker.ProcessStatus.SUCCEEDED, task.pk))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.result, stored.finished_at), ("succeeded", {"count": 3, "sum": 12, "label": "demo"}, T0))
        self.assertEqual((stored.lease_token, stored.lease_expires_at), (None, None))
        attempt = TaskAttempt.objects.get(task=task)
        self.assertEqual((attempt.outcome, attempt.attempt_number, attempt.worker_id), ("succeeded", 1, "w-test"))

    def test_an_empty_queue_is_idle(self):
        self.assertEqual(self.process(T0).status, worker.ProcessStatus.IDLE)

    def test_the_claim_is_committed_and_no_transaction_is_open_while_the_handler_runs(self):
        seen = {}

        def run(payload):
            seen["in_atomic_block"] = connection.in_atomic_block

            def read_from_another_connection():
                try:
                    row = Task.objects.get(pk=task.pk)
                    seen["other"] = (row.state, row.lease_token is not None, row.attempt_count, TaskAttempt.objects.filter(task=row, outcome="running").count())
                finally:
                    connection.close()

            thread = threading.Thread(target=read_from_another_connection, daemon=True)
            thread.start()
            thread.join(timeout=JOIN_TIMEOUT)
            return {"ok": 1}

        registry = registry_with(fake_kind=run)
        task = enqueue(kind="fake.kind", payload={}, registry=registry)
        self.assertEqual(self.process(T0, registry).status, worker.ProcessStatus.SUCCEEDED)
        self.assertFalse(seen["in_atomic_block"])
        self.assertEqual(seen["other"], ("running", True, 1, 1))  # visible to another connection => committed before the handler ran

    def test_a_permanent_failure_is_stored_as_a_safe_code(self):
        def run(payload):
            raise TaskPermanentError()

        registry = registry_with(fake_kind=run)
        task = enqueue(kind="fake.kind", payload={}, registry=registry)
        self.assertEqual(self.process(T0, registry).status, worker.ProcessStatus.FAILED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.attempt_count), ("failed", "rejected", 1))

    def test_a_retryable_failure_is_retried_later_and_can_then_succeed(self):
        calls = []

        def run(payload):
            calls.append(1)
            if len(calls) == 1:
                raise TaskRetryableError()
            return {"calls": len(calls)}

        registry = registry_with(fake_kind=run)
        task = enqueue(kind="fake.kind", payload={}, registry=registry)
        self.assertEqual(self.process(T0, registry).status, worker.ProcessStatus.REQUEUED)
        self.assertEqual(self.process(T0 + timedelta(seconds=1), registry).status, worker.ProcessStatus.IDLE)  # backoff not over
        self.assertEqual(self.process(T0 + timedelta(seconds=10), registry).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.result, stored.attempt_count), ("succeeded", {"calls": 2}, 2))
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", "failure_code")), [("failed", "temporary_failure"), ("succeeded", "")])

    def test_a_handler_that_always_fails_is_tried_a_bounded_number_of_times(self):
        calls = []

        def run(payload):
            calls.append(1)
            raise RuntimeError(f"internal detail {SECRET}")

        registry = registry_with(fake_kind=run)
        task = enqueue(kind="fake.kind", payload={}, registry=registry, max_attempts=3)
        statuses = [self.process(T0 + timedelta(seconds=1000 * i), registry).status for i in range(5)]
        self.assertEqual(
            statuses,
            [worker.ProcessStatus.REQUEUED, worker.ProcessStatus.REQUEUED, worker.ProcessStatus.FAILED, worker.ProcessStatus.IDLE, worker.ProcessStatus.IDLE],
        )
        self.assertEqual(len(calls), 3)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "handler_error"))
        everything = repr(list(Task.objects.values())) + repr(list(TaskAttempt.objects.values()))
        self.assertNotIn(SECRET, everything)  # the exception message is never stored
        self.assertNotIn("Traceback", everything)

    def test_a_worker_that_outlives_its_lease_cannot_overwrite_the_result(self):
        clock = [T0]
        calls = []

        def run(payload):
            calls.append(1)
            if len(calls) == 1:
                clock[0] = T0 + timedelta(seconds=120)  # the 60 s lease ends while this handler is still "running"
                return {"run": "stale"}
            return {"run": "fresh"}

        registry = registry_with(fake_kind=run)
        task = enqueue(kind="fake.kind", payload={}, registry=registry)
        stale = worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: clock[0], registry=registry)
        self.assertEqual(stale.status, worker.ProcessStatus.LEASE_LOST)
        mid = Task.objects.get(pk=task.pk)
        self.assertEqual((mid.state, mid.result), ("running", None))  # the late result was not written
        clock[0] = T0 + timedelta(seconds=121)
        self.assertEqual(worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: clock[0], registry=registry).status, worker.ProcessStatus.IDLE)  # recovered; backoff pending
        clock[0] = T0 + timedelta(seconds=200)
        self.assertEqual(worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: clock[0], registry=registry).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.result, stored.attempt_count), ("succeeded", {"run": "fresh"}, 2))
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", "failure_code")), [("abandoned", "lease_expired"), ("succeeded", "")])
        self.assertEqual(len(calls), 2)  # the handler ran twice: at-least-once, not exactly-once

    def test_a_crashed_worker_is_recovered_after_its_lease_and_the_task_completes_once(self):
        task = enqueue(payload={"values": [5, 6]})
        crashed = claim(now=T0, lease=60, worker_id="crashed")  # claimed, then the process died: nothing else happens
        self.assertIsNotNone(crashed)
        self.assertEqual(self.process(T0 + timedelta(seconds=30)).status, worker.ProcessStatus.IDLE)  # lease still held
        self.assertEqual(self.process(T0 + timedelta(seconds=61)).status, worker.ProcessStatus.IDLE)  # recovered, backoff pending
        self.assertEqual(self.process(T0 + timedelta(seconds=100)).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.result, stored.attempt_count), ("succeeded", {"count": 2, "sum": 11}, 2))
        self.assertEqual(
            list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("worker_id", "outcome")),
            [("crashed", "abandoned"), ("w-test", "succeeded")],
        )

    def test_an_unknown_kind_in_storage_fails_permanently_without_running_anything(self):
        raw = raw_task(kind="os.system", payload={})
        self.assertEqual(self.process(T0).status, worker.ProcessStatus.FAILED)
        stored = Task.objects.get(pk=raw.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "unknown_kind"))

    def test_a_stored_payload_that_no_longer_validates_fails_permanently(self):
        raw = raw_task(kind=KIND, payload={"values": "tampered"})
        self.assertEqual(self.process(T0).status, worker.ProcessStatus.FAILED)
        self.assertEqual(Task.objects.get(pk=raw.pk).failure_code, "invalid_payload")
