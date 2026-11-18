"""Tests for workflow attribution that REQUIRE a real PostgreSQL server (MOD-1b S5).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and
docs/development/runbook.md). When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to
run against anything else.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project owner runs them locally they are
NOT RUN, and nothing about PostgreSQL behaviour is proven by this file. Their DB-free counterparts are in test_workflow_attribution.py (these pass
in the VM).

What they cover: cases migrations 0006 to 0009 applied to a database that already holds runs (the Request Intake runs are attributed on evidence, every
other column and every linked record is unchanged, a run without a Request Intake source is NOT guessed at and stops the migration with a count, and the
owner's way forward works); the backfill repeated (nothing is overwritten); the migrations reversed and applied again; the columns and constraints as
PostgreSQL stores them (NOT NULL, the three CHECKs, PostgreSQL's reading of the shape patterns against Python's); what the database cannot know (a reserved
namespace, raw SQL); a new run's attribution; the ORM guard against changing it; Request Intake end to end (submission, replay, extraction, edit, approval,
rejection) with the attribution and the canonical hash unchanged; and concurrent creation with one client request id.

The existing suites must still pass UNCHANGED apart from the narrow edits listed in the slice document: tests/test_cases_persistence_postgres.py,
tests/test_intake_postgres.py, tests/test_review_postgres.py, tests/test_api_postgres.py, tests/test_tasks_*_postgres.py and tests/test_module_host_postgres.py.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_workflow_attribution_postgres --tag requires_postgres -v 2

This file is ASCII only.
"""

import os
import random
import re
import string
import unittest
import uuid
from importlib import import_module

from django.db import DataError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag

from apps.audit.models import AuditEvent
from apps.cases import attribution, services as case_services
from apps.cases.attribution import WorkflowAttribution
from apps.cases.errors import ImmutableAttributionError
from apps.cases.models import Approval, Case, Rejection, ResultRevision, WorkflowRun
from apps.intake.models import ExtractionOutcome, RequestSource, StepTask
from apps.intake.policy import KIND_EXTRACT, extraction_idempotency_key, extraction_payload
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.submission import REQUEST_INTAKE_ATTRIBUTION, submit_request
from apps.tasks.models import Task
from apps.tasks.services import enqueue_task
from tests import test_review_postgres as review_base
from tests import test_workflow_attribution as dbfree
from tests.attribution_support import DEMO_ATTRIBUTION
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, make_user, run_threads, submit

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

TABLE = WorkflowRun._meta.db_table
REQUEST_INTAKE_VALUES = {"module_id": "request_intake", "workflow_key": "request_intake", "workflow_version": 1}
OLD_RUN_COLUMNS = ("id", "case_id", "current_revision_id", "state", "failed_step", "created_at")
ATTRIBUTION_CONSTRAINTS = ("cases_run_module_id_shape", "cases_run_workflow_key_shape", "cases_run_workflow_version_positive")

BEFORE = [("cases", "0005_rejection"), ("intake", "0002_revisionedit_request_permissions")]
STEP_A = ("cases", "0006_workflowrun_attribution_columns")
STEP_B = ("cases", "0007_backfill_run_attribution")
STEP_C = ("cases", "0008_validate_run_attribution")
STEP_D = ("cases", "0009_workflowrun_attribution_constraints")


def column_info():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, is_nullable, data_type, character_maximum_length FROM information_schema.columns WHERE table_name = %s", [TABLE]
        )
        return {name: (nullable, kind, length) for name, nullable, kind, length in cursor.fetchall()}


def constraint_info():
    with connection.cursor() as cursor:
        cursor.execute("SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'", [TABLE])
        return {name: (definition, validated) for name, definition, validated in cursor.fetchall()}


# --- the migrations against existing rows ---------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AttributionMigrationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Moves the test database back to cases 0005 (unapplying the Request Intake migrations that depend on it), inserts runs the way the old code did,
    and migrates forward again. The test database is emptied and returned to the latest migrations afterwards, pass or fail."""

    def migrate(self, targets):
        MigrationExecutor(connection).migrate(targets)
        return MigrationExecutor(connection)

    def historical_run_model(self, node):
        return MigrationExecutor(connection).loader.project_state([node]).apps.get_model("cases", "WorkflowRun")

    def setUp(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))
        self.addCleanup(self.empty_cases_tables)  # registered second, so it runs first: a failed test must not leave runs the migrations refuse
        self.latest = latest

    def empty_cases_tables(self):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE cases_case CASCADE")  # the test database only

    def build_history(self):
        """At cases 0005: two Request Intake runs (one with a revision, an approval, a rejection, a task link and audit events) and no other."""
        OldRun = self.historical_run_model(("cases", "0005_rejection"))
        user = make_user("history")
        runs = []
        for number in (1, 2):
            case = Case.objects.create(id=uuid.uuid4(), reference=f"HIST-{number}")
            run = OldRun.objects.create(id=uuid.uuid4(), case_id=case.pk, state="queued")
            RequestSource.objects.create(run_id=run.pk, submitted_by=user, text=f"text {number}", text_sha256="a" * 64, byte_length=6)
            task = enqueue_task(kind=KIND_EXTRACT, payload=extraction_payload(run.pk), idempotency_key=extraction_idempotency_key(run.pk, 1), registry=PRODUCTION_REGISTRY).task
            StepTask.objects.create(run_id=run.pk, step="extract", generation=1, task=task)
            AuditEvent.objects.create(actor_kind="system", action="run.state_changed", subject_type="run", subject_id=run.pk, run_id=run.pk, case_id=case.pk, details={})
            runs.append(run)
        revision = ResultRevision.objects.create(
            id=uuid.uuid4(), run_id=runs[0].pk, revision_number=1, canonical_format_version=1, canonical_bytes=b'{"x":1}', content_hash="b" * 64, findings=[]
        )
        OldRun.objects.filter(pk=runs[0].pk).update(current_revision_id=revision.pk, state="approved")
        Approval.objects.create(revision_id=revision.pk, content_hash="b" * 64, approver_id=uuid.uuid4(), acknowledged_warnings=[])
        Rejection.objects.create(revision_id=revision.pk, content_hash="b" * 64, rejected_by=uuid.uuid4(), reason_code="other")
        return runs

    def link_snapshot(self):
        """Every table the attribution migrations must not touch, as plain data."""
        return {
            "cases": list(Case.objects.order_by("pk").values()),
            "revisions": list(ResultRevision.objects.order_by("pk").values()),
            "approvals": list(Approval.objects.order_by("pk").values()),
            "rejections": list(Rejection.objects.order_by("pk").values()),
            "sources": list(RequestSource.objects.order_by("pk").values()),
            "links": list(StepTask.objects.order_by("pk").values()),
            "tasks": list(Task.objects.order_by("pk").values()),
            "audit": list(AuditEvent.objects.order_by("pk").values()),
        }

    def old_columns(self, model):
        return {row["id"]: {name: row[name] for name in OLD_RUN_COLUMNS} for row in model.objects.values()}

    def test_request_intake_runs_are_attributed_and_nothing_else_changes(self):
        self.migrate(BEFORE)
        runs = self.build_history()
        before_run_rows = self.old_columns(self.historical_run_model(("cases", "0005_rejection")))
        before_links = self.link_snapshot()

        self.migrate(self.latest)

        after = {row["id"]: row for row in WorkflowRun.objects.values()}
        self.assertEqual(set(after), {run.pk for run in runs})  # no run was added or deleted
        for run_id, row in after.items():
            with self.subTest(run=str(run_id)):
                self.assertEqual({name: row[name] for name in OLD_RUN_COLUMNS}, before_run_rows[run_id])  # id, case, revision pointer, state, timestamps
                self.assertEqual({name: row[name] for name in REQUEST_INTAKE_VALUES}, REQUEST_INTAKE_VALUES)
        self.assertEqual(self.link_snapshot(), before_links)  # revisions (bytes and hash), approvals, rejections, sources, tasks, links and audit

    def test_a_run_without_a_request_source_is_not_guessed_at_and_stops_the_migration(self):
        self.migrate(BEFORE)
        OldRun = self.historical_run_model(("cases", "0005_rejection"))
        sourced = self.build_history()
        orphan_case = Case.objects.create(id=uuid.uuid4(), reference="NO-SOURCE")
        orphan = OldRun.objects.create(id=uuid.uuid4(), case_id=orphan_case.pk)
        before = self.link_snapshot()

        with self.assertRaises(import_module("apps.cases.migrations.0008_validate_run_attribution").UnattributedRunsError) as caught:
            self.migrate(self.latest)
        self.assertIn("1 workflow run(s) have no workflow attribution", str(caught.exception))
        self.assertNotIn(str(orphan.pk), str(caught.exception))  # counts only

        applied = {name for app, name in MigrationExecutor(connection).loader.applied_migrations if app == "cases"}
        self.assertLessEqual({STEP_A[1], STEP_B[1]}, applied)  # steps A and B committed on their own
        self.assertFalse({STEP_C[1], STEP_D[1]} & applied)  # validation failed, so the constraints were not added
        self.assertFalse(set(ATTRIBUTION_CONSTRAINTS) & set(constraint_info()))
        self.assertEqual({column: info[0] for column, info in column_info().items() if column in REQUEST_INTAKE_VALUES}, {c: "YES" for c in REQUEST_INTAKE_VALUES})
        rows = {row["id"]: row for row in self.historical_run_model(STEP_B).objects.values()}
        self.assertEqual(set(rows), {run.pk for run in sourced} | {orphan.pk})  # nothing was deleted
        for run in sourced:
            self.assertEqual({name: rows[run.pk][name] for name in REQUEST_INTAKE_VALUES}, REQUEST_INTAKE_VALUES)
        self.assertEqual({name: rows[orphan.pk][name] for name in REQUEST_INTAKE_VALUES}, {"module_id": None, "workflow_key": None, "workflow_version": None})
        self.assertEqual(self.link_snapshot(), before)

        # The owner's way forward: decide what the orphan is, say so explicitly, and migrate again.
        self.historical_run_model(STEP_B).objects.filter(pk=orphan.pk).update(module_id="demo_module", workflow_key="demo_module.review", workflow_version=1)
        self.migrate(self.latest)
        self.assertEqual(WorkflowRun.objects.get(pk=orphan.pk).attribution, DEMO_ATTRIBUTION)
        self.assertEqual({row.attribution for row in WorkflowRun.objects.filter(pk__in=[run.pk for run in sourced])}, {REQUEST_INTAKE_ATTRIBUTION})
        self.assertLessEqual(set(ATTRIBUTION_CONSTRAINTS), set(constraint_info()))

    def test_a_partly_filled_row_is_reported_not_completed(self):
        self.migrate([STEP_B])
        OldRun = self.historical_run_model(STEP_B)
        case = Case.objects.create(id=uuid.uuid4(), reference="PARTIAL")
        partial = OldRun.objects.create(id=uuid.uuid4(), case_id=case.pk, module_id="demo_module")  # one of three columns set
        with self.assertRaises(import_module("apps.cases.migrations.0008_validate_run_attribution").UnattributedRunsError):
            self.migrate(self.latest)
        row = OldRun.objects.get(pk=partial.pk)
        self.assertEqual((row.module_id, row.workflow_key, row.workflow_version), ("demo_module", None, None))  # untouched

    def test_the_backfill_is_idempotent_and_never_overwrites(self):
        self.migrate(BEFORE)
        runs = self.build_history()
        self.migrate([STEP_B])
        OldRun = self.historical_run_model(STEP_B)
        # A run that already carries another attribution (a later release's run) must survive a repeat of the backfill.
        OldRun.objects.filter(pk=runs[1].pk).update(module_id="other_module", workflow_key="other_module.flow", workflow_version=3)
        before = {row["id"]: row for row in OldRun.objects.values()}

        backfill = import_module("apps.cases.migrations.0007_backfill_run_attribution").backfill
        state_apps = MigrationExecutor(connection).loader.project_state([STEP_B]).apps
        backfill(state_apps, None)
        backfill(state_apps, None)

        self.assertEqual({row["id"]: row for row in OldRun.objects.values()}, before)
        self.assertEqual(before[runs[0].pk]["module_id"], "request_intake")
        self.assertEqual(before[runs[1].pk]["workflow_version"], 3)

    def test_the_migrations_reverse_and_apply_again_without_losing_a_row(self):
        self.migrate(BEFORE)
        runs = self.build_history()
        self.migrate(self.latest)
        attributed = {row["id"]: row for row in WorkflowRun.objects.values()}
        self.assertLessEqual(set(REQUEST_INTAKE_VALUES), set(column_info()))
        self.assertEqual(set(ATTRIBUTION_CONSTRAINTS) - set(constraint_info()), set())

        self.migrate(BEFORE)  # reverse 0009, 0008, 0007 and 0006
        self.assertFalse(set(REQUEST_INTAKE_VALUES) & set(column_info()))
        self.assertFalse(set(ATTRIBUTION_CONSTRAINTS) & set(constraint_info()))
        unchanged = {row["id"]: {name: row[name] for name in OLD_RUN_COLUMNS} for row in self.historical_run_model(("cases", "0005_rejection")).objects.values()}
        self.assertEqual(unchanged, {run_id: {name: row[name] for name in OLD_RUN_COLUMNS} for run_id, row in attributed.items()})

        self.migrate(self.latest)  # evidence still exists, so the runs are attributed again
        self.assertEqual({row["id"]: row for row in WorkflowRun.objects.values()}, attributed)
        self.assertEqual(set(attributed), {run.pk for run in runs})

    def test_step_by_step_the_columns_are_nullable_then_required(self):
        self.migrate([("cases", "0005_rejection")])
        self.assertFalse(set(REQUEST_INTAKE_VALUES) & set(column_info()))
        self.migrate([STEP_A])
        info = column_info()
        for name in REQUEST_INTAKE_VALUES:
            self.assertEqual(info[name][0], "YES", name)
        self.assertFalse(set(ATTRIBUTION_CONSTRAINTS) & set(constraint_info()))
        self.migrate([STEP_C])
        self.assertEqual({name: column_info()[name][0] for name in REQUEST_INTAKE_VALUES}, {name: "YES" for name in REQUEST_INTAKE_VALUES})
        self.migrate([STEP_D])
        self.assertEqual({name: column_info()[name][0] for name in REQUEST_INTAKE_VALUES}, {name: "NO" for name in REQUEST_INTAKE_VALUES})

    def test_an_empty_database_migrates_straight_through(self):
        self.migrate(BEFORE)
        self.migrate(self.latest)  # no runs: nothing to backfill, nothing to refuse
        self.assertEqual(WorkflowRun.objects.count(), 0)
        self.assertEqual({name: column_info()[name][0] for name in REQUEST_INTAKE_VALUES}, {name: "NO" for name in REQUEST_INTAKE_VALUES})


# --- the schema as PostgreSQL stores it ---------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.case = case_services.create_case(reference=f"schema-{uuid.uuid4()}")

    def insert(self, **overrides):
        values = {"case": self.case, **DEMO_ATTRIBUTION.fields(), **overrides}
        with transaction.atomic():
            return WorkflowRun.objects.create(**values)

    def test_the_columns_are_required_and_typed(self):
        info = column_info()
        self.assertEqual(info["module_id"], ("NO", "character varying", 40))
        self.assertEqual(info["workflow_key"], ("NO", "character varying", 64))
        self.assertEqual(info["workflow_version"][:2], ("NO", "integer"))

    def test_the_constraints_exist_and_are_validated(self):
        constraints = constraint_info()
        for name in ATTRIBUTION_CONSTRAINTS:
            with self.subTest(constraint=name):
                self.assertIn(name, constraints)
                self.assertTrue(constraints[name][1])
        self.assertLessEqual({"cases_run_state_valid", "cases_run_failed_step_valid", "cases_run_failed_step_matches_state"}, set(constraints))  # the older ones stay

    def test_a_run_with_no_attribution_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowRun.objects.create(case=self.case)  # module_id and workflow_key default to "", the version to NULL
        with self.assertRaises(IntegrityError):
            self.insert(workflow_version=None)
        with self.assertRaises(IntegrityError):
            self.insert(module_id="")
        with self.assertRaises(IntegrityError):
            self.insert(workflow_key="")
        with self.assertRaises(IntegrityError):
            self.insert(module_id=None)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_a_malformed_attribution_is_refused_by_the_database(self):
        bad = [
            {"module_id": "ab"},
            {"module_id": "Demo_module"},
            {"module_id": "1demo"},
            {"module_id": "demo-module"},
            {"module_id": "demo.module"},
            {"workflow_key": "Demo"},
            {"workflow_key": "demo..flow"},
            {"workflow_key": "demo."},
            {"workflow_key": ".demo"},
            {"workflow_key": "demo-flow"},
            {"workflow_version": 0},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError):
                self.insert(**overrides)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_values_that_do_not_fit_the_columns_are_refused_too(self):
        for overrides in ({"module_id": "a" * 41}, {"workflow_key": "a" * 40 + "." + "b" * 24}, {"workflow_version": 2**31}):
            with self.subTest(overrides=overrides), self.assertRaises(DataError):
                self.insert(**overrides)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_the_largest_values_are_stored(self):
        run = self.insert(module_id="a" * 40, workflow_key="a" * 40 + "." + "b" * 23, workflow_version=attribution.MAX_WORKFLOW_VERSION)
        stored = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual((len(stored.module_id), len(stored.workflow_key), stored.workflow_version), (40, 64, 2147483647))

    def test_postgresql_and_python_read_the_shape_patterns_alike(self):
        rng = random.Random(20261118)
        alphabet = string.ascii_lowercase[:4] + "AZ09_.- "
        samples = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14))) for _ in range(1500)]
        samples += ["request_intake", "a.b.c", "x" * 40, "x" * 41, "a" + "." + "b" * 40, "abc_1", "ab", "a..b", "a.", ".a"]
        with connection.cursor() as cursor:
            for pattern in (attribution.MODULE_ID_PATTERN, attribution.WORKFLOW_KEY_PATTERN):
                for sample in samples:
                    cursor.execute("SELECT %s ~ %s", [sample, pattern])
                    self.assertEqual(cursor.fetchone()[0], re.fullmatch(pattern, sample) is not None, (pattern, sample))

    def test_what_the_database_cannot_know_stays_pythons_job(self):
        """The CHECKs are shape only. A reserved namespace, an unregistered module or an unrelated key and module pair are well formed, so raw SQL stores them."""
        run = self.insert(module_id="core", workflow_key="task.flow", workflow_version=7)  # refused by WorkflowAttribution, accepted by the database
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).module_id, "core")
        with self.assertRaises(attribution.WorkflowAttributionError):
            WorkflowAttribution("core", "task.flow", 7)

    def test_raw_sql_can_still_change_an_attribution_there_is_no_trigger(self):
        run = self.insert()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE cases_workflowrun SET workflow_version = 2 WHERE id = %s", [run.pk])
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).workflow_version, 2)  # ORM-level protection only, as for the other records


# --- the services and the ORM guard on a real database ---------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RunCreationTests(DedicatedTestDatabaseMixin, TestCase):
    def new_case(self):
        return case_services.create_case(reference=f"case-{uuid.uuid4()}")

    def test_create_run_stores_the_attribution_it_was_given(self):
        run = case_services.create_run(case_id=self.new_case().pk, attribution=DEMO_ATTRIBUTION)
        stored = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual(stored.attribution, DEMO_ATTRIBUTION)
        self.assertEqual((stored.module_id, stored.workflow_key, stored.workflow_version), ("demo_module", "demo_module.review", 1))
        self.assertEqual((stored.state, stored.failed_step, stored.current_revision_id), ("queued", "", None))

    def test_two_runs_of_one_case_keep_their_own_attribution(self):
        case = self.new_case()
        other = WorkflowAttribution("other_module", "other_module.flow", 4)
        first = case_services.create_run(case_id=case.pk, attribution=DEMO_ATTRIBUTION)
        second = case_services.create_run(case_id=case.pk, attribution=other)
        self.assertEqual(WorkflowRun.objects.get(pk=first.pk).attribution, DEMO_ATTRIBUTION)
        self.assertEqual(WorkflowRun.objects.get(pk=second.pk).attribution, other)

    def test_an_invalid_attribution_never_reaches_the_database(self):
        case = self.new_case()
        bad = WorkflowAttribution("demo_module", "demo_module.review", 1)
        object.__setattr__(bad, "workflow_version", 0)
        for value in (None, {"module_id": "demo_module"}, bad):
            with self.subTest(value=value), self.assertRaises((TypeError, attribution.WorkflowAttributionError)):
                case_services.create_run(case_id=case.pk, attribution=value)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_a_missing_case_is_still_refused_and_creates_nothing(self):
        from apps.cases.errors import RecordNotFoundError

        with self.assertRaises(RecordNotFoundError):
            case_services.create_run(case_id=uuid.uuid4(), attribution=DEMO_ATTRIBUTION)
        self.assertEqual(WorkflowRun.objects.count(), 0)


@tag("requires_postgres")
@requires_postgres
class ImmutableAttributionTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.run = case_services.create_run(case_id=case_services.create_case(reference=f"imm-{uuid.uuid4()}").pk, attribution=DEMO_ATTRIBUTION)

    def stored(self):
        return WorkflowRun.objects.get(pk=self.run.pk).attribution

    def test_a_loaded_run_cannot_be_given_another_attribution(self):
        for name, value in (("module_id", "other_module"), ("workflow_key", "other.flow"), ("workflow_version", 2)):
            loaded = WorkflowRun.objects.get(pk=self.run.pk)
            setattr(loaded, name, value)
            with self.subTest(field=name):
                with self.assertRaises(ImmutableAttributionError):
                    loaded.save()
                with self.assertRaises(ImmutableAttributionError):
                    loaded.save(update_fields=[name])
                self.assertEqual(self.stored(), DEMO_ATTRIBUTION)

    def test_the_run_returned_by_create_is_guarded_as_well(self):
        self.run.workflow_version = 5
        with self.assertRaises(ImmutableAttributionError):
            self.run.save()
        self.assertEqual(self.stored(), DEMO_ATTRIBUTION)

    def test_bulk_and_upsert_paths_are_refused(self):
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.filter(pk=self.run.pk).update(workflow_key="other.flow")
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.update(module_id="other_module")
        loaded = WorkflowRun.objects.get(pk=self.run.pk)
        loaded.module_id = "other_module"
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.bulk_update([loaded], ["module_id"])
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.update_or_create(pk=self.run.pk, defaults={"workflow_version": 9})
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.bulk_create([loaded], update_conflicts=True, update_fields=["module_id"], unique_fields=["id"])
        self.assertEqual(self.stored(), DEMO_ATTRIBUTION)

    def test_a_new_object_with_an_existing_id_does_not_overwrite_the_row(self):
        forged = WorkflowRun(id=self.run.pk, case_id=self.run.case_id, **WorkflowAttribution("forged_module", "forged_module.flow", 9).fields())
        with self.assertRaises(IntegrityError), transaction.atomic():
            forged.save()  # an INSERT, never an UPDATE of the existing row
        self.assertEqual(self.stored(), DEMO_ATTRIBUTION)

    def test_a_refresh_after_a_refused_change_shows_the_stored_value(self):
        loaded = WorkflowRun.objects.get(pk=self.run.pk)
        loaded.workflow_version = 2
        loaded.refresh_from_db()
        self.assertEqual(loaded.attribution, DEMO_ATTRIBUTION)
        loaded.save()  # unchanged now: allowed

    def test_everything_else_on_the_run_is_still_updatable_and_the_attribution_stays(self):
        from apps.cases.run_state import RunState

        moved = case_services.transition_run(self.run.pk, to=RunState.EXTRACTING)
        self.assertEqual((moved.state, moved.attribution), ("extracting", DEMO_ATTRIBUTION))
        self.assertEqual(WorkflowRun.objects.filter(pk=self.run.pk).update(state="queued"), 1)
        self.assertEqual(WorkflowRun.objects.get(pk=self.run.pk).state, "queued")
        self.assertEqual(self.stored(), DEMO_ATTRIBUTION)

    def test_a_partial_load_can_still_be_saved_without_touching_the_attribution(self):
        partial = WorkflowRun.objects.only("state").get(pk=self.run.pk)
        partial.state = "extracting"
        partial.save()
        self.assertEqual((WorkflowRun.objects.get(pk=self.run.pk).state, self.stored()), ("extracting", DEMO_ATTRIBUTION))


# --- Request Intake end to end ----------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RequestIntakeAttributionTests(review_base.ReviewCase):
    """The review fixtures (roles, users) with the attribution checked at every step of the workflow."""

    def attribution_of(self, run):
        return WorkflowRun.objects.get(pk=run.pk).attribution

    def test_a_submitted_request_is_attributed_to_request_intake(self):
        result = submit_request(text=VALID_TEXT, submitted_by=self.operator)
        self.assertEqual(result.run.attribution, REQUEST_INTAKE_ATTRIBUTION)
        stored = WorkflowRun.objects.get(pk=result.run.pk)
        self.assertEqual((stored.module_id, stored.workflow_key, stored.workflow_version), ("request_intake", "request_intake", 1))
        self.assertEqual(stored.state, "queued")

    def test_the_whole_workflow_leaves_the_attribution_and_the_canonical_hash_alone(self):
        run = self.new_run()  # submit and extract
        self.assertEqual(self.attribution_of(run), REQUEST_INTAKE_ATTRIBUTION)
        self.assertEqual(self.state(run), "needs_review")
        first = case_services.get_current_revision(run.pk)
        self.assertEqual(first.content_hash, dbfree.GoldenCanonicalTests.GOLDEN_HASH)  # the bytes and hash of the unchanged canonicalization
        self.assertEqual(len(first.canonical_bytes), dbfree.GoldenCanonicalTests.GOLDEN_LENGTH)

        self.edit(self.operator, run, "attribution")
        self.assertEqual(self.attribution_of(run), REQUEST_INTAKE_ATTRIBUTION)
        self.assertEqual(case_services.list_revisions(run.pk)[0].content_hash, dbfree.GoldenCanonicalTests.GOLDEN_HASH)  # history is not recomputed

        self.approve(self.approver, run)
        self.assertEqual((self.state(run), self.attribution_of(run)), ("approved", REQUEST_INTAKE_ATTRIBUTION))
        self.assertEqual(Approval.objects.filter(revision__run_id=run.pk).count(), 1)

    def test_a_rejected_request_keeps_its_attribution(self):
        run = self.new_run()
        self.reject(self.approver, run)
        self.assertEqual(self.attribution_of(run), REQUEST_INTAKE_ATTRIBUTION)
        self.assertEqual(Rejection.objects.filter(revision__run_id=run.pk).count(), 1)

    def test_a_request_whose_extraction_is_invalid_keeps_its_attribution(self):
        from tests.test_intake_postgres import INVALID_TEXT

        run = self.new_run(text=INVALID_TEXT)
        self.assertEqual(self.attribution_of(run), REQUEST_INTAKE_ATTRIBUTION)
        self.assertEqual(ExtractionOutcome.objects.get(run_id=run.pk).status, "invalid")

    def test_a_replayed_submission_returns_the_same_attributed_run_and_creates_nothing(self):
        client_request_id = uuid.uuid4()
        first = submit(self.operator, client_request_id=client_request_id)
        again = submit(self.operator, client_request_id=client_request_id)
        self.assertEqual((first.created, again.created, again.run.pk), (True, False, first.run.pk))
        self.assertEqual(WorkflowRun.objects.count(), 1)
        self.assertEqual(self.attribution_of(first.run), REQUEST_INTAKE_ATTRIBUTION)

    def test_a_refused_submission_stores_no_run_at_all(self):
        from apps.intake.policy import RequestTextError

        with self.assertRaises(RequestTextError):
            submit_request(text="   ", submitted_by=self.operator)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_a_failed_extraction_keeps_the_attribution(self):
        from apps.cases.run_state import FailedStep, RunState

        result = submit_request(text=VALID_TEXT, submitted_by=self.operator)
        case_services.transition_run(result.run.pk, to=RunState.FAILED, failed_step=FailedStep.EXTRACT.value)
        self.assertEqual(self.attribution_of(result.run), REQUEST_INTAKE_ATTRIBUTION)


@tag("requires_postgres")
@requires_postgres
class ConcurrentCreationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def test_concurrent_submissions_with_one_client_request_id_create_one_attributed_run(self):
        user = make_user("racer")
        client_request_id = uuid.uuid4()
        results = run_threads(self, [lambda: submit(user, client_request_id=client_request_id) for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(1 for _, value in results if value.created), 1)
        self.assertEqual(WorkflowRun.objects.count(), 1)
        (run,) = WorkflowRun.objects.all()
        self.assertEqual(run.attribution, REQUEST_INTAKE_ATTRIBUTION)
        self.assertEqual({value.run.pk for _, value in results}, {run.pk})

    def test_concurrent_distinct_submissions_each_get_the_attribution(self):
        user = make_user("racer2")
        results = run_threads(self, [lambda i=i: submit(user, text=f"Item: {i + 1} | | Pens\n") for i in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(WorkflowRun.objects.count(), 6)
        self.assertEqual({run.attribution for run in WorkflowRun.objects.all()}, {REQUEST_INTAKE_ATTRIBUTION})

    def test_concurrent_creation_of_runs_through_the_service_keeps_each_attribution(self):
        case = case_services.create_case(reference="concurrent-core")
        values = [WorkflowAttribution(f"module_{n}", f"module_{n}.flow", n + 1) for n in range(4)]
        results = run_threads(self, [lambda a=a: case_services.create_run(case_id=case.pk, attribution=a) for a in values])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual({run.attribution for run in WorkflowRun.objects.all()}, set(values))
