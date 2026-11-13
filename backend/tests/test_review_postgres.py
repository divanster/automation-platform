"""Tests for the review services that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: written on a development VM that has no PostgreSQL and no connection to one; the project owner ran
them locally against PostgreSQL and reported that all of them passed (owner-supplied; see
docs/development/review-services-slice.md, section 10.4). That covers the tested scenarios only: it does not
prove that no other race exists. This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_review_postgres --tag requires_postgres -v 2

What needs a real server and is therefore here and not in the DB-free tests: the user migration applied to
existing users, constraints, transactions and rollback, row locking, and competing connections.
"""

import dataclasses
import io
import os
import unittest
import uuid
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, TransactionTestCase, tag

from apps.accounts.errors import AuthorizationError
from apps.accounts.roles import APPROVER, AUDITOR, OPERATOR, ROLE_PERMISSIONS, ROLES, split_permission
from apps.audit import events as audit_events
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditTransactionError, record_event
from apps.cases import services as case_services
from apps.cases.errors import ImmutableRecordError, RecordNotFoundError
from apps.cases.models import Approval, Case, Rejection, ResultRevision, WorkflowRun
from apps.cases.run_state import STATES
from apps.intake import review, services
from apps.intake.draft_json import draft_from_json
from apps.intake.errors import (
    DraftInvalidError,
    NoChangeError,
    ReviewRefusedError,
    SelfApprovalError,
    StaleRevisionError,
    WarningsNotAcknowledgedError,
)
from apps.intake.models import ExtractionOutcome, RequestSource, RevisionEdit
from apps.intake.policy import REJECTION_REASONS
from apps.intake.submission import submit_request, submit_request_as
from apps.tasks.models import Task
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import INVALID_TEXT, DedicatedTestDatabaseMixin, run_threads
from workflows.request_intake import RequestedItem, Requester, SourceRef, StructuredRequest, make_excerpt

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

User = get_user_model()


def make_user(username, *roles, superuser=False, active=True):
    user = User.objects.create_user(username=username)
    if superuser or not active:
        User.objects.filter(pk=user.pk).update(is_superuser=superuser, is_staff=superuser, is_active=active)
        user.refresh_from_db()
    for role in roles:
        user.groups.add(Group.objects.get(name=role))
    return user


def setup_roles():
    call_command("setup_roles", stdout=io.StringIO())


class ReviewCase(DedicatedTestDatabaseMixin, TestCase):
    """Roles set up, one user per kind, and helpers that build requests in each state."""

    def setUp(self):
        setup_roles()
        self.operator = make_user("olive", OPERATOR)
        self.other_operator = make_user("omar", OPERATOR)
        self.approver = make_user("anna", APPROVER)
        self.second_approver = make_user("amir", APPROVER)
        self.auditor = make_user("audrey", AUDITOR)
        self.superuser = make_user("root", superuser=True)
        self.nobody = make_user("nina")
        self.hybrid = make_user("hugo", OPERATOR, APPROVER)  # may submit/edit AND approve: self-approval must still fail

    # --- builders ---------------------------------------------------------------------------------

    def new_run(self, owner=None, text=VALID_TEXT, extract=True):
        result = submit_request(text=text, submitted_by=owner or self.operator)
        if extract:
            services.run_extraction(result.run.pk)
        return WorkflowRun.objects.get(pk=result.run.pk)

    def draft_of(self, run):
        return draft_from_json(ExtractionOutcome.objects.get(run_id=run.pk).draft)

    def variant(self, run, marker):
        return dataclasses.replace(self.draft_of(run), notes=f"Edit {marker}")

    def summary(self, run):
        return review.get_review_summary(user=self.auditor, run_id=run.pk)

    def edit(self, user, run, marker="1", base="current", draft=None):
        base_id = self.summary(run).current_revision_id if base == "current" else base
        return review.edit_request(user=user, run_id=run.pk, base_revision_id=base_id, draft=draft or self.variant(run, marker))

    def approve(self, user, run, **overrides):
        summary = self.summary(run)
        arguments = {
            "user": user,
            "run_id": run.pk,
            "revision_id": summary.current_revision_id,
            "content_hash": summary.content_hash,
            "acknowledged_warnings": summary.warnings,
        }
        arguments.update(overrides)
        return review.approve_request(**arguments)

    def reject(self, user, run, reason="incorrect_content", **overrides):
        summary = self.summary(run)
        arguments = {"user": user, "run_id": run.pk, "revision_id": summary.current_revision_id, "content_hash": summary.content_hash, "reason_code": reason}
        arguments.update(overrides)
        return review.reject_request(**arguments)

    def state(self, run):
        return WorkflowRun.objects.get(pk=run.pk).state

    def snapshot(self, run=None):
        """Everything a refused operation must leave exactly as it was."""
        data = {
            "revisions": ResultRevision.objects.count(),
            "edits": RevisionEdit.objects.count(),
            "approvals": Approval.objects.count(),
            "rejections": Rejection.objects.count(),
            "audit": AuditEvent.objects.count(),
        }
        if run is not None:
            row = WorkflowRun.objects.get(pk=run.pk)
            data["run"] = (row.state, row.failed_step, row.current_revision_id)
        return data

    def assertRefused(self, error_type, reason, run, call):
        before = self.snapshot(run)
        with self.assertRaises(error_type) as caught:
            call()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.snapshot(run), before, "a refused operation must write nothing")
        return caught.exception

    def actions(self, run):
        return [event.action for event in AuditEvent.objects.filter(run_id=run.pk).order_by("id")]


class FailOn:
    """A ``record_event`` stand-in that fails on its Nth call and otherwise really records."""

    def __init__(self, nth):
        self.nth, self.calls, self.real = nth, 0, record_event

    def __call__(self, event):
        self.calls += 1
        if self.calls == self.nth:
            raise RuntimeError("simulated audit write failure")
        return self.real(event)


# --- schema, permissions, roles --------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def constraints(self, model):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(cursor, model._meta.db_table)

    def test_the_database_has_every_named_constraint(self):
        expected = {
            Rejection: {"cases_rejection_hash_is_sha256_hex", "cases_rejection_reason_code_shape"},
            RevisionEdit: {"intake_edit_base_is_another_revision"},
            AuditEvent: {"audit_event_action_valid", "audit_event_subject_type_valid", "audit_event_actor_kind_valid", "audit_event_actor_matches_kind"},
        }
        for model, names in expected.items():
            with self.subTest(model=model.__name__):
                self.assertLessEqual(names, set(self.constraints(model)))

    def test_public_id_is_unique_and_not_null_in_the_database(self):
        constraints = self.constraints(User)
        self.assertTrue(any(c["unique"] and c["columns"] == ["public_id"] for c in constraints.values()))
        with connection.cursor() as cursor:
            columns = {c.name: c for c in connection.introspection.get_table_description(cursor, User._meta.db_table)}
        self.assertFalse(columns["public_id"].null_ok)

    def test_no_unmanaged_model_without_a_table_exists_and_dumpdata_works(self):
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
        for model in apps.get_models():
            if model._meta.managed and not model._meta.proxy:
                self.assertIn(model._meta.db_table, tables, model.__name__)
        out = io.StringIO()
        call_command("dumpdata", "intake", "audit", "accounts", stdout=out)  # fails on a model without a table
        self.assertTrue(out.getvalue().startswith("["))

    def test_the_request_permissions_belong_to_request_source_and_its_default_permissions_are_off(self):
        codenames = set(Permission.objects.filter(content_type__app_label="intake", content_type__model="requestsource").values_list("codename", flat=True))
        self.assertEqual(
            {c for c in codenames if c.endswith("_request")},
            {"submit_request", "view_own_request", "edit_own_request", "view_any_request", "approve_request", "reject_request"},
        )
        self.assertFalse({"add_requestsource", "change_requestsource", "delete_requestsource", "view_requestsource"} & codenames)

    def test_migrate_created_every_permission_the_roles_need(self):
        for roles in ROLE_PERMISSIONS.values():
            for name in roles:
                app_label, codename = split_permission(name)
                with self.subTest(permission=name):
                    self.assertTrue(Permission.objects.filter(content_type__app_label=app_label, codename=codename).exists())

    def test_audit_events_have_a_view_permission_and_no_other(self):
        codenames = set(Permission.objects.filter(content_type__app_label="audit").values_list("codename", flat=True))
        self.assertEqual(codenames, {"view_auditevent"})

    def test_the_database_refuses_malformed_rows(self):
        owner = make_user("owner")
        result = submit_request(text=VALID_TEXT, submitted_by=owner)
        services.run_extraction(result.run.pk)
        revision = ResultRevision.objects.get(run=result.run)
        bad_rows = {
            "rejection hash": lambda: Rejection.objects.create(revision=revision, content_hash="x", rejected_by=uuid.uuid4(), reason_code="other"),
            "rejection reason shape": lambda: Rejection.objects.create(revision=revision, content_hash="a" * 64, rejected_by=uuid.uuid4(), reason_code="Please fix it"),
            "audit action": lambda: AuditEvent.objects.create(actor_kind="system", action="nope", subject_type="run", subject_id=uuid.uuid4()),
            "audit subject": lambda: AuditEvent.objects.create(actor_kind="system", action="run.state_changed", subject_type="x", subject_id=uuid.uuid4()),
            "audit user without id": lambda: AuditEvent.objects.create(actor_kind="user", action="run.state_changed", subject_type="run", subject_id=uuid.uuid4()),
            "audit system with id": lambda: AuditEvent.objects.create(
                actor_kind="system", actor_public_id=uuid.uuid4(), action="run.state_changed", subject_type="run", subject_id=uuid.uuid4()
            ),
            "edit base is itself": lambda: RevisionEdit.objects.create(revision=revision, base_revision=revision, edited_by=owner),
        }
        for label, create in bad_rows.items():
            with self.subTest(row=label), self.assertRaises(IntegrityError), transaction.atomic():
                create()


@tag("requires_postgres")
@requires_postgres
class RolesCommandTests(DedicatedTestDatabaseMixin, TestCase):
    def permissions_of(self, role):
        return {f"{p.content_type.app_label}.{p.codename}" for p in Group.objects.get(name=role).permissions.select_related("content_type")}

    def test_it_creates_each_role_with_exactly_its_permissions(self):
        setup_roles()
        self.assertEqual(set(Group.objects.values_list("name", flat=True)), set(ROLES))
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(self.permissions_of(role), set(ROLE_PERMISSIONS[role]))

    def test_running_it_again_changes_nothing(self):
        setup_roles()
        before = {role: (Group.objects.get(name=role).pk, self.permissions_of(role)) for role in ROLES}
        setup_roles()
        setup_roles()
        self.assertEqual({role: (Group.objects.get(name=role).pk, self.permissions_of(role)) for role in ROLES}, before)
        self.assertEqual(Group.objects.count(), 3)

    def test_it_repairs_drift_in_both_directions(self):
        setup_roles()
        approver = Group.objects.get(name=APPROVER)
        approver.permissions.add(Permission.objects.get(codename="submit_request"))  # an extra permission
        approver.permissions.remove(Permission.objects.get(codename="approve_request"))  # a missing one
        setup_roles()
        self.assertEqual(self.permissions_of(APPROVER), set(ROLE_PERMISSIONS[APPROVER]))

    def test_it_never_touches_users_memberships_or_other_groups(self):
        setup_roles()
        member = make_user("member", OPERATOR)
        other = Group.objects.create(name="Somebody else's group")
        other.permissions.add(Permission.objects.get(codename="view_auditevent"))
        setup_roles()
        self.assertEqual(list(member.groups.values_list("name", flat=True)), [OPERATOR])
        self.assertEqual({p.codename for p in other.permissions.all()}, {"view_auditevent"})

    def test_a_missing_permission_is_a_clear_error_and_changes_nothing(self):
        setup_roles()
        before = {role: self.permissions_of(role) for role in ROLES}
        Permission.objects.filter(codename="approve_request").delete()
        with self.assertRaises(CommandError) as caught:
            setup_roles()
        self.assertIn("intake.approve_request", str(caught.exception))
        self.assertIn("migrate", str(caught.exception))
        for role in ROLES:
            self.assertLessEqual(self.permissions_of(role), before[role])

    def test_the_output_names_roles_and_counts_only(self):
        make_user("secret-username")
        out = io.StringIO()
        call_command("setup_roles", stdout=out)
        text = out.getvalue()
        self.assertIn("Operator: created, 3 permissions", text)
        self.assertNotIn("secret-username", text)

    def test_a_role_grants_what_it_says_through_django_itself(self):
        setup_roles()
        operator, approver, auditor = make_user("a", OPERATOR), make_user("b", APPROVER), make_user("c", AUDITOR)
        self.assertTrue(operator.has_perm("intake.submit_request"))
        self.assertFalse(operator.has_perm("intake.approve_request"))
        self.assertTrue(approver.has_perm("intake.approve_request"))
        self.assertFalse(approver.has_perm("intake.edit_own_request"))
        self.assertTrue(auditor.has_perm("audit.view_auditevent"))
        self.assertFalse(auditor.has_perm("intake.reject_request"))


# --- the user identity and its migration ------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class PublicIdTests(DedicatedTestDatabaseMixin, TestCase):
    def test_every_new_user_gets_a_distinct_random_public_id(self):
        users = [User.objects.create_user(username=f"user{i}") for i in range(20)]
        self.assertEqual(len({u.public_id for u in users}), 20)
        self.assertTrue(all(u.public_id.version == 4 for u in users))
        self.assertEqual(User.objects.filter(public_id__isnull=True).count(), 0)

    def test_superusers_get_one_too(self):
        user = User.objects.create_superuser(username="admin", password=uuid.uuid4().hex)
        self.assertIsInstance(user.public_id, uuid.UUID)

    def test_the_database_refuses_a_duplicate(self):
        first = make_user("first")
        second = make_user("second")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=second.pk).update(public_id=first.public_id)

    def test_the_public_id_survives_a_reload_and_a_save(self):
        user = make_user("stable")
        original = user.public_id
        user.first_name = "Changed"
        user.save()
        self.assertEqual(User.objects.get(pk=user.pk).public_id, original)

    def test_the_integer_primary_key_still_identifies_the_user(self):
        user = make_user("pk-check")
        self.assertIsInstance(user.pk, int)
        self.assertNotEqual(user.pk, user.public_id)


@tag("requires_postgres")
@requires_postgres
class UserMigrationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Applies accounts 0002..0004 to a database that already holds users, as the owner's local one does.

    The test database is migrated back to accounts 0001, users are inserted the old way (no public_id), and
    the migrations are applied one step at a time. It is returned to the latest migrations afterwards.
    """

    STEP_1, STEP_2, STEP_3 = ("accounts", "0002_user_public_id"), ("accounts", "0003_backfill_user_public_id"), ("accounts", "0004_user_public_id_constraints")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return MigrationExecutor(connection)

    def setUp(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def old_users(self, state_key, names):
        executor = MigrationExecutor(connection)
        model = executor.loader.project_state([state_key]).apps.get_model("accounts", "User")
        return model, [model.objects.create(username=name, password="") for name in names]

    def columns(self):
        with connection.cursor() as cursor:
            return {c.name: c for c in connection.introspection.get_table_description(cursor, User._meta.db_table)}

    def test_existing_users_each_get_a_distinct_public_id_and_nothing_else_changes(self):
        self.migrate(("accounts", "0001_initial"))
        self.assertNotIn("public_id", self.columns())
        model, created = self.old_users(("accounts", "0001_initial"), [f"old{i}" for i in range(12)])
        before = {u.pk: (u.username, u.password, u.is_active) for u in created}

        self.migrate(self.STEP_3)

        users = list(User.objects.order_by("pk"))
        self.assertEqual(len(users), 12)  # nobody was deleted
        self.assertEqual({u.pk: (u.username, u.password, u.is_active) for u in users}, before)  # nothing else changed
        self.assertEqual(len({u.public_id for u in users}), 12)
        self.assertTrue(all(isinstance(u.public_id, uuid.UUID) for u in users))
        self.assertFalse(self.columns()["public_id"].null_ok)

    def test_step_by_step(self):
        self.migrate(("accounts", "0001_initial"))
        self.old_users(("accounts", "0001_initial"), ["a", "b", "c"])

        self.migrate(self.STEP_1)
        self.assertTrue(self.columns()["public_id"].null_ok)
        self.assertEqual(User.objects.filter(public_id__isnull=True).count(), 3)

        self.migrate(self.STEP_2)
        self.assertEqual(User.objects.filter(public_id__isnull=True).count(), 0)
        values = set(User.objects.values_list("public_id", flat=True))
        self.assertEqual(len(values), 3)

        self.migrate(self.STEP_3)
        self.assertFalse(self.columns()["public_id"].null_ok)
        self.assertEqual(set(User.objects.values_list("public_id", flat=True)), values)  # step 3 keeps what step 2 wrote

    def test_the_backfill_keeps_an_existing_value_and_can_be_run_again(self):
        self.migrate(("accounts", "0001_initial"))
        self.migrate(self.STEP_1)
        model, created = self.old_users(self.STEP_1, ["kept", "new"])
        keep = uuid.uuid4()
        model.objects.filter(pk=created[0].pk).update(public_id=keep)
        self.migrate(self.STEP_2)
        self.assertEqual(User.objects.get(username="kept").public_id, keep)
        first = User.objects.get(username="new").public_id
        self.assertIsNotNone(first)
        self.migrate(self.STEP_1)  # backwards over the no-op step, then forwards again
        self.migrate(self.STEP_2)
        self.assertEqual(User.objects.get(username="kept").public_id, keep)

    def test_step_3_refuses_duplicates_instead_of_accepting_them_and_leaves_the_database_at_step_2(self):
        self.migrate(("accounts", "0001_initial"))
        self.migrate(self.STEP_2)
        executor = MigrationExecutor(connection)
        model = executor.loader.project_state([self.STEP_2]).apps.get_model("accounts", "User")
        model.objects.create(username="d1", password="")
        model.objects.create(username="d2", password="")
        same = uuid.uuid4()
        model.objects.update(public_id=same)  # the situation the unique constraint exists for
        with self.assertRaises(IntegrityError):
            self.migrate(self.STEP_3)
        applied = MigrationRecorder(connection).applied_migrations()
        self.assertNotIn(self.STEP_3, applied)
        self.assertIn(self.STEP_2, applied)
        for number, user in enumerate(model.objects.order_by("pk")):  # repair so the cleanup can migrate forward
            model.objects.filter(pk=user.pk).update(public_id=uuid.uuid4())

    def test_a_user_without_any_history_migrates_forward_on_an_empty_table(self):
        self.migrate(("accounts", "0001_initial"))
        self.assertEqual(User.objects.count(), 0)
        self.migrate(self.STEP_3)
        self.assertFalse(self.columns()["public_id"].null_ok)
        self.assertEqual(make_user("after").public_id.version, 4)

    def test_the_migrations_can_be_reversed_to_the_original_schema(self):
        self.migrate(("accounts", "0001_initial"))
        self.assertNotIn("public_id", self.columns())
        self.migrate(self.STEP_3)
        self.assertIn("public_id", self.columns())


# --- authentication and authorization inside the services -----------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AuthorizationTests(ReviewCase):
    def test_an_unauthenticated_caller_is_refused_everywhere_and_writes_nothing(self):
        run = self.new_run()
        revision = self.summary(run)
        callers = {
            "anonymous": AnonymousUser(),
            "unsaved user": User(username="ghost"),
            "none": None,
            "a string": "olive",
        }
        calls = {
            "edit": lambda u: review.edit_request(user=u, run_id=run.pk, base_revision_id=revision.current_revision_id, draft=self.variant(run, "x")),
            "approve": lambda u: review.approve_request(user=u, run_id=run.pk, revision_id=revision.current_revision_id, content_hash=revision.content_hash),
            "reject": lambda u: review.reject_request(user=u, run_id=run.pk, revision_id=revision.current_revision_id, content_hash=revision.content_hash, reason_code="other"),
            "view": lambda u: review.get_review_summary(user=u, run_id=run.pk),
            "audit": lambda u: review.list_audit_events(user=u, run_id=run.pk),
            "submit": lambda u: submit_request_as(user=u, text=VALID_TEXT),
        }
        for who, user in callers.items():
            for name, call in calls.items():
                with self.subTest(caller=who, call=name):
                    self.assertRefused(AuthorizationError, "not_authenticated", run, lambda: call(user))
        self.assertEqual(RequestSource.objects.count(), 1)  # the one the test itself created

    def test_a_deleted_user_object_is_not_authenticated(self):
        ghost = make_user("soon-gone", OPERATOR)
        pk = ghost.pk
        User.objects.filter(pk=pk).delete()
        run = self.new_run()
        self.assertRefused(AuthorizationError, "not_authenticated", run, lambda: self.edit(ghost, run))

    def test_an_inactive_user_is_refused_even_with_every_role_and_even_as_superuser(self):
        run = self.new_run()
        for name, roles, superuser in (("dormant", (OPERATOR, APPROVER, AUDITOR), False), ("dormant-root", (), True)):
            user = make_user(name, *roles, superuser=superuser, active=False)
            with self.subTest(user=name):
                self.assertRefused(AuthorizationError, "inactive", run, lambda: self.edit(user, run))
                self.assertRefused(AuthorizationError, "inactive", run, lambda: self.approve(user, run))
                self.assertRefused(AuthorizationError, "inactive", run, lambda: self.reject(user, run))

    def test_a_user_deactivated_after_the_object_was_loaded_is_judged_by_the_database(self):
        run = self.new_run()
        stale = User.objects.get(pk=self.operator.pk)  # loaded while active
        User.objects.filter(pk=stale.pk).update(is_active=False)
        self.assertTrue(stale.is_active)  # the stale object still says so
        self.assertRefused(AuthorizationError, "inactive", run, lambda: self.edit(stale, run))

    def test_a_role_removed_after_the_object_was_loaded_is_honoured_despite_cached_permissions(self):
        run = self.new_run()
        stale = User.objects.get(pk=self.operator.pk)
        self.assertTrue(stale.has_perm("intake.edit_own_request"))  # fills Django's per-object permission cache
        stale.groups.clear()
        self.assertTrue(stale.has_perm("intake.edit_own_request"))  # still cached on the stale object
        self.assertRefused(AuthorizationError, "missing_permission", run, lambda: self.edit(stale, run))

    def test_a_demoted_superuser_loses_the_bypass_immediately(self):
        run = self.new_run()
        stale = User.objects.get(pk=self.superuser.pk)
        User.objects.filter(pk=stale.pk).update(is_superuser=False)
        self.assertRefused(AuthorizationError, "missing_permission", run, lambda: self.edit(stale, run))

    def test_a_refusal_is_logged_with_the_reason_and_public_id_but_no_personal_data(self):
        run = self.new_run()
        with self.assertLogs("apps.accounts.authz", level="WARNING") as logs:
            with self.assertRaises(AuthorizationError):
                self.edit(self.auditor, run)
        text = "\n".join(logs.output)
        self.assertIn("reason=missing_permission", text)
        self.assertIn("action=edit", text)
        self.assertIn(str(self.auditor.public_id), text)
        for secret in (self.auditor.username, "audrey", "password", "@"):
            self.assertNotIn(secret, text)

    def test_role_by_action_matrix_against_a_real_request(self):
        """Each user kind against each service, on a request the operator submitted (a fresh one per attempt)."""

        def attempt(user, action):
            run = self.new_run()
            summary = self.summary(run)
            calls = {
                "view": lambda: review.get_review_summary(user=user, run_id=run.pk),
                "edit": lambda: review.edit_request(user=user, run_id=run.pk, base_revision_id=summary.current_revision_id, draft=self.variant(run, "m")),
                "reject": lambda: review.reject_request(user=user, run_id=run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, reason_code="other"),
                "approve": lambda: review.approve_request(user=user, run_id=run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, acknowledged_warnings=summary.warnings),
            }
            try:
                calls[action]()
                return "allowed"
            except AuthorizationError as error:
                return error.reason

        users = {
            "operator-owner": self.operator,
            "operator-other": self.other_operator,
            "approver": self.approver,
            "auditor": self.auditor,
            "nobody": self.nobody,
            "superuser": self.superuser,
        }
        expected = {
            "operator-owner": {"view": "allowed", "edit": "allowed", "reject": "missing_permission", "approve": "missing_permission"},
            "operator-other": {"view": "out_of_scope", "edit": "out_of_scope", "reject": "missing_permission", "approve": "missing_permission"},
            "approver": {"view": "allowed", "edit": "missing_permission", "reject": "allowed", "approve": "allowed"},
            "auditor": {"view": "allowed", "edit": "missing_permission", "reject": "missing_permission", "approve": "missing_permission"},
            "nobody": {"view": "missing_permission", "edit": "missing_permission", "reject": "missing_permission", "approve": "missing_permission"},
            "superuser": {"view": "allowed", "edit": "allowed", "reject": "allowed", "approve": "allowed"},
        }
        actual = {label: {action: attempt(user, action) for action in ("view", "edit", "reject", "approve")} for label, user in users.items()}
        self.assertEqual(actual, expected)


# --- submit -------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SubmitAsTests(ReviewCase):
    def counts(self):
        return (Case.objects.count(), WorkflowRun.objects.count(), RequestSource.objects.count(), Task.objects.count(), AuditEvent.objects.count())

    def test_an_operator_submits_and_the_audit_event_commits_with_it(self):
        result = submit_request_as(user=self.operator, text=VALID_TEXT)
        self.assertEqual(result.source.submitted_by_id, self.operator.pk)
        (event,) = AuditEvent.objects.filter(run_id=result.run.pk)
        self.assertEqual((event.action, event.actor_kind, event.actor_public_id), ("request.submitted", "user", self.operator.public_id))
        self.assertEqual((event.subject_type, event.subject_id, event.case_id), ("run", result.run.pk, result.case.pk))
        self.assertEqual(event.details, {"byte_length": len(VALID_TEXT.encode()), "client_request_id_used": False})

    def test_roles_without_the_submit_permission_are_refused_and_store_nothing(self):
        before = self.counts()
        for user in (self.approver, self.auditor, self.nobody):
            with self.subTest(user=user.username):
                with self.assertRaises(AuthorizationError) as caught:
                    submit_request_as(user=user, text=VALID_TEXT)
                self.assertEqual(caught.exception.reason, "missing_permission")
        self.assertEqual(self.counts(), before)

    def test_a_superuser_may_submit(self):
        self.assertTrue(submit_request_as(user=self.superuser, text=VALID_TEXT).created)

    def test_the_submitter_recorded_is_the_loaded_user(self):
        stale = User.objects.get(pk=self.operator.pk)
        stale.username = "someone-else-in-memory"
        result = submit_request_as(user=stale, text=VALID_TEXT)
        self.assertEqual(User.objects.get(pk=result.source.submitted_by_id).username, "olive")

    def test_a_replayed_submit_adds_no_second_audit_event(self):
        client_id = uuid.uuid4()
        first = submit_request_as(user=self.operator, text=VALID_TEXT, client_request_id=client_id)
        again = submit_request_as(user=self.operator, text=VALID_TEXT, client_request_id=client_id)
        self.assertEqual((first.created, again.created, first.run.pk == again.run.pk), (True, False, True))
        self.assertEqual(AuditEvent.objects.filter(run_id=first.run.pk).count(), 1)
        self.assertTrue(AuditEvent.objects.get(run_id=first.run.pk).details["client_request_id_used"])

    def test_an_audit_failure_rolls_the_whole_submission_back(self):
        before = self.counts()
        with mock.patch("apps.intake.submission.record_event", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                submit_request_as(user=self.operator, text=VALID_TEXT)
        self.assertEqual(self.counts(), before)

    def test_an_invalid_text_is_refused_after_authorization_and_stores_nothing(self):
        before = self.counts()
        with self.assertRaises(Exception):
            submit_request_as(user=self.operator, text="   ")
        self.assertEqual(self.counts(), before)


# --- edit -------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class EditServiceTests(ReviewCase):
    def test_an_edit_creates_the_next_immutable_revision_and_keeps_the_previous_one(self):
        run = self.new_run()
        first = ResultRevision.objects.get(run=run)
        first_bytes, first_hash = bytes(first.canonical_bytes), first.content_hash

        result = self.edit(self.operator, run, "A")

        self.assertEqual((result.revision_number, result.run_state, result.approval_invalidated), (2, "needs_review", False))
        second = ResultRevision.objects.get(pk=result.revision_id)
        self.assertEqual((second.run_id, second.revision_number, second.content_hash), (run.pk, 2, result.content_hash))
        self.assertNotEqual(second.content_hash, first_hash)
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).current_revision_id, second.pk)
        again = ResultRevision.objects.get(pk=first.pk)  # revision 1 is exactly as it was
        self.assertEqual((bytes(again.canonical_bytes), again.content_hash, again.revision_number), (first_bytes, first_hash, 1))
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 2)

    def test_the_new_revision_is_the_domains_canonical_form_of_the_draft(self):
        run = self.new_run()
        draft = self.variant(run, "canon")
        result = self.edit(self.operator, run, draft=draft)
        from workflows.request_intake import canonicalize_request

        expected = canonicalize_request(draft, source_text=RequestSource.objects.get(run=run).text)
        row = ResultRevision.objects.get(pk=result.revision_id)
        self.assertEqual((bytes(row.canonical_bytes), row.content_hash), (expected.canonical_bytes, expected.content_hash))

    def test_the_editor_and_the_base_are_recorded(self):
        run = self.new_run()
        base = self.summary(run).current_revision_id
        result = self.edit(self.operator, run, "who")
        record = RevisionEdit.objects.get(revision_id=result.revision_id)
        self.assertEqual((record.edited_by_id, record.base_revision_id), (self.operator.pk, base))

    def test_warnings_are_stored_on_the_revision_and_returned(self):
        run = self.new_run()
        result = self.edit(self.operator, run, "warn")
        self.assertIn(("value_not_in_source", "notes"), result.warnings)  # the new note is not in the source text
        stored = case_services.load_revision(result.revision_id)
        self.assertEqual({(w.code, w.field_path) for w in stored.warnings}, set(result.warnings))
        self.assertFalse(stored.blocking_findings)

    def test_a_duplicate_item_edit_keeps_its_warning(self):
        run = self.new_run()
        draft = self.draft_of(run)
        twice = dataclasses.replace(draft, items=(draft.items[0], draft.items[0]))
        result = self.edit(self.operator, run, draft=twice)
        self.assertIn(("duplicate_item", "items[1].description"), result.warnings)

    def test_source_references_are_verified_against_the_stored_source(self):
        run = self.new_run()
        draft = self.draft_of(run)
        line = RequestSource.objects.get(run=run).text.splitlines()[0]
        good = dataclasses.replace(draft, source_refs=(SourceRef(field_path="requester.name", excerpt=make_excerpt(line), line=1),), notes=None)
        self.assertEqual(self.edit(self.operator, run, draft=good).revision_number, 2)
        invented = dataclasses.replace(draft, source_refs=(SourceRef(field_path="requester.name", excerpt="not a line of the source", line=None),), notes="x")
        error = self.assertRefused(DraftInvalidError, "blocking_findings", run, lambda: self.edit(self.operator, run, draft=invented))
        self.assertIn("invalid_source_ref", {f.code for f in error.findings})

    def test_an_invalid_draft_is_refused_with_its_findings_and_never_becomes_a_revision(self):
        run = self.new_run()
        bad = dataclasses.replace(self.draft_of(run), requester=Requester(name=None), items=(RequestedItem(description="Pens", quantity=0),))
        error = self.assertRefused(DraftInvalidError, "blocking_findings", run, lambda: self.edit(self.operator, run, draft=bad))
        self.assertIn(("missing_required", "requester.name"), {(f.code, f.field_path) for f in error.findings})
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 1)

    def test_a_draft_that_cannot_be_serialized_is_refused(self):
        from decimal import Decimal

        run = self.new_run()
        huge = dataclasses.replace(self.draft_of(run), source_refs=(), items=(RequestedItem(description="A4 paper", quantity=Decimal("1E+1000000")),))
        self.assertRefused(DraftInvalidError, "not_serializable", run, lambda: self.edit(self.operator, run, draft=huge))

    def test_an_unchanged_draft_is_refused_and_creates_no_revision(self):
        run = self.new_run()
        self.edit(self.operator, run, "same")
        current = case_services.get_current_revision(run.pk)
        again = self.variant(run, "same")
        self.assertRefused(NoChangeError, "unchanged_content", run, lambda: self.edit(self.operator, run, draft=again))
        self.assertEqual(case_services.get_current_revision(run.pk).revision_id, current.revision_id)

    def test_a_stale_base_is_refused_and_nothing_is_written(self):
        run = self.new_run()
        old = self.summary(run).current_revision_id
        self.edit(self.operator, run, "first")
        error = self.assertRefused(StaleRevisionError, "stale_base_revision", run, lambda: self.edit(self.operator, run, "second", base=old))
        self.assertIsInstance(error, StaleRevisionError)
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 2)

    def test_a_base_of_none_is_stale_once_a_revision_exists(self):
        run = self.new_run()
        self.assertRefused(StaleRevisionError, "stale_base_revision", run, lambda: self.edit(self.operator, run, base=None))

    def test_a_base_from_another_run_is_stale(self):
        run, other = self.new_run(), self.new_run()
        foreign = self.summary(other).current_revision_id
        self.assertRefused(StaleRevisionError, "stale_base_revision", run, lambda: self.edit(self.operator, run, base=foreign))

    def test_an_unknown_run_is_not_found(self):
        with self.assertRaises(RecordNotFoundError):
            review.edit_request(user=self.operator, run_id=uuid.uuid4(), base_revision_id=None, draft=self.draft_of(self.new_run()))

    def test_only_needs_review_and_approved_runs_can_be_edited(self):
        run = self.new_run()
        for state in STATES:
            if state in ("needs_review", "approved"):
                continue
            failed_step = "extract" if state == "failed" else ""
            WorkflowRun.objects.filter(pk=run.pk).update(state=state, failed_step=failed_step)
            with self.subTest(state=state):
                self.assertRefused(ReviewRefusedError, "run_state_not_editable", run, lambda: self.edit(self.operator, run, "s", base=self.summary(run).current_revision_id))

    def test_editing_an_approved_run_returns_it_to_review_and_keeps_the_approval(self):
        run = self.new_run()
        self.approve(self.approver, run)
        approved = Approval.objects.get(revision__run=run)
        approved_hash = approved.content_hash
        self.assertEqual(self.state(run), "approved")

        result = self.edit(self.operator, run, "after-approval")

        self.assertEqual((result.run_state, result.approval_invalidated), ("needs_review", True))
        self.assertEqual(self.state(run), "needs_review")
        kept = Approval.objects.get(pk=approved.pk)
        self.assertEqual((kept.content_hash, kept.revision_id), (approved_hash, approved.revision_id))
        self.assertEqual(Approval.objects.filter(revision_id=result.revision_id).count(), 0)  # the new revision has no approval

    def test_an_old_approval_does_not_authorize_the_new_revision(self):
        run = self.new_run()
        self.approve(self.approver, run)
        approval = Approval.objects.get(revision__run=run)
        old_revision = approval.revision_id
        self.edit(self.operator, run, "newer")
        for requested in (old_revision, self.summary(run).current_revision_id):
            with self.subTest(requested=str(requested)[:8]):
                decision = case_services.check_persisted_export_eligibility(
                    expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=requested, approval_id=approval.pk
                )
                self.assertFalse(decision.allowed)

    def test_the_first_edit_of_an_invalid_extraction_creates_revision_1(self):
        run = self.new_run(text=INVALID_TEXT)
        outcome = ExtractionOutcome.objects.get(run=run)
        self.assertEqual((outcome.status, outcome.revision_id, run.current_revision_id), ("invalid", None, None))
        self.assertEqual(self.state(run), "needs_review")
        summary = self.summary(run)
        self.assertEqual((summary.current_revision_id, summary.outcome_status), (None, "invalid"))
        self.assertTrue(any(f.blocking for f in summary.findings))

        corrected = StructuredRequest(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="Pens", quantity=5),))
        result = review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=None, draft=corrected)

        self.assertEqual((result.revision_number, result.run_state, result.approval_invalidated), (1, "needs_review", False))
        row = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual((row.current_revision_id, row.state), (result.revision_id, "needs_review"))
        edit = RevisionEdit.objects.get(revision_id=result.revision_id)
        self.assertEqual((edit.base_revision_id, edit.edited_by_id), (None, self.operator.pk))
        same_outcome = ExtractionOutcome.objects.get(pk=outcome.pk)  # the extraction outcome is immutable history
        self.assertEqual((same_outcome.status, same_outcome.revision_id), ("invalid", None))
        first_edit_event = AuditEvent.objects.get(run_id=run.pk, action="request.edited")
        self.assertEqual(first_edit_event.details, {"base_revision_id": None, "first_edit": True})

    def test_the_first_edit_still_refuses_an_invalid_draft(self):
        run = self.new_run(text=INVALID_TEXT)
        still_bad = StructuredRequest(requester=Requester(name=None), items=())
        self.assertRefused(DraftInvalidError, "blocking_findings", run, lambda: review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=None, draft=still_bad))
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 0)

    def test_the_first_edit_cannot_name_a_base(self):
        run = self.new_run(text=INVALID_TEXT)
        foreign = self.summary(self.new_run()).current_revision_id
        corrected = StructuredRequest(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="Pens", quantity=5),))
        self.assertRefused(StaleRevisionError, "stale_base_revision", run, lambda: review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=foreign, draft=corrected))

    def test_a_run_that_was_never_extracted_cannot_be_edited(self):
        run = self.new_run(extract=False)  # state queued
        self.assertRefused(ReviewRefusedError, "run_state_not_editable", run, lambda: review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=None, draft=StructuredRequest()))

    def test_a_revision_after_an_invalid_first_edit_can_be_approved_by_someone_else(self):
        run = self.new_run(text=INVALID_TEXT)
        corrected = StructuredRequest(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="Pens", quantity=5),))
        review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=None, draft=corrected)
        self.assertEqual(self.approve(self.approver, run).run_state, "approved")

    def test_a_superuser_may_edit_somebody_elses_request(self):
        run = self.new_run()
        result = self.edit(self.superuser, run, "by-root")
        self.assertEqual(RevisionEdit.objects.get(revision_id=result.revision_id).edited_by_id, self.superuser.pk)

    def test_an_audit_failure_rolls_the_edit_back(self):
        for nth in (1, 2):
            run = self.new_run()
            before = self.snapshot(run)
            with mock.patch("apps.intake.review.record_event", FailOn(nth)), self.assertRaises(RuntimeError):
                self.edit(self.operator, run, f"audit-{nth}")
            self.assertEqual(self.snapshot(run), before, f"failure of event {nth}")

    def test_an_audit_failure_while_editing_an_approved_run_keeps_the_run_approved(self):
        run = self.new_run()
        self.approve(self.approver, run)
        for nth in (1, 2, 3, 4):
            before = self.snapshot(run)
            with mock.patch("apps.intake.review.record_event", FailOn(nth)), self.assertRaises(RuntimeError):
                self.edit(self.operator, run, f"approved-audit-{nth}")
            self.assertEqual(self.snapshot(run), before, f"failure of event {nth}")
            self.assertEqual(self.state(run), "approved")

    def test_a_failed_state_transition_rolls_the_new_revision_back(self):
        run = self.new_run()
        self.approve(self.approver, run)
        before = self.snapshot(run)
        with mock.patch("apps.cases.services.transition_run", side_effect=RuntimeError("transition failed")), self.assertRaises(RuntimeError):
            self.edit(self.operator, run, "transition")
        self.assertEqual(self.snapshot(run), before)

    def test_audit_events_of_an_edit(self):
        run = self.new_run()
        before = self.actions(run)
        result = self.edit(self.operator, run, "events")
        new = AuditEvent.objects.filter(run_id=run.pk).order_by("id")[len(before):]
        self.assertEqual([e.action for e in new], ["revision.created", "request.edited"])
        created, edited = new
        self.assertEqual((created.actor_public_id, created.subject_id), (self.operator.public_id, result.revision_id))
        self.assertEqual(created.details["origin"], "edit")
        self.assertEqual(created.details["revision_number"], 2)
        self.assertEqual(created.details["content_hash"], result.content_hash)
        self.assertEqual(edited.details["first_edit"], False)

    def test_audit_events_of_an_edit_after_approval(self):
        run = self.new_run()
        approval = self.approve(self.approver, run)
        before = len(self.actions(run))
        result = self.edit(self.operator, run, "events2")
        new = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id"))[before:]
        self.assertEqual([e.action for e in new], ["revision.created", "request.edited", "approval.invalidated", "run.state_changed"])
        invalidated, changed = new[2], new[3]
        self.assertEqual(invalidated.details, {"revision_id": str(approval.revision_id), "by_revision_id": str(result.revision_id), "approval_count": 1})
        self.assertEqual(changed.details, {"from_state": "approved", "to_state": "needs_review", "cause": "edit"})


# --- approval ------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ApprovalServiceTests(ReviewCase):
    def test_an_approver_approves_the_current_revision(self):
        run = self.new_run()
        summary = self.summary(run)
        result = self.approve(self.approver, run)
        self.assertEqual((result.revision_id, result.content_hash, result.run_state), (summary.current_revision_id, summary.content_hash, "approved"))
        row = Approval.objects.get(pk=result.approval_id)
        self.assertEqual((row.revision_id, row.content_hash, row.approver_id), (summary.current_revision_id, summary.content_hash, self.approver.public_id))
        self.assertEqual(self.state(run), "approved")

    def test_the_approver_is_derived_from_the_loaded_user_never_from_the_object_passed_in(self):
        run = self.new_run()
        forged = User.objects.get(pk=self.approver.pk)
        forged.public_id = uuid.uuid4()  # in memory only
        result = self.approve(forged, run)
        self.assertEqual(Approval.objects.get(pk=result.approval_id).approver_id, self.approver.public_id)

    def test_the_stored_approval_equals_what_the_domain_policy_produces(self):
        run = self.new_run()
        self.approve(self.approver, run)
        stored = case_services.list_approvals(self.summary(run).current_revision_id)
        (only,) = stored
        revision = case_services.get_current_revision(run.pk)
        from workflows.revision_approval import approve_revision

        self.assertEqual(only.record, approve_revision(revision, approver_id=self.approver.public_id, warnings_reviewed=True))

    def test_roles_that_may_not_approve_are_refused(self):
        for user in (self.operator, self.auditor, self.nobody):
            run = self.new_run()
            with self.subTest(user=user.username):
                self.assertRefused(AuthorizationError, "missing_permission", run, lambda: self.approve(user, run))

    def test_a_superuser_may_approve_somebody_elses_request(self):
        run = self.new_run()
        self.assertEqual(self.approve(self.superuser, run).run_state, "approved")

    def test_the_submitter_may_not_approve_even_holding_both_roles(self):
        run = self.new_run(owner=self.hybrid)
        self.assertRefused(SelfApprovalError, "self_approval_forbidden", run, lambda: self.approve(self.hybrid, run))
        self.assertEqual(self.approve(self.approver, run).run_state, "approved")  # somebody else can

    def test_a_superuser_who_submitted_may_not_approve(self):
        run = self.new_run(owner=self.superuser)
        self.assertRefused(SelfApprovalError, "self_approval_forbidden", run, lambda: self.approve(self.superuser, run))

    def test_a_superuser_who_edited_may_not_approve(self):
        run = self.new_run()
        self.edit(self.superuser, run, "root-edit")
        self.assertRefused(SelfApprovalError, "self_approval_forbidden", run, lambda: self.approve(self.superuser, run))

    def test_an_author_of_an_earlier_revision_may_not_approve_a_later_one(self):
        run = self.new_run()
        self.edit(self.superuser, run, "root-edit")
        self.edit(self.operator, run, "operator-edit")  # the superuser's revision is no longer current
        self.assertRefused(SelfApprovalError, "self_approval_forbidden", run, lambda: self.approve(self.superuser, run))

    def test_self_approval_is_refused_before_anything_about_the_revision_is_revealed(self):
        run = self.new_run(owner=self.hybrid)
        stale = self.assertRefused(
            SelfApprovalError, "self_approval_forbidden", run,
            lambda: self.approve(self.hybrid, run, revision_id=uuid.uuid4(), content_hash="f" * 64),
        )  # fmt: skip
        self.assertIsInstance(stale, ReviewRefusedError)

    def test_only_a_run_in_needs_review_can_be_approved(self):
        run = self.new_run()
        for state in STATES:
            if state == "needs_review":
                continue
            WorkflowRun.objects.filter(pk=run.pk).update(state=state, failed_step="extract" if state == "failed" else "")
            with self.subTest(state=state):
                self.assertRefused(ReviewRefusedError, "run_not_in_review", run, lambda: self.approve(self.approver, run))

    def test_a_second_approval_of_an_approved_run_is_refused(self):
        run = self.new_run()
        self.approve(self.approver, run)
        self.assertRefused(ReviewRefusedError, "run_not_in_review", run, lambda: self.approve(self.second_approver, run))
        self.assertEqual(Approval.objects.filter(revision__run=run).count(), 1)

    def test_a_run_without_a_revision_cannot_be_approved(self):
        run = self.new_run(text=INVALID_TEXT)
        self.assertRefused(ReviewRefusedError, "no_current_revision", run, lambda: review.approve_request(user=self.approver, run_id=run.pk, revision_id=uuid.uuid4(), content_hash="a" * 64))

    def test_an_old_revision_is_stale(self):
        run = self.new_run()
        old = self.summary(run)
        self.edit(self.operator, run, "newer")
        self.assertRefused(
            StaleRevisionError, "stale_revision", run,
            lambda: review.approve_request(user=self.approver, run_id=run.pk, revision_id=old.current_revision_id, content_hash=old.content_hash, acknowledged_warnings=old.warnings),
        )  # fmt: skip

    def test_a_revision_of_another_run_is_stale(self):
        run, other = self.new_run(), self.new_run()
        foreign = self.summary(other)
        self.assertRefused(
            StaleRevisionError, "stale_revision", run,
            lambda: review.approve_request(user=self.approver, run_id=run.pk, revision_id=foreign.current_revision_id, content_hash=foreign.content_hash, acknowledged_warnings=foreign.warnings),
        )  # fmt: skip

    def test_a_wrong_content_hash_is_refused_even_for_the_current_revision(self):
        run = self.new_run()
        self.assertRefused(ReviewRefusedError, "content_hash_mismatch", run, lambda: self.approve(self.approver, run, content_hash="0" * 64))
        self.assertRefused(ReviewRefusedError, "content_hash_mismatch", run, lambda: self.approve(self.approver, run, content_hash="short"))

    def test_the_hash_of_an_older_revision_does_not_approve_the_current_one(self):
        run = self.new_run()
        old = self.summary(run)
        self.edit(self.operator, run, "newer")
        self.assertRefused(ReviewRefusedError, "content_hash_mismatch", run, lambda: self.approve(self.approver, run, content_hash=old.content_hash))

    def test_warnings_must_be_acknowledged_exactly(self):
        run = self.new_run()
        self.edit(self.operator, run, "with-warning")
        summary = self.summary(run)
        self.assertTrue(summary.warnings)
        self.assertRefused(WarningsNotAcknowledgedError, "warnings_not_acknowledged", run, lambda: self.approve(self.approver, run, acknowledged_warnings=()))
        missing = self.assertRefused(WarningsNotAcknowledgedError, "warnings_not_acknowledged", run, lambda: self.approve(self.approver, run, acknowledged_warnings=summary.warnings[1:]))
        self.assertEqual(missing.missing, summary.warnings[:1])
        extra = self.assertRefused(
            WarningsNotAcknowledgedError, "unknown_warning_acknowledged", run,
            lambda: self.approve(self.approver, run, acknowledged_warnings=[*summary.warnings, ("duplicate_item", "items[9]")]),
        )  # fmt: skip
        self.assertEqual(extra.unexpected, (("duplicate_item", "items[9]"),))
        result = self.approve(self.approver, run)  # the exact set
        self.assertEqual(set(result.acknowledged_warnings), set(summary.warnings))
        self.assertEqual({tuple(p) for p in Approval.objects.get(pk=result.approval_id).acknowledged_warnings}, set(summary.warnings))

    def test_acknowledgements_may_come_as_lists_in_any_order_with_repeats(self):
        run = self.new_run()
        self.edit(self.operator, run, "unordered")
        summary = self.summary(run)
        shuffled = [list(pair) for pair in reversed(summary.warnings)] + [list(summary.warnings[0])]
        self.assertEqual(self.approve(self.approver, run, acknowledged_warnings=shuffled).run_state, "approved")

    def test_a_malformed_acknowledgement_is_a_value_error_before_any_database_work(self):
        run = self.new_run()
        for bad in ("notes", [("only-one",)], [("a", 1)], None):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                self.approve(self.approver, run, acknowledged_warnings=bad)

    def test_an_acknowledgement_made_for_an_earlier_revision_does_not_carry_over(self):
        run = self.new_run()
        self.edit(self.operator, run, "w1")
        earlier = self.summary(run).warnings
        draft = self.draft_of(run)
        self.edit(self.operator, run, draft=dataclasses.replace(draft, items=(draft.items[0], draft.items[0]), notes="w2"))
        self.assertNotEqual(self.summary(run).warnings, earlier)
        self.assertRefused(WarningsNotAcknowledgedError, "warnings_not_acknowledged", run, lambda: self.approve(self.approver, run, acknowledged_warnings=earlier))

    def test_historical_approvals_and_revisions_are_preserved_across_edits(self):
        run = self.new_run()
        first = self.approve(self.approver, run)
        self.edit(self.operator, run, "again")
        second = self.approve(self.second_approver, run)
        self.assertEqual(Approval.objects.filter(revision__run=run).count(), 2)
        self.assertNotEqual(first.revision_id, second.revision_id)
        self.assertEqual(
            {(a.revision_id, a.approver_id) for a in Approval.objects.filter(revision__run=run)},
            {(first.revision_id, self.approver.public_id), (second.revision_id, self.second_approver.public_id)},
        )
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 2)
        self.assertEqual(len(case_services.list_approvals(first.revision_id)), 1)
        decision = case_services.check_persisted_export_eligibility(
            expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=second.revision_id, approval_id=second.approval_id
        )
        self.assertTrue(decision.allowed)
        stale = case_services.check_persisted_export_eligibility(
            expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=second.revision_id, approval_id=first.approval_id
        )
        self.assertFalse(stale.allowed)

    def test_the_approval_row_cannot_be_changed_afterwards(self):
        run = self.new_run()
        row = Approval.objects.get(pk=self.approve(self.approver, run).approval_id)
        with self.assertRaises(ImmutableRecordError):
            row.save()
        with self.assertRaises(ImmutableRecordError):
            Approval.objects.filter(pk=row.pk).update(approver_id=uuid.uuid4())
        with self.assertRaises(ImmutableRecordError):
            row.delete()

    def test_audit_events_of_an_approval(self):
        run = self.new_run()
        before = len(self.actions(run))
        result = self.approve(self.approver, run)
        new = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id"))[before:]
        self.assertEqual([e.action for e in new], ["approval.recorded", "run.state_changed"])
        recorded, changed = new
        self.assertEqual((recorded.actor_public_id, recorded.subject_type, recorded.subject_id), (self.approver.public_id, "approval", result.approval_id))
        self.assertEqual(recorded.details, {"revision_id": str(result.revision_id), "content_hash": result.content_hash, "warning_count": len(result.acknowledged_warnings)})
        self.assertEqual(changed.details, {"from_state": "needs_review", "to_state": "approved", "cause": "approval"})

    def test_an_audit_failure_rolls_the_approval_and_the_state_change_back(self):
        for nth in (1, 2):
            run = self.new_run()
            before = self.snapshot(run)
            with mock.patch("apps.intake.review.record_event", FailOn(nth)), self.assertRaises(RuntimeError):
                self.approve(self.approver, run)
            self.assertEqual(self.snapshot(run), before, f"failure of event {nth}")
            self.assertEqual(self.state(run), "needs_review")

    def test_a_failed_state_change_rolls_the_approval_back(self):
        run = self.new_run()
        before = self.snapshot(run)
        with mock.patch("apps.cases.services.transition_run", side_effect=RuntimeError("transition failed")), self.assertRaises(RuntimeError):
            self.approve(self.approver, run)
        self.assertEqual(self.snapshot(run), before)
        self.assertEqual(Approval.objects.count(), 0)

    def test_a_corrupted_revision_is_never_approved(self):
        run = self.new_run()
        ResultRevision._base_manager.filter(run=run).update(content_hash="e" * 64)  # simulated tampering, bypassing the guards
        with self.assertRaises(Exception) as caught:
            self.approve(self.approver, run, content_hash="e" * 64)
        self.assertNotIsInstance(caught.exception, AuthorizationError)
        self.assertEqual(Approval.objects.count(), 0)
        self.assertEqual(self.state(run), "needs_review")


# --- rejection ---------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RejectionServiceTests(ReviewCase):
    def test_a_rejection_is_recorded_bound_to_case_run_revision_and_hash(self):
        run = self.new_run()
        summary = self.summary(run)
        result = self.reject(self.approver, run, "incomplete_content")
        row = Rejection.objects.get(pk=result.rejection_id)
        self.assertEqual((row.revision_id, row.content_hash, row.rejected_by, row.reason_code), (summary.current_revision_id, summary.content_hash, self.approver.public_id, "incomplete_content"))
        self.assertEqual((row.revision.run_id, row.revision.run.case_id), (run.pk, run.case_id))
        self.assertEqual(result, (row.pk, summary.current_revision_id, "incomplete_content", "needs_review"))

    def test_the_run_stays_in_needs_review_and_nothing_else_changes(self):
        run = self.new_run()
        before_run = WorkflowRun.objects.get(pk=run.pk)
        revision = ResultRevision.objects.get(run=run)
        self.reject(self.approver, run)
        after_run = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual((after_run.state, after_run.failed_step, after_run.current_revision_id), (before_run.state, before_run.failed_step, before_run.current_revision_id))
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 1)
        again = ResultRevision.objects.get(pk=revision.pk)
        self.assertEqual((bytes(again.canonical_bytes), again.content_hash, again.findings), (bytes(revision.canonical_bytes), revision.content_hash, revision.findings))
        self.assertEqual(Approval.objects.count(), 0)

    def test_a_rejected_revision_cannot_be_approved_but_a_new_one_can(self):
        run = self.new_run()
        self.reject(self.approver, run)
        self.assertRefused(ReviewRefusedError, "revision_rejected", run, lambda: self.approve(self.second_approver, run))
        self.edit(self.operator, run, "fixed")
        self.assertEqual(self.approve(self.second_approver, run).run_state, "approved")
        self.assertEqual(Rejection.objects.filter(revision__run=run).count(), 1)  # the rejection is history

    def test_rejections_of_the_same_revision_by_different_approvers_are_all_kept(self):
        run = self.new_run()
        self.reject(self.approver, run, "incorrect_content")
        self.reject(self.second_approver, run, "other")
        self.assertEqual(Rejection.objects.filter(revision__run=run).count(), 2)

    def test_only_the_fixed_reason_codes_are_accepted_and_free_text_is_refused(self):
        run = self.new_run()
        for reason in ("", "Incorrect content", "totally wrong, see jane@example.invalid", "OTHER", "other ", "x" * 41):
            with self.subTest(reason=reason[:20]):
                self.assertRefused(ReviewRefusedError, "unknown_reason_code", run, lambda: self.reject(self.approver, run, reason))
        for reason in REJECTION_REASONS:
            self.reject(self.approver, self.new_run(), reason)

    def test_roles_that_may_not_reject_are_refused(self):
        for user in (self.operator, self.auditor, self.nobody):
            run = self.new_run()
            with self.subTest(user=user.username):
                self.assertRefused(AuthorizationError, "missing_permission", run, lambda: self.reject(user, run))

    def test_a_superuser_may_reject(self):
        self.assertEqual(self.reject(self.superuser, self.new_run()).run_state, "needs_review")

    def test_the_submitter_may_reject_their_own_request_because_that_is_not_an_approval(self):
        run = self.new_run(owner=self.hybrid)
        self.assertEqual(self.reject(self.hybrid, run).run_state, "needs_review")

    def test_stale_and_mismatched_rejections_are_refused(self):
        run = self.new_run()
        old = self.summary(run)
        self.edit(self.operator, run, "newer")
        self.assertRefused(StaleRevisionError, "stale_revision", run, lambda: self.reject(self.approver, run, revision_id=old.current_revision_id, content_hash=old.content_hash))
        self.assertRefused(ReviewRefusedError, "content_hash_mismatch", run, lambda: self.reject(self.approver, run, content_hash=old.content_hash))

    def test_an_approved_run_cannot_be_rejected(self):
        run = self.new_run()
        self.approve(self.approver, run)
        self.assertRefused(ReviewRefusedError, "run_not_in_review", run, lambda: self.reject(self.second_approver, run))

    def test_a_run_without_a_revision_cannot_be_rejected(self):
        run = self.new_run(text=INVALID_TEXT)
        self.assertRefused(ReviewRefusedError, "no_current_revision", run, lambda: review.reject_request(user=self.approver, run_id=run.pk, revision_id=uuid.uuid4(), content_hash="a" * 64, reason_code="other"))

    def test_the_rejection_row_cannot_be_changed_afterwards(self):
        row = Rejection.objects.get(pk=self.reject(self.approver, self.new_run()).rejection_id)
        with self.assertRaises(ImmutableRecordError):
            row.save()
        with self.assertRaises(ImmutableRecordError):
            Rejection.objects.filter(pk=row.pk).update(reason_code="other")
        with self.assertRaises(ImmutableRecordError):
            Rejection.objects.filter(pk=row.pk).delete()
        with self.assertRaises(ImmutableRecordError):
            row.delete()

    def test_a_revision_with_a_rejection_cannot_be_deleted(self):
        run = self.new_run()
        self.reject(self.approver, run)
        with self.assertRaises(ImmutableRecordError):
            ResultRevision.objects.filter(run=run).delete()

    def test_audit_event_of_a_rejection_carries_the_code_and_no_free_text(self):
        run = self.new_run()
        before = len(self.actions(run))
        result = self.reject(self.approver, run, "not_supported_by_source")
        (event,) = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id"))[before:]
        self.assertEqual((event.action, event.subject_type, event.subject_id, event.actor_public_id), ("rejection.recorded", "rejection", result.rejection_id, self.approver.public_id))
        self.assertEqual(event.details, {"revision_id": str(result.revision_id), "content_hash": self.summary(run).content_hash, "reason_code": "not_supported_by_source"})

    def test_an_audit_failure_rolls_the_rejection_back(self):
        run = self.new_run()
        before = self.snapshot(run)
        with mock.patch("apps.intake.review.record_event", FailOn(1)), self.assertRaises(RuntimeError):
            self.reject(self.approver, run)
        self.assertEqual(self.snapshot(run), before)
        self.assertEqual(Rejection.objects.count(), 0)


# --- reads --------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ReadServiceTests(ReviewCase):
    def test_the_summary_of_a_fresh_valid_run(self):
        run = self.new_run()
        revision = case_services.get_current_revision(run.pk)
        summary = review.get_review_summary(user=self.operator, run_id=run.pk)
        self.assertEqual((summary.run_id, summary.case_id, summary.state), (run.pk, run.case_id, "needs_review"))
        self.assertEqual((summary.current_revision_id, summary.current_revision_number, summary.content_hash), (revision.revision_id, 1, revision.content_hash))
        self.assertEqual((summary.outcome_status, summary.approval_count, summary.rejection_count), ("valid", 0, 0))

    def test_the_summary_counts_decisions_on_the_current_revision_only(self):
        run = self.new_run()
        self.reject(self.approver, run)
        self.assertEqual(self.summary(run).rejection_count, 1)
        self.edit(self.operator, run, "newer")
        self.assertEqual((self.summary(run).rejection_count, self.summary(run).approval_count), (0, 0))
        self.approve(self.second_approver, run)
        self.assertEqual(self.summary(run).approval_count, 1)

    def test_visibility_follows_scope(self):
        run = self.new_run()
        for user in (self.operator, self.approver, self.auditor, self.superuser):
            with self.subTest(user=user.username):
                self.assertEqual(review.get_review_summary(user=user, run_id=run.pk).run_id, run.pk)
        for user, reason in ((self.other_operator, "out_of_scope"), (self.nobody, "missing_permission")):
            with self.subTest(user=user.username), self.assertRaises(AuthorizationError) as caught:
                review.get_review_summary(user=user, run_id=run.pk)
            self.assertEqual(caught.exception.reason, reason)

    def test_the_summary_of_an_unknown_run(self):
        with self.assertRaises(RecordNotFoundError):
            review.get_review_summary(user=self.auditor, run_id=uuid.uuid4())

    def test_the_summary_of_an_invalid_run_shows_the_outcome_findings(self):
        summary = self.summary(self.new_run(text=INVALID_TEXT))
        self.assertEqual((summary.outcome_status, summary.current_revision_id, summary.content_hash), ("invalid", None, None))
        self.assertTrue(any(f.blocking for f in summary.findings))

    def test_audit_events_are_readable_by_auditors_and_superusers_only(self):
        run = self.new_run()
        self.assertEqual([e.action for e in review.list_audit_events(user=self.auditor, run_id=run.pk)], ["request.submitted", "revision.created", "run.state_changed"])
        self.assertEqual(len(review.list_audit_events(user=self.superuser, run_id=run.pk)), 3)
        for user in (self.operator, self.approver, self.nobody):
            with self.subTest(user=user.username), self.assertRaises(AuthorizationError):
                review.list_audit_events(user=user, run_id=run.pk)


# --- extraction audit ------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ExtractionAuditTests(ReviewCase):
    def test_a_valid_extraction_audits_the_revision_and_the_state_change_as_the_system(self):
        run = self.new_run()
        events = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id"))
        self.assertEqual([e.action for e in events], ["request.submitted", "revision.created", "run.state_changed"])
        created, changed = events[1], events[2]
        revision = ResultRevision.objects.get(run=run)
        self.assertEqual((created.actor_kind, created.actor_public_id, created.subject_id), ("system", None, revision.pk))
        self.assertEqual(created.details["origin"], "extraction")
        self.assertEqual(created.details["revision_number"], 1)
        self.assertEqual(created.details["content_hash"], revision.content_hash)
        self.assertEqual(changed.details, {"from_state": "extracting", "to_state": "needs_review", "cause": "extraction"})

    def test_an_invalid_extraction_audits_only_the_state_change(self):
        run = self.new_run(text=INVALID_TEXT)
        self.assertEqual(self.actions(run), ["request.submitted", "run.state_changed"])

    def test_a_repeated_extraction_adds_no_events(self):
        run = self.new_run()
        before = AuditEvent.objects.count()
        self.assertFalse(services.run_extraction(run.pk).created)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_an_audit_failure_rolls_the_whole_commit_of_the_extraction_back(self):
        for nth in (1, 2):
            run = self.new_run(extract=False)
            with mock.patch("apps.intake.services.record_event", FailOn(nth)), self.assertRaises(RuntimeError):
                services.run_extraction(run.pk)
            row = WorkflowRun.objects.get(pk=run.pk)
            self.assertEqual((row.state, row.current_revision_id), ("extracting", None), f"failure of event {nth}")
            self.assertEqual((ExtractionOutcome.objects.filter(run=run).count(), ResultRevision.objects.filter(run=run).count()), (0, 0))
            self.assertEqual(self.actions(run), ["request.submitted"])
            self.assertTrue(services.run_extraction(run.pk).created)  # and the retry then succeeds


# --- audit records --------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AuditRecordTests(ReviewCase):
    def full_flow(self):
        run = self.new_run()
        self.edit(self.operator, run, "one")
        self.reject(self.approver, run)
        self.edit(self.operator, run, "two")
        self.approve(self.approver, run)
        self.edit(self.operator, run, "three")
        return run

    def test_the_whole_history_of_a_run_in_order(self):
        run = self.full_flow()
        self.assertEqual(
            self.actions(run),
            [
                "request.submitted", "revision.created", "run.state_changed",  # submit, extraction
                "revision.created", "request.edited",  # edit one
                "rejection.recorded",
                "revision.created", "request.edited",  # edit two
                "approval.recorded", "run.state_changed",  # approve
                "revision.created", "request.edited", "approval.invalidated", "run.state_changed",  # edit three
            ],
        )  # fmt: skip

    def test_every_event_belongs_to_its_case_and_run(self):
        run = self.full_flow()
        for event in AuditEvent.objects.filter(run_id=run.pk):
            self.assertEqual((event.case_id, event.run_id), (run.case_id, run.pk))

    def test_user_events_carry_the_public_id_and_system_events_none(self):
        run = self.full_flow()
        public_ids = {u.public_id for u in User.objects.all()}
        for event in AuditEvent.objects.filter(run_id=run.pk):
            with self.subTest(event=event.pk):
                if event.actor_kind == "user":
                    self.assertIn(event.actor_public_id, public_ids)
                else:
                    self.assertIsNone(event.actor_public_id)

    def test_no_event_holds_request_content_names_or_personal_data(self):
        run = self.full_flow()
        text = RequestSource.objects.get(run=run).text
        sensitive = {"Jane", "Doe", "jane@example.invalid", "A4 paper", "Stapler", "Needed this week", "olive", "anna", "Edit one", "@"}
        for event in AuditEvent.objects.filter(run_id=run.pk):
            blob = f"{event.action} {event.details} {event.actor_public_id} {event.subject_id}"
            for word in sensitive:
                with self.subTest(event=event.pk, word=word):
                    self.assertNotIn(word, blob)
        self.assertTrue(text)  # the content exists; it is simply not in the audit

    def test_audit_rows_cannot_be_updated_or_deleted_through_the_orm(self):
        run = self.new_run()
        event = AuditEvent.objects.filter(run_id=run.pk).first()
        with self.assertRaises(ImmutableRecordError):
            event.save()
        with self.assertRaises(ImmutableRecordError):
            event.delete()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(pk=event.pk).update(action="request.edited")
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(run_id=run.pk).delete()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.bulk_update([event], ["action"])

    def test_the_orm_boundary_is_not_database_level_protection(self):
        # Documented limitation, pinned so nobody starts to claim more: raw SQL changes an audit row.
        run = self.new_run()
        event = AuditEvent.objects.filter(run_id=run.pk).first()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE audit_auditevent SET action = %s WHERE id = %s", ["request.edited", event.pk])
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).action, "request.edited")

    def test_the_audit_ids_increase_in_the_order_events_were_written(self):
        run = self.full_flow()
        ids = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id").values_list("id", flat=True))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))


@tag("requires_postgres")
@requires_postgres
class RecorderTransactionTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """TransactionTestCase: no test-wide transaction, so ``in_atomic_block`` is really False here."""

    def test_recording_outside_a_transaction_is_refused_and_writes_nothing(self):
        event = audit_events.system_event(audit_events.ACTION_RUN_STATE_CHANGED, audit_events.SUBJECT_RUN, uuid.uuid4(), from_state="a", to_state="b", cause="c")
        with self.assertRaises(AuditTransactionError):
            record_event(event)
        self.assertEqual(AuditEvent.objects.count(), 0)
        with transaction.atomic():
            record_event(event)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_an_event_rolls_back_with_its_transaction(self):
        event = audit_events.system_event(audit_events.ACTION_RUN_STATE_CHANGED, audit_events.SUBJECT_RUN, uuid.uuid4(), from_state="a", to_state="b", cause="c")
        with self.assertRaises(RuntimeError), transaction.atomic():
            record_event(event)
            raise RuntimeError("the operation failed after its audit event was written")
        self.assertEqual(AuditEvent.objects.count(), 0)


# --- competing connections ----------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        setup_roles()
        self.operator = make_user("olive", OPERATOR)
        self.approver = make_user("anna", APPROVER)
        self.second_approver = make_user("amir", APPROVER)
        self.auditor = make_user("audrey", AUDITOR)

    def new_run(self, text=VALID_TEXT):
        result = submit_request(text=text, submitted_by=self.operator)
        services.run_extraction(result.run.pk)
        return WorkflowRun.objects.get(pk=result.run.pk)

    def summary(self, run):
        return review.get_review_summary(user=self.auditor, run_id=run.pk)

    def draft(self, run, marker):
        return dataclasses.replace(draft_from_json(ExtractionOutcome.objects.get(run_id=run.pk).draft), notes=f"Edit {marker}")

    def approve_call(self, run, summary, user=None):
        return lambda: review.approve_request(
            user=user or self.approver, run_id=run.pk, revision_id=summary.current_revision_id, content_hash=summary.content_hash, acknowledged_warnings=summary.warnings
        )

    def edit_call(self, run, summary, marker):
        return lambda: review.edit_request(user=self.operator, run_id=run.pk, base_revision_id=summary.current_revision_id, draft=self.draft(run, marker))

    def test_two_edits_of_the_same_revision_give_one_new_revision_and_one_stale_refusal(self):
        run = self.new_run()
        base = self.summary(run)
        results = run_threads(self, [self.edit_call(run, base, "a"), self.edit_call(run, base, "b")])
        kinds = sorted(kind for kind, _ in results)
        self.assertEqual(kinds, ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, StaleRevisionError)
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 2)
        self.assertEqual(RevisionEdit.objects.count(), 1)
        row = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual(row.current_revision_id, ResultRevision.objects.get(run=run, revision_number=2).pk)

    def test_many_edits_of_the_same_revision_still_give_exactly_one_winner(self):
        run = self.new_run()
        base = self.summary(run)
        results = run_threads(self, [self.edit_call(run, base, str(i)) for i in range(5)])
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1, results)
        self.assertTrue(all(isinstance(v, StaleRevisionError) for kind, v in results if kind == "error"))
        self.assertEqual(sorted(ResultRevision.objects.filter(run=run).values_list("revision_number", flat=True)), [1, 2])

    def test_an_edit_racing_an_approval_never_leaves_an_approval_on_the_current_revision(self):
        for attempt in range(3):
            run = self.new_run()
            base = self.summary(run)
            results = run_threads(self, [self.approve_call(run, base), self.edit_call(run, base, f"race{attempt}")])
            by_kind = {name: result for name, result in zip(("approve", "edit"), results)}
            self.assertEqual(by_kind["edit"][0], "ok", by_kind)  # the edit is valid in either order
            row = WorkflowRun.objects.get(pk=run.pk)
            self.assertEqual(ResultRevision.objects.filter(run=run).count(), 2)
            self.assertEqual(row.state, "needs_review")  # approved-then-edited, or edited-then-stale-approval
            self.assertEqual(Approval.objects.filter(revision_id=row.current_revision_id).count(), 0)
            if by_kind["approve"][0] == "ok":
                approval = Approval.objects.get(revision__run=run)
                self.assertEqual(approval.revision_id, base.current_revision_id)  # bound to the OLD revision
                self.assertEqual(approval.content_hash, base.content_hash)
                self.assertIn("approval.invalidated", [e.action for e in AuditEvent.objects.filter(run_id=run.pk)])
            else:
                self.assertIsInstance(by_kind["approve"][1], (StaleRevisionError, ReviewRefusedError))
                self.assertEqual(Approval.objects.filter(revision__run=run).count(), 0)

    def test_two_approvals_of_one_run_give_one_approval(self):
        run = self.new_run()
        base = self.summary(run)
        results = run_threads(self, [self.approve_call(run, base), self.approve_call(run, base, self.second_approver)])
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, ReviewRefusedError)
        self.assertEqual(error.reason, "run_not_in_review")
        self.assertEqual(Approval.objects.filter(revision__run=run).count(), 1)
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).state, "approved")
        self.assertEqual(AuditEvent.objects.filter(run_id=run.pk, action="approval.recorded").count(), 1)

    def test_a_rejection_racing_an_approval_leaves_a_consistent_run(self):
        run = self.new_run()
        base = self.summary(run)
        reject = lambda: review.reject_request(  # noqa: E731
            user=self.second_approver, run_id=run.pk, revision_id=base.current_revision_id, content_hash=base.content_hash, reason_code="other"
        )
        results = run_threads(self, [self.approve_call(run, base), reject])
        approvals, rejections = Approval.objects.filter(revision__run=run).count(), Rejection.objects.filter(revision__run=run).count()
        state = WorkflowRun.objects.get(pk=run.pk).state
        # Exactly one decision wins: approved-then-reject-refused, or rejected-then-approval-refused.
        self.assertEqual(approvals + rejections, 1, results)
        self.assertEqual(state, "approved" if approvals else "needs_review")

    def test_a_losing_attempt_leaves_no_trace_in_the_audit(self):
        run = self.new_run()
        base = self.summary(run)
        run_threads(self, [self.approve_call(run, base), self.approve_call(run, base, self.second_approver)])
        self.assertEqual(
            [e.action for e in AuditEvent.objects.filter(run_id=run.pk).order_by("id")],
            ["request.submitted", "revision.created", "run.state_changed", "approval.recorded", "run.state_changed"],
        )

    def test_edits_of_different_runs_do_not_block_each_other(self):
        runs = [self.new_run() for _ in range(4)]
        results = run_threads(self, [self.edit_call(run, self.summary(run), "parallel") for run in runs])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(ResultRevision.objects.count(), 8)
