"""Tests for the audit vocabulary migration and recorder that REQUIRE a real PostgreSQL server (MOD-1b S3).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project
owner runs them locally they are NOT RUN, and nothing about PostgreSQL behaviour is proven by this file. Their
DB-free counterparts are in test_audit_vocabulary.py (these pass in the VM).

What they cover: audit migration 0002 applied to a database that already holds audit events (nothing is changed
or deleted); the two new CHECK constraints as PostgreSQL stores them; PostgreSQL's reading of the action-shape
pattern against Python's on a fixed corpus; a valid module action through the one recorder; an unknown but
well-formed action refused by Python and (a documented limit) accepted by the database; malformed actions
refused by the database even when Python is bypassed; the "module" subject type; rollback of the business
operation when the audit write fails; Core's own audit history unchanged while a module vocabulary is installed;
and the reverse migration with and without events that only the new vocabulary allows (refused, nothing deleted).

The existing suites must still pass UNCHANGED: tests/test_review_postgres.py (AuditRecordTests,
RecorderTransactionTests and the services that write events) and tests/test_api_postgres.py (AuditApiTests).

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_audit_vocabulary_postgres --tag requires_postgres -v 2

This file is ASCII only.
"""

import os
import random
import string
import unittest
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DataError, IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, TransactionTestCase, tag

from apps.accounts.errors import AuthorizationError
from apps.audit import events
from apps.audit.events import AuditEventError, system_event, user_event
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditTransactionError, list_run_events, record_event
from apps.audit.vocabulary import ACTION_SHAPE_PATTERN, action_shape_ok
from apps.cases import services as case_services
from apps.cases.models import Case, WorkflowRun
from apps.intake.submission import submit_request
from apps.tasks.models import Task
from tests import test_api_postgres as api_base
from tests import test_review_postgres as review_base
from tests.audit_support import DEMO_ACTION, demo_vocabulary, installed
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import DedicatedTestDatabaseMixin

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

AUDIT_0001 = ("audit", "0001_initial")
AUDIT_0002 = ("audit", "0002_extensible_audit_vocabulary")
TABLE = "audit_auditevent"
OLD_SUBJECTS = ("run", "revision", "approval", "rejection")
FIELDS = ("id", "occurred_at", "actor_kind", "actor_public_id", "action", "subject_type", "subject_id", "case_id", "run_id", "details")

MALFORMED = [
    "",
    "single",
    "Upper.case",
    "a.Upper",
    "a..b",
    "a.b.",
    ".a.b",
    "a.b c",
    "a.b\n",
    "\na.b",
    "1a.b",
    "a.1b",
    "a.b-c",
    "a.b/c",
    "a.b;",
    "caf\u00e9.b",
    "a" * 33 + ".b",
]
WELL_FORMED = ["a.b", "mod_x.did_it", "a_1.b_2", "a.b.c", "legacy.did_it", "x" * 15 + "." + "y" * 16, ".".join("a" * 16)]


def row(action="run.state_changed", subject_type="run", **fields):
    """The keyword arguments of one audit row (the user kind needs a public id, so the default actor is the system)."""
    values = {"actor_kind": "system", "action": action, "subject_type": subject_type, "subject_id": uuid.uuid4(), "details": {}}
    values.update(fields)
    return values


def snapshot(model=AuditEvent):
    return list(model.objects.order_by("id").values_list(*FIELDS))


def corpus():
    """Strings of at most 32 characters, fixed by a seed, mixing valid shapes with near misses."""
    rng = random.Random(20261117)
    alphabet = string.ascii_lowercase[:5] + "AZ09_.-. \n"
    generated = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 32))) for _ in range(1500)]
    return [*WELL_FORMED, *(m for m in MALFORMED if len(m) <= 32), *generated]


@tag("requires_postgres")
@requires_postgres
class AuditMigrationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Applies audit 0002 to a database that already holds events, and reverses it. The database is returned to the latest migrations afterwards."""

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return MigrationExecutor(connection)

    def setUp(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def old_model(self, state_key):
        return MigrationExecutor(connection).loader.project_state([state_key]).apps.get_model("audit", "AuditEvent")

    def constraints(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'", [TABLE]
            )
            return {name: (definition, validated) for name, definition, validated in cursor.fetchall()}

    def insert_old_vocabulary_rows(self):
        model = self.old_model(AUDIT_0001)
        actions = events.ACTIONS
        for index, action in enumerate(actions):
            model.objects.create(**row(action, OLD_SUBJECTS[index % 4], details={"n": index, "flag": True}, case_id=uuid.uuid4(), run_id=uuid.uuid4()))
        model.objects.create(**row(actor_kind="user", actor_public_id=uuid.uuid4()))
        return model

    def test_forward_with_existing_rows_changes_and_deletes_nothing(self):
        self.migrate(AUDIT_0001)
        self.assertIn("audit_event_action_valid", self.constraints())
        model = self.insert_old_vocabulary_rows()
        before = snapshot(model)
        self.assertEqual(len(before), len(events.ACTIONS) + 1)

        self.migrate(AUDIT_0002)

        self.assertEqual(snapshot(), before)  # same ids, timestamps, actors, actions, subjects, details: not one value differs

    def test_the_constraints_after_the_migration(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        constraints = self.constraints()
        self.assertEqual(set(constraints), {"audit_event_action_shape", "audit_event_subject_type_valid", "audit_event_actor_kind_valid", "audit_event_actor_matches_kind"})
        self.assertNotIn("audit_event_action_valid", constraints)
        shape, validated = constraints["audit_event_action_shape"]
        self.assertTrue(validated)
        self.assertIn("~", shape)  # a regular expression match, not a list
        self.assertNotIn("request.submitted", shape)  # no fixed action name is left in it
        subject, validated = constraints["audit_event_subject_type_valid"]
        self.assertTrue(validated)
        for name in (*OLD_SUBJECTS, "module"):
            self.assertIn(f"'{name}'", subject)

    def test_the_old_vocabulary_still_works_and_new_shapes_and_the_module_subject_are_accepted(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        for index, action in enumerate(events.ACTIONS):
            AuditEvent.objects.create(**row(action, OLD_SUBJECTS[index % 4]))  # every Core action, raw ORM insert (no Python vocabulary involved)
        for action in WELL_FORMED:
            with self.subTest(action=action):
                AuditEvent.objects.create(**row(action))
        AuditEvent.objects.create(**row("run.state_changed", "module"))
        self.assertEqual(AuditEvent.objects.count(), len(events.ACTIONS) + len(WELL_FORMED) + 1)

    def test_malformed_actions_and_unknown_subjects_are_refused_by_the_database(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        for action in MALFORMED:
            with self.subTest(action=action), self.assertRaises((IntegrityError, DataError)), transaction.atomic():
                AuditEvent.objects.create(**row(action))
        for subject in ("document", "Module", "modules", ""):
            with self.subTest(subject=subject), self.assertRaises(IntegrityError), transaction.atomic():
                AuditEvent.objects.create(**row(subject_type=subject))
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_postgresql_and_python_read_the_action_shape_the_same_way(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        with connection.cursor() as cursor:
            for text in corpus():
                cursor.execute("SELECT %s::text ~ %s", [text, ACTION_SHAPE_PATTERN])
                self.assertEqual(cursor.fetchone()[0], action_shape_ok(text), repr(text))
        for text in WELL_FORMED:
            self.assertTrue(action_shape_ok(text), text)
        for text in MALFORMED:
            if len(text) <= 32:
                self.assertFalse(action_shape_ok(text), repr(text))

    def test_reverse_without_new_events_restores_the_old_constraints_and_keeps_every_row(self):
        self.migrate(AUDIT_0001)
        self.insert_old_vocabulary_rows()
        self.migrate(AUDIT_0002)
        before = snapshot()

        self.migrate(AUDIT_0001)

        self.assertEqual(snapshot(self.old_model(AUDIT_0001)), before)
        constraints = self.constraints()
        self.assertIn("audit_event_action_valid", constraints)
        self.assertNotIn("audit_event_action_shape", constraints)
        self.assertIn("'request.submitted'", constraints["audit_event_action_valid"][0])
        self.assertNotIn("'module'", constraints["audit_event_subject_type_valid"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.old_model(AUDIT_0001).objects.create(**row("mod_x.did_it"))  # the fixed list is back
        self.migrate(AUDIT_0002)  # and forward again
        self.assertEqual(snapshot(), before)

    def test_reverse_is_refused_when_a_module_action_is_stored_and_nothing_is_deleted(self):
        self.migrate(AUDIT_0001)
        self.insert_old_vocabulary_rows()
        self.migrate(AUDIT_0002)
        AuditEvent.objects.create(**row("mod_x.did_it", details={"count": 1}))
        before = snapshot()
        constraints_before = self.constraints()

        with self.assertRaises(IrreversibleError) as caught:
            self.migrate(AUDIT_0001)

        self.assertIn("never deleted or rewritten", str(caught.exception))
        self.assertNotIn("mod_x", str(caught.exception))  # counts, not content
        self.assertEqual(snapshot(), before)  # every event is still there, unchanged
        self.assertEqual(self.constraints(), constraints_before)  # no constraint was touched
        self.assertIn(AUDIT_0002, MigrationRecorder(connection).applied_migrations())  # and the migration is still recorded as applied

    def test_reverse_is_refused_when_only_the_module_subject_type_is_stored(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        AuditEvent.objects.create(**row("run.state_changed", "module"))
        before = snapshot()
        with self.assertRaises(IrreversibleError):
            self.migrate(AUDIT_0001)
        self.assertEqual(snapshot(), before)
        self.assertIn(AUDIT_0002, MigrationRecorder(connection).applied_migrations())

    def test_reverse_succeeds_on_an_empty_table_and_the_migration_can_be_reapplied(self):
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        self.migrate(AUDIT_0001)
        self.migrate(AUDIT_0002)
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertIn("audit_event_action_shape", self.constraints())


@tag("requires_postgres")
@requires_postgres
class ModuleActionRecorderTests(DedicatedTestDatabaseMixin, TestCase):
    """The one recorder with a module vocabulary installed. TestCase runs inside a transaction, as ``record_event`` requires."""

    def setUp(self):
        self.enterContext(installed(demo_vocabulary()))
        self.actor, self.subject, self.case, self.run = (uuid.uuid4() for _ in range(4))

    def module_event(self, subject="run", **details):
        return user_event(self.actor, DEMO_ACTION, subject, self.subject, case_id=self.case, run_id=self.run, **(details or {"count": 2, "reason_code": "ok_1"}))

    def test_a_declared_module_action_is_recorded_with_all_its_fields(self):
        stored = record_event(self.module_event())
        stored = AuditEvent.objects.get(pk=stored.pk)
        self.assertEqual(
            (stored.actor_kind, stored.actor_public_id, stored.action, stored.subject_type, stored.subject_id, stored.case_id, stored.run_id, stored.details),
            ("user", self.actor, DEMO_ACTION, "run", self.subject, self.case, self.run, {"count": 2, "reason_code": "ok_1"}),
        )

    def test_the_module_subject_type_is_recorded_and_read_back_in_order_with_core_events(self):
        record_event(system_event("run.state_changed", "run", self.subject, run_id=self.run, from_state="a", to_state="b", cause="c"))
        record_event(self.module_event(subject="module"))
        record_event(system_event("run.state_changed", "module", self.subject, run_id=self.run, cause="c"))
        listed = list_run_events(self.run)
        self.assertEqual([(e.action, e.subject_type) for e in listed], [("run.state_changed", "run"), (DEMO_ACTION, "module"), ("run.state_changed", "module")])
        self.assertEqual([e.id for e in listed], sorted(e.id for e in listed))

    def test_an_unknown_but_well_formed_action_is_refused_by_python_before_anything_is_written(self):
        with self.assertRaises(AuditEventError):
            user_event(self.actor, "audit_demo.unknown", "run", self.subject)
        with self.assertRaises(AuditEventError):
            user_event(self.actor, "other_mod.did_it", "run", self.subject)
        with self.assertRaises(AuditEventError):
            self.module_event(**{"undeclared_key": 1})
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_the_database_does_not_know_the_allowlist_so_raw_inserts_of_an_unknown_well_formed_action_succeed(self):
        # Pinned so nobody claims more: the CHECK is a shape, not a membership test. Only the Python vocabulary allows or refuses an action.
        AuditEvent.objects.create(**row("audit_demo.unknown"))
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO audit_auditevent (occurred_at, actor_kind, action, subject_type, subject_id, details) VALUES (now(), 'system', %s, 'run', %s::uuid, '{}')",
                ["never_declared.at_all", str(uuid.uuid4())],
            )
        self.assertEqual(AuditEvent.objects.filter(action__in=["audit_demo.unknown", "never_declared.at_all"]).count(), 2)

    def test_a_malformed_action_is_refused_by_the_database_even_with_python_bypassed(self):
        for action in ("Bad.Action", "single", "a..b", "a.b\n"):
            with self.subTest(action=action), self.assertRaises(IntegrityError), transaction.atomic():
                AuditEvent.objects.create(**row(action))
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO audit_auditevent (occurred_at, actor_kind, action, subject_type, subject_id, details) VALUES (now(), 'system', 'NoDots', 'run', %s::uuid, '{}')",
                [str(uuid.uuid4())],
            )
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_core_actions_are_recorded_exactly_as_before(self):
        for action, keys in events.DETAIL_KEYS.items():
            with self.subTest(action=action):
                stored = record_event(system_event(action, "run", self.subject, run_id=self.run, **{key: "ok_1" for key in keys}))
                self.assertEqual(AuditEvent.objects.get(pk=stored.pk).action, action)

    def test_without_the_vocabulary_the_module_action_is_refused_again(self):
        event = self.module_event()
        with mock.patch.object(events, "_installed", None):
            with self.assertRaises(AuditEventError):
                self.module_event()
        self.assertEqual(event.action, DEMO_ACTION)


@tag("requires_postgres")
@requires_postgres
class AuditFailureRollsBackTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """TransactionTestCase: real commits and rollbacks, so ``in_atomic_block`` is only true where the code opens a transaction."""

    def setUp(self):
        self.enterContext(installed(demo_vocabulary()))

    def test_recording_a_module_event_outside_a_transaction_is_refused_and_writes_nothing(self):
        event = user_event(uuid.uuid4(), DEMO_ACTION, "run", uuid.uuid4(), count=1)
        with self.assertRaises(AuditTransactionError):
            record_event(event)
        self.assertEqual(AuditEvent.objects.count(), 0)
        with transaction.atomic():
            record_event(event)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_a_module_event_rolls_back_with_its_transaction(self):
        event = user_event(uuid.uuid4(), DEMO_ACTION, "run", uuid.uuid4(), count=1)
        with self.assertRaises(RuntimeError), transaction.atomic():
            record_event(event)
            raise RuntimeError("the operation failed after its audit event was written")
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_when_the_audit_write_fails_the_business_operation_is_rolled_back(self):
        # A database-level audit failure (a malformed action written around Python) takes the business row with it.
        with self.assertRaises(IntegrityError), transaction.atomic():
            case_services.create_case(reference="module-operation")
            AuditEvent.objects.create(**row("Not.Well.Formed.Because.Upper"))
        self.assertEqual((Case.objects.count(), AuditEvent.objects.count()), (0, 0))

    def test_when_python_refuses_the_event_the_business_operation_is_rolled_back(self):
        with self.assertRaises(AuditEventError), transaction.atomic():
            case_services.create_case(reference="module-operation-2")
            record_event(user_event(uuid.uuid4(), "audit_demo.undeclared", "run", uuid.uuid4()))
        self.assertEqual((Case.objects.count(), AuditEvent.objects.count()), (0, 0))

    def test_a_core_operation_whose_audit_write_fails_leaves_nothing_behind_with_a_module_vocabulary_installed(self):
        user = get_user_model().objects.create_user(username="alice")
        with mock.patch("apps.intake.submission.record_event", side_effect=RuntimeError("simulated audit failure")):
            with self.assertRaises(RuntimeError):
                submit_request(text=VALID_TEXT, submitted_by=user)
        self.assertEqual((Case.objects.count(), WorkflowRun.objects.count(), Task.objects.count(), AuditEvent.objects.count()), (0, 0, 0, 0))
        result = submit_request(text=VALID_TEXT, submitted_by=user)  # and the unpatched operation still works and audits as before
        self.assertEqual([e.action for e in AuditEvent.objects.filter(run_id=result.run.pk)], ["request.submitted"])


@tag("requires_postgres")
@requires_postgres
class CoreAuditHistoryWithModuleVocabularyTests(review_base.ReviewCase):
    """The review services with a module vocabulary installed: Core's audit history is exactly what the existing suite expects."""

    def setUp(self):
        super().setUp()
        self.enterContext(installed(demo_vocabulary()))

    def test_submit_extract_edit_reject_edit_approve_writes_the_same_history(self):
        run = self.new_run()
        self.edit(self.operator, run, "one")
        self.reject(self.approver, run)
        self.edit(self.operator, run, "two")
        self.approve(self.approver, run)
        self.assertEqual(
            self.actions(run),
            [
                "request.submitted", "revision.created", "run.state_changed",
                "revision.created", "request.edited",
                "rejection.recorded",
                "revision.created", "request.edited",
                "approval.recorded", "run.state_changed",
            ],
        )  # fmt: skip
        self.assertEqual(AuditEvent.objects.exclude(action__in=events.ACTIONS).count(), 0)

    def test_a_refused_operation_still_writes_no_audit_event(self):
        run = self.new_run()
        before = AuditEvent.objects.count()
        with self.assertRaises(AuthorizationError):
            self.approve(self.nobody, run)  # no permission: refused before anything is written
        self.assertEqual(AuditEvent.objects.count(), before)


@tag("requires_postgres")
@requires_postgres
class AuditApiWithModuleActionsTests(api_base.ApiCase):
    """The audit endpoint reports a module action as plain text, next to Core's events, for an Auditor only."""

    def setUp(self):
        super().setUp()
        self.enterContext(installed(demo_vocabulary()))

    def test_an_auditor_sees_core_and_module_events_in_order_and_others_are_refused(self):
        run_id = self.extracted()
        run = WorkflowRun.objects.get(pk=run_id)
        with transaction.atomic():
            record_event(system_event(DEMO_ACTION, "module", uuid.uuid4(), case_id=run.case_id, run_id=run.pk, count=5))
        data = self.as_(self.auditor).get(f"/api/requests/{run_id}/audit/").json()
        actions = [e["action"] for e in data["results"]]
        self.assertEqual(actions, ["request.submitted", "revision.created", "run.state_changed", DEMO_ACTION])
        self.assertEqual(data["results"][-1]["details"], {"count": 5})
        self.assertEqual(data["results"][-1]["subject_type"], "module")
        for user in (self.operator, self.nobody):
            self.assertEqual(self.as_(user).get(f"/api/requests/{run_id}/audit/").status_code, 403)
