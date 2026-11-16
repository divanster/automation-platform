"""DB-free tests: the read services behind the API (decoder, allowed actions, validation, audit paging)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.accounts.roles import ROLE_PERMISSIONS
from apps.cases.errors import RecordNotFoundError
from apps.cases.run_state import STATES
from apps.intake import queries
from apps.intake.draft_json import draft_from_canonical_bytes, draft_to_json
from apps.intake.errors import UnsupportedDraftError
from tests.intake_support import make_request
from workflows.request_intake import RequestedItem, Requester, SourceRef, StructuredRequest, canonicalize_request

HASH = "c" * 64
REV = uuid.uuid4()


class CanonicalDecoderTests(SimpleTestCase):
    def drafts(self):
        yield make_request(source_refs=())
        yield StructuredRequest(requester=Requester(name="Jane Doe", contact=None), items=(RequestedItem("Pens", Decimal("2.50"), None, "blue"),), notes=None)
        yield StructuredRequest(requester=Requester(name="Ünï   cödé", contact=""), items=(RequestedItem("A", Decimal("1E+3"), "box", ""), RequestedItem("B", 3, None, None)), notes="line\nbreak \"quoted\"")
        yield make_request(source_refs=(SourceRef("requester.name", "Requester: Jane Doe", 1),))

    def test_decoding_the_canonical_bytes_gives_a_draft_that_canonicalizes_to_the_same_bytes(self):
        checked = 0
        for draft in self.drafts():
            source = "Requester: Jane Doe\nItem: 10 | boxes | A4 paper | for printing\nItem: 2 | | Stapler\nNotes: Needed this week.\nContact: jane@example.invalid\nPens\nA\nB\n2.50\n1000\n3\nblue\nÜnï\n"
            try:
                content = canonicalize_request(draft, source_text=source)
            except Exception:  # a draft invalid against this source is not what this test is about
                continue
            with self.subTest(draft=repr(draft)[:40]):
                decoded = draft_from_canonical_bytes(content.canonical_bytes)
                self.assertEqual(canonicalize_request(decoded, source_text=source).canonical_bytes, content.canonical_bytes)
                checked += 1
        self.assertGreaterEqual(checked, 3, "the round trip must really run for several drafts")

    def test_the_decoded_payload_has_the_same_fields_as_the_draft_json_form(self):
        draft = make_request(source_refs=())
        decoded = draft_to_json(draft_from_canonical_bytes(canonicalize_request(draft).canonical_bytes))
        self.assertEqual(set(decoded), set(draft_to_json(draft)))
        self.assertEqual(decoded["items"][0]["quantity"], "10")  # decimal text, not a float

    def test_it_accepts_bytes_like_values(self):
        content = canonicalize_request(make_request(source_refs=()))
        self.assertEqual(draft_from_canonical_bytes(memoryview(content.canonical_bytes)), draft_from_canonical_bytes(bytes(content.canonical_bytes)))

    def test_unreadable_or_unexpected_bytes_are_refused_not_repaired(self):
        good = b'{"canonical_format_version":1,"payload":{"items":[],"notes":null,"requester":{"contact":null,"name":null},"source_refs":[]}}'
        self.assertIsInstance(draft_from_canonical_bytes(good), StructuredRequest)
        bad = [
            b"",
            b"not json",
            b"\xff\xfe",
            b"[]",
            b"null",
            b'{"canonical_format_version":1}',
            b'{"canonical_format_version":2,"payload":{}}',
            b'{"canonical_format_version":true,"payload":{}}',
            b'{"canonical_format_version":"1","payload":{}}',
            b'{"canonical_format_version":1,"payload":{},"extra":1}',
            b'{"canonical_format_version":1,"payload":[]}',
            b'{"canonical_format_version":1,"payload":{"items":[],"notes":null,"requester":{"name":null},"source_refs":[]}}',
            b'{"canonical_format_version":1,"payload":{"items":[{"description":"x","quantity":5,"unit":null,"notes":null}],"notes":null,"requester":{"contact":null,"name":null},"source_refs":[]}}',
            b"[" * 100000,
        ]
        for data in bad:
            with self.subTest(data=data[:30]), self.assertRaises(UnsupportedDraftError):
                draft_from_canonical_bytes(data)


class ListValidationTests(SimpleTestCase):
    def test_bad_paging_and_state_are_refused_before_anything_is_read(self):
        for kwargs in ({"page": 0}, {"page": -1}, {"page": True}, {"page": "1"}, {"page_size": 0}, {"page_size": 101}, {"page_size": 2.5}, {"state": "pending"}, {"state": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                queries.list_requests(user=object(), **kwargs)

    def test_page_bounds_are_the_documented_ones(self):
        self.assertEqual((queries.DEFAULT_PAGE_SIZE, queries.MAX_PAGE_SIZE), (20, 100))


class AllowedActionsTests(SimpleTestCase):
    """``_actions`` against every role and state, with the two database lookups it makes replaced."""

    def actor(self, role=None, pk=1, superuser=False):
        return SimpleNamespace(pk=pk, is_superuser=superuser, permissions=frozenset(ROLE_PERMISSIONS[role]) if role else frozenset())

    def run_of(self, state="needs_review", revision=REV):
        return SimpleNamespace(pk=uuid.uuid4(), state=state, current_revision_id=revision)

    def compute(self, actor, *, submitter=2, state="needs_review", has_revision=True, invalid_outcome=False, authors=(), rejected=False, warnings=()):
        run = self.run_of(state, REV if has_revision else None)
        current = SimpleNamespace(revision_id=REV, content_hash=HASH) if has_revision else None
        with mock.patch.object(queries, "RevisionEdit") as edits, mock.patch.object(queries, "Rejection") as rejections:
            edits.objects.filter.return_value.values_list.return_value = list(authors)
            rejections.objects.filter.return_value.exists.return_value = rejected
            return queries._actions(
                actor=actor, run=run, source=SimpleNamespace(submitted_by_id=submitter), current=current, has_invalid_outcome=invalid_outcome, warnings=warnings
            )

    def test_an_operator_may_edit_their_own_request_in_review_and_nothing_else(self):
        allowed, blockers = self.compute(self.actor("Operator", pk=2), submitter=2)
        self.assertEqual((allowed, blockers), (("edit",), {}))

    def test_an_operator_sees_no_actions_on_somebody_elses_request_and_no_blockers_either(self):
        self.assertEqual(self.compute(self.actor("Operator", pk=1), submitter=2), ((), {}))

    def test_an_approver_may_approve_and_reject_but_not_edit(self):
        allowed, blockers = self.compute(self.actor("Approver"))
        self.assertEqual((allowed, blockers), (("approve", "reject"), {}))

    def test_an_auditor_has_no_actions(self):
        self.assertEqual(self.compute(self.actor("Auditor")), ((), {}))
        self.assertEqual(self.compute(self.actor(None)), ((), {}))

    def test_self_approval_is_a_blocker_for_the_submitter_and_for_an_author(self):
        both = SimpleNamespace(pk=5, is_superuser=False, permissions=frozenset(ROLE_PERMISSIONS["Operator"]) | frozenset(ROLE_PERMISSIONS["Approver"]))
        allowed, blockers = self.compute(both, submitter=5)
        self.assertEqual((allowed, blockers), (("edit", "reject"), {"approve": "self_approval_forbidden"}))
        allowed, blockers = self.compute(self.actor("Approver", pk=9), submitter=2, authors=[3, 9])
        self.assertEqual((allowed, blockers), (("reject",), {"approve": "self_approval_forbidden"}))

    def test_a_superuser_is_bound_by_self_approval_and_state(self):
        root = self.actor(None, pk=1, superuser=True)
        self.assertEqual(self.compute(root, submitter=2), (("edit", "approve", "reject"), {}))
        self.assertEqual(self.compute(root, submitter=1)[1], {"approve": "self_approval_forbidden"})
        self.assertEqual(self.compute(root, submitter=2, authors=[1])[1], {"approve": "self_approval_forbidden"})

    def test_a_rejected_revision_cannot_be_approved(self):
        allowed, blockers = self.compute(self.actor("Approver"), rejected=True)
        self.assertEqual((allowed, blockers), (("reject",), {"approve": "revision_rejected"}))

    def test_warnings_do_not_block_approval_because_the_client_acknowledges_them(self):
        allowed, _ = self.compute(self.actor("Approver"), warnings=(("duplicate_item", "items[1].description"),))
        self.assertIn("approve", allowed)

    def test_every_state_for_every_role(self):
        for state in STATES:
            operator = self.compute(self.actor("Operator", pk=2), submitter=2, state=state)
            approver = self.compute(self.actor("Approver"), state=state)
            with self.subTest(state=state):
                if state in ("needs_review", "approved"):
                    self.assertEqual(operator, (("edit",), {}))
                else:
                    self.assertEqual(operator, ((), {"edit": "run_state_not_editable"}))
                if state == "needs_review":
                    self.assertEqual(approver, (("approve", "reject"), {}))
                else:
                    self.assertEqual(approver, ((), {"approve": "run_not_in_review", "reject": "run_not_in_review"}))

    def test_a_run_without_a_revision(self):
        operator = self.compute(self.actor("Operator", pk=2), submitter=2, has_revision=False, invalid_outcome=True)
        self.assertEqual(operator, (("edit",), {}))  # the first edit
        self.assertEqual(self.compute(self.actor("Operator", pk=2), submitter=2, has_revision=False, invalid_outcome=False)[1], {"edit": "nothing_to_edit"})
        self.assertEqual(self.compute(self.actor("Approver"), has_revision=False, invalid_outcome=True), ((), {"approve": "no_current_revision", "reject": "no_current_revision"}))

    def test_the_decision_uses_the_services_own_pure_rules(self):
        import ast
        from pathlib import Path

        names = {n.id for n in ast.walk(ast.parse(Path(queries.__file__).read_text(encoding="utf-8"))) if isinstance(n, ast.Name)}
        self.assertLessEqual({"edit_problem", "approval_problem", "rejection_problem", "is_self_approval", "access_problem"}, names)


class AuditPagingTests(SimpleTestCase):
    def events(self, n):
        return [SimpleNamespace(id=i) for i in range(1, n + 1)]

    def page(self, n, page, size):
        with mock.patch.object(queries.review, "list_audit_events", return_value=self.events(n)) as reader:
            result = queries.list_run_audit(user="u", run_id=uuid.uuid4(), page=page, page_size=size)
        reader.assert_called_once()
        return result

    def test_pages_slice_in_order_and_report_the_total(self):
        first = self.page(7, 1, 3)
        self.assertEqual(([e.id for e in first.items], first.count, first.page, first.page_size), ([1, 2, 3], 7, 1, 3))
        self.assertEqual([e.id for e in self.page(7, 3, 3).items], [7])
        self.assertEqual(self.page(7, 4, 3).items, ())

    def test_it_uses_the_existing_authorized_reader(self):
        with mock.patch.object(queries.review, "list_audit_events", return_value=self.events(1)) as reader:
            queries.list_run_audit(user="the-user", run_id=RUN_ID, page=1, page_size=10)
        self.assertEqual(reader.call_args.kwargs, {"user": "the-user", "run_id": RUN_ID})

    def test_an_authorization_error_from_the_reader_passes_through(self):
        from apps.accounts.errors import AuthorizationError

        with mock.patch.object(queries.review, "list_audit_events", side_effect=AuthorizationError("missing_permission")), self.assertRaises(AuthorizationError):
            queries.list_run_audit(user="u", run_id=RUN_ID)

    def test_a_run_with_no_events_and_no_row_is_not_found_but_an_existing_empty_run_is_an_empty_page(self):
        with mock.patch.object(queries.review, "list_audit_events", return_value=[]), mock.patch.object(queries, "WorkflowRun") as run:
            run.objects.filter.return_value.exists.return_value = False
            with self.assertRaises(RecordNotFoundError):
                queries.list_run_audit(user="u", run_id=RUN_ID)
            run.objects.filter.return_value.exists.return_value = True
            self.assertEqual(queries.list_run_audit(user="u", run_id=RUN_ID).items, ())

    def test_bad_paging_is_refused_before_the_reader_is_called(self):
        with mock.patch.object(queries.review, "list_audit_events") as reader:
            for kwargs in ({"page": 0}, {"page_size": 101}, {"page_size": 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    queries.list_run_audit(user="u", run_id=RUN_ID, **kwargs)
        reader.assert_not_called()


RUN_ID = uuid.uuid4()


class QueryCompilationTests(SimpleTestCase):
    """The new querysets must compile to valid PostgreSQL SQL (bad field paths fail here, without any connection).

    This does NOT execute anything and proves nothing about results; the PostgreSQL tests do that.
    """

    def sql(self, queryset):
        return str(queryset.query)

    def test_the_list_query_for_an_own_scope_user_filters_on_the_submitter_and_orders_stably(self):
        sql = self.sql(queries._visible_runs(7, False, None).select_related(*queries.LIST_RELATED))
        self.assertIn('"intake_requestsource"."submitted_by_id" = 7', sql)
        self.assertIn("ORDER BY", sql)
        self.assertLess(sql.index('"created_at" DESC'), sql.index('"id" DESC'))
        self.assertIn('INNER JOIN "intake_requestsource"', sql)
        for table in ("cases_case", "cases_resultrevision", "accounts_user", "intake_extractionoutcome"):
            self.assertIn(f'"{table}"', sql)

    def test_the_list_query_for_a_see_all_user_has_no_submitter_filter(self):
        sql = self.sql(queries._visible_runs(7, True, None))
        self.assertNotIn("submitted_by_id", sql)
        self.assertIn('"intake_requestsource"', sql)  # still only runs that have a source

    def test_the_state_filter(self):
        self.assertIn("\"state\" = needs_review", self.sql(queries._visible_runs(1, True, "needs_review")).replace("'", ""))

    def test_the_processing_query(self):
        sql = self.sql(queries._extract_links(uuid.uuid4()))
        self.assertIn('INNER JOIN "tasks_task"', sql)
        self.assertIn('"generation" DESC', sql)
        self.assertIn("extract", sql)

    def test_the_author_and_decision_lookups(self):
        from apps.cases.models import Approval, Rejection
        from apps.intake.models import RevisionEdit

        self.assertIn("run_id", self.sql(RevisionEdit.objects.filter(revision__run_id=uuid.uuid4()).values_list("edited_by_id", flat=True)))
        self.assertIn("revision_id", self.sql(Rejection.objects.filter(revision_id=uuid.uuid4())))
        self.assertIn("revision_id", self.sql(Approval.objects.filter(revision_id=uuid.uuid4())))
