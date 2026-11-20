"""Tests of PDF text extraction that REQUIRE a real PostgreSQL server (DOC-2).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet; the project owner runs them locally. When they
do pass, that covers the tested scenarios only. This file is ASCII only.

Every document is a small synthetic PDF built in memory (``tests/pdf_support.py``); the storage directory is a temporary one behind a permissive test policy. Most tests use a SCRIPTED extractor (a
function returning a prepared result) so they exercise the database, the queue and the services, not the PDF library; two use the real isolated extractor. Nothing is sent to any server.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_document_text_postgres --tag requires_postgres -v 2
"""

import io
import os
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase, override_settings, tag
from django.utils import timezone

from apps.accounts.roles import APPROVER, AUDITOR, OPERATOR
from apps.audit.models import AuditEvent
from apps.cases.errors import ImmutableRecordError
from apps.document_text import execution, handlers, rules, services
from apps.document_text.errors import ExtractionNotReadyError, ExtractionRecordMissingError
from apps.document_text.execution import ExtractionRuntime
from apps.document_text.isolation import ExtractorUnavailableError, IsolatedExtractor
from apps.document_text.models import TextExtraction, TextExtractionPage, TextExtractionResult
from apps.document_text.registry import DOCUMENT_TEXT_REGISTRY
from apps.document_text.rules import ExtractionLimits, PageText
from apps.documents import services as document_services
from apps.documents.errors import CODE_DISABLED, DocumentNotFoundError, DocumentStorageError
from apps.documents.models import Document
from apps.documents.storage import DocumentStore
from apps.accounts.errors import AuthorizationError
from apps.tasks import services as task_services
from apps.tasks import worker
from apps.tasks.models import Task, TaskAttempt
from tests import test_api_postgres as base
from tests.documents_support import PermissivePolicy
from tests.pdf_support import build_pdf
from tests.test_documents_postgres import StorageMixin, put_document
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, run_threads

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

User = get_user_model()
RATES = {**base.RATES, "document_upload": "10000/min", "document_upload_hour": "10000/hour", "document_text": "10000/min", "document_text_hour": "10000/hour"}
VERSION = "7.1.1"
SENTINEL = "EXTRACTED-SENTINEL-93aa"
LIMITS = ExtractionLimits()


def pdf_pages(*texts):
    return build_pdf([(text,) if text else () for text in texts])


def scripted_output(*texts, status_override=None):
    pages = [PageText(n, rules.PAGE_TEXT if text.strip() else rules.PAGE_EMPTY, "", text) for n, text in enumerate(texts, 1)]
    return rules.output_from_pages(pages, extractor_version=VERSION)


class Scripted:
    """An extractor stand-in that returns a prepared output (or raises) and records every call."""

    def __init__(self, output=None, error=None):
        self.output = output if output is not None else scripted_output(SENTINEL, "second page", "")
        self.error = error
        self.calls = []

    def __call__(self, data, limits):
        self.calls.append((bytes(data), limits))
        if self.error is not None:
            raise self.error
        return self.output


def events_of(action):
    return list(AuditEvent.objects.filter(action=action).order_by("id"))


class TextCase(StorageMixin, DedicatedTestDatabaseMixin, TransactionTestCase):
    """Roles, users, a prepared storage directory, an uploader and a runtime installed for the handler.

    A ``TransactionTestCase``, not a ``TestCase``: these tests run the real worker (``worker.process_one``), which refuses to run a handler inside a database transaction (``apps.tasks.worker._refuse_open_transaction``),
    and ``TestCase`` wraps every test in one. The tests also need real commits (the handler's own short transactions, the result written once, a recovery after a crash). Like the other PostgreSQL worker tests
    (``tests/test_intake_postgres.py`` ``_WorkerCase``, ``tests/test_tasks_postgres.py`` ``WorkerProcessTests``) the tables are emptied after each test and every test creates its own roles and users in ``setUp``.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        override = override_settings(API_THROTTLE_RATES=RATES, **base.FAST_HASHERS)
        override.enable()
        self.addCleanup(override.disable)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = base.make_user("olive", OPERATOR)
        self.other_operator = base.make_user("omar", OPERATOR)
        self.approver = base.make_user("anna", APPROVER)
        self.auditor = base.make_user("audrey", AUDITOR)
        self.superuser = base.make_user("root", superuser=True)
        self.use_storage()
        self.addCleanup(handlers.install_runtime, handlers._runtime)
        self.previous_composition = worker._composition
        self.addCleanup(setattr, worker, "_composition", self.previous_composition)

    def store(self):
        return DocumentStore(self.root, PermissivePolicy(), max_bytes=20 * 1024 * 1024)

    def upload(self, user=None, data=None):
        data = data if data is not None else pdf_pages("Hello", "World")
        item = document_services.upload_document(
            user=user or self.operator, filename_header="a.pdf", content_type="application/pdf", content_length=str(len(data)), read=io.BytesIO(data).read, store_factory=self.store
        )
        return Document.objects.get(pk=item.id)

    def install(self, extractor=None, store_factory=None, limits=LIMITS):
        extractor = Scripted() if extractor is None else extractor
        handlers.install_runtime(ExtractionRuntime(store_factory or self.store, limits, extractor))
        return extractor

    def runtime(self, extractor=None, store_factory=None):
        return ExtractionRuntime(store_factory or self.store, LIMITS, extractor or Scripted())

    def request(self, document, user=None):
        return services.request_text_extraction(user=user or self.operator, document_id=document.pk, extractor_version=VERSION)

    def run_worker(self, hours=0):
        return worker.process_one(worker_id="w-test", lease_seconds=60, registry=DOCUMENT_TEXT_REGISTRY, clock=lambda: timezone.now() + timedelta(hours=hours))

    def extract(self, document=None, extractor=None, user=None):
        """Request, install a runtime and run the worker once; returns the request item and the worker outcome."""
        document = document or self.upload()
        item = self.request(document, user).item
        self.install(extractor)
        return item, self.run_worker()


@tag("requires_postgres")
@requires_postgres
class RequestPersistenceTests(TextCase):
    def test_a_request_persists_one_extraction_one_queued_task_and_one_audit_event_together(self):
        document = self.upload()
        outcome = self.request(document)
        self.assertTrue(outcome.created)
        extraction = TextExtraction.objects.get()
        self.assertEqual((extraction.pk, extraction.document_id, extraction.requested_by_id), (outcome.item.id, document.pk, self.operator.pk))
        self.assertIsNotNone(extraction.requested_at)
        task = Task.objects.get(pk=extraction.task_id)
        self.assertEqual((task.kind, task.state, task.payload, task.idempotency_key, task.attempt_count), ("documents.extract_text", "queued", {"extraction_id": str(extraction.pk)}, f"documents.extract_text:{extraction.pk}", 0))
        (event,) = events_of("document.text_requested")
        self.assertEqual((event.actor_kind, event.actor_public_id, event.subject_type, event.subject_id, event.details), ("user", self.operator.public_id, "document", document.pk, {"extraction_id": str(extraction.pk)}))
        self.assertEqual((outcome.item.status, TextExtractionResult.objects.count()), ("queued", 0))

    def test_the_task_and_the_payload_hold_no_text_no_file_name_and_no_path(self):
        document = self.upload()
        self.request(document)
        task = Task.objects.get()
        for forbidden in ("a.pdf", document.storage_key, self.root, "Hello"):
            self.assertNotIn(forbidden, str(task.payload) + task.idempotency_key + task.request_fingerprint)

    def test_a_pending_request_is_returned_not_duplicated(self):
        document = self.upload()
        first = self.request(document)
        second = self.request(document)
        self.assertEqual((first.created, second.created, first.item.id == second.item.id), (True, False, True))
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count(), len(events_of("document.text_requested"))), (1, 1, 1))

    def test_a_finished_extraction_by_the_same_library_version_is_returned_but_another_version_makes_a_new_one(self):
        document = self.upload()
        item, _ = self.extract(document)
        again = self.request(document)
        self.assertEqual((again.created, again.item.id, again.item.status), (False, item.id, "extracted"))
        newer = services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version="8.0.0")
        self.assertTrue(newer.created)
        self.assertEqual(TextExtraction.objects.count(), 2)  # the first one and its result are untouched history
        self.assertEqual(TextExtractionResult.objects.filter(extraction_id=item.id).count(), 1)

    def test_a_failed_extraction_can_be_requested_again_and_the_failed_one_stays(self):
        document = self.upload()
        item, _ = self.extract(document, Scripted(output=rules.failed_output(rules.ERR_MALFORMED, extractor_version=VERSION)))
        self.assertEqual(self.request(document).item.status, "queued")
        self.assertEqual(TextExtraction.objects.count(), 2)
        self.assertEqual(TextExtractionResult.objects.get(extraction_id=item.id).status, "failed")

    def test_a_refused_request_queues_nothing_and_records_nothing(self):
        document = self.upload()
        for user, error in ((self.other_operator, "out_of_scope"), (self.auditor, "missing_permission")):
            with self.subTest(user=user.username), self.assertRaises(AuthorizationError) as caught:
                services.request_text_extraction(user=user, document_id=document.pk)
            self.assertEqual(caught.exception.reason, error)
        with self.assertRaises(DocumentNotFoundError):
            services.request_text_extraction(user=self.operator, document_id=uuid.uuid4())
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count(), AuditEvent.objects.filter(action="document.text_requested").count()), (0, 0, 0))

    def test_a_failure_after_the_task_was_queued_rolls_everything_back(self):
        document = self.upload()
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")), self.assertRaises(RuntimeError):
            self.request(document)
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count(), AuditEvent.objects.filter(action="document.text_requested").count()), (0, 0, 0))

    def test_the_document_row_is_not_changed_by_a_request_or_an_extraction(self):
        document = self.upload()
        before = Document.objects.filter(pk=document.pk).values().get()
        self.extract(document)
        self.assertEqual(Document.objects.filter(pk=document.pk).values().get(), before)  # not even processing_state


@tag("requires_postgres")
@requires_postgres
class WorkerExecutionTests(TextCase):
    def test_the_worker_stores_the_result_the_pages_and_one_audit_event_and_completes_the_task(self):
        document = self.upload()
        item, outcome = self.extract(document)
        self.assertEqual((outcome.status, outcome.kind), (worker.ProcessStatus.SUCCEEDED, "documents.extract_text"))
        result = TextExtractionResult.objects.get(extraction_id=item.id)
        self.assertEqual(
            (result.status, result.error_code, result.extractor_name, result.extractor_version, result.page_count, result.char_count, result.source_sha256, result.source_size_bytes),
            ("extracted", "", "pypdf", VERSION, 3, len(SENTINEL) + len("second page"), document.sha256, document.size_bytes),
        )
        pages = list(TextExtractionPage.objects.filter(result=result).order_by("page_number"))
        self.assertEqual([(p.page_number, p.status, p.error_code, p.text, p.char_count) for p in pages], [(1, "text", "", SENTINEL, len(SENTINEL)), (2, "text", "", "second page", 11), (3, "empty", "", "", 0)])
        self.assertEqual(result.text_sha256, scripted_output(SENTINEL, "second page", "").text_sha256)
        task = Task.objects.get()
        self.assertEqual((task.state, task.result, task.attempt_count), ("succeeded", {"status": "extracted", "error_code": None, "page_count": 3}, 1))
        self.assertEqual(TaskAttempt.objects.get().outcome, "succeeded")
        (event,) = events_of("document.text_extracted")
        self.assertEqual((event.actor_kind, event.actor_public_id, event.subject_id), ("system", None, document.pk))
        self.assertEqual(event.details, {"extraction_id": str(item.id), "status": "extracted", "error_code": None, "page_count": 3, "char_count": result.char_count, "content_hash": result.text_sha256, "extractor_version": VERSION})

    def test_no_text_reaches_the_task_the_audit_log_or_the_attempt(self):
        self.extract()
        everything = repr(list(Task.objects.values())) + repr(list(TaskAttempt.objects.values())) + repr(list(AuditEvent.objects.values()))
        for forbidden in (SENTINEL, "second page", "Hello"):
            self.assertNotIn(forbidden, everything)

    def test_the_extractor_gets_exactly_the_stored_bytes_and_the_original_is_untouched(self):
        data = pdf_pages("Alpha", "Beta")
        document = self.upload(data=data)
        path = os.path.join(self.root, "objects", *document.storage_key.split("/"))
        extractor = Scripted()
        self.extract(document, extractor)
        (passed, limits), = extractor.calls
        self.assertEqual((passed, limits), (data, LIMITS))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_a_document_that_cannot_be_extracted_is_a_failed_result_and_a_successful_task(self):
        for output, code in ((rules.failed_output(rules.ERR_ENCRYPTED, extractor_version=VERSION), "encrypted_pdf"), (rules.failed_output(rules.ERR_TIMEOUT, extractor_version=VERSION), "extraction_timeout")):
            with self.subTest(code=code):
                document = self.upload()
                item, outcome = self.extract(document, Scripted(output=output))
                self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
                result = TextExtractionResult.objects.get(extraction_id=item.id)
                self.assertEqual((result.status, result.error_code, result.char_count, result.page_count), ("failed", code, 0, None))
                self.assertEqual(TextExtractionPage.objects.filter(result=result).count(), 0)
                self.assertEqual(Task.objects.get(pk=TextExtraction.objects.get(pk=item.id).task_id).state, "succeeded")

    def test_empty_partial_and_text_results_store_their_pages(self):
        cases = (
            (rules.output_from_pages([PageText(1, "empty", "", "")], extractor_version=VERSION), "empty", "no_text", 1),
            (rules.output_from_pages([], extractor_version=VERSION), "empty", "no_pages", 0),
            (rules.output_from_pages([PageText(1, "text", "", "x"), PageText(2, "failed", "page_extraction_failed", "")], extractor_version=VERSION), "partial", "some_pages_failed", 2),
        )
        for output, status, code, pages in cases:
            with self.subTest(status=status, code=code):
                item, _ = self.extract(self.upload(), Scripted(output=output))
                result = TextExtractionResult.objects.get(extraction_id=item.id)
                self.assertEqual((result.status, result.error_code, result.page_count), (status, code, pages))
                self.assertEqual(TextExtractionPage.objects.filter(result=result).count(), pages)

    def test_a_requester_who_lost_the_permission_gets_a_failed_result_and_no_byte_is_read(self):
        document = self.upload()
        self.request(document)
        self.operator.groups.clear()
        opened = []
        extractor = self.install(store_factory=lambda: opened.append("store") or self.store())
        outcome = self.run_worker()
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((opened, extractor.calls), ([], []))
        result = TextExtractionResult.objects.get()
        self.assertEqual((result.status, result.error_code, TextExtractionPage.objects.count()), ("failed", "not_authorized", 0))

    def test_a_deactivated_requester_is_refused_too(self):
        document = self.upload()
        self.request(document)
        User.objects.filter(pk=self.operator.pk).update(is_active=False)
        self.install()
        self.run_worker()
        self.assertEqual(TextExtractionResult.objects.get().error_code, "not_authorized")

    def test_a_missing_object_is_a_failed_result(self):
        document = self.upload()
        self.request(document)
        os.unlink(os.path.join(self.root, "objects", *document.storage_key.split("/")))
        extractor = self.install()
        self.assertEqual(self.run_worker().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((TextExtractionResult.objects.get().error_code, extractor.calls), ("document_missing", []))

    def test_an_object_that_no_longer_matches_its_recorded_hash_is_a_failed_result(self):
        data = pdf_pages("Alpha")
        document = self.upload(data=data)
        self.request(document)
        path = os.path.join(self.root, "objects", *document.storage_key.split("/"))
        altered = bytearray(data)
        altered[20] ^= 0x01
        with open(path, "wb") as handle:
            handle.write(altered)
        extractor = self.install()
        self.run_worker()
        self.assertEqual((TextExtractionResult.objects.get().error_code, extractor.calls), ("integrity_mismatch", []))

    def test_a_document_over_the_extraction_size_limit_is_a_failed_result_and_is_never_handed_to_the_extractor(self):
        small = self.upload(data=pdf_pages("tiny"))
        large = self.upload(data=build_pdf([tuple(f"line {n}" for n in range(200))]))
        self.assertLess(small.size_bytes, 1024)
        self.assertGreater(large.size_bytes, 1024)
        small_item, large_item = self.request(small).item, self.request(large).item
        extractor = self.install(limits=ExtractionLimits(max_bytes=1024))
        while self.run_worker().status != worker.ProcessStatus.IDLE:
            pass
        self.assertEqual(TextExtractionResult.objects.get(extraction_id=large_item.id).error_code, "document_too_large")
        self.assertEqual(TextExtractionResult.objects.get(extraction_id=small_item.id).status, "extracted")
        self.assertEqual(len(extractor.calls), 1)  # only the small document reached the extractor

    def test_an_unknown_request_is_a_permanent_failure(self):
        task = task_services.enqueue_task(kind="documents.extract_text", payload={"extraction_id": str(uuid.uuid4())}, registry=DOCUMENT_TEXT_REGISTRY).task
        self.install()
        outcome = self.run_worker()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "rejected"))
        self.assertEqual(Task.objects.get(pk=task.pk).state, "failed")
        with self.assertRaises(ExtractionRecordMissingError):
            execution.run_text_extraction(uuid.uuid4(), self.runtime())

    def test_a_worker_without_the_runtime_fails_the_task_permanently_and_stores_no_result(self):
        document = self.upload()
        self.request(document)
        handlers.install_runtime(None)
        outcome = self.run_worker()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.FAILED, "rejected"))
        self.assertEqual(TextExtractionResult.objects.count(), 0)


@tag("requires_postgres")
@requires_postgres
class RetryAndRecoveryTests(TextCase):
    def test_a_repeated_run_returns_the_stored_result_and_stores_nothing_again(self):
        document = self.upload()
        item = self.request(document).item
        extractor = Scripted()
        first = execution.run_text_extraction(item.id, self.runtime(extractor))
        second = execution.run_text_extraction(item.id, self.runtime(extractor))
        self.assertEqual((first.created, second.created), (True, False))
        self.assertEqual(first.as_task_result(), second.as_task_result())
        self.assertEqual((TextExtractionResult.objects.count(), TextExtractionPage.objects.count(), len(events_of("document.text_extracted"))), (1, 3, 1))
        self.assertEqual(len(extractor.calls), 1)  # the repeat did not even read the document

    def test_a_crash_between_the_result_and_the_queues_bookkeeping_is_recovered_without_a_duplicate(self):
        document = self.upload()
        item = self.request(document).item
        now = timezone.now()
        claimed = task_services.claim_next_task(worker_id="w-1", lease_seconds=60, now=now)
        self.assertEqual(claimed.kind, "documents.extract_text")
        execution.run_text_extraction(item.id, self.runtime())  # the result is committed ... and the worker dies before complete_task
        recovered = task_services.recover_expired_leases(now=now + timedelta(seconds=120))
        self.assertEqual((recovered.requeued, recovered.failed), (1, 0))
        self.install()
        outcome = self.run_worker(hours=1)
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((TextExtractionResult.objects.count(), TextExtractionPage.objects.count(), len(events_of("document.text_extracted"))), (1, 3, 1))
        task = Task.objects.get()
        self.assertEqual((task.state, task.attempt_count, task.result["status"]), ("succeeded", 2, "extracted"))
        self.assertEqual(sorted(a.outcome for a in TaskAttempt.objects.all()), ["abandoned", "succeeded"])

    def test_a_stale_worker_that_reaches_the_commit_late_discards_its_work(self):
        document = self.upload()
        item = self.request(document).item
        job_a = execution._begin(item.id)
        job_b = execution._begin(item.id)  # a second worker took over and started too
        winner = execution._commit(item.id, scripted_output("first"))
        loser = execution._commit(item.id, scripted_output("second, different"))
        self.assertEqual((winner.created, loser.created), (True, False))
        self.assertEqual(TextExtractionPage.objects.get().text, "first")  # the first committed result stands and is never replaced
        self.assertEqual((job_a.extraction_id, job_b.extraction_id), (item.id, item.id))
        self.assertEqual(len(events_of("document.text_extracted")), 1)

    def test_a_storage_that_cannot_be_used_is_retried_and_stores_nothing(self):
        document = self.upload()
        item = self.request(document).item
        self.install(store_factory=lambda: (_ for _ in ()).throw(DocumentStorageError(CODE_DISABLED)))
        outcome = self.run_worker()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "temporary_failure"))
        self.assertEqual((TextExtractionResult.objects.count(), Task.objects.get().state, services.get_text_extraction(user=self.operator, document_id=document.pk, extraction_id=item.id).status), (0, "queued", "queued"))

    def test_an_unavailable_extractor_is_retried_then_recorded_as_unavailable_when_the_attempts_run_out(self):
        document = self.upload()
        item = self.request(document).item
        self.install(Scripted(error=ExtractorUnavailableError()))
        statuses = [self.run_worker(hours=n * 2).status for n in range(1, 4)]
        self.assertEqual(statuses, [worker.ProcessStatus.REQUEUED, worker.ProcessStatus.REQUEUED, worker.ProcessStatus.FAILED])
        task = Task.objects.get()
        self.assertEqual((task.state, task.failure_code, task.attempt_count), ("failed", "temporary_failure", 3))
        self.assertEqual(TextExtractionResult.objects.count(), 0)
        self.assertEqual(services.get_text_extraction(user=self.operator, document_id=document.pk, extraction_id=item.id).error_code, "extraction_unavailable")  # reported at once
        self.assertEqual(execution.reconcile_failed_extractions(), 1)
        result = TextExtractionResult.objects.get()
        self.assertEqual((result.status, result.error_code, result.page_count, result.char_count, TextExtractionPage.objects.count()), ("failed", "extraction_unavailable", None, 0, 0))
        (event,) = events_of("document.text_extracted")
        self.assertEqual((event.details["status"], event.details["error_code"], event.details["page_count"]), ("failed", "extraction_unavailable", None))
        self.assertEqual(execution.reconcile_failed_extractions(), 0)  # idempotent
        self.assertEqual(len(events_of("document.text_extracted")), 1)
        self.assertTrue(services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION).created)  # and the document can be tried again

    def test_the_reconciler_leaves_pending_extractions_and_finished_ones_alone(self):
        document = self.upload()
        self.request(document)
        self.assertEqual(execution.reconcile_failed_extractions(), 0)  # queued
        self.install()
        self.run_worker()
        self.assertEqual(execution.reconcile_failed_extractions(), 0)  # succeeded
        self.assertEqual(TextExtractionResult.objects.count(), 1)

    def test_the_maintenance_hook_in_the_composition_closes_a_failed_extraction(self):
        from config import task_registry

        document = self.upload()
        self.request(document)
        Task.objects.update(state="failed", failure_code="temporary_failure", attempt_count=3, finished_at=timezone.now())
        hook = next(h for h in task_registry.COMPOSITION.maintenance_hooks if h.hook_id == "documents.reconcile_text")
        hook.run()
        self.assertEqual(TextExtractionResult.objects.get().error_code, "extraction_unavailable")


@tag("requires_postgres")
@requires_postgres
class RealExtractorEndToEndTests(TextCase):
    def test_a_real_pdf_goes_through_the_api_the_queue_the_isolated_process_and_back(self):
        browser = base.Browser()
        self.assertEqual(browser.login(self.operator.username).status_code, 200)
        data = pdf_pages("Invoice 4711", "Total due: 42 EUR")
        document_id = put_document(browser, data).json()["id"]
        created = browser.post(f"/api/documents/{document_id}/text-extractions/")
        self.assertEqual((created.status_code, created.json()["status"]), (201, "queued"))
        extraction_id = created.json()["id"]
        not_ready = browser.get(f"/api/documents/{document_id}/text-extractions/{extraction_id}/text/")
        self.assertEqual((not_ready.status_code, not_ready.json()["error"]["code"]), (409, "extraction_not_ready"))
        handlers.install_runtime(ExtractionRuntime(self.store, LIMITS, IsolatedExtractor()))
        self.assertEqual(self.run_worker().status, worker.ProcessStatus.SUCCEEDED)
        status = browser.get(f"/api/documents/{document_id}/text-extractions/{extraction_id}/").json()
        self.assertEqual((status["status"], status["page_count"], status["extractor"]["name"], status["source"]["document_id"]), ("extracted", 2, "pypdf", document_id))
        text = browser.get(f"/api/documents/{document_id}/text-extractions/{extraction_id}/text/").json()
        self.assertEqual([p["text"] for p in text["pages"]], ["Invoice 4711", "Total due: 42 EUR"])
        self.assertEqual(text["pages"][0]["source_reference"], rules.source_reference(uuid.UUID(document_id), Document.objects.get(pk=document_id).sha256, 1))

    def test_an_encrypted_upload_is_a_failed_result_with_its_code(self):
        document = self.upload(data=build_pdf([("secret",)], encrypt=True))  # the DOC-1 envelope check accepts it: it does not look for encryption
        item = self.request(document).item
        handlers.install_runtime(ExtractionRuntime(self.store, LIMITS, IsolatedExtractor()))
        self.assertEqual(self.run_worker().status, worker.ProcessStatus.SUCCEEDED)
        result = TextExtractionResult.objects.get(extraction_id=item.id)
        self.assertEqual((result.status, result.error_code, result.char_count, TextExtractionPage.objects.count()), ("failed", "encrypted_pdf", 0, 0))


@tag("requires_postgres")
@requires_postgres
class ConstraintAndImmutabilityTests(TextCase):
    def finished(self):
        document = self.upload()
        item, _ = self.extract(document)
        return document, TextExtraction.objects.get(pk=item.id), TextExtractionResult.objects.get(extraction_id=item.id)

    def rejected(self, **fields):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TextExtractionResult.objects.create(**fields)

    def test_the_database_refuses_a_second_result_for_one_request(self):
        _, extraction, result = self.finished()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TextExtractionResult.objects.create(extraction=extraction, status="empty", error_code="no_text", extractor_name="pypdf", extractor_version=VERSION, page_count=0, char_count=0, source_sha256="0" * 64, source_size_bytes=1, text_sha256="0" * 64)

    def test_the_database_refuses_a_second_request_for_one_task(self):
        document, extraction, _ = self.finished()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TextExtraction.objects.create(document=document, requested_by=self.operator, task=extraction.task)

    def test_the_database_refuses_inconsistent_results(self):
        _, _, result = self.finished()
        good = dict(extractor_name="pypdf", extractor_version=VERSION, source_sha256="0" * 64, source_size_bytes=1, text_sha256="0" * 64)
        for fields in (
            dict(status="other", error_code="x", page_count=0, char_count=0),
            dict(status="extracted", error_code="no_text", page_count=1, char_count=1),  # an extraction with a code
            dict(status="empty", error_code="", page_count=0, char_count=0),  # no code on a result that is not an extraction
            dict(status="empty", error_code="No Text", page_count=0, char_count=0),  # a code of the wrong shape
            dict(status="empty", error_code="no_text", page_count=None, char_count=0),  # only a failed result may lack a page count
            dict(status="failed", error_code="malformed_pdf", page_count=None, char_count=5),  # a failed result has no text
            dict(status="empty", error_code="no_text", page_count=0, char_count=0, source_sha256="XYZ"),
            dict(status="empty", error_code="no_text", page_count=0, char_count=0, text_sha256="A" * 64),
        ):
            with self.subTest(fields=fields):
                other = self.request(self.upload()).item
                self.rejected(extraction=TextExtraction.objects.get(pk=other.id), **{**good, **fields})

    def test_the_database_refuses_inconsistent_pages(self):
        _, _, result = self.finished()
        for fields in (
            dict(page_number=9, status="other", error_code="", text="", char_count=0),
            dict(page_number=9, status="failed", error_code="", text="", char_count=0),  # a failed page needs a code
            dict(page_number=9, status="failed", error_code="page_extraction_failed", text="x", char_count=1),  # and has no text
            dict(page_number=9, status="text", error_code="page_extraction_failed", text="x", char_count=1),  # a page with text has no code
            dict(page_number=0, status="text", error_code="", text="x", char_count=1),
            dict(page_number=1, status="text", error_code="", text="x", char_count=1),  # page 1 exists
            dict(page_number=9, status="failed", error_code="Not A Code", text="", char_count=0),
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                TextExtractionPage.objects.create(result=result, **fields)

    def test_nothing_can_be_changed_or_removed_through_the_orm(self):
        _, extraction, result = self.finished()
        page = TextExtractionPage.objects.filter(result=result).first()
        for row in (extraction, result, page):
            with self.subTest(row=type(row).__name__):
                with self.assertRaises(ImmutableRecordError):
                    row.save()
                with self.assertRaises(ImmutableRecordError):
                    row.delete()
                with self.assertRaises(ImmutableRecordError):
                    type(row).objects.filter(pk=row.pk).update(pk=row.pk)
                with self.assertRaises(ImmutableRecordError):
                    type(row).objects.all().delete()
        self.assertEqual(TextExtractionPage.objects.get(pk=page.pk).text, page.text)

    def test_the_history_cannot_be_orphaned(self):
        document, extraction, result = self.finished()
        for protected in (document, extraction, result, extraction.task, self.operator):
            with self.subTest(target=type(protected).__name__), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:  # raw SQL is the only way to try: the ORM refuses first. The foreign keys still hold.
                    cursor.execute(f"DELETE FROM {protected._meta.db_table} WHERE {protected._meta.pk.column} = %s", [protected.pk])
        with self.assertRaises(ProtectedError):
            User.objects.filter(pk=self.operator.pk).delete()

    def test_the_result_its_pages_and_its_audit_event_exist_together(self):
        document = self.upload()
        item = self.request(document).item
        self.install()
        self.assertEqual(TextExtractionResult.objects.count(), 0)
        self.run_worker()
        result = TextExtractionResult.objects.get(extraction_id=item.id)
        self.assertEqual((TextExtractionPage.objects.filter(result=result).count(), len(events_of("document.text_extracted"))), (result.page_count, 1))

    def test_a_failure_while_storing_the_pages_rolls_the_whole_result_back(self):
        document = self.upload()
        item = self.request(document).item
        with mock.patch.object(execution.TextExtractionPage.objects, "bulk_create", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            execution.run_text_extraction(item.id, self.runtime())
        self.assertEqual((TextExtractionResult.objects.count(), TextExtractionPage.objects.count(), len(events_of("document.text_extracted"))), (0, 0, 0))
        self.assertTrue(execution.run_text_extraction(item.id, self.runtime()).created)  # and a retry can still succeed

    def test_a_failed_audit_write_rolls_the_result_back(self):
        document = self.upload()
        item = self.request(document).item
        with mock.patch.object(execution, "record_event", side_effect=RuntimeError("audit down")), self.assertRaises(RuntimeError):
            execution.run_text_extraction(item.id, self.runtime())
        self.assertEqual((TextExtractionResult.objects.count(), TextExtractionPage.objects.count()), (0, 0))


@tag("requires_postgres")
@requires_postgres
class ApiPermissionTests(TextCase):
    def setUp(self):
        super().setUp()
        self.browsers = {}

    def browser(self, user):
        if user.username not in self.browsers:
            browser = base.Browser()
            assert browser.login(user.username).status_code == 200
            self.browsers[user.username] = browser
        return self.browsers[user.username]

    def finished_text(self):
        document = self.upload()
        item, _ = self.extract(document)
        return document, item

    def test_the_owner_an_approver_and_a_superuser_may_request(self):
        for user in (self.operator, self.approver, self.superuser):
            with self.subTest(user=user.username):
                document = self.upload()
                created = self.browser(user).post(f"/api/documents/{document.pk}/text-extractions/")
                self.assertEqual(created.status_code, 201, created.content)

    def test_the_auditor_who_may_view_metadata_may_neither_request_nor_read_extracted_text(self):
        document, item = self.finished_text()
        browser = self.browser(self.auditor)
        self.assertEqual(browser.get(f"/api/documents/{document.pk}/").status_code, 200)  # metadata is theirs
        for method, path in (("post", f"/api/documents/{document.pk}/text-extractions/"), ("get", f"/api/documents/{document.pk}/text-extractions/"), ("get", f"/api/documents/{document.pk}/text-extractions/{item.id}/"), ("get", f"/api/documents/{document.pk}/text-extractions/{item.id}/text/")):
            with self.subTest(path=path):
                response = getattr(browser, method)(path)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))

    def test_another_operator_gets_the_same_404_as_for_an_unknown_document_and_never_the_text(self):
        document, item = self.finished_text()
        other = self.browser(self.other_operator)
        responses = [
            other.post(f"/api/documents/{document.pk}/text-extractions/"),
            other.get(f"/api/documents/{document.pk}/text-extractions/"),
            other.get(f"/api/documents/{document.pk}/text-extractions/{item.id}/"),
            other.get(f"/api/documents/{document.pk}/text-extractions/{item.id}/text/"),
        ]
        unknown = other.get(f"/api/documents/{uuid.uuid4()}/text-extractions/{item.id}/text/")
        for response in responses:
            self.assertEqual((response.status_code, response.json()), (404, unknown.json()))
            self.assertNotIn(SENTINEL, response.content.decode())

    def test_an_extraction_is_reachable_only_through_its_own_document(self):
        document, item = self.finished_text()
        other_document = self.upload()
        response = self.browser(self.operator).get(f"/api/documents/{other_document.pk}/text-extractions/{item.id}/text/")
        self.assertEqual(response.status_code, 404)

    def test_the_status_routes_never_carry_text_and_the_text_route_records_a_read(self):
        document, item = self.finished_text()
        browser = self.browser(self.operator)
        for path in (f"/api/documents/{document.pk}/text-extractions/", f"/api/documents/{document.pk}/text-extractions/{item.id}/"):
            self.assertNotIn(SENTINEL, browser.get(path).content.decode())
        self.assertEqual(events_of("document.text_read"), [])
        response = browser.get(f"/api/documents/{document.pk}/text-extractions/{item.id}/text/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["text"] for p in response.json()["pages"]], [SENTINEL, "second page", ""])
        (event,) = events_of("document.text_read")
        self.assertEqual((event.actor_public_id, event.subject_id, event.details), (self.operator.public_id, document.pk, {"extraction_id": str(item.id), "page_count": 3, "char_count": len(SENTINEL) + 11, "by_owner": True}))
        self.browser(self.approver).get(f"/api/documents/{document.pk}/text-extractions/{item.id}/text/")
        self.assertEqual([e.details["by_owner"] for e in events_of("document.text_read")], [True, False])
        self.assertNotIn(SENTINEL, repr(list(AuditEvent.objects.values())))

    def test_a_failed_audit_write_returns_no_text(self):
        document, item = self.finished_text()
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")):
            response = self.browser(self.operator).get(f"/api/documents/{document.pk}/text-extractions/{item.id}/text/")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SENTINEL, response.content.decode())
        self.assertEqual(events_of("document.text_read"), [])

    def test_a_post_without_a_csrf_token_is_refused_and_queues_nothing(self):
        document = self.upload()
        response = self.browser(self.operator).post(f"/api/documents/{document.pk}/text-extractions/", csrf=False)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "csrf_failed"))
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count()), (0, 0))

    def test_an_anonymous_caller_is_refused_everywhere(self):
        document, item = self.finished_text()
        anonymous = base.Browser()
        for method, path in (("post", f"/api/documents/{document.pk}/text-extractions/"), ("get", f"/api/documents/{document.pk}/text-extractions/"), ("get", f"/api/documents/{document.pk}/text-extractions/{item.id}/text/")):
            response = getattr(anonymous, method)(path)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))

    def test_the_listing_is_newest_first_and_paginated(self):
        document = self.upload()
        browser = self.browser(self.operator)
        first = self.request(document).item
        self.install(Scripted(output=rules.failed_output(rules.ERR_MALFORMED, extractor_version=VERSION)))
        self.run_worker()
        second = self.request(document).item
        body = browser.get(f"/api/documents/{document.pk}/text-extractions/", page_size=1).json()
        self.assertEqual((body["count"], [r["id"] for r in body["results"]]), (2, [str(second.id)]))
        self.assertEqual([r["id"] for r in browser.get(f"/api/documents/{document.pk}/text-extractions/", page=2, page_size=1).json()["results"]], [str(first.id)])

    def test_the_request_is_throttled(self):
        override = override_settings(API_THROTTLE_RATES={**RATES, "document_text": "2/min"})
        override.enable()
        self.addCleanup(override.disable)
        document = self.upload()
        browser = self.browser(self.operator)
        codes = [browser.post(f"/api/documents/{document.pk}/text-extractions/").status_code for _ in range(3)]
        self.assertEqual(codes, [201, 200, 429])
        self.assertEqual(TextExtraction.objects.count(), 1)

    def test_reading_the_text_of_an_extraction_that_has_no_result_is_a_conflict(self):
        document = self.upload()
        item = self.request(document).item
        with self.assertRaises(ExtractionNotReadyError):
            services.read_extracted_text(user=self.operator, document_id=document.pk, extraction_id=item.id)
        self.assertEqual(events_of("document.text_read"), [])


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(StorageMixin, DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        override = override_settings(API_THROTTLE_RATES=RATES, **base.FAST_HASHERS)
        override.enable()
        self.addCleanup(override.disable)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = base.make_user("olive", OPERATOR)
        self.approver = base.make_user("anna", APPROVER)
        self.use_storage()
        self.addCleanup(handlers.install_runtime, handlers._runtime)
        self.addCleanup(setattr, worker, "_composition", worker._composition)

    def store(self):
        return DocumentStore(self.root, PermissivePolicy(), max_bytes=20 * 1024 * 1024)

    def upload(self, data=None):
        data = data or pdf_pages("Hello")
        item = document_services.upload_document(user=self.operator, filename_header="a.pdf", content_type="application/pdf", content_length=str(len(data)), read=io.BytesIO(data).read, store_factory=self.store)
        return Document.objects.get(pk=item.id)

    def test_concurrent_requests_for_one_document_create_exactly_one_extraction_and_one_task(self):
        document = self.upload()
        users = [self.operator, self.approver] * 3
        results = run_threads(self, [lambda u=u: services.request_text_extraction(user=u, document_id=document.pk, extractor_version=VERSION) for u in users])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        outcomes = [value for _, value in results]
        self.assertEqual(sum(o.created for o in outcomes), 1)
        self.assertEqual(len({o.item.id for o in outcomes}), 1)
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count(), len(events_of("document.text_requested"))), (1, 1, 1))

    def test_concurrent_requests_for_different_documents_do_not_block_each_other(self):
        documents = [self.upload(pdf_pages(f"Doc {n}")) for n in range(4)]
        results = run_threads(self, [lambda d=d: services.request_text_extraction(user=self.operator, document_id=d.pk, extractor_version=VERSION) for d in documents])
        self.assertTrue(all(kind == "ok" and value.created for kind, value in results), results)
        self.assertEqual((TextExtraction.objects.count(), Task.objects.count()), (4, 4))

    def test_concurrent_runs_of_one_extraction_store_exactly_one_result_and_one_audit_event(self):
        document = self.upload()
        item = services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION).item
        runtime = ExtractionRuntime(self.store, LIMITS, Scripted())
        results = run_threads(self, [lambda: execution.run_text_extraction(item.id, runtime) for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(value.created for _, value in results), 1)
        self.assertEqual(len({value.as_task_result()["status"] for _, value in results}), 1)
        self.assertEqual((TextExtractionResult.objects.count(), TextExtractionPage.objects.count(), len(events_of("document.text_extracted"))), (1, 3, 1))

    def test_two_workers_claim_two_extractions_and_never_the_same_one(self):
        documents = [self.upload(pdf_pages(f"Doc {n}")) for n in range(2)]
        for document in documents:
            services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION)
        handlers.install_runtime(ExtractionRuntime(self.store, LIMITS, Scripted()))
        results = run_threads(self, [lambda n=n: worker.process_one(worker_id=f"w-{n}", lease_seconds=60, registry=DOCUMENT_TEXT_REGISTRY) for n in range(2)])
        self.assertTrue(all(kind == "ok" and value.status == worker.ProcessStatus.SUCCEEDED for kind, value in results), results)
        self.assertEqual(len({value.task_id for _, value in results}), 2)
        self.assertEqual((TextExtractionResult.objects.count(), Task.objects.filter(state="succeeded").count()), (2, 2))

    def test_concurrent_reconcilers_close_a_failed_extraction_once(self):
        document = self.upload()
        item = services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION).item
        Task.objects.update(state="failed", failure_code="temporary_failure", attempt_count=3, finished_at=timezone.now())
        results = run_threads(self, [execution.reconcile_failed_extractions for _ in range(4)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(value for _, value in results), 1)
        self.assertEqual((TextExtractionResult.objects.filter(extraction_id=item.id).count(), len(events_of("document.text_extracted"))), (1, 1))

    def test_a_request_racing_a_finished_extraction_returns_it_and_creates_nothing(self):
        document = self.upload()
        item = services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION).item
        execution.run_text_extraction(item.id, ExtractionRuntime(self.store, LIMITS, Scripted()))
        results = run_threads(self, [lambda: services.request_text_extraction(user=self.operator, document_id=document.pk, extractor_version=VERSION) for _ in range(4)])
        self.assertTrue(all(kind == "ok" and not value.created and value.item.id == item.id for kind, value in results), results)
        self.assertEqual(TextExtraction.objects.count(), 1)


@tag("requires_postgres")
@requires_postgres
class MigrationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def test_the_migration_creates_exactly_the_three_tables_and_its_constraints_exist_in_the_database(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name LIKE 'document_text_%' ORDER BY 1")
            self.assertEqual([row[0] for row in cursor.fetchall()], ["document_text_textextraction", "document_text_textextractionpage", "document_text_textextractionresult"])
            cursor.execute("SELECT conname FROM pg_constraint WHERE conname LIKE 'doctext_%' ORDER BY 1")
            names = [row[0] for row in cursor.fetchall()]
        for expected in ("doctext_result_status_valid", "doctext_result_code_matches_status", "doctext_page_number_unique", "doctext_page_code_matches_status", "doctext_result_hash_shape"):
            self.assertIn(expected, names)

    def test_the_text_is_a_text_column_and_nothing_stores_bytes(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'document_text_textextractionpage' AND column_name = 'text'")
            self.assertEqual(cursor.fetchone()[0], "text")
            cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_name LIKE 'document_text_%' AND data_type = 'bytea'")
            self.assertEqual(cursor.fetchone()[0], 0)
