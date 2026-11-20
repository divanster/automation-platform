"""DB-free tests of the text extraction services (DOC-2): authorization (the same permission and scope as downloading the document, decided before any record is looked up), the request rules that make a request
idempotent, the read models, the audit events and the logging. ``load_actor``, the tables, ``transaction``, ``enqueue_task`` and ``record_event`` are replaced by stand-ins (as in tests/documents_support.py); what
needs a real database (the row lock, the unique one-to-one result, atomic rollback, concurrent requests) is in ``tests/test_document_text_postgres.py`` and is NOT run in the development VM."""

import contextlib
import datetime
import logging
import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ObjectDoesNotExist
from django.test import SimpleTestCase

from apps.accounts.errors import AuthorizationError
from apps.audit import events
from apps.document_text import rules, services
from apps.document_text.errors import ExtractionNotReadyError
from apps.documents.errors import DocumentNotFoundError
from tests.documents_support import APPROVER, AUDITOR, NOBODY, OPERATOR, OTHER_OPERATOR, ROOT

VERSION = "7.1.1"
SHA = "cd" * 32
DOCUMENT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
SENTINEL = "EXTRACTED-SENTINEL-93aa"


class NoResult:
    """A reverse one-to-one that has no row: reading it raises, as Django's does."""

    def __get__(self, instance, owner):
        raise ObjectDoesNotExist


def result(status="extracted", code="", version=VERSION, pages=2, chars=20, name="pypdf"):
    return SimpleNamespace(
        pk=uuid.uuid4(), status=status, error_code=code, extractor_name=name, extractor_version=version, page_count=pages, char_count=chars, source_sha256=SHA, source_size_bytes=999,
        text_sha256="ef" * 32, completed_at=NOW,
    )  # fmt: skip


def extraction(*, state="queued", outcome=None, requested_at=NOW, owner_public_id=OPERATOR.public_id):
    class Row:
        pass

    row = Row()
    row.pk = uuid.uuid4()
    row.document_id = DOCUMENT_ID
    row.requested_at = requested_at
    row.requested_by = SimpleNamespace(public_id=owner_public_id)
    row.task = SimpleNamespace(state=state)
    if outcome is None:
        Row.result = NoResult()
    else:
        row.result = outcome
    return row


def document(owner=OPERATOR):
    return SimpleNamespace(pk=DOCUMENT_ID, owner_id=owner.pk)


class FakeDocuments:
    def __init__(self, row=None):
        self.row, self.lookups, self.locked = row, 0, 0
        self.objects = self

    def select_for_update(self):
        self.locked += 1
        return self

    def filter(self, **conditions):
        self.lookups += 1
        self.conditions = conditions
        return self

    def first(self):
        return self.row if self.row is not None and self.conditions.get("pk") == self.row.pk else None


class Rows:
    """The extraction queryset of one document, newest first."""

    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def __call__(self, document_id):
        return self

    def order_by(self, *names):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def get(self, pk):
        return next(row for row in self.rows if row.pk == pk)

    def filter(self, **conditions):
        self.filters.append(conditions)
        return Rows([row for row in self.rows if row.pk == conditions.get("pk", row.pk)])

    def count(self):
        return len(self.rows)

    def __getitem__(self, item):
        return self.rows[item]


class ServiceCase(SimpleTestCase):
    def setUp(self):
        self.recorded, self.enqueued, self.created = [], [], []
        self.documents = FakeDocuments(document())
        self.rows = Rows([])
        self.actor = OPERATOR
        self.patchers = [
            mock.patch.object(services, "load_actor", side_effect=lambda user: self.actor),
            mock.patch.object(services, "Document", self.documents),
            mock.patch.object(services, "_extractions", self.rows),
            mock.patch.object(services, "transaction", SimpleNamespace(atomic=contextlib.nullcontext)),
            mock.patch.object(services, "record_event", side_effect=self.recorded.append),
            mock.patch.object(services, "enqueue_task", side_effect=self.enqueue),
            mock.patch.object(services, "installed_version", return_value=VERSION),
            mock.patch.object(services.TextExtraction.objects, "create", side_effect=self.create),
            mock.patch.object(services, "TextExtractionPage", SimpleNamespace(objects=SimpleNamespace(filter=self.page_rows))),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.page_data = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return SimpleNamespace(task=SimpleNamespace(state="queued"), created=True)

    def create(self, **fields):
        row = extraction(state="queued")
        row.pk = fields["id"]
        self.created.append(fields)
        self.rows.rows.insert(0, row)
        return row

    def page_rows(self, **conditions):
        return SimpleNamespace(order_by=lambda *names: self.page_data)

    def as_(self, actor):
        self.actor = actor

    def assertNothingHappened(self):
        self.assertEqual((self.recorded, self.enqueued, self.created), ([], [], []))


class AuthorizationTests(ServiceCase):
    CALLS = {
        "request": lambda: services.request_text_extraction(user=object(), document_id=DOCUMENT_ID),
        "list": lambda: services.list_text_extractions(user=object(), document_id=DOCUMENT_ID),
        "view": lambda: services.get_text_extraction(user=object(), document_id=DOCUMENT_ID, extraction_id=uuid.uuid4()),
        "read": lambda: services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=uuid.uuid4()),
    }

    def test_without_a_download_permission_every_call_is_refused_before_any_record_is_looked_up(self):
        for actor in (AUDITOR, NOBODY):  # the Auditor may view metadata only: it may not request or read extracted text either
            for name, call in self.CALLS.items():
                with self.subTest(actor=actor.pk, call=name):
                    self.as_(actor)
                    with self.assertRaises(AuthorizationError) as caught:
                        call()
                    self.assertEqual(caught.exception.reason, "missing_permission")
        self.assertEqual((self.documents.lookups, self.documents.locked), (0, 0))
        self.assertNothingHappened()

    def test_an_operator_may_use_only_the_extractions_of_their_own_documents(self):
        self.as_(OTHER_OPERATOR)
        for name, call in self.CALLS.items():
            with self.subTest(call=name), self.assertRaises(AuthorizationError) as caught:
                call()
            self.assertEqual(caught.exception.reason, "out_of_scope")
        self.assertNothingHappened()

    def test_the_owner_an_approver_and_a_superuser_pass(self):
        for actor in (OPERATOR, APPROVER, ROOT):
            with self.subTest(actor=actor.pk):
                self.as_(actor)
                self.rows.rows = []
                outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
                self.assertTrue(outcome.created)

    def test_an_unknown_document_is_not_found_and_nothing_is_queued(self):
        self.documents.row = None
        for name, call in self.CALLS.items():
            with self.subTest(call=name), self.assertRaises(DocumentNotFoundError):
                call()
        self.assertNothingHappened()

    def test_the_permission_is_the_download_permission_and_nothing_wider(self):
        from apps.accounts import roles
        from apps.documents.access import Action, permission_problem

        needed = {roles.PERM_DOC_DOWNLOAD_OWN, roles.PERM_DOC_DOWNLOAD_ANY}
        for permission in roles.DOCUMENT_PERMISSIONS:
            with self.subTest(permission=permission):
                actor = SimpleNamespace(pk=1, public_id=uuid.uuid4(), is_superuser=False, permissions=frozenset({permission}))
                allowed = permission_problem(Action.DOWNLOAD, is_superuser=False, permissions=actor.permissions) is None
                self.assertEqual(allowed, permission in needed)
        self.assertFalse(hasattr(roles, "PERM_DOC_EXTRACT"))  # no new permission: setup_roles is unchanged

    def test_an_operator_with_only_the_download_permission_is_still_confined_to_their_own_documents(self):
        only_download = SimpleNamespace(pk=20, public_id=uuid.uuid4(), is_superuser=False, permissions=frozenset({"documents.download_own_document"}), user=SimpleNamespace(pk=20, public_id=uuid.uuid4()))
        self.as_(only_download)
        with self.assertRaises(AuthorizationError) as caught:  # somebody else's document
            services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertEqual(caught.exception.reason, "out_of_scope")
        self.documents.row = SimpleNamespace(pk=DOCUMENT_ID, owner_id=20)
        self.assertTrue(services.request_text_extraction(user=object(), document_id=DOCUMENT_ID).created)  # their own

    def test_a_refusal_is_logged_by_code_and_actor_only(self):
        self.as_(AUDITOR)
        with self.assertLogs("apps.accounts.authz", level="WARNING") as captured, self.assertRaises(AuthorizationError):
            services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertEqual(captured.output, [f"WARNING:apps.accounts.authz:authorization refused: action=document_text.request reason=missing_permission actor={AUDITOR.public_id}"])


class RequestTests(ServiceCase):
    def test_a_first_request_creates_one_extraction_queues_one_task_and_records_one_event(self):
        outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertTrue(outcome.created)
        (task,) = self.enqueued
        (fields,) = self.created
        self.assertEqual(task["kind"], "documents.extract_text")
        self.assertEqual(task["payload"], {"extraction_id": str(fields["id"])})  # an identifier, never any text
        self.assertEqual(task["idempotency_key"], f"documents.extract_text:{fields['id']}")
        self.assertIs(task["registry"], services.DOCUMENT_TEXT_REGISTRY)
        self.assertEqual((fields["document"].pk, fields["requested_by"], fields["task"].state), (DOCUMENT_ID, OPERATOR.user, "queued"))
        (event,) = self.recorded
        self.assertEqual((event.actor_kind, event.actor_public_id, event.action, event.subject_type, event.subject_id), ("user", OPERATOR.public_id, "document.text_requested", "document", DOCUMENT_ID))
        self.assertEqual(event.details, {"extraction_id": str(fields["id"])})
        self.assertEqual((outcome.item.id, outcome.item.status, outcome.item.error_code), (fields["id"], "queued", ""))
        self.assertEqual(self.documents.locked, 1)  # the document row is locked: concurrent requests for it are serialized

    def test_the_caller_supplies_no_identity_limit_or_path(self):
        import inspect

        self.assertEqual(list(inspect.signature(services.request_text_extraction).parameters), ["user", "document_id", "extractor_version"])

    def test_a_pending_extraction_is_returned_not_duplicated(self):
        for state in ("queued", "running"):
            with self.subTest(state=state):
                self.rows.rows = [extraction(state=state)]
                outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
                self.assertEqual((outcome.created, outcome.item.status, outcome.item.id), (False, state, self.rows.rows[0].pk))
        self.assertNothingHappened()

    def test_a_finished_extraction_by_the_installed_extractor_is_returned_not_repeated(self):
        for status, code in (("extracted", ""), ("partial", "some_pages_failed"), ("empty", "no_text")):
            with self.subTest(status=status):
                self.rows.rows = [extraction(outcome=result(status, code))]
                outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
                self.assertEqual((outcome.created, outcome.item.status), (False, status))
        self.assertNothingHappened()

    def test_a_failed_extraction_is_retried_with_a_new_one_and_history_is_kept(self):
        old = extraction(outcome=result("failed", "extraction_unavailable", pages=None))
        self.rows.rows = [old]
        outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertTrue(outcome.created)
        self.assertNotEqual(outcome.item.id, old.pk)
        self.assertIn(old, self.rows.rows)  # the earlier extraction and its result are untouched
        self.assertEqual(len(self.created), 1)

    def test_an_extraction_by_another_library_version_is_redone(self):
        self.rows.rows = [extraction(outcome=result(version="6.0.0"))]
        self.assertTrue(services.request_text_extraction(user=object(), document_id=DOCUMENT_ID).created)
        self.rows.rows = [extraction(outcome=result(name="other"))]
        self.assertTrue(services.request_text_extraction(user=object(), document_id=DOCUMENT_ID).created)

    def test_a_task_that_failed_for_good_without_a_result_does_not_block_a_new_request(self):
        self.rows.rows = [extraction(state="failed")]
        self.assertTrue(services.request_text_extraction(user=object(), document_id=DOCUMENT_ID).created)

    def test_the_installed_version_is_read_from_package_metadata_unless_given(self):
        self.rows.rows = [extraction(outcome=result(version="9.9.9"))]
        self.assertFalse(services.request_text_extraction(user=object(), document_id=DOCUMENT_ID, extractor_version="9.9.9").created)

    def test_a_failed_enqueue_leaves_no_extraction_and_no_event(self):
        with mock.patch.object(services, "enqueue_task", side_effect=RuntimeError("queue down")), self.assertRaises(RuntimeError):
            services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertEqual((self.created, self.recorded), ([], []))

    def test_the_log_line_holds_identifiers_only(self):
        with self.assertLogs("apps.document_text.services", level="INFO") as captured:
            outcome = services.request_text_extraction(user=object(), document_id=DOCUMENT_ID)
        self.assertEqual(captured.output, [f"INFO:apps.document_text.services:Text extraction requested: extraction={outcome.item.id} document={DOCUMENT_ID} actor={OPERATOR.public_id}"])


class ReadModelTests(ServiceCase):
    def test_a_finished_extraction_shows_its_result_without_any_text(self):
        item = services._item(extraction(outcome=result("partial", "some_pages_failed", pages=3, chars=44)))
        self.assertEqual(
            (item.status, item.error_code, item.extractor_name, item.extractor_version, item.page_count, item.char_count, item.source_sha256, item.source_size_bytes, item.text_sha256, item.completed_at),
            ("partial", "some_pages_failed", "pypdf", VERSION, 3, 44, SHA, 999, "ef" * 32, NOW),
        )
        self.assertFalse({"text", "pages"} & set(item._fields))

    def test_an_unfinished_extraction_shows_the_task_state_and_no_result_facts(self):
        for state, status in (("queued", "queued"), ("running", "running")):
            item = services._item(extraction(state=state))
            self.assertEqual((item.status, item.error_code, item.page_count, item.char_count, item.source_sha256, item.text_sha256, item.completed_at), (status, "", None, None, None, None, None))

    def test_a_task_that_failed_for_good_is_reported_as_unavailable_until_the_reconciler_records_it(self):
        for state in ("failed", "cancelled", "succeeded"):
            item = services._item(extraction(state=state))
            self.assertEqual((item.status, item.error_code), ("failed", "extraction_unavailable"))

    def test_the_requester_is_shown_by_public_id_only(self):
        item = services._item(extraction(owner_public_id=OTHER_OPERATOR.public_id))
        self.assertEqual(item.requested_by, OTHER_OPERATOR.public_id)

    def test_listing_is_paginated_and_validated(self):
        self.rows.rows = [extraction(state="queued") for _ in range(5)]
        page = services.list_text_extractions(user=object(), document_id=DOCUMENT_ID, page=2, page_size=2)
        self.assertEqual((page.count, page.page, page.page_size, len(page.items)), (5, 2, 2, 2))
        for kwargs in ({"page": 0}, {"page": True}, {"page_size": 0}, {"page_size": 101}, {"page": "1"}, {"page_size": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                services.list_text_extractions(user=object(), document_id=DOCUMENT_ID, **kwargs)
        self.assertEqual(services.MAX_PAGE_SIZE, 100)

    def test_an_extraction_of_another_document_is_not_found(self):
        self.rows.rows = [extraction()]
        with self.assertRaises(DocumentNotFoundError):
            services.get_text_extraction(user=object(), document_id=DOCUMENT_ID, extraction_id=uuid.uuid4())
        self.assertEqual(services.get_text_extraction(user=object(), document_id=DOCUMENT_ID, extraction_id=self.rows.rows[0].pk).id, self.rows.rows[0].pk)


class ReadTextTests(ServiceCase):
    def finished(self, **kwargs):
        row = extraction(outcome=result(**kwargs))
        self.rows.rows = [row]
        return row

    def pages(self, *texts):
        return [SimpleNamespace(page_number=i, status="text" if t else "empty", error_code="", text=t, char_count=len(t)) for i, t in enumerate(texts, 1)]

    def test_the_pages_come_back_with_their_source_reference_and_one_read_event_is_recorded_first(self):
        row = self.finished()
        self.page_data = self.pages(SENTINEL, "")
        order = []
        with mock.patch.object(services, "record_event", side_effect=lambda event: order.append("event") or self.recorded.append(event)):
            outcome = services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=row.pk)
        self.assertEqual([(p.page_number, p.status, p.text) for p in outcome.pages], [(1, "text", SENTINEL), (2, "empty", "")])
        self.assertEqual([p.source_reference for p in outcome.pages], [rules.source_reference(DOCUMENT_ID, SHA, 1), rules.source_reference(DOCUMENT_ID, SHA, 2)])
        self.assertEqual(order, ["event"])
        (event,) = self.recorded
        self.assertEqual((event.actor_public_id, event.action, event.subject_id), (OPERATOR.public_id, "document.text_read", DOCUMENT_ID))
        self.assertEqual(event.details, {"extraction_id": str(row.pk), "page_count": 2, "char_count": 20, "by_owner": True})
        self.assertNotIn(SENTINEL, repr(event))

    def test_by_owner_is_false_for_another_reader(self):
        row = self.finished()
        self.as_(APPROVER)
        services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=row.pk)
        self.assertIs(self.recorded[0].details["by_owner"], False)

    def test_a_failed_audit_write_returns_no_text(self):
        row = self.finished()
        self.page_data = self.pages(SENTINEL)
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")), self.assertRaises(RuntimeError):
            services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=row.pk)

    def test_an_extraction_without_a_result_has_no_text_to_read_and_records_no_read(self):
        for state in ("queued", "running", "failed"):
            self.rows.rows = [extraction(state=state)]
            with self.subTest(state=state), self.assertRaises(ExtractionNotReadyError):
                services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=self.rows.rows[0].pk)
        self.assertEqual(self.recorded, [])

    def test_a_failed_result_is_read_as_a_result_with_no_pages(self):
        row = self.finished(status="failed", code="encrypted_pdf", pages=None, chars=0)
        outcome = services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=row.pk)
        self.assertEqual((outcome.extraction.status, outcome.extraction.error_code, outcome.pages), ("failed", "encrypted_pdf", ()))

    def test_no_text_reaches_a_log_line_or_an_audit_event(self):
        row = self.finished()
        self.page_data = self.pages(SENTINEL)
        root = logging.getLogger()
        with self.assertLogs(root, level="DEBUG") as captured:
            services.read_extracted_text(user=object(), document_id=DOCUMENT_ID, extraction_id=row.pk)
        self.assertNotIn(SENTINEL, " ".join(captured.output))
        self.assertNotIn(SENTINEL, repr(self.recorded))

    def test_every_audit_event_the_slice_builds_is_valid_in_the_installed_vocabulary(self):
        for builder in (
            lambda: events.user_event(OPERATOR.public_id, events.ACTION_DOCUMENT_TEXT_REQUESTED, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id=str(uuid.uuid4())),
            lambda: events.system_event(events.ACTION_DOCUMENT_TEXT_EXTRACTED, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id=str(uuid.uuid4()), status="extracted", error_code=None, page_count=2, char_count=20, content_hash="ef" * 32, extractor_version=VERSION),
            lambda: events.system_event(events.ACTION_DOCUMENT_TEXT_EXTRACTED, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id=str(uuid.uuid4()), status="failed", error_code="encrypted_pdf", page_count=None, char_count=0, content_hash="ef" * 32, extractor_version=VERSION),
            lambda: events.user_event(OPERATOR.public_id, events.ACTION_DOCUMENT_TEXT_READ, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id=str(uuid.uuid4()), page_count=2, char_count=20, by_owner=False),
        ):
            builder()
        with self.assertRaises(events.AuditEventError):  # a detail the action does not list, such as text, is refused
            events.user_event(OPERATOR.public_id, events.ACTION_DOCUMENT_TEXT_READ, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id="x", text="hello")
        with self.assertRaises(events.AuditEventError):  # and so is a value with a space or a slash
            events.user_event(OPERATOR.public_id, events.ACTION_DOCUMENT_TEXT_READ, events.SUBJECT_DOCUMENT, DOCUMENT_ID, extraction_id="a b")
