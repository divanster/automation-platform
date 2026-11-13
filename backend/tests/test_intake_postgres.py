"""Tests for the request-intake orchestration that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no
connection to one. Nothing in them is evidence yet; the project owner runs them locally (see
docs/development/request-intake-orchestration-slice.md). When they do pass, that covers the tested
scenarios only: it does not prove that no other race exists. This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_intake_postgres --tag requires_postgres -v 2

What needs a real server and is therefore here and not in the DB-free tests: constraints and
migrations, transactions and rollback, row locking, the worker (which refuses to run a handler
inside a transaction, so the worker tests use TransactionTestCase), and competing connections.
"""

import io
import os
import threading
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
from django.utils import timezone

from apps.cases import services as case_services
from apps.cases.errors import ImmutableRecordError, InvalidTransitionError, RecordNotFoundError
from apps.cases.models import Case, ResultRevision, WorkflowRun
from apps.cases.run_state import RunState
from apps.intake import handlers as intake_handlers
from apps.intake import services
from apps.intake.draft_json import draft_from_json
from apps.intake.errors import ClientRequestConflictError, RequestSourceMissingError
from apps.intake.extraction import build_extraction
from apps.intake.models import ExtractionOutcome, RequestSource, StepTask
from apps.intake.policy import (
    KIND_EXTRACT,
    MAX_REQUEST_TEXT_BYTES,
    RequestTextError,
    extraction_idempotency_key,
    extraction_payload,
)
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.submission import submit_request
from apps.tasks import worker
from apps.tasks.handlers import TaskHandler
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.policy import Failure, FailureCode, TaskPermanentError, TaskRetryableError, TaskState
from apps.tasks.services import claim_next_task, enqueue_task, fail_task, recover_expired_leases
from config.task_registry import COMPOSITION
from tests.intake_support import VALID_TEXT
from workflows.request_intake import (
    DeterministicTestExtractor,
    ExtractionResult,
    RequestedItem,
    Requester,
    StructuredRequest,
)
from workflows.revision_approval import ResultRevision as DomainRevision

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

INVALID_TEXT = "Contact: nobody@example.invalid\nItem: a few | boxes | Pens\nItem: 0 | | Stapler\n"

# Bounds for the competing-connection tests (seconds): a wrong locking design must surface as an
# error within these limits, not as a run that hangs and keeps the test database busy.
BARRIER_TIMEOUT = 30
LOCK_TIMEOUT_SECONDS = 15
JOIN_TIMEOUT = 60


class DedicatedTestDatabaseMixin:
    """Refuse to run against anything but the Django-created test database (second line of defence)."""

    @classmethod
    def setUpClass(cls):
        name = connection.settings_dict["NAME"]
        if not str(name).startswith("test_"):
            raise AssertionError("PostgreSQL intake tests must run against a dedicated test database (test_*).")
        super().setUpClass()


def make_user(name="alice"):
    return get_user_model().objects.create_user(username=name)


def submit(user, text=VALID_TEXT, **kwargs):
    return submit_request(text=text, submitted_by=user, **kwargs)


def counts():
    return {
        "cases": Case.objects.count(),
        "runs": WorkflowRun.objects.count(),
        "sources": RequestSource.objects.count(),
        "tasks": Task.objects.count(),
        "links": StepTask.objects.count(),
        "outcomes": ExtractionOutcome.objects.count(),
        "revisions": ResultRevision.objects.count(),
    }


ZERO = {key: 0 for key in ("cases", "runs", "sources", "tasks", "links", "outcomes", "revisions")}


class SpyExtractor:
    """Wraps the deterministic extractor; records calls and whether a transaction was open."""

    name = "spy"
    version = "1"

    def __init__(self, hook=None):
        self.inner = DeterministicTestExtractor()
        self.calls = 0
        self.in_transaction = []
        self.hook = hook

    def extract(self, text):
        self.calls += 1
        self.in_transaction.append(connection.in_atomic_block)
        if self.hook is not None:
            self.hook()
        return self.inner.extract(text)


class FixedExtractor:
    """Returns a prepared candidate whatever the text is."""

    name = "fixed"
    version = "7"

    def __init__(self, candidate):
        self.candidate = candidate
        self.calls = 0

    def extract(self, text):
        self.calls += 1
        return ExtractionResult(candidate=self.candidate, extractor_name=self.name, extractor_version=self.version)


def run_threads(test, targets):
    """Run each target on its own thread (and so its own connection) at the same moment."""
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
            connection.close()

    threads = [threading.Thread(target=work, args=(i, t), daemon=True) for i, t in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    test.assertEqual([t for t in threads if t.is_alive()], [], "a thread did not finish within the bound (possible deadlock)")
    test.assertNotIn(None, results)
    return results


# --- schema ------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def constraints(self, model):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(cursor, model._meta.db_table)

    def test_the_database_has_every_named_constraint(self):
        expected = {
            RequestSource: {"intake_source_client_request_unique", "intake_source_hash_is_sha256_hex", "intake_source_size_within_limit"},
            StepTask: {"intake_steptask_run_step_generation_unique", "intake_steptask_step_valid", "intake_steptask_generation_at_least_1"},
            ExtractionOutcome: {"intake_outcome_status_valid", "intake_outcome_revision_matches_status"},
            WorkflowRun: {"cases_run_state_valid", "cases_run_failed_step_valid", "cases_run_failed_step_matches_state"},
        }
        for model, names in expected.items():
            with self.subTest(model=model.__name__):
                self.assertLessEqual(names, set(self.constraints(model)))

    def test_one_source_and_one_outcome_per_run_in_the_database(self):
        for model in (RequestSource, ExtractionOutcome):
            with self.subTest(model=model.__name__):
                unique = [c["columns"] for c in self.constraints(model).values() if c["unique"] and not c["primary_key"]]
                self.assertIn(["run_id"], unique)

    def test_a_task_is_linked_to_at_most_one_step(self):
        unique = [c["columns"] for c in self.constraints(StepTask).values() if c["unique"] and not c["primary_key"]]
        self.assertIn(["task_id"], unique)
        self.assertIn(["run_id", "step", "generation"], unique)

    def test_the_run_state_constraints_reject_bad_rows(self):
        run = case_services.create_run(case_id=case_services.create_case(reference="constraints").pk)
        self.assertEqual((run.state, run.failed_step), ("queued", ""))  # the default for a run made the old way
        for fields in ({"state": "bogus"}, {"failed_step": "extract"}, {"state": "failed"}, {"state": "failed", "failed_step": "other"}, {"state": "queued", "failed_step": "export"}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                WorkflowRun.objects.filter(pk=run.pk).update(**fields)
        WorkflowRun.objects.filter(pk=run.pk).update(state="failed", failed_step="extract")  # the allowed combination
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).state, "failed")

    def test_the_source_constraints_reject_bad_rows(self):
        result = submit(make_user())
        base = dict(submitted_by=result.source.submitted_by, text="x", text_sha256="a" * 64, byte_length=1)
        for fields in ({"text_sha256": "A" * 64}, {"text_sha256": "a" * 63}, {"byte_length": 0}, {"byte_length": MAX_REQUEST_TEXT_BYTES + 1}):
            other = case_services.create_run(case_id=case_services.create_case(reference=f"src-{uuid.uuid4()}").pk)
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                RequestSource.objects.create(run=other, **{**base, **fields})
        with self.assertRaises(IntegrityError), transaction.atomic():  # a second source for the same run
            RequestSource.objects.create(run=result.run, **base)

    def test_a_step_task_is_unique_per_run_step_and_generation(self):
        result = submit(make_user())
        again = enqueue_task(kind="test.add_numbers", payload={"values": [1]}, registry=PRODUCTION_REGISTRY).task
        with self.assertRaises(IntegrityError), transaction.atomic():
            StepTask.objects.create(run=result.run, step="extract", generation=1, task=again)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StepTask.objects.create(run=result.run, step="other", generation=2, task=again)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StepTask.objects.create(run=result.run, step="extract", generation=0, task=again)

    def test_the_outcome_status_and_revision_must_agree_and_be_unique(self):
        user = make_user()
        first = submit(user)
        services.run_extraction(first.run.pk)  # valid: has a revision
        second = submit(user, INVALID_TEXT)
        row = dict(extractor_name="x", extractor_version="1", draft={}, ambiguities=[], findings=[])
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExtractionOutcome.objects.create(run=second.run, status="valid", revision=None, **row)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExtractionOutcome.objects.create(run=second.run, status="invalid", revision=ResultRevision.objects.get(run=first.run), **row)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExtractionOutcome.objects.create(run=second.run, status="other", revision=None, **row)
        with self.assertRaises(IntegrityError), transaction.atomic():  # one outcome per run
            ExtractionOutcome.objects.create(run=first.run, status="invalid", revision=None, **row)

    def test_the_source_and_the_outcome_are_immutable_through_the_orm(self):
        user = make_user()
        result = submit(user)
        services.run_extraction(result.run.pk)
        source, outcome = RequestSource.objects.get(run=result.run), ExtractionOutcome.objects.get(run=result.run)
        source.text = "changed"
        for action in (source.save, source.delete, outcome.delete, lambda: RequestSource.objects.filter(pk=source.pk).update(text="x"), lambda: ExtractionOutcome.objects.all().delete()):
            with self.assertRaises(ImmutableRecordError):
                action()
        self.assertEqual(RequestSource.objects.get(pk=source.pk).text, VALID_TEXT)

    def test_history_is_protected_from_deletion(self):
        result = submit(make_user())
        services.run_extraction(result.run.pk)
        from django.db.models import ProtectedError

        for obj in (result.run, result.case, result.task, ResultRevision.objects.get(run=result.run)):
            with self.subTest(obj=type(obj).__name__), self.assertRaises((ProtectedError, ImmutableRecordError)), transaction.atomic():
                obj.delete()


# --- the cases-level state service ----------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class TransitionServiceTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.run = case_services.create_run(case_id=case_services.create_case(reference="transitions").pk)

    def test_an_allowed_transition_is_saved(self):
        run = case_services.transition_run(self.run.pk, to=RunState.EXTRACTING)
        self.assertEqual(run.state, "extracting")
        self.assertEqual(WorkflowRun.objects.get(pk=self.run.pk).state, "extracting")

    def test_a_refused_transition_writes_nothing(self):
        for kwargs, reason in (
            ({"to": RunState.NEEDS_REVIEW}, "not_allowed"),
            ({"to": RunState.EXTRACTING, "failed_step": "extract"}, "failed_step_unexpected"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidTransitionError) as caught:
                case_services.transition_run(self.run.pk, **kwargs)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(WorkflowRun.objects.get(pk=self.run.pk).state, "queued")

    def test_allowed_from_must_match_the_stored_state(self):
        with self.assertRaises(InvalidTransitionError) as caught:
            case_services.transition_run(self.run.pk, to=RunState.EXTRACTING, allowed_from=[RunState.EXTRACTING])
        self.assertEqual(caught.exception.reason, "state_mismatch")

    def test_failure_needs_a_step_and_a_retry_clears_it(self):
        case_services.transition_run(self.run.pk, to=RunState.EXTRACTING)
        with self.assertRaises(InvalidTransitionError):
            case_services.transition_run(self.run.pk, to=RunState.FAILED)
        failed = case_services.transition_run(self.run.pk, to=RunState.FAILED, failed_step="extract")
        self.assertEqual((failed.state, failed.failed_step), ("failed", "extract"))
        with self.assertRaises(InvalidTransitionError):  # the wrong retry target for a failed extract
            case_services.transition_run(self.run.pk, to=RunState.APPROVED)
        retried = case_services.transition_run(self.run.pk, to=RunState.QUEUED)
        self.assertEqual((retried.state, retried.failed_step), ("queued", ""))

    def test_unknown_runs_and_bad_arguments(self):
        with self.assertRaises(RecordNotFoundError):
            case_services.transition_run(uuid.uuid4(), to=RunState.EXTRACTING)
        with self.assertRaises(ValueError):
            case_services.transition_run(self.run.pk, to="bogus")


# --- submission ------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SubmissionTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.user = make_user()

    def test_one_call_creates_the_case_run_source_task_and_link(self):
        result = submit(self.user)
        self.assertTrue(result.created)
        self.assertEqual(counts(), {**ZERO, "cases": 1, "runs": 1, "sources": 1, "tasks": 1, "links": 1})
        self.assertRegex(result.case.reference, r"^REQ-\d{8}-[0-9A-F]{8}$")
        self.assertEqual(result.run.case_id, result.case.pk)
        self.assertEqual((result.run.state, result.run.failed_step, result.run.current_revision_id), ("queued", "", None))

    def test_the_task_and_the_run_are_linked_correctly(self):
        result = submit(self.user)
        link = StepTask.objects.get()
        self.assertEqual((link.run_id, link.step, link.generation, link.task_id), (result.run.pk, "extract", 1, result.task.pk))
        task = Task.objects.get(pk=result.task.pk)
        self.assertEqual(task.kind, KIND_EXTRACT)
        self.assertEqual(task.state, TaskState.QUEUED.value)
        self.assertEqual(task.idempotency_key, extraction_idempotency_key(result.run.pk, 1))
        self.assertEqual(task.payload, {"run_id": str(result.run.pk)})

    def test_no_business_text_reaches_the_queue(self):
        result = submit(self.user, "Requester: Jane SECRET\nItem: 1 | | Pens\n")
        task = Task.objects.get(pk=result.task.pk)
        self.assertNotIn("SECRET", str(task.payload) + task.idempotency_key + task.request_fingerprint)
        self.assertEqual(task.payload, extraction_payload(result.run.pk))

    def test_the_source_is_stored_exactly_with_its_hash_and_size(self):
        import hashlib

        text = "  Requester: Zo\u00eb \r\nItem: 1 | | x\n\n\tNotes: z  \n"
        result = submit(self.user, text)
        source = RequestSource.objects.get(run=result.run)
        self.assertEqual(source.text, text)  # no trimming, no newline normalization
        self.assertEqual(source.text_sha256, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(source.byte_length, len(text.encode("utf-8")))
        self.assertEqual(source.submitted_by_id, self.user.pk)
        self.assertIsNone(source.client_request_id)

    def test_text_that_may_not_be_stored_creates_nothing(self):
        for text in ("", "  \n ", "a\x00b", "a" * (MAX_REQUEST_TEXT_BYTES + 1), None, b"bytes"):
            with self.subTest(text=repr(text)[:20]), self.assertRaises(RequestTextError):
                submit(self.user, text)
        self.assertEqual(counts(), ZERO)

    def test_the_largest_allowed_text_is_stored(self):
        text = "a" * MAX_REQUEST_TEXT_BYTES
        self.assertEqual(RequestSource.objects.get(run=submit(self.user, text).run).byte_length, MAX_REQUEST_TEXT_BYTES)

    def test_a_refused_enqueue_rolls_everything_back(self):
        from apps.tasks.errors import UnknownTaskKindError

        with mock.patch("apps.intake.submission.enqueue_task", side_effect=UnknownTaskKindError("refused")):
            with self.assertRaises(UnknownTaskKindError):
                submit(self.user)
        self.assertEqual(counts(), ZERO)

    def test_a_failure_after_the_enqueue_rolls_the_task_back_too(self):
        with mock.patch.object(StepTask.objects, "create", side_effect=RuntimeError("link failed")):
            with self.assertRaises(RuntimeError):
                submit(self.user)
        self.assertEqual(counts(), ZERO)  # no case, run, source, or orphaned task

    def test_a_failure_while_storing_the_source_rolls_the_case_and_run_back(self):
        with mock.patch.object(RequestSource.objects, "create", side_effect=RuntimeError("source failed")):
            with self.assertRaises(RuntimeError):
                submit(self.user)
        self.assertEqual(counts(), ZERO)

    def test_two_requests_get_two_cases_two_runs_and_two_tasks(self):
        first, second = submit(self.user), submit(self.user)
        self.assertEqual(len({first.case.pk, second.case.pk}), 2)
        self.assertEqual(len({first.case.reference, second.case.reference}), 2)
        self.assertEqual(len({first.task.pk, second.task.pk}), 2)
        self.assertEqual(counts()["links"], 2)

    def test_a_repeated_client_request_id_returns_the_original_and_creates_nothing(self):
        cid = uuid.uuid4()
        first = submit(self.user, client_request_id=cid)
        again = submit(self.user, client_request_id=cid)
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual((again.case.pk, again.run.pk, again.source.pk, again.task.pk), (first.case.pk, first.run.pk, first.source.pk, first.task.pk))
        self.assertEqual(counts(), {**ZERO, "cases": 1, "runs": 1, "sources": 1, "tasks": 1, "links": 1})

    def test_the_same_client_request_id_with_other_text_is_a_conflict(self):
        cid = uuid.uuid4()
        submit(self.user, client_request_id=cid)
        with self.assertRaises(ClientRequestConflictError):
            submit(self.user, INVALID_TEXT, client_request_id=cid)
        self.assertEqual(counts()["runs"], 1)

    def test_a_client_request_id_is_per_user(self):
        cid = uuid.uuid4()
        other = make_user("bob")
        first, second = submit(self.user, client_request_id=cid), submit(other, client_request_id=cid)
        self.assertNotEqual(first.run.pk, second.run.pk)
        self.assertTrue(second.created)

    def test_a_case_reference_collision_is_retried(self):
        Case.objects.create(reference="REQ-DUPLICATE")
        with mock.patch("apps.intake.submission.new_case_reference", side_effect=["REQ-DUPLICATE", "REQ-DUPLICATE", "REQ-FRESH"]):
            result = submit(self.user)
        self.assertEqual(result.case.reference, "REQ-FRESH")
        self.assertEqual(counts()["cases"], 2)

    def test_repeated_collisions_give_up_and_store_nothing_new(self):
        Case.objects.create(reference="REQ-DUPLICATE")
        with mock.patch("apps.intake.submission.new_case_reference", return_value="REQ-DUPLICATE"):
            with self.assertRaises(IntegrityError):
                submit(self.user)
        self.assertEqual(counts(), {**ZERO, "cases": 1})


# --- the extraction step (direct calls) ----------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ExtractionServiceTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.user = make_user()

    def refreshed(self, run):
        return WorkflowRun.objects.get(pk=run.pk)

    def test_a_valid_request_gives_an_outcome_revision_one_and_needs_review(self):
        result = submit(self.user)
        summary = services.run_extraction(result.run.pk)
        outcome = ExtractionOutcome.objects.get(run=result.run)
        revision = ResultRevision.objects.get(run=result.run)
        run = self.refreshed(result.run)
        self.assertEqual((summary.status, summary.created, summary.outcome_id, summary.revision_id), ("valid", True, outcome.pk, revision.pk))
        self.assertEqual((outcome.status, outcome.revision_id), ("valid", revision.pk))
        self.assertEqual((revision.revision_number, revision.run_id), (1, result.run.pk))
        self.assertEqual((run.state, run.failed_step, run.current_revision_id), ("needs_review", "", revision.pk))
        self.assertEqual((outcome.extractor_name, outcome.extractor_version), (DeterministicTestExtractor.name, DeterministicTestExtractor.version))

    def test_the_stored_revision_is_the_one_the_existing_domain_code_produces(self):
        result = submit(self.user)
        services.run_extraction(result.run.pk)
        stored = case_services.load_revision(ResultRevision.objects.get(run=result.run).pk)  # re-verifies the hash on load
        expected = build_extraction(VALID_TEXT, case_id=result.case.pk, run_id=result.run.pk, revision_id=uuid.uuid4()).revision
        self.assertEqual((stored.canonical_bytes, stored.content_hash, stored.canonical_format_version), (expected.canonical_bytes, expected.content_hash, expected.canonical_format_version))
        self.assertEqual(stored.findings, expected.findings)
        self.assertIsInstance(stored, DomainRevision)

    def test_the_outcome_keeps_the_draft_and_the_findings(self):
        result = submit(self.user, "Requester: Jane\nItem: 1 | | Pens\nItem: 2 | | pens\n")
        services.run_extraction(result.run.pk)
        outcome = ExtractionOutcome.objects.get(run=result.run)
        revision = ResultRevision.objects.get(run=result.run)
        self.assertEqual(draft_from_json(outcome.draft), DeterministicTestExtractor().extract(result.source.text).candidate)
        self.assertEqual(outcome.findings, revision.findings)  # the warnings, identically stored
        self.assertIn("duplicate_item", [f["code"] for f in outcome.findings])
        self.assertEqual(outcome.ambiguities, [])

    def test_an_invalid_request_gives_an_invalid_outcome_and_no_revision(self):
        result = submit(self.user, INVALID_TEXT)
        summary = services.run_extraction(result.run.pk)
        outcome = ExtractionOutcome.objects.get(run=result.run)
        run = self.refreshed(result.run)
        self.assertEqual((summary.status, summary.revision_id), ("invalid", None))
        self.assertEqual((outcome.status, outcome.revision_id), ("invalid", None))
        self.assertEqual(ResultRevision.objects.count(), 0)
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", None))  # not a run failure
        blocking = {(f["code"], f["field_path"]) for f in outcome.findings if f["blocking"]}
        self.assertIn(("missing_required", "requester.name"), blocking)
        self.assertEqual(draft_from_json(outcome.draft), DeterministicTestExtractor().extract(INVALID_TEXT).candidate)  # nothing invented
        self.assertEqual(outcome.ambiguities, [{"field_path": "items[0].quantity", "excerpt": "a few"}])

    def test_a_repeated_call_returns_the_existing_outcome_and_writes_nothing(self):
        result = submit(self.user)
        spy = SpyExtractor()
        first = services.run_extraction(result.run.pk, extractor=spy)
        before = counts()
        again = services.run_extraction(result.run.pk, extractor=spy)
        self.assertEqual(spy.calls, 1)  # the second call did not even extract
        self.assertEqual((again.status, again.outcome_id, again.revision_id, again.created), (first.status, first.outcome_id, first.revision_id, False))
        self.assertEqual(again.as_task_result(), first.as_task_result())
        self.assertEqual(counts(), before)
        self.assertEqual(self.refreshed(result.run).state, "needs_review")

    def test_a_repeat_for_an_invalid_outcome_is_also_idempotent(self):
        result = submit(self.user, INVALID_TEXT)
        first = services.run_extraction(result.run.pk)
        again = services.run_extraction(result.run.pk)
        self.assertEqual((again.outcome_id, again.created), (first.outcome_id, False))
        self.assertEqual(counts()["outcomes"], 1)

    def test_a_later_call_never_overwrites_the_first_committed_result(self):
        result = submit(self.user)
        first = services.run_extraction(result.run.pk)
        stored = ExtractionOutcome.objects.values().get(run=result.run)
        other = FixedExtractor(StructuredRequest(requester=Requester(name="Someone Else"), items=(RequestedItem(description="Z", quantity=9),)))
        again = services.run_extraction(result.run.pk, extractor=other)
        self.assertEqual(other.calls, 0)
        self.assertEqual(again.outcome_id, first.outcome_id)
        self.assertEqual(ExtractionOutcome.objects.values().get(run=result.run), stored)  # every column unchanged
        self.assertEqual(ResultRevision.objects.filter(run=result.run).count(), 1)
        self.assertEqual(self.refreshed(result.run).current_revision_id, first.revision_id)

    def test_if_another_call_commits_during_the_extraction_its_result_stands(self):
        result = submit(self.user)
        winner = []

        def commit_a_competing_result():
            winner.append(services.run_extraction(result.run.pk))  # runs while the outer call is "extracting"

        spy = SpyExtractor(hook=commit_a_competing_result)
        outer = services.run_extraction(result.run.pk, extractor=spy)
        self.assertEqual(len(winner), 1)
        self.assertEqual((outer.created, outer.outcome_id), (False, winner[0].outcome_id))
        self.assertEqual(counts()["outcomes"], 1)
        self.assertEqual(counts()["revisions"], 1)

    def test_a_cancelled_run_is_left_alone(self):
        result = submit(self.user)
        case_services.transition_run(result.run.pk, to=RunState.CANCELLED)
        summary = services.run_extraction(result.run.pk)
        self.assertEqual((summary.status, summary.run_state, summary.outcome_id), ("discarded", "cancelled", None))
        self.assertEqual(summary.as_task_result(), {"status": "discarded", "run_state": "cancelled"})
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))
        self.assertEqual(self.refreshed(result.run).state, "cancelled")

    def test_a_run_that_cancels_during_the_extraction_discards_the_result(self):
        result = submit(self.user)
        spy = SpyExtractor(hook=lambda: case_services.transition_run(result.run.pk, to=RunState.CANCELLED))
        summary = services.run_extraction(result.run.pk, extractor=spy)
        self.assertEqual((summary.status, summary.run_state), ("discarded", "cancelled"))
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))

    def test_a_run_without_a_stored_source_is_refused_and_not_moved(self):
        run = case_services.create_run(case_id=case_services.create_case(reference="no-source").pk)
        with self.assertRaises(RequestSourceMissingError):
            services.run_extraction(run.pk)
        self.assertEqual(self.refreshed(run).state, "queued")

    def test_an_unknown_run_is_refused(self):
        with self.assertRaises(RecordNotFoundError):
            services.run_extraction(uuid.uuid4())

    def test_an_extractor_error_leaves_the_run_extracting_and_a_later_call_completes_it(self):
        result = submit(self.user)

        class Broken:
            name, version = "broken", "1"

            def extract(self, text):
                raise RuntimeError("provider down")

        with self.assertRaises(RuntimeError):
            services.run_extraction(result.run.pk, extractor=Broken())
        self.assertEqual(self.refreshed(result.run).state, "extracting")  # claimed, nothing committed
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))
        summary = services.run_extraction(result.run.pk)  # the retry
        self.assertEqual((summary.status, self.refreshed(result.run).state), ("valid", "needs_review"))

    def test_a_value_that_cannot_be_serialized_stores_nothing(self):
        from decimal import Decimal

        from workflows.request_intake import SerializationSafetyError

        result = submit(self.user)
        candidate = StructuredRequest(requester=Requester(name="Jane"), items=(RequestedItem(description="x", quantity=Decimal("1E+1000000")),))
        with self.assertRaises(SerializationSafetyError):
            services.run_extraction(result.run.pk, extractor=FixedExtractor(candidate))
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))

    def test_the_result_commit_is_atomic(self):
        result = submit(self.user)
        with mock.patch.object(ExtractionOutcome.objects, "create", side_effect=RuntimeError("outcome failed")):
            with self.assertRaises(RuntimeError):
                services.run_extraction(result.run.pk)
        self.assertEqual(ResultRevision.objects.count(), 0)  # the revision inserted just before was rolled back
        run = self.refreshed(result.run)
        self.assertEqual((run.current_revision_id, run.state), (None, "extracting"))

    def test_a_failure_moving_the_state_rolls_the_revision_and_outcome_back(self):
        result = submit(self.user)
        real = case_services.transition_run

        def fail_on_needs_review(run_id, **kwargs):
            if kwargs.get("to") == RunState.NEEDS_REVIEW:
                raise RuntimeError("state failed")
            return real(run_id, **kwargs)

        with mock.patch.object(services.case_services, "transition_run", side_effect=fail_on_needs_review):
            with self.assertRaises(RuntimeError):
                services.run_extraction(result.run.pk)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))
        self.assertEqual(self.refreshed(result.run).current_revision_id, None)

    def test_every_run_gets_its_own_revision_one(self):
        first, second = submit(self.user), submit(self.user)
        services.run_extraction(first.run.pk)
        services.run_extraction(second.run.pk)
        self.assertEqual(sorted(ResultRevision.objects.values_list("revision_number", flat=True)), [1, 1])
        self.assertEqual(ResultRevision.objects.filter(run=first.run).count(), 1)


@tag("requires_postgres")
@requires_postgres
class ExtractionHoldsNoTransactionTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def test_no_transaction_and_no_run_lock_is_held_during_the_extraction(self):
        result = submit(make_user())
        observed = {}

        def during_extraction():
            def probe():
                try:
                    with transaction.atomic():
                        WorkflowRun.objects.select_for_update(nowait=True).get(pk=result.run.pk)  # fails at once if locked
                    observed["lock"] = "free"
                except Exception as error:  # recorded; asserted below
                    observed["lock"] = type(error).__name__
                finally:
                    connection.close()

            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            thread.join(timeout=JOIN_TIMEOUT)

        spy = SpyExtractor(hook=during_extraction)
        services.run_extraction(result.run.pk, extractor=spy)
        self.assertEqual(spy.in_transaction, [False])
        self.assertEqual(observed, {"lock": "free"})
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).state, "needs_review")


# --- the worker end to end ----------------------------------------------------------------------------


class _WorkerCase(DedicatedTestDatabaseMixin, TransactionTestCase):
    """The worker refuses to run a handler inside a transaction, so these cannot be TestCase."""

    def setUp(self):
        self.user = make_user()
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)

    def process(self, **kwargs):
        kwargs.setdefault("registry", PRODUCTION_REGISTRY)
        return worker.process_one(worker_id="test-worker", lease_seconds=60, **kwargs)

    def run_row(self, result):
        return WorkflowRun.objects.get(pk=result.run.pk)

    def task_row(self, result):
        return Task.objects.get(pk=result.task.pk)


@tag("requires_postgres")
@requires_postgres
class WorkerEndToEndTests(_WorkerCase):
    def test_a_valid_request_runs_through_the_worker_to_needs_review(self):
        result = submit(self.user)
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.kind), (worker.ProcessStatus.SUCCEEDED, KIND_EXTRACT))
        task, run = self.task_row(result), self.run_row(result)
        stored = ExtractionOutcome.objects.get(run=run)
        self.assertEqual((task.state, task.attempt_count), (TaskState.SUCCEEDED.value, 1))
        self.assertEqual(task.result, {"status": "valid", "outcome_id": str(stored.pk), "revision_id": str(stored.revision_id)})
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", stored.revision_id))
        self.assertEqual(ResultRevision.objects.get(run=run).revision_number, 1)
        self.assertEqual(self.task_row(result).payload, {"run_id": str(run.pk)})  # still no text

    def test_an_invalid_request_is_a_successful_task_with_an_invalid_outcome(self):
        result = submit(self.user, INVALID_TEXT)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        task, run = self.task_row(result), self.run_row(result)
        self.assertEqual((task.state, task.result["status"], task.result["revision_id"]), ("succeeded", "invalid", None))
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", None))
        self.assertEqual(ResultRevision.objects.count(), 0)

    def test_the_task_result_holds_no_business_content(self):
        result = submit(self.user, "Requester: Jane SECRET\nItem: 1 | | Pens\n")
        self.process()
        self.assertNotIn("SECRET", str(self.task_row(result).result))
        self.assertEqual(set(self.task_row(result).result), {"status", "outcome_id", "revision_id"})

    def test_the_installed_composition_supplies_the_allowlist(self):
        result = submit(self.user)
        worker.install_composition(COMPOSITION)
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60)  # no registry passed
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_without_the_composition_the_intake_kind_is_unknown_and_the_task_fails_permanently(self):
        result = submit(self.user)
        worker.install_composition(worker.Composition())
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60)
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "unknown_kind"))
        self.assertEqual(self.task_row(result).state, "failed")

    def test_the_runworker_command_installs_the_composition_and_processes_the_task(self):
        result = submit(self.user)
        out = io.StringIO()
        call_command("runworker", "--once", stdout=out, stderr=io.StringIO())  # closes the connection at the end; Django reconnects
        self.assertIn(f"Task {result.task.pk} succeeded.", out.getvalue())
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_a_second_pass_finds_nothing_to_do(self):
        submit(self.user)
        self.process()
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual(counts()["outcomes"], 1)


@tag("requires_postgres")
@requires_postgres
class CrashAfterPersistenceTests(_WorkerCase):
    def test_a_crash_after_the_outcome_is_committed_neither_duplicates_nor_overwrites_it(self):
        result = submit(self.user)
        # The worker claims the task and the handler commits the outcome, then the worker "dies"
        # before the queue records the task as succeeded.
        claimed = claim_next_task(worker_id="doomed-worker", lease_seconds=5)
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        first_result = handler.run(handler.validate(claimed.payload))
        self.assertEqual(self.task_row(result).state, "running")
        stored = ExtractionOutcome.objects.values().get(run=result.run)
        revision_id = ResultRevision.objects.get(run=result.run).pk

        later = timezone.now() + timedelta(seconds=120)
        self.assertEqual(recover_expired_leases(now=later).requeued, 1)
        self.assertEqual(self.task_row(result).state, "queued")

        outcome = self.process(clock=lambda: later + timedelta(seconds=60))  # the retry, after the backoff
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        task = self.task_row(result)
        self.assertEqual((task.state, task.attempt_count, task.result), ("succeeded", 2, first_result))  # the same result again
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", flat=True)), ["abandoned", "succeeded"])
        self.assertEqual(ExtractionOutcome.objects.values().get(run=result.run), stored)  # untouched
        self.assertEqual(list(ResultRevision.objects.filter(run=result.run).values_list("pk", flat=True)), [revision_id])  # no second revision
        self.assertEqual(self.run_row(result).state, "needs_review")

    def test_a_crash_after_the_outcome_for_an_invalid_draft_behaves_the_same(self):
        result = submit(self.user, INVALID_TEXT)
        claimed = claim_next_task(worker_id="doomed-worker", lease_seconds=5)
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        handler.run(handler.validate(claimed.payload))
        later = timezone.now() + timedelta(seconds=120)
        recover_expired_leases(now=later)
        self.assertEqual(self.process(clock=lambda: later + timedelta(seconds=60)).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 0))

    def test_a_stale_worker_finishing_late_cannot_record_over_the_winner(self):
        result = submit(self.user)
        stale = claim_next_task(worker_id="stale-worker", lease_seconds=5)
        later = timezone.now() + timedelta(seconds=120)
        recover_expired_leases(now=later)
        self.assertEqual(self.process(clock=lambda: later + timedelta(seconds=60)).status, worker.ProcessStatus.SUCCEEDED)  # the new attempt wins
        stored = ExtractionOutcome.objects.values().get(run=result.run)
        handler = PRODUCTION_REGISTRY[KIND_EXTRACT]
        late = handler.run(handler.validate(stale.payload))  # the stale handler runs again, too late
        self.assertEqual(late, self.task_row(result).result)
        self.assertEqual(ExtractionOutcome.objects.values().get(run=result.run), stored)
        self.assertEqual(counts()["revisions"], 1)


@tag("requires_postgres")
@requires_postgres
class ReconciliationTests(_WorkerCase):
    def fail_permanently(self, result):
        claimed = claim_next_task(worker_id="w", lease_seconds=60)
        fail_task(task_id=claimed.task_id, lease_token=claimed.lease_token, failure=Failure(FailureCode.REJECTED, retryable=False))
        self.assertEqual(self.task_row(result).state, "failed")

    def raising_registry(self, error):
        def run(payload):
            raise error

        handler = TaskHandler(kind=KIND_EXTRACT, validate=intake_handlers.validate_payload, run=run, is_test_handler=False)
        return {**PRODUCTION_REGISTRY, KIND_EXTRACT: handler}

    def test_a_permanently_failed_task_moves_its_run_to_failed(self):
        result = submit(self.user)
        self.fail_permanently(result)
        self.assertEqual(self.run_row(result).state, "queued")  # nothing reconciled yet
        self.assertEqual(services.reconcile_runs().failed, 1)
        run = self.run_row(result)
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))

    def test_reconciling_again_changes_nothing(self):
        result = submit(self.user)
        self.fail_permanently(result)
        services.reconcile_runs()
        before = list(WorkflowRun.objects.values())
        self.assertEqual(services.reconcile_runs().failed, 0)
        self.assertEqual(list(WorkflowRun.objects.values()), before)

    def test_a_task_waiting_for_a_retry_does_not_fail_the_run(self):
        result = submit(self.user)
        with mock.patch.object(services, "build_extraction", side_effect=RuntimeError("provider down")):
            outcome = self.process()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "handler_error"))
        self.assertEqual(self.task_row(result).state, "queued")
        self.assertEqual(services.reconcile_runs().failed, 0)
        run = self.run_row(result)
        self.assertEqual((run.state, run.failed_step), ("extracting", ""))  # the run was claimed; it has not failed

    def test_a_run_whose_attempts_are_exhausted_is_failed_by_the_worker_hook(self):
        result = submit(self.user)
        worker.install_composition(COMPOSITION)  # the after-iteration hook is the reconciler
        base = timezone.now()
        statuses = []
        with mock.patch.object(services, "build_extraction", side_effect=RuntimeError("SECRET provider detail")):
            for attempt in range(3):  # the default attempt limit
                clock = lambda attempt=attempt: base + timedelta(hours=attempt + 1)  # past any backoff
                statuses.append(worker.process_one(worker_id="test-worker", lease_seconds=60, clock=clock).status)
                if attempt < 2:
                    self.assertEqual(self.run_row(result).state, "extracting")  # still retrying: not failed
        self.assertEqual(statuses, [worker.ProcessStatus.REQUEUED, worker.ProcessStatus.REQUEUED, worker.ProcessStatus.FAILED])
        task, run = self.task_row(result), self.run_row(result)
        self.assertEqual((task.state, task.failure_code, task.attempt_count), ("failed", "handler_error", 3))
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))  # reconciled in the same iteration
        stored = str(list(WorkflowRun.objects.values())) + str(list(Task.objects.values())) + str(list(TaskAttempt.objects.values()))
        self.assertNotIn("SECRET", stored)  # no exception text anywhere
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (0, 0))

    def test_a_permanent_handler_failure_fails_the_run_in_one_iteration(self):
        result = submit(self.user)
        worker.install_composition(COMPOSITION)
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60, registry=self.raising_registry(TaskPermanentError(FailureCode.REJECTED)))
        self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)
        run = self.run_row(result)
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))
        self.assertEqual(self.task_row(result).failure_code, "rejected")

    def test_a_retryable_handler_failure_with_attempts_left_does_not_fail_the_run(self):
        result = submit(self.user)
        worker.install_composition(COMPOSITION)
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60, registry=self.raising_registry(TaskRetryableError()))
        self.assertEqual(outcome.status, worker.ProcessStatus.REQUEUED)
        self.assertEqual(self.run_row(result).state, "queued")

    def test_a_run_that_already_has_an_outcome_is_never_failed(self):
        result = submit(self.user)
        self.process()
        WorkflowRun.objects.filter(pk=result.run.pk).update(state="extracting")  # an inconsistent state, forced
        Task.objects.filter(pk=result.task.pk).update(state="failed", failure_code="rejected", lease_token=None, lease_expires_at=None, result=None)
        self.assertEqual(services.reconcile_runs().failed, 0)
        self.assertEqual(self.run_row(result).state, "extracting")

    def test_the_newest_generation_of_the_step_decides(self):
        result = submit(self.user)
        self.fail_permanently(result)
        newer = enqueue_task(
            kind=KIND_EXTRACT,
            payload=extraction_payload(result.run.pk),
            idempotency_key=extraction_idempotency_key(result.run.pk, 2),
            registry=PRODUCTION_REGISTRY,
        ).task
        StepTask.objects.create(run=result.run, step="extract", generation=2, task=newer)
        self.assertEqual(services.reconcile_runs().failed, 0)  # generation 2 is still queued
        self.assertEqual(self.run_row(result).state, "queued")

    def test_runs_in_other_states_are_ignored(self):
        result = submit(self.user)
        self.fail_permanently(result)
        case_services.transition_run(result.run.pk, to=RunState.CANCELLED)
        self.assertEqual(services.reconcile_runs().failed, 0)
        self.assertEqual(self.run_row(result).state, "cancelled")

    def test_the_batch_limit_is_respected_and_validated(self):
        results = [submit(self.user) for _ in range(3)]
        for result in results:
            self.fail_permanently(result)
        self.assertEqual(services.reconcile_runs(limit=2).failed, 2)
        self.assertEqual(services.reconcile_runs(limit=2).failed, 1)
        for bad in (0, -1, True, "2"):
            with self.assertRaises(ValueError):
                services.reconcile_runs(limit=bad)

    def test_a_failure_in_the_hook_does_not_stop_the_worker(self):
        submit(self.user)
        worker.install_composition(worker.Composition(registry=PRODUCTION_REGISTRY, after_iteration=lambda: 1 / 0))
        with self.assertLogs("apps.tasks.worker", level="ERROR"):
            self.assertEqual(worker.process_one(worker_id="test-worker", lease_seconds=60).status, worker.ProcessStatus.SUCCEEDED)


# --- competing connections ----------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        self.user = make_user()

    def test_concurrent_submissions_are_independent_and_complete(self):
        texts = [f"Requester: Person {i}\nItem: {i + 1} | | Thing {i}\n" for i in range(6)]
        results = run_threads(self, [lambda t=t: submit(self.user, t) for t in texts])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        submissions = [value for _, value in results]
        self.assertEqual(counts(), {**ZERO, "cases": 6, "runs": 6, "sources": 6, "tasks": 6, "links": 6})
        self.assertEqual(len({s.case.reference for s in submissions}), 6)
        self.assertEqual(len({s.task.pk for s in submissions}), 6)
        for s in submissions:  # every run is linked to exactly its own task and text
            link = StepTask.objects.get(run=s.run)
            self.assertEqual(link.task_id, s.task.pk)
            self.assertEqual(Task.objects.get(pk=link.task_id).payload, {"run_id": str(s.run.pk)})
            self.assertEqual(RequestSource.objects.get(run=s.run).text, texts[[x.run.pk for x in submissions].index(s.run.pk)])

    def test_concurrent_submissions_with_one_client_request_id_create_one_run(self):
        cid = uuid.uuid4()
        results = run_threads(self, [lambda: submit(self.user, client_request_id=cid) for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        submissions = [value for _, value in results]
        self.assertEqual(sum(1 for s in submissions if s.created), 1)
        self.assertEqual(len({s.run.pk for s in submissions}), 1)
        self.assertEqual(counts(), {**ZERO, "cases": 1, "runs": 1, "sources": 1, "tasks": 1, "links": 1})

    def test_concurrent_extractions_of_one_run_give_one_outcome_and_one_revision(self):
        result = submit(self.user)
        results = run_threads(self, [lambda: services.run_extraction(result.run.pk) for _ in range(4)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        summaries = [value for _, value in results]
        self.assertEqual(sum(1 for s in summaries if s.created), 1)
        self.assertEqual(len({s.outcome_id for s in summaries}), 1)
        self.assertEqual(len({s.revision_id for s in summaries}), 1)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))
        run = WorkflowRun.objects.get(pk=result.run.pk)
        self.assertEqual((run.state, run.current_revision_id), ("needs_review", summaries[0].revision_id))
        self.assertEqual(ResultRevision.objects.get(run=run).revision_number, 1)

    def test_concurrent_extractions_of_an_invalid_request_give_one_outcome(self):
        result = submit(self.user, INVALID_TEXT)
        results = run_threads(self, [lambda: services.run_extraction(result.run.pk) for _ in range(4)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 0))

    def test_concurrent_reconcilers_fail_the_run_once(self):
        result = submit(self.user)
        claimed = claim_next_task(worker_id="w", lease_seconds=60)
        fail_task(task_id=claimed.task_id, lease_token=claimed.lease_token, failure=Failure(FailureCode.REJECTED, retryable=False))
        results = run_threads(self, [services.reconcile_runs for _ in range(3)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(value.failed for _, value in results), 1)
        run = WorkflowRun.objects.get(pk=result.run.pk)
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))

    def test_two_workers_racing_for_one_task_run_it_once(self):
        result = submit(self.user)
        results = run_threads(
            self,
            [lambda: worker.process_one(worker_id="w", lease_seconds=60, registry=PRODUCTION_REGISTRY) for _ in range(2)],
        )
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        statuses = sorted(value.status.value for _, value in results)
        self.assertEqual(statuses, ["idle", "succeeded"])
        self.assertEqual((counts()["outcomes"], counts()["revisions"]), (1, 1))
        self.assertEqual(Task.objects.get(pk=result.task.pk).attempt_count, 1)


# --- the existing-row migration ------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RunStateMigrationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Applies cases 0002..0004 to a database that already holds runs, as the owner's local one may.

    It migrates the test database back to cases 0001 (unapplying the intake migration that depends on
    cases 0004), inserts runs the old way, migrates forward again, and checks what happened to them.
    The test database is returned to the latest migrations afterwards, pass or fail.
    """

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection)

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def test_existing_runs_survive_and_get_a_consistent_state(self):
        executor = self.migrate([("intake", None), ("cases", "0001_initial")])
        old = executor.loader.project_state([("cases", "0001_initial")]).apps
        OldCase, OldRun, OldRevision = old.get_model("cases", "Case"), old.get_model("cases", "WorkflowRun"), old.get_model("cases", "ResultRevision")
        case = OldCase.objects.create(id=uuid.uuid4(), reference="old-case")
        without = OldRun.objects.create(id=uuid.uuid4(), case=case)
        with_revision = OldRun.objects.create(id=uuid.uuid4(), case=case)
        revision = OldRevision.objects.create(
            id=uuid.uuid4(), run=with_revision, revision_number=1, canonical_format_version=1, canonical_bytes=b"{}", content_hash="a" * 64, findings=[]
        )
        OldRun.objects.filter(pk=with_revision.pk).update(current_revision=revision)

        self.migrate(self.latest)

        runs = {run.pk: run for run in WorkflowRun.objects.all()}
        self.assertEqual(len(runs), 2)  # nothing was deleted
        self.assertEqual((runs[without.pk].state, runs[without.pk].failed_step), ("queued", ""))
        self.assertEqual((runs[with_revision.pk].state, runs[with_revision.pk].failed_step), ("needs_review", ""))
        self.assertEqual(runs[with_revision.pk].current_revision_id, revision.pk)  # untouched
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(ResultRevision.objects.count(), 1)

    def column_names(self):
        with connection.cursor() as cursor:
            return {column.name for column in connection.introspection.get_table_description(cursor, WorkflowRun._meta.db_table)}

    def test_the_state_columns_are_added_and_removed_by_the_migrations(self):
        self.migrate([("intake", None), ("cases", "0001_initial")])
        self.assertFalse({"state", "failed_step"} & self.column_names())
        self.migrate(self.latest)
        self.assertLessEqual({"state", "failed_step"}, self.column_names())
