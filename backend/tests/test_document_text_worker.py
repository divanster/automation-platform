"""DB-free tests of the worker side of text extraction (DOC-2): the task handler and its failure mapping, the reading and verification of the stored object, the order of one run's steps and what makes it
idempotent, the composition (kind gate, runtime, limits, lease) and the reconciler's guards. The database touch points (``_begin``, ``_commit``) are replaced; what needs a real database (the row locks, the
one-to-one result, a true retry, concurrent runs) is in ``tests/test_document_text_postgres.py`` and is NOT run in the development VM. Every document is synthetic."""

import hashlib
import json
import os
import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.document_text import execution, handlers, rules
from apps.document_text.errors import ExtractionConfigurationError, ExtractionRecordMissingError
from apps.document_text.execution import ExtractionRuntime, ExtractionSummary
from apps.document_text.isolation import ExtractorUnavailableError, IsolatedExtractor
from apps.document_text.rules import ExtractionLimits
from apps.documents.errors import CODE_DISABLED, CODE_OBJECT_MISSING, CODE_UNAVAILABLE, DocumentStorageError
from apps.tasks import policy, worker
from apps.tasks.services import ClaimedTask
from config import document_extraction, task_registry
from tests.documents_support import Reader, temporary_store
from tests.pdf_support import build_pdf

EXTRACTION = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DOCUMENT = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
VERSION = "7.1.1"
SENTINEL = "TEXT-SENTINEL-71c4"


def summary(created=True, status="extracted", code="", pages=1):
    return ExtractionSummary(EXTRACTION, status, code, pages, created)


def job(data=b"%PDF-demo", *, authorized=True, size=None, sha=None, key="ab/" + "c" * 32):
    return execution._Job(EXTRACTION, DOCUMENT, key, sha or hashlib.sha256(data).hexdigest(), len(data) if size is None else size, authorized)


class FakeStream:
    def __init__(self, chunks, fail=None):
        self.chunks, self.fail, self.closed = chunks, fail, False

    def __iter__(self):
        yield from self.chunks
        if self.fail:
            raise self.fail

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self, stream=None, error=None):
        self.stream, self.error, self.opened = stream, error, []

    def open(self, key, size):
        self.opened.append((key, size))
        if self.error:
            raise self.error
        return self.stream


def runtime(store=None, extractor=None, limits=None):
    return ExtractionRuntime(store_factory=lambda: store, limits=limits or ExtractionLimits(max_bytes=1024), extractor=extractor or mock.Mock(name="extractor"))


class PayloadTests(SimpleTestCase):
    def test_the_payload_is_exactly_one_canonical_uuid(self):
        self.assertEqual(handlers.validate_payload({"extraction_id": str(EXTRACTION)}), {"extraction_id": str(EXTRACTION)})
        for bad in (None, [], "x", {}, {"extraction_id": 5}, {"extraction_id": "not-a-uuid"}, {"extraction_id": str(EXTRACTION).upper()}, {"extraction_id": EXTRACTION.hex}, {"extraction_id": str(EXTRACTION), "extra": 1},
                    {"extraction_id": str(EXTRACTION) + " "}, {"document": str(DOCUMENT)}, {"extraction_id": None}):  # fmt: skip
            with self.subTest(payload=bad), self.assertRaises(policy.InvalidPayloadError):
                handlers.validate_payload(bad)

    def test_the_payload_and_key_never_hold_text_or_a_file_name(self):
        self.assertEqual(handlers.payload_for(EXTRACTION), {"extraction_id": str(EXTRACTION)})
        self.assertEqual(handlers.idempotency_key_for(EXTRACTION), f"documents.extract_text:{EXTRACTION}")
        policy.validate_idempotency_key(handlers.idempotency_key_for(EXTRACTION))  # a key the queue accepts
        self.assertLessEqual(len(policy.canonical_json(handlers.payload_for(EXTRACTION), max_bytes=policy.MAX_PAYLOAD_BYTES, what="payload")), policy.MAX_PAYLOAD_BYTES)

    def test_the_handler_is_a_core_non_test_handler_with_the_fixed_kind(self):
        self.assertEqual(handlers.EXTRACT_TEXT_HANDLER.kind, "documents.extract_text")
        self.assertFalse(handlers.EXTRACT_TEXT_HANDLER.is_test_handler)
        from apps.document_text.registry import DOCUMENT_TEXT_REGISTRY

        self.assertEqual(dict(DOCUMENT_TEXT_REGISTRY), {"documents.extract_text": handlers.EXTRACT_TEXT_HANDLER})
        with self.assertRaises(TypeError):
            DOCUMENT_TEXT_REGISTRY["other.kind"] = handlers.EXTRACT_TEXT_HANDLER


class HandlerFailureMappingTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(handlers.install_runtime, handlers._runtime)
        handlers.install_runtime(runtime())

    def run_handler(self, **patch):
        with mock.patch.object(handlers, "run_text_extraction", **patch):
            return handlers.run_extract({"extraction_id": str(EXTRACTION)})

    def test_success_returns_the_small_summary_and_nothing_else(self):
        result = self.run_handler(return_value=summary())
        self.assertEqual(result, {"status": "extracted", "error_code": None, "page_count": 1})

    def test_a_failed_result_is_a_successful_task_with_the_code(self):
        result = self.run_handler(return_value=summary(status="failed", code="encrypted_pdf", pages=None))
        self.assertEqual(result, {"status": "failed", "error_code": "encrypted_pdf", "page_count": None})

    def test_an_unknown_request_is_permanent(self):
        with self.assertRaises(policy.TaskPermanentError) as caught:
            self.run_handler(side_effect=ExtractionRecordMissingError())
        self.assertEqual(caught.exception.code, policy.FailureCode.REJECTED)

    def test_a_missing_runtime_is_permanent_and_nothing_runs(self):
        handlers.install_runtime(None)
        with mock.patch.object(handlers, "run_text_extraction") as run, self.assertRaises(policy.TaskPermanentError):
            handlers.run_extract({"extraction_id": str(EXTRACTION)})
        run.assert_not_called()

    def test_storage_problems_and_an_unavailable_extractor_are_retryable(self):
        for error in (DocumentStorageError(CODE_DISABLED), DocumentStorageError(CODE_UNAVAILABLE), ExtractorUnavailableError()):
            with self.subTest(error=type(error).__name__), self.assertRaises(policy.TaskRetryableError) as caught:
                self.run_handler(side_effect=error)
            self.assertEqual(caught.exception.code, policy.FailureCode.TEMPORARY_FAILURE)

    def test_anything_else_propagates_to_the_queue_which_classifies_it_by_type_only(self):
        with self.assertRaises(RuntimeError):
            self.run_handler(side_effect=RuntimeError(SENTINEL))
        failure = policy.classify_exception(RuntimeError(SENTINEL))
        self.assertEqual((failure.code, failure.retryable), (policy.FailureCode.HANDLER_ERROR, True))
        self.assertNotIn(SENTINEL, repr(failure))

    def test_a_refused_runtime_type_is_not_installed(self):
        for bad in ("runtime", object(), {"limits": 1}):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                handlers.install_runtime(bad)

    def test_a_handler_log_line_holds_identifiers_and_codes_only(self):
        with self.assertLogs("apps.document_text.handlers", level="DEBUG") as captured:
            with self.assertRaises(policy.TaskRetryableError):
                self.run_handler(side_effect=DocumentStorageError(CODE_UNAVAILABLE))
            handlers.install_runtime(None)
            with self.assertRaises(policy.TaskPermanentError):
                handlers.run_extract({"extraction_id": str(EXTRACTION)})
        text = " ".join(captured.output)
        self.assertIn(str(EXTRACTION), text)
        for forbidden in ("Traceback", SENTINEL, "pdf", "storage_key"):
            self.assertNotIn(forbidden, text)


class WorkerBoundaryTests(SimpleTestCase):
    """The real worker loop code (``apps.tasks.worker._run_handler``) around the real handler."""

    def claimed(self, payload=None, kind="documents.extract_text"):
        return ClaimedTask(
            task_id=uuid.uuid4(), kind=kind, payload=payload if payload is not None else {"extraction_id": str(EXTRACTION)}, attempt_id=uuid.uuid4(), attempt_number=1, max_attempts=3,
            lease_token=uuid.uuid4(), lease_expires_at=None,
        )  # fmt: skip

    def setUp(self):
        self.addCleanup(handlers.install_runtime, handlers._runtime)
        handlers.install_runtime(runtime())

    def test_the_worker_revalidates_the_stored_payload_before_running(self):
        with mock.patch.object(handlers, "run_text_extraction") as run:
            result, failure = worker._run_handler(self.claimed({"extraction_id": "garbage"}), task_registry.WORKER_REGISTRY)
        run.assert_not_called()
        self.assertIsNone(result)
        self.assertEqual(failure.code, policy.FailureCode.INVALID_PAYLOAD)  # the queue's own code for a payload its handler refuses: the handler never ran

    def test_a_success_becomes_a_small_task_result(self):
        with mock.patch.object(handlers, "run_text_extraction", return_value=summary()):
            result, failure = worker._run_handler(self.claimed(), task_registry.WORKER_REGISTRY)
        self.assertEqual((result, failure), ({"status": "extracted", "error_code": None, "page_count": 1}, None))
        self.assertLess(len(json.dumps(result)), policy.MAX_RESULT_BYTES)

    def test_failures_reach_the_queue_as_its_closed_codes(self):
        cases = ((ExtractionRecordMissingError(), policy.FailureCode.REJECTED, False), (DocumentStorageError(CODE_UNAVAILABLE), policy.FailureCode.TEMPORARY_FAILURE, True), (ExtractorUnavailableError(), policy.FailureCode.TEMPORARY_FAILURE, True))
        for error, code, retryable in cases:
            with self.subTest(error=type(error).__name__), mock.patch.object(handlers, "run_text_extraction", side_effect=error):
                result, failure = worker._run_handler(self.claimed(), task_registry.WORKER_REGISTRY)
                self.assertEqual((result, failure.code, failure.retryable), (None, code, retryable))

    def test_the_handler_refuses_to_run_inside_a_transaction(self):
        with mock.patch.object(worker.connection, "in_atomic_block", True), mock.patch.object(handlers, "run_text_extraction") as run, self.assertRaises(RuntimeError):
            worker._run_handler(self.claimed(), task_registry.WORKER_REGISTRY)
        run.assert_not_called()

    def test_a_task_of_this_kind_is_not_in_the_queues_own_allowlist(self):
        from apps.tasks.handlers import HANDLERS

        self.assertNotIn("documents.extract_text", HANDLERS)  # apps.tasks knows no business code
        result, failure = worker._run_handler(self.claimed(), HANDLERS)
        self.assertEqual((result, failure.code), (None, policy.FailureCode.UNKNOWN_KIND))


class ReadObjectTests(SimpleTestCase):
    version = VERSION

    def read(self, job_, store, limits=None):
        return execution._read_object(job_, runtime(store, limits=limits), self.version)

    def test_the_bytes_are_returned_when_size_and_hash_match(self):
        data = b"%PDF-1.4 demo bytes"
        stream = FakeStream([data[:5], data[5:]])
        found, failure = self.read(job(data), FakeStore(stream))
        self.assertEqual((bytes(found), failure), (data, None))
        self.assertTrue(stream.closed)

    def test_a_document_over_the_extraction_size_limit_is_refused_without_opening_it(self):
        store = FakeStore(FakeStream([b"x"]))
        found, failure = self.read(job(b"x" * 10, size=5000), store, limits=ExtractionLimits(max_bytes=1024))
        self.assertIsNone(found)
        self.assertEqual((failure.status, failure.error_code), (rules.RESULT_FAILED, rules.ERR_DOCUMENT_TOO_LARGE))
        self.assertEqual(store.opened, [])

    def test_a_missing_object_is_a_failed_result_but_an_unusable_storage_is_an_error(self):
        found, failure = self.read(job(), FakeStore(error=DocumentStorageError(CODE_OBJECT_MISSING)))
        self.assertEqual((found, failure.error_code), (None, rules.ERR_DOCUMENT_MISSING))
        for code in (CODE_DISABLED, CODE_UNAVAILABLE):
            with self.subTest(code=code), self.assertRaises(DocumentStorageError) as caught:
                self.read(job(), FakeStore(error=DocumentStorageError(code)))
            self.assertEqual(caught.exception.code, code)

    def test_a_stream_of_another_length_is_a_missing_object(self):
        data = b"%PDF-1.4 demo bytes"
        for chunks in ([data[:-3]], [data, b"extra"], [], [data + b"x" * 5000]):
            with self.subTest(total=sum(map(len, chunks))):
                stream = FakeStream(chunks)
                found, failure = self.read(job(data), FakeStore(stream))
                self.assertEqual((found, failure.error_code), (None, rules.ERR_DOCUMENT_MISSING))
                self.assertTrue(stream.closed)

    def test_bytes_that_do_not_hash_to_the_recorded_value_are_an_integrity_failure(self):
        data = b"%PDF-1.4 demo bytes"
        tampered = b"%PDF-1.4 demo bytez"
        found, failure = self.read(job(data), FakeStore(FakeStream([tampered])))
        self.assertEqual((found, failure.error_code), (None, rules.ERR_INTEGRITY_MISMATCH))

    def test_a_read_error_is_an_unusable_storage_and_the_stream_is_closed(self):
        stream = FakeStream([b"%PDF"], fail=OSError("disk gone"))
        with self.assertRaises(DocumentStorageError) as caught:
            self.read(job(b"%PDF-1.4 demo bytes"), FakeStore(stream))
        self.assertEqual(caught.exception.code, CODE_UNAVAILABLE)
        self.assertTrue(stream.closed)
        self.assertNotIn("disk gone", str(caught.exception))

    def test_a_real_stored_document_is_read_unchanged_and_extracted(self):
        data = build_pdf([("Stored text",), ("Second",)])
        with temporary_store() as (store, root):
            stored = store.ingest(Reader(data).read, len(data))
            path = os.path.join(root, "objects", *stored.key.split("/"))
            before = open(path, "rb").read()
            found, failure = execution._read_object(job(data, key=stored.key), ExtractionRuntime(lambda: store, ExtractionLimits(), IsolatedExtractor()), VERSION)
            self.assertEqual((bytes(found), failure), (data, None))
            result = IsolatedExtractor()(found, ExtractionLimits())
            self.assertEqual([p.text for p in result.pages], ["Stored text", "Second"])
            self.assertEqual(open(path, "rb").read(), before)  # the original is read, never modified
            self.assertEqual(sorted(os.listdir(os.path.join(root, "incoming"))), [])  # and nothing was written beside it

    def test_a_real_object_that_was_altered_on_disk_is_refused(self):
        data = build_pdf([("Stored text",)])
        with temporary_store() as (store, root):
            stored = store.ingest(Reader(data).read, len(data))
            path = os.path.join(root, "objects", *stored.key.split("/"))
            altered = bytearray(data)
            altered[20] ^= 0x01  # one flipped bit, same size
            with open(path, "wb") as handle:
                handle.write(altered)
            found, failure = execution._read_object(job(data, key=stored.key), ExtractionRuntime(lambda: store, ExtractionLimits(), IsolatedExtractor()), VERSION)
            self.assertEqual((found, failure.error_code), (None, rules.ERR_INTEGRITY_MISMATCH))


class RunOrderTests(SimpleTestCase):
    """``run_text_extraction`` with the database steps replaced: the order, what is skipped, and what a repeat does."""

    def setUp(self):
        self.calls = []
        self.extractor = mock.Mock(side_effect=lambda data, limits: self.calls.append("extract") or rules.output_from_pages([rules.PageText(1, "text", "", "Hello")], extractor_version=VERSION))
        patcher = mock.patch.object(execution, "installed_version", return_value=VERSION)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, begin, store=None, **overrides):
        committed = []

        def commit(extraction_id, output):
            self.calls.append("commit")
            committed.append(output)
            return summary()

        with mock.patch.object(execution, "_begin", return_value=begin), mock.patch.object(execution, "_commit", side_effect=commit):
            result = execution.run_text_extraction(EXTRACTION, ExtractionRuntime(lambda: self.store(store), ExtractionLimits(max_bytes=4096), self.extractor))
        return result, committed

    def store(self, store):
        self.calls.append("storage")
        return store

    def test_an_existing_result_is_returned_and_nothing_else_happens(self):
        existing = summary(created=False)
        result, committed = self.run_with(existing)
        self.assertIs(result, existing)
        self.assertEqual((self.calls, committed), ([], []))  # no storage, no extraction, no second result
        self.extractor.assert_not_called()

    def test_a_requester_who_may_no_longer_read_the_document_gets_a_failed_result_and_no_byte_is_read(self):
        result, committed = self.run_with(job(authorized=False))
        self.assertEqual(self.calls, ["commit"])
        self.assertEqual((committed[0].status, committed[0].error_code), (rules.RESULT_FAILED, rules.ERR_NOT_AUTHORIZED))
        self.extractor.assert_not_called()

    def test_the_steps_run_in_order_storage_then_extraction_then_one_commit(self):
        data = b"%PDF-1.4 demo bytes"
        result, committed = self.run_with(job(data), FakeStore(FakeStream([data])))
        self.assertEqual(self.calls, ["storage", "extract", "commit"])
        self.assertEqual(len(committed), 1)
        self.assertEqual([p.text for p in committed[0].pages], ["Hello"])
        self.extractor.assert_called_once()
        passed, limits = self.extractor.call_args.args
        self.assertEqual((bytes(passed), limits.max_bytes), (data, 4096))

    def test_a_document_problem_is_committed_as_a_failed_result_without_extracting(self):
        data = b"%PDF-1.4 demo bytes"
        result, committed = self.run_with(job(data), FakeStore(FakeStream([b"%PDF-1.4 demo bytez"])))
        self.assertEqual(self.calls, ["storage", "commit"])
        self.assertEqual(committed[0].error_code, rules.ERR_INTEGRITY_MISMATCH)

    def test_a_problem_outside_the_document_stores_nothing(self):
        for error in (DocumentStorageError(CODE_UNAVAILABLE), ExtractorUnavailableError()):
            with self.subTest(error=type(error).__name__):
                self.calls.clear()
                data = b"%PDF-1.4 demo bytes"
                if isinstance(error, ExtractorUnavailableError):
                    self.extractor.side_effect = error
                    store = FakeStore(FakeStream([data]))
                else:
                    store = FakeStore(error=error)
                with self.assertRaises(type(error)):
                    self.run_with(job(data), store)
                self.assertNotIn("commit", self.calls)  # no result: the task is retried, and the reconciler closes it if the attempts run out

    def test_the_task_result_is_identical_for_a_first_run_and_every_repeat(self):
        first, repeat = summary(created=True), summary(created=False)
        self.assertEqual(first.as_task_result(), repeat.as_task_result())
        self.assertEqual(set(first.as_task_result()), {"status", "error_code", "page_count"})
        json.dumps(first.as_task_result())
        self.assertNotIn(SENTINEL, json.dumps(first.as_task_result()))

    def test_the_summary_is_built_from_the_stored_result_so_a_repeat_equals_the_original(self):
        stored = SimpleNamespace(extraction_id=EXTRACTION, status="partial", error_code="some_pages_failed", page_count=4)
        self.assertEqual(execution._summary(stored, created=True).as_task_result(), execution._summary(stored, created=False).as_task_result())
        self.assertEqual(execution._summary(stored, created=False), ExtractionSummary(EXTRACTION, "partial", "some_pages_failed", 4, False))

    def test_log_lines_hold_identifiers_and_a_status_only(self):
        with self.assertLogs("apps.document_text.execution", level="DEBUG") as captured:
            self.run_with(job(authorized=False))
        text = " ".join(captured.output)
        self.assertIn(str(EXTRACTION), text)
        for forbidden in ("Hello", SENTINEL, "storage_key", "sha256"):
            self.assertNotIn(forbidden, text)


class StillAuthorizedTests(SimpleTestCase):
    """``execution._still_authorized``: the requester is judged again, on a freshly loaded user, when the worker runs."""

    def check(self, actor=None, error=None, owner_pk=7):
        from apps.accounts.errors import AuthorizationError
        from tests.documents_support import OPERATOR

        document = SimpleNamespace(owner_id=owner_pk)
        loader = mock.Mock(side_effect=AuthorizationError(error)) if error else mock.Mock(return_value=actor or OPERATOR)
        with mock.patch.object(execution, "load_actor", loader):
            return execution._still_authorized(object(), document)

    def test_the_owner_with_the_download_permission_is_still_authorized(self):
        self.assertTrue(self.check())

    def test_an_approver_with_the_any_permission_and_a_superuser_are_authorized_for_a_document_they_do_not_own(self):
        from tests.documents_support import APPROVER, ROOT

        self.assertTrue(self.check(APPROVER, owner_pk=99))
        self.assertTrue(self.check(ROOT, owner_pk=99))

    def test_an_operator_is_not_authorized_for_somebody_elses_document(self):
        self.assertFalse(self.check(owner_pk=99))

    def test_a_user_with_only_the_view_permission_or_none_is_not_authorized(self):
        from tests.documents_support import AUDITOR, NOBODY

        self.assertFalse(self.check(AUDITOR))
        self.assertFalse(self.check(NOBODY))

    def test_an_inactive_or_vanished_user_is_not_authorized(self):
        self.assertFalse(self.check(error="inactive"))
        self.assertFalse(self.check(error="not_authenticated"))


class ReconcilerGuardTests(SimpleTestCase):
    def test_the_batch_limit_is_a_positive_integer(self):
        for bad in (0, -1, True, 1.5, "5", None):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                execution.reconcile_failed_extractions(limit=bad)

    def test_the_hook_runs_one_bounded_pass(self):
        with mock.patch.object(execution, "reconcile_failed_extractions") as run:
            execution.reconcile_after_iteration()
        run.assert_called_once_with()
        self.assertEqual(execution.RECONCILE_BATCH_LIMIT, 50)


class LimitsConfigurationTests(SimpleTestCase):
    def test_nothing_set_gives_the_documented_defaults(self):
        self.assertEqual(document_extraction.build_limits({}), ExtractionLimits())
        self.assertEqual(document_extraction.build_limits({name: "  " for name in (document_extraction.ENV_MAX_PAGES, document_extraction.ENV_TIMEOUT_SECONDS)}), ExtractionLimits())

    def test_every_variable_is_read_and_scaled(self):
        env = {
            "AP_DOCUMENT_EXTRACTION_MAX_BYTES": "2048", "AP_DOCUMENT_EXTRACTION_MAX_PAGES": "7", "AP_DOCUMENT_EXTRACTION_MAX_PAGE_CHARS": "100",
            "AP_DOCUMENT_EXTRACTION_MAX_TOTAL_CHARS": "300", "AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS": "9", "AP_DOCUMENT_EXTRACTION_MEMORY_MB": "256",
        }  # fmt: skip
        limits = document_extraction.build_limits(env)
        self.assertEqual((limits.max_bytes, limits.max_pages, limits.max_page_chars, limits.max_total_chars, limits.timeout_seconds, limits.memory_bytes), (2048, 7, 100, 300, 9, 256 * 1024 * 1024))

    def test_an_invalid_value_stops_the_start_naming_the_variable_but_never_the_value(self):
        for name, value in (("MAX_PAGES", "abc"), ("MAX_PAGES", "-5"), ("MAX_PAGES", "5.5"), ("MAX_PAGES", "0"), ("MAX_PAGES", "100000"), ("TIMEOUT_SECONDS", "0"), ("TIMEOUT_SECONDS", "121"), ("MEMORY_MB", "64"), ("MEMORY_MB", "99999"),
                            ("MAX_BYTES", "10"), ("MAX_PAGES", "\u0665"), ("MAX_PAGES", "1e3")):  # fmt: skip
            variable = f"AP_DOCUMENT_EXTRACTION_{name}"
            with self.subTest(variable=variable, value=value), self.assertRaises(ExtractionConfigurationError) as caught:
                document_extraction.build_limits({variable: value})
            self.assertIn(variable, str(caught.exception))
            self.assertNotIn(value, str(caught.exception).replace(variable, ""))

    def test_a_page_limit_above_the_total_limit_is_a_configuration_error(self):
        with self.assertRaises(ExtractionConfigurationError):
            document_extraction.build_limits({"AP_DOCUMENT_EXTRACTION_MAX_PAGE_CHARS": "600000"})

    def test_the_lease_must_cover_the_timeout_and_the_margin(self):
        limits = ExtractionLimits(timeout_seconds=20)
        self.assertFalse(document_extraction.lease_covers(34, limits))
        self.assertTrue(document_extraction.lease_covers(35, limits))
        self.assertTrue(document_extraction.lease_covers(60, limits))  # the default lease covers the default timeout
        self.assertEqual(document_extraction.LEASE_MARGIN_SECONDS, 15)

    def test_the_default_worker_lease_covers_the_default_timeout(self):
        from django.conf import settings

        self.assertTrue(document_extraction.lease_covers(settings.WORKER_LEASE_SECONDS, ExtractionLimits()))

    def test_the_runtime_uses_the_isolated_extractor_and_the_document_store_unless_told_otherwise(self):
        built = document_extraction.build_runtime({})
        self.assertIsInstance(built.extractor, IsolatedExtractor)
        self.assertEqual(built.limits, ExtractionLimits())
        with self.assertRaises(DocumentStorageError) as caught:  # AP_DOCUMENT_STORAGE_DIRECTORY unset: storage is disabled and the task fails closed
            built.store_factory()
        self.assertEqual(caught.exception.code, CODE_DISABLED)


class PrepareRuntimeTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(handlers.install_runtime, handlers._runtime)
        self.addCleanup(setattr, task_registry, "_held_kinds", task_registry._held_kinds)
        handlers.install_runtime(None)

    def test_a_lease_that_covers_the_timeout_installs_the_runtime_and_holds_nothing_back(self):
        line = task_registry.prepare_document_runtime(60, {})
        self.assertIsInstance(handlers._runtime, ExtractionRuntime)
        self.assertEqual(task_registry._held_kinds, frozenset())
        self.assertIn("pypdf", line)
        self.assertIn("isolated process", line)
        self.assertNotIn("DISABLED", line)

    def test_a_short_lease_does_not_stop_the_worker_but_holds_text_extraction_back(self):
        line = task_registry.prepare_document_runtime(30, {})
        self.assertIsNone(handlers._runtime)
        self.assertEqual(task_registry._held_kinds, frozenset({"documents.extract_text"}))
        self.assertIn("DISABLED", line)
        self.assertIn("35", line)
        self.assertIn("stay queued", line)
        with mock.patch.object(task_registry, "_module_kind_gate", return_value=frozenset({"test.add_numbers", "documents.extract_text", "request_intake.extract"})):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), frozenset({"test.add_numbers", "request_intake.extract"}))  # held, never failed; everything else is claimable as before

    def test_preparing_again_with_a_good_lease_releases_the_hold(self):
        task_registry.prepare_document_runtime(30, {})
        task_registry.prepare_document_runtime(60, {})
        self.assertEqual(task_registry._held_kinds, frozenset())
        self.assertIsNotNone(handlers._runtime)

    def test_an_invalid_setting_is_a_start_up_error_that_names_the_setting(self):
        with self.assertRaises(task_registry.WorkerConfigurationError) as caught:
            task_registry.prepare_document_runtime(60, {"AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS": "soon"})
        self.assertIn("AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", str(caught.exception))
        self.assertNotIn("soon", str(caught.exception))
        self.assertIsNone(handlers._runtime)  # nothing half-installed

    def test_the_production_gate_is_the_module_gate_when_nothing_is_held(self):
        task_registry._held_kinds = frozenset()
        with mock.patch.object(task_registry, "_module_kind_gate", return_value=frozenset({"a"})):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), frozenset({"a"}))

    def test_the_composition_has_one_core_hook_and_the_text_handler_and_still_the_intake_reconciler(self):
        composition = task_registry.COMPOSITION
        self.assertEqual([hook.hook_id for hook in composition.maintenance_hooks], ["documents.reconcile_text"])
        self.assertIs(composition.maintenance_hooks[0].run, execution.reconcile_after_iteration)
        self.assertIs(composition.registry["documents.extract_text"], handlers.EXTRACT_TEXT_HANDLER)
        from apps.intake.services import reconcile_after_iteration

        self.assertIs(composition.after_iteration, reconcile_after_iteration)
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        worker.install_composition(composition)  # a composition the worker accepts

    def test_the_text_kind_is_claimable_only_with_a_runtime_installed_in_the_worker_it_runs_in(self):
        self.assertEqual(task_registry.CORE_UNGATED_KINDS, {"documents.extract_text"})
        self.assertIn("documents.extract_text", task_registry.UNGATED_KINDS)
        self.assertNotIn("documents.extract_text", task_registry.LEGACY_UNGATED_KINDS)
