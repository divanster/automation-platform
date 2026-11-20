"""DB-free tests of the document services (DOC-1): authorization before anything is read, the order of the checks, atomic cleanup, the audit contract and the absence of content from every log line.

The real services and the real ``DocumentStore`` run on a temporary directory; the database touch points are the stand-ins of ``tests/documents_support.py``. The behaviour that needs a real database
(a true rollback, the CHECK constraints, concurrent transactions) is in ``tests/test_documents_postgres.py`` and is NOT run in the development VM.
"""

import hashlib
import itertools
import logging
import os
import urllib.parse
import uuid
from unittest import mock

from django.db import DatabaseError, IntegrityError
from django.test import SimpleTestCase

from apps.accounts import roles
from apps.accounts.errors import AuthorizationError
from apps.audit import events
from apps.documents import access, rules, services
from apps.documents.errors import (
    CODE_DISABLED,
    CODE_OBJECT_MISSING,
    DocumentNotFoundError,
    DocumentRejectedError,
    DocumentStorageError,
)
from tests.documents_support import (
    APPROVER,
    AUDITOR,
    NOBODY,
    OPERATOR,
    OTHER_OPERATOR,
    ROOT,
    SENTINEL_NAME,
    SENTINEL_TEXT,
    VALID_PDF,
    FakeDatabase,
    Reader,
    make_pdf,
    patched_documents,
    temporary_store,
    tree_listing,
)

NAME_HEADER = urllib.parse.quote("scan 1.pdf")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args))


class ServiceCase(SimpleTestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)

    def upload(self, who, data=VALID_PDF, *, store, name=NAME_HEADER, content_type="application/pdf", length="auto", atomic=None, **reader):
        stream = Reader(data, **reader)
        calls = []

        def factory():
            calls.append("factory")
            return store

        with patched_documents(self.db, who, atomic=atomic):
            try:
                item = services.upload_document(
                    user=object(), filename_header=name, content_type=content_type,
                    content_length=str(len(data)) if length == "auto" else length, read=stream.read, store_factory=factory,
                )  # fmt: skip
            finally:
                self.stream, self.factory_calls = stream, calls
        return item

    def fetch(self, who, document_id, *, store):
        with patched_documents(self.db, who):
            return services.open_download(user=object(), document_id=document_id, store_factory=lambda: store)

    def assertNothingStored(self, root):
        self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(root)), ([], [], []))

    def assertLogsClean(self, *extra):
        everything = "\n".join(self.capture.lines)
        for forbidden in (SENTINEL_NAME, SENTINEL_TEXT.decode(), "scan 1", NAME_HEADER, *extra):
            self.assertNotIn(forbidden, everything)


class UploadTests(ServiceCase):
    def test_an_operator_uploads_a_pdf_and_gets_its_metadata(self):
        with temporary_store() as (store, root):
            item = self.upload(OPERATOR, store=store)
            self.assertEqual(
                (item.original_filename, item.content_type, item.size_bytes, item.sha256, item.processing_state, item.uploaded_by, item.allowed_actions),
                ("scan 1.pdf", "application/pdf", len(VALID_PDF), hashlib.sha256(VALID_PDF).hexdigest(), "uploaded", OPERATOR.public_id, ("download",)),
            )
            (row,) = self.db.documents.rows
            self.assertEqual((row.pk, row.owner_id, row.size_bytes, row.sha256), (item.id, OPERATOR.pk, len(VALID_PDF), item.sha256))
            self.assertRegex(row.storage_key, r"^[0-9a-f]{2}/[0-9a-f]{32}$")
            self.assertEqual(len(tree_listing(root)), 1)
            self.assertEqual(set(item._fields) & {"storage_key", "path", "owner", "content"}, set())

    def test_the_owner_is_the_authenticated_user_and_nothing_the_client_sent(self):
        with temporary_store() as (store, _):
            item = self.upload(OTHER_OPERATOR, store=store, name=urllib.parse.quote(f"{OPERATOR.public_id}.pdf"))
            self.assertEqual(self.db.documents.rows[0].owner_id, OTHER_OPERATOR.pk)
            self.assertEqual(item.uploaded_by, OTHER_OPERATOR.public_id)

    def test_exactly_one_audit_event_with_safe_metadata_only(self):
        with temporary_store() as (store, _):
            item = self.upload(OPERATOR, make_pdf(marker=SENTINEL_TEXT), store=store, name=urllib.parse.quote(SENTINEL_NAME + ".pdf"))
            (event,) = self.db.events
            self.assertEqual((event.actor_kind, event.actor_public_id, event.action, event.subject_type, event.subject_id), ("user", OPERATOR.public_id, "document.uploaded", "document", item.id))
            self.assertEqual(event.details, {"byte_length": item.size_bytes, "content_hash": item.sha256})
            self.assertEqual((event.case_id, event.run_id), (None, None))
            text = repr(event)
            for forbidden in (SENTINEL_NAME, SENTINEL_TEXT.decode(), "scan", self.db.documents.rows[0].storage_key):
                self.assertNotIn(forbidden, text)

    def test_the_event_is_written_in_the_same_transaction_as_the_row(self):
        with temporary_store() as (store, _):
            depths = []
            real = self.db.record_event
            self.db.record_event = lambda event: (depths.append(self.db.depth), real(event))[1]
            self.upload(OPERATOR, store=store)
            self.assertEqual(depths, [1])

    def test_a_superuser_may_upload_and_no_role_but_the_operator_does_by_default(self):
        with temporary_store() as (store, _):
            self.upload(ROOT, store=store)
        for who in (APPROVER, AUDITOR, NOBODY):
            with self.subTest(who=who.pk), temporary_store() as (store, root), self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
                db = self.db = FakeDatabase()
                self.upload(who, store=store)
            self.assertEqual(caught.exception.reason, "missing_permission")

    def test_a_refused_caller_has_nothing_read_nothing_looked_at_and_nothing_stored(self):
        with temporary_store() as (store, root):
            for who in (NOBODY, APPROVER, AUDITOR):
                self.db = FakeDatabase()
                with self.assertRaises(AuthorizationError), self.assertLogs("apps.accounts.authz", "WARNING"):
                    self.upload(who, store=store, name="%FF%FE", content_type="text/plain", length="abc")  # the input is invalid too: the refusal comes first
                self.assertEqual((self.stream.calls, self.factory_calls), (0, []))
                self.assertNothingStored(root)

    def test_an_unauthenticated_or_inactive_user_is_refused(self):
        with temporary_store() as (store, root):
            for reason in ("not_authenticated", "inactive"):
                with self.assertRaises(AuthorizationError) as caught:
                    self.upload(AuthorizationError(reason), store=store)
                self.assertEqual((caught.exception.reason, self.stream.calls), (reason, 0))
            self.assertNothingStored(root)

    def test_what_the_client_declared_is_checked_before_a_byte_is_read(self):
        with temporary_store(max_bytes=4096) as (store, root):
            cases = {
                "type": dict(content_type="text/plain"),
                "no type": dict(content_type=None),
                "name": dict(name="%00.pdf"),
                "no name": dict(name=None),
                "extension": dict(name="a.txt"),
                "no length": dict(length=None),
                "bad length": dict(length="12abc"),
                "empty": dict(length="0"),
                "too large": dict(length="4097"),
            }
            for label, kwargs in cases.items():
                with self.subTest(case=label), self.assertRaises(DocumentRejectedError):
                    self.upload(OPERATOR, store=store, **kwargs)
                self.assertEqual(self.stream.calls, 0, label)
            self.assertNothingStored(root)

    def test_an_unconfigured_storage_stops_the_upload_before_the_body_is_read(self):
        def disabled():
            raise DocumentStorageError(CODE_DISABLED)

        with patched_documents(self.db, OPERATOR), self.assertRaises(DocumentStorageError) as caught:
            reader = Reader(VALID_PDF)
            services.upload_document(user=object(), filename_header=NAME_HEADER, content_type="application/pdf", content_length=str(len(VALID_PDF)), read=reader.read, store_factory=disabled)
        self.assertEqual((caught.exception.code, reader.calls, self.db.documents.rows, self.db.events), (CODE_DISABLED, 0, [], []))

    def test_a_rejected_body_stores_nothing(self):
        with temporary_store() as (store, root):
            for data in (b"not a pdf at all", VALID_PDF[:-30], VALID_PDF.replace(b"trailer", b"trailxx")):
                with self.assertRaises(DocumentRejectedError):
                    self.upload(OPERATOR, data, store=store)
            with self.assertRaises(DocumentRejectedError):
                self.upload(OPERATOR, VALID_PDF, store=store, length=str(len(VALID_PDF) + 5))
            self.assertNothingStored(root)

    def test_a_failed_audit_write_rolls_the_row_back_and_removes_the_object(self):
        self.db = FakeDatabase(audit_error=DatabaseError("audit insert failed " + SENTINEL_NAME))
        with temporary_store() as (store, root):
            with self.assertRaises(DatabaseError):
                self.upload(OPERATOR, store=store)
            self.assertEqual((self.db.documents.rows, self.db.events, self.db.rolled_back), ([], [], True))
            self.assertEqual(tree_listing(root), [])

    def test_a_failed_insert_removes_the_object(self):
        self.db = FakeDatabase()
        self.db.documents.fail_create = IntegrityError("constraint")
        with temporary_store() as (store, root):
            with self.assertRaises(IntegrityError):
                self.upload(OPERATOR, store=store)
            self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(root)), ([], [], []))

    def test_an_interruption_before_the_commit_removes_the_object(self):
        self.db = FakeDatabase(audit_error=KeyboardInterrupt())  # raised inside the transaction, before the commit
        with temporary_store() as (store, root):
            with self.assertRaises(KeyboardInterrupt):
                self.upload(OPERATOR, store=store)
            self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(root)), ([], [], []))

    def test_an_interruption_during_the_commit_is_treated_as_unknown_and_keeps_the_object(self):
        import contextlib

        @contextlib.contextmanager
        def interrupted_commit():
            with self.db.atomic():
                yield
            raise KeyboardInterrupt()

        with temporary_store() as (store, root):
            with self.assertRaises(KeyboardInterrupt):
                self.upload(OPERATOR, store=store, atomic=interrupted_commit)
            self.assertEqual(len(tree_listing(root)), 1)
        self.assertEqual(len([line for line in self.capture.lines if "commit was not confirmed" in line]), 1)

    def test_a_commit_whose_outcome_is_unknown_keeps_the_object_and_says_so_once(self):
        import contextlib

        @contextlib.contextmanager
        def lost_acknowledgement():
            with self.db.atomic():
                yield
            raise DatabaseError("connection lost during COMMIT " + SENTINEL_NAME)

        with temporary_store() as (store, root):
            with self.assertRaises(DatabaseError) as caught:
                self.upload(OPERATOR, store=store, atomic=lost_acknowledgement)
            self.assertIn(SENTINEL_NAME, str(caught.exception))  # the original exception is unchanged
            self.assertEqual(len(tree_listing(root)), 1)  # a committed row without its file would be data loss: nothing is deleted to guess
        errors = [line for line in self.capture.lines if "commit was not confirmed" in line]
        self.assertEqual(len(errors), 1)
        self.assertIn(str(self.db.documents.rows[0].pk), errors[0])
        self.assertLogsClean()

    def test_a_cleanup_that_fails_is_reported_without_replacing_the_original_error(self):
        self.db = FakeDatabase(audit_error=DatabaseError("audit down"))
        with temporary_store() as (store, root):
            store.delete = mock.Mock(side_effect=DocumentStorageError("document_storage_unavailable"))
            with self.assertRaises(DatabaseError) as caught:
                self.upload(OPERATOR, store=store)
            self.assertEqual(str(caught.exception), "audit down")
        self.assertEqual(len([line for line in self.capture.lines if "could not be removed" in line]), 1)

    def test_no_log_line_holds_a_name_a_hash_a_path_or_content(self):
        with temporary_store() as (store, root):
            item = self.upload(OPERATOR, make_pdf(marker=SENTINEL_TEXT), store=store, name=urllib.parse.quote(SENTINEL_NAME + ".pdf"))
            self.assertLogsClean(item.sha256, root, store._root, self.db.documents.rows[0].storage_key)
            self.assertTrue(any("Document uploaded" in line for line in self.capture.lines))

    def test_a_refusal_log_has_the_action_the_reason_and_only_the_actor_id(self):
        with temporary_store() as (store, _), self.assertLogs("apps.accounts.authz", "WARNING") as logs, self.assertRaises(AuthorizationError):
            self.upload(NOBODY, store=store, name=urllib.parse.quote(SENTINEL_NAME + ".pdf"))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].getMessage(), f"authorization refused: action=document.upload reason=missing_permission actor={NOBODY.public_id}")


class DownloadTests(ServiceCase):
    def seed(self, who=OPERATOR, data=None):
        data = make_pdf(marker=SENTINEL_TEXT) if data is None else data
        store_cm = temporary_store()
        store, root = store_cm.__enter__()
        self.addCleanup(store_cm.__exit__, None, None, None)
        item = self.upload(who, data, store=store, name=urllib.parse.quote(SENTINEL_NAME + ".pdf"))
        self.db.events.clear()
        self.capture.lines.clear()
        return store, item, data

    def test_the_owner_downloads_the_exact_bytes_and_the_start_is_audited(self):
        store, item, data = self.seed()
        download = self.fetch(OPERATOR, item.id, store=store)
        body = b"".join(download.stream)
        download.stream.close()
        self.assertEqual((body, download.filename, download.content_type, download.size_bytes), (data, SENTINEL_NAME + ".pdf", "application/pdf", len(data)))
        (event,) = self.db.events
        self.assertEqual((event.action, event.subject_type, event.subject_id, event.actor_public_id), ("document.downloaded", "document", item.id, OPERATOR.public_id))
        self.assertEqual(event.details, {"byte_length": len(data), "content_hash": hashlib.sha256(data).hexdigest(), "by_owner": True})
        self.assertNotIn(SENTINEL_NAME, repr(event))
        self.assertLogsClean(item.sha256)

    def test_an_approver_downloads_somebody_elses_document_and_the_event_says_so(self):
        store, item, data = self.seed()
        download = self.fetch(APPROVER, item.id, store=store)
        download.stream.close()
        self.assertEqual(self.db.events[0].details["by_owner"], False)
        self.assertEqual(self.db.events[0].actor_public_id, APPROVER.public_id)

    def test_a_superuser_may_download_any_document(self):
        store, item, _ = self.seed()
        self.fetch(ROOT, item.id, store=store).stream.close()

    def test_another_operator_may_not_and_gets_out_of_scope(self):
        store, item, _ = self.seed()
        with self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
            self.fetch(OTHER_OPERATOR, item.id, store=store)
        self.assertEqual((caught.exception.reason, self.db.events), ("out_of_scope", []))

    def test_an_auditor_may_read_metadata_but_never_download_the_content(self):
        store, item, _ = self.seed()
        with self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
            self.fetch(AUDITOR, item.id, store=store)
        self.assertEqual((caught.exception.reason, self.db.events), ("missing_permission", []))
        with patched_documents(self.db, AUDITOR):
            self.assertEqual(services.get_document(user=object(), document_id=item.id).id, item.id)

    def test_nobody_with_no_role_gets_missing_permission_even_for_an_unknown_document(self):
        store, item, _ = self.seed()
        for document_id in (item.id, uuid.uuid4()):
            with self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
                self.fetch(NOBODY, document_id, store=store)
            self.assertEqual(caught.exception.reason, "missing_permission")  # decided before any lookup: it never says whether the document exists
        self.assertEqual(self.db.events, [])

    def test_an_unknown_document_is_not_found_and_nothing_is_opened_or_audited(self):
        store, _, _ = self.seed()
        store.open = mock.Mock()
        with self.assertRaises(DocumentNotFoundError):
            self.fetch(OPERATOR, uuid.uuid4(), store=store)
        store.open.assert_not_called()
        self.assertEqual(self.db.events, [])

    def test_a_refused_download_never_opens_the_object(self):
        store, item, _ = self.seed()
        store.open = mock.Mock()
        for who in (OTHER_OPERATOR, AUDITOR, NOBODY):
            with self.assertRaises(AuthorizationError), self.assertLogs("apps.accounts.authz", "WARNING"):
                self.fetch(who, item.id, store=store)
        store.open.assert_not_called()

    def test_a_missing_or_changed_object_is_a_storage_error_with_no_event(self):
        store, item, data = self.seed()
        path = os.path.join(store._root, "objects", *self.db.documents.rows[0].storage_key.split("/"))
        with open(path, "ab") as handle:
            handle.write(b"x")
        with self.assertRaises(DocumentStorageError) as caught:
            self.fetch(OPERATOR, item.id, store=store)
        self.assertEqual((caught.exception.code, self.db.events), (CODE_OBJECT_MISSING, []))
        os.unlink(path)
        with self.assertRaises(DocumentStorageError) as caught:
            self.fetch(OPERATOR, item.id, store=store)
        self.assertEqual((caught.exception.code, self.db.events), (CODE_OBJECT_MISSING, []))

    def test_a_failed_audit_write_stops_the_download_and_closes_the_object(self):
        store, item, _ = self.seed()
        self.db.audit_error = DatabaseError("audit down")
        opened = []
        real_open = store.open
        store.open = lambda *args: opened.append(real_open(*args)) or opened[-1]
        with self.assertRaises(DatabaseError):
            self.fetch(OPERATOR, item.id, store=store)
        self.assertEqual((self.db.events, opened[0]._handle.closed), ([], True))

    def test_every_download_is_audited_separately(self):
        store, item, _ = self.seed()
        for _ in range(3):
            self.fetch(OPERATOR, item.id, store=store).stream.close()
        self.assertEqual([e.action for e in self.db.events], ["document.downloaded"] * 3)


class VisibilityTests(ServiceCase):
    def seed_many(self):
        store_cm = temporary_store()
        store, _ = store_cm.__enter__()
        self.addCleanup(store_cm.__exit__, None, None, None)
        self.items = []
        for who in (OPERATOR, OTHER_OPERATOR, OPERATOR, OTHER_OPERATOR, OPERATOR):
            self.items.append(self.upload(who, make_pdf(marker=uuid.uuid4().bytes), store=store))
        return store

    def listing(self, who, **kwargs):
        with patched_documents(self.db, who):
            return services.list_documents(user=object(), **kwargs)

    def test_an_operator_sees_only_their_own_documents_newest_first(self):
        self.seed_many()
        page = self.listing(OPERATOR)
        self.assertEqual([item.id for item in page.items], [self.items[4].id, self.items[2].id, self.items[0].id])
        self.assertEqual((page.count, page.page, page.page_size), (3, 1, 20))

    def test_an_approver_an_auditor_and_a_superuser_see_every_document(self):
        self.seed_many()
        for who in (APPROVER, AUDITOR, ROOT):
            with self.subTest(pk=who.pk):
                self.assertEqual([item.id for item in self.listing(who).items], [item.id for item in reversed(self.items)])

    def test_a_user_with_no_view_permission_gets_nothing_and_is_logged(self):
        self.seed_many()
        with self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
            self.listing(NOBODY)
        self.assertEqual(caught.exception.reason, "missing_permission")

    def test_pagination_is_stable_and_bounded(self):
        self.seed_many()
        first, second, third = (self.listing(ROOT, page=n, page_size=2) for n in (1, 2, 3))
        self.assertEqual([len(p.items) for p in (first, second, third)], [2, 2, 1])
        self.assertEqual(len({i.id for p in (first, second, third) for i in p.items}), 5)
        self.assertEqual(self.listing(ROOT, page=9, page_size=2).items, ())
        for kwargs in ({"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": True}, {"page": "1"}, {"page_size": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.listing(ROOT, **kwargs)

    def test_allowed_actions_follow_the_callers_permissions_per_document(self):
        self.seed_many()
        mine = {item.id: item.allowed_actions for item in self.listing(OPERATOR).items}
        self.assertTrue(all(actions == ("download",) for actions in mine.values()))
        self.assertTrue(all(item.allowed_actions == ("download",) for item in self.listing(APPROVER).items))
        self.assertTrue(all(item.allowed_actions == () for item in self.listing(AUDITOR).items))  # metadata yes, content no

    def test_one_document_is_visible_to_its_owner_and_hidden_from_another_operator(self):
        self.seed_many()
        with patched_documents(self.db, OPERATOR):
            self.assertEqual(services.get_document(user=object(), document_id=self.items[0].id).id, self.items[0].id)
        with patched_documents(self.db, OTHER_OPERATOR), self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
            services.get_document(user=object(), document_id=self.items[0].id)
        self.assertEqual(caught.exception.reason, "out_of_scope")
        with patched_documents(self.db, APPROVER):
            self.assertEqual(services.get_document(user=object(), document_id=self.items[0].id).id, self.items[0].id)
        with patched_documents(self.db, OPERATOR), self.assertRaises(DocumentNotFoundError):
            services.get_document(user=object(), document_id=uuid.uuid4())

    def test_the_read_model_has_no_storage_reference(self):
        self.seed_many()
        for item in self.listing(ROOT).items:
            self.assertEqual(set(item._fields), {"id", "original_filename", "content_type", "size_bytes", "sha256", "uploaded_at", "processing_state", "uploaded_by", "allowed_actions"})


class PermissionDecisionTests(SimpleTestCase):
    """The pure table: every role against every action, on the actor's own document and on somebody else's."""

    ME, OTHER = 1, 2

    def problem(self, action, role=None, *, superuser=False, owner=ME):
        permissions = set(roles.DOCUMENT_ROLE_PERMISSIONS[role]) if role else set()
        return access.access_problem(action, is_superuser=superuser, permissions=permissions, actor_pk=self.ME, owner_pk=owner)

    def test_the_matrix(self):
        A = access.Action
        expected = {
            roles.OPERATOR: {A.UPLOAD: (None, None), A.VIEW: (None, "out_of_scope"), A.DOWNLOAD: (None, "out_of_scope")},
            roles.APPROVER: {A.UPLOAD: ("missing_permission",) * 2, A.VIEW: (None, None), A.DOWNLOAD: (None, None)},
            roles.AUDITOR: {A.UPLOAD: ("missing_permission",) * 2, A.VIEW: (None, None), A.DOWNLOAD: ("missing_permission",) * 2},
        }
        for role, actions in expected.items():
            for action, (own, other) in actions.items():
                with self.subTest(role=role, action=action.value):
                    self.assertEqual(self.problem(action, role, owner=self.ME), own)
                    self.assertEqual(self.problem(action, role, owner=self.OTHER), other)

    def test_a_user_with_no_role_can_do_nothing_and_a_superuser_can_do_everything(self):
        for action in access.Action:
            self.assertEqual(self.problem(action), "missing_permission")
            self.assertIsNone(self.problem(action, superuser=True, owner=self.OTHER))

    def test_every_permission_grants_only_its_own_action(self):
        grants = {
            roles.PERM_DOC_UPLOAD: {access.Action.UPLOAD},
            roles.PERM_DOC_VIEW_OWN: {access.Action.VIEW},
            roles.PERM_DOC_VIEW_ANY: {access.Action.VIEW},
            roles.PERM_DOC_DOWNLOAD_OWN: {access.Action.DOWNLOAD},
            roles.PERM_DOC_DOWNLOAD_ANY: {access.Action.DOWNLOAD},
        }
        self.assertEqual(set(grants), roles.DOCUMENT_PERMISSIONS)
        for permission, allowed in grants.items():
            for action in access.Action:
                result = access.access_problem(action, is_superuser=False, permissions={permission}, actor_pk=self.ME, owner_pk=self.ME)
                with self.subTest(permission=permission, action=action.value):
                    self.assertEqual(result is None, action in allowed)

    def test_viewing_never_grants_the_content_and_an_own_permission_never_reaches_somebody_elses(self):
        self.assertEqual(self.problem(access.Action.DOWNLOAD, roles.AUDITOR), "missing_permission")
        for permission in (roles.PERM_DOC_VIEW_OWN, roles.PERM_DOC_DOWNLOAD_OWN):
            action = access.Action.VIEW if permission == roles.PERM_DOC_VIEW_OWN else access.Action.DOWNLOAD
            self.assertEqual(access.access_problem(action, is_superuser=False, permissions={permission}, actor_pk=self.ME, owner_pk=self.OTHER), "out_of_scope")
            self.assertEqual(access.access_problem(action, is_superuser=False, permissions={permission}, actor_pk=self.ME, owner_pk=None), "out_of_scope")

    def test_lookalike_permissions_grant_nothing(self):
        for name in ("documents.upload_documents", "document.upload_document", "intake.upload_document", "upload_document", "documents.UPLOAD_DOCUMENT", ""):
            with self.subTest(name=name):
                self.assertEqual(access.access_problem(access.Action.UPLOAD, is_superuser=False, permissions={name}, actor_pk=1, owner_pk=1), "missing_permission")

    def test_the_decision_agrees_with_the_cores_own_rule_for_every_combination(self):
        from apps.accounts import access as core

        for action, role, superuser, owner in itertools.product(access.Action, (None, *roles.ROLES), (False, True), (self.ME, self.OTHER)):
            permissions = set(roles.DOCUMENT_ROLE_PERMISSIONS[role]) if role else set()
            kw = {"is_superuser": superuser, "permissions": permissions}
            expected = core.permission_problem(access._RULES[action], **kw) or core.scope_problem(access._RULES[action], actor_pk=self.ME, owner_pk=owner, **kw)
            self.assertEqual(access.access_problem(action, actor_pk=self.ME, owner_pk=owner, **kw), expected)

    def test_unknown_actions_are_refused_loudly(self):
        with self.assertRaises(ValueError):
            access.permission_problem("delete", is_superuser=True, permissions=())

    def test_the_audit_vocabulary_has_the_two_document_actions_with_safe_keys_only(self):
        self.assertEqual(events.DETAIL_KEYS[events.ACTION_DOCUMENT_UPLOADED], frozenset({"byte_length", "content_hash"}))
        self.assertEqual(events.DETAIL_KEYS[events.ACTION_DOCUMENT_DOWNLOADED], frozenset({"byte_length", "content_hash", "by_owner"}))
        self.assertIn(events.SUBJECT_DOCUMENT, events.SUBJECT_TYPES)
        actor = uuid.uuid4()
        for hostile in ({"original_filename": "x"}, {"storage_key": "ab/" + "c" * 32}, {"path": "C:\\x"}, {"content": "x"}, {"name": "x.pdf"}):
            with self.subTest(keys=list(hostile)), self.assertRaises(events.AuditEventError):
                events.user_event(actor, events.ACTION_DOCUMENT_UPLOADED, events.SUBJECT_DOCUMENT, uuid.uuid4(), **hostile)
        for value in ("scan 1.pdf", "a/b", "C:\\x", "x" * 101):
            with self.subTest(value=value), self.assertRaises(events.AuditEventError):
                events.user_event(actor, events.ACTION_DOCUMENT_UPLOADED, events.SUBJECT_DOCUMENT, uuid.uuid4(), content_hash=value, byte_length=1)
        events.user_event(actor, events.ACTION_DOCUMENT_UPLOADED, events.SUBJECT_DOCUMENT, uuid.uuid4(), content_hash=hashlib.sha256(b"x").hexdigest(), byte_length=1)

    def test_the_document_namespace_is_core_s_so_no_module_can_declare_these_actions(self):
        from apps.modules.manifest import RESERVED_NAMESPACES

        self.assertTrue({"document", "documents"} <= set(RESERVED_NAMESPACES))
        self.assertEqual({action.split(".")[0] for action in (events.ACTION_DOCUMENT_UPLOADED, events.ACTION_DOCUMENT_DOWNLOADED)}, {"document"})
