"""Tests of the document slice that REQUIRE a real PostgreSQL server (DOC-1).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet; the project owner runs them locally. When they
do pass, that covers the tested scenarios only. This file is ASCII only.

Every document is a small synthetic PDF built in memory; the storage directory is a temporary one behind a permissive test policy (the real platform policies are tested DB-free). Nothing is sent
to any server.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_documents_postgres --tag requires_postgres -v 2
"""

import hashlib
import io
import json
import os
import threading
import unittest
import urllib.parse
import uuid
from unittest import mock

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings, tag

from apps.accounts import roles
from apps.accounts.roles import APPROVER, AUDITOR, OPERATOR
from apps.audit.models import AuditEvent
from apps.documents import services
from apps.documents.errors import DocumentImmutableError
from apps.documents.models import Document
from apps.documents.storage import prepare_directory
from tests import test_api_postgres as base
from tests.documents_support import SENTINEL_NAME, SENTINEL_TEXT, VALID_PDF, PermissivePolicy, make_pdf, temporary_root, tree_listing
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, run_threads

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

COLLECTION = "/api/documents/"
RATES = {**base.RATES, "document_upload": "10000/min", "document_upload_hour": "10000/hour"}


def document_events():
    return list(AuditEvent.objects.filter(subject_type="document").order_by("id"))


class StorageMixin:
    """A prepared temporary storage directory for the test, reached through the route's store factory the way production reaches it."""

    def use_storage(self, **env):
        context = temporary_root()
        self.root = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        prepare_directory(self.root, PermissivePolicy())
        for patcher in (
            mock.patch.dict(os.environ, {"AP_DOCUMENT_STORAGE_DIRECTORY": self.root, **env}),
            mock.patch("config.document_storage._platform_policy", return_value=PermissivePolicy()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


def put_document(browser, data=VALID_PDF, name="scan 1.pdf", content_type="application/pdf", csrf=True, **extra):
    headers = {"HTTP_X_DOCUMENT_FILENAME": urllib.parse.quote(name)}
    if csrf:
        headers["HTTP_X_CSRFTOKEN"] = browser.token
    return browser.client.post(COLLECTION, data=data, content_type=content_type, **headers, **extra)


@tag("requires_postgres")
@requires_postgres
class DocumentApiCase(StorageMixin, base.ApiCase):
    def setUp(self):
        super().setUp()
        override = override_settings(API_THROTTLE_RATES=RATES)
        override.enable()
        self.addCleanup(override.disable)
        self.use_storage()


@tag("requires_postgres")
@requires_postgres
class UploadPersistenceTests(DocumentApiCase):
    def test_an_upload_persists_the_metadata_the_bytes_and_one_audit_event_together(self):
        browser = self.as_(self.operator)
        data = make_pdf(marker=SENTINEL_TEXT, padding=5000)
        response = put_document(browser, data, name=SENTINEL_NAME + ".pdf")
        self.assertEqual(response.status_code, 201, response.content)
        row = Document.objects.get()
        self.assertEqual(
            (row.pk, row.owner_id, row.original_filename, row.content_type, row.size_bytes, row.sha256, row.processing_state),
            (uuid.UUID(response.json()["id"]), self.operator.pk, SENTINEL_NAME + ".pdf", "application/pdf", len(data), hashlib.sha256(data).hexdigest(), "uploaded"),
        )
        self.assertIsNotNone(row.uploaded_at)
        self.assertRegex(row.storage_key, r"^[0-9a-f]{2}/[0-9a-f]{32}$")
        with open(os.path.join(self.root, "objects", *row.storage_key.split("/")), "rb") as handle:
            self.assertEqual(handle.read(), data)
        (event,) = document_events()
        self.assertEqual(
            (event.actor_kind, event.actor_public_id, event.action, event.subject_id, event.details, event.case_id, event.run_id),
            ("user", self.operator.public_id, "document.uploaded", row.pk, {"byte_length": len(data), "content_hash": row.sha256}, None, None),
        )
        self.assertIsNotNone(event.occurred_at)

    def test_the_bytes_are_in_the_directory_and_never_in_the_database(self):
        put_document(self.as_(self.operator), make_pdf(marker=SENTINEL_TEXT))
        with connection.cursor() as cursor:
            cursor.execute("SELECT row_to_json(d)::text FROM documents_document d")
            stored = cursor.fetchone()[0]
        self.assertNotIn(SENTINEL_TEXT.decode(), stored)
        self.assertNotIn("%PDF", stored)
        events = json.dumps(list(AuditEvent.objects.values()), default=str)
        for forbidden in (SENTINEL_TEXT.decode(), SENTINEL_NAME, "%PDF", self.root, Document.objects.get().storage_key):
            self.assertNotIn(forbidden, events)

    def test_the_owner_is_the_logged_in_user_whatever_the_request_says(self):
        response = put_document(self.as_(self.other_operator), name=str(self.operator.public_id) + ".pdf")
        self.assertEqual(Document.objects.get().owner_id, self.other_operator.pk)
        self.assertEqual(response.json()["uploaded_by"], str(self.other_operator.public_id))

    def test_a_refused_upload_leaves_no_row_no_event_and_no_file(self):
        browser = self.as_(self.operator)
        bad = {
            "text": dict(content_type="text/plain"), "not a pdf": dict(data=b"hello " * 100), "truncated": dict(data=VALID_PDF[:-40]), "name": dict(name="a.txt"),
            "multipart": dict(content_type="multipart/form-data; boundary=x"),
        }  # fmt: skip
        for label, kwargs in bad.items():
            with self.subTest(case=label):
                self.assertGreaterEqual(put_document(browser, **kwargs).status_code, 400)
        self.assertEqual(put_document(browser, VALID_PDF, csrf=False).status_code, 403)
        for who in (self.approver, self.auditor, self.nobody):
            self.assertEqual(put_document(self.as_(who)).status_code, 403)
        self.assertEqual((Document.objects.count(), document_events(), tree_listing(self.root)), (0, [], []))

    def test_an_oversize_body_is_refused_with_413_and_nothing_stored(self):
        with mock.patch.dict(os.environ, {"AP_DOCUMENT_MAX_BYTES": "2048"}):
            response = put_document(self.as_(self.operator), make_pdf(padding=4000))
        self.assertEqual((response.status_code, base.err(response)), (413, "document_too_large"))
        self.assertEqual((Document.objects.count(), tree_listing(self.root)), (0, []))

    def test_the_same_bytes_uploaded_twice_are_two_documents(self):
        browser = self.as_(self.operator)
        first, second = put_document(browser).json(), put_document(browser).json()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((Document.objects.count(), len(Document.objects.values_list("storage_key", flat=True).distinct()), len(tree_listing(self.root))), (2, 2, 2))


@tag("requires_postgres")
@requires_postgres
class VisibilityTests(DocumentApiCase):
    def setUp(self):
        super().setUp()
        self.mine = put_document(self.as_(self.operator), name="mine.pdf").json()
        self.theirs = put_document(self.as_(self.other_operator), name="theirs.pdf", data=make_pdf(marker=b"other")).json()

    def ids(self, user, **query):
        return {item["id"] for item in self.as_(user).get(COLLECTION, **query).json()["results"]}

    def test_each_role_sees_what_it_should(self):
        self.assertEqual(self.ids(self.operator), {self.mine["id"]})
        self.assertEqual(self.ids(self.other_operator), {self.theirs["id"]})
        for user in (self.approver, self.auditor, self.superuser):
            self.assertEqual(self.ids(user), {self.mine["id"], self.theirs["id"]}, user.username)
        self.assertEqual(self.as_(self.nobody).get(COLLECTION).status_code, 403)

    def test_a_hybrid_operator_and_approver_sees_everything(self):
        self.assertEqual(self.ids(self.hybrid), {self.mine["id"], self.theirs["id"]})

    def test_metadata_of_a_document_the_user_may_not_view_is_a_404_identical_to_an_unknown_one(self):
        hidden = self.as_(self.operator).get(f"/api/documents/{self.theirs['id']}/")
        unknown = self.as_(self.operator).get(f"/api/documents/{uuid.uuid4()}/")
        self.assertEqual((hidden.status_code, unknown.status_code, hidden.json()), (404, 404, unknown.json()))
        self.assertEqual(self.as_(self.nobody).get(f"/api/documents/{self.mine['id']}/").status_code, 403)

    def test_the_list_is_newest_first_and_paginated(self):
        for number in range(4):
            put_document(self.as_(self.operator), name=f"n{number}.pdf", data=make_pdf(marker=f"n{number}".encode()))
        page = self.as_(self.operator).get(COLLECTION, page=1, page_size=2).json()
        self.assertEqual((page["count"], len(page["results"])), (5, 2))
        stamps = [item["uploaded_at"] for item in page["results"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        every = [item["id"] for p in (1, 2, 3) for item in self.as_(self.operator).get(COLLECTION, page=p, page_size=2).json()["results"]]
        self.assertEqual((len(every), len(set(every))), (5, 5))

    def test_the_response_never_carries_a_storage_key_or_a_path(self):
        text = self.as_(self.approver).get(COLLECTION).content.decode()
        for row in Document.objects.all():
            self.assertNotIn(row.storage_key, text)
        self.assertNotIn(self.root, text)


@tag("requires_postgres")
@requires_postgres
class DownloadAuthorizationTests(DocumentApiCase):
    def setUp(self):
        super().setUp()
        self.data = make_pdf(marker=SENTINEL_TEXT, padding=200_000)
        self.item = put_document(self.as_(self.operator), self.data, name="mine.pdf").json()
        self.url = f"/api/documents/{self.item['id']}/content/"
        AuditEvent.objects.all()  # the upload event exists; the counts below are relative to it
        self.before = len(document_events())

    def body(self, response):
        # Exhausting the stream closes the response (the test client wraps it). A second explicit close() would fire request_finished again with close_old_connections
        # connected and close the test's own connection inside the TestCase transaction.
        return b"".join(response.streaming_content)

    def test_the_owner_and_an_approver_and_a_superuser_download_the_exact_bytes(self):
        for user in (self.operator, self.approver, self.superuser, self.hybrid):
            with self.subTest(user=user.username):
                response = self.as_(user).get(self.url)
                self.assertEqual((response.status_code, response["Content-Type"], response["Content-Disposition"].startswith("attachment;")), (200, "application/pdf", True))
                self.assertEqual(self.body(response), self.data)

    def test_everybody_else_is_refused_without_a_byte_and_without_an_audit_event(self):
        expectations = ((self.other_operator, 404), (self.auditor, 403), (self.nobody, 403))
        for user, status in expectations:
            with self.subTest(user=user.username):
                response = self.as_(user).get(self.url)
                self.assertEqual(response.status_code, status)
                self.assertFalse(response.streaming)
        self.assertEqual(len(document_events()), self.before)

    def test_an_anonymous_client_gets_nothing(self):
        response = base.Browser().client.get(self.url)
        self.assertEqual((response.status_code, base.err(response)), (403, "not_authenticated"))

    def test_every_download_is_audited_with_the_actor_and_whether_it_was_the_owner(self):
        self.body(self.as_(self.operator).get(self.url))
        self.body(self.as_(self.approver).get(self.url))
        events = document_events()[self.before :]
        self.assertEqual([e.action for e in events], ["document.downloaded"] * 2)
        self.assertEqual([e.actor_public_id for e in events], [self.operator.public_id, self.approver.public_id])
        self.assertEqual([e.details["by_owner"] for e in events], [True, False])
        for event in events:
            self.assertEqual((event.subject_id, event.details["content_hash"], event.details["byte_length"]), (uuid.UUID(self.item["id"]), self.item["sha256"], len(self.data)))
            self.assertEqual(set(event.details), {"byte_length", "content_hash", "by_owner"})

    def test_a_failed_audit_write_stops_the_download(self):
        self.as_(self.operator)
        browser = self.as_(self.operator)
        browser.client.raise_request_exception = False
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")):
            response = browser.get(self.url)
        self.assertEqual((response.status_code, base.err(response)), (500, "internal_error"))
        self.assertFalse(response.streaming)
        self.assertEqual(len(document_events()), self.before)

    def test_a_missing_object_is_a_503_with_no_event_and_no_path(self):
        row = Document.objects.get()
        os.unlink(os.path.join(self.root, "objects", *row.storage_key.split("/")))
        response = self.as_(self.operator).get(self.url)
        self.assertEqual((response.status_code, base.err(response)), (503, "document_storage_unavailable"))
        self.assertNotIn(self.root, response.content.decode())
        self.assertEqual(len(document_events()), self.before)

    def test_a_role_change_takes_effect_on_the_next_request(self):
        first = self.as_(self.approver).get(self.url)
        self.assertEqual(first.status_code, 200)
        self.body(first)  # consume the stream so its file handle is released
        self.approver.groups.clear()
        self.assertEqual(self.as_(self.approver).get(self.url).status_code, 403)
        self.approver.groups.add(Group.objects.get(name=OPERATOR))
        self.assertEqual(self.as_(self.approver).get(self.url).status_code, 404)  # an operator may only reach their own

    def test_a_deactivated_user_is_refused(self):
        browser = self.as_(self.operator)
        type(self.operator).objects.filter(pk=self.operator.pk).update(is_active=False)
        self.assertIn(browser.get(self.url).status_code, (401, 403))


@tag("requires_postgres")
@requires_postgres
class AtomicityTests(DocumentApiCase):
    def test_a_failed_audit_write_rolls_back_the_row_and_removes_the_file(self):
        browser = self.as_(self.operator)
        browser.client.raise_request_exception = False
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")):
            response = put_document(browser)
        self.assertEqual((response.status_code, base.err(response)), (500, "internal_error"))
        self.assertEqual((Document.objects.count(), document_events(), tree_listing(self.root)), (0, [], []))

    def test_a_database_constraint_failure_rolls_everything_back_and_removes_the_file(self):
        browser = self.as_(self.operator)
        browser.client.raise_request_exception = False
        real_create = Document.objects.create

        def break_the_row(**fields):
            fields["sha256"] = "not-a-hash"  # violates documents_sha256_shape inside the real transaction
            return real_create(**fields)

        with mock.patch.object(Document.objects, "create", side_effect=break_the_row):
            response = put_document(browser)
        self.assertEqual(response.status_code, 500)
        self.assertEqual((Document.objects.count(), document_events(), tree_listing(self.root)), (0, [], []))

    def test_an_error_after_the_audit_insert_but_before_the_commit_leaves_nothing(self):
        browser = self.as_(self.operator)
        browser.client.raise_request_exception = False
        real_record = services.record_event

        def record_then_fail(event):
            real_record(event)
            raise RuntimeError("fails after the audit INSERT")

        with mock.patch.object(services, "record_event", record_then_fail):
            self.assertEqual(put_document(browser).status_code, 500)
        self.assertEqual((Document.objects.count(), AuditEvent.objects.count(), tree_listing(self.root)), (0, 0, []))

    def test_a_storage_failure_stores_nothing_in_the_database(self):
        browser = self.as_(self.operator)
        with mock.patch.dict(os.environ, {"AP_DOCUMENT_STORAGE_DIRECTORY": os.path.join(self.root, "missing")}):
            response = put_document(browser)
        self.assertEqual((response.status_code, base.err(response)), (503, "document_storage_unavailable"))
        self.assertEqual((Document.objects.count(), AuditEvent.objects.count()), (0, 0))


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
        self.other_operator = base.make_user("omar", OPERATOR)
        self.approver = base.make_user("anna", APPROVER)
        self.use_storage()

    def login(self, user):
        browser = base.Browser()
        assert browser.login(user.username).status_code == 200
        return browser

    def test_concurrent_uploads_all_succeed_with_distinct_whole_documents(self):
        payloads = [make_pdf(marker=f"upload-{n}".encode(), padding=30_000 + n) for n in range(8)]
        browsers = [self.login(self.operator if n % 2 == 0 else self.other_operator) for n in range(len(payloads))]
        results = run_threads(self, [lambda b=b, p=p, n=n: put_document(b, p, name=f"c{n}.pdf") for n, (b, p) in enumerate(zip(browsers, payloads))])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sorted(response.status_code for _, response in results), [201] * len(payloads))
        rows = list(Document.objects.order_by("original_filename"))
        self.assertEqual(len(rows), len(payloads))
        self.assertEqual(len({row.storage_key for row in rows}), len(payloads))
        for number, row in enumerate(rows):
            with open(os.path.join(self.root, "objects", *row.storage_key.split("/")), "rb") as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), row.sha256)
        self.assertEqual(len(tree_listing(self.root)), len(payloads))  # nothing left in incoming/
        self.assertEqual(AuditEvent.objects.filter(action="document.uploaded").count(), len(payloads))

    def test_concurrent_uploads_of_identical_bytes_give_independent_documents(self):
        browsers = [self.login(self.operator) for _ in range(5)]
        results = run_threads(self, [lambda b=b: put_document(b) for b in browsers])
        self.assertTrue(all(kind == "ok" and response.status_code == 201 for kind, response in results), results)
        self.assertEqual((Document.objects.count(), len({d.storage_key for d in Document.objects.all()})), (5, 5))

    def test_concurrent_downloads_each_get_the_whole_file_and_their_own_audit_event(self):
        data = make_pdf(padding=400_000)
        item = put_document(self.login(self.operator), data).json()
        url = f"/api/documents/{item['id']}/content/"
        browsers = [self.login(self.operator), self.login(self.approver), self.login(self.operator), self.login(self.approver)]

        def fetch(browser):
            response = browser.get(url)
            body = b"".join(response.streaming_content)  # exhausting the stream closes the response
            return response.status_code, body

        results = run_threads(self, [lambda b=b: fetch(b) for b in browsers])
        self.assertTrue(all(kind == "ok" and value == (200, data) for kind, value in results), [r[0] for r in results])
        self.assertEqual(AuditEvent.objects.filter(action="document.downloaded").count(), 4)

    def test_uploads_racing_a_failing_audit_leave_no_orphans_for_the_ones_that_failed(self):
        calls = {"n": 0}
        guard = threading.Lock()
        real = services.record_event

        def flaky(event):
            with guard:
                calls["n"] += 1
                fail = calls["n"] % 2 == 0
            if fail:
                raise RuntimeError("audit down")
            return real(event)

        browsers = [self.login(self.operator) for _ in range(6)]
        for browser in browsers:
            browser.client.raise_request_exception = False
        with mock.patch.object(services, "record_event", flaky):
            results = run_threads(self, [lambda b=b: put_document(b) for b in browsers])
        statuses = sorted(response.status_code for _, response in results)
        self.assertEqual(statuses, [201] * 3 + [500] * 3)
        self.assertEqual((Document.objects.count(), AuditEvent.objects.count(), len(tree_listing(self.root))), (3, 3, 3))


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def owner(self):
        return base.User.objects.create_user(username="owner-" + uuid.uuid4().hex[:6])

    def fields(self, **changes):
        good = dict(
            owner=self.owner(), original_filename="a.pdf", content_type="application/pdf", size_bytes=10, sha256="a" * 64,
            storage_key=uuid.uuid4().hex[:2] + "/" + uuid.uuid4().hex, processing_state="uploaded",
        )  # fmt: skip
        good.update(changes)
        return good

    def test_a_valid_row_is_accepted_and_every_malformed_one_is_refused_by_the_database(self):
        Document.objects.create(**self.fields())
        bad = {
            "content type": dict(content_type="text/plain"), "sha too short": dict(sha256="a" * 63), "sha upper": dict(sha256="A" * 64), "sha not hex": dict(sha256="g" * 64),
            "size zero": dict(size_bytes=0), "size over the hard maximum": dict(size_bytes=100 * 1024 * 1024 + 1), "key shape": dict(storage_key="../../x"), "key upper": dict(storage_key="AB/" + "C" * 32),
            "key short": dict(storage_key="ab/" + "c" * 31), "state": dict(processing_state="done"), "empty name": dict(original_filename=""),
        }  # fmt: skip
        for label, changes in bad.items():
            with self.subTest(case=label), self.assertRaises(IntegrityError), transaction.atomic():
                Document.objects.create(**self.fields(**changes))

    def test_the_storage_key_is_unique(self):
        first = Document.objects.create(**self.fields())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Document.objects.create(**self.fields(storage_key=first.storage_key))

    def test_a_user_with_documents_cannot_be_deleted(self):
        document = Document.objects.create(**self.fields())
        with self.assertRaises(ProtectedError):
            document.owner.delete()

    def test_stored_metadata_is_immutable_and_only_the_processing_state_may_change(self):
        document = Document.objects.create(**self.fields())
        document.sha256 = "b" * 64
        with self.assertRaises(DocumentImmutableError):
            document.save()
        with self.assertRaises(DocumentImmutableError):
            document.save(update_fields=["sha256"])
        with self.assertRaises(DocumentImmutableError):
            Document.objects.filter(pk=document.pk).update(original_filename="x.pdf")
        with self.assertRaises(DocumentImmutableError):
            document.delete()
        with self.assertRaises(DocumentImmutableError):
            Document.objects.all().delete()
        self.assertEqual(Document.objects.get(pk=document.pk).sha256, "a" * 64)
        Document.objects.filter(pk=document.pk).update(processing_state="processing")  # the one permitted change
        self.assertEqual(Document.objects.get(pk=document.pk).processing_state, "processing")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Document.objects.filter(pk=document.pk).update(processing_state="nonsense")

    def test_an_existing_primary_key_is_never_overwritten(self):
        document = Document.objects.create(**self.fields())
        clone = Document(id=document.pk, **self.fields())
        with self.assertRaises(IntegrityError), transaction.atomic():
            clone.save()

    def test_the_document_permissions_exist_and_match_the_roles(self):
        existing = {f"{p.content_type.app_label}.{p.codename}" for p in Permission.objects.filter(content_type__app_label="documents").select_related("content_type")}
        self.assertEqual(existing, set(roles.DOCUMENT_PERMISSIONS))

    def test_setup_roles_gives_each_role_exactly_its_document_permissions(self):
        call_command("setup_roles", stdout=io.StringIO())
        for role in roles.ROLES:
            held = {f"{p.content_type.app_label}.{p.codename}" for p in Group.objects.get(name=role).permissions.select_related("content_type")}
            self.assertEqual(held, set(roles.granted_permissions(role)), role)
            self.assertEqual({p for p in held if p.startswith("documents.")}, set(roles.DOCUMENT_ROLE_PERMISSIONS[role]), role)

    def test_the_audit_table_accepts_the_document_events_and_still_refuses_unknown_subjects(self):
        for action in ("document.uploaded", "document.downloaded"):
            AuditEvent.objects.create(actor_kind="user", actor_public_id=uuid.uuid4(), action=action, subject_type="document", subject_id=uuid.uuid4(), details={"byte_length": 1})
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditEvent.objects.create(actor_kind="user", actor_public_id=uuid.uuid4(), action="document.uploaded", subject_type="documents", subject_id=uuid.uuid4())


@tag("requires_postgres")
@requires_postgres
class AuditSchemaTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """audit 0004: the database accepts the ``document`` subject type, and a reverse past a stored document event is refused. Returns to the latest migrations afterwards."""

    BEFORE = ("audit", "0003_ai_settings_subject_type")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return MigrationExecutor(connection)

    def setUp(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def constraint(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = 'audit_event_subject_type_valid'", [AuditEvent._meta.db_table])
            return cursor.fetchone()[0]

    def new_event(self, subject_type="document"):
        return AuditEvent.objects.create(actor_kind="user", actor_public_id=uuid.uuid4(), action="document.uploaded", subject_type=subject_type, subject_id=uuid.uuid4(), details={"byte_length": 1})

    def test_the_latest_schema_accepts_the_subject_type(self):
        self.assertIn("'document'", self.constraint())
        self.new_event()

    def test_before_0004_the_subject_type_is_refused_and_after_it_accepted(self):
        self.migrate(self.BEFORE)
        self.assertNotIn("'document'", self.constraint())
        self.assertIn("'ai_settings'", self.constraint())
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.new_event()
        self.migrate(("audit", "0004_document_subject_type"))
        self.assertIn("'document'", self.constraint())

    def test_reverse_without_a_document_event_succeeds_and_keeps_every_row(self):
        self.new_event("ai_settings")
        before = list(AuditEvent.objects.order_by("id").values_list("id", "action", "subject_type", "details"))
        self.migrate(self.BEFORE)
        self.assertEqual(list(AuditEvent.objects.order_by("id").values_list("id", "action", "subject_type", "details")), before)

    def test_reverse_is_refused_when_a_document_event_exists_and_nothing_is_deleted(self):
        self.new_event()
        with self.assertRaises(IrreversibleError):
            self.migrate(self.BEFORE)
        self.assertEqual(AuditEvent.objects.filter(subject_type="document").count(), 1)
        self.assertIn("'document'", self.constraint())

    def test_the_documents_table_migrates_forward_and_back_without_touching_other_tables(self):
        self.migrate(("documents", None))
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('documents_document')")
            self.assertIsNone(cursor.fetchone()[0])
        self.migrate(("documents", "0001_initial"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('documents_document')")
            self.assertIsNotNone(cursor.fetchone()[0])
