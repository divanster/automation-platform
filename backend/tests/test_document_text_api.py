"""DB-free tests of the text extraction API (DOC-2): routing, session authentication, CSRF, the response contract (status routes carry no text field, the text route carries the pages), the error mapping,
throttling, caching headers and the absence of extracted text from every response but the one that is meant to carry it. The views, serializers and error handler are the real ones; the services are replaced by
stand-ins that return the real read models (the services themselves are tested in ``tests/test_document_text_services.py``). The PostgreSQL behaviour is in ``tests/test_document_text_postgres.py``
and is NOT run in the development VM."""

import datetime
import json
import logging
import uuid
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.errors import AuthorizationError
from apps.accounts.models import User
from apps.api import views_document_text
from apps.api.parsers import UnreadBodyParser
from apps.document_text import rules, services
from apps.document_text.errors import ExtractionNotReadyError
from apps.documents.errors import DocumentNotFoundError

DOCUMENT = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EXTRACTION = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REQUESTER = uuid.UUID("22222222-2222-4222-8222-222222222222")
SHA = "cd" * 32
NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
SENTINEL = "EXTRACTED-SENTINEL-93aa"
COLLECTION = f"/api/documents/{DOCUMENT}/text-extractions/"
DETAIL = f"{COLLECTION}{EXTRACTION}/"
TEXT = f"{DETAIL}text/"
RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min", "document_text": "1000/min", "document_text_hour": "1000/hour"}
STATUS_KEYS = {"id", "document_id", "requested_at", "requested_by", "status", "error_code", "extractor", "source", "page_count", "char_count", "text_sha256", "completed_at"}


def fake_user():
    return User(pk=7, username="synthetic-user", is_active=True)


def item(status="extracted", code="", **overrides):
    done = status not in ("queued", "running")
    fields = dict(
        id=EXTRACTION, document_id=DOCUMENT, requested_at=NOW, requested_by=REQUESTER, status=status, error_code=code, extractor_name="pypdf" if done else None, extractor_version="7.1.1" if done else None,
        page_count=2 if done else None, char_count=20 if done else None, source_sha256=SHA if done else None, source_size_bytes=999 if done else None, text_sha256="ef" * 32 if done else None,
        completed_at=NOW if done else None,
    )  # fmt: skip
    fields.update(overrides)
    return services.ExtractionItem(**fields)


def extracted_text():
    pages = (
        services.PageItem(1, "text", "", SENTINEL, len(SENTINEL), rules.source_reference(DOCUMENT, SHA, 1)),
        services.PageItem(2, "failed", "page_extraction_failed", "", 0, rules.source_reference(DOCUMENT, SHA, 2)),
    )
    return services.ExtractedText(item("partial", "some_pages_failed"), pages)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args))


class ApiCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = APIClient()
        self.capture = Capture()
        root = logging.getLogger()
        previous = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, previous)

    def call(self, method, path, **patches):
        self.client.force_authenticate(user=fake_user())
        with mock.patch.multiple(views_document_text.services, **patches):
            return getattr(self.client, method)(path)


class RoutingTests(ApiCase):
    def test_the_routes_resolve_to_the_three_views_and_reverse_to_the_same_paths(self):
        self.assertIs(resolve(COLLECTION).func.cls, views_document_text.TextExtractionCollectionView)
        self.assertIs(resolve(DETAIL).func.cls, views_document_text.TextExtractionDetailView)
        self.assertIs(resolve(TEXT).func.cls, views_document_text.TextExtractionTextView)
        self.assertEqual(reverse("api:document-text-collection", kwargs={"document_id": DOCUMENT}), COLLECTION)
        self.assertEqual(reverse("api:document-text-detail", kwargs={"document_id": DOCUMENT, "extraction_id": EXTRACTION}), DETAIL)
        self.assertEqual(reverse("api:document-text-text", kwargs={"document_id": DOCUMENT, "extraction_id": EXTRACTION}), TEXT)

    def test_paths_that_are_not_these_routes_are_404(self):
        self.client.force_authenticate(user=fake_user())
        for path in (f"/api/documents/not-a-uuid/text-extractions/", f"{COLLECTION}not-a-uuid/", f"{DETAIL}text/more/", f"{DETAIL}content/", f"/api/documents/{DOCUMENT}/text/", f"/api/documents/{DOCUMENT}/extract/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_only_the_documented_methods_exist(self):
        expected = {views_document_text.TextExtractionCollectionView: {"get", "post"}, views_document_text.TextExtractionDetailView: {"get"}, views_document_text.TextExtractionTextView: {"get"}}
        for cls, methods in expected.items():
            self.assertEqual({m for m in ("get", "post", "put", "patch", "delete") if hasattr(cls, m)}, methods)
        self.client.force_authenticate(user=fake_user())
        for method, path in (("put", COLLECTION), ("delete", COLLECTION), ("post", DETAIL), ("put", DETAIL), ("post", TEXT), ("delete", TEXT)):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 405)


class AuthenticationTests(ApiCase):
    def test_anonymous_requests_are_403_not_authenticated_and_call_no_service(self):
        anonymous = APIClient(enforce_csrf_checks=True)
        with mock.patch.multiple(views_document_text.services, request_text_extraction=mock.DEFAULT, list_text_extractions=mock.DEFAULT, get_text_extraction=mock.DEFAULT, read_extracted_text=mock.DEFAULT) as services_:
            for method, path in (("get", COLLECTION), ("post", COLLECTION), ("get", DETAIL), ("get", TEXT)):
                with self.subTest(method=method, path=path):
                    response = getattr(anonymous, method)(path)
                    self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))
            for called in services_.values():
                called.assert_not_called()


class CsrfTests(ApiCase):
    """The REAL ``SessionAuthentication`` with CSRF enforced: a request that changes state needs the token; a read does not."""

    def setUp(self):
        super().setUp()
        self.secret = _get_new_csrf_string()
        self.token = _mask_cipher_secret(self.secret)

    def post(self, *, token="valid", cookie=True, data=None, content_type=None):
        factory = APIRequestFactory(enforce_csrf_checks=True)
        if cookie:
            factory.cookies[settings.CSRF_COOKIE_NAME] = self.secret
        extra = {}
        if token is not None:
            extra["HTTP_X_CSRFTOKEN"] = self.token if token == "valid" else token
        request = factory.generic("POST", COLLECTION, data or b"", content_type=content_type or "application/octet-stream", **extra)
        request.user = fake_user()
        with mock.patch.object(views_document_text.services, "request_text_extraction", return_value=services.RequestOutcome(item("queued"), True)) as service:
            response = views_document_text.TextExtractionCollectionView.as_view()(request, document_id=DOCUMENT)
            body = json.loads(response.rendered_content)
        return response, body, service

    def test_a_valid_token_lets_the_request_through_whatever_the_body(self):
        for data, content_type in ((None, None), (b"{}", "application/json"), (b"not json at all", "application/json"), (b"%PDF-1.4 whatever", "application/pdf"), (b"a=b", "application/x-www-form-urlencoded")):
            with self.subTest(content_type=content_type):
                response, body, service = self.post(data=data, content_type=content_type)
                self.assertEqual(response.status_code, 201)
                service.assert_called_once()
                self.assertEqual(set(service.call_args.kwargs), {"user", "document_id"})  # nothing from the body reaches the service

    def test_a_missing_or_invalid_token_or_cookie_is_403_csrf_failed_and_the_service_never_runs(self):
        for label, kwargs in (("no token", {"token": None}), ("garbage", {"token": "not-a-token"}), ("another secret", {"token": _mask_cipher_secret(_get_new_csrf_string())}), ("no cookie", {"cookie": False})):
            with self.subTest(case=label):
                response, body, service = self.post(**kwargs)
                self.assertEqual((response.status_code, body["error"]["code"]), (403, "csrf_failed"))
                service.assert_not_called()

    def test_reads_need_no_csrf_token(self):
        request = APIRequestFactory(enforce_csrf_checks=True).get(COLLECTION)
        request.user = fake_user()
        page = services.ExtractionPage((item("queued"),), 1, 1, 20)
        with mock.patch.object(views_document_text.services, "list_text_extractions", return_value=page):
            self.assertEqual(views_document_text.TextExtractionCollectionView.as_view()(request, document_id=DOCUMENT).status_code, 200)

    def test_the_collection_view_reads_no_body_at_all(self):
        self.assertEqual(views_document_text.TextExtractionCollectionView.parser_classes, [UnreadBodyParser])


class RequestTests(ApiCase):
    def test_a_new_extraction_is_201_with_the_status_and_no_text(self):
        response = self.call("post", COLLECTION, request_text_extraction=mock.Mock(return_value=services.RequestOutcome(item("queued"), True)))
        body = response.json()
        self.assertEqual((response.status_code, set(body)), (201, STATUS_KEYS))
        self.assertEqual((body["id"], body["document_id"], body["status"], body["error_code"], body["extractor"], body["source"], body["page_count"]), (str(EXTRACTION), str(DOCUMENT), "queued", None, None, None, None))

    def test_an_existing_extraction_is_200(self):
        response = self.call("post", COLLECTION, request_text_extraction=mock.Mock(return_value=services.RequestOutcome(item("extracted"), False)))
        self.assertEqual((response.status_code, response.json()["status"]), (200, "extracted"))

    def test_the_service_gets_the_document_from_the_path_and_the_user_from_the_session_only(self):
        service = mock.Mock(return_value=services.RequestOutcome(item("queued"), True))
        self.call("post", f"{COLLECTION}?limit=1&user=9&max_pages=99", request_text_extraction=service)
        service.assert_called_once()
        self.assertEqual(set(service.call_args.kwargs), {"user", "document_id"})
        self.assertEqual(service.call_args.kwargs["document_id"], DOCUMENT)
        self.assertEqual(service.call_args.kwargs["user"].pk, 7)

    def test_the_listing_is_paginated_in_the_projects_envelope(self):
        page = services.ExtractionPage((item("queued"), item("failed", "encrypted_pdf", page_count=None)), 2, 1, 20)
        service = mock.Mock(return_value=page)
        response = self.call("get", f"{COLLECTION}?page=1&page_size=20", list_text_extractions=service)
        body = response.json()
        self.assertEqual((response.status_code, body["count"], body["page"], body["page_size"], len(body["results"])), (200, 2, 1, 20, 2))
        self.assertEqual(set(body["results"][0]), STATUS_KEYS)
        self.assertEqual(service.call_args.kwargs["page_size"], 20)

    def test_a_bad_page_query_is_a_validation_error(self):
        for query in ("page=0", "page_size=101", "page=x", "unknown=1"):
            with self.subTest(query=query):
                response = self.call("get", f"{COLLECTION}?{query}", list_text_extractions=mock.Mock())
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))


class StatusAndTextTests(ApiCase):
    def test_the_detail_route_returns_status_and_facts_with_no_text_field(self):
        response = self.call("get", DETAIL, get_text_extraction=mock.Mock(return_value=item("partial", "some_pages_failed")))
        body = response.json()
        self.assertEqual((response.status_code, set(body)), (200, STATUS_KEYS))
        self.assertEqual((body["status"], body["error_code"], body["extractor"], body["source"], body["page_count"], body["char_count"]),
                         ("partial", "some_pages_failed", {"name": "pypdf", "version": "7.1.1"}, {"document_id": str(DOCUMENT), "sha256": SHA, "size_bytes": 999}, 2, 20))  # fmt: skip
        self.assertNotIn("text", json.dumps(body).replace("text_sha256", ""))

    def test_the_text_route_returns_the_pages_with_their_provenance(self):
        response = self.call("get", TEXT, read_extracted_text=mock.Mock(return_value=extracted_text()))
        body = response.json()
        self.assertEqual((response.status_code, set(body)), (200, {"extraction", "pages"}))
        self.assertEqual(set(body["extraction"]), STATUS_KEYS)
        first, second = body["pages"]
        self.assertEqual(set(first), {"page_number", "status", "error_code", "text", "char_count", "source_reference"})
        self.assertEqual((first["page_number"], first["status"], first["error_code"], first["text"], first["source_reference"]), (1, "text", None, SENTINEL, f"document:{DOCUMENT}:sha256:{SHA}:page:1"))
        self.assertEqual((second["page_number"], second["status"], second["error_code"], second["text"]), (2, "failed", "page_extraction_failed", ""))

    def test_the_extracted_text_appears_in_no_other_response(self):
        responses = [
            self.call("get", DETAIL, get_text_extraction=mock.Mock(return_value=item("extracted"))),
            self.call("get", COLLECTION, list_text_extractions=mock.Mock(return_value=services.ExtractionPage((item("extracted"),), 1, 1, 20))),
            self.call("post", COLLECTION, request_text_extraction=mock.Mock(return_value=services.RequestOutcome(item("extracted"), False))),
        ]
        for response in responses:
            self.assertNotIn(SENTINEL, response.content.decode())

    def test_every_response_is_never_cached(self):
        for response in (
            self.call("get", DETAIL, get_text_extraction=mock.Mock(return_value=item("queued"))),
            self.call("get", TEXT, read_extracted_text=mock.Mock(return_value=extracted_text())),
            self.call("get", TEXT, read_extracted_text=mock.Mock(side_effect=ExtractionNotReadyError())),
            self.call("get", DETAIL, get_text_extraction=mock.Mock(side_effect=DocumentNotFoundError())),
        ):
            self.assertIn("no-store", response["Cache-Control"])

    def test_no_text_reaches_a_log_line(self):
        self.call("get", TEXT, read_extracted_text=mock.Mock(return_value=extracted_text()))
        self.assertNotIn(SENTINEL, "\n".join(self.capture.lines))


class ErrorTests(ApiCase):
    def error(self, method, path, exception, service):
        response = self.call(method, path, **{service: mock.Mock(side_effect=exception)})
        return response.status_code, response.json()["error"]

    def test_a_missing_permission_is_403_and_leaves_the_message_fixed(self):
        for method, path, service in (("post", COLLECTION, "request_text_extraction"), ("get", COLLECTION, "list_text_extractions"), ("get", DETAIL, "get_text_extraction"), ("get", TEXT, "read_extracted_text")):
            with self.subTest(service=service):
                status, error = self.error(method, path, AuthorizationError("missing_permission"), service)
                self.assertEqual((status, error["code"]), (403, "permission_denied"))
                self.assertEqual(error["message"], "You do not have permission to do this.")

    def test_a_document_or_extraction_the_caller_may_not_see_is_the_same_404_as_an_unknown_one(self):
        statuses = []
        for exception in (AuthorizationError("out_of_scope"), DocumentNotFoundError()):
            response = self.call("get", DETAIL, get_text_extraction=mock.Mock(side_effect=exception))
            statuses.append((response.status_code, json.dumps(response.json(), sort_keys=True)))
        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual(statuses[0][0], 404)

    def test_reading_the_text_before_the_result_exists_is_409_extraction_not_ready(self):
        status, error = self.error("get", TEXT, ExtractionNotReadyError(), "read_extracted_text")
        self.assertEqual((status, error["code"], error["message"]), (409, "extraction_not_ready", "The text extraction has not finished."))

    def test_an_unexpected_error_is_a_bare_500_that_names_only_its_class_in_the_log(self):
        status, error = self.error("get", DETAIL, RuntimeError(SENTINEL), "get_text_extraction")
        self.assertEqual((status, error), (500, {"code": "internal_error", "message": "An internal error occurred."}))
        self.assertNotIn(SENTINEL, "\n".join(self.capture.lines))


class ThrottleTests(ApiCase):
    def test_only_the_request_is_throttled_by_the_extraction_limits(self):
        factory = APIRequestFactory()
        for method, expected in (("post", ["ApiUserThrottle", "DocumentTextThrottle", "DocumentTextHourThrottle"]), ("get", ["ApiUserThrottle"])):
            request = getattr(factory, method)(COLLECTION)
            view = views_document_text.TextExtractionCollectionView()
            view.request = view.initialize_request(request)
            self.assertEqual([type(t).__name__ for t in view.get_throttles()], expected)

    def test_the_request_is_refused_with_429_past_the_limit_and_nothing_is_queued(self):
        self.client.force_authenticate(user=fake_user())
        service = mock.Mock(return_value=services.RequestOutcome(item("queued"), True))
        with override_settings(API_THROTTLE_RATES={**RATES, "document_text": "2/min", "document_text_hour": "100/hour"}), mock.patch.object(views_document_text.services, "request_text_extraction", service):
            codes = [self.client.post(COLLECTION).status_code for _ in range(4)]
        self.assertEqual(codes, [201, 201, 429, 429])
        self.assertEqual(service.call_count, 2)

    def test_the_rates_are_strict_and_hourly_is_below_sixty_times_the_per_minute_rate(self):
        base = __import__("config.settings.base", fromlist=["API_THROTTLE_RATES"])
        per_minute = lambda r: int(r.split("/")[0]) * {"min": 1, "hour": 1 / 60}[r.split("/")[1]]  # noqa: E731
        self.assertLess(per_minute(base.API_THROTTLE_RATES["document_text"]), per_minute(base.API_THROTTLE_RATES["document_upload"]))
        self.assertLess(per_minute(base.API_THROTTLE_RATES["document_text_hour"]), per_minute(base.API_THROTTLE_RATES["document_text"]) * 60 + 1)
