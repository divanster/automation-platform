"""DB-free tests for the worker composition, the handler boundaries, the models and the migrations.

Nothing here opens a database connection (``SimpleTestCase`` refuses queries). The behaviour that needs
PostgreSQL (atomic submission, locking, idempotent retry, reconciliation) is in
``tests/test_intake_postgres.py``, which is NOT run in the development VM.
"""

import ast
import uuid
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from django.apps import apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase

from apps.cases.errors import RecordNotFoundError
from apps.cases.models import ImmutableRecord, ResultRevision, WorkflowRun
from apps.intake import handlers as intake_handlers
from apps.intake import registry as intake_registry
from apps.intake import services
from apps.intake.errors import RequestSourceMissingError, UnsupportedDraftError
from apps.intake.models import ExtractionOutcome, RequestSource, StepTask
from apps.intake.policy import KIND_EXTRACT
from apps.tasks import handlers as task_handlers
from apps.tasks import worker
from apps.tasks.models import Task
from apps.tasks.policy import (
    MAX_RESULT_BYTES,
    FailureCode,
    InvalidPayloadError,
    TaskPermanentError,
    TaskRetryableError,
    canonical_json,
    classify_exception,
)
from config import task_registry
from tests.test_request_intake_boundaries import imported_modules
from workflows.request_intake import SerializationSafetyError

BACKEND_DIR = Path(__file__).resolve().parent.parent
INTAKE_DIR = BACKEND_DIR / "apps" / "intake"
TASKS_DIR = BACKEND_DIR / "apps" / "tasks"
RUN_ID = uuid.UUID("1234abcd-12ab-56cd-92ef-5678abcd1234")


def sources(directory):
    return sorted(p for p in directory.rglob("*.py") if "migrations" not in p.parts and "__pycache__" not in p.parts)


class RegistryCompositionTests(SimpleTestCase):
    def test_the_production_registry_is_the_queue_allowlist_plus_the_intake_handler(self):
        self.assertEqual(set(intake_registry.PRODUCTION_REGISTRY), set(task_handlers.HANDLERS) | {KIND_EXTRACT})
        self.assertEqual(set(intake_registry.PRODUCTION_REGISTRY), {"test.add_numbers", "request_intake.extract"})

    def test_the_queue_allowlist_itself_is_unchanged(self):
        self.assertEqual(set(task_handlers.HANDLERS), {"test.add_numbers"})

    def test_the_registry_is_read_only(self):
        self.assertIsInstance(intake_registry.PRODUCTION_REGISTRY, MappingProxyType)
        with self.assertRaises(TypeError):
            intake_registry.PRODUCTION_REGISTRY["evil"] = intake_handlers.EXTRACT_HANDLER  # type: ignore[index]
        with self.assertRaises(TypeError):
            intake_registry.PRODUCTION_REGISTRY[KIND_EXTRACT] = task_handlers.HANDLERS["test.add_numbers"]  # type: ignore[index]

    def test_every_entry_is_a_task_handler_under_its_own_kind(self):
        for kind, handler in intake_registry.PRODUCTION_REGISTRY.items():
            self.assertIsInstance(handler, task_handlers.TaskHandler)
            self.assertEqual(handler.kind, kind)

    def test_the_intake_handler_is_not_a_test_handler_and_the_test_handler_is(self):
        self.assertFalse(intake_registry.PRODUCTION_REGISTRY[KIND_EXTRACT].is_test_handler)
        self.assertTrue(intake_registry.PRODUCTION_REGISTRY["test.add_numbers"].is_test_handler)

    def test_unknown_kinds_are_not_found(self):
        for kind in ("request_intake.extract ", "REQUEST_INTAKE.EXTRACT", "request_intake", "os.system", "", None, 5):
            with self.subTest(kind=kind):
                self.assertIsNone(task_handlers.get_handler(kind, intake_registry.PRODUCTION_REGISTRY))

    def test_the_composition_root_assembles_exactly_this_registry_and_the_reconciler(self):
        self.assertIs(task_registry.COMPOSITION.registry, intake_registry.PRODUCTION_REGISTRY)
        self.assertIs(task_registry.COMPOSITION.after_iteration, services.reconcile_after_iteration)
        self.assertIsInstance(task_registry.COMPOSITION, worker.Composition)

    def test_the_composition_is_valid_for_the_worker(self):
        previous = worker._composition
        self.addCleanup(setattr, worker, "_composition", previous)
        worker.install_composition(task_registry.COMPOSITION)
        self.assertIs(worker._composition, task_registry.COMPOSITION)


class InstallCompositionTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(setattr, worker, "_composition", worker._composition)

    def test_a_bad_composition_is_refused_and_nothing_changes(self):
        before = worker._composition
        good = task_handlers.HANDLERS["test.add_numbers"]
        bad = [
            "not a composition",
            worker.Composition(registry={}),
            worker.Composition(registry=[good]),
            worker.Composition(registry={"x": good}),  # registered under another kind
            worker.Composition(registry={"test.add_numbers": object()}),
            worker.Composition(after_iteration="not callable"),
        ]
        for composition in bad:
            with self.subTest(composition=repr(composition)[:50]), self.assertRaises(TypeError):
                worker.install_composition(composition)
        self.assertIs(worker._composition, before)

    def test_without_a_composition_the_default_allowlist_is_used(self):
        worker.install_composition(worker.Composition())
        with mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(worker, "claim_next_task", return_value=None):
            self.assertEqual(worker.process_one(worker_id="w", lease_seconds=60).status, worker.ProcessStatus.IDLE)

    def claimed(self, kind):
        return mock.Mock(kind=kind, task_id=uuid.uuid4(), attempt_number=1, payload={"values": [1]}, lease_token=uuid.uuid4())

    def test_the_installed_registry_is_used_when_none_is_passed(self):
        seen = []
        handler = task_handlers.TaskHandler(kind="fake.kind", validate=lambda p: p, run=lambda p: seen.append(p) or {"ok": 1}, is_test_handler=True)
        worker.install_composition(worker.Composition(registry=MappingProxyType({"fake.kind": handler})))
        claimed = self.claimed("fake.kind")
        with (
            mock.patch.object(worker, "recover_expired_leases"),
            mock.patch.object(worker, "claim_next_task", return_value=claimed),
            mock.patch.object(worker, "complete_task") as complete,
        ):
            outcome = worker.process_one(worker_id="w", lease_seconds=60)
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(seen, [{"values": [1]}])
        complete.assert_called_once()

    def test_an_explicit_registry_wins_over_the_installed_one(self):
        installed = task_handlers.TaskHandler(kind="fake.kind", validate=lambda p: p, run=lambda p: {"from": "installed"}, is_test_handler=True)
        explicit = task_handlers.TaskHandler(kind="fake.kind", validate=lambda p: p, run=lambda p: {"from": "explicit"}, is_test_handler=True)
        worker.install_composition(worker.Composition(registry=MappingProxyType({"fake.kind": installed})))
        with (
            mock.patch.object(worker, "recover_expired_leases"),
            mock.patch.object(worker, "claim_next_task", return_value=self.claimed("fake.kind")),
            mock.patch.object(worker, "complete_task") as complete,
        ):
            worker.process_one(worker_id="w", lease_seconds=60, registry={"fake.kind": explicit})
        self.assertEqual(complete.call_args.kwargs["result"], {"from": "explicit"})

    def test_a_kind_outside_the_installed_registry_is_refused_as_unknown(self):
        worker.install_composition(worker.Composition(registry=MappingProxyType({"test.add_numbers": task_handlers.HANDLERS["test.add_numbers"]})))
        with (
            mock.patch.object(worker, "recover_expired_leases"),
            mock.patch.object(worker, "claim_next_task", return_value=self.claimed("request_intake.extract")),
            mock.patch.object(worker, "fail_task", return_value=mock.Mock(state=worker.TaskState.FAILED)) as fail,
        ):
            outcome = worker.process_one(worker_id="w", lease_seconds=60)
        self.assertEqual(outcome.status, worker.ProcessStatus.FAILED)
        self.assertEqual(fail.call_args.kwargs["failure"].code, FailureCode.UNKNOWN_KIND)

    def test_the_hook_runs_after_every_iteration_idle_or_not(self):
        calls = []
        worker.install_composition(worker.Composition(after_iteration=lambda: calls.append(1)))
        with mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(worker, "claim_next_task", return_value=None):
            worker.process_one(worker_id="w", lease_seconds=60)
            worker.process_one(worker_id="w", lease_seconds=60)
        self.assertEqual(calls, [1, 1])

    def test_the_hook_runs_after_the_outcome_is_recorded(self):
        order = []
        worker.install_composition(worker.Composition(after_iteration=lambda: order.append("hook")))
        claimed = self.claimed("test.add_numbers")
        with (
            mock.patch.object(worker, "recover_expired_leases"),
            mock.patch.object(worker, "claim_next_task", return_value=claimed),
            mock.patch.object(worker, "complete_task", side_effect=lambda **kw: order.append("complete")),
        ):
            worker.process_one(worker_id="w", lease_seconds=60)
        self.assertEqual(order, ["complete", "hook"])

    def test_a_failing_hook_does_not_stop_the_worker_and_only_its_class_is_logged(self):
        def hook():
            raise RuntimeError("SECRET detail")

        worker.install_composition(worker.Composition(after_iteration=hook))
        with (
            mock.patch.object(worker, "recover_expired_leases"),
            mock.patch.object(worker, "claim_next_task", return_value=None),
            self.assertLogs("apps.tasks.worker", level="ERROR") as logs,
        ):
            outcome = worker.process_one(worker_id="w", lease_seconds=60)
        self.assertEqual(outcome.status, worker.ProcessStatus.IDLE)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("SECRET", "".join(logs.output))

    def test_a_database_error_in_the_hook_reaches_the_worker_loop(self):
        from django.db import OperationalError

        def hook():
            raise OperationalError("connection lost")

        worker.install_composition(worker.Composition(after_iteration=hook))
        with mock.patch.object(worker, "recover_expired_leases"), mock.patch.object(worker, "claim_next_task", return_value=None):
            with self.assertRaises(OperationalError):
                worker.process_one(worker_id="w", lease_seconds=60)


class RunWorkerInstallsTheCompositionTests(SimpleTestCase):
    def test_the_command_installs_the_composition_root_before_working(self):
        import io

        from django.core.management import call_command

        self.addCleanup(setattr, worker, "_composition", worker._composition)
        seen = []
        outcome = worker.ProcessOutcome(worker.ProcessStatus.IDLE)

        def fake_process(**kwargs):
            seen.append(worker._composition)
            return outcome

        with mock.patch("apps.tasks.management.commands.runworker.connections"), mock.patch.object(worker, "process_one", side_effect=fake_process):
            call_command("runworker", "--once", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(seen, [task_registry.COMPOSITION])


class HandlerBoundaryTests(SimpleTestCase):
    HANDLER = intake_handlers.EXTRACT_HANDLER

    def test_validate_accepts_only_the_run_id_payload(self):
        self.assertEqual(self.HANDLER.validate({"run_id": str(RUN_ID)}), {"run_id": str(RUN_ID)})
        for payload in ({}, {"run_id": str(RUN_ID), "text": "Requester: Jane"}, {"text": "x"}, {"run_id": 5}, None, "x"):
            with self.subTest(payload=repr(payload)), self.assertRaises(InvalidPayloadError):
                self.HANDLER.validate(payload)

    def test_the_validation_error_is_a_permanent_invalid_payload_failure(self):
        try:
            self.HANDLER.validate({"run_id": "SECRET"})
        except InvalidPayloadError as error:
            self.assertNotIn("SECRET", str(error))
            failure = classify_exception(error)
            self.assertEqual((failure.code, failure.retryable), (FailureCode.INVALID_PAYLOAD, False))
        else:
            self.fail("not refused")

    def test_run_passes_the_run_id_as_a_uuid_and_returns_the_task_result(self):
        summary = services.ExtractionSummary("valid", uuid.UUID(int=1), uuid.UUID(int=2), True)
        with mock.patch.object(intake_handlers, "run_extraction", return_value=summary) as run:
            result = self.HANDLER.run({"run_id": str(RUN_ID)})
        run.assert_called_once_with(RUN_ID)
        self.assertEqual(result, {"status": "valid", "outcome_id": str(uuid.UUID(int=1)), "revision_id": str(uuid.UUID(int=2))})

    def test_permanent_errors_map_to_a_rejected_permanent_failure_without_the_original_message(self):
        for error in (
            RecordNotFoundError("The run does not exist."),
            RequestSourceMissingError("SECRET"),
            SerializationSafetyError("decimal_expansion", "items[0].quantity"),
            UnsupportedDraftError("SECRET"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(intake_handlers, "run_extraction", side_effect=error):
                    with self.assertRaises(TaskPermanentError) as caught:
                        self.HANDLER.run({"run_id": str(RUN_ID)})
                self.assertEqual(caught.exception.code, FailureCode.REJECTED)
                self.assertNotIn("SECRET", str(caught.exception))
                failure = classify_exception(caught.exception)
                self.assertEqual((failure.code, failure.retryable), (FailureCode.REJECTED, False))

    def test_unexpected_errors_propagate_and_are_retryable_within_the_attempt_limit(self):
        for error in (RuntimeError("boom"), ValueError("boom"), OSError("boom")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(intake_handlers, "run_extraction", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.HANDLER.run({"run_id": str(RUN_ID)})
                failure = classify_exception(error)
                self.assertEqual((failure.code, failure.retryable), (FailureCode.HANDLER_ERROR, True))

    def test_a_retryable_error_stays_retryable(self):
        failure = classify_exception(TaskRetryableError())
        self.assertTrue(failure.retryable)

    def test_the_result_is_small_free_of_business_content_and_the_same_every_time(self):
        for summary in (
            services.ExtractionSummary("valid", uuid.uuid4(), uuid.uuid4(), True),
            services.ExtractionSummary("invalid", uuid.uuid4(), None, False),
            services.ExtractionSummary("discarded", None, None, False, run_state="cancelled"),
        ):
            result = summary.as_task_result()
            canonical_json(result, max_bytes=MAX_RESULT_BYTES, what="result")
            self.assertLess(len(str(result)), 300)
            self.assertEqual(set(result) - {"status", "outcome_id", "revision_id", "run_state"}, set())
        again = services.ExtractionSummary("valid", uuid.UUID(int=1), uuid.UUID(int=2), False)
        first = services.ExtractionSummary("valid", uuid.UUID(int=1), uuid.UUID(int=2), True)
        self.assertEqual(first.as_task_result(), again.as_task_result())  # a repeat records the same result

    def test_a_discarded_result_names_the_state_only(self):
        result = services.ExtractionSummary("discarded", None, None, False, run_state="cancelled").as_task_result()
        self.assertEqual(result, {"status": "discarded", "run_state": "cancelled"})


class SourceBoundaryTests(SimpleTestCase):
    def test_the_app_has_the_expected_source_files(self):
        names = {p.relative_to(INTAKE_DIR).as_posix() for p in sources(INTAKE_DIR)}
        self.assertEqual(
            names,
            {
                "__init__.py", "access.py", "apps.py", "authorization.py", "draft_json.py", "editing.py", "errors.py", "extraction.py",
                "handlers.py", "models.py", "policy.py", "queries.py", "registry.py", "review.py", "review_rules.py", "services.py", "submission.py",
            },
        )  # fmt: skip

    def test_the_policy_module_is_standard_library_only(self):
        self.assertEqual(imported_modules(INTAKE_DIR / "policy.py") - {"re", "uuid", "datetime"}, set())

    def test_the_pure_modules_import_no_django_and_no_database_code(self):
        for name in ("draft_json.py", "extraction.py", "errors.py"):
            with self.subTest(module=name):
                self.assertEqual(imported_modules(INTAKE_DIR / name) & {"django", "psycopg", "rest_framework"}, set())

    def test_no_module_executes_or_imports_code_by_name_or_uses_the_network(self):
        forbidden_imports = {"importlib", "subprocess", "socket", "ssl", "urllib", "http", "requests", "pickle", "marshal", "ctypes", "anthropic", "openai"}
        forbidden_names = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals"}
        for path in sources(INTAKE_DIR):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & forbidden_imports, set())
                self.assertEqual({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} & forbidden_names, set())

    def test_no_second_canonicalization_or_validation_exists_in_the_app(self):
        banned = {"canonicalize_request", "canonical_decimal_string", "validate_request", "dumps", "sha1", "md5", "approve_revision", "check_export_eligibility"}
        for path in sources(INTAKE_DIR):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            # The review service REUSES the domain's approve_revision (it is the call the approval is built on);
            # tests.test_review_boundaries checks that it is imported from the domain and never redefined.
            allowed = {"approve_revision"} if path.name == "review.py" else set()
            with self.subTest(module=path.name):
                self.assertEqual(used & (banned - allowed), set())
                self.assertNotIn("json.dumps", path.read_text(encoding="utf-8"))

    def test_only_the_submission_module_hashes_and_only_the_source_text(self):
        for path in sources(INTAKE_DIR):
            with self.subTest(module=path.name):
                hashes = "hashlib" in imported_modules(path)
                self.assertEqual(hashes, path.name == "submission.py")

    def test_the_queue_is_used_only_through_its_services_models_and_policy(self):
        allowed = {"apps.tasks.services", "apps.tasks.models", "apps.tasks.policy", "apps.tasks.handlers", "apps.tasks.worker"}
        for path in sources(INTAKE_DIR):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("apps.tasks"):
                    self.assertIn(node.module, allowed, path.name)

    def test_the_queue_app_imports_no_business_code(self):
        for path in sources(TASKS_DIR):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertFalse(node.module.startswith(("apps.cases", "apps.intake", "workflows")), f"{path.name}: {node.module}")
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.startswith(("apps.cases", "apps.intake", "workflows")) for a in node.names), path.name)

    def test_the_only_project_import_in_the_queue_app_outside_itself_is_the_composition_root(self):
        found = {}
        for path in sources(TASKS_DIR):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] in {"config", "apps"}:
                    if not (node.module or "").startswith("apps.tasks"):
                        found.setdefault(path.name, set()).add(node.module)
        self.assertEqual(found, {"runworker.py": {"config.task_registry"}})

    def test_the_queue_handlers_module_is_still_standard_library_only(self):
        self.assertEqual(imported_modules(TASKS_DIR / "handlers.py") - {"collections", "dataclasses", "types", "typing"}, set())


class ModelMetadataTests(SimpleTestCase):
    def test_the_app_is_registered_with_a_stable_label(self):
        config = apps.get_app_config("intake")
        self.assertEqual((config.name, config.label), ("apps.intake", "intake"))

    def test_primary_keys_are_uuids_with_a_default(self):
        for model in (RequestSource, StepTask, ExtractionOutcome):
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model._meta.pk, models.UUIDField)
                self.assertIs(model._meta.pk.default, uuid.uuid4)

    def test_every_foreign_key_protects_history(self):
        links = [
            (RequestSource, "run", WorkflowRun), (RequestSource, "submitted_by", None), (StepTask, "run", WorkflowRun), (StepTask, "task", Task),
            (ExtractionOutcome, "run", WorkflowRun), (ExtractionOutcome, "revision", ResultRevision),
        ]  # fmt: skip
        for model, name, target in links:
            with self.subTest(field=f"{model.__name__}.{name}"):
                field = model._meta.get_field(name)
                self.assertIs(field.remote_field.on_delete, models.PROTECT)
                if target is not None:
                    self.assertIs(field.remote_field.model, target)

    def test_one_to_one_links_give_database_uniqueness_per_run(self):
        for model in (RequestSource, ExtractionOutcome):
            with self.subTest(model=model.__name__):
                field = model._meta.get_field("run")
                self.assertIsInstance(field, models.OneToOneField)
                self.assertTrue(field.unique)
        self.assertTrue(StepTask._meta.get_field("task").unique)
        self.assertTrue(ExtractionOutcome._meta.get_field("revision").unique)

    def test_there_is_no_second_run_hierarchy(self):
        self.assertEqual(
            {f.name for f in WorkflowRun._meta.get_fields() if f.is_relation and f.auto_created and not f.concrete} & {"request_source", "extraction_outcome", "step_tasks"},
            {"request_source", "extraction_outcome", "step_tasks"},
        )
        for model in (RequestSource, StepTask, ExtractionOutcome):
            self.assertNotIn("case", {f.name for f in model._meta.get_fields()})  # the case is reached through the run

    def test_the_source_and_the_outcome_are_immutable_records_and_the_link_is_not(self):
        self.assertTrue(issubclass(RequestSource, ImmutableRecord))
        self.assertTrue(issubclass(ExtractionOutcome, ImmutableRecord))
        self.assertFalse(issubclass(StepTask, ImmutableRecord))

    def test_named_constraints(self):
        names = {model.__name__: {c.name for c in model._meta.constraints} for model in (RequestSource, StepTask, ExtractionOutcome)}
        self.assertEqual(
            names,
            {
                "RequestSource": {"intake_source_client_request_unique", "intake_source_hash_is_sha256_hex", "intake_source_size_within_limit"},
                "StepTask": {"intake_steptask_run_step_generation_unique", "intake_steptask_step_valid", "intake_steptask_generation_at_least_1"},
                "ExtractionOutcome": {"intake_outcome_status_valid", "intake_outcome_revision_matches_status"},
            },
        )

    def test_a_step_is_unique_per_run_and_generation(self):
        unique = [c for c in StepTask._meta.constraints if isinstance(c, models.UniqueConstraint)]
        self.assertEqual([tuple(c.fields) for c in unique], [("run", "step", "generation")])

    def test_the_client_request_id_is_unique_per_user_only_when_set(self):
        (constraint,) = [c for c in RequestSource._meta.constraints if isinstance(c, models.UniqueConstraint)]
        self.assertEqual(tuple(constraint.fields), ("submitted_by", "client_request_id"))
        self.assertIsNotNone(constraint.condition)

    def test_the_stored_columns(self):
        self.assertEqual(
            {f.name for f in RequestSource._meta.local_fields},
            {"id", "run", "submitted_by", "text", "text_sha256", "byte_length", "client_request_id", "created_at"},
        )
        self.assertEqual(
            {f.name for f in ExtractionOutcome._meta.local_fields},
            {"id", "run", "status", "revision", "extractor_name", "extractor_version", "draft", "ambiguities", "findings", "created_at"},
        )
        self.assertEqual({f.name for f in StepTask._meta.local_fields}, {"id", "run", "step", "generation", "task", "created_at"})

    def test_the_task_tables_gained_no_run_or_business_column(self):
        self.assertEqual({f.name for f in Task._meta.local_fields} & {"run", "run_id", "case", "revision"}, set())

    def test_the_run_gained_exactly_the_state_columns(self):
        fields = {f.name for f in WorkflowRun._meta.local_fields}
        self.assertEqual(fields, {"id", "case", "current_revision", "state", "failed_step", "created_at"})
        self.assertEqual(WorkflowRun._meta.get_field("state").default, "queued")
        self.assertEqual({c.name for c in WorkflowRun._meta.constraints}, {"cases_run_state_valid", "cases_run_failed_step_valid", "cases_run_failed_step_matches_state"})


class MigrationTests(SimpleTestCase):
    def loader(self):
        return MigrationLoader(None, ignore_no_migrations=True)

    def test_models_match_migrations(self):
        loader = self.loader()
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph)
        self.assertEqual(changes, {}, f"Missing migrations for: {sorted(changes)}")

    def test_the_cases_state_migrations_are_split_into_three_non_destructive_steps(self):
        loader = self.loader()
        ops = {}
        for name in ("0002_workflowrun_state", "0003_backfill_run_state", "0004_workflowrun_state_constraints"):
            migration = loader.disk_migrations[("cases", name)]
            ops[name] = [type(op).__name__ for op in migration.operations]
        self.assertEqual(
            ops,
            {
                "0002_workflowrun_state": ["AddField", "AddField"],
                "0003_backfill_run_state": ["RunPython"],
                "0004_workflowrun_state_constraints": ["AddConstraint", "AddConstraint", "AddConstraint"],
            },
        )  # data and schema changes never share a migration, so never a PostgreSQL transaction

    def test_the_cases_migrations_form_a_chain(self):
        loader = self.loader()
        chain = [("cases", "0001_initial"), ("cases", "0002_workflowrun_state"), ("cases", "0003_backfill_run_state"), ("cases", "0004_workflowrun_state_constraints")]
        for previous, current in zip(chain, chain[1:]):
            self.assertEqual(list(loader.disk_migrations[current].dependencies), [previous])

    def test_no_cases_migration_deletes_or_alters_existing_data_or_columns(self):
        loader = self.loader()
        for name in ("0002_workflowrun_state", "0003_backfill_run_state", "0004_workflowrun_state_constraints"):
            kinds = {type(op).__name__ for op in loader.disk_migrations[("cases", name)].operations}
            self.assertEqual(kinds & {"RemoveField", "DeleteModel", "AlterField", "RenameField", "RunSQL", "RemoveConstraint"}, set(), name)

    def test_new_columns_have_defaults_so_existing_rows_stay_valid(self):
        loader = self.loader()
        for operation in loader.disk_migrations[("cases", "0002_workflowrun_state")].operations:
            self.assertTrue(operation.field.has_default(), operation.name)
            self.assertFalse(operation.field.null)

    def test_the_backfill_only_touches_default_state_rows_that_have_a_revision(self):
        source = (BACKEND_DIR / "apps" / "cases" / "migrations" / "0003_backfill_run_state.py").read_text(encoding="utf-8")
        self.assertIn('filter(state="queued", current_revision__isnull=False).update(state="needs_review")', source)
        self.assertNotIn(".delete(", source)
        operation = self.loader().disk_migrations[("cases", "0003_backfill_run_state")].operations[0]
        self.assertTrue(operation.reversible)

    def test_the_backfill_function_updates_only_the_documented_rows(self):
        from importlib import import_module

        module = import_module("apps.cases.migrations.0003_backfill_run_state")
        historical = mock.Mock()
        fake_apps = mock.Mock()
        fake_apps.get_model.return_value = historical
        module.backfill(fake_apps, None)
        fake_apps.get_model.assert_called_once_with("cases", "WorkflowRun")
        historical.objects.filter.assert_called_once_with(state="queued", current_revision__isnull=False)
        historical.objects.filter.return_value.update.assert_called_once_with(state="needs_review")

    def test_the_intake_migration_is_one_schema_only_initial_migration(self):
        loader = self.loader()
        migration = loader.disk_migrations[("intake", "0001_initial")]
        self.assertTrue(migration.initial)
        self.assertEqual({type(op).__name__ for op in migration.operations}, {"CreateModel"})
        self.assertEqual({d for d in migration.dependencies if d[0] in {"cases", "tasks"}}, {("cases", "0004_workflowrun_state_constraints"), ("tasks", "0001_initial")})
        # 0002 (review services) is checked by tests.test_review_models; 0001 stays exactly as the orchestration slice shipped it.
        self.assertEqual({k[1] for k in loader.disk_migrations if k[0] == "intake"}, {"0001_initial", "0002_revisionedit_request_permissions"})

    def test_the_intake_migration_carries_the_constraints(self):
        migration = self.loader().disk_migrations[("intake", "0001_initial")]
        declared = set()
        for operation in migration.operations:
            declared |= {c.name for c in operation.options.get("constraints", [])}
        self.assertEqual(declared, {c.name for m in (RequestSource, StepTask, ExtractionOutcome) for c in m._meta.constraints})

    def test_the_tasks_migrations_are_untouched(self):
        self.assertEqual({k[1] for k in self.loader().disk_migrations if k[0] == "tasks"}, {"0001_initial"})
