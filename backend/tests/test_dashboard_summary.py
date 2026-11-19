"""BE-DASH-1: the dashboard summary service and endpoint, without a database.

What these prove: the response contract (exact keys, zero-filled states, sums), the permission decision for every role
(through the SAME visibility function as the request list), the unauthorized and refused responses, the SQL the
service builds (compiled, never executed: one GROUP BY, no per-state query, a bounded and stably ordered recent list), and
the architecture boundaries (a thin view, no duplicated rule, nothing that reads request text).

What they cannot prove, because the development VM has no PostgreSQL: real aggregation, real visibility, real query counts
and concurrent changes. ``tests/test_dashboard_summary_postgres.py`` covers those and has NOT been run by the VM.
"""

import ast
import inspect
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from apps.accounts.authz import Actor
from apps.accounts.errors import AuthorizationError
from apps.accounts.roles import ROLE_PERMISSIONS
from apps.api import serializers, views_dashboard
from apps.cases.run_state import STATES
from apps.intake import authorization, queries
from tests import test_api_views as base
from tests.test_request_intake_boundaries import imported_modules

BACKEND = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ITEM_FIELDS = {"run_id", "case_id", "reference", "state", "failed_step", "created_at", "current_revision_number", "outcome_status", "submitted_by"}
SUMMARY_FIELDS = {"total", "processing", "needs_review", "approved", "failed", "by_state", "recent_requests"}


def actor(*, pk=1, superuser=False, permissions=frozenset()) -> Actor:
    return Actor(user=object(), pk=pk, public_id=uuid.uuid4(), is_superuser=superuser, permissions=frozenset(permissions))


def with_actor(fake):
    return mock.patch.object(authorization, "load_actor", return_value=fake)


class FakeRuns:
    """Stands in for the visible-runs queryset: records the calls the service makes and answers them from fixed data."""

    def __init__(self, counts=(), recent=()):
        self.counts, self.recent, self.calls = list(counts), list(recent), []

    def order_by(self, *args):
        self.calls.append(("order_by", args))
        return self

    def values(self, *args):
        self.calls.append(("values", args))
        return self

    def annotate(self, **kwargs):
        self.calls.append(("annotate", tuple(kwargs)))
        return self.counts

    def select_related(self, *args):
        self.calls.append(("select_related", args))
        return self

    def only(self, *args):
        self.calls.append(("only", args))
        return self

    def __getitem__(self, key):
        self.calls.append(("slice", (key.start, key.stop)))
        return self.recent[key]


def fake_run(state="needs_review", number=1):
    return SimpleNamespace(
        pk=uuid.uuid4(),
        case_id=uuid.uuid4(),
        case=SimpleNamespace(reference="REQ-20260102-ABCDEF12"),
        state=state,
        failed_step="",
        created_at=NOW,
        current_revision=SimpleNamespace(revision_number=number),
        extraction_outcome=SimpleNamespace(status="valid"),
        request_source=SimpleNamespace(submitted_by=SimpleNamespace(public_id=uuid.uuid4())),
    )


def summary_of(counts=(), recent=(), *, who=None):
    """Run the real service against ``FakeRuns``; returns (summary, the fake queryset, the arguments ``_visible_runs`` got)."""
    runs = FakeRuns([{"state": s, "n": n} for s, n in counts], recent)
    with with_actor(who or actor(permissions=ROLE_PERMISSIONS["Auditor"])), mock.patch.object(queries, "_visible_runs", return_value=runs) as visible:
        result = queries.get_dashboard_summary(user=object())
    return result, runs, visible.call_args


class AggregationTests(SimpleTestCase):
    def test_every_state_is_present_and_zero_when_there_are_no_requests(self):
        result, _, _ = summary_of()
        self.assertEqual(list(result.by_state), list(STATES))
        self.assertEqual(set(result.by_state.values()), {0})
        self.assertEqual((result.total, result.processing, result.needs_review, result.approved, result.failed), (0, 0, 0, 0, 0))
        self.assertEqual(result.recent_requests, ())

    def test_counts_total_and_the_processing_group_come_from_the_grouped_rows(self):
        result, _, _ = summary_of([("queued", 2), ("extracting", 3), ("needs_review", 5), ("approved", 7), ("failed", 11), ("exporting", 13), ("exported", 17), ("cancelled", 19)])
        self.assertEqual(result.by_state, {"queued": 2, "extracting": 3, "needs_review": 5, "approved": 7, "exporting": 13, "exported": 17, "failed": 11, "cancelled": 19})
        self.assertEqual(result.total, sum(result.by_state.values()))
        self.assertEqual(result.total, 2 + 3 + 5 + 7 + 11 + 13 + 17 + 19)
        self.assertEqual(result.processing, 2 + 3 + 13)  # queued, extracting and exporting only
        self.assertEqual((result.needs_review, result.approved, result.failed), (5, 7, 11))

    def test_the_total_is_always_the_sum_of_the_states_so_the_counts_cannot_disagree(self):
        for counts in ([("failed", 4)], [("queued", 1), ("cancelled", 1)], [("exported", 9), ("approved", 1)]):
            with self.subTest(counts=counts):
                result, _, _ = summary_of(counts)
                self.assertEqual(result.total, sum(result.by_state.values()))
                self.assertEqual(result.total, sum(n for _, n in counts))

    def test_the_processing_group_is_a_subset_of_the_run_states_and_excludes_review_and_terminal_states(self):
        self.assertTrue(set(queries.PROCESSING_STATES) <= set(STATES))
        self.assertEqual(set(queries.PROCESSING_STATES), {"queued", "extracting", "exporting"})

    def test_the_aggregate_is_one_grouped_query_and_the_recent_list_is_one_bounded_query(self):
        _, runs, _ = summary_of([("queued", 1)], [fake_run()])
        names = [name for name, _ in runs.calls]
        self.assertEqual(names, ["order_by", "values", "annotate", "select_related", "only", "slice"])  # no per-state filter, no count(), no loop of queries
        self.assertEqual(dict(runs.calls)["order_by"], ())  # the list's ordering is cleared, or it would join the GROUP BY
        self.assertEqual(dict(runs.calls)["values"], ("state",))
        self.assertEqual(dict(runs.calls)["slice"], (None, queries.RECENT_LIMIT))
        self.assertEqual(dict(runs.calls)["select_related"], queries.LIST_RELATED)  # the list's joins: no query per row
        self.assertEqual(dict(runs.calls)["only"], queries.RECENT_FIELDS)

    def test_the_recent_list_is_capped_and_keeps_the_service_order(self):
        rows = [fake_run(number=n) for n in range(1, 9)]
        result, _, _ = summary_of([("needs_review", 8)], rows)
        self.assertEqual(queries.RECENT_LIMIT, 5)
        self.assertEqual([i.current_revision_number for i in result.recent_requests], [1, 2, 3, 4, 5])

    def test_a_recent_item_is_exactly_the_lists_item(self):
        run = fake_run("approved", 3)
        result, _, _ = summary_of([("approved", 1)], [run])
        self.assertEqual(result.recent_requests, (queries._list_item(run),))
        self.assertIsInstance(result.recent_requests[0], queries.RequestListItem)

    def test_a_run_without_an_outcome_or_revision_has_none_for_both(self):
        run = fake_run()
        run.current_revision, run.extraction_outcome = None, None
        item = summary_of([("queued", 1)], [run])[0].recent_requests[0]
        self.assertEqual((item.current_revision_number, item.outcome_status), (None, None))


class VisibilityTests(SimpleTestCase):
    """The same scope the list uses, decided by the same function, before any request is read."""

    def visible_args(self, who):
        _, _, call = summary_of(who=who)
        return call.args

    def test_roles_with_view_any_see_everything(self):
        for role in ("Approver", "Auditor"):
            with self.subTest(role=role):
                self.assertEqual(self.visible_args(actor(pk=5, permissions=ROLE_PERMISSIONS[role])), (5, True, None))

    def test_an_operator_sees_only_their_own_requests(self):
        self.assertEqual(self.visible_args(actor(pk=5, permissions=ROLE_PERMISSIONS["Operator"])), (5, False, None))

    def test_a_superuser_sees_everything_even_with_no_group(self):
        self.assertEqual(self.visible_args(actor(pk=5, superuser=True)), (5, True, None))

    def test_a_hybrid_with_view_any_sees_everything(self):
        both = ROLE_PERMISSIONS["Operator"] | ROLE_PERMISSIONS["Approver"]
        self.assertEqual(self.visible_args(actor(pk=5, permissions=both)), (5, True, None))

    def test_a_user_with_no_view_permission_is_refused_and_nothing_is_read(self):
        for held in (frozenset(), frozenset({"intake.submit_request"}), frozenset({"intake.approve_request", "intake.reject_request"})):
            with self.subTest(held=sorted(held)), with_actor(actor(permissions=held)), mock.patch.object(queries, "_visible_runs") as visible, self.assertLogs("apps.accounts.authz", level="WARNING"):
                with self.assertRaises(AuthorizationError) as caught:
                    queries.get_dashboard_summary(user=object())
                self.assertEqual(caught.exception.reason, "missing_permission")
                visible.assert_not_called()

    def test_an_unauthenticated_or_inactive_user_is_refused_before_anything_is_read(self):
        for reason in ("not_authenticated", "inactive"):
            with self.subTest(reason=reason), mock.patch.object(authorization, "load_actor", side_effect=AuthorizationError(reason)), mock.patch.object(queries, "_visible_runs") as visible:
                with self.assertRaises(AuthorizationError) as caught:
                    queries.get_dashboard_summary(user=object())
                self.assertEqual(caught.exception.reason, reason)
                visible.assert_not_called()

    def test_list_and_summary_decide_visibility_with_the_same_function(self):
        for who in (actor(pk=5, permissions=ROLE_PERMISSIONS["Operator"]), actor(pk=5, permissions=ROLE_PERMISSIONS["Auditor"]), actor(pk=5, superuser=True)):
            with self.subTest(superuser=who.is_superuser, permissions=sorted(who.permissions)), with_actor(who):
                scope = queries._view_scope(object())
                runs = mock.MagicMock()
                runs.count.return_value = 0
                runs.select_related.return_value = []
                with mock.patch.object(queries, "_visible_runs", return_value=runs) as visible:
                    queries.list_requests(user=object())
                self.assertEqual(visible.call_args.args[:2], (scope[0].pk, scope[1]))
                self.assertEqual(self.visible_args(who)[:2], (scope[0].pk, scope[1]))


class CompiledQueryTests(SimpleTestCase):
    """The querysets compile to the SQL intended. Compiled only: nothing is executed and no connection is opened."""

    def aggregate_sql(self, pk, sees_all):
        return str(queries._visible_runs(pk, sees_all, None).order_by().values("state").annotate(n=queries.Count("pk")).query)

    def test_the_aggregate_is_one_group_by_state_with_no_ordering(self):
        sql = self.aggregate_sql(7, True)
        self.assertRegex(sql, r'^SELECT "cases_workflowrun"\."state" AS "state", COUNT\(.*GROUP BY (1|"cases_workflowrun"\."state")$')  # Django 5.2 groups by position
        self.assertNotIn("ORDER BY", sql)
        self.assertIn("COUNT(", sql)
        self.assertEqual(sql.count("SELECT"), 1)

    def test_the_aggregate_counts_only_runs_that_have_a_source_like_the_list(self):
        self.assertIn('INNER JOIN "intake_requestsource"', self.aggregate_sql(7, True))

    def test_an_own_scope_aggregate_filters_on_the_submitter_and_a_see_all_one_does_not(self):
        self.assertIn('"intake_requestsource"."submitted_by_id" = 7', self.aggregate_sql(7, False))
        self.assertNotIn("submitted_by_id", self.aggregate_sql(7, True))

    def test_the_aggregate_reads_no_text_or_draft_column(self):
        sql = self.aggregate_sql(7, False)
        for column in ('"text"', "text_sha256", "draft", "canonical", "payload"):
            self.assertNotIn(column, sql)

    def recent_sql(self, pk=7, sees_all=False):
        runs = queries._visible_runs(pk, sees_all, None).select_related(*queries.LIST_RELATED).only(*queries.RECENT_FIELDS)
        return str(runs[: queries.RECENT_LIMIT].query)

    def test_the_recent_query_is_limited_and_ordered_newest_first_with_the_id_as_tiebreak(self):
        sql = self.recent_sql()
        self.assertTrue(sql.rstrip().endswith("LIMIT 5"), sql[-40:])
        self.assertLess(sql.index('"created_at" DESC'), sql.index('"id" DESC'))

    def test_the_recent_query_is_one_statement_with_the_lists_joins_and_the_own_scope_filter(self):
        sql = self.recent_sql()
        self.assertEqual(sql.count("SELECT"), 1)
        self.assertIn('"intake_requestsource"."submitted_by_id" = 7', sql)
        self.assertNotIn("submitted_by_id", self.recent_sql(sees_all=True).split(" WHERE ", 1)[1])  # a see-all user has no submitter filter
        for table in ("cases_case", "cases_resultrevision", "accounts_user", "intake_extractionoutcome"):
            self.assertIn(f'JOIN "{table}"', sql)

    def test_the_recent_query_selects_no_text_bytes_draft_findings_or_password(self):
        # ``only`` must not break ``select_related`` (Django refuses a deferred-and-traversed field at compile time).
        select = self.recent_sql().split(" FROM ", 1)[0]
        for column in ('"intake_requestsource"."text"', "text_sha256", "canonical_bytes", '"intake_extractionoutcome"."draft"', "ambiguities", '"password"', '"email"', "extractor_name"):
            self.assertNotIn(column, select)
        for column in ('"cases_case"."reference"', '"cases_resultrevision"."revision_number"', '"accounts_user"."public_id"', '"intake_extractionoutcome"."status"', '"cases_workflowrun"."state"'):
            self.assertIn(column, select)

class FakeSummary:
    """What a service result looks like to the serializer, plus attributes that must never reach the response."""

    def __init__(self, recent=()):
        self.total, self.processing, self.needs_review, self.approved, self.failed = 8, 3, 1, 2, 1
        self.by_state = dict.fromkeys(STATES, 0) | {"queued": 1, "extracting": 1, "exporting": 1, "needs_review": 1, "approved": 2, "failed": 1, "exported": 1}
        self.recent_requests = recent
        self.text = "SECRET REQUEST TEXT"
        self.draft = {"items": []}


def listed(**overrides):
    item = {
        "run_id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "reference": "REQ-20260102-ABCDEF12",
        "state": "needs_review",
        "failed_step": "",
        "created_at": NOW,
        "current_revision_number": 1,
        "outcome_status": "valid",
        "submitted_by": uuid.uuid4(),
        "text": "SECRET REQUEST TEXT",
    }
    item.update(overrides)
    return SimpleNamespace(**item)


class EndpointContractTests(base.ApiTestCase):
    URL = "/api/dashboard/summary/"

    def setUp(self):
        super().setUp()
        self.user = base.fake_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def get(self, summary=None, query=""):
        with mock.patch.object(queries, "get_dashboard_summary", return_value=summary or FakeSummary()) as service:
            response = self.client.get(self.URL + query)
        return response, service

    def test_the_response_has_exactly_the_documented_fields(self):
        response, service = self.get(FakeSummary([listed()]))
        self.assertEqual(response.status_code, 200)
        data = base.body(response)
        self.assertEqual(set(data), SUMMARY_FIELDS)
        self.assertEqual(set(data["by_state"]), set(STATES))
        self.assertEqual(set(data["recent_requests"][0]), ITEM_FIELDS)
        service.assert_called_once_with(user=self.user)

    def test_the_types_and_values_are_those_of_the_service_result(self):
        data = base.body(self.get(FakeSummary([listed()]))[0])
        for key in ("total", "processing", "needs_review", "approved", "failed"):
            self.assertIsInstance(data[key], int)
        self.assertEqual((data["total"], data["processing"], data["needs_review"], data["approved"], data["failed"]), (8, 3, 1, 2, 1))
        self.assertEqual(data["total"], sum(data["by_state"].values()))
        item = data["recent_requests"][0]
        self.assertEqual((item["state"], item["failed_step"], item["current_revision_number"], item["outcome_status"]), ("needs_review", "", 1, "valid"))
        uuid.UUID(item["run_id"]), uuid.UUID(item["submitted_by"])

    def test_the_recent_item_has_the_same_fields_as_the_request_list_item(self):
        self.assertEqual(set(serializers.RequestListItemOutput().fields), ITEM_FIELDS)
        self.assertEqual(set(serializers.DashboardSummaryOutput().fields), SUMMARY_FIELDS)
        self.assertIs(type(serializers.DashboardSummaryOutput().fields["recent_requests"].child), serializers.RequestListItemOutput)

    def test_no_request_text_draft_or_source_can_leave_the_endpoint(self):
        payload = json.dumps(base.body(self.get(FakeSummary([listed()]))[0]))
        self.assertNotIn("SECRET", payload)
        for word in ('"text"', '"draft"', "canonical", "payload", "source", "content_hash"):
            self.assertNotIn(word, payload)

    def test_an_empty_summary_serializes_with_empty_collections(self):
        empty = FakeSummary()
        empty.total = empty.processing = empty.needs_review = empty.approved = empty.failed = 0
        empty.by_state = dict.fromkeys(STATES, 0)
        data = base.body(self.get(empty)[0])
        self.assertEqual((data["total"], data["recent_requests"]), (0, []))
        self.assertEqual(set(data["by_state"].values()), {0})

    def test_the_response_is_never_cached(self):
        self.assertIn("no-store", self.get()[0]["Cache-Control"])

    def test_the_endpoint_takes_no_query_parameters(self):
        for query in ("?state=failed", "?page=2", "?recent=50", "?x=1"):
            with self.subTest(query=query):
                response, service = self.get(query=query)
                self.assertEqual((response.status_code, base.body(response)["error"]["code"]), (400, "validation_error"))
                service.assert_not_called()

    def test_only_get_is_allowed(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method), mock.patch.object(queries, "get_dashboard_summary") as service:
                response = getattr(self.client, method)(self.URL, {}, format="json")
                self.assertEqual(response.status_code, 405)
                service.assert_not_called()


class EndpointAuthorizationTests(base.ApiTestCase):
    URL = "/api/dashboard/summary/"

    def test_an_anonymous_request_is_refused_before_the_service_is_called(self):
        with mock.patch.object(queries, "get_dashboard_summary") as service:
            response = APIClient().get(self.URL)
        self.assertEqual((response.status_code, base.body(response)["error"]["code"]), (403, "not_authenticated"))  # the API's existing answer for no session
        service.assert_not_called()
        self.assertIn("no-store", response["Cache-Control"])

    def test_a_refusal_by_the_service_is_the_apis_existing_403(self):
        client = APIClient()
        client.force_authenticate(user=base.fake_user())
        # The API's own mapping (errors.problem_for), unchanged: a missing permission is permission_denied, the rest not_authenticated.
        for reason, code in (("missing_permission", "permission_denied"), ("inactive", "not_authenticated"), ("not_authenticated", "not_authenticated")):
            with self.subTest(reason=reason), mock.patch.object(queries, "get_dashboard_summary", side_effect=AuthorizationError(reason)):
                response = client.get(self.URL)
                data = base.body(response)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(data["error"]["code"], code)
                self.assertEqual(set(data["error"]), {"code", "message"})

    def test_the_view_keeps_the_project_authentication_and_permission_defaults(self):
        self.assertNotIn("authentication_classes", views_dashboard.DashboardSummaryView.__dict__)
        self.assertNotIn("permission_classes", views_dashboard.DashboardSummaryView.__dict__)
        from rest_framework.permissions import IsAuthenticated
        from rest_framework.authentication import SessionAuthentication

        view = views_dashboard.DashboardSummaryView()
        self.assertEqual([type(p) for p in view.get_permissions()], [IsAuthenticated])
        self.assertEqual([type(a) for a in view.get_authenticators()], [SessionAuthentication])

    def test_the_route_is_throttled_like_every_other_api_view(self):
        self.assertTrue(any(t.__name__ == "ApiUserThrottle" for t in views_dashboard.DashboardSummaryView.throttle_classes))


class ArchitectureTests(SimpleTestCase):
    def function_source(self):
        return inspect.getsource(queries.get_dashboard_summary)

    def test_the_view_is_a_thin_adapter_over_one_service(self):
        path = BACKEND / "apps" / "api" / "views_dashboard.py"
        modules = imported_modules(path)
        self.assertEqual({m for m in modules if m.endswith(".models") or m.startswith("django.db") or m.startswith("workflows")}, set())
        source = path.read_text(encoding="utf-8")
        for token in (".objects", "transaction", "cursor", ".save(", ".delete(", "logger", "print("):
            self.assertNotIn(token, source)
        calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
        services = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "get_dashboard_summary"]
        self.assertEqual(len(services), 1)
        self.assertEqual({k.arg: ast.unparse(k.value) for k in services[0].keywords}, {"user": "request.user"})

    def test_visibility_is_not_duplicated_the_service_uses_the_lists_functions(self):
        names = {n.id for n in ast.walk(ast.parse(self.function_source().lstrip())) if isinstance(n, ast.Name)}
        self.assertLessEqual({"_view_scope", "_visible_runs", "_list_item"}, names)
        self.assertEqual(names & {"scope_problem", "access_problem", "permission_problem", "require_permission", "load_actor", "is_superuser", "submitted_by_id"}, set())

    def test_list_and_summary_share_the_scope_and_row_mapping_helpers(self):
        source = inspect.getsource(queries.list_requests)
        self.assertIn("_view_scope(user)", source)
        self.assertIn("_list_item(", source)
        for forbidden in ("scope_problem(", "load_actor(", "require_permission("):
            self.assertNotIn(forbidden, source)

    def test_the_service_only_reads_through_the_one_aggregate_and_the_one_list_query(self):
        source = self.function_source()
        self.assertEqual(source.count(".annotate("), 1)
        self.assertEqual(source.count(".select_related("), 1)
        for token in (".count(", ".filter(", ".exclude(", ".exists(", ".get(", ".first(", ".save(", ".create(", ".update(", ".delete(", ".raw(", ".extra(", "cursor", "transaction", "RequestSource", ".text", "canonical", "draft"):
            self.assertNotIn(token, source)

    def test_the_service_is_read_only_and_adds_no_migration_or_model(self):
        migrations = {p.name for p in (BACKEND / "apps" / "intake" / "migrations").glob("0*.py")} | {p.name for p in (BACKEND / "apps" / "cases" / "migrations").glob("0*.py")}
        self.assertFalse([m for m in migrations if "dashboard" in m or "index" in m.lower()])

    def test_the_service_takes_the_user_and_nothing_else_so_a_client_cannot_choose_scope_or_size(self):
        self.assertEqual(list(inspect.signature(queries.get_dashboard_summary).parameters), ["user"])