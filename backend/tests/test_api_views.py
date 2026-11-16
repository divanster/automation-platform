"""DB-free tests: the API views, through the real URL, middleware, CSRF, throttling and error stack.

No database is touched (``SimpleTestCase`` forbids it): Django's ``authenticate``/``login``/``logout`` and the
services are replaced with mocks, so what is tested is the TRANSPORT: routes, CSRF enforcement, throttling,
parsing, serialization, status codes, and that each view hands the right arguments, and ONLY those, to ONE service.
The services themselves are tested in the review and PostgreSQL tests.
"""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.errors import AuthorizationError
from apps.accounts.models import User
from apps.accounts.profile import UserProfile
from apps.api import views_auth, views_requests
from apps.cases.errors import RecordNotFoundError
from apps.intake import queries
from apps.intake.errors import (
    ClientRequestConflictError,
    DraftInvalidError,
    SelfApprovalError,
    StaleRevisionError,
    WarningsNotAcknowledgedError,
)
from apps.intake.policy import RequestTextError
from apps.intake.review import ApprovalResult, EditResult, RejectionResult
from workflows.request_intake import RequestedItem, Requester, StructuredRequest
from workflows.revision_approval import RevisionFinding

RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "1000/min"}
RUN, REVISION, CASE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
HASH = "a" * 64
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
PROFILE = UserProfile(public_id=uuid.uuid4(), username="olive", is_superuser=False, roles=("Operator",), permissions=("intake.submit_request",))


def body(response):
    response.render()
    return json.loads(response.content) if response.content else None


def fake_user(pk=7, active=True):
    return User(pk=pk, username="synthetic-user", is_active=active)


class ApiTestCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = override_settings(API_THROTTLE_RATES=RATES)
        patcher.enable()
        self.addCleanup(patcher.disable)


# --- the CSRF bootstrap, login, logout, me ---------------------------------------------------------------


class CsrfAndLoginTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient(enforce_csrf_checks=True)

    def token(self):
        response = self.client.get("/api/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def login(self, username="olive", password="synthetic-password", token=True, **extra):
        headers = {"HTTP_X_CSRFTOKEN": self.token()} if token is True else ({"HTTP_X_CSRFTOKEN": token} if token else {})
        return self.client.post("/api/auth/login/", {"username": username, "password": password}, format="json", **headers, **extra)

    def test_the_csrf_bootstrap_is_public_sets_the_cookie_and_returns_the_token(self):
        response = self.client.get("/api/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"csrf_token"})
        self.assertTrue(response.json()["csrf_token"])
        cookie = response.cookies["csrftoken"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_csrf_bootstrap_does_not_accept_unsafe_methods(self):
        self.assertEqual(self.client.post("/api/auth/csrf/", {}, format="json").status_code, 405)

    def test_login_without_a_csrf_token_is_refused_before_credentials_are_read(self):
        with mock.patch.object(views_auth, "authenticate") as authenticate:
            response = self.client.post("/api/auth/login/", {"username": "olive", "password": "x"}, format="json")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "csrf_failed"))
        authenticate.assert_not_called()

    def test_login_with_a_wrong_csrf_token_is_refused(self):
        self.token()  # a real cookie is set; the header is wrong
        with mock.patch.object(views_auth, "authenticate") as authenticate:
            response = self.client.post("/api/auth/login/", {"username": "olive", "password": "x"}, format="json", HTTP_X_CSRFTOKEN="wrong-token")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "csrf_failed"))
        authenticate.assert_not_called()

    def test_login_with_a_header_but_no_cookie_is_refused(self):
        token = self.token()
        self.client.cookies.clear()
        with mock.patch.object(views_auth, "authenticate") as authenticate:
            response = self.client.post("/api/auth/login/", {"username": "u", "password": "p"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 403)
        authenticate.assert_not_called()

    def test_a_failed_login_is_one_generic_response_whatever_the_reason(self):
        # Django's authenticate returns None for an unknown user, a wrong password AND an inactive user.
        with mock.patch.object(views_auth, "authenticate", return_value=None), mock.patch.object(views_auth, "django_login") as login:
            first = self.login("nobody", "wrong-password-1")
            second = self.login("inactive-user", "right-password-but-inactive")
        self.assertEqual((first.status_code, second.status_code), (403, 403))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["error"]["code"], "invalid_credentials")
        login.assert_not_called()
        self.assertIn("no-store", first["Cache-Control"])  # errors are never cached either
        text = json.dumps(first.json())
        for secret in ("nobody", "wrong-password", "inactive", "olive"):
            self.assertNotIn(secret, text)

    def test_a_successful_login_starts_the_session_and_returns_the_profile_and_a_fresh_token(self):
        user = fake_user()
        with mock.patch.object(views_auth, "authenticate", return_value=user) as authenticate, mock.patch.object(
            views_auth, "django_login"
        ) as login, mock.patch.object(views_auth, "current_user_profile", return_value=PROFILE):
            response = self.login("olive", "synthetic-password")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"public_id", "username", "is_superuser", "roles", "permissions", "csrf_token"})
        self.assertEqual((data["username"], data["roles"], data["permissions"]), ("olive", ["Operator"], ["intake.submit_request"]))
        self.assertTrue(data["csrf_token"])
        self.assertEqual(authenticate.call_args.kwargs, {"username": "olive", "password": "synthetic-password"})
        login.assert_called_once()
        self.assertIs(login.call_args.args[1], user)
        self.assertIn("no-store", response["Cache-Control"])

    def test_no_credential_ever_appears_in_a_response(self):
        with mock.patch.object(views_auth, "authenticate", return_value=fake_user()), mock.patch.object(views_auth, "django_login"), mock.patch.object(
            views_auth, "current_user_profile", return_value=PROFILE
        ):
            response = self.login("olive", "synthetic-password-XYZ")
        for needle in ("synthetic-password-XYZ", "password", "pbkdf2"):
            self.assertNotIn(needle, response.content.decode())

    def test_login_input_is_validated(self):
        token = self.token()
        post = lambda data, **kw: self.client.post("/api/auth/login/", data, format="json", HTTP_X_CSRFTOKEN=token, **kw)  # noqa: E731
        with mock.patch.object(views_auth, "authenticate") as authenticate:
            missing = post({})
            unknown = post({"username": "u", "password": "p", "remember": True})
            typed = post({"username": 5, "password": ["x"]})
        self.assertEqual((missing.status_code, missing.json()["error"]["details"]["fields"]), (400, {"username": ["required"], "password": ["required"]}))
        self.assertEqual(unknown.json()["error"]["details"]["fields"], {"remember": ["unknown_field"]})
        self.assertEqual(typed.json()["error"]["details"]["fields"], {"username": ["invalid"], "password": ["invalid"]})
        authenticate.assert_not_called()

    def test_malformed_bodies(self):
        token = self.token()
        raw = lambda data, ctype="application/json": self.client.post("/api/auth/login/", data=data, content_type=ctype, HTTP_X_CSRFTOKEN=token)  # noqa: E731
        for data in ('{"username": ', "not json", "[1, 2]", '"text"', "null", "NaN"):
            with self.subTest(data=data):
                response = raw(data)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "malformed_json"))
        self.assertEqual(raw("a=b", "application/x-www-form-urlencoded").status_code, 415)
        self.assertEqual(raw("hello", "text/plain").json()["error"]["code"], "unsupported_media_type")

    def test_a_deeply_nested_body_is_a_400_not_a_crash(self):
        token = self.token()
        response = self.client.post("/api/auth/login/", data="[" * 50000, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "malformed_json"))

    @override_settings(API_MAX_BODY_BYTES=1024)
    def test_an_oversize_body_is_refused_before_it_is_decoded(self):
        token = self.token()
        response = self.client.post("/api/auth/login/", data=json.dumps({"username": "u", "password": "p" * 5000}), content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (413, "payload_too_large"))

    def test_login_only_accepts_post(self):
        self.assertEqual(self.client.get("/api/auth/login/").status_code, 405)
        self.assertIn("no-store", self.client.get("/api/auth/login/")["Cache-Control"])
        self.assertEqual(self.client.put("/api/auth/login/", {}, format="json").status_code, 403)  # CSRF first

    def test_no_cors_headers_are_ever_sent(self):
        for response in (self.client.get("/api/auth/csrf/"), self.client.options("/api/auth/login/", HTTP_ORIGIN="https://evil.example.invalid")):
            self.assertFalse([h for h in response.headers if h.lower().startswith("access-control-")])


class ProtectedAuthRoutesTests(ApiTestCase):
    factory = APIRequestFactory(enforce_csrf_checks=True)

    def csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        token = client.get("/api/auth/csrf/").json()["csrf_token"]
        return token, client.cookies["csrftoken"].value

    def post_as(self, view, user, token=None, cookie=None):
        request = self.factory.post("/x/", {}, format="json")
        request.user = user  # what AuthenticationMiddleware would set from a valid session
        if token:
            request.META["HTTP_X_CSRFTOKEN"] = token
            request.COOKIES["csrftoken"] = cookie
        return view(request)

    def test_every_protected_route_without_a_session_is_a_403_not_authenticated(self):
        client = APIClient(enforce_csrf_checks=True)
        run = uuid.uuid4()
        calls = [
            ("get", "/api/auth/me/"),
            ("post", "/api/auth/logout/"),
            ("get", "/api/requests/"),
            ("post", "/api/requests/"),
            ("get", f"/api/requests/{run}/"),
            ("post", f"/api/requests/{run}/edit/"),
            ("post", f"/api/requests/{run}/approve/"),
            ("post", f"/api/requests/{run}/reject/"),
            ("get", f"/api/requests/{run}/audit/"),
        ]
        for method, path in calls:
            with self.subTest(route=f"{method.upper()} {path}"):
                response = getattr(client, method)(path)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "not_authenticated"))

    def test_logout_without_csrf_is_refused_and_does_not_log_out(self):
        with mock.patch.object(views_auth, "django_logout") as logout:
            response = self.post_as(views_auth.LogoutView.as_view(), fake_user())
        self.assertEqual((response.status_code, body(response)["error"]["code"]), (403, "csrf_failed"))
        logout.assert_not_called()

    def test_logout_with_csrf_flushes_the_session_and_returns_204(self):
        token, cookie = self.csrf()
        with mock.patch.object(views_auth, "django_logout") as logout:
            response = self.post_as(views_auth.LogoutView.as_view(), fake_user(), token, cookie)
        self.assertEqual(response.status_code, 204)
        logout.assert_called_once()

    def test_an_inactive_users_session_authenticates_nobody(self):
        token, cookie = self.csrf()
        with mock.patch.object(views_auth, "django_logout") as logout:
            response = self.post_as(views_auth.LogoutView.as_view(), fake_user(active=False), token, cookie)
        self.assertEqual((response.status_code, body(response)["error"]["code"]), (403, "not_authenticated"))
        logout.assert_not_called()

    def test_me_returns_the_profile_and_nothing_else(self):
        request = APIRequestFactory().get("/api/auth/me/")
        force_authenticate(request, user=fake_user())
        with mock.patch.object(views_auth, "current_user_profile", return_value=PROFILE):
            response = views_auth.MeView.as_view()(request)
        data = body(response)
        self.assertEqual(set(data), {"public_id", "username", "is_superuser", "roles", "permissions"})
        self.assertIn("no-store", response["Cache-Control"])

    def test_me_for_a_user_removed_since_the_session_began_is_a_403(self):
        request = APIRequestFactory().get("/api/auth/me/")
        force_authenticate(request, user=fake_user())
        with mock.patch.object(views_auth, "current_user_profile", side_effect=AuthorizationError("not_authenticated")):
            response = views_auth.MeView.as_view()(request)
        self.assertEqual((response.status_code, body(response)["error"]["code"]), (403, "not_authenticated"))


# --- login throttling -------------------------------------------------------------------------------------


class LoginThrottleTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient(enforce_csrf_checks=True)
        self.token = self.client.get("/api/auth/csrf/").json()["csrf_token"]

    def attempt(self, username="olive", **extra):
        return self.client.post("/api/auth/login/", {"username": username, "password": "wrong"}, format="json", HTTP_X_CSRFTOKEN=self.token, **extra)

    def rates(self, **overrides):
        return override_settings(API_THROTTLE_RATES={**RATES, **overrides})

    def test_attempts_per_username_are_bounded(self):
        with self.rates(login_user="3/min"), mock.patch.object(views_auth, "authenticate", return_value=None) as authenticate:
            statuses = [self.attempt().status_code for _ in range(5)]
            throttled = self.attempt()
        self.assertEqual(statuses, [403, 403, 403, 429, 429])
        self.assertEqual(authenticate.call_count, 3)  # a throttled attempt never reaches the password check
        data = throttled.json()["error"]
        self.assertEqual(data["code"], "throttled")
        self.assertGreaterEqual(data["details"]["retry_after_seconds"], 1)
        self.assertGreaterEqual(int(throttled["Retry-After"]), 1)

    def test_other_usernames_are_not_blocked_by_one_throttled_username(self):
        with self.rates(login_user="2/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            [self.attempt("victim") for _ in range(3)]
            self.assertEqual(self.attempt("victim").status_code, 429)
            self.assertEqual(self.attempt("someone-else").status_code, 403)

    def test_username_variants_share_one_limit(self):
        with self.rates(login_user="2/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            codes = [self.attempt(name).status_code for name in ("Olive", "OLIVE", "olive", " olive ", "ｏlive")]
        self.assertEqual(codes[:2], [403, 403])
        self.assertEqual(set(codes[2:]), {429})

    def test_attempts_per_ip_are_bounded_whatever_the_username(self):
        with self.rates(login_ip="3/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            statuses = [self.attempt(f"user-{i}").status_code for i in range(5)]
        self.assertEqual(statuses, [403, 403, 403, 429, 429])

    def test_a_forged_x_forwarded_for_does_not_dodge_the_ip_limit(self):
        with self.rates(login_ip="3/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            statuses = [self.attempt(f"user-{i}", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code for i in range(5)]
        self.assertEqual(statuses[3:], [429, 429])

    def test_the_hourly_username_limit_applies_too(self):
        with self.rates(login_user_hour="2/hour"), mock.patch.object(views_auth, "authenticate", return_value=None):
            statuses = [self.attempt().status_code for _ in range(4)]
        self.assertEqual(statuses, [403, 403, 429, 429])

    def test_the_cache_never_holds_a_username_or_a_password(self):
        with mock.patch.object(views_auth, "authenticate", return_value=None):
            self.attempt("distinctive-account-name")
        keys = " ".join(str(k) for k in cache._cache)
        self.assertNotIn("distinctive", keys)
        self.assertNotIn("wrong", keys)
        self.assertIn("login_user", keys)

    def test_a_request_without_a_usable_username_is_not_counted_per_username(self):
        with self.rates(login_user="1/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            first = self.client.post("/api/auth/login/", {"password": "x"}, format="json", HTTP_X_CSRFTOKEN=self.token)
            second = self.client.post("/api/auth/login/", {"username": 5, "password": "x"}, format="json", HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual((first.status_code, second.status_code), (400, 400))

    def test_csrf_failures_do_not_consume_the_username_limit(self):
        with self.rates(login_user="1/min"), mock.patch.object(views_auth, "authenticate", return_value=None):
            for _ in range(3):
                self.client.post("/api/auth/login/", {"username": "olive", "password": "x"}, format="json")  # no CSRF header
            self.assertEqual(self.attempt().status_code, 403)

    def test_the_proxy_setting_defaults_to_ignoring_forwarded_headers(self):
        from rest_framework.settings import api_settings

        self.assertEqual(api_settings.NUM_PROXIES, 0)

    def test_the_default_rates_are_present_and_well_formed(self):
        from django.conf import settings
        from rest_framework.throttling import SimpleRateThrottle

        for scope in ("login_ip", "login_user", "login_user_hour", "api_user"):
            with self.subTest(scope=scope):
                num, period = SimpleRateThrottle.parse_rate(SimpleRateThrottle, settings.API_THROTTLE_RATES[scope])
                self.assertGreaterEqual(num, 1)
                self.assertIn(period, (1, 60, 3600, 86400))


# --- the request endpoints delegate to ONE service each ------------------------------------------------------


class DelegationTests(ApiTestCase):
    factory = APIRequestFactory()

    def setUp(self):
        super().setUp()
        self.user = fake_user()

    def call(self, view, method, data=None, query="", **path):
        make = getattr(self.factory, method)
        request = make(f"/x/{query}", data, format="json") if method != "get" else make(f"/x/{query}")
        force_authenticate(request, user=self.user)
        response = view.as_view()(request, **path)
        return response, body(response)

    # --- submit ---

    def submission(self, created=True):
        return SimpleNamespace(run=SimpleNamespace(pk=RUN, state="queued"), case=SimpleNamespace(pk=CASE, reference="REQ-20260102-ABCDEF12"), created=created)

    def test_submit_passes_the_text_exactly_and_returns_ids_not_text(self):
        text = "  Requester: Jane Doe\r\nItem: 2 | | Pens \n"
        client_id = uuid.uuid4()
        with mock.patch.object(views_requests, "submit_request_as", return_value=self.submission()) as service:
            response, data = self.call(views_requests.RequestCollectionView, "post", {"text": text, "client_request_id": str(client_id)})
        self.assertEqual(response.status_code, 201)
        service.assert_called_once_with(user=self.user, text=text, client_request_id=client_id)
        self.assertEqual(data, {"run_id": str(RUN), "case_id": str(CASE), "reference": "REQ-20260102-ABCDEF12", "state": "queued", "created": True})
        self.assertNotIn("Jane", json.dumps(data))

    def test_a_replayed_submit_is_a_200_with_created_false(self):
        with mock.patch.object(views_requests, "submit_request_as", return_value=self.submission(created=False)) as service:
            response, data = self.call(views_requests.RequestCollectionView, "post", {"text": "x"})
        self.assertEqual((response.status_code, data["created"]), (200, False))
        self.assertIsNone(service.call_args.kwargs["client_request_id"])

    def test_submit_validation_failures_never_reach_the_service_and_never_echo_the_text(self):
        secret = "SECRET-REQUEST-TEXT"
        with mock.patch.object(views_requests, "submit_request_as") as service:
            for payload in ({}, {"text": 5}, {"text": secret, "extra": 1}, {"text": secret, "client_request_id": "x"}):
                with self.subTest(payload=list(payload)):
                    response, data = self.call(views_requests.RequestCollectionView, "post", payload)
                    self.assertEqual((response.status_code, data["error"]["code"]), (400, "validation_error"))
                    self.assertNotIn(secret, json.dumps(data))
        service.assert_not_called()

    def test_submit_service_errors(self):
        cases = [
            (RequestTextError("blank"), 400, "invalid_request_text"),
            (RequestTextError("too_large"), 400, "invalid_request_text"),
            (ClientRequestConflictError("x"), 409, "idempotency_conflict"),
            (AuthorizationError("missing_permission"), 403, "permission_denied"),
        ]
        for exc, status, code in cases:
            with self.subTest(code=code), mock.patch.object(views_requests, "submit_request_as", side_effect=exc):
                response, data = self.call(views_requests.RequestCollectionView, "post", {"text": "SECRET-TEXT"})
                self.assertEqual((response.status_code, data["error"]["code"]), (status, code))
                self.assertNotIn("SECRET", json.dumps(data))

    # --- list ---

    def test_list_passes_the_paging_and_filter_to_the_service_and_serializes_the_page(self):
        item = queries.RequestListItem(RUN, CASE, "REQ-1", "needs_review", "", NOW, 1, "valid", PROFILE.public_id)
        page = queries.RequestPage((item,), 41, 3, 10)
        with mock.patch.object(queries, "list_requests", return_value=page) as service:
            response, data = self.call(views_requests.RequestCollectionView, "get", query="?page=3&page_size=10&state=needs_review")
        service.assert_called_once_with(user=self.user, page=3, page_size=10, state="needs_review")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["count"], data["page"], data["page_size"]), (41, 3, 10))
        self.assertEqual(data["results"][0], {"run_id": str(RUN), "case_id": str(CASE), "reference": "REQ-1", "state": "needs_review", "failed_step": "", "created_at": "2026-01-02T03:04:05Z", "current_revision_number": 1, "outcome_status": "valid", "submitted_by": str(PROFILE.public_id)})

    def test_list_defaults_and_rejects_bad_queries(self):
        with mock.patch.object(queries, "list_requests", return_value=queries.RequestPage((), 0, 1, 20)) as service:
            self.call(views_requests.RequestCollectionView, "get")
            service.assert_called_once_with(user=self.user, page=1, page_size=20, state=None)
            service.reset_mock()
            for query in ("?page=0", "?page_size=101", "?page=x", "?state=pending", "?owner=me", "?page=1&extra=1"):
                with self.subTest(query=query):
                    response, data = self.call(views_requests.RequestCollectionView, "get", query=query)
                    self.assertEqual((response.status_code, data["error"]["code"]), (400, "validation_error"))
            service.assert_not_called()

    def test_list_without_a_view_permission_is_a_403(self):
        with mock.patch.object(queries, "list_requests", side_effect=AuthorizationError("missing_permission")):
            response, data = self.call(views_requests.RequestCollectionView, "get")
        self.assertEqual((response.status_code, data["error"]["code"]), (403, "permission_denied"))

    # --- detail ---

    def detail(self, **overrides):
        values = dict(
            run_id=RUN, case_id=CASE, reference="REQ-1", state="needs_review", failed_step="", created_at=NOW, submitted_by=PROFILE.public_id,
            processing={"step": "extract", "task_state": "succeeded", "attempt_count": 1, "failure_code": None},
            outcome_status="valid",
            current_revision={"id": REVISION, "number": 1, "content_hash": HASH, "created_at": NOW},
            draft={"requester": {"name": "Jane Doe", "contact": None}, "items": [{"description": "A4 paper", "quantity": "10", "unit": None, "notes": None}], "notes": None, "source_refs": []},
            draft_source="revision",
            findings=(RevisionFinding("value_not_in_source", "notes", False, "Value was not found in the source text.", None),),
            warnings=(("value_not_in_source", "notes"),), approval_count=0, rejection_count=0,
            allowed_actions=("edit", "reject"), action_blockers={"approve": "self_approval_forbidden"},
        )  # fmt: skip
        values.update(overrides)
        return queries.RequestDetail(**values)

    def test_detail_returns_the_contract_fields_and_only_those(self):
        with mock.patch.object(queries, "get_request_detail", return_value=self.detail()) as service:
            response, data = self.call(views_requests.RequestDetailView, "get", run_id=RUN)
        service.assert_called_once_with(user=self.user, run_id=RUN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(data),
            {"run_id", "case_id", "reference", "state", "failed_step", "created_at", "submitted_by", "processing", "outcome_status", "current_revision", "draft", "draft_source", "findings", "warnings", "approval_count", "rejection_count", "allowed_actions", "action_blockers"},
        )
        self.assertEqual(data["current_revision"], {"id": str(REVISION), "number": 1, "content_hash": HASH, "created_at": "2026-01-02T03:04:05Z"})
        self.assertEqual(data["warnings"], [{"code": "value_not_in_source", "field_path": "notes"}])
        self.assertEqual(data["findings"][0]["excerpt"], None)
        self.assertEqual(data["processing"]["failure_code"], None)
        self.assertEqual((data["allowed_actions"], data["action_blockers"]), (["edit", "reject"], {"approve": "self_approval_forbidden"}))
        self.assertEqual(data["draft"]["items"][0]["quantity"], "10")
        for leaked in ("canonical", "payload", "lease", "traceback"):
            self.assertNotIn(leaked, json.dumps(data))

    def test_detail_of_a_run_without_a_revision(self):
        detail = self.detail(current_revision=None, draft_source="extraction_outcome", outcome_status="invalid", processing=None, warnings=(), findings=(), allowed_actions=("edit",), action_blockers={})
        with mock.patch.object(queries, "get_request_detail", return_value=detail):
            _, data = self.call(views_requests.RequestDetailView, "get", run_id=RUN)
        self.assertEqual((data["current_revision"], data["processing"], data["draft_source"]), (None, None, "extraction_outcome"))

    def test_a_request_the_user_may_not_see_is_the_same_404_as_an_unknown_one(self):
        results = []
        for exc in (AuthorizationError("out_of_scope"), RecordNotFoundError("The run does not exist.")):
            with mock.patch.object(queries, "get_request_detail", side_effect=exc):
                response, data = self.call(views_requests.RequestDetailView, "get", run_id=RUN)
            results.append((response.status_code, data))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 404)

    # --- edit ---

    def test_edit_passes_the_base_and_the_domain_draft_to_the_service(self):
        payload = {"base_revision_id": str(REVISION), "draft": {"requester": {"name": "Jane Doe"}, "items": [{"description": "Pens", "quantity": "5"}]}}
        result = EditResult(uuid.uuid4(), 2, HASH, "needs_review", (("value_not_in_source", "notes"),), False)
        with mock.patch("apps.intake.review.edit_request", return_value=result) as service:
            response, data = self.call(views_requests.RequestEditView, "post", payload, run_id=RUN)
        self.assertEqual(response.status_code, 200)
        kwargs = service.call_args.kwargs
        self.assertEqual(set(kwargs), {"user", "run_id", "base_revision_id", "draft"})
        self.assertIs(kwargs["user"], self.user)
        self.assertEqual((kwargs["run_id"], kwargs["base_revision_id"]), (RUN, REVISION))
        self.assertIsInstance(kwargs["draft"], StructuredRequest)
        self.assertEqual(kwargs["draft"].requester, Requester(name="Jane Doe", contact=None))
        self.assertEqual(kwargs["draft"].items[0].quantity.__class__.__name__, "Decimal")
        self.assertEqual(data, {"revision_id": str(result.revision_id), "revision_number": 2, "content_hash": HASH, "run_state": "needs_review", "warnings": [{"code": "value_not_in_source", "field_path": "notes"}], "approval_invalidated": False})

    def test_the_first_edit_has_a_null_base(self):
        with mock.patch("apps.intake.review.edit_request", return_value=EditResult(uuid.uuid4(), 1, HASH, "needs_review", (), False)) as service:
            self.call(views_requests.RequestEditView, "post", {"base_revision_id": None, "draft": {}}, run_id=RUN)
        self.assertIsNone(service.call_args.kwargs["base_revision_id"])

    def test_edit_validation_failures_never_reach_the_service(self):
        bad = [
            {"draft": {}},  # base missing
            {"base_revision_id": None},  # draft missing
            {"base_revision_id": None, "draft": {"items": [{"quantity": 1.5}]}},
            {"base_revision_id": None, "draft": {"items": [{"quantity": "1e-999999999"}]}},
            {"base_revision_id": None, "draft": {}, "approver_id": str(uuid.uuid4())},
            {"base_revision_id": "x", "draft": {}},
        ]
        with mock.patch("apps.intake.review.edit_request") as service:
            for payload in bad:
                with self.subTest(payload=json.dumps(payload)[:50]):
                    response, data = self.call(views_requests.RequestEditView, "post", payload, run_id=RUN)
                    self.assertEqual((response.status_code, data["error"]["code"]), (400, "validation_error"))
        service.assert_not_called()

    def test_edit_service_errors(self):
        finding = RevisionFinding("missing_required", "requester.name", True, "Requester name is required.", "SECRET-EXCERPT")
        cases = [
            (StaleRevisionError("stale_base_revision"), 409, "stale_base_revision"),
            (DraftInvalidError("blocking_findings", findings=(finding,)), 400, "draft_invalid"),
            (DraftInvalidError("not_serializable", path="items[0].quantity"), 400, "draft_not_serializable"),
            (AuthorizationError("missing_permission"), 403, "permission_denied"),
            (AuthorizationError("out_of_scope"), 404, "not_found"),
            (RecordNotFoundError("x"), 404, "not_found"),
        ]
        for exc, status, code in cases:
            with self.subTest(code=code), mock.patch("apps.intake.review.edit_request", side_effect=exc):
                response, data = self.call(views_requests.RequestEditView, "post", {"base_revision_id": None, "draft": {}}, run_id=RUN)
                self.assertEqual((response.status_code, data["error"]["code"]), (status, code))
                self.assertNotIn("SECRET", json.dumps(data))

    # --- approve ---

    def test_approve_passes_the_actor_and_exact_arguments_and_nothing_from_the_body_as_identity(self):
        payload = {"revision_id": str(REVISION), "content_hash": HASH, "acknowledged_warnings": [{"code": "value_not_in_source", "field_path": "notes"}]}
        result = ApprovalResult(uuid.uuid4(), REVISION, HASH, "approved", (("value_not_in_source", "notes"),))
        with mock.patch("apps.intake.review.approve_request", return_value=result) as service:
            response, data = self.call(views_requests.RequestApproveView, "post", payload, run_id=RUN)
        self.assertEqual(response.status_code, 200)
        kwargs = service.call_args.kwargs
        self.assertEqual(set(kwargs), {"user", "run_id", "revision_id", "content_hash", "acknowledged_warnings"})
        self.assertIs(kwargs["user"], self.user)
        self.assertEqual((kwargs["run_id"], kwargs["revision_id"], kwargs["content_hash"]), (RUN, REVISION, HASH))
        self.assertEqual(kwargs["acknowledged_warnings"], (("value_not_in_source", "notes"),))
        self.assertEqual(data["run_state"], "approved")
        self.assertEqual(data["acknowledged_warnings"], [{"code": "value_not_in_source", "field_path": "notes"}])

    def test_a_client_supplied_approver_is_refused_and_never_reaches_the_service(self):
        with mock.patch("apps.intake.review.approve_request") as service:
            for name in ("approver_id", "approver", "public_id", "user", "user_id"):
                with self.subTest(field=name):
                    response, data = self.call(views_requests.RequestApproveView, "post", {"revision_id": str(REVISION), "content_hash": HASH, name: str(uuid.uuid4())}, run_id=RUN)
                    self.assertEqual((response.status_code, data["error"]["details"]["fields"]), (400, {name: ["unknown_field"]}))
        service.assert_not_called()

    def test_approve_service_errors(self):
        cases = [
            (SelfApprovalError("self_approval_forbidden"), 403, "self_approval_forbidden"),
            (StaleRevisionError("stale_revision"), 409, "stale_revision"),
            (AuthorizationError("missing_permission"), 403, "permission_denied"),
            (AuthorizationError("out_of_scope"), 404, "not_found"),
        ]
        for exc, status, code in cases:
            with self.subTest(code=code), mock.patch("apps.intake.review.approve_request", side_effect=exc):
                response, data = self.call(views_requests.RequestApproveView, "post", {"revision_id": str(REVISION), "content_hash": HASH}, run_id=RUN)
                self.assertEqual((response.status_code, data["error"]["code"]), (status, code))

    def test_missing_warning_acknowledgement_lists_the_pairs(self):
        exc = WarningsNotAcknowledgedError("warnings_not_acknowledged", missing=(("duplicate_item", "items[1]"),))
        with mock.patch("apps.intake.review.approve_request", side_effect=exc):
            response, data = self.call(views_requests.RequestApproveView, "post", {"revision_id": str(REVISION), "content_hash": HASH}, run_id=RUN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(data["error"]["details"]["missing"], [{"code": "duplicate_item", "field_path": "items[1]"}])

    # --- reject ---

    def test_reject_passes_only_the_defined_reason_code(self):
        result = RejectionResult(uuid.uuid4(), REVISION, "other", "needs_review")
        with mock.patch("apps.intake.review.reject_request", return_value=result) as service:
            response, data = self.call(views_requests.RequestRejectView, "post", {"revision_id": str(REVISION), "content_hash": HASH, "reason_code": "other"}, run_id=RUN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.call_args.kwargs["reason_code"], "other")
        self.assertIs(service.call_args.kwargs["user"], self.user)
        self.assertEqual(data, {"rejection_id": str(result.rejection_id), "revision_id": str(REVISION), "reason_code": "other", "run_state": "needs_review"})

    def test_free_text_reasons_are_refused_without_being_echoed(self):
        secret = "please fix jane@example.invalid"
        with mock.patch("apps.intake.review.reject_request") as service:
            response, data = self.call(views_requests.RequestRejectView, "post", {"revision_id": str(REVISION), "content_hash": HASH, "reason_code": secret}, run_id=RUN)
        self.assertEqual((response.status_code, data["error"]["details"]["fields"]), (400, {"reason_code": ["invalid_choice"]}))
        self.assertNotIn("jane", json.dumps(data))
        service.assert_not_called()

    # --- audit ---

    def test_audit_returns_a_bounded_page_of_events(self):
        event = SimpleNamespace(id=5, occurred_at=NOW, actor_kind="user", actor_public_id=PROFILE.public_id, action="approval.recorded", subject_type="approval", subject_id=uuid.uuid4(), details={"revision_id": str(REVISION), "warning_count": 0})
        with mock.patch.object(queries, "list_run_audit", return_value=queries.AuditPage((event,), 12, 2, 5)) as service:
            response, data = self.call(views_requests.RequestAuditView, "get", query="?page=2&page_size=5", run_id=RUN)
        service.assert_called_once_with(user=self.user, run_id=RUN, page=2, page_size=5)
        self.assertEqual((response.status_code, data["count"], data["page"], data["page_size"]), (200, 12, 2, 5))
        self.assertEqual(set(data["results"][0]), {"id", "occurred_at", "actor_kind", "actor_public_id", "action", "subject_type", "subject_id", "details"})

    def test_audit_without_the_permission_is_a_403_and_a_bad_page_is_a_400(self):
        with mock.patch.object(queries, "list_run_audit", side_effect=AuthorizationError("missing_permission")):
            response, data = self.call(views_requests.RequestAuditView, "get", run_id=RUN)
        self.assertEqual((response.status_code, data["error"]["code"]), (403, "permission_denied"))
        with mock.patch.object(queries, "list_run_audit") as service:
            response, _ = self.call(views_requests.RequestAuditView, "get", query="?page_size=1000", run_id=RUN)
        self.assertEqual(response.status_code, 400)
        service.assert_not_called()

    # --- cross-cutting ---

    def test_an_unexpected_service_failure_is_a_bare_500_that_leaks_nothing(self):
        with mock.patch.object(queries, "get_request_detail", side_effect=RuntimeError("SECRET db password=hunter2 text=Jane Doe")), self.assertLogs("apps.api.errors", level="ERROR") as logs:
            response, data = self.call(views_requests.RequestDetailView, "get", run_id=RUN)
        self.assertEqual((response.status_code, data["error"]["code"]), (500, "internal_error"))
        for needle in ("SECRET", "hunter2", "Jane", "Traceback", "RuntimeError"):
            self.assertNotIn(needle, json.dumps(data))
        self.assertNotIn("hunter2", "\n".join(logs.output))

    def test_every_response_is_no_store(self):
        with mock.patch.object(queries, "list_requests", return_value=queries.RequestPage((), 0, 1, 20)):
            response, _ = self.call(views_requests.RequestCollectionView, "get")
        self.assertIn("no-store", response["Cache-Control"])
        response, _ = self.call(views_requests.RequestEditView, "post", {}, run_id=RUN)  # a 400
        self.assertIn("no-store", response["Cache-Control"])

    def test_wrong_methods_are_a_405(self):
        for view, method, path in (
            (views_requests.RequestDetailView, "post", {"run_id": RUN}),
            (views_requests.RequestEditView, "get", {"run_id": RUN}),
            (views_requests.RequestApproveView, "get", {"run_id": RUN}),
            (views_requests.RequestRejectView, "delete", {"run_id": RUN}),
            (views_requests.RequestAuditView, "post", {"run_id": RUN}),
        ):
            with self.subTest(view=view.__name__, method=method):
                response, data = self.call(view, method, {} if method != "get" else None, **path)
                self.assertEqual((response.status_code, data["error"]["code"]), (405, "method_not_allowed"))

    def test_the_run_id_must_be_a_uuid_in_the_path(self):
        client = APIClient()
        for path in ("/api/requests/not-a-uuid/", "/api/requests/123/", f"/api/requests/{RUN}x/", "/api/requests/00000000-0000-0000-0000/edit/"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)
