"""DB-free tests of the document API (DOC-1): routing, session authentication, CSRF, the upload and download contract, the error mapping, streaming, the response headers and the absence of any
name, path, hash-of-content or content from a response or a log line.

The views, the serializers, the error handler, the services and the document store are the real ones, on a temporary directory; the database touch points are the stand-ins of
``tests/documents_support.py``. The PostgreSQL behaviour is in ``tests/test_documents_postgres.py`` and is NOT run in the development VM.
"""

import json
import logging
import os
import re
import tempfile
import urllib.parse
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.handlers.wsgi import WSGIRequest
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.models import User
from apps.api import views_documents
from tests.documents_support import (
    APPROVER,
    AUDITOR,
    NOBODY,
    OPERATOR,
    OTHER_OPERATOR,
    SENTINEL_NAME,
    SENTINEL_TEXT,
    VALID_PDF,
    FakeDatabase,
    PermissivePolicy,
    make_pdf,
    patched_documents,
    temporary_root,
    tree_listing,
)

COLLECTION = "/api/documents/"
RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min", "document_upload": "1000/min", "document_upload_hour": "1000/hour"}
OUTPUT_KEYS = {"id", "original_filename", "content_type", "size_bytes", "sha256", "uploaded_at", "processing_state", "uploaded_by", "allowed_actions"}


def fake_user():
    return User(pk=7, username="synthetic-user", is_active=True)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


class ApiCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = APIClient()
        self.db = FakeDatabase()
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)
        context = temporary_root()
        self.root = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        from apps.documents.storage import prepare_directory

        prepare_directory(self.root, PermissivePolicy())
        self.env = {"AP_DOCUMENT_STORAGE_DIRECTORY": self.root}
        self.texts = []

    def environment(self, **extra):
        env = {**self.env, **extra}
        return mock.patch.dict(os.environ, env), mock.patch("config.document_storage._platform_policy", return_value=PermissivePolicy())

    def call(self, method, path, who=OPERATOR, *, body=None, filename=urllib.parse.quote("scan 1.pdf"), content_type="application/pdf", headers=None, env=None, content_length=None, **extra):
        self.client.force_authenticate(user=fake_user())
        env_patch, policy_patch = self.environment(**(env or {}))
        meta = dict(headers or {})
        if filename is not None:
            meta["HTTP_X_DOCUMENT_FILENAME"] = filename
        if content_length is not None:
            meta["CONTENT_LENGTH"] = content_length
        with env_patch, policy_patch, patched_documents(self.db, who):
            if method == "post":
                response = self.client.post(path, data=VALID_PDF if body is None else body, content_type=content_type, **meta, **extra)
            else:
                response = getattr(self.client, method)(path, **meta, **extra)
        return response

    def upload(self, who=OPERATOR, **kwargs):
        return self.call("post", COLLECTION, who, **kwargs)

    def text_of(self, response):
        if getattr(response, "streaming", False):
            return ""
        text = response.content.decode("latin-1")
        self.texts.append(text)
        return text

    def assertNoLeak(self, *extra):
        everything = "\n".join(self.texts + self.capture.lines)
        for forbidden in (SENTINEL_NAME, SENTINEL_TEXT.decode(), "scan%201", self.root, *extra):
            self.assertNotIn(forbidden, everything)


class RoutingTests(ApiCase):
    def test_the_routes_resolve_to_the_three_views(self):
        self.assertIs(resolve(COLLECTION).func.cls, views_documents.DocumentCollectionView)
        document = uuid.uuid4()
        self.assertIs(resolve(f"/api/documents/{document}/").func.cls, views_documents.DocumentDetailView)
        self.assertIs(resolve(f"/api/documents/{document}/content/").func.cls, views_documents.DocumentContentView)
        self.assertEqual(reverse("document-collection"), COLLECTION)
        self.assertEqual(reverse("document-detail", kwargs={"document_id": document}), f"/api/documents/{document}/")
        self.assertEqual(reverse("document-content", kwargs={"document_id": document}), f"/api/documents/{document}/content/")

    def test_there_is_no_public_file_url_and_no_path_parameter(self):
        for path in ("/api/documents/../x/", "/api/documents/not-a-uuid/", "/api/documents/not-a-uuid/content/", f"/api/documents/{uuid.uuid4()}/content/extra/", f"/api/documents/{uuid.uuid4()}.pdf", "/media/x.pdf", "/static/x.pdf"):
            with self.subTest(path=path):
                self.client.force_authenticate(user=fake_user())
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_only_the_documented_methods_are_allowed(self):
        document = uuid.uuid4()
        for method, path, allowed in (
            ("get", COLLECTION, True), ("post", COLLECTION, True), ("put", COLLECTION, False), ("patch", COLLECTION, False), ("delete", COLLECTION, False),
            ("get", f"/api/documents/{document}/", True), ("post", f"/api/documents/{document}/", False), ("put", f"/api/documents/{document}/", False), ("delete", f"/api/documents/{document}/", False),
            ("get", f"/api/documents/{document}/content/", True), ("post", f"/api/documents/{document}/content/", False), ("delete", f"/api/documents/{document}/content/", False),
        ):  # fmt: skip
            with self.subTest(method=method, path=path):
                self.client.force_authenticate(user=fake_user())
                response = getattr(self.client, method)(path, b"", content_type="application/pdf") if method in ("post", "put", "patch") else getattr(self.client, method)(path)
                self.assertEqual(response.status_code == 405, not allowed)

    def test_there_is_no_delete_replace_or_overwrite_endpoint(self):
        for cls in (views_documents.DocumentCollectionView, views_documents.DocumentDetailView, views_documents.DocumentContentView):
            self.assertEqual({m for m in ("put", "patch", "delete") if hasattr(cls, m)}, set())


class AuthenticationTests(ApiCase):
    def test_anonymous_requests_are_403_not_authenticated_and_touch_nothing(self):
        document = uuid.uuid4()
        anonymous = APIClient(enforce_csrf_checks=True)
        for method, path in (("get", COLLECTION), ("post", COLLECTION), ("get", f"/api/documents/{document}/"), ("get", f"/api/documents/{document}/content/")):
            with self.subTest(method=method, path=path):
                response = anonymous.post(path, data=VALID_PDF, content_type="application/pdf") if method == "post" else anonymous.get(path)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))
        self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(self.root)), ([], [], []))

    def test_an_upload_without_a_csrf_token_is_refused_before_the_service_runs(self):
        request = APIRequestFactory(enforce_csrf_checks=True).generic("POST", COLLECTION, VALID_PDF, content_type="application/pdf", HTTP_X_DOCUMENT_FILENAME="a.pdf")
        request.user = fake_user()  # what AuthenticationMiddleware sets from a valid session
        with mock.patch.object(views_documents.services, "upload_document") as called:
            response = views_documents.DocumentCollectionView.as_view(store_factory=lambda: None)(request)
            body = json.loads(response.rendered_content)
        self.assertEqual((response.status_code, body["error"]["code"]), (403, "csrf_failed"))
        called.assert_not_called()

    def test_reads_need_no_csrf_token(self):
        request = APIRequestFactory(enforce_csrf_checks=True).get(COLLECTION)
        request.user = fake_user()
        with patched_documents(self.db, OPERATOR):
            self.assertEqual(views_documents.DocumentCollectionView.as_view(store_factory=lambda: None)(request).status_code, 200)

    def test_a_view_that_was_not_wired_with_a_store_fails_closed(self):
        request = APIRequestFactory().generic("POST", COLLECTION, VALID_PDF, content_type="application/pdf", HTTP_X_DOCUMENT_FILENAME="a.pdf")
        force_authenticate(request, user=fake_user())
        with patched_documents(self.db, OPERATOR):
            response = views_documents.DocumentCollectionView.as_view()(request)
            response.render()
        self.assertEqual((response.status_code, json.loads(response.content)["error"]["code"]), (500, "internal_error"))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))


class UploadContractTests(ApiCase):
    def test_a_pdf_upload_returns_201_and_exactly_the_metadata(self):
        response = self.upload()
        text = self.text_of(response)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(set(data), OUTPUT_KEYS)
        self.assertEqual((data["original_filename"], data["content_type"], data["size_bytes"], data["processing_state"], data["allowed_actions"]), ("scan 1.pdf", "application/pdf", len(VALID_PDF), "uploaded", ["download"]))
        self.assertEqual(data["uploaded_by"], str(OPERATOR.public_id))
        self.assertEqual(len(data["sha256"]), 64)
        self.assertEqual(len(tree_listing(self.root)), 1)
        self.assertEqual([e.action for e in self.db.events], ["document.uploaded"])
        for forbidden in (self.db.documents.rows[0].storage_key, self.root, "objects", "incoming"):
            self.assertNotIn(forbidden, text)

    def test_the_response_is_never_cached(self):
        self.assertIn("no-store", self.upload()["Cache-Control"])
        self.assertIn("no-store", self.call("get", COLLECTION)["Cache-Control"])
        self.assertIn("no-store", self.upload(content_type="text/plain")["Cache-Control"])

    def test_the_client_name_is_a_label_and_a_path_in_it_is_reduced_to_its_last_component(self):
        response = self.upload(filename=urllib.parse.quote("..\\..\\Windows\\System32\\x y.pdf"))
        self.assertEqual((response.status_code, response.json()["original_filename"]), (201, "x y.pdf"))
        for part in tree_listing(self.root):
            self.assertRegex(part.replace("\\", "/"), r"^objects/[0-9a-f]{2}/[0-9a-f]{32}$")

    def test_a_non_ascii_name_survives_the_percent_encoded_header(self):
        response = self.upload(filename=urllib.parse.quote("r\u00e9sum\u00e9 \u8acb\u6c42.pdf"))
        self.assertEqual(response.json()["original_filename"], "r\u00e9sum\u00e9 \u8acb\u6c42.pdf")

    def test_the_declared_type_must_be_application_pdf_and_the_name_must_end_in_pdf(self):
        for label, kwargs in (
            ("text", dict(content_type="text/plain")), ("octet", dict(content_type="application/octet-stream")), ("json", dict(content_type="application/json")),
            ("multipart", dict(content_type="multipart/form-data; boundary=x")), ("extension", dict(filename="a.txt")), ("exe", dict(filename="a.pdf.exe")),
        ):  # fmt: skip
            with self.subTest(case=label):
                response = self.upload(**kwargs)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (415, "unsupported_document_type"))
        self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(self.root)), ([], [], []))

    def test_a_missing_or_hostile_name_is_a_400_with_a_closed_reason(self):
        for filename in (None, "", "%00.pdf", "%FF.pdf", "..", "%2F", "a" * 5000):
            with self.subTest(filename=filename if filename is None else filename[:10]):
                response = self.upload(filename=filename)
                self.assertEqual((response.status_code, response.json()["error"]), (400, {"code": "invalid_document", "message": "The document is not acceptable.", "details": {"reason": "invalid_filename"}}))

    def test_content_that_is_not_a_complete_pdf_is_refused_whatever_the_name_and_type(self):
        samples = {
            "text": (b"just some text " * 50, "not_a_pdf"), "zip": (b"PK\x03\x04" + b"\x00" * 300, "not_a_pdf"), "polyglot": (b"PK\x03\x04" + VALID_PDF, "not_a_pdf"),
            "truncated": (VALID_PDF[:-40], "truncated_pdf"), "half": (VALID_PDF[: len(VALID_PDF) // 2], "truncated_pdf"), "malformed": (VALID_PDF.replace(b"/Size", b"/Sizz"), "malformed_pdf"),
        }  # fmt: skip
        for label, (data, reason) in samples.items():
            with self.subTest(case=label):
                response = self.upload(body=data)
                self.assertEqual((response.status_code, response.json()["error"]["code"], response.json()["error"]["details"]), (400, "invalid_document", {"reason": reason}))
        self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(self.root)), ([], [], []))

    def test_an_oversize_body_is_413_before_any_byte_is_read(self):
        data = make_pdf(padding=5000)
        response = self.upload(body=data, env={"AP_DOCUMENT_MAX_BYTES": "4096"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (413, "document_too_large"))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))
        self.assertEqual(self.upload(body=VALID_PDF, env={"AP_DOCUMENT_MAX_BYTES": "4096"}).status_code, 201)

    def test_the_limit_is_exact(self):
        exact = make_pdf(padding=1024 - len(make_pdf(padding=1)) + 1)
        for size, expected in ((len(exact), 201), (len(exact) + 1, 413)):
            data = exact if size == len(exact) else make_pdf(padding=len(exact) - len(VALID_PDF) + 1)
            self.assertEqual(self.upload(body=data, env={"AP_DOCUMENT_MAX_BYTES": str(len(exact))}).status_code, expected)

    def test_a_missing_or_malformed_content_length_is_411(self):
        factory = APIRequestFactory()
        for value in ("", "abc", "-5", "1.5"):
            with self.subTest(value=value):
                request = factory.generic("POST", COLLECTION, VALID_PDF, content_type="application/pdf", HTTP_X_DOCUMENT_FILENAME="a.pdf")
                request.META["CONTENT_LENGTH"] = value
                force_authenticate(request, user=fake_user())
                env_patch, policy_patch = self.environment()
                with env_patch, policy_patch, patched_documents(self.db, OPERATOR):
                    response = views_documents.DocumentCollectionView.as_view(store_factory=__import__("config.document_storage", fromlist=["x"]).build_document_store)(request)
                    response.render()
                self.assertEqual((response.status_code, json.loads(response.content)["error"]["code"]), (411, "length_required"))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))

    def test_a_body_shorter_or_longer_than_declared_is_refused_and_leaves_nothing(self):
        for declared in (len(VALID_PDF) + 50, len(VALID_PDF) - 50):
            with self.subTest(declared=declared):
                factory = APIRequestFactory()
                request = factory.generic("POST", COLLECTION, VALID_PDF, content_type="application/pdf", HTTP_X_DOCUMENT_FILENAME="a.pdf")
                request.META["CONTENT_LENGTH"] = str(declared)
                force_authenticate(request, user=fake_user())
                env_patch, policy_patch = self.environment()
                from config.document_storage import build_document_store

                with env_patch, policy_patch, patched_documents(self.db, OPERATOR):
                    response = views_documents.DocumentCollectionView.as_view(store_factory=build_document_store)(request)
                    response.render()
                self.assertEqual((response.status_code, json.loads(response.content)["error"]["details"]), (400, {"reason": "length_mismatch"}))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))

    def test_a_multipart_body_is_refused_and_nothing_is_written_anywhere(self):
        boundary = "BOUNDARYxyz"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode() + make_pdf(padding=400_000) + f"\r\n--{boundary}--\r\n".encode()

        def refused(*args, **kwargs):
            raise AssertionError("a temporary file was requested")

        with mock.patch.object(tempfile, "mkstemp", refused), mock.patch.object(tempfile, "NamedTemporaryFile", refused), mock.patch.object(tempfile, "SpooledTemporaryFile", refused):
            response = self.upload(body=body, content_type=f"multipart/form-data; boundary={boundary}")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (415, "unsupported_document_type"))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))

    def test_an_unconfigured_storage_is_503_disabled_and_nothing_is_stored(self):
        self.client.force_authenticate(user=fake_user())
        with mock.patch.dict(os.environ, {}, clear=False), patched_documents(self.db, OPERATOR):
            os.environ.pop("AP_DOCUMENT_STORAGE_DIRECTORY", None)
            response = self.client.post(COLLECTION, data=VALID_PDF, content_type="application/pdf", HTTP_X_DOCUMENT_FILENAME="a.pdf")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "document_storage_disabled"))
        self.assertEqual(self.db.documents.rows, [])

    def test_an_unsafe_or_missing_directory_is_503_unavailable_without_a_path(self):
        for directory in (os.path.join(self.root, "missing"), "relative/path", "."):
            with self.subTest(directory=directory[-12:]):
                response = self.upload(env={"AP_DOCUMENT_STORAGE_DIRECTORY": directory})
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "document_storage_unavailable"))
                self.assertNotIn(self.root, response.content.decode("latin-1"))
        response = self.upload(env={"AP_DOCUMENT_MAX_BYTES": "banana"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "document_storage_unavailable"))
        self.assertEqual(self.db.documents.rows, [])

    def test_an_asgi_request_is_refused_because_its_body_is_already_in_the_system_temporary_directory(self):
        from django.core.handlers.asgi import ASGIRequest

        self.assertTrue(views_documents.ASGIRequest is ASGIRequest)
        with mock.patch.object(views_documents, "ASGIRequest", WSGIRequest):  # the test client builds WSGI requests: stand one in for the ASGI class
            response = self.upload()
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "document_storage_unavailable"))
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))

    def test_users_without_the_upload_permission_get_403_and_nothing_is_stored(self):
        for who in (NOBODY, APPROVER, AUDITOR):
            with self.subTest(pk=who.pk), self.assertLogs("apps.accounts.authz", "WARNING"):
                response = self.upload(who, filename="%00", content_type="text/plain")  # invalid input too: the refusal comes first
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))
        self.assertEqual((self.db.documents.rows, self.db.events, tree_listing(self.root)), ([], [], []))

    def test_an_audit_failure_is_a_bare_500_and_the_object_is_removed(self):
        self.db.audit_error = RuntimeError("audit down " + SENTINEL_NAME)
        self.client.raise_request_exception = False
        response = self.upload()
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (500, "internal_error"))
        self.assertNotIn(SENTINEL_NAME, response.content.decode())
        self.assertEqual((self.db.documents.rows, tree_listing(self.root)), ([], []))
        self.texts.append(response.content.decode())
        self.assertNoLeak()

    def test_uploads_are_throttled_per_user_but_reads_are_not_counted_against_the_upload_limit(self):
        with override_settings(API_THROTTLE_RATES={**RATES, "document_upload": "2/min"}):
            codes = [self.upload().status_code for _ in range(3)]
            self.assertEqual(codes, [201, 201, 429])
            self.assertEqual(self.call("get", COLLECTION).status_code, 200)
            response = self.upload()
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (429, "throttled"))
        self.assertEqual(len(self.db.documents.rows), 2)

    def test_the_throttle_is_applied_to_post_only(self):
        view = views_documents.DocumentCollectionView()
        view.request = mock.Mock(method="GET")
        self.assertEqual([type(t).__name__ for t in view.get_throttles()], ["ApiUserThrottle"])
        view.request = mock.Mock(method="POST")
        self.assertEqual([type(t).__name__ for t in view.get_throttles()], ["ApiUserThrottle", "DocumentUploadThrottle", "DocumentUploadHourThrottle"])


class ReadContractTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.mine = self.upload(OPERATOR).json()
        self.theirs = self.upload(OTHER_OPERATOR, filename="theirs.pdf", body=make_pdf(marker=b"other")).json()

    def test_the_list_shows_an_operator_only_their_own_and_an_approver_all(self):
        mine = self.call("get", COLLECTION, OPERATOR).json()
        self.assertEqual((mine["count"], [d["id"] for d in mine["results"]]), (1, [self.mine["id"]]))
        everything = self.call("get", COLLECTION, APPROVER).json()
        self.assertEqual((everything["count"], {d["id"] for d in everything["results"]}), (2, {self.mine["id"], self.theirs["id"]}))
        self.assertEqual(set(everything), {"count", "page", "page_size", "results"})
        for item in everything["results"]:
            self.assertEqual(set(item), OUTPUT_KEYS)

    def test_the_list_needs_a_view_permission_and_validates_its_query(self):
        with self.assertLogs("apps.accounts.authz", "WARNING"):
            self.assertEqual(self.call("get", COLLECTION, NOBODY).status_code, 403)
        for query in ("?page=0", "?page_size=101", "?page=x", "?page_size=0", "?bogus=1", "?page=1&state=uploaded"):
            with self.subTest(query=query):
                response = self.call("get", COLLECTION + query, APPROVER)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
        page = self.call("get", COLLECTION + "?page=2&page_size=1", APPROVER).json()
        self.assertEqual((page["count"], page["page"], page["page_size"], len(page["results"])), (2, 2, 1, 1))

    def test_metadata_of_one_document(self):
        response = self.call("get", f"/api/documents/{self.mine['id']}/", OPERATOR)
        self.assertEqual((response.status_code, response.json()), (200, self.mine))
        self.assertEqual(self.call("get", f"/api/documents/{self.mine['id']}/", APPROVER).status_code, 200)
        self.assertEqual(self.call("get", f"/api/documents/{self.mine['id']}/", AUDITOR).json()["allowed_actions"], [])

    def test_somebody_elses_document_and_an_unknown_one_look_exactly_the_same(self):
        with self.assertLogs("apps.accounts.authz", "WARNING"):
            hidden = self.call("get", f"/api/documents/{self.theirs['id']}/", OPERATOR)
        unknown = self.call("get", f"/api/documents/{uuid.uuid4()}/", OPERATOR)
        self.assertEqual((hidden.status_code, unknown.status_code), (404, 404))
        self.assertEqual(hidden.json(), unknown.json())
        self.assertEqual(unknown.json(), {"error": {"code": "not_found", "message": "The resource does not exist."}})

    def test_a_user_with_no_view_permission_gets_403_before_any_lookup(self):
        for document_id in (self.mine["id"], str(uuid.uuid4())):
            with self.assertLogs("apps.accounts.authz", "WARNING"):
                response = self.call("get", f"/api/documents/{document_id}/", NOBODY)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))


class DownloadContractTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.responses = []
        self.data = make_pdf(marker=SENTINEL_TEXT, padding=300_000)
        self.item = self.upload(OPERATOR, body=self.data, filename=urllib.parse.quote(SENTINEL_NAME + ".pdf")).json()
        self.url = f"/api/documents/{self.item['id']}/content/"
        self.db.events.clear()
        self.capture.lines.clear()

    def fetch(self, who=OPERATOR, **extra):
        response = self.call("get", self.url, who, **extra)
        self.responses.append(response)
        return response

    def tearDown(self):
        for response in self.responses:
            response.close()

    def test_the_owner_downloads_the_exact_bytes_as_a_streamed_attachment(self):
        response = self.fetch()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        self.assertEqual(b"".join(response.streaming_content), self.data)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Length"], str(len(self.data)))
        self.assertEqual(response["Content-Disposition"], f"attachment; filename=\"{SENTINEL_NAME}.pdf\"")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(response["Accept-Ranges"], "none")
        for word in ("no-store", "no-cache", "private"):
            self.assertIn(word, response["Cache-Control"])

    def test_the_body_is_read_lazily_in_bounded_chunks(self):
        response = self.fetch()
        chunks = response.streaming_content
        first = next(iter(chunks))
        self.assertLessEqual(len(first), 64 * 1024)
        self.assertLess(len(first), len(self.data))
        rest = list(chunks)
        self.assertEqual(len(first) + sum(len(c) for c in rest), len(self.data))
        self.assertTrue(all(len(c) <= 64 * 1024 for c in rest))

    def test_the_object_is_released_when_the_response_is_closed_even_if_never_read(self):
        response = self.fetch()
        response.close()
        path = os.path.join(self.root, "objects", *self.db.documents.rows[0].storage_key.split("/"))
        os.unlink(path)  # a still-open handle would refuse on Windows
        self.assertFalse(os.path.exists(path))

    def test_the_client_accept_header_does_not_decide_the_answer(self):
        for accept in ("application/pdf", "text/html", "*/*", "image/png", "application/json"):
            with self.subTest(accept=accept):
                response = self.fetch(HTTP_ACCEPT=accept)
                self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/pdf"))
                response.close()

    def test_the_download_is_audited_with_the_actor_and_safe_metadata(self):
        self.fetch().close()
        (event,) = self.db.events
        self.assertEqual((event.action, event.actor_public_id, str(event.subject_id)), ("document.downloaded", OPERATOR.public_id, self.item["id"]))
        self.assertEqual(event.details, {"byte_length": len(self.data), "content_hash": self.item["sha256"], "by_owner": True})

    def test_an_approver_may_download_and_the_audit_says_it_was_not_the_owner(self):
        self.fetch(APPROVER).close()
        self.assertEqual(self.db.events[0].details["by_owner"], False)

    def test_everybody_else_is_refused_without_a_byte_or_an_event(self):
        with self.assertLogs("apps.accounts.authz", "WARNING"):
            other = self.fetch(OTHER_OPERATOR)
            auditor = self.fetch(AUDITOR)
            nobody = self.fetch(NOBODY)
        self.assertEqual([(r.status_code, r.json()["error"]["code"]) for r in (other, auditor, nobody)], [(404, "not_found"), (403, "permission_denied"), (403, "permission_denied")])
        for response in (other, auditor, nobody):
            self.assertFalse(response.streaming)
            self.assertNotIn("Content-Disposition", response)
        self.assertEqual(self.db.events, [])

    def test_an_unknown_document_is_404_and_has_no_event(self):
        response = self.call("get", f"/api/documents/{uuid.uuid4()}/content/")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.db.events, [])

    def test_a_missing_or_changed_object_is_503_without_a_path_and_without_an_event(self):
        path = os.path.join(self.root, "objects", *self.db.documents.rows[0].storage_key.split("/"))
        with open(path, "ab") as handle:
            handle.write(b"x")
        response = self.fetch()
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "document_storage_unavailable"))
        self.assertNotIn(self.root, response.content.decode())
        os.unlink(path)
        self.assertEqual(self.fetch().status_code, 503)
        self.assertEqual(self.db.events, [])

    def test_a_failed_audit_write_stops_the_download(self):
        self.db.audit_error = RuntimeError("audit down")
        self.client.raise_request_exception = False
        response = self.fetch()
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (500, "internal_error"))
        self.assertFalse(response.streaming)

    def test_a_filename_cannot_inject_a_header_or_break_the_disposition(self):
        for name in ('a"b;c.pdf', "x\\y.pdf", "caf\u00e9 \u8acb.pdf", "semi;colon=1.pdf", "q'uote.pdf"):
            with self.subTest(name=name):
                item = self.upload(OPERATOR, filename=urllib.parse.quote(name)).json()
                response = self.call("get", f"/api/documents/{item['id']}/content/")
                self.responses.append(response)
                header = response["Content-Disposition"]
                self.assertTrue(header.startswith("attachment; "))
                self.assertNotRegex(header, r"[\r\n\x00]")
                self.assertEqual(header.count("attachment"), 1)
                self.assertEqual(urllib.parse.unquote(header.split("filename*=utf-8''", 1)[1]) if "filename*=" in header else self._plain_name(header), name.rsplit("\\", 1)[-1])
                self.assertNotIn("Set-Cookie", response)

    @staticmethod
    def _plain_name(header):
        return re.search(r'filename="((?:[^"\\]|\\.)*)"', header).group(1).replace('\\"', '"').replace("\\\\", "\\")

    def test_nothing_in_a_response_or_a_log_line_reveals_a_path_the_storage_key_or_content(self):
        self.responses = [self.fetch(), self.fetch(APPROVER)]
        for response in self.responses:
            for name, value in response.items():
                for forbidden in (self.root, self.db.documents.rows[0].storage_key, SENTINEL_TEXT.decode()):
                    self.assertNotIn(forbidden, f"{name}: {value}")
        for response in self.responses:
            response.close()
        self.assertNoLeak(self.db.documents.rows[0].storage_key, self.item["sha256"])  # the logs hold identifiers only
        self.assertTrue(any("Document download started" in line for line in self.capture.lines))


class UploadHandlersTests(SimpleTestCase):
    def test_a_large_multipart_file_is_discarded_never_staged_in_a_temporary_file(self):
        boundary = "B"
        payload = b"x" * 1_000_000
        body = f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode() + payload + f"\r\n--{boundary}--\r\n".encode()

        def refused(*args, **kwargs):
            raise AssertionError("a temporary file was requested")

        request = RequestFactory().post("/x/", data=body, content_type=f"multipart/form-data; boundary={boundary}")
        with mock.patch.object(tempfile, "mkstemp", refused), mock.patch.object(tempfile, "NamedTemporaryFile", refused), mock.patch.object(tempfile, "SpooledTemporaryFile", refused):
            self.assertEqual(len(request.FILES), 0)
        self.assertEqual([type(h).__name__ for h in request.upload_handlers], ["MemoryFileUploadHandler"])
