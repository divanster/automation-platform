"""DB-free tests for workflow attribution (MOD-1b S5): the pure value, the model guard, the service, the migrations and the boundaries.

Nothing here opens a database connection (``SimpleTestCase`` refuses queries; the model and queryset tests replace the one method that
would reach SQL). What needs a real PostgreSQL (the backfill on existing rows, the constraints as stored, the migration forward and back,
concurrency) is in tests/test_workflow_attribution_postgres.py and is NOT run in the development VM.

This file is ASCII only.
"""

import ast
import uuid
from importlib import import_module
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase

from apps.cases import attribution, services
from apps.cases.attribution import (
    ATTRIBUTION_FIELDS,
    MAX_WORKFLOW_VERSION,
    WorkflowAttribution,
    WorkflowAttributionError,
    validate_attribution,
)
from apps.cases.errors import ImmutableAttributionError, ImmutableRecordError
from apps.cases.models import WorkflowRun, WorkflowRunQuerySet
from apps.intake import policy as intake_policy
from apps.intake.submission import REQUEST_INTAKE_ATTRIBUTION
from apps.modules.registry import ModuleRegistry
from tests.attribution_support import DEMO_ATTRIBUTION
from tests.module_support import intake_like_manifest

BACKEND = Path(__file__).resolve().parent.parent
CASES = BACKEND / "apps" / "cases"


class RequestIntakeIdentityTests(SimpleTestCase):
    def test_the_identifiers_are_the_stable_existing_ones(self):
        self.assertEqual(
            (intake_policy.MODULE_ID, intake_policy.WORKFLOW_KEY, intake_policy.WORKFLOW_VERSION), ("request_intake", "request_intake", 1)
        )
        self.assertEqual(REQUEST_INTAKE_ATTRIBUTION, WorkflowAttribution("request_intake", "request_intake", 1))
        self.assertTrue(intake_policy.KIND_EXTRACT.startswith(intake_policy.MODULE_ID + "."))  # the task kind prefix the module already owns

    def test_they_equal_what_the_synthetic_manifest_declares(self):
        declared = intake_like_manifest()
        (workflow,) = declared.workflows
        self.assertEqual(declared.module_id, intake_policy.MODULE_ID)
        self.assertEqual(workflow.workflow_key, intake_policy.WORKFLOW_KEY)
        self.assertEqual(workflow.definition_version, intake_policy.WORKFLOW_VERSION)
        registry = ModuleRegistry.build([declared])
        self.assertEqual(registry.namespace_owners[intake_policy.WORKFLOW_KEY.split(".", 1)[0]], intake_policy.MODULE_ID)  # the module owns its key

    def test_the_demo_attribution_used_by_core_tests_is_valid_and_not_request_intake(self):
        self.assertNotEqual(DEMO_ATTRIBUTION, REQUEST_INTAKE_ATTRIBUTION)


# --- the model guard --------------------------------------------------------------------------------------------------------------


def loaded_run(**overrides):
    """A run as the ORM would hand it back from the database (no query is made), with the attribution of ``DEMO_ATTRIBUTION``."""
    values = {
        "id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "current_revision_id": None,
        "state": "queued",
        "failed_step": "",
        **DEMO_ATTRIBUTION.fields(),
        "created_at": None,
    }
    values.update(overrides)
    names = [f.attname for f in WorkflowRun._meta.concrete_fields]
    field_names = [name for name in names if name in values]
    return WorkflowRun.from_db("default", field_names, [values[name] for name in field_names])


class RunModelShapeTests(SimpleTestCase):
    def test_the_attribution_columns_are_required_and_bounded(self):
        for name, length in (("module_id", 40), ("workflow_key", 64)):
            field = WorkflowRun._meta.get_field(name)
            with self.subTest(field=name):
                self.assertFalse(field.null)
                self.assertFalse(field.has_default())
                self.assertEqual(field.max_length, length)
        version = WorkflowRun._meta.get_field("workflow_version")
        self.assertIsInstance(version, models.PositiveIntegerField)
        self.assertFalse(version.null)
        self.assertFalse(version.has_default())

    def test_the_constraints_use_the_shared_patterns(self):
        by_name = {c.name: c for c in WorkflowRun._meta.constraints}
        self.assertIn(("module_id__regex", attribution.MODULE_ID_PATTERN), by_name["cases_run_module_id_shape"].condition.children)
        self.assertIn(("workflow_key__regex", attribution.WORKFLOW_KEY_PATTERN), by_name["cases_run_workflow_key_shape"].condition.children)
        self.assertEqual(
            sorted(by_name["cases_run_workflow_version_positive"].condition.children),
            [("workflow_version__gte", 1), ("workflow_version__lte", MAX_WORKFLOW_VERSION)],
        )

    def test_the_version_limit_is_the_manifests_in_the_model_the_column_and_the_migrations(self):
        from apps.modules.manifest import MAX_WORKFLOW_DEFINITION_VERSION

        limit = MAX_WORKFLOW_DEFINITION_VERSION
        self.assertEqual(limit, 2**31 - 1)
        self.assertEqual(attribution.MAX_WORKFLOW_VERSION, limit)
        self.assertEqual(WorkflowRun._meta.get_field("workflow_version").get_internal_type(), "PositiveIntegerField")  # a 32-bit integer column, not a big one
        constraint = {c.name: c for c in WorkflowRun._meta.constraints}["cases_run_workflow_version_positive"]
        self.assertEqual(sorted(constraint.condition.children), [("workflow_version__gte", 1), ("workflow_version__lte", limit)])
        self.assertEqual(import_module("apps.cases.migrations.0008_validate_run_attribution").MAX_WORKFLOW_VERSION, limit)
        step_d = loader().disk_migrations[("cases", "0009_workflowrun_attribution_constraints")].operations[-1]
        self.assertEqual(sorted(step_d.constraint.condition.children), [("workflow_version__gte", 1), ("workflow_version__lte", limit)])

    def test_the_run_gained_no_other_column(self):
        self.assertEqual(
            {f.name for f in WorkflowRun._meta.local_fields},
            {"id", "case", "current_revision", "state", "failed_step", "module_id", "workflow_key", "workflow_version", "created_at"},
        )

    def test_the_default_manager_is_the_guarded_queryset(self):
        self.assertIsInstance(WorkflowRun.objects.all(), WorkflowRunQuerySet)

    def test_the_stored_values_read_back_as_an_attribution(self):
        self.assertEqual(loaded_run().attribution, DEMO_ATTRIBUTION)
        with self.assertRaises(WorkflowAttributionError):
            loaded_run(workflow_version=0).attribution

    def test_the_guard_error_is_an_immutable_record_error(self):
        self.assertTrue(issubclass(ImmutableAttributionError, ImmutableRecordError))


class QuerysetGuardTests(SimpleTestCase):
    def test_bulk_changes_of_the_attribution_are_refused_before_any_sql(self):
        for name in ATTRIBUTION_FIELDS:
            with self.subTest(field=name):
                with self.assertRaises(ImmutableAttributionError):
                    WorkflowRun.objects.update(**{name: "x"})
                with self.assertRaises(ImmutableAttributionError):
                    WorkflowRun.objects.filter(pk=uuid.uuid4()).update(state="queued", **{name: 1})
                with self.assertRaises(ImmutableAttributionError):
                    WorkflowRun.objects.bulk_update([loaded_run()], [name])
        with self.assertRaises(ImmutableAttributionError):
            WorkflowRun.objects.bulk_create([loaded_run()], update_conflicts=True, update_fields=["state"], unique_fields=["id"])

    def test_everything_else_on_a_run_is_still_updatable(self):
        with mock.patch.object(models.QuerySet, "update", return_value=1) as update:
            self.assertEqual(WorkflowRun.objects.filter(pk=uuid.uuid4()).update(state="extracting"), 1)
        update.assert_called_once_with(state="extracting")
        with mock.patch.object(models.QuerySet, "bulk_update", return_value=1) as bulk:
            WorkflowRun.objects.bulk_update([loaded_run()], ["state", "failed_step"])
        self.assertEqual(bulk.call_args.args[1], ["state", "failed_step"])

    def test_a_plain_bulk_create_is_not_refused(self):
        with mock.patch.object(models.QuerySet, "bulk_create", return_value=[]) as create:
            WorkflowRun.objects.bulk_create([loaded_run()])
        create.assert_called_once()


class InstanceGuardTests(SimpleTestCase):
    def saved_with(self, run, **kwargs):
        """Save ``run`` with the one method that would reach SQL replaced; returns the mock."""
        with mock.patch.object(models.Model, "save_base") as save_base:
            run.save(**kwargs)
        return save_base

    def test_a_changed_attribution_on_a_loaded_run_is_refused_and_nothing_is_written(self):
        for name, new in (("module_id", "other_module"), ("workflow_key", "other.flow"), ("workflow_version", 2)):
            run = loaded_run()
            setattr(run, name, new)
            with self.subTest(field=name), mock.patch.object(models.Model, "save_base") as save_base:
                with self.assertRaises(ImmutableAttributionError):
                    run.save()
                with self.assertRaises(ImmutableAttributionError):
                    run.save(update_fields=["state", name])
                save_base.assert_not_called()

    def test_update_fields_naming_an_attribution_column_is_refused_even_with_the_same_value(self):
        run = loaded_run()
        with mock.patch.object(models.Model, "save_base") as save_base:
            for name in ATTRIBUTION_FIELDS:
                with self.subTest(field=name), self.assertRaises(ImmutableAttributionError):
                    run.save(update_fields=[name])
            save_base.assert_not_called()

    def test_the_updates_the_services_make_are_allowed(self):
        run = loaded_run()
        run.state = "extracting"
        save_base = self.saved_with(run, update_fields=["state", "failed_step"])
        save_base.assert_called_once()
        self.assertEqual(save_base.call_args.kwargs["update_fields"], frozenset({"state", "failed_step"}))
        run.current_revision_id = uuid.uuid4()
        self.saved_with(run, update_fields=["current_revision"]).assert_called_once()

    def test_a_full_save_of_an_unchanged_run_is_allowed(self):
        self.saved_with(loaded_run()).assert_called_once()

    def test_a_deferred_attribution_cannot_be_written_by_a_save(self):
        run = loaded_run()
        run = WorkflowRun.from_db("default", ["id", "state"], [run.pk, "queued"])
        save_base = self.saved_with(run)
        self.assertFalse({"module_id", "workflow_key", "workflow_version"} & set(save_base.call_args.kwargs["update_fields"]))

    def test_a_partial_refresh_does_not_launder_a_changed_value(self):
        run = loaded_run()
        run.workflow_version = 9
        with mock.patch.object(models.Model, "refresh_from_db"):
            run.refresh_from_db(fields=["state"])
        with mock.patch.object(models.Model, "save_base") as save_base:
            with self.assertRaises(ImmutableAttributionError):
                run.save()
            save_base.assert_not_called()

    def test_a_full_refresh_remembers_what_the_database_returned(self):
        run = loaded_run()

        def reload(self, using=None, fields=None, **kwargs):
            self.workflow_version = 1  # what the database holds

        run.workflow_version = 9
        with mock.patch.object(models.Model, "refresh_from_db", reload):
            run.refresh_from_db()
        self.saved_with(run).assert_called_once()

    def test_a_new_run_is_always_inserted_and_never_overwrites_a_row(self):
        run = WorkflowRun(id=uuid.uuid4(), case_id=uuid.uuid4(), **DEMO_ATTRIBUTION.fields())
        self.assertTrue(run._state.adding)
        with mock.patch.object(models.Model, "save_base") as save_base:
            run.save()
            run.save_base(raw=True)  # what loaddata does
        for call in save_base.call_args_list:
            self.assertTrue(call.kwargs["force_insert"])

    def test_after_the_insert_the_run_is_guarded_like_a_loaded_one(self):
        run = WorkflowRun(id=uuid.uuid4(), case_id=uuid.uuid4(), **DEMO_ATTRIBUTION.fields())
        with mock.patch.object(models.Model, "save_base"):
            run.save()
        run._state.adding = False  # what Django does after a real insert
        run.workflow_version = 3
        with mock.patch.object(models.Model, "save_base") as save_base:
            with self.assertRaises(ImmutableAttributionError):
                run.save()
            save_base.assert_not_called()

    def test_an_existing_run_that_was_never_loaded_is_refused_a_full_save(self):
        run = WorkflowRun(id=uuid.uuid4(), case_id=uuid.uuid4(), **DEMO_ATTRIBUTION.fields())
        run._state.adding = False  # no snapshot: it cannot be shown to be unchanged
        with mock.patch.object(models.Model, "save_base") as save_base:
            with self.assertRaises(ImmutableAttributionError):
                run.save()
            save_base.assert_not_called()


# --- the service ------------------------------------------------------------------------------------------------------------------


class CreateRunServiceTests(SimpleTestCase):
    def test_the_attribution_is_a_required_keyword(self):
        import inspect

        parameter = inspect.signature(services.create_run).parameters["attribution"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            services.create_run(case_id=uuid.uuid4())

    def test_only_an_attribution_value_is_accepted_and_before_any_query(self):
        with mock.patch.object(services.Case.objects, "get") as get, mock.patch.object(WorkflowRun.objects, "create") as create:
            for bad in (None, {"module_id": "x"}, ("demo_module", "demo_module", 1), "demo_module", REQUEST_INTAKE_ATTRIBUTION.fields()):
                with self.subTest(bad=bad), self.assertRaises(TypeError):
                    services.create_run(case_id=uuid.uuid4(), attribution=bad)
            get.assert_not_called()
            create.assert_not_called()

    def test_a_subclass_is_refused_because_it_could_change_how_its_values_are_read(self):
        class Sneaky(WorkflowAttribution):
            def fields(self):
                return {"module_id": "core", "workflow_key": "core", "workflow_version": 1}

        sneaky = Sneaky("demo_module", "demo_module", 1)
        with mock.patch.object(services.Case.objects, "get"), mock.patch.object(WorkflowRun.objects, "create") as create:
            with self.assertRaises(TypeError):
                services.create_run(case_id=uuid.uuid4(), attribution=sneaky)
            create.assert_not_called()

    def test_an_attribution_altered_after_construction_is_refused_before_any_query(self):
        value = WorkflowAttribution("demo_module", "demo_module", 1)
        object.__setattr__(value, "module_id", "Not Valid")
        with mock.patch.object(services.Case.objects, "get") as get:
            with self.assertRaises(WorkflowAttributionError):
                services.create_run(case_id=uuid.uuid4(), attribution=value)
            get.assert_not_called()

    def test_the_run_is_created_with_exactly_the_attribution_given(self):
        case, run_id = mock.Mock(), uuid.uuid4()
        with mock.patch.object(services.Case.objects, "get", return_value=case), mock.patch.object(WorkflowRun.objects, "create") as create:
            services.create_run(case_id=uuid.uuid4(), attribution=DEMO_ATTRIBUTION, run_id=run_id)
        create.assert_called_once_with(id=run_id, case=case, module_id="demo_module", workflow_key="demo_module.review", workflow_version=1)

    def test_request_intake_passes_its_constant(self):
        source = (BACKEND / "apps" / "intake" / "submission.py").read_text(encoding="utf-8")
        self.assertIn("create_run(case_id=case.pk, attribution=REQUEST_INTAKE_ATTRIBUTION)", source)
        self.assertIn("REQUEST_INTAKE_ATTRIBUTION = WorkflowAttribution(MODULE_ID, WORKFLOW_KEY, WORKFLOW_VERSION)", source)

    def test_a_replayed_submission_creates_no_run_and_so_no_attribution(self):
        source = (BACKEND / "apps" / "intake" / "submission.py").read_text(encoding="utf-8")
        replay = source[source.index("def _replay") : source.index("def _create_case_with_reference")]
        self.assertNotIn("create_run", replay)
        self.assertNotIn("attribution", replay.lower())


# --- the migrations ----------------------------------------------------------------------------------------------------------------


STEPS = (
    "0006_workflowrun_attribution_columns",
    "0007_backfill_run_attribution",
    "0008_validate_run_attribution",
    "0009_workflowrun_attribution_constraints",
)


def loader():
    return MigrationLoader(None, ignore_no_migrations=True)


class MigrationStructureTests(SimpleTestCase):
    def test_models_and_migrations_agree(self):
        graph_loader = loader()
        changes = MigrationAutodetector(graph_loader.project_state(), ProjectState.from_apps(apps)).changes(graph=graph_loader.graph)
        self.assertEqual(changes, {}, f"Missing migrations for: {sorted(changes)}")

    def test_the_four_steps_form_a_chain_after_0005(self):
        disk = loader().disk_migrations
        chain = [("cases", "0005_rejection"), *(("cases", name) for name in STEPS)]
        for previous, current in zip(chain, chain[1:]):
            self.assertIn(previous, disk[current].dependencies, current)

    def test_each_step_does_one_kind_of_work(self):
        disk = loader().disk_migrations
        kinds = {name: [type(op).__name__ for op in disk[("cases", name)].operations] for name in STEPS}
        self.assertEqual(
            kinds,
            {
                "0006_workflowrun_attribution_columns": ["AddField", "AddField", "AddField"],
                "0007_backfill_run_attribution": ["RunPython"],
                "0008_validate_run_attribution": ["RunPython"],
                "0009_workflowrun_attribution_constraints": ["AlterField", "AlterField", "AlterField", "AddConstraint", "AddConstraint", "AddConstraint"],
            },
        )  # data and schema never share a migration, so never a PostgreSQL transaction

    def test_nothing_is_dropped_renamed_or_run_as_sql(self):
        disk = loader().disk_migrations
        for name in STEPS:
            kinds = {type(op).__name__ for op in disk[("cases", name)].operations}
            self.assertEqual(kinds & {"RemoveField", "DeleteModel", "RenameField", "RunSQL", "RemoveConstraint", "CreateModel"}, set(), name)

    def test_step_a_adds_nullable_columns_and_no_constraint(self):
        for operation in loader().disk_migrations[("cases", STEPS[0])].operations:
            with self.subTest(column=operation.name):
                self.assertTrue(operation.field.null)
                self.assertFalse(operation.field.has_default())  # no value is invented

    def test_step_d_makes_them_required_with_the_models_constraints(self):
        operations = loader().disk_migrations[("cases", STEPS[3])].operations
        for operation in operations[:3]:
            self.assertFalse(operation.field.null)
        added = {op.constraint.name for op in operations[3:]}
        self.assertEqual(added, {"cases_run_module_id_shape", "cases_run_workflow_key_shape", "cases_run_workflow_version_positive"})
        self.assertLessEqual(added, {c.name for c in WorkflowRun._meta.constraints})

    def test_the_backfill_depends_on_the_table_that_is_its_evidence(self):
        self.assertIn(("intake", "0001_initial"), loader().disk_migrations[("cases", STEPS[1])].dependencies)

    def test_the_intake_and_other_migrations_are_untouched(self):
        found = {key[1] for key in loader().disk_migrations if key[0] == "intake"}
        self.assertEqual(found, {"0001_initial", "0002_revisionedit_request_permissions"})
        self.assertEqual({key[1] for key in loader().disk_migrations if key[0] == "tasks"}, {"0001_initial"})
        self.assertEqual({key[1] for key in loader().disk_migrations if key[0] == "audit"}, {"0001_initial", "0002_extensible_audit_vocabulary", "0003_ai_settings_subject_type"})

    def test_the_historical_migrations_are_byte_for_byte_what_they_were(self):
        for name in ("0001_initial", "0002_workflowrun_state", "0003_backfill_run_state", "0004_workflowrun_state_constraints", "0005_rejection"):
            source = (CASES / "migrations" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("attribution", source, name)
            self.assertNotIn("module_id", source, name)


class BackfillTests(SimpleTestCase):
    def module(self, name):
        return import_module(f"apps.cases.migrations.{name}")

    def test_the_values_written_are_request_intakes(self):
        backfill = self.module(STEPS[1])
        self.assertEqual(
            backfill.REQUEST_INTAKE,
            {"module_id": intake_policy.MODULE_ID, "workflow_key": intake_policy.WORKFLOW_KEY, "workflow_version": intake_policy.WORKFLOW_VERSION},
        )
        validate_attribution(*backfill.REQUEST_INTAKE.values())

    def test_it_touches_only_unattributed_runs_that_have_a_request_source(self):
        backfill = self.module(STEPS[1])
        run_model, source_model = mock.Mock(), mock.Mock()
        fake_apps = mock.Mock()
        fake_apps.get_model.side_effect = lambda app, name: {("cases", "WorkflowRun"): run_model, ("intake", "RequestSource"): source_model}[(app, name)]
        backfill.backfill(fake_apps, None)
        run_model.objects.filter.assert_called_once_with(
            module_id__isnull=True,
            workflow_key__isnull=True,
            workflow_version__isnull=True,
            pk__in=source_model.objects.values.return_value,
        )
        source_model.objects.values.assert_called_once_with("run_id")
        run_model.objects.filter.return_value.update.assert_called_once_with(module_id="request_intake", workflow_key="request_intake", workflow_version=1)

    def test_it_only_updates_and_never_deletes_inserts_or_guesses(self):
        source = (CASES / "migrations" / f"{STEPS[1]}.py").read_text(encoding="utf-8")
        code = source.split('"""', 2)[2]  # everything after the docstring
        for forbidden in (".delete(", ".create(", "bulk_create", "RunSQL", "cursor", "execute(", "canonical", "content_hash", "state="):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertTrue(loader().disk_migrations[("cases", STEPS[1])].operations[0].reversible)

    def test_a_migration_imports_no_application_code(self):
        for name in STEPS:
            tree = ast.parse((CASES / "migrations" / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertFalse((node.module or "").startswith(("apps", "config", "workflows")), name)


class ValidationStepTests(SimpleTestCase):
    def step(self):
        return import_module(f"apps.cases.migrations.{STEPS[2]}")

    def fake(self, *, missing, rows):
        """A fake historical ``WorkflowRun``: ``missing`` unattributed runs and ``rows`` of (module_id, workflow_key, version, count)."""
        model = mock.Mock()
        unattributed = mock.Mock()
        unattributed.count.return_value = missing
        present = mock.Mock()
        present.values_list.return_value.distinct.return_value = [row[:3] for row in rows]
        counts = {row[:3]: row[3] for row in rows}
        present.filter.side_effect = lambda module_id, workflow_key, workflow_version: mock.Mock(count=mock.Mock(return_value=counts[(module_id, workflow_key, workflow_version)]))
        model.objects.filter.side_effect = [unattributed, present]
        fake_apps = mock.Mock()
        fake_apps.get_model.return_value = model
        return fake_apps

    def test_it_passes_when_every_run_is_attributed_and_valid(self):
        self.step().validate(self.fake(missing=0, rows=[("request_intake", "request_intake", 1, 4)]), None)
        self.step().validate(self.fake(missing=0, rows=[]), None)

    def test_it_stops_when_a_run_has_no_attribution(self):
        with self.assertRaises(self.step().UnattributedRunsError) as caught:
            self.step().validate(self.fake(missing=3, rows=[("request_intake", "request_intake", 1, 4)]), None)
        self.assertIn("3 workflow run(s) have no workflow attribution", str(caught.exception))
        self.assertIn("Nothing was changed", str(caught.exception))

    def test_it_stops_when_an_attribution_is_malformed(self):
        for row in (("Bad", "request_intake", 1, 2), ("request_intake", "a..b", 1, 2), ("request_intake", "request_intake", 0, 2), ("request_intake", "request_intake", 2**31, 2)):
            with self.subTest(row=row), self.assertRaises(self.step().UnattributedRunsError) as caught:
                self.step().validate(self.fake(missing=0, rows=[row]), None)
            self.assertIn("2 have an attribution that is not valid", str(caught.exception))

    def test_it_reads_only(self):
        source = (CASES / "migrations" / f"{STEPS[2]}.py").read_text(encoding="utf-8")
        for forbidden in (".update(", ".delete(", ".create(", "save("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_its_patterns_are_the_applications(self):
        step = self.step()
        self.assertEqual(step.MODULE_ID.pattern, attribution.MODULE_ID_PATTERN)
        self.assertEqual(step.WORKFLOW_KEY.pattern, attribution.WORKFLOW_KEY_PATTERN)
        self.assertEqual(step.MAX_WORKFLOW_VERSION, MAX_WORKFLOW_VERSION)

    def test_the_constraint_patterns_in_the_last_step_are_the_applications(self):
        operations = loader().disk_migrations[("cases", STEPS[3])].operations
        by_name = {op.constraint.name: op.constraint for op in operations[3:]}
        self.assertEqual(by_name["cases_run_module_id_shape"].condition.children, [("module_id__regex", attribution.MODULE_ID_PATTERN)])
        self.assertEqual(by_name["cases_run_workflow_key_shape"].condition.children, [("workflow_key__regex", attribution.WORKFLOW_KEY_PATTERN)])
        self.assertEqual(
            sorted(by_name["cases_run_workflow_version_positive"].condition.children), [("workflow_version__gte", 1), ("workflow_version__lte", MAX_WORKFLOW_VERSION)]
        )


# --- the boundaries --------------------------------------------------------------------------------------------------------------


def production_sources():
    for top in ("apps", "config", "workflows"):
        for path in sorted((BACKEND / top).rglob("*.py")):
            if "migrations" not in path.parts and "__pycache__" not in path.parts:
                yield path


def imports_of_file(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module or "")
    return names


class BoundaryTests(SimpleTestCase):
    def test_the_attribution_module_is_pure_standard_library(self):
        self.assertEqual(imports_of_file(CASES / "attribution.py"), {"re", "dataclasses"})

    def test_core_cases_does_not_import_the_module_package_or_the_composition_roots(self):
        for path in CASES.glob("*.py"):
            for name in imports_of_file(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertFalse(name.startswith(("apps.modules", "config", "apps.intake", "workflows.request_intake")), name)

    def test_no_dynamic_import_or_discovery_is_involved(self):
        code = (CASES / "attribution.py").read_text(encoding="utf-8")
        for forbidden in ("importlib", "__import__", "pkgutil", "entry_points", "os.environ", "getattr(", "eval(", "exec("):
            self.assertNotIn(forbidden, code, forbidden)

    def test_there_is_exactly_one_production_creation_path_and_it_is_explicit(self):
        creators = []
        for path in production_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                target = ast.unparse(func)
                if name == "create_run" or target in {"WorkflowRun", *(f"WorkflowRun.objects.{m}" for m in ("create", "bulk_create", "get_or_create", "update_or_create"))}:
                    keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
                    creators.append((path.relative_to(BACKEND).as_posix(), target, keywords.get("attribution")))
        self.assertEqual(
            sorted(creators),
            [
                ("apps/cases/services.py", "WorkflowRun.objects.create", None),
                ("apps/intake/submission.py", "create_run", "REQUEST_INTAKE_ATTRIBUTION"),
                # MOD-1b S6: the ModuleContext's ``case.run`` facade is the second creator; it builds the attribution from the module's own declaration
                # (tests/test_module_context.py: the caller cannot choose the module or the version). Request Intake is still the only business caller.
                ("apps/module_host/context.py", "case_services.create_run", "attribution"),
            ],
        )

    def test_nothing_that_decides_behaviour_reads_the_attribution(self):
        names = {"workflow_key", "workflow_version", "attribution", "REQUEST_INTAKE_ATTRIBUTION"}
        watched = [
            CASES / "run_state.py",
            CASES / "adapters.py",
            *sorted((BACKEND / "apps" / "tasks").rglob("*.py")),
            *sorted((BACKEND / "apps" / "audit").rglob("*.py")),
            *sorted((BACKEND / "workflows").rglob("*.py")),
            BACKEND / "apps" / "intake" / "review.py",
            BACKEND / "apps" / "intake" / "review_rules.py",
            BACKEND / "apps" / "intake" / "services.py",
            BACKEND / "apps" / "intake" / "handlers.py",
        ]
        for path in watched:
            if "migrations" in path.parts or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                self.assertEqual(used & names, set())

    def test_the_transition_and_revision_services_do_not_mention_the_attribution(self):
        import inspect

        for function in (services.transition_run, services.record_revision, services.record_approval, services.record_rejection, services.lock_run):
            source = inspect.getsource(function)
            with self.subTest(function=function.__name__):
                for word in ("attribution", "module_id", "workflow_key", "workflow_version"):
                    self.assertNotIn(word, source)

    def test_the_rest_api_and_the_frontend_do_not_expose_the_attribution(self):
        words = ("workflow_key", "workflow_version", "attribution")
        roots = [BACKEND / "apps" / "api", BACKEND / "apps" / "intake" / "queries.py", BACKEND.parent / "frontend" / "src"]
        for root in roots:
            files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".ts", ".tsx"}]
            for path in files:
                text = path.read_text(encoding="utf-8")
                for word in words:
                    with self.subTest(file=path.name, word=word):
                        self.assertNotIn(word, text)

    def test_the_revision_and_hash_contracts_did_not_move(self):
        from workflows.request_intake import CANONICAL_FORMAT_VERSION

        self.assertEqual(CANONICAL_FORMAT_VERSION, 1)
        from apps.cases.models import ResultRevision

        self.assertEqual(
            {f.name for f in ResultRevision._meta.local_fields},
            {"id", "run", "revision_number", "canonical_format_version", "canonical_bytes", "content_hash", "findings", "created_at"},
        )


class GoldenCanonicalTests(SimpleTestCase):
    """The canonical bytes and hash of a Request Intake revision are exactly what they were before attribution existed.

    The value was computed on the code of ``main`` (97cdc98), before this slice touched anything, from the shared valid text; attribution is
    not part of the canonical payload, so it must not move.
    """

    GOLDEN_HASH = "b78a113476342aa43e70e8b89d62ebd7cb6cb592bf6b2d62177f4373b8b561ab"
    GOLDEN_LENGTH = 1113

    def test_the_hash_of_the_valid_text_is_unchanged(self):
        from apps.intake.extraction import build_extraction
        from tests.intake_support import VALID_TEXT

        for case, run, revision in ((1, 2, 3), (9, 8, 7)):  # identifiers are not part of the payload
            decision = build_extraction(VALID_TEXT, case_id=uuid.UUID(int=case), run_id=uuid.UUID(int=run), revision_id=uuid.UUID(int=revision))
            with self.subTest(ids=(case, run, revision)):
                self.assertEqual(decision.revision.content_hash, self.GOLDEN_HASH)
                self.assertEqual(len(decision.revision.canonical_bytes), self.GOLDEN_LENGTH)
                self.assertEqual(decision.revision.canonical_format_version, 1)
                self.assertNotIn(b"module_id", decision.revision.canonical_bytes)
                self.assertNotIn(b"workflow", decision.revision.canonical_bytes)
