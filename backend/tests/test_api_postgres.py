"""Tests for the REST API that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later destroys a
separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to
one. Nothing in them is evidence yet; the project owner runs them locally (see docs/development/rest-api-slice.md).
When they do pass, that covers the tested scenarios only: it does not prove that no other race exists. This file is
ASCII only. No SQLite substitute was used.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_api_postgres --tag requires_postgres -v 2

They drive the API exactly as a browser would: ``APIClient(enforce_csrf_checks=True)`` keeps the cookie jar, the
session and CSRF cookies are real, and every unsafe request sends the CSRF token in ``X-CSRFToken``. What needs a
real server and is therefore here and not in the DB-free tests: the session table, password verification against
stored hashes, the services against real rows, row locking, and competing connections.
"""

import io
import json
import logging
import os
import secrets
import time
import unittest
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings, tag
from rest_framework.test import APIClient

from apps.accounts.roles import APPROVER, AUDITOR, OPERATOR, ALL_PERMISSIONS
from apps.audit.models import AuditEvent
from apps.cases import services as case_services
from apps.cases.models import Approval, Rejection, ResultRevision, WorkflowRun
from apps.intake import services
from apps.intake.models import ExtractionOutcome, RequestSource, RevisionEdit
from apps.intake.policy import KIND_EXTRACT
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.tasks import worker
from apps.tasks.policy import Failure, FailureCode
from apps.tasks.services import claim_next_task, fail_task
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import INVALID_TEXT, DedicatedTestDatabaseMixin, run_threads

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

User = get_user_model()
# Hashing a password with the configured PBKDF2 hasher takes a noticeable fraction of a second; these tests create
# several users per test. They use a fast hasher for SPEED ONLY; AuthLifecycleTests.test_the_configured_hasher_is_used
# proves a real login works with the project's default hasher.
FAST_HASHERS = {"PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"]}
PASSWORD = secrets.token_urlsafe(18)  # synthetic, generated per run, never stored or printed
RATES = {"login_ip": "1000/min", "login_user": "1000/min", "login_user_hour": "1000/hour", "api_user": "10000/min"}
HASH_ZERO = "0" * 64
SECRET_TEXT = "Requester: Jane SECRETNAME\nContact: jane.secret@example.invalid\nItem: 10 | boxes | A4 paper SECRETITEM\nItem: 2 | | Stapler\n"


def make_user(username, *roles, superuser=False, active=True):
    user = User.objects.create_user(username=username, password=PASSWORD)
    if superuser or not active:
        User.objects.filter(pk=user.pk).update(is_superuser=superuser, is_staff=superuser, is_active=active)
        user.refresh_from_db()
    for role in roles:
        user.groups.add(Group.objects.get(name=role))
    return user


class Browser:
    """A browser stand-in: a cookie jar and the CSRF token, talking to the real URL, middleware and session stack."""

    def __init__(self):
        self.client = APIClient(enforce_csrf_checks=True)
        self.token = None

    def bootstrap(self):
        response = self.client.get("/api/auth/csrf/")
        assert response.status_code == 200, response.content
        self.token = response.json()["csrf_token"]
        return response

    def login(self, username, password=PASSWORD):
        if self.token is None:
            self.bootstrap()
        response = self.client.post("/api/auth/login/", {"username": username, "password": password}, format="json", HTTP_X_CSRFTOKEN=self.token)
        if response.status_code == 200:
            self.token = response.json()["csrf_token"]
        return response

    def get(self, path, **query):
        return self.client.get(path, query)

    def post(self, path, data=None, *, csrf=True, **extra):
        headers = {"HTTP_X_CSRFTOKEN": self.token} if csrf is True and self.token else ({"HTTP_X_CSRFTOKEN": csrf} if isinstance(csrf, str) else {})
        return self.client.post(path, {} if data is None else data, format="json", **headers, **extra)


def err(response):
    return response.json()["error"]["code"]


class ApiCase(DedicatedTestDatabaseMixin, TestCase):
    """Roles set up, one user per kind, throttles raised out of the way, and helpers that drive the API."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        rates = override_settings(API_THROTTLE_RATES=RATES, **FAST_HASHERS)
        rates.enable()
        self.addCleanup(rates.disable)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = make_user("olive", OPERATOR)
        self.other_operator = make_user("omar", OPERATOR)
        self.approver = make_user("anna", APPROVER)
        self.second_approver = make_user("amir", APPROVER)
        self.auditor = make_user("audrey", AUDITOR)
        self.superuser = make_user("root", superuser=True)
        self.nobody = make_user("nina")
        self.hybrid = make_user("hugo", OPERATOR, APPROVER)
        self._browsers = {}

    # --- drivers ---

    def as_(self, user):
        """A logged-in browser for ``user`` (one per user per test)."""
        if user.username not in self._browsers:
            browser = Browser()
            response = browser.login(user.username)
            assert response.status_code == 200, (user.username, response.content)
            self._browsers[user.username] = browser
        return self._browsers[user.username]

    def submit(self, user, text=VALID_TEXT, **extra):
        response = self.as_(user).post("/api/requests/", {"text": text, **extra})
        assert response.status_code in (200, 201), response.content
        return uuid.UUID(response.json()["run_id"])

    def extracted(self, user=None, text=VALID_TEXT):
        """Submit through the API, then run the extraction the worker would run."""
        run_id = self.submit(user or self.operator, text)
        services.run_extraction(run_id)
        return run_id

    def detail(self, user, run_id):
        return self.as_(user).get(f"/api/requests/{run_id}/")

    def current(self, run_id):
        return self.detail(self.superuser, run_id).json()

    def edit_body(self, run_id, marker="1", base="current"):
        current = self.current(run_id)
        draft = dict(current["draft"])
        draft["notes"] = f"Edit {marker}"
        base_id = current["current_revision"]["id"] if base == "current" and current["current_revision"] else (None if base == "current" else base)
        return {"base_revision_id": base_id, "draft": draft}

    def approve_body(self, run_id, **overrides):
        current = self.current(run_id)
        body = {
            "revision_id": current["current_revision"]["id"],
            "content_hash": current["current_revision"]["content_hash"],
            "acknowledged_warnings": [{"code": c, "field_path": p} for c, p in ((w["code"], w["field_path"]) for w in current["warnings"])],
        }
        body.update(overrides)
        return body

    def reject_body(self, run_id, reason="incorrect_content", **overrides):
        current = self.current(run_id)
        body = {"revision_id": current["current_revision"]["id"], "content_hash": current["current_revision"]["content_hash"], "reason_code": reason}
        body.update(overrides)
        return body

    def snapshot(self):
        return {
            "revisions": ResultRevision.objects.count(),
            "edits": RevisionEdit.objects.count(),
            "approvals": Approval.objects.count(),
            "rejections": Rejection.objects.count(),
            "audit": AuditEvent.objects.count(),
            "runs": WorkflowRun.objects.count(),
            "sources": RequestSource.objects.count(),
        }


# --- authentication, sessions and CSRF -----------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AuthLifecycleTests(ApiCase):
    def test_csrf_login_me_logout_and_the_session_really_ends(self):
        browser = Browser()
        self.assertEqual(browser.get("/api/auth/me/").status_code, 403)  # not signed in
        browser.bootstrap()
        login = browser.login("olive")
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["username"], "olive")
        me = browser.get("/api/auth/me/")
        self.assertEqual((me.status_code, me.json()["public_id"]), (200, str(self.operator.public_id)))

        old_cookie = browser.client.cookies["sessionid"].value
        sessions_before = Session.objects.count()
        logout = browser.post("/api/auth/logout/")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(Session.objects.count(), sessions_before - 1)  # flushed server-side
        self.assertEqual(browser.get("/api/auth/me/").status_code, 403)

        replay = APIClient(enforce_csrf_checks=True)  # an attacker replaying the old session cookie
        replay.cookies["sessionid"] = old_cookie
        self.assertEqual(replay.get("/api/auth/me/").status_code, 403)

    def test_the_session_and_csrf_cookies_carry_the_security_attributes(self):
        browser = Browser()
        browser.bootstrap()
        csrf_cookie = browser.client.cookies["csrftoken"]
        self.assertTrue(csrf_cookie["httponly"])
        self.assertEqual(csrf_cookie["samesite"], "Lax")
        browser.login("olive")
        session_cookie = browser.client.cookies["sessionid"]
        self.assertTrue(session_cookie["httponly"])
        self.assertEqual(session_cookie["samesite"], "Lax")
        self.assertTrue(session_cookie["max-age"] or session_cookie["expires"])

    def test_login_replaces_a_pre_existing_anonymous_session_key_against_fixation(self):
        # Django's login() calls cycle_key() when the session has no user yet (the fixation case: an attacker who
        # planted a session id in the victim's browser must not keep it after the victim signs in).
        from django.contrib.sessions.backends.db import SessionStore

        planted = SessionStore()
        planted["probe"] = 1
        planted.save()
        planted_key = planted.session_key
        browser = Browser()
        browser.bootstrap()
        browser.client.cookies["sessionid"] = planted_key
        self.assertEqual(browser.login("olive").status_code, 200)
        new_key = browser.client.cookies["sessionid"].value
        self.assertNotEqual(new_key, planted_key)
        self.assertFalse(Session.objects.filter(session_key=planted_key).exists())  # the planted id is dead
        self.assertEqual(browser.get("/api/auth/me/").status_code, 200)
        replay = APIClient(enforce_csrf_checks=True)
        replay.cookies["sessionid"] = planted_key
        self.assertEqual(replay.get("/api/auth/me/").status_code, 403)

    def test_login_rotates_the_csrf_secret(self):
        browser = Browser()
        browser.bootstrap()
        before_cookie, before_token = browser.client.cookies["csrftoken"].value, browser.token
        response = browser.login("olive")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(browser.client.cookies["csrftoken"].value, before_cookie)  # the secret itself changed
        self.assertNotEqual(response.json()["csrf_token"], before_token)
        stale = browser.client.post("/api/requests/", {"text": VALID_TEXT}, format="json", HTTP_X_CSRFTOKEN=before_token)
        self.assertEqual((stale.status_code, err(stale)), (403, "csrf_failed"))  # a pre-login token no longer works

    def test_logging_in_as_another_user_in_the_same_browser_discards_the_previous_session(self):
        # Django flush()es a session that belongs to a different user. (Logging in again as the SAME user keeps the
        # session key, by Django's design: there is no other identity to protect.)
        browser = Browser()
        self.assertEqual(browser.login("olive").status_code, 200)
        first_key = browser.client.cookies["sessionid"].value
        self.assertEqual(browser.login("omar").status_code, 200)
        second_key = browser.client.cookies["sessionid"].value
        self.assertNotEqual(second_key, first_key)
        self.assertFalse(Session.objects.filter(session_key=first_key).exists())
        self.assertEqual(browser.get("/api/auth/me/").json()["username"], "omar")

    def test_a_failed_login_is_one_generic_response_and_creates_no_session(self):
        make_user("dormant", OPERATOR, active=False)
        sessions = Session.objects.count()
        responses = []
        for username, password in (("olive", "wrong-password"), ("nobody-by-that-name", PASSWORD), ("dormant", PASSWORD)):
            browser = Browser()
            response = browser.login(username, password)
            responses.append((response.status_code, response.json()))
            self.assertNotIn("sessionid", browser.client.cookies)
        self.assertEqual(responses[0], (403, responses[0][1]))
        self.assertEqual(len({json.dumps(r[1], sort_keys=True) for r in responses}), 1)
        self.assertEqual(responses[0][1]["error"]["code"], "invalid_credentials")
        self.assertEqual(Session.objects.count(), sessions)

    def test_an_inactive_user_cannot_log_in_even_with_the_right_password_and_every_role(self):
        make_user("dormant-root", superuser=True, active=False)
        for username in ("dormant-root",):
            response = Browser().login(username)
            self.assertEqual((response.status_code, err(response)), (403, "invalid_credentials"))

    def test_a_user_deactivated_after_login_loses_access_at_once(self):
        browser = self.as_(self.operator)
        self.assertEqual(browser.get("/api/auth/me/").status_code, 200)
        User.objects.filter(pk=self.operator.pk).update(is_active=False)
        self.assertEqual(browser.get("/api/auth/me/").status_code, 403)
        self.assertEqual(browser.post("/api/requests/", {"text": VALID_TEXT}).status_code, 403)

    def test_a_password_change_ends_existing_sessions(self):
        browser = self.as_(self.operator)
        user = User.objects.get(pk=self.operator.pk)
        user.set_password(secrets.token_urlsafe(18))
        user.save()
        self.assertEqual(browser.get("/api/auth/me/").status_code, 403)

    def test_a_deleted_user_loses_access(self):
        gone = make_user("gone", OPERATOR)
        browser = self.as_(gone)
        User.objects.filter(pk=gone.pk).delete()
        self.assertEqual(browser.get("/api/auth/me/").status_code, 403)

    def test_me_reports_roles_and_permissions_for_each_kind_of_user(self):
        expected = {
            "olive": (["Operator"], {"intake.submit_request", "intake.view_own_request", "intake.edit_own_request"}),
            "anna": (["Approver"], {"intake.view_any_request", "intake.approve_request", "intake.reject_request"}),
            "audrey": (["Auditor"], {"intake.view_any_request", "audit.view_auditevent"}),
            "nina": ([], set()),
            "hugo": (["Operator", "Approver"], {"intake.submit_request", "intake.view_own_request", "intake.edit_own_request", "intake.view_any_request", "intake.approve_request", "intake.reject_request"}),
            "root": ([], set(ALL_PERMISSIONS)),
        }
        users = {u.username: u for u in (self.operator, self.approver, self.auditor, self.nobody, self.hybrid, self.superuser)}
        for name, (roles, permissions) in expected.items():
            with self.subTest(user=name):
                data = self.as_(users[name]).get("/api/auth/me/").json()
                self.assertEqual((data["roles"], set(data["permissions"])), (roles, permissions))
                self.assertEqual(data["is_superuser"], name == "root")

    def test_a_role_removed_after_login_is_reflected_at_once(self):
        browser = self.as_(self.operator)
        self.operator.groups.clear()
        self.assertEqual(browser.get("/api/auth/me/").json()["permissions"], [])
        self.assertEqual(browser.post("/api/requests/", {"text": VALID_TEXT}).status_code, 403)

    def test_me_never_returns_credentials_or_personal_extras(self):
        data = self.as_(self.operator).get("/api/auth/me/").json()
        self.assertEqual(set(data), {"public_id", "username", "is_superuser", "roles", "permissions"})
        text = json.dumps(data)
        for needle in ("password", "pbkdf2", PASSWORD, "email"):
            self.assertNotIn(needle, text)

    def test_the_configured_hasher_is_used(self):
        from django.conf import global_settings

        with override_settings(PASSWORD_HASHERS=global_settings.PASSWORD_HASHERS):  # the project's own (default) hashers
            make_user("slow-hash", OPERATOR)
            self.assertTrue(User.objects.get(username="slow-hash").password.startswith("pbkdf2_sha256$"))
            self.assertEqual(Browser().login("slow-hash").status_code, 200)

    def test_no_password_reaches_any_log_record(self):
        probe = secrets.token_urlsafe(12)
        browser = Browser()
        browser.bootstrap()
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("probe").debug("keep assertLogs happy")
            browser.login("olive", probe)  # wrong password
            browser.login("olive", PASSWORD)  # right password
        text = "\n".join(logs.output)
        self.assertNotIn(probe, text)
        self.assertNotIn(PASSWORD, text)

    def test_the_login_response_never_contains_the_password_or_its_hash(self):
        response = Browser().login("olive")
        self.assertNotIn(PASSWORD, response.content.decode())
        self.assertNotIn(User.objects.get(pk=self.operator.pk).password, response.content.decode())

    def test_login_without_a_csrf_token_creates_no_session(self):
        browser = Browser()
        sessions = Session.objects.count()
        response = browser.client.post("/api/auth/login/", {"username": "olive", "password": PASSWORD}, format="json")
        self.assertEqual((response.status_code, err(response)), (403, "csrf_failed"))
        self.assertEqual(Session.objects.count(), sessions)


@tag("requires_postgres")
@requires_postgres
class CsrfEnforcementTests(ApiCase):
    """Every unsafe route refuses a signed-in user's request without a valid CSRF token, and changes nothing."""

    def setUp(self):
        super().setUp()
        self.run_id = self.extracted()

    def unsafe_routes(self):
        run = self.run_id
        return [
            ("/api/auth/logout/", {}, self.operator),
            ("/api/requests/", {"text": VALID_TEXT}, self.operator),
            (f"/api/requests/{run}/edit/", self.edit_body(run), self.operator),
            (f"/api/requests/{run}/approve/", self.approve_body(run), self.approver),
            (f"/api/requests/{run}/reject/", self.reject_body(run), self.approver),
        ]

    def test_no_token_wrong_token_and_empty_token_are_all_refused(self):
        before = self.snapshot()
        variants = {"no header": {"csrf": False}, "wrong header": {"csrf": "not-the-token"}, "empty header": {"csrf": ""}}
        for path, body, user in self.unsafe_routes():
            browser = self.as_(user)
            for label, kwargs in variants.items():
                with self.subTest(route=path, variant=label):
                    response = browser.post(path, body, **kwargs)
                    self.assertEqual((response.status_code, err(response)), (403, "csrf_failed"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.as_(self.operator).get("/api/auth/me/").status_code, 200)  # and nobody was logged out

    def test_a_token_from_another_session_is_refused(self):
        stranger = Browser()
        stranger.bootstrap()
        victim = self.as_(self.operator)
        response = victim.client.post("/api/requests/", {"text": VALID_TEXT}, format="json", HTTP_X_CSRFTOKEN=stranger.token)
        self.assertEqual((response.status_code, err(response)), (403, "csrf_failed"))

    def test_safe_methods_need_no_token(self):
        browser = self.as_(self.operator)
        browser.token = None
        self.assertEqual(browser.get("/api/requests/").status_code, 200)
        self.assertEqual(browser.get(f"/api/requests/{self.run_id}/").status_code, 200)

    def test_with_the_token_the_same_requests_succeed(self):
        for path, body, user in self.unsafe_routes()[2:3]:
            response = self.as_(user).post(path, body)
            self.assertEqual(response.status_code, 200, response.content)

    def test_an_unauthenticated_unsafe_request_is_a_403_not_authenticated_not_a_500(self):
        browser = Browser()
        browser.bootstrap()
        for path, body, _ in self.unsafe_routes():
            response = browser.post(path, body)
            self.assertEqual((response.status_code, err(response)), (403, "not_authenticated"))


@tag("requires_postgres")
@requires_postgres
class LoginThrottleDbTests(ApiCase):
    def test_repeated_failures_for_one_account_are_throttled_even_for_the_right_password(self):
        with override_settings(API_THROTTLE_RATES={**RATES, "login_user": "3/min"}):
            cache.clear()
            statuses = [Browser().login("olive", "wrong").status_code for _ in range(3)]
            blocked = Browser().login("olive", PASSWORD)
            other_account = Browser().login("omar", PASSWORD)
        self.assertEqual(statuses, [403, 403, 403])
        self.assertEqual((blocked.status_code, err(blocked)), (429, "throttled"))
        self.assertGreaterEqual(int(blocked["Retry-After"]), 1)
        self.assertEqual(other_account.status_code, 200)

    def test_a_throttled_attempt_does_not_touch_the_database_session_table(self):
        with override_settings(API_THROTTLE_RATES={**RATES, "login_user": "1/min"}):
            cache.clear()
            Browser().login("olive", "wrong")
            sessions = Session.objects.count()
            Browser().login("olive", PASSWORD)
        self.assertEqual(Session.objects.count(), sessions)

    def test_the_ip_limit_applies_across_accounts(self):
        with override_settings(API_THROTTLE_RATES={**RATES, "login_ip": "3/min"}):
            cache.clear()
            statuses = [Browser().login(f"nobody-{i}", "wrong").status_code for i in range(5)]
        self.assertEqual(statuses, [403, 403, 403, 429, 429])


# --- submission, listing, detail -----------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SubmissionApiTests(ApiCase):
    def test_an_operator_submits_and_gets_ids_a_reference_and_a_queued_state(self):
        response = self.as_(self.operator).post("/api/requests/", {"text": VALID_TEXT})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(set(data), {"run_id", "case_id", "reference", "state", "created"})
        self.assertEqual((data["state"], data["created"]), ("queued", True))
        self.assertRegex(data["reference"], r"^REQ-\d{8}-[0-9A-F]{8}$")
        run = WorkflowRun.objects.get(pk=data["run_id"])
        self.assertEqual((str(run.case_id), RequestSource.objects.get(run=run).submitted_by_id), (data["case_id"], self.operator.pk))
        self.assertEqual(RequestSource.objects.get(run=run).text, VALID_TEXT)  # stored exactly

    def test_only_roles_with_the_submit_permission_may_submit(self):
        before = self.snapshot()
        for user in (self.approver, self.auditor, self.nobody):
            with self.subTest(user=user.username):
                response = self.as_(user).post("/api/requests/", {"text": VALID_TEXT})
                self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))
        self.assertEqual(self.snapshot(), before)
        for user in (self.operator, self.superuser, self.hybrid):
            self.assertEqual(self.as_(user).post("/api/requests/", {"text": VALID_TEXT}).status_code, 201)

    def test_the_client_request_id_makes_a_repeat_safe_and_a_different_text_a_conflict(self):
        client_id = str(uuid.uuid4())
        browser = self.as_(self.operator)
        first = browser.post("/api/requests/", {"text": VALID_TEXT, "client_request_id": client_id})
        again = browser.post("/api/requests/", {"text": VALID_TEXT, "client_request_id": client_id})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual((again.json()["created"], again.json()["run_id"]), (False, first.json()["run_id"]))
        self.assertEqual(WorkflowRun.objects.count(), 1)
        conflict = browser.post("/api/requests/", {"text": VALID_TEXT + "Notes: different\n", "client_request_id": client_id})
        self.assertEqual((conflict.status_code, err(conflict)), (409, "idempotency_conflict"))
        self.assertEqual(WorkflowRun.objects.count(), 1)

    def test_the_same_client_request_id_from_another_user_is_a_separate_request(self):
        client_id = str(uuid.uuid4())
        first = self.as_(self.operator).post("/api/requests/", {"text": VALID_TEXT, "client_request_id": client_id})
        second = self.as_(self.other_operator).post("/api/requests/", {"text": VALID_TEXT, "client_request_id": client_id})
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertNotEqual(first.json()["run_id"], second.json()["run_id"])

    def test_the_services_text_rules_decide_and_the_error_carries_a_stable_reason_only(self):
        before = self.snapshot()
        # 40,000 two-byte characters: within the serializer's character guard, over the service's 65,536-BYTE rule.
        cases = {"blank": "   \n ", "contains_nul": "a\x00b", "too_large": "\u00e9" * 40_000}
        for reason, text in cases.items():
            with self.subTest(reason=reason):
                response = self.as_(self.operator).post("/api/requests/", {"text": text})
                self.assertEqual((response.status_code, err(response)), (400, "invalid_request_text"))
                self.assertEqual(response.json()["error"]["details"], {"reason": reason})
        self.assertEqual(self.snapshot(), before)

    def test_a_text_over_the_character_guard_is_a_validation_error_before_the_service(self):
        response = self.as_(self.operator).post("/api/requests/", {"text": "x" * 65_537})
        self.assertEqual((response.status_code, err(response)), (400, "validation_error"))
        self.assertEqual(response.json()["error"]["details"]["fields"], {"text": ["max_length"]})
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_malformed_and_unknown_input_is_a_400_and_stores_nothing(self):
        browser = self.as_(self.operator)
        before = self.snapshot()
        for payload in ({}, {"text": 5}, {"text": VALID_TEXT, "submitted_by": 1}, {"text": VALID_TEXT, "client_request_id": "x"}):
            response = browser.post("/api/requests/", payload)
            self.assertEqual((response.status_code, err(response)), (400, "validation_error"))
        raw = browser.client.post("/api/requests/", data="{not json", content_type="application/json", HTTP_X_CSRFTOKEN=browser.token)
        self.assertEqual((raw.status_code, err(raw)), (400, "malformed_json"))
        self.assertEqual(self.snapshot(), before)

    def test_the_submitted_text_is_never_logged_and_never_echoed(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("probe").debug("keep assertLogs happy")
            ok = self.as_(self.operator).post("/api/requests/", {"text": SECRET_TEXT})
            bad = self.as_(self.operator).post("/api/requests/", {"text": SECRET_TEXT, "extra": SECRET_TEXT})
            denied = self.as_(self.auditor).post("/api/requests/", {"text": SECRET_TEXT})
        for response in (ok, bad, denied):
            self.assertNotIn("SECRET", response.content.decode())
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_submission_creates_the_audit_event_and_the_queued_task_in_one_go(self):
        run_id = self.submit(self.operator)
        self.assertEqual([e.action for e in AuditEvent.objects.filter(run_id=run_id)], ["request.submitted"])
        self.assertEqual(self.current(run_id)["processing"], {"step": "extract", "task_state": "queued", "attempt_count": 0, "failure_code": None})


@tag("requires_postgres")
@requires_postgres
class ListingApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        text = lambda owner, i: f"Requester: Person {owner}{i}\nItem: {i + 1} | | Thing {owner}{i}\n"  # noqa: E731
        self.mine = [self.submit(self.operator, text("m", i)) for i in range(3)]
        self.theirs = [self.submit(self.other_operator, text("t", i)) for i in range(2)]
        self.everything = {str(r) for r in self.mine + self.theirs}

    def ids(self, user, **query):
        response = self.as_(user).get("/api/requests/", **query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_an_operator_sees_only_their_own_requests(self):
        for user, own in ((self.operator, self.mine), (self.other_operator, self.theirs)):
            with self.subTest(user=user.username):
                data = self.ids(user)
                self.assertEqual({r["run_id"] for r in data["results"]}, {str(r) for r in own})
                self.assertEqual({r["submitted_by"] for r in data["results"]}, {str(user.public_id)})
                self.assertEqual(data["count"], len(own))

    def test_approvers_auditors_and_superusers_see_every_request(self):
        for user in (self.approver, self.auditor, self.superuser, self.hybrid):
            with self.subTest(user=user.username):
                self.assertEqual({r["run_id"] for r in self.ids(user)["results"]}, self.everything)

    def test_a_user_without_a_view_permission_gets_a_403_not_an_empty_list(self):
        response = self.as_(self.nobody).get("/api/requests/")
        self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))

    def test_the_list_never_leaks_across_users_even_with_filters_and_paging(self):
        for query in ({"page_size": 100}, {"state": "queued"}, {"page": 1, "page_size": 1}):
            data = self.ids(self.operator, **query)
            self.assertFalse({r["run_id"] for r in data["results"]} & {str(r) for r in self.theirs}, query)
            self.assertLessEqual(data["count"], 3)

    def test_pagination_is_stable_complete_and_disjoint(self):
        seen = []
        for page in (1, 2, 3):
            data = self.ids(self.approver, page=page, page_size=2)
            self.assertEqual((data["count"], data["page"], data["page_size"]), (5, page, 2))
            seen += [r["run_id"] for r in data["results"]]
        self.assertEqual(len(seen), 5)
        self.assertEqual(set(seen), self.everything)
        repeat = []
        for page in (1, 2, 3):
            repeat += [r["run_id"] for r in self.ids(self.approver, page=page, page_size=2)["results"]]
        self.assertEqual(seen, repeat)  # the same order every time

    def test_the_order_is_newest_first(self):
        created = [r["created_at"] for r in self.ids(self.approver)["results"]]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertEqual(self.ids(self.approver)["results"][0]["run_id"], str(self.theirs[-1]))

    def test_a_page_beyond_the_end_is_empty_with_the_true_count(self):
        data = self.ids(self.approver, page=9, page_size=2)
        self.assertEqual((data["results"], data["count"]), ([], 5))

    def test_the_state_filter(self):
        services.run_extraction(self.mine[0])
        self.assertEqual({r["run_id"] for r in self.ids(self.approver, state="needs_review")["results"]}, {str(self.mine[0])})
        self.assertEqual(len(self.ids(self.approver, state="queued")["results"]), 4)
        self.assertEqual(self.ids(self.approver, state="exported")["results"], [])

    def test_bad_queries_are_400s(self):
        for query in ({"page": 0}, {"page_size": 101}, {"page": "x"}, {"state": "pending"}, {"owner": "me"}):
            with self.subTest(query=query):
                response = self.as_(self.approver).get("/api/requests/", **query)
                self.assertEqual((response.status_code, err(response)), (400, "validation_error"))

    def test_each_item_has_exactly_the_contract_fields(self):
        item = self.ids(self.approver)["results"][0]
        self.assertEqual(set(item), {"run_id", "case_id", "reference", "state", "failed_step", "created_at", "current_revision_number", "outcome_status", "submitted_by"})

    def test_the_list_reflects_extraction_progress(self):
        services.run_extraction(self.mine[1])
        item = next(r for r in self.ids(self.operator)["results"] if r["run_id"] == str(self.mine[1]))
        self.assertEqual((item["state"], item["current_revision_number"], item["outcome_status"]), ("needs_review", 1, "valid"))


@tag("requires_postgres")
@requires_postgres
class DetailApiTests(ApiCase):
    def test_the_detail_of_an_extracted_request(self):
        run_id = self.extracted(text=SECRET_TEXT)
        data = self.detail(self.operator, run_id).json()
        self.assertEqual((data["state"], data["outcome_status"], data["draft_source"]), ("needs_review", "valid", "revision"))
        self.assertEqual(data["current_revision"]["number"], 1)
        self.assertRegex(data["current_revision"]["content_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(data["draft"]["requester"], {"name": "Jane SECRETNAME", "contact": "jane.secret@example.invalid"})
        self.assertEqual(data["draft"]["items"][0], {"description": "A4 paper SECRETITEM", "quantity": "10", "unit": "boxes", "notes": None})
        self.assertEqual(data["reference"], WorkflowRun.objects.get(pk=run_id).case.reference)
        self.assertEqual((data["approval_count"], data["rejection_count"]), (0, 0))

    def test_the_detail_never_exposes_canonical_bytes_the_source_text_or_task_internals(self):
        run_id = self.extracted(text=SECRET_TEXT)
        text = self.detail(self.operator, run_id).content.decode()
        for needle in ("canonical", "lease", "payload", "idempotency", "traceback", "Traceback", "password", "worker_id"):
            self.assertNotIn(needle, text)
        self.assertNotIn(RequestSource.objects.get(run_id=run_id).text, text)  # the whole stored text is not returned

    def test_the_draft_matches_the_stored_revision_exactly_after_edits(self):
        run_id = self.extracted()
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "A"))
        data = self.current(run_id)
        self.assertEqual((data["current_revision"]["number"], data["draft"]["notes"]), (2, "Edit A"))
        revision = case_services.get_current_revision(run_id)
        self.assertEqual(data["current_revision"]["content_hash"], revision.content_hash)

    def test_processing_status_of_a_queued_task(self):
        run_id = self.submit(self.operator)
        data = self.current(run_id)
        self.assertEqual((data["state"], data["processing"]["task_state"], data["current_revision"], data["draft"]), ("queued", "queued", None, None))

    def test_a_task_that_failed_for_good_shows_a_safe_failure_code_and_a_failed_run(self):
        run_id = self.submit(self.operator)
        claimed = claim_next_task(worker_id="w", lease_seconds=60)
        fail_task(task_id=claimed.task_id, lease_token=claimed.lease_token, failure=Failure(FailureCode.REJECTED, retryable=False))
        services.reconcile_runs()
        data = self.current(run_id)
        self.assertEqual((data["state"], data["failed_step"]), ("failed", "extract"))
        self.assertEqual(data["processing"], {"step": "extract", "task_state": "failed", "attempt_count": 1, "failure_code": "rejected"})
        self.assertEqual(data["allowed_actions"], [])

    def test_an_invalid_extraction_shows_the_outcome_draft_and_blocking_findings(self):
        run_id = self.extracted(text=INVALID_TEXT)
        data = self.detail(self.operator, run_id).json()
        self.assertEqual((data["outcome_status"], data["current_revision"], data["draft_source"]), ("invalid", None, "extraction_outcome"))
        self.assertTrue(any(f["blocking"] for f in data["findings"]))
        self.assertEqual(data["allowed_actions"], ["edit"])
        self.assertIsNotNone(data["draft"])

    def test_findings_and_warnings_after_an_edit_with_a_warning(self):
        run_id = self.extracted()
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "warn"))
        data = self.current(run_id)
        self.assertIn({"code": "value_not_in_source", "field_path": "notes"}, data["warnings"])
        self.assertTrue(all(not f["blocking"] for f in data["findings"]))

    def test_allowed_actions_follow_the_role_and_the_state(self):
        run_id = self.extracted()
        expected = {
            self.operator: (["edit"], {}),
            self.other_operator: None,
            self.approver: (["approve", "reject"], {}),
            self.auditor: ([], {}),
            self.superuser: (["edit", "approve", "reject"], {}),
        }
        for user, want in expected.items():
            if want is None:  # another operator: the request is hidden
                self.assertEqual(self.detail(user, run_id).status_code, 404)
                continue
            data = self.detail(user, run_id).json()
            with self.subTest(user=user.username):
                self.assertEqual((data["allowed_actions"], data["action_blockers"]), want)

    def test_self_approval_is_a_blocker_not_an_allowed_action(self):
        run_id = self.extracted(self.hybrid)
        data = self.detail(self.hybrid, run_id).json()
        self.assertEqual(data["allowed_actions"], ["edit", "reject"])
        self.assertEqual(data["action_blockers"], {"approve": "self_approval_forbidden"})
        root_run = self.extracted(self.superuser)
        self.assertEqual(self.detail(self.superuser, root_run).json()["action_blockers"], {"approve": "self_approval_forbidden"})

    def test_after_approval_only_edit_remains_and_after_rejection_approve_is_blocked(self):
        approved = self.extracted()
        self.as_(self.approver).post(f"/api/requests/{approved}/approve/", self.approve_body(approved))
        data = self.current(approved)
        self.assertEqual((data["state"], data["approval_count"], data["allowed_actions"]), ("approved", 1, ["edit"]))
        self.assertEqual(self.detail(self.approver, approved).json()["action_blockers"], {"approve": "run_not_in_review", "reject": "run_not_in_review"})
        rejected = self.extracted()
        self.as_(self.approver).post(f"/api/requests/{rejected}/reject/", self.reject_body(rejected))
        self.assertEqual(self.detail(self.approver, rejected).json()["action_blockers"], {"approve": "revision_rejected"})
        self.assertEqual(self.current(rejected)["rejection_count"], 1)

    def test_a_request_you_may_not_see_is_exactly_as_missing_as_one_that_does_not_exist(self):
        theirs = self.extracted(self.other_operator)
        hidden = self.detail(self.operator, theirs)
        unknown = self.detail(self.operator, uuid.uuid4())
        self.assertEqual((hidden.status_code, unknown.status_code), (404, 404))
        self.assertEqual(hidden.json(), unknown.json())
        self.assertEqual(hidden.content, unknown.content)
        self.assertNotIn(str(theirs), hidden.content.decode())

    def test_a_user_without_view_permission_is_a_403(self):
        run_id = self.extracted()
        response = self.detail(self.nobody, run_id)
        self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))


# --- edit, approve, reject ------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class EditApiTests(ApiCase):
    def test_an_operator_edit_creates_the_next_revision_and_keeps_the_previous_one(self):
        run_id = self.extracted()
        first = ResultRevision.objects.get(run_id=run_id)
        first_bytes, first_hash = bytes(first.canonical_bytes), first.content_hash
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "A"))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data["revision_number"], data["run_state"], data["approval_invalidated"]), (2, "needs_review", False))
        self.assertEqual(ResultRevision.objects.filter(run_id=run_id).count(), 2)
        again = ResultRevision.objects.get(pk=first.pk)
        self.assertEqual((bytes(again.canonical_bytes), again.content_hash), (first_bytes, first_hash))
        self.assertEqual(RevisionEdit.objects.get(revision_id=data["revision_id"]).edited_by_id, self.operator.pk)

    def test_a_stale_base_revision_is_a_409_and_stores_nothing(self):
        run_id = self.extracted()
        stale = self.current(run_id)["current_revision"]["id"]
        self.assertEqual(self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "A")).status_code, 200)
        before = self.snapshot()
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "B", base=stale))
        self.assertEqual((response.status_code, err(response)), (409, "stale_base_revision"))
        self.assertEqual(self.snapshot(), before)

    def test_the_first_edit_of_an_invalid_extraction_creates_revision_one(self):
        run_id = self.extracted(text=INVALID_TEXT)
        body = {"base_revision_id": None, "draft": {"requester": {"name": "Jane Doe"}, "items": [{"description": "Pens", "quantity": "5"}]}}
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", body)
        self.assertEqual((response.status_code, response.json()["revision_number"]), (200, 1))
        data = self.current(run_id)
        self.assertEqual((data["current_revision"]["number"], data["draft_source"], data["outcome_status"]), (1, "revision", "invalid"))

    def test_the_base_is_required(self):
        run_id = self.extracted()
        body = self.edit_body(run_id)
        del body["base_revision_id"]
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", body)
        self.assertEqual((response.status_code, response.json()["error"]["details"]["fields"]), (400, {"base_revision_id": ["required"]}))

    def test_an_invalid_draft_is_a_400_with_findings_and_never_becomes_a_revision(self):
        run_id = self.extracted(text=SECRET_TEXT)
        current = self.current(run_id)
        body = {"base_revision_id": current["current_revision"]["id"], "draft": {"requester": {"name": None}, "items": [{"description": "SECRET-DESC", "quantity": "0"}]}}
        before = self.snapshot()
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", body)
        self.assertEqual((response.status_code, err(response)), (400, "draft_invalid"))
        findings = response.json()["error"]["details"]["findings"]
        # The machine-readable contract is the code, the field path and the blocking flag (workflows/request_intake/
        # findings.py). The human-readable message is fixed explanatory text and NOT part of that contract: for a
        # Requester whose name is null the domain says "A value is required." (only a missing Requester object says
        # "Requester name is required."), so the message is checked for being present and fixed, not for its wording.
        missing = [f for f in findings if (f["code"], f["field_path"]) == ("missing_required", "requester.name")]
        self.assertEqual(len(missing), 1)
        self.assertIs(missing[0]["blocking"], True)
        self.assertTrue(isinstance(missing[0]["message"], str) and missing[0]["message"])
        self.assertTrue(all(set(f) == {"code", "field_path", "blocking", "message"} for f in findings))  # no excerpts
        self.assertTrue(all(isinstance(f["message"], str) and f["message"] for f in findings))
        self.assertNotIn("SECRET", response.content.decode())
        self.assertEqual(self.snapshot(), before)

    def test_an_unchanged_draft_is_a_409(self):
        run_id = self.extracted()
        current = self.current(run_id)
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", {"base_revision_id": current["current_revision"]["id"], "draft": current["draft"]})
        self.assertEqual((response.status_code, err(response)), (409, "unchanged_content"))

    def test_source_references_are_verified_by_the_domain(self):
        run_id = self.extracted()
        current = self.current(run_id)
        draft = {**current["draft"], "source_refs": [{"field_path": "requester.name", "excerpt": "a line that is not in the source", "line": None}]}
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", {"base_revision_id": current["current_revision"]["id"], "draft": draft})
        self.assertEqual((response.status_code, err(response)), (400, "draft_invalid"))
        self.assertIn("invalid_source_ref", {f["code"] for f in response.json()["error"]["details"]["findings"]})

    def test_hostile_quantities_are_refused_at_once_and_never_reach_the_domain(self):
        run_id = self.extracted()
        current = self.current(run_id)
        before = self.snapshot()
        for quantity in (1.5, True, "1e-999999999", "1E+999999999", "NaN-ish", [1]):
            started = time.monotonic()
            body = {"base_revision_id": current["current_revision"]["id"], "draft": {"requester": {"name": "Jane"}, "items": [{"description": "x", "quantity": quantity}]}}
            response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", body)
            self.assertEqual((response.status_code, err(response)), (400, "validation_error"), quantity)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.snapshot(), before)

    def test_roles_and_scope(self):
        run_id = self.extracted()
        body = self.edit_body(run_id, "R")
        for user, status, code in (
            (self.approver, 403, "permission_denied"),
            (self.auditor, 403, "permission_denied"),
            (self.nobody, 403, "permission_denied"),
            (self.other_operator, 404, "not_found"),  # another operator's request is hidden, not "forbidden"
        ):
            with self.subTest(user=user.username):
                before = self.snapshot()
                response = self.as_(user).post(f"/api/requests/{run_id}/edit/", body)
                self.assertEqual((response.status_code, err(response)), (status, code))
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.as_(self.superuser).post(f"/api/requests/{run_id}/edit/", body).status_code, 200)

    def test_the_hidden_request_answer_is_identical_to_an_unknown_run(self):
        run_id = self.extracted()
        body = self.edit_body(run_id, "R")
        hidden = self.as_(self.other_operator).post(f"/api/requests/{run_id}/edit/", body)
        unknown = self.as_(self.other_operator).post(f"/api/requests/{uuid.uuid4()}/edit/", body)
        self.assertEqual(hidden.content, unknown.content)

    def test_editing_an_approved_request_returns_it_to_review_and_keeps_the_approval(self):
        run_id = self.extracted()
        self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "after"))
        self.assertEqual((response.status_code, response.json()["approval_invalidated"], response.json()["run_state"]), (200, True, "needs_review"))
        self.assertEqual(Approval.objects.filter(revision__run_id=run_id).count(), 1)
        self.assertEqual(self.current(run_id)["approval_count"], 0)  # the new revision has none

    def test_a_run_in_a_state_that_cannot_be_edited_is_a_409(self):
        run_id = self.submit(self.operator)  # still queued
        response = self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", {"base_revision_id": None, "draft": {}})
        self.assertEqual((response.status_code, err(response)), (409, "run_state_not_editable"))


@tag("requires_postgres")
@requires_postgres
class ApprovalApiTests(ApiCase):
    def test_an_approver_approves_and_the_approver_is_the_signed_in_user(self):
        run_id = self.extracted()
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["run_state"], "approved")
        approval = Approval.objects.get(pk=data["approval_id"])
        self.assertEqual((approval.approver_id, str(approval.revision_id), approval.content_hash), (self.approver.public_id, data["revision_id"], data["content_hash"]))
        self.assertEqual(self.current(run_id)["state"], "approved")

    def test_a_client_supplied_approver_is_refused_and_nothing_is_stored(self):
        run_id = self.extracted()
        before = self.snapshot()
        for name in ("approver_id", "approver", "public_id", "user"):
            body = self.approve_body(run_id, **{name: str(self.second_approver.public_id)})
            response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", body)
            self.assertEqual((response.status_code, response.json()["error"]["details"]["fields"]), (400, {name: ["unknown_field"]}))
        self.assertEqual(self.snapshot(), before)

    def test_the_approver_is_the_logged_in_user_even_when_two_approvers_exist(self):
        run_id = self.extracted()
        self.as_(self.second_approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        self.assertEqual(Approval.objects.get(revision__run_id=run_id).approver_id, self.second_approver.public_id)

    def test_roles_that_may_not_approve_get_a_403_and_change_nothing(self):
        run_id = self.extracted()
        for user in (self.operator, self.auditor, self.nobody):
            with self.subTest(user=user.username):
                before = self.snapshot()
                response = self.as_(user).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
                self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))
                self.assertEqual(self.snapshot(), before)

    def test_self_approval_is_refused_for_the_submitter_and_for_a_superuser_author(self):
        hybrid_run = self.extracted(self.hybrid)
        before = self.snapshot()
        response = self.as_(self.hybrid).post(f"/api/requests/{hybrid_run}/approve/", self.approve_body(hybrid_run))
        self.assertEqual((response.status_code, err(response)), (403, "self_approval_forbidden"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.as_(self.approver).post(f"/api/requests/{hybrid_run}/approve/", self.approve_body(hybrid_run)).status_code, 200)
        root_run = self.extracted(self.superuser)
        response = self.as_(self.superuser).post(f"/api/requests/{root_run}/approve/", self.approve_body(root_run))
        self.assertEqual((response.status_code, err(response)), (403, "self_approval_forbidden"))
        authored = self.extracted()
        self.as_(self.superuser).post(f"/api/requests/{authored}/edit/", self.edit_body(authored, "root"))
        response = self.as_(self.superuser).post(f"/api/requests/{authored}/approve/", self.approve_body(authored))
        self.assertEqual((response.status_code, err(response)), (403, "self_approval_forbidden"))

    def test_a_stale_revision_or_wrong_hash_is_a_409(self):
        run_id = self.extracted()
        stale = self.approve_body(run_id)
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "newer"))
        before = self.snapshot()
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", stale)
        self.assertEqual((response.status_code, err(response)), (409, "stale_revision"))
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id, content_hash=HASH_ZERO))
        self.assertEqual((response.status_code, err(response)), (409, "content_hash_mismatch"))
        self.assertEqual(self.snapshot(), before)

    def test_warnings_must_be_acknowledged_exactly(self):
        run_id = self.extracted()
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "warn"))
        warnings = self.current(run_id)["warnings"]
        self.assertTrue(warnings)
        before = self.snapshot()
        none = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id, acknowledged_warnings=[]))
        self.assertEqual((none.status_code, err(none)), (400, "warnings_not_acknowledged"))
        self.assertEqual({(w["code"], w["field_path"]) for w in none.json()["error"]["details"]["missing"]}, {(w["code"], w["field_path"]) for w in warnings})
        extra = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id, acknowledged_warnings=warnings + [{"code": "duplicate_item", "field_path": "items[9]"}]))
        self.assertEqual((extra.status_code, err(extra)), (400, "unknown_warning_acknowledged"))
        self.assertEqual(self.snapshot(), before)
        done = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        self.assertEqual(done.status_code, 200)
        self.assertEqual({(w["code"], w["field_path"]) for w in done.json()["acknowledged_warnings"]}, {(w["code"], w["field_path"]) for w in warnings})

    def test_a_second_approval_is_a_409(self):
        run_id = self.extracted()
        self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        response = self.as_(self.second_approver).post(f"/api/requests/{run_id}/approve/", {"revision_id": str(uuid.uuid4()), "content_hash": HASH_ZERO})
        self.assertEqual((response.status_code, err(response)), (409, "run_not_in_review"))
        self.assertEqual(Approval.objects.filter(revision__run_id=run_id).count(), 1)

    def test_a_run_without_a_revision_cannot_be_approved(self):
        run_id = self.extracted(text=INVALID_TEXT)
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", {"revision_id": str(uuid.uuid4()), "content_hash": HASH_ZERO})
        self.assertEqual((response.status_code, err(response)), (409, "no_current_revision"))

    def test_historical_revisions_and_approvals_stay_after_edit_and_reapproval(self):
        run_id = self.extracted()
        first = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id)).json()
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "again"))
        second = self.as_(self.second_approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id)).json()
        self.assertEqual(Approval.objects.filter(revision__run_id=run_id).count(), 2)
        self.assertEqual(ResultRevision.objects.filter(run_id=run_id).count(), 2)
        self.assertEqual({str(a.revision_id) for a in Approval.objects.filter(revision__run_id=run_id)}, {first["revision_id"], second["revision_id"]})
        decision = case_services.check_persisted_export_eligibility(
            expected_case_id=WorkflowRun.objects.get(pk=run_id).case_id,
            expected_run_id=run_id,
            requested_revision_id=uuid.UUID(second["revision_id"]),
            approval_id=uuid.UUID(first["approval_id"]),
        )
        self.assertFalse(decision.allowed)  # the first approval does not authorize the second revision


@tag("requires_postgres")
@requires_postgres
class RejectionApiTests(ApiCase):
    def test_an_approver_rejects_with_a_defined_reason_and_the_run_stays_in_review(self):
        run_id = self.extracted()
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id, "incomplete_content"))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data["reason_code"], data["run_state"]), ("incomplete_content", "needs_review"))
        row = Rejection.objects.get(pk=data["rejection_id"])
        self.assertEqual((row.rejected_by, str(row.revision_id)), (self.approver.public_id, data["revision_id"]))
        self.assertEqual(self.current(run_id)["state"], "needs_review")

    def test_free_text_and_unknown_reasons_are_refused_without_being_echoed(self):
        run_id = self.extracted()
        before = self.snapshot()
        for reason in ("please fix jane@example.invalid", "", "OTHER", "Incorrect content", None, 5):
            response = self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id, reason))
            self.assertEqual((response.status_code, err(response)), (400, "validation_error"), reason)
            self.assertNotIn("jane", response.content.decode())
        self.assertEqual(self.snapshot(), before)

    def test_a_client_supplied_identity_or_note_is_refused(self):
        run_id = self.extracted()
        for extra in ({"rejected_by": str(self.approver.public_id)}, {"note": "typed explanation"}, {"reason": "free text"}):
            response = self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id, **extra))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(Rejection.objects.count(), 0)

    def test_roles_that_may_not_reject(self):
        run_id = self.extracted()
        for user in (self.operator, self.auditor, self.nobody):
            response = self.as_(user).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id))
            self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))
        self.assertEqual(Rejection.objects.count(), 0)

    def test_stale_and_state_conflicts(self):
        run_id = self.extracted()
        stale = self.reject_body(run_id)
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "n"))
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", stale)
        self.assertEqual((response.status_code, err(response)), (409, "stale_revision"))
        self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        response = self.as_(self.second_approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id))
        self.assertEqual((response.status_code, err(response)), (409, "run_not_in_review"))

    def test_a_rejected_revision_cannot_be_approved_but_an_edited_one_can(self):
        run_id = self.extracted()
        self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id))
        response = self.as_(self.second_approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        self.assertEqual((response.status_code, err(response)), (409, "revision_rejected"))
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "fixed"))
        self.assertEqual(self.as_(self.second_approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id)).status_code, 200)
        self.assertEqual(Rejection.objects.filter(revision__run_id=run_id).count(), 1)  # kept as history


# --- audit ---------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AuditApiTests(ApiCase):
    def full_flow(self):
        run_id = self.extracted(text=SECRET_TEXT)
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "one"))
        self.as_(self.approver).post(f"/api/requests/{run_id}/reject/", self.reject_body(run_id))
        self.as_(self.operator).post(f"/api/requests/{run_id}/edit/", self.edit_body(run_id, "two"))
        self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        return run_id

    def audit(self, user, run_id, **query):
        return self.as_(user).get(f"/api/requests/{run_id}/audit/", **query)

    def test_an_auditor_reads_the_history_oldest_first(self):
        run_id = self.full_flow()
        data = self.audit(self.auditor, run_id).json()
        self.assertEqual(
            [e["action"] for e in data["results"]],
            ["request.submitted", "revision.created", "run.state_changed", "revision.created", "request.edited", "rejection.recorded", "revision.created", "request.edited", "approval.recorded", "run.state_changed"],
        )
        self.assertEqual(data["count"], 10)
        ids = [e["id"] for e in data["results"]]
        self.assertEqual(ids, sorted(ids))

    def test_only_auditors_and_superusers_may_read_it(self):
        run_id = self.extracted()
        for user in (self.operator, self.approver, self.nobody, self.hybrid):
            with self.subTest(user=user.username):
                response = self.audit(user, run_id)
                self.assertEqual((response.status_code, err(response)), (403, "permission_denied"))
        for user in (self.auditor, self.superuser):
            self.assertEqual(self.audit(user, run_id).status_code, 200)

    def test_pagination_is_bounded_and_complete(self):
        run_id = self.full_flow()
        seen = []
        for page in (1, 2, 3, 4):
            data = self.audit(self.auditor, run_id, page=page, page_size=3).json()
            self.assertLessEqual(len(data["results"]), 3)
            self.assertEqual((data["count"], data["page_size"]), (10, 3))
            seen += [e["id"] for e in data["results"]]
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)
        for bad in ({"page": 0}, {"page_size": 101}, {"page_size": 0}, {"action": "x"}):
            self.assertEqual(self.audit(self.auditor, run_id, **bad).status_code, 400)
        self.assertEqual(self.audit(self.auditor, run_id, page=9, page_size=3).json()["results"], [])

    def test_events_carry_ids_states_counts_and_hashes_never_content(self):
        run_id = self.full_flow()
        text = self.audit(self.auditor, run_id).content.decode()
        for sensitive in ("SECRET", "Jane", "jane.secret", "A4 paper", "Stapler", "olive", "anna", "Edit one", "@"):
            self.assertNotIn(sensitive, text)
        data = self.audit(self.auditor, run_id).json()["results"]
        for event in data:
            self.assertEqual(set(event), {"id", "occurred_at", "actor_kind", "actor_public_id", "action", "subject_type", "subject_id", "details"})
        first = lambda action: next(e for e in data if e["action"] == action)  # noqa: E731
        self.assertEqual(first("approval.recorded")["actor_public_id"], str(self.approver.public_id))
        self.assertEqual(first("request.submitted")["actor_public_id"], str(self.operator.public_id))
        extraction_revision = first("revision.created")  # the first one is the worker's, the later ones are edits
        self.assertEqual((extraction_revision["actor_kind"], extraction_revision["actor_public_id"], extraction_revision["details"]["origin"]), ("system", None, "extraction"))
        edit_revisions = [e for e in data if e["action"] == "revision.created"][1:]
        self.assertEqual({(e["actor_kind"], e["actor_public_id"]) for e in edit_revisions}, {("user", str(self.operator.public_id))})

    def test_an_unknown_run_is_a_404_for_an_auditor(self):
        response = self.audit(self.auditor, uuid.uuid4())
        self.assertEqual((response.status_code, err(response)), (404, "not_found"))

    def test_a_refused_attempt_leaves_no_audit_event_and_a_losing_attempt_leaves_no_trace(self):
        run_id = self.extracted()
        before = AuditEvent.objects.filter(run_id=run_id).count()
        self.as_(self.operator).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))  # 403
        self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id, content_hash=HASH_ZERO))  # 409
        self.assertEqual(AuditEvent.objects.filter(run_id=run_id).count(), before)


# --- cross-user isolation ------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class IsolationApiTests(ApiCase):
    def test_one_operator_can_do_nothing_to_anothers_request_and_learns_nothing(self):
        theirs = self.extracted(self.other_operator, SECRET_TEXT)
        mine = self.extracted(self.operator)
        before = self.snapshot()
        attacker = self.as_(self.operator)
        body = self.edit_body(theirs, "x")
        responses = {
            "detail": attacker.get(f"/api/requests/{theirs}/"),
            "edit": attacker.post(f"/api/requests/{theirs}/edit/", body),
            "audit": attacker.get(f"/api/requests/{theirs}/audit/"),
        }
        self.assertEqual({k: r.status_code for k, r in responses.items()}, {"detail": 404, "edit": 404, "audit": 403})
        for name, response in responses.items():
            self.assertNotIn(str(theirs), response.content.decode(), name)
            self.assertNotIn("SECRET", response.content.decode(), name)
        listed = {r["run_id"] for r in attacker.get("/api/requests/").json()["results"]}
        self.assertEqual(listed, {str(mine)})
        self.assertEqual(self.snapshot(), before)

    def test_sessions_are_isolated_one_browser_cannot_use_anothers_token_or_cookie_jar(self):
        a, b = self.as_(self.operator), self.as_(self.other_operator)
        self.assertEqual(a.get("/api/auth/me/").json()["username"], "olive")
        self.assertEqual(b.get("/api/auth/me/").json()["username"], "omar")
        crossed = a.client.post("/api/requests/", {"text": VALID_TEXT}, format="json", HTTP_X_CSRFTOKEN=b.token)
        self.assertEqual((crossed.status_code, err(crossed)), (403, "csrf_failed"))

    def test_a_logged_out_browser_sees_nothing(self):
        browser = self.as_(self.operator)
        run_id = self.extracted()
        self.assertEqual(browser.post("/api/auth/logout/").status_code, 204)
        for path in (f"/api/requests/{run_id}/", "/api/requests/", f"/api/requests/{run_id}/audit/", "/api/auth/me/"):
            self.assertEqual(browser.get(path).status_code, 403, path)

    def test_every_response_is_no_store_and_never_carries_cors_headers(self):
        run_id = self.extracted()
        browser = self.as_(self.operator)
        for response in (browser.get("/api/requests/"), browser.get(f"/api/requests/{run_id}/"), browser.get("/api/auth/me/"), browser.get(f"/api/requests/{uuid.uuid4()}/")):
            self.assertIn("no-store", response["Cache-Control"])
            self.assertFalse([h for h in response.headers if h.lower().startswith("access-control-")])


# --- API -> worker -> review -> approval, end to end ----------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class EndToEndApiTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """The worker refuses to run a handler inside a transaction, so this cannot be TestCase."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        rates = override_settings(API_THROTTLE_RATES=RATES, **FAST_HASHERS)
        rates.enable()
        self.addCleanup(rates.disable)
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = make_user("olive", OPERATOR)
        self.approver = make_user("anna", APPROVER)
        self.auditor = make_user("audrey", AUDITOR)

    def login(self, user):
        browser = Browser()
        self.assertEqual(browser.login(user.username).status_code, 200)
        return browser

    def process(self):
        return worker.process_one(worker_id="test-worker", lease_seconds=60, registry=PRODUCTION_REGISTRY)

    def test_operator_submits_worker_extracts_approver_approves_auditor_reads_the_history(self):
        operator, approver, auditor = self.login(self.operator), self.login(self.approver), self.login(self.auditor)

        submitted = operator.post("/api/requests/", {"text": VALID_TEXT, "client_request_id": str(uuid.uuid4())})
        self.assertEqual(submitted.status_code, 201)
        run_id = submitted.json()["run_id"]
        queued = operator.get(f"/api/requests/{run_id}/").json()
        self.assertEqual((queued["state"], queued["processing"]["task_state"]), ("queued", "queued"))

        outcome = self.process()
        self.assertEqual((outcome.status, outcome.kind), (worker.ProcessStatus.SUCCEEDED, KIND_EXTRACT))
        reviewed = operator.get(f"/api/requests/{run_id}/").json()
        self.assertEqual((reviewed["state"], reviewed["current_revision"]["number"], reviewed["outcome_status"]), ("needs_review", 1, "valid"))
        self.assertEqual(reviewed["processing"], {"step": "extract", "task_state": "succeeded", "attempt_count": 1, "failure_code": None})
        self.assertEqual(reviewed["allowed_actions"], ["edit"])

        approval_body = {
            "revision_id": reviewed["current_revision"]["id"],
            "content_hash": reviewed["current_revision"]["content_hash"],
            "acknowledged_warnings": [{"code": w["code"], "field_path": w["field_path"]} for w in reviewed["warnings"]],
        }
        refused = operator.post(f"/api/requests/{run_id}/approve/", approval_body)
        self.assertEqual((refused.status_code, err(refused)), (403, "permission_denied"))
        seen_by_approver = approver.get(f"/api/requests/{run_id}/").json()
        self.assertEqual(seen_by_approver["allowed_actions"], ["approve", "reject"])

        approved = approver.post(f"/api/requests/{run_id}/approve/", approval_body)
        self.assertEqual((approved.status_code, approved.json()["run_state"]), (200, "approved"))
        self.assertEqual(Approval.objects.get(revision__run_id=run_id).approver_id, self.approver.public_id)
        self.assertEqual(operator.get(f"/api/requests/{run_id}/").json()["state"], "approved")

        history = auditor.get(f"/api/requests/{run_id}/audit/").json()
        self.assertEqual(
            [e["action"] for e in history["results"]],
            ["request.submitted", "revision.created", "run.state_changed", "approval.recorded", "run.state_changed"],
        )
        self.assertEqual(operator.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(operator.get(f"/api/requests/{run_id}/").status_code, 403)

    def test_an_invalid_request_is_fixed_through_the_api_and_then_approved(self):
        operator, approver = self.login(self.operator), self.login(self.approver)
        run_id = operator.post("/api/requests/", {"text": INVALID_TEXT}).json()["run_id"]
        self.process()
        first = operator.get(f"/api/requests/{run_id}/").json()
        self.assertEqual((first["outcome_status"], first["current_revision"]), ("invalid", None))
        fixed = operator.post(f"/api/requests/{run_id}/edit/", {"base_revision_id": None, "draft": {"requester": {"name": "Jane Doe"}, "items": [{"description": "Pens", "quantity": "5"}]}})
        self.assertEqual((fixed.status_code, fixed.json()["revision_number"]), (200, 1))
        current = operator.get(f"/api/requests/{run_id}/").json()
        body = {"revision_id": current["current_revision"]["id"], "content_hash": current["current_revision"]["content_hash"], "acknowledged_warnings": [{"code": w["code"], "field_path": w["field_path"]} for w in current["warnings"]]}
        self.assertEqual(approver.post(f"/api/requests/{run_id}/approve/", body).status_code, 200)


# --- competing connections -----------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ApiConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        rates = override_settings(API_THROTTLE_RATES=RATES, **FAST_HASHERS)
        rates.enable()
        self.addCleanup(rates.disable)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = make_user("olive", OPERATOR)
        self.approver = make_user("anna", APPROVER)
        self.second_approver = make_user("amir", APPROVER)
        self.superuser = make_user("root", superuser=True)

    def login(self, user):
        browser = Browser()
        assert browser.login(user.username).status_code == 200
        return browser

    def prepared_run(self):
        operator = self.login(self.operator)
        run_id = operator.post("/api/requests/", {"text": VALID_TEXT}).json()["run_id"]
        services.run_extraction(uuid.UUID(run_id))
        current = self.login(self.superuser).get(f"/api/requests/{run_id}/").json()
        return run_id, current

    def approve_call(self, browser, run_id, current):
        body = {
            "revision_id": current["current_revision"]["id"],
            "content_hash": current["current_revision"]["content_hash"],
            "acknowledged_warnings": [{"code": w["code"], "field_path": w["field_path"]} for w in current["warnings"]],
        }
        return lambda: browser.post(f"/api/requests/{run_id}/approve/", body)

    def test_two_approvers_approving_at_once_give_one_approval_and_one_conflict(self):
        run_id, current = self.prepared_run()
        a, b = self.login(self.approver), self.login(self.second_approver)
        results = run_threads(self, [self.approve_call(a, run_id, current), self.approve_call(b, run_id, current)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        statuses = sorted(response.status_code for _, response in results)
        self.assertEqual(statuses, [200, 409])
        loser = next(response for _, response in results if response.status_code == 409)
        self.assertEqual(err(loser), "run_not_in_review")
        self.assertEqual(Approval.objects.filter(revision__run_id=run_id).count(), 1)
        self.assertEqual(WorkflowRun.objects.get(pk=run_id).state, "approved")

    def test_two_edits_of_one_revision_give_one_new_revision_and_one_stale_conflict(self):
        run_id, current = self.prepared_run()
        operators = [self.login(self.operator), self.login(self.operator)]

        def edit(browser, marker):
            draft = dict(current["draft"])
            draft["notes"] = f"Edit {marker}"
            return lambda: browser.post(f"/api/requests/{run_id}/edit/", {"base_revision_id": current["current_revision"]["id"], "draft": draft})

        results = run_threads(self, [edit(operators[0], "a"), edit(operators[1], "b")])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sorted(r.status_code for _, r in results), [200, 409])
        self.assertEqual(ResultRevision.objects.filter(run_id=run_id).count(), 2)

    def test_an_edit_racing_an_approval_never_leaves_an_approval_on_the_current_revision(self):
        for attempt in range(3):
            run_id, current = self.prepared_run()
            approver, operator = self.login(self.approver), self.login(self.operator)
            draft = dict(current["draft"])
            draft["notes"] = f"Race {attempt}"
            edit = lambda: operator.post(f"/api/requests/{run_id}/edit/", {"base_revision_id": current["current_revision"]["id"], "draft": draft})  # noqa: E731
            results = run_threads(self, [self.approve_call(approver, run_id, current), edit])
            self.assertTrue(all(kind == "ok" for kind, _ in results), results)
            approve_response, edit_response = results[0][1], results[1][1]
            self.assertEqual(edit_response.status_code, 200)  # the edit is valid in either order
            row = WorkflowRun.objects.get(pk=run_id)
            self.assertEqual(row.state, "needs_review")
            self.assertEqual(Approval.objects.filter(revision_id=row.current_revision_id).count(), 0)
            self.assertIn(approve_response.status_code, (200, 409))
