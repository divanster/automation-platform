"""Tests for AI extraction (AI-2a) through the existing worker that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md).
When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet; the
project owner runs them locally (docs/development/request-intake-slice.md, section on AI extraction). When they do pass, that covers the tested scenarios only.
This file is ASCII only.

THE AI PROVIDER IS MOCKED. The real Workbench adapter and gateway run behind a FAKE ``HttpTransport`` (``tests.intake_ai_support``): no socket is opened, no
credential is real, and the Workbench is never contacted. The fake key and the sentinels below must appear in no stored row.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_intake_ai_postgres --tag requires_postgres -v 2

What needs a real server and is therefore here and not in the DB-free tests: the worker running the handler outside a transaction, the two short transactions
of ``run_extraction`` around a slow call, rollback of the second one, retries and recovery, locking between competing extractions, the module state gate, and
the review services on an AI-produced revision.
"""

import io
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TransactionTestCase, tag
from django.utils import timezone

from apps.accounts.roles import APPROVER, OPERATOR
from apps.ai_gateway import PolicyContext, PolicyMode
from apps.audit.models import AuditEvent
from apps.cases.models import Approval, Rejection, ResultRevision, WorkflowRun
from apps.intake import handlers as intake_handlers
from apps.intake import review, services
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import SelfApprovalError, WarningsNotAcknowledgedError
from apps.intake.models import ExtractionOutcome, RevisionEdit
from apps.intake.policy import KIND_EXTRACT
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.module_host import services as host
from apps.tasks import worker
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.services import claim_next_task, recover_expired_leases
from config import task_registry
from config.intake_extraction import build_extractor_source
from config.task_registry import COMPOSITION, MODULE_REGISTRY
from tests import test_review_postgres as review_base
from tests.intake_ai_support import SECRET_SENTINEL, TEXT_SENTINEL, VALID_OUTPUT, VALID_TEXT, FakeTransport, ai_environ, answer, answer_with, output_for, status
from tests.test_intake_postgres import INVALID_TEXT, DedicatedTestDatabaseMixin, counts, make_user, requires_postgres, run_threads, submit
from workflows.request_intake import DeterministicTestExtractor, Requester

ANSWER_SENTINEL = "ANSWER-SENTINEL-5e2b"
SENTINELS = (SECRET_SENTINEL, TEXT_SENTINEL, ANSWER_SENTINEL)
SOURCE_WITH_SENTINEL = f"Requester: Jane Doe {TEXT_SENTINEL}\nItem: 3 | | Pens\n"


class AIWorkerCase(DedicatedTestDatabaseMixin, TransactionTestCase):
    """The production composition (allowlist, reconciler, module gate) with an AI extractor source on a scripted fake transport.

    The worker refuses to run a handler inside a transaction, so these cannot be ``TestCase``.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        worker.install_composition(COMPOSITION)
        host.sync_modules(MODULE_REGISTRY)
        self.user = make_user("requester")
        self.clock_base = timezone.now()
        self.tick = 0

    # --- the AI source ---------------------------------------------------------------------------------------------

    def install_ai(self, *script, **env):
        """Install an ai-mode source (the real composition, the worker's own kind gate) over a scripted fake transport; return the transport."""
        transport = FakeTransport(*script)
        source = build_extractor_source(
            ai_environ(AP_AI_WORKBENCH_MAX_RETRIES="0", **env), module_gate=COMPOSITION.kind_gate, lease_seconds=60, transport=transport
        )  # MAX_RETRIES=0: no back-off sleep is ever real
        intake_handlers.install_extractor_source(source)
        return transport

    def process(self, advance_hours=None, **kwargs):
        """One worker iteration. ``advance_hours`` moves the clock past any back-off (the queue's retry delay), like the existing recovery tests."""
        if advance_hours is None:
            return worker.process_one(worker_id="test-worker", lease_seconds=60, **kwargs)
        base = self.clock_base
        return worker.process_one(worker_id="test-worker", lease_seconds=60, clock=lambda: base + timedelta(hours=advance_hours), **kwargs)

    def run_row(self, result):
        return WorkflowRun.objects.get(pk=result.run.pk)

    def task_row(self, result):
        return Task.objects.get(pk=result.task.pk)

    def outcome_row(self, result):
        return ExtractionOutcome.objects.get(run_id=result.run.pk)

    def everything_stored_about_the_queue_and_audit(self):
        """Every row an operator or a log shipper could read about the work, as text: the queue, the run and the audit history (never the outcome, which holds the draft)."""
        parts = [Task.objects.values(), TaskAttempt.objects.values(), AuditEvent.objects.values(), WorkflowRun.objects.values()]
        return "".join(str(list(rows)) for rows in parts)

    def assertNothingWritten(self, **expected):
        base = {"outcomes": 0, "revisions": 0}
        base.update(expected)
        stored = counts()
        self.assertEqual({k: stored[k] for k in base}, base)
        self.assertEqual((Approval.objects.count(), Rejection.objects.count(), RevisionEdit.objects.count()), (0, 0, 0))


# --- AI extraction through the worker -------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AIExtractionThroughTheWorkerTests(AIWorkerCase):
    def test_a_good_answer_becomes_outcome_revision_one_and_needs_review(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.kind), (worker.ProcessStatus.SUCCEEDED, KIND_EXTRACT))
        self.assertEqual(len(transport.requests), 1)
        task, run, stored = self.task_row(result), self.run_row(result), self.outcome_row(result)
        self.assertEqual((task.state, task.attempt_count), ("succeeded", 1))
        self.assertEqual(task.result, {"status": "valid", "outcome_id": str(stored.pk), "revision_id": str(stored.revision_id)})
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", stored.revision_id))
        revision = ResultRevision.objects.get(run=run)
        self.assertEqual((revision.revision_number, revision.pk), (1, stored.revision_id))
        self.assertEqual((stored.status, stored.extractor_name, stored.extractor_version), ("valid", "ai-request-extractor", "2|workbench-gpt5|gpt-5-2025-08-07"))
        self.assertEqual(stored.draft["requester"], {"name": "Jane Doe", "contact": "jane@example.invalid"})
        self.assertEqual(stored.draft["items"][0]["quantity"], "10")  # the exact digits, as text, like every stored draft
        self.assertEqual(stored.draft["source_refs"], [])

    def test_the_stored_revision_is_what_the_existing_domain_code_hashes(self):
        from apps.intake.draft_json import draft_from_json
        from workflows.request_intake import canonicalize_request

        self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        self.process()
        stored = self.outcome_row(result)
        expected = canonicalize_request(draft_from_json(stored.draft), source_text=VALID_TEXT)
        revision = ResultRevision.objects.get(run=result.run)
        self.assertEqual((revision.content_hash, bytes(revision.canonical_bytes)), (expected.content_hash, expected.canonical_bytes))

    def test_the_task_payload_and_result_hold_no_business_content_and_no_secret(self):
        self.install_ai(answer_with(output_for(notes=ANSWER_SENTINEL)))
        result = submit(self.user, SOURCE_WITH_SENTINEL)
        self.process()
        self.assertEqual(self.task_row(result).payload, {"run_id": str(result.run.pk)})
        everything = self.everything_stored_about_the_queue_and_audit()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, everything)

    def test_the_audit_history_is_the_same_vocabulary_with_the_same_safe_keys_as_the_deterministic_extraction(self):
        self.install_ai(answer_with(VALID_OUTPUT))
        ai = submit(self.user)
        self.process()
        intake_handlers.install_extractor_source(None)
        fixture = submit(self.user)
        self.process()

        def shape(result):
            events = AuditEvent.objects.filter(run_id=result.run.pk).order_by("id")
            return [(e.action, e.actor_kind, e.subject_type, sorted(e.details)) for e in events]

        self.assertEqual(shape(ai), shape(fixture))
        created = AuditEvent.objects.get(run_id=ai.run.pk, action="revision.created")
        self.assertEqual(created.details["origin"], "extraction")
        self.assertEqual((created.details["revision_number"], created.details["blocking_count"]), (1, 0))

    def test_a_business_invalid_answer_is_a_successful_task_with_an_invalid_outcome_and_no_revision(self):
        self.install_ai(answer_with(output_for(requester={"name": None, "contact": None})))
        result = submit(self.user)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        stored = self.outcome_row(result)
        self.assertEqual((stored.status, stored.revision_id), ("invalid", None))
        self.assertIn(["missing_required", "requester.name", True], [[f["code"], f["field_path"], f["blocking"]] for f in stored.findings])
        run = self.run_row(result)
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", None))
        self.assertEqual(ResultRevision.objects.count(), 0)

    def test_warnings_are_stored_and_the_revision_still_exists(self):
        invented = output_for(items=[{"description": "Gold bars", "quantity": "10", "unit": None, "notes": None}], requester={"name": "Jane Doe", "contact": None}, notes=None)
        self.install_ai(answer_with(invented))
        result = submit(self.user)
        self.process()
        stored = self.outcome_row(result)
        self.assertEqual(stored.status, "valid")
        self.assertIn(["value_not_in_source", "items[0].description", False], [[f["code"], f["field_path"], f["blocking"]] for f in stored.findings])

    def test_an_ai_answer_cannot_approve_itself(self):
        self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        self.process()
        self.assertEqual(self.run_row(result).state, "needs_review")
        self.assertEqual((Approval.objects.count(), Rejection.objects.count()), (0, 0))
        hostile = self.install_ai(answer_with({**VALID_OUTPUT, "approved": True}))
        again = submit(self.user)
        self.assertEqual(self.process().status, worker.ProcessStatus.FAILED)  # the answer is refused outright
        self.assertEqual(len(hostile.requests), 1)
        self.assertEqual(Approval.objects.count(), 0)
        self.assertNotEqual(self.run_row(again).state, "approved")

    def test_a_second_pass_finds_nothing_to_do_and_makes_no_ai_call(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT))
        submit(self.user)
        self.process()
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual((len(transport.requests), counts()["outcomes"]), (1, 1))

    def test_the_run_is_not_locked_and_no_transaction_is_open_during_the_ai_call(self):
        from django.db import connection

        observed = {}
        result = submit(self.user)

        class Probe(FakeTransport):
            def send(self, request):
                observed["in_transaction"] = connection.in_atomic_block
                return super().send(request)

        transport = Probe(answer_with(VALID_OUTPUT))
        source = build_extractor_source(ai_environ(AP_AI_WORKBENCH_MAX_RETRIES="0"), module_gate=COMPOSITION.kind_gate, lease_seconds=60, transport=transport)
        intake_handlers.install_extractor_source(source)
        self.process()
        self.assertEqual(observed, {"in_transaction": False})
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_the_runworker_command_installs_the_ai_extractor_and_processes_the_task(self):
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        real = build_extractor_source

        def with_fake_transport(*args, **kwargs):
            return real(*args, **{**kwargs, "transport": transport})

        env = ai_environ(AP_AI_WORKBENCH_MAX_RETRIES="0")
        out = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=False), mock.patch.object(task_registry, "build_extractor_source", side_effect=with_fake_transport):
            call_command("runworker", "--once", stdout=out, stderr=io.StringIO())
        self.assertIn(f"Task {result.task.pk} succeeded.", out.getvalue())
        self.assertEqual(self.outcome_row(result).extractor_name, "ai-request-extractor")
        self.assertEqual(len(transport.requests), 1)


# --- failures: nothing partial, nothing approved, no fallback ---------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AIFailureSemanticsTests(AIWorkerCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(DeterministicTestExtractor, "extract", side_effect=AssertionError("silent fallback to the fixture parser"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_retryable_provider_failure_requeues_the_task_and_writes_nothing(self):
        transport = self.install_ai(status(503))
        result = submit(self.user)
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "temporary_failure"))
        task = self.task_row(result)
        self.assertEqual((task.state, task.attempt_count), ("queued", 1))
        self.assertEqual(len(transport.requests), 1)
        self.assertNothingWritten()
        self.assertEqual(self.run_row(result).state, "extracting")  # claimed, not failed: a retry is waiting

    def test_a_retry_after_a_provider_failure_completes_the_run_once(self):
        transport = self.install_ai(status(503), answer_with(VALID_OUTPUT))
        result = submit(self.user)
        self.assertEqual(self.process().status, worker.ProcessStatus.REQUEUED)
        self.assertEqual(self.process(advance_hours=1).status, worker.ProcessStatus.SUCCEEDED)  # past the back-off
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))
        task = self.task_row(result)
        self.assertEqual((task.state, task.attempt_count), ("succeeded", 2))
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", "failure_code")), [("failed", "temporary_failure"), ("succeeded", "")])
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_retries_are_bounded_and_the_exhausted_run_is_failed_by_the_existing_reconciler(self):
        transport = self.install_ai(status(503), status(503), status(503), status(503))
        result = submit(self.user)
        statuses = [self.process(advance_hours=hour).status for hour in (1, 2, 3)]  # the queue's default attempt limit is 3
        self.assertEqual(statuses, [worker.ProcessStatus.REQUEUED, worker.ProcessStatus.REQUEUED, worker.ProcessStatus.FAILED])
        self.assertEqual(len(transport.requests), 3)  # never a fourth: no infinite retries
        task, run = self.task_row(result), self.run_row(result)
        self.assertEqual((task.state, task.failure_code, task.attempt_count), ("failed", "temporary_failure", 3))
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))
        self.assertNothingWritten()
        self.assertEqual(self.process(advance_hours=4).status, worker.ProcessStatus.IDLE)
        self.assertEqual(len(transport.requests), 3)

    def test_a_non_retryable_failure_fails_the_task_at_once_with_the_safe_code_and_fails_the_run(self):
        for name, response, reason in (
            ("authentication", status(401), "authentication_failure"),
            ("unknown deployment", status(404), "unsupported_model"),
            ("server error", status(500), "provider_error"),
            ("not json", answer(f"{ANSWER_SENTINEL} this is not json"), "invalid_structured_output"),
            ("a wrong shape", answer_with({"requester": ANSWER_SENTINEL}), "invalid_structured_output"),
            ("an unreadable quantity", answer_with(output_for(items=[{"description": "x", "quantity": "ten", "unit": None, "notes": None}])), "invalid_ai_output"),
        ):
            with self.subTest(name):
                transport = self.install_ai(response)
                result = submit(self.user, SOURCE_WITH_SENTINEL)
                outcome = self.process()
                self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "rejected"), reason)
                task, run = self.task_row(result), self.run_row(result)
                self.assertEqual((task.state, task.attempt_count, task.failure_code), ("failed", 1, "rejected"))
                self.assertEqual((run.state, run.failed_step), ("failed", "extract"))
                self.assertEqual(len(transport.requests), 1)  # no retry of a permanent failure, no second profile
                self.assertFalse(ExtractionOutcome.objects.filter(run_id=run.pk).exists())
                self.assertFalse(ResultRevision.objects.filter(run_id=run.pk).exists())
        self.assertEqual(Approval.objects.count(), 0)

    def test_failures_leave_no_text_answer_prompt_or_key_in_any_stored_row(self):
        for response in (status(503), status(401), answer(f"{ANSWER_SENTINEL} not json"), TimeoutError(f"{ANSWER_SENTINEL} {SECRET_SENTINEL}")):
            self.install_ai(response)
            submit(self.user, SOURCE_WITH_SENTINEL)
            self.process()
        everything = self.everything_stored_about_the_queue_and_audit()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, everything)
        self.assertNotIn("BEGIN REQUEST_TEXT", everything)

    def test_a_closed_policy_fails_the_task_permanently_and_sends_nothing(self):
        transport = FakeTransport()
        source = build_extractor_source(ai_environ(), module_gate=COMPOSITION.kind_gate, lease_seconds=60, transport=transport)
        closed = AIRequestExtractor(source.extractor._gateway, profile_id="workbench-gpt5", policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        intake_handlers.install_extractor_source(lambda: closed)  # a policy tightened after start-up: the gateway enforces it on every call
        result = submit(self.user)
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "rejected"))
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.run_row(result).failed_step, "extract")
        self.assertNothingWritten()

    def test_an_error_after_the_answer_rolls_the_second_transaction_back_completely(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT), answer_with(VALID_OUTPUT))
        result = submit(self.user)
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")):
            outcome = self.process()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "handler_error"))
        self.assertNothingWritten()  # the revision and the outcome were inserted in the same transaction: both rolled back
        self.assertEqual(self.run_row(result).state, "extracting")
        self.assertFalse(AuditEvent.objects.filter(run_id=result.run.pk, action="revision.created").exists())
        self.assertEqual(self.process(advance_hours=1).status, worker.ProcessStatus.SUCCEEDED)  # the retry asks the provider again and completes
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))

    def test_the_module_disabled_before_the_claim_holds_the_task_and_sends_nothing(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        host.disable_module(MODULE_REGISTRY, "request_intake")
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual((self.task_row(result).state, self.task_row(result).attempt_count, self.run_row(result).state), ("queued", 0, "queued"))
        self.assertEqual(transport.requests, [])
        host.enable_module(MODULE_REGISTRY, "request_intake")
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(len(transport.requests), 1)

    def test_a_module_disabled_between_the_claim_and_the_call_stops_the_call_before_any_text_is_sent(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        claimed = claim_next_task(worker_id="w", lease_seconds=60)
        host.disable_module(MODULE_REGISTRY, "request_intake")  # the race the claim-time gate cannot see
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        from apps.tasks.policy import TaskRetryableError

        with self.assertRaises(TaskRetryableError):
            handler.run(handler.validate(claimed.payload))
        self.assertEqual(transport.requests, [])
        self.assertNothingWritten()
        self.assertEqual(self.task_row(result).state, "running")  # the worker records the failure and requeues; nothing was extracted

    def test_a_version_mismatched_module_is_treated_like_a_disabled_one(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT))
        submit(self.user)
        from apps.module_host.models import ModuleState

        ModuleState.objects.filter(module_id="request_intake").update(installed_version="0.9.0")
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual(transport.requests, [])


# --- idempotency, recovery and competing workers ------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AIIdempotencyAndRecoveryTests(AIWorkerCase):
    def test_a_crash_after_the_outcome_is_committed_neither_duplicates_nor_overwrites_it_and_asks_the_provider_no_more(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT), answer_with(output_for(notes="A DIFFERENT ANSWER")))
        result = submit(self.user)
        claimed = claim_next_task(worker_id="doomed-worker", lease_seconds=5)
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        first_result = handler.run(handler.validate(claimed.payload))  # the AI call and the commit; then the worker "dies"
        stored = ExtractionOutcome.objects.values().get(run=result.run)
        revision_id = ResultRevision.objects.get(run=result.run).pk
        self.assertEqual(len(transport.requests), 1)

        later = timezone.now() + timedelta(seconds=120)
        self.assertEqual(recover_expired_leases(now=later).requeued, 1)
        outcome = self.process(clock=lambda: later + timedelta(seconds=60))
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        task = self.task_row(result)
        self.assertEqual((task.state, task.attempt_count, task.result), ("succeeded", 2, first_result))
        self.assertEqual(ExtractionOutcome.objects.values().get(run=result.run), stored)
        self.assertEqual(list(ResultRevision.objects.filter(run=result.run).values_list("pk", flat=True)), [revision_id])
        self.assertEqual(len(transport.requests), 1)  # the recovered attempt found the outcome and never called the provider again (no second cost, no second answer)

    def test_a_stale_worker_finishing_late_cannot_replace_the_committed_answer(self):
        transport = self.install_ai(answer_with(VALID_OUTPUT), answer_with(output_for(notes="THE STALE ANSWER")))
        result = submit(self.user)
        stale = claim_next_task(worker_id="stale-worker", lease_seconds=5)
        later = timezone.now() + timedelta(seconds=120)
        recover_expired_leases(now=later)
        self.assertEqual(self.process(clock=lambda: later + timedelta(seconds=60)).status, worker.ProcessStatus.SUCCEEDED)
        stored = ExtractionOutcome.objects.values().get(run=result.run)
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        late = handler.run(handler.validate(stale.payload))
        self.assertEqual(late, self.task_row(result).result)
        self.assertEqual(ExtractionOutcome.objects.values().get(run=result.run), stored)
        self.assertEqual(counts()["revisions"], 1)
        self.assertEqual(len(transport.requests), 1)

    def test_an_answer_that_arrives_after_another_worker_committed_is_discarded_and_the_first_committed_answer_stands(self):
        """A worker that lost its lease mid-call finishes late: its T2 finds the other worker's outcome and writes nothing (the lease fencing does not cover this; the outcome row does)."""
        transport = self.install_ai(answer_with(VALID_OUTPUT), answer_with(output_for(notes="THE SECOND ANSWER")))
        result = submit(self.user)
        inner = intake_handlers._extractor_source()
        late = {}

        class Racing:
            name, version = inner.name, inner.version

            def extract(self, text):
                mine = inner.extract(text)  # answer 1: this worker's, still uncommitted
                late["other"] = services.run_extraction(result.run.pk, extractor=inner)  # meanwhile another worker asks (answer 2) and commits
                return mine

        summary = services.run_extraction(result.run.pk, extractor=Racing())
        self.assertEqual((late["other"].created, summary.created), (True, False))
        self.assertEqual(summary.outcome_id, late["other"].outcome_id)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))
        self.assertEqual(ExtractionOutcome.objects.get(run=result.run).draft["notes"], "THE SECOND ANSWER")  # the first COMMITTED result stands, never overwritten
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_concurrent_extractions_of_one_run_give_one_outcome_and_one_revision(self):
        self.install_ai(answer_with(VALID_OUTPUT), answer_with(VALID_OUTPUT))
        result = submit(self.user)
        extractor = intake_handlers._extractor_source()
        results = run_threads(self, [lambda: services.run_extraction(result.run.pk, extractor=extractor)] * 2)
        self.assertEqual([kind for kind, _ in results], ["ok", "ok"], results)
        self.assertEqual(sorted(value.created for _, value in results), [False, True])
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_a_cancelled_run_discards_the_answer(self):
        self.install_ai(answer_with(VALID_OUTPUT))
        result = submit(self.user)
        WorkflowRun.objects.filter(pk=result.run.pk).update(state="cancelled")
        summary = services.run_extraction(result.run.pk, extractor=intake_handlers._extractor_source())
        self.assertEqual(summary.status, "discarded")
        self.assertNothingWritten()


# --- the modes: deterministic is untouched, history is never altered ---------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ModesTests(AIWorkerCase):
    def test_with_no_source_installed_the_default_end_to_end_flow_is_the_deterministic_one(self):
        result = submit(self.user)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        stored = self.outcome_row(result)
        self.assertEqual((stored.status, stored.extractor_name, stored.extractor_version), ("valid", "deterministic-test-fixture-parser", "1"))
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_an_explicit_deterministic_source_gives_exactly_the_same_stored_result_and_calls_no_provider(self):
        plain = submit(self.user)
        self.process()
        intake_handlers.install_extractor_source(build_extractor_source({}, lease_seconds=60))  # explicit deterministic mode: no gateway is built at all
        explicit = submit(self.user)
        self.process()
        keep = ("status", "extractor_name", "extractor_version", "draft", "ambiguities", "findings")
        self.assertEqual({k: v for k, v in ExtractionOutcome.objects.values().get(run=plain.run).items() if k in keep}, {k: v for k, v in ExtractionOutcome.objects.values().get(run=explicit.run).items() if k in keep})
        self.assertEqual(ResultRevision.objects.get(run=plain.run).content_hash, ResultRevision.objects.get(run=explicit.run).content_hash)

    def test_the_fixture_parser_is_not_called_in_ai_mode_and_an_invalid_text_is_still_an_ai_question(self):
        transport = self.install_ai(answer_with(output_for(requester={"name": None, "contact": None})))
        with mock.patch.object(DeterministicTestExtractor, "extract", side_effect=AssertionError("fixture parser used in ai mode")):
            submit(self.user, INVALID_TEXT)
            self.process()
        self.assertEqual(len(transport.requests), 1)

    def test_switching_modes_never_rewrites_an_earlier_outcome(self):
        first = submit(self.user)
        self.process()  # deterministic
        before = ExtractionOutcome.objects.values().get(run=first.run)
        self.install_ai(answer_with(VALID_OUTPUT))
        second = submit(self.user)
        self.process()  # ai
        self.assertEqual(ExtractionOutcome.objects.values().get(run=first.run), before)
        self.assertEqual({o.extractor_name for o in ExtractionOutcome.objects.all()}, {"deterministic-test-fixture-parser", "ai-request-extractor"})
        self.assertEqual(self.run_row(second).state, "needs_review")


# --- human review of an AI-produced revision -----------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class HumanReviewOfAIRevisionTests(AIWorkerCase):
    def setUp(self):
        super().setUp()
        review_base.setup_roles()
        self.operator = review_base.make_user("olive", OPERATOR)
        self.approver = review_base.make_user("anna", APPROVER)
        self.hybrid = review_base.make_user("hugo", OPERATOR, APPROVER)

    def extracted(self, owner=None, document=None):
        self.install_ai(answer_with(document or VALID_OUTPUT))
        result = submit(owner or self.operator)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        return result

    def summary(self, result):
        return review.get_review_summary(user=self.approver, run_id=result.run.pk)

    def test_a_different_approver_approves_the_ai_revision_and_the_approval_binds_its_hash(self):
        result = self.extracted()
        summary = self.summary(result)
        approved = review.approve_request(user=self.approver, run_id=result.run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, acknowledged_warnings=summary.warnings)
        self.assertEqual(approved.run_state, "approved")
        approval = Approval.objects.get(revision__run=result.run)
        self.assertEqual((approval.revision.content_hash, approval.revision.revision_number), (summary.content_hash, 1))

    def test_the_submitter_cannot_approve_the_ai_revision(self):
        result = self.extracted(owner=self.hybrid)
        summary = self.summary(result)
        with self.assertRaises(SelfApprovalError):
            review.approve_request(user=self.hybrid, run_id=result.run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, acknowledged_warnings=summary.warnings)
        self.assertEqual(Approval.objects.count(), 0)

    def test_warnings_must_be_acknowledged_before_an_ai_revision_with_warnings_is_approved(self):
        invented = output_for(items=[{"description": "Gold bars", "quantity": "10", "unit": None, "notes": None}], requester={"name": "Jane Doe", "contact": None}, notes=None)
        result = self.extracted(document=invented)
        summary = self.summary(result)
        self.assertTrue(summary.warnings)
        with self.assertRaises(WarningsNotAcknowledgedError):
            review.approve_request(user=self.approver, run_id=result.run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, acknowledged_warnings=())
        self.assertEqual(Approval.objects.count(), 0)

    def test_an_ai_revision_can_be_rejected_with_a_fixed_reason(self):
        result = self.extracted()
        summary = self.summary(result)
        review.reject_request(user=self.approver, run_id=result.run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, reason_code="not_supported_by_source")
        self.assertEqual((Rejection.objects.count(), Approval.objects.count()), (1, 0))

    def test_a_human_edit_of_an_ai_revision_creates_revision_two_and_the_ai_revision_is_untouched(self):
        import dataclasses

        from apps.intake.draft_json import draft_from_json

        result = self.extracted()
        first = ResultRevision.objects.get(run=result.run)
        original = (first.content_hash, bytes(first.canonical_bytes))
        draft = dataclasses.replace(draft_from_json(self.outcome_row(result).draft), notes="Edited by a person")
        edited = review.edit_request(user=self.operator, run_id=result.run.pk, base_revision_id=first.pk, draft=draft)
        self.assertEqual(edited.revision_number, 2)
        first.refresh_from_db()
        self.assertEqual((first.content_hash, bytes(first.canonical_bytes)), original)
        self.assertEqual(RevisionEdit.objects.get(revision_id=edited.revision_id).edited_by_id, self.operator.pk)

    def test_an_invalid_ai_outcome_can_be_repaired_by_a_person_through_the_existing_edit_path(self):
        import dataclasses

        from apps.intake.draft_json import draft_from_json

        result = self.extracted(document=output_for(requester={"name": None, "contact": None}))
        self.assertEqual(self.outcome_row(result).status, "invalid")
        stored = draft_from_json(self.outcome_row(result).draft)
        draft = dataclasses.replace(stored, requester=Requester(name="Jane Doe", contact=None))
        edited = review.edit_request(user=self.operator, run_id=result.run.pk, base_revision_id=None, draft=draft)  # no base: the AI outcome had no revision
        self.assertEqual(edited.revision_number, 1)
        self.assertEqual(ResultRevision.objects.filter(run=result.run).count(), 1)
        self.assertEqual(RevisionEdit.objects.get(revision_id=edited.revision_id).base_revision_id, None)
