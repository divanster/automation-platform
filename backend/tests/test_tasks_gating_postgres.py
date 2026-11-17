"""Tests for the claim filter and the kind gate that REQUIRE a real PostgreSQL server (MOD-1b S2).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; the mixin it reuses refuses any other name.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project
owner runs them locally they are NOT RUN. Their DB-free counterparts are in test_tasks_gating.py (these pass in the VM).

What they cover: the filtered claim is one atomic ``SELECT ... FOR UPDATE SKIP LOCKED`` statement with the kind
condition inside it; tasks of other kinds (an unknown kind included) are never locked, claimed, counted or
changed; ordering, lease token, attempt numbering, retry and lease recovery behave exactly as without a filter;
concurrent workers with different allowed sets never take each other's tasks or the same task twice; and the
worker with an installed kind gate leaves held tasks queued. The existing suites (test_tasks_postgres.py,
test_tasks_recovery_postgres.py) cover the unrestricted behaviour and the process-level crash recovery and must
still pass UNCHANGED; no test here kills a real process.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_tasks_gating_postgres --tag requires_postgres

This file is ASCII only.
"""

from datetime import timedelta

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext

from apps.tasks import services, worker
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.policy import Failure, FailureCode, TaskRetryableError
from tests import test_tasks_postgres as base

requires_postgres = base.requires_postgres

T0 = base.T0
at = base.at
KIND_A = "fake.alpha"
KIND_B = "fake.beta"
KIND_C = "fake.gamma"
UNKNOWN = "os.system"  # a kind no allowlist has; only raw rows can carry it


def registry():
    """The real allowlist plus three fake kinds that accept any payload."""
    return base.registry_with(fake_alpha=lambda p: {"kind": "a"}, fake_beta=lambda p: {"kind": "b"}, fake_gamma=lambda p: {"kind": "c"})


REGISTRY = registry()


def put(kind, *, seconds=0, **changes):
    """A queued task of ``kind`` created through the service, available at ``T0 + seconds``."""
    changes.setdefault("payload", {})
    changes.setdefault("now", at(seconds))
    changes.setdefault("available_at", at(seconds))
    return services.enqueue_task(kind=kind, registry=REGISTRY, **changes).task


def claim(kinds, *, now=T0, worker_id="w-1", lease=60):
    return services.claim_next_task(worker_id=worker_id, lease_seconds=lease, now=now, allowed_kinds=kinds)


def untouched(task):
    """The stored task is still queued and was never claimed."""
    row = Task.objects.get(pk=task.pk)
    return (row.state, row.attempt_count, row.lease_token, row.lease_expires_at, TaskAttempt.objects.filter(task=row).count()) == ("queued", 0, None, None, 0)


# --- the filtered claim on one connection -----------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class FilteredClaimTests(base.DedicatedTestDatabaseMixin, TestCase):
    def test_no_filter_claims_any_kind_exactly_as_before(self):
        first, second = put(KIND_A, seconds=0), put(KIND_B, seconds=1)
        self.assertEqual(claim(None, now=at(5)).task_id, first.pk)
        self.assertEqual(claim(None, now=at(5)).task_id, second.pk)
        self.assertIsNone(claim(None, now=at(5)))

    def test_leaving_the_argument_out_is_the_same_as_none(self):
        task = put(KIND_B)
        self.assertEqual(services.claim_next_task(worker_id="w", lease_seconds=60, now=T0).task_id, task.pk)

    def test_an_empty_filter_claims_nothing_runs_no_query_and_changes_nothing(self):
        tasks = [put(KIND_A), put(KIND_B)]
        before = [base.snapshot(t) for t in tasks]
        for empty in (set(), frozenset(), [], ()):
            with self.subTest(empty=repr(empty)), self.assertNumQueries(0):
                self.assertIsNone(claim(empty))
        self.assertEqual([base.snapshot(t) for t in tasks], before)
        self.assertEqual(TaskAttempt.objects.count(), 0)

    def test_a_filter_claims_only_the_allowed_kinds_even_when_another_kind_is_older(self):
        older = put(KIND_A, seconds=0)
        wanted = put(KIND_B, seconds=10)
        claimed = claim({KIND_B}, now=at(20))
        self.assertEqual((claimed.task_id, claimed.kind), (wanted.pk, KIND_B))
        self.assertTrue(untouched(older))
        self.assertIsNone(claim({KIND_B}, now=at(20)))  # nothing else of that kind
        self.assertTrue(untouched(older))

    def test_ordering_inside_the_allowed_set_is_unchanged(self):
        by_time = [put(KIND_B, seconds=5), put(KIND_A, seconds=1), put(KIND_C, seconds=3), put(KIND_A, seconds=2)]
        expected = [by_time[1].pk, by_time[3].pk, by_time[0].pk]  # A@1, A@2, B@5; the C task is outside the set
        claimed = [claim({KIND_A, KIND_B}, now=at(10)).task_id for _ in range(3)]
        self.assertEqual(claimed, expected)
        self.assertIsNone(claim({KIND_A, KIND_B}, now=at(10)))
        self.assertTrue(untouched(by_time[2]))

    def test_the_tie_break_on_creation_time_and_id_is_unchanged(self):
        # Same available_at: created_at then id decide, exactly as without a filter.
        late = put(KIND_A, seconds=0, now=at(2), available_at=at(0))
        early = put(KIND_B, seconds=0, now=at(1), available_at=at(0))
        self.assertEqual([claim({KIND_A, KIND_B}, now=at(10)).task_id for _ in range(2)], [early.pk, late.pk])

    def test_a_kind_that_nothing_in_the_queue_has_claims_nothing(self):
        task = put(KIND_A)
        self.assertIsNone(claim({KIND_B, KIND_C}))
        self.assertTrue(untouched(task))

    def test_a_task_of_an_unknown_kind_stays_queued_when_the_filter_excludes_it(self):
        unknown = base.raw_task(kind=UNKNOWN, payload={})
        known = put(KIND_A, seconds=5)
        self.assertEqual(claim({KIND_A}, now=at(10)).task_id, known.pk)
        self.assertIsNone(claim({KIND_A}, now=at(10)))
        self.assertTrue(untouched(unknown))
        self.assertEqual(Task.objects.get(pk=unknown.pk).failure_code, "")

    def test_an_unrestricted_claim_still_takes_an_unknown_kind_as_before(self):
        unknown = base.raw_task(kind=UNKNOWN, payload={})
        self.assertEqual(claim(None).task_id, unknown.pk)

    def test_a_filter_that_names_the_unknown_kind_can_claim_it_the_service_does_not_judge_kinds(self):
        unknown = base.raw_task(kind=UNKNOWN, payload={})
        self.assertEqual(claim({UNKNOWN}).task_id, unknown.pk)

    def test_the_claim_itself_is_unchanged_lease_token_attempt_and_state(self):
        task = put(KIND_A)
        claimed = claim({KIND_A}, now=at(1), lease=90, worker_id="w-9")
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.lease_token, stored.lease_expires_at), ("running", 1, claimed.lease_token, at(91)))
        attempt = TaskAttempt.objects.get(task=task)
        self.assertEqual((attempt.pk, attempt.attempt_number, attempt.worker_id, attempt.lease_token, attempt.outcome, attempt.started_at), (claimed.attempt_id, 1, "w-9", claimed.lease_token, "running", at(1)))
        self.assertEqual((claimed.kind, claimed.max_attempts, claimed.attempt_number, claimed.lease_expires_at), (KIND_A, 3, 1, at(91)))

    def test_ineligible_tasks_of_an_allowed_kind_are_still_not_claimed(self):
        future = put(KIND_A, seconds=100)
        exhausted = base.raw_task(kind=KIND_A, payload={}, attempt_count=3, max_attempts=3, state="queued", available_at=T0)
        cancelled = put(KIND_A)
        services.cancel_task(cancelled.pk, now=T0)
        running = put(KIND_A)
        self.assertEqual(claim({KIND_A}).task_id, running.pk)
        self.assertIsNone(claim({KIND_A}, now=at(1)))
        self.assertTrue(untouched(future))
        self.assertEqual(Task.objects.get(pk=exhausted.pk).attempt_count, 3)
        self.assertEqual(Task.objects.get(pk=cancelled.pk).state, "cancelled")

    def test_one_statement_carries_the_lock_the_skip_and_the_kind_condition(self):
        put(KIND_A)
        put(KIND_B)
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNotNone(claim({KIND_B, KIND_A}))
        selects = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith("SELECT") and "tasks_task" in q["sql"]]
        self.assertEqual(len(selects), 1, selects)
        sql = selects[0].upper()
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("SKIP LOCKED", sql)
        self.assertIn('"KIND" IN', sql)
        self.assertIn("ORDER BY", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertLess(sql.index('"KIND" IN'), sql.index("ORDER BY"))  # a WHERE condition, not a later step

    def test_the_unfiltered_statement_has_no_kind_condition(self):
        put(KIND_A)
        with CaptureQueriesContext(connection) as queries:
            claim(None)
        selects = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith("SELECT") and "tasks_task" in q["sql"]]
        self.assertEqual(len(selects), 1)
        self.assertNotIn('"KIND" IN', selects[0].upper())
        self.assertIn("SKIP LOCKED", selects[0].upper())

    def test_a_filtered_out_task_is_never_written_to_while_a_claim_runs(self):
        other = put(KIND_B)
        wanted = put(KIND_A, seconds=1)
        with CaptureQueriesContext(connection) as queries:
            claim({KIND_A}, now=at(5))
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
        self.assertTrue(writes)
        self.assertFalse([w for w in writes if str(other.pk) in w or str(other.pk).replace("-", "") in w], writes)  # no statement mentions the other task
        self.assertTrue(untouched(other))
        self.assertEqual(Task.objects.get(pk=wanted.pk).state, "running")

    def test_a_filter_with_many_kinds_still_works(self):
        task = put(KIND_C)
        kinds = {f"fake.k{i}" for i in range(200)} | {KIND_C}
        self.assertEqual(claim(kinds).task_id, task.pk)


@tag("requires_postgres")
@requires_postgres
class FilteredRetryAndRecoveryTests(base.DedicatedTestDatabaseMixin, TestCase):
    """Lease, retry, attempt numbering and recovery do not know about the filter."""

    def test_a_retry_gets_the_next_attempt_number_and_a_new_token_under_a_filter(self):
        task = put(KIND_A)
        first = claim({KIND_A}, now=T0)
        result = services.fail_task(task_id=task.pk, lease_token=first.lease_token, failure=base.RETRYABLE, now=at(1), rng=base.zero)
        self.assertEqual((result.state.value, result.available_at), ("queued", at(1) + timedelta(seconds=5)))
        self.assertIsNone(claim({KIND_A}, now=at(2)))  # backoff not over
        self.assertIsNone(claim({KIND_B}, now=at(100)))  # due, but not an allowed kind
        self.assertEqual(Task.objects.get(pk=task.pk).attempt_count, 1)
        second = claim({KIND_A}, now=at(100))
        self.assertEqual((second.task_id, second.attempt_number), (task.pk, 2))
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("attempt_number", "outcome")), [(1, "failed"), (2, "running")])

    def test_attempts_are_exhausted_exactly_as_without_a_filter(self):
        task = put(KIND_A, max_attempts=2)
        for attempt in (1, 2):
            claimed = claim({KIND_A}, now=at(1000 * attempt))
            self.assertEqual(claimed.attempt_number, attempt)
            services.fail_task(task_id=task.pk, lease_token=claimed.lease_token, failure=base.RETRYABLE, now=at(1000 * attempt + 1), rng=base.zero)
        self.assertIsNone(claim({KIND_A}, now=at(10000)))
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.failure_code), ("failed", 2, "temporary_failure"))

    def test_recovery_ignores_the_filter_and_requeues_an_expired_task_of_any_kind(self):
        a, b = put(KIND_A), put(KIND_B)
        claim({KIND_A, KIND_B}, now=T0, worker_id="x")
        claim({KIND_A, KIND_B}, now=T0, worker_id="y")
        result = services.recover_expired_leases(now=at(1000), rng=base.zero)
        self.assertEqual((result.requeued, result.failed), (2, 0))
        self.assertEqual(sorted(Task.objects.values_list("state", flat=True)), ["queued", "queued"])
        self.assertEqual(sorted(TaskAttempt.objects.values_list("outcome", "failure_code")), [("abandoned", "lease_expired")] * 2)
        # after recovery each kind is claimable again, only by a filter that allows it
        self.assertIsNone(claim({KIND_C}, now=at(2000)))
        self.assertEqual(claim({KIND_B}, now=at(2000)).task_id, b.pk)
        self.assertEqual(claim({KIND_A}, now=at(2000)).task_id, a.pk)

    def test_a_late_worker_is_still_fenced_out_after_recovery_and_a_filtered_reclaim(self):
        task = put(KIND_A)
        stale = claim({KIND_A}, now=T0)
        services.recover_expired_leases(now=at(1000), rng=base.zero)
        fresh = claim({KIND_A}, now=at(2000), worker_id="fresh")
        self.assertEqual(fresh.attempt_number, 2)
        with self.assertRaises(base.errors.LeaseLostError):
            services.complete_task(task_id=task.pk, lease_token=stale.lease_token, result={"x": 1}, now=at(2001))
        services.complete_task(task_id=task.pk, lease_token=fresh.lease_token, result={"x": 2}, now=at(2001))
        self.assertEqual(Task.objects.get(pk=task.pk).result, {"x": 2})

    def test_a_filtered_out_task_whose_lease_expired_is_recovered_not_claimed(self):
        task = put(KIND_A)
        claim(None, now=T0)  # running under an unrestricted worker that then died
        self.assertIsNone(claim({KIND_B}, now=at(1000)))  # a filtered claim neither recovers nor takes a running task
        self.assertEqual(Task.objects.get(pk=task.pk).state, "running")
        services.recover_expired_leases(now=at(1000), rng=base.zero)
        self.assertEqual(Task.objects.get(pk=task.pk).state, "queued")

    def test_cancelling_a_task_the_filter_excludes_works_as_before(self):
        task = put(KIND_A)
        self.assertEqual(services.cancel_task(task.pk, now=at(1)).value, "cancelled")
        self.assertIsNone(claim({KIND_A}, now=at(5)))


# --- concurrent workers with different filters --------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class FilteredClaimConcurrencyTests(base.ThreadedMixin, base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """Committed data visible across connections, hence TransactionTestCase; one connection per thread."""

    def test_workers_with_disjoint_filters_each_get_only_their_kind_and_no_task_twice(self):
        a_tasks = [put(KIND_A, payload={"n": i}) for i in range(2)]
        b_tasks = [put(KIND_B, payload={"n": i}) for i in range(2)]
        unknown = base.raw_task(kind=UNKNOWN, payload={})
        targets = [lambda i=i: claim({KIND_A}, worker_id=f"a-{i}") for i in range(3)] + [lambda i=i: claim({KIND_B}, worker_id=f"b-{i}") for i in range(3)]
        results = self.run_threads(targets)
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        a_got = [v for _, v in results[:3] if v is not None]
        b_got = [v for _, v in results[3:] if v is not None]
        self.assertEqual(sorted(c.task_id for c in a_got), sorted(t.pk for t in a_tasks))
        self.assertEqual(sorted(c.task_id for c in b_got), sorted(t.pk for t in b_tasks))
        self.assertTrue(all(c.kind == KIND_A for c in a_got) and all(c.kind == KIND_B for c in b_got))
        self.assertEqual(len({c.lease_token for c in a_got + b_got}), 4)
        self.assertEqual(Task.objects.filter(state="running").count(), 4)
        self.assertEqual(TaskAttempt.objects.filter(outcome="running").count(), 4)
        self.assertTrue(untouched(unknown))

    def test_a_filtered_worker_is_not_blocked_by_a_locked_row_of_another_kind(self):
        locked = put(KIND_A)
        wanted = put(KIND_B)
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=locked.pk)  # this connection holds the lock on the A task
            results = self.run_threads([lambda: claim({KIND_B}, worker_id="b")])
        self.assertTrue(results[0][0] == "ok" and results[0][1].task_id == wanted.pk, results)
        self.assertTrue(untouched(locked))

    def test_a_locked_row_of_the_allowed_kind_is_skipped_and_a_later_one_is_taken(self):
        first = put(KIND_A, seconds=0)
        second = put(KIND_A, seconds=1)
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=first.pk)
            results = self.run_threads([lambda: claim({KIND_A}, now=at(5), worker_id="skip")])
        self.assertEqual(results[0][1].task_id, second.pk)  # SKIP LOCKED: the second task, not a wait and not None
        self.assertTrue(untouched(first))

    def test_overlapping_filters_never_hand_out_a_task_twice_or_outside_a_workers_filter(self):
        tasks = {KIND_A: [put(KIND_A, payload={"n": i}) for i in range(2)], KIND_B: [put(KIND_B, payload={"n": i}) for i in range(2)], KIND_C: [put(KIND_C)]}
        filters = [{KIND_A, KIND_B}] * 3 + [{KIND_B, KIND_C}] * 3 + [{KIND_B}] * 2
        results = self.run_threads([lambda f=f, i=i: claim(f, worker_id=f"w-{i}") for i, f in enumerate(filters)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        pairs = [(f, v) for f, (_, v) in zip(filters, results) if v is not None]
        for allowed, claimed in pairs:
            self.assertIn(claimed.kind, allowed)
        ids = [v.task_id for _, v in pairs]
        self.assertEqual(len(ids), len(set(ids)))  # nobody shared a task
        every = {t.pk for group in tasks.values() for t in group}
        self.assertTrue(set(ids) <= every)
        self.assertEqual(Task.objects.filter(state="running").count(), len(ids))
        self.assertEqual(TaskAttempt.objects.count(), len(ids))
        self.assertEqual(Task.objects.filter(state="queued").count(), len(every) - len(ids))

    def test_an_empty_filter_worker_next_to_busy_workers_claims_nothing_and_disturbs_nobody(self):
        tasks = [put(KIND_A, payload={"n": i}) for i in range(3)]
        targets = [lambda: claim(frozenset(), worker_id="idle")] * 2 + [lambda i=i: claim({KIND_A}, worker_id=f"a-{i}") for i in range(4)]
        results = self.run_threads(targets)
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual([v for _, v in results[:2]], [None, None])
        got = [v.task_id for _, v in results[2:] if v is not None]
        self.assertEqual(sorted(got), sorted(t.pk for t in tasks))

    def test_competing_filtered_workers_for_one_task_have_exactly_one_winner(self):
        task = put(KIND_A)
        results = self.run_threads([lambda i=i: claim({KIND_A, KIND_B}, worker_id=f"w-{i}") for i in range(6)])
        winners = [v for _, v in results if v is not None]
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(len(winners), 1)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.lease_token), ("running", 1, winners[0].lease_token))
        self.assertEqual(TaskAttempt.objects.filter(task=task).count(), 1)

    def test_cancelling_and_a_filtered_claim_never_both_win(self):
        task = put(KIND_A)
        cancel_result, claim_result = self.run_threads([lambda: services.cancel_task(task.pk, now=at(1)), lambda: claim({KIND_A})])
        stored = Task.objects.get(pk=task.pk)
        if cancel_result[0] == "ok":
            self.assertEqual((claim_result, stored.state, stored.attempt_count), (("ok", None), "cancelled", 0))
        else:
            self.assertIsInstance(cancel_result[1], base.errors.CancellationNotSupportedError)
            self.assertEqual((claim_result[0], stored.state, stored.attempt_count), ("ok", "running", 1))


# --- the worker with an installed kind gate, against the database ---------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class GatedWorkerTests(base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """``process_one`` with a composition carrying a gate, on committed data, with an injected clock."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        self.open_kinds = {KIND_A, KIND_B}  # what the test gate answers; tests change it between iterations
        self.ran = []
        self.registry = base.registry_with(
            fake_alpha=lambda p: self.ran.append("a") or {"kind": "a"},
            fake_beta=lambda p: self.ran.append("b") or {"kind": "b"},
        )

    def install(self, gate):
        worker.install_composition(worker.Composition(registry=self.registry, kind_gate=gate))

    def process(self, seconds=0):
        return worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: at(seconds), registry=None)

    def enqueue(self, kind, **changes):
        return services.enqueue_task(kind=kind, payload={}, registry=self.registry, now=T0, **changes).task

    def test_only_the_gated_kinds_run_and_the_others_stay_queued_untouched(self):
        held = self.enqueue(KIND_A)
        open_ = self.enqueue(KIND_B)
        self.install(lambda: {KIND_B})
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(self.ran, ["b"])
        self.assertEqual(Task.objects.get(pk=open_.pk).state, "succeeded")
        self.assertTrue(untouched(held))
        self.assertEqual(self.process(10).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))

    def test_a_held_kind_runs_once_the_gate_opens_with_its_original_position_and_attempt_one(self):
        held = self.enqueue(KIND_A)
        self.open_kinds = {KIND_B}
        self.install(lambda: self.open_kinds)
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))
        self.open_kinds = {KIND_A, KIND_B}
        self.assertEqual(self.process(60).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=held.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("succeeded", 1))
        self.assertEqual(list(TaskAttempt.objects.filter(task=held).values_list("attempt_number", "outcome")), [(1, "succeeded")])

    def test_an_empty_gate_claims_nothing_but_still_recovers_expired_leases(self):
        crashed = self.enqueue(KIND_A)
        base.claim(now=T0, lease=60, worker_id="crashed")  # claimed, then the process died
        waiting = self.enqueue(KIND_B)  # enqueued after the claim, so the claim could only have taken the first task
        self.install(frozenset)
        self.assertEqual(self.process(1000).status, worker.ProcessStatus.IDLE)
        stored = Task.objects.get(pk=crashed.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("queued", 1))  # recovered: recovery does not look at the gate
        self.assertEqual(list(TaskAttempt.objects.filter(task=crashed).values_list("outcome", "failure_code")), [("abandoned", "lease_expired")])
        self.assertTrue(untouched(waiting))
        self.assertEqual(self.ran, [])

    def test_a_failing_gate_fails_closed_and_nothing_is_claimed(self):
        task = self.enqueue(KIND_A)

        def gate():
            raise RuntimeError("gate unavailable")

        self.install(gate)
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(task))
        self.assertEqual(self.ran, [])

    def test_a_task_of_a_kind_the_worker_cannot_run_stays_queued_when_a_gate_allows_it(self):
        stranger = base.raw_task(kind=UNKNOWN, payload={})
        self.install(lambda: {UNKNOWN, KIND_A})
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(stranger))  # not claimed and failed as unknown_kind: the gate is narrowed to known kinds

    def test_the_same_unknown_kind_is_failed_as_before_by_a_worker_with_no_gate(self):
        stranger = base.raw_task(kind=UNKNOWN, payload={})
        worker.install_composition(worker.Composition(registry=self.registry))
        self.assertEqual(self.process().status, worker.ProcessStatus.FAILED)
        stored = Task.objects.get(pk=stranger.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "unknown_kind"))

    def test_a_running_task_is_allowed_to_finish_after_its_kind_is_gated_off(self):
        calls = []

        def run(payload):
            calls.append(1)
            self.open_kinds = set()  # the gate closes while this attempt is running
            return {"done": 1}

        self.registry = base.registry_with(fake_alpha=run)
        task = self.enqueue(KIND_A)
        self.install(lambda: self.open_kinds)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((len(calls), Task.objects.get(pk=task.pk).state), (1, "succeeded"))

    def test_a_retry_of_a_gated_kind_waits_while_the_gate_is_closed_and_runs_when_it_opens(self):
        calls = []

        def run(payload):
            calls.append(1)
            if len(calls) == 1:
                raise TaskRetryableError()
            return {"calls": len(calls)}

        self.registry = base.registry_with(fake_alpha=run)
        task = self.enqueue(KIND_A)
        self.install(lambda: self.open_kinds)
        self.assertEqual(self.process(0).status, worker.ProcessStatus.REQUEUED)
        self.open_kinds = set()
        self.assertEqual(self.process(100).status, worker.ProcessStatus.IDLE)
        self.assertEqual(Task.objects.get(pk=task.pk).attempt_count, 1)
        self.open_kinds = {KIND_A}
        self.assertEqual(self.process(200).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count, stored.result), ("succeeded", 2, {"calls": 2}))
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", "failure_code")), [("failed", "temporary_failure"), ("succeeded", "")])

    def test_lease_fencing_is_unchanged_under_a_gate(self):
        clock = [T0]

        def run(payload):
            clock[0] = at(120)  # the 60 s lease ends while the handler is "running"
            return {"run": "stale"}

        self.registry = base.registry_with(fake_alpha=run)
        task = self.enqueue(KIND_A)
        self.install(lambda: {KIND_A})
        stale = worker.process_one(worker_id="w-test", lease_seconds=60, clock=lambda: clock[0], registry=None)
        self.assertEqual(stale.status, worker.ProcessStatus.LEASE_LOST)
        self.assertEqual(Task.objects.get(pk=task.pk).state, "running")
        failure = Failure(FailureCode.TEMPORARY_FAILURE, True)
        with self.assertRaises(base.errors.LeaseLostError):
            services.fail_task(task_id=task.pk, lease_token=TaskAttempt.objects.get(task=task).lease_token, failure=failure, now=at(121))

    def test_the_hooks_run_after_a_gated_iteration_in_order_with_the_database_available(self):
        order = []
        hooks = (
            worker.MaintenanceHook("one.count", lambda: order.append(("count", Task.objects.filter(state="queued").count()))),
            worker.MaintenanceHook("two.fail", lambda: 1 / 0),
            worker.MaintenanceHook("three.count", lambda: order.append(("after", Task.objects.count()))),
        )
        self.enqueue(KIND_A)
        worker.install_composition(worker.Composition(registry=self.registry, kind_gate=lambda: set(), after_iteration=lambda: order.append("legacy"), maintenance_hooks=hooks))
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            self.process()
        self.assertEqual(order, ["legacy", ("count", 1), ("after", 1)])
