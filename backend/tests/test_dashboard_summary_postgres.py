"""Tests for GET /api/dashboard/summary/ that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later destroys a
separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to
one. Nothing in them is evidence yet; the project owner runs them locally (see docs/development/runbook.md).
When they do pass, that covers the tested scenarios only. This file is ASCII only. No SQLite substitute was used.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_dashboard_summary_postgres --tag requires_postgres -v 2

Requests are created through the real API and services. To reach states that need a worker or an external system
(``extracting``, ``exporting``, ``exported``, ``failed``, ``cancelled``), a test moves the row with a queryset
``update``; the database CHECK constraints (known state, ``failed_step`` set exactly when failed) still apply to
it. That shortcut is a fixture, not a claim about how a run gets there.
"""

import io
import os
import threading
import unittest

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext

from apps.accounts.roles import OPERATOR, APPROVER
from apps.cases.models import WorkflowRun
from apps.cases.run_state import STATES
from tests import test_api_postgres as base
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, run_threads

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

URL = "/api/dashboard/summary/"
PROCESSING = ("queued", "extracting", "exporting")
ITEM_FIELDS = {"run_id", "case_id", "reference", "state", "failed_step", "created_at", "current_revision_number", "outcome_status", "submitted_by"}
TOP_FIELDS = {"total", "processing", "needs_review", "approved", "failed", "by_state", "recent_requests"}


def set_state(run_id, state):
    fields = {"state": state, "failed_step": "extract" if state == "failed" else ""}
    assert WorkflowRun.objects.filter(pk=run_id).update(**fields) == 1


class SummaryCase(base.ApiCase):
    def summary(self, user, expect=200):
        response = self.as_(user).get(URL)
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def list_count(self, user, **query):
        response = self.as_(user).get("/api/requests/", page_size=1, **query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["count"]

    def make_runs(self, user, n, prefix="r"):
        return [self.submit(user, f"Requester: Person {prefix}{i}\nItem: {i + 1} | | Thing {prefix}{i}\n") for i in range(n)]


@tag("requires_postgres")
@requires_postgres
class AggregationTests(SummaryCase):
    def test_an_empty_database_gives_zeros_for_every_state_and_an_empty_recent_list(self):
        for user in (self.approver, self.operator, self.superuser):
            with self.subTest(user=user.username):
                data = self.summary(user)
                self.assertEqual(set(data), TOP_FIELDS)
                self.assertEqual((data["total"], data["processing"], data["needs_review"], data["approved"], data["failed"]), (0, 0, 0, 0, 0))
                self.assertEqual(data["by_state"], {state: 0 for state in STATES})
                self.assertEqual(data["recent_requests"], [])

    def test_counts_come_from_the_real_rows_in_every_state(self):
        wanted = {"queued": 2, "extracting": 1, "needs_review": 3, "approved": 2, "exporting": 1, "exported": 4, "failed": 2, "cancelled": 1}
        runs = self.make_runs(self.operator, sum(wanted.values()))
        index = 0
        for state, n in wanted.items():
            for _ in range(n):
                set_state(runs[index], state)
                index += 1
        data = self.summary(self.approver)
        self.assertEqual(data["by_state"], wanted)
        self.assertEqual(data["total"], sum(wanted.values()))
        self.assertEqual(data["processing"], sum(wanted[s] for s in PROCESSING))
        self.assertEqual((data["needs_review"], data["approved"], data["failed"]), (3, 2, 2))
        self.assertEqual(data["total"], sum(data["by_state"].values()))

    def test_the_summary_agrees_with_the_list_endpoint_for_every_state_and_user(self):
        for i, run_id in enumerate(self.make_runs(self.operator, 4) + self.make_runs(self.other_operator, 3, "o")):
            set_state(run_id, ("queued", "needs_review", "approved", "failed", "exported", "queued", "cancelled")[i])
        for user in (self.operator, self.other_operator, self.approver, self.auditor, self.superuser, self.hybrid):
            data = self.summary(user)
            with self.subTest(user=user.username):
                self.assertEqual(data["total"], self.list_count(user))
                for state in STATES:
                    self.assertEqual(data["by_state"][state], self.list_count(user, state=state), state)

    def test_real_extraction_and_approval_move_the_counts(self):
        run_id = self.extracted()
        self.assertEqual(self.summary(self.approver)["by_state"]["needs_review"], 1)
        response = self.as_(self.approver).post(f"/api/requests/{run_id}/approve/", self.approve_body(run_id))
        self.assertEqual(response.status_code, 200, response.content)
        data = self.summary(self.approver)
        self.assertEqual((data["needs_review"], data["approved"], data["total"]), (0, 1, 1))
        self.assertEqual(data["recent_requests"][0]["state"], "approved")

    def test_a_run_that_failed_extraction_is_counted_as_failed(self):
        run_id = self.submit(self.operator)
        set_state(run_id, "failed")
        data = self.summary(self.operator)
        self.assertEqual((data["failed"], data["by_state"]["failed"]), (1, 1))
        self.assertEqual(data["recent_requests"][0]["failed_step"], "extract")


@tag("requires_postgres")
@requires_postgres
class VisibilityTests(SummaryCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_runs(self.operator, 3, "m")
        self.theirs = self.make_runs(self.other_operator, 2, "t")
        self.everything = {str(r) for r in self.mine + self.theirs}
        set_state(self.mine[0], "needs_review")
        set_state(self.theirs[0], "failed")

    def test_an_operator_counts_and_lists_only_their_own_requests(self):
        for user, own in ((self.operator, self.mine), (self.other_operator, self.theirs)):
            with self.subTest(user=user.username):
                data = self.summary(user)
                self.assertEqual(data["total"], len(own))
                self.assertEqual({r["run_id"] for r in data["recent_requests"]}, {str(r) for r in own})
                self.assertEqual({r["submitted_by"] for r in data["recent_requests"]}, {str(user.public_id)})

    def test_an_operator_never_sees_another_users_state_in_the_counts(self):
        data = self.summary(self.operator)
        self.assertEqual((data["by_state"]["failed"], data["failed"]), (0, 0))
        self.assertEqual(data["by_state"]["needs_review"], 1)
        other = self.summary(self.other_operator)
        self.assertEqual((other["by_state"]["failed"], other["by_state"]["needs_review"]), (1, 0))

    def test_approvers_auditors_superusers_and_view_any_hybrids_see_everything(self):
        for user in (self.approver, self.auditor, self.superuser, self.hybrid):
            with self.subTest(user=user.username):
                data = self.summary(user)
                self.assertEqual(data["total"], 5)
                self.assertEqual({r["run_id"] for r in data["recent_requests"]}, self.everything)

    def test_a_user_with_no_view_permission_gets_the_apis_403_not_zeros(self):
        response = self.as_(self.nobody).get(URL)
        self.assertEqual((response.status_code, base.err(response)), (403, "permission_denied"))

    def test_an_anonymous_client_gets_the_apis_403_and_nothing_else(self):
        response = base.Browser().client.get(URL)
        self.assertEqual((response.status_code, base.err(response)), (403, "not_authenticated"))
        self.assertNotIn("total", response.json())

    def test_an_inactive_user_cannot_read_the_summary(self):
        browser = self.as_(self.operator)
        type(self.operator).objects.filter(pk=self.operator.pk).update(is_active=False)
        self.assertEqual(browser.get(URL).status_code, 403)

    def test_a_user_who_loses_the_role_is_refused_on_the_next_call(self):
        self.assertEqual(self.summary(self.approver)["total"], 5)
        self.approver.groups.clear()
        self.assertEqual(self.summary(self.approver, expect=403)["error"]["code"], "permission_denied")

    def test_the_endpoint_is_get_only_and_takes_no_query_parameters(self):
        browser = self.as_(self.approver)
        self.assertEqual(browser.post(URL, {}).status_code, 405)
        self.assertEqual(browser.get(URL, state="queued").status_code, 400)

    def test_the_response_is_not_cacheable(self):
        self.assertIn("no-store", self.as_(self.approver).get(URL)["Cache-Control"])


@tag("requires_postgres")
@requires_postgres
class RecentRequestsTests(SummaryCase):
    def test_recent_requests_are_the_five_newest_in_stable_order_and_independent_of_paging(self):
        runs = self.make_runs(self.operator, 8)
        data = self.summary(self.approver)
        self.assertEqual(len(data["recent_requests"]), 5)
        self.assertEqual(data["total"], 8)
        expected = [str(r) for r in reversed(runs)][:5]
        self.assertEqual([r["run_id"] for r in data["recent_requests"]], expected)
        for page_size in (1, 2, 100):
            first = self.as_(self.approver).get("/api/requests/", page=1, page_size=page_size).json()
            self.assertEqual(first["count"], 8)
            self.assertEqual(self.summary(self.approver)["recent_requests"], data["recent_requests"])
        self.assertEqual([r["run_id"] for r in self.as_(self.approver).get("/api/requests/", page=1, page_size=5).json()["results"]], expected)

    def test_rows_with_the_same_creation_time_keep_a_deterministic_order(self):
        runs = self.make_runs(self.operator, 6)
        same = WorkflowRun.objects.get(pk=runs[0]).created_at
        WorkflowRun.objects.filter(pk__in=runs).update(created_at=same)
        first = [r["run_id"] for r in self.summary(self.approver)["recent_requests"]]
        for _ in range(3):
            self.assertEqual([r["run_id"] for r in self.summary(self.approver)["recent_requests"]], first)
        self.assertEqual(first, [r["run_id"] for r in self.as_(self.approver).get("/api/requests/", page_size=5).json()["results"]])

    def test_a_recent_item_is_exactly_the_lists_item(self):
        run_id = self.extracted()
        recent = self.summary(self.approver)["recent_requests"][0]
        listed = self.as_(self.approver).get("/api/requests/").json()["results"][0]
        self.assertEqual(set(recent), ITEM_FIELDS)
        self.assertEqual(recent, listed)
        self.assertEqual((recent["run_id"], recent["current_revision_number"], recent["outcome_status"]), (str(run_id), 1, "valid"))

    def test_a_request_with_no_revision_yet_has_none_for_the_revision_and_outcome(self):
        self.submit(self.operator)
        item = self.summary(self.operator)["recent_requests"][0]
        self.assertEqual((item["state"], item["current_revision_number"], item["outcome_status"]), ("queued", None, None))

    def test_no_request_text_or_parsed_content_is_in_the_response(self):
        self.extracted(text=base.SECRET_TEXT)
        for user in (self.operator, self.approver):
            body = self.as_(user).get(URL).content.decode()
            for marker in ("SECRETNAME", "SECRETITEM", "jane.secret", "Stapler", "password", "draft", "findings"):
                self.assertNotIn(marker, body, marker)


@tag("requires_postgres")
@requires_postgres
class QueryCountTests(SummaryCase):
    def count_queries(self, user):
        self.as_(user).get(URL)  # warm the session and the per-process caches
        with CaptureQueriesContext(connection) as captured:
            response = self.as_(user).get(URL)
        self.assertEqual(response.status_code, 200)
        return captured

    def test_the_number_of_queries_does_not_grow_with_the_number_of_requests(self):
        self.make_runs(self.operator, 1)
        few = self.count_queries(self.approver)
        self.make_runs(self.operator, 30, "x")
        many = self.count_queries(self.approver)
        self.assertEqual(len(few), len(many), [q["sql"] for q in many.captured_queries])
        self.assertLessEqual(len(many), 8)

    def test_exactly_one_grouped_count_and_one_bounded_recent_query_touch_the_run_table(self):
        self.make_runs(self.operator, 10)
        captured = self.count_queries(self.approver)
        runs = [q["sql"] for q in captured.captured_queries if '"cases_workflowrun"' in q["sql"]]
        self.assertEqual(len(runs), 2, runs)
        self.assertEqual(sum("GROUP BY" in sql for sql in runs), 1)
        self.assertEqual(sum("LIMIT 5" in sql for sql in runs), 1)

    def test_the_own_scope_costs_no_more_queries_than_the_see_all_scope(self):
        self.make_runs(self.operator, 12)
        self.assertLessEqual(len(self.count_queries(self.operator)), len(self.count_queries(self.approver)) + 1)

    def test_the_recent_query_does_not_load_the_source_text_or_the_password_hash(self):
        self.extracted(text=base.SECRET_TEXT)
        captured = self.count_queries(self.approver)
        recent = [q["sql"] for q in captured.captured_queries if "LIMIT 5" in q["sql"]][0]
        select = recent.split(" FROM ", 1)[0]
        for column in ('"intake_requestsource"."text"', '"accounts_user"."password"', "canonical", "draft"):
            self.assertNotIn(column, select, column)


@tag("requires_postgres")
@requires_postgres
class RegressionTests(SummaryCase):
    """The existing list and detail endpoints behave as before; the summary changed their shared code."""

    def test_the_list_still_filters_pages_and_scopes(self):
        mine = self.make_runs(self.operator, 3, "m")
        theirs = self.make_runs(self.other_operator, 2, "t")
        set_state(mine[0], "needs_review")
        everything = {str(r) for r in mine + theirs}
        data = self.as_(self.approver).get("/api/requests/", page_size=100).json()
        self.assertEqual({r["run_id"] for r in data["results"]}, everything)
        self.assertEqual({r["run_id"] for r in self.as_(self.operator).get("/api/requests/").json()["results"]}, {str(r) for r in mine})
        self.assertEqual(len(self.as_(self.approver).get("/api/requests/", state="needs_review").json()["results"]), 1)
        response = self.as_(self.nobody).get("/api/requests/")
        self.assertEqual((response.status_code, base.err(response)), (403, "permission_denied"))

    def test_the_detail_still_hides_another_users_request(self):
        run_id = self.submit(self.operator)
        self.assertEqual(self.detail(self.approver, run_id).status_code, 200)
        self.assertEqual(self.detail(self.other_operator, run_id).status_code, 404)


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        rates = override_settings(API_THROTTLE_RATES=base.RATES, **base.FAST_HASHERS)
        rates.enable()
        self.addCleanup(rates.disable)
        call_command("setup_roles", stdout=io.StringIO())
        self.operator = base.make_user("olive", OPERATOR)
        self.approver = base.make_user("anna", APPROVER)

    def login(self, user):
        browser = base.Browser()
        assert browser.login(user.username).status_code == 200
        return browser

    def test_readers_during_writes_always_get_a_consistent_summary(self):
        operator = self.login(self.operator)
        runs = []
        for i in range(12):
            response = operator.post("/api/requests/", {"text": f"Requester: Person {i}\nItem: 1 | | Thing {i}\n"})
            self.assertIn(response.status_code, (200, 201), response.content)
            runs.append(response.json()["run_id"])
        self.assertEqual(len(runs), 12)
        readers = [self.login(self.approver) for _ in range(3)]
        done = threading.Event()
        cycle = ("queued", "needs_review", "approved", "failed", "exported")

        def write():
            try:
                for step in range(25):
                    for i, run_id in enumerate(runs):
                        set_state(run_id, cycle[(step + i) % len(cycle)])
            finally:
                done.set()

        def read(browser):
            def target():
                seen = []
                while not done.is_set() or len(seen) < 5:
                    response = browser.get(URL)
                    assert response.status_code == 200, response.content
                    seen.append(response.json())
                return seen
            return target

        results = run_threads(self, [write] + [read(b) for b in readers])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        for _, seen in results[1:]:
            for data in seen:
                self.assertEqual(data["total"], 12)  # every snapshot of the count query is one consistent statement
                self.assertEqual(data["total"], sum(data["by_state"].values()))
                self.assertEqual(data["processing"], sum(data["by_state"][s] for s in PROCESSING))
                self.assertLessEqual(len(data["recent_requests"]), 5)
        final = readers[0].get(URL).json()
        self.assertEqual(final["total"], 12)

    def test_new_requests_arriving_during_reads_never_break_the_invariants(self):
        operator = self.login(self.operator)
        reader = self.login(self.approver)

        def submit_many():
            ids = []
            for i in range(10):
                response = operator.post("/api/requests/", {"text": f"Requester: Person {i}\nItem: 1 | | Thing {i}\n"})
                assert response.status_code in (200, 201), response.content
                ids.append(response.json()["run_id"])
            return ids

        def read_many():
            totals = []
            for _ in range(15):
                data = reader.get(URL).json()
                assert data["total"] == sum(data["by_state"].values())
                totals.append(data["total"])
            return totals

        results = run_threads(self, [submit_many, read_many])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        totals = results[1][1]
        self.assertEqual(totals, sorted(totals))  # requests are only added, so a reader never sees the total go down
        self.assertTrue(all(0 <= t <= 10 for t in totals))
        self.assertEqual(reader.get(URL).json()["total"], 10)
        self.assertEqual(len(results[0][1]), 10)
