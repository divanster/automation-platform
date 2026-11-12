"""DB-free tests for the queue's schema, constraint logic, migration, and source boundaries.

Nothing here opens a database connection (``SimpleTestCase`` refuses queries). CHECK constraints
are evaluated in Python by a small interpreter for the Q expressions they use, against sample
rows: that tests the LOGIC of the constraints, not PostgreSQL's enforcement of them (unique
indexes, locking and real constraint enforcement are in test_tasks_postgres.py, NOT run in the VM).
This file is ASCII only.
"""

import ast
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.apps import apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.db.models import F, Q
from django.test import SimpleTestCase

from apps.tasks import policy
from apps.tasks.models import Task, TaskAttempt
from tests.test_request_intake_boundaries import imported_modules

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "apps" / "tasks"
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TOKEN = uuid.UUID(int=0xAB)


def eval_q(q, row):
    """Evaluate a Q built from exact / in / isnull / gte / lte / regex lookups and F() against a dict."""
    results = []
    for child in q.children:
        if isinstance(child, Q):
            results.append(eval_q(child, row))
            continue
        lookup, value = child
        field, _, operator = lookup.partition("__")
        operator = operator or "exact"
        if isinstance(value, F):
            value = row[value.name]
        actual = row[field]
        if operator == "exact":
            results.append(actual == value)
        elif operator == "in":
            results.append(actual in value)
        elif operator == "isnull":
            results.append((actual is None) == value)
        elif operator == "gte":
            results.append(actual >= value)
        elif operator == "lte":
            results.append(actual <= value)
        elif operator == "regex":
            results.append(re.search(value, actual) is not None)
        else:
            raise AssertionError(f"the test interpreter does not support the lookup {operator!r}")
    combined = all(results) if q.connector == Q.AND else any(results)
    return (not combined) if q.negated else combined


def violated(model, row):
    return {c.name for c in model._meta.constraints if isinstance(c, models.CheckConstraint) and not eval_q(c.condition, row)}


def task_row(base="queued", **changes):
    state = base
    row = {
        "state": state,
        "lease_token": None,
        "lease_expires_at": None,
        "max_attempts": 3,
        "attempt_count": 0,
        "finished_at": None,
        "failure_code": "",
        "request_fingerprint": "a" * 64,
        "idempotency_key": None,
    }
    if state == "running":
        row.update(lease_token=TOKEN, lease_expires_at=T0 + timedelta(seconds=60), attempt_count=1)
    elif state in ("succeeded", "failed"):
        row.update(finished_at=T0, attempt_count=1)
        if state == "failed":
            row["failure_code"] = "rejected"
    elif state == "cancelled":
        row.update(finished_at=T0)
    row.update(changes)
    return row


def attempt_row(base="running", **changes):
    outcome = base
    row = {"attempt_number": 1, "outcome": outcome, "ended_at": None, "failure_code": ""}
    if outcome != "running":
        row["ended_at"] = T0
    if outcome in ("failed", "abandoned"):
        row["failure_code"] = "handler_error"
    row.update(changes)
    return row


class ModelMetadataTests(SimpleTestCase):
    def test_the_app_is_installed_with_the_expected_label(self):
        self.assertEqual(apps.get_app_config("tasks").name, "apps.tasks")

    def test_task_fields(self):
        names = {f.name for f in Task._meta.get_fields()}
        self.assertEqual(
            names,
            {
                "id", "kind", "idempotency_key", "request_fingerprint", "payload", "state", "available_at", "max_attempts",
                "attempt_count", "lease_token", "lease_expires_at", "result", "failure_code", "created_at", "finished_at", "attempts",
            },
        )  # fmt: skip

    def test_attempt_fields(self):
        self.assertEqual(
            {f.name for f in TaskAttempt._meta.get_fields()},
            {"id", "task", "attempt_number", "worker_id", "lease_token", "started_at", "ended_at", "outcome", "failure_code"},
        )

    def test_identifiers_are_uuids_like_the_existing_models(self):
        for model in (Task, TaskAttempt):
            field = model._meta.pk
            self.assertIsInstance(field, models.UUIDField)
            self.assertIs(field.default, uuid.uuid4)

    def test_timestamps_have_no_auto_now_so_the_clock_is_injectable(self):
        for model, names in ((Task, ("created_at", "available_at", "finished_at", "lease_expires_at")), (TaskAttempt, ("started_at", "ended_at"))):
            for name in names:
                field = model._meta.get_field(name)
                self.assertFalse(field.auto_now or field.auto_now_add, name)

    def test_the_queue_holds_no_business_records(self):
        for model in (Task, TaskAttempt):
            targets = {f.related_model for f in model._meta.get_fields() if f.is_relation}
            self.assertLessEqual({t._meta.label for t in targets}, {"tasks.Task", "tasks.TaskAttempt"})

    def test_deleting_a_task_with_attempts_is_protected(self):
        self.assertIs(TaskAttempt._meta.get_field("task").remote_field.on_delete, models.PROTECT)

    def test_the_state_default_is_queued(self):
        self.assertEqual(Task._meta.get_field("state").default, "queued")
        self.assertEqual(Task._meta.get_field("attempt_count").default, 0)

    def test_the_constraint_names(self):
        self.assertEqual(
            {c.name for c in Task._meta.constraints},
            {
                "tasks_task_idempotency_key_unique",
                "tasks_task_state_valid",
                "tasks_task_lease_matches_state",
                "tasks_task_attempts_within_limit",
                "tasks_task_started_states_have_attempts",
                "tasks_task_finished_at_matches_state",
                "tasks_task_failure_code_matches_state",
                "tasks_task_fingerprint_is_sha256_hex",
            },
        )
        self.assertEqual(
            {c.name for c in TaskAttempt._meta.constraints},
            {
                "tasks_attempt_number_unique_per_task",
                "tasks_attempt_one_running_per_task",
                "tasks_attempt_number_at_least_1",
                "tasks_attempt_outcome_valid",
                "tasks_attempt_ended_at_matches_outcome",
                "tasks_attempt_failure_code_matches_outcome",
            },
        )

    def test_idempotency_uniqueness_is_global_and_ignores_null_keys(self):
        constraint = next(c for c in Task._meta.constraints if c.name == "tasks_task_idempotency_key_unique")
        self.assertEqual(constraint.fields, ("idempotency_key",))  # not (kind, key): the scope is the whole installation
        self.assertEqual(constraint.condition, Q(idempotency_key__isnull=False))

    def test_at_most_one_running_attempt_per_task_is_declared(self):
        constraint = next(c for c in TaskAttempt._meta.constraints if c.name == "tasks_attempt_one_running_per_task")
        self.assertEqual(constraint.fields, ("task",))
        self.assertEqual(constraint.condition, Q(outcome="running"))

    def test_the_claim_and_recovery_indexes_exist(self):
        self.assertEqual({tuple(i.fields) for i in Task._meta.indexes}, {("state", "available_at"), ("state", "lease_expires_at")})


class TaskConstraintLogicTests(SimpleTestCase):
    """The CHECK expressions accept every legitimate state and refuse each inconsistent one."""

    def test_every_legitimate_lifecycle_row_satisfies_all_constraints(self):
        rows = {
            "new": task_row("queued"),
            "requeued after a failed attempt": task_row("queued", attempt_count=1),
            "requeued after the second attempt": task_row("queued", attempt_count=2),
            "running first attempt": task_row("running"),
            "running last attempt": task_row("running", attempt_count=3),
            "succeeded": task_row("succeeded"),
            "failed": task_row("failed"),
            "failed with attempts exhausted": task_row("failed", attempt_count=3, failure_code="lease_expired"),
            "cancelled before any attempt": task_row("cancelled"),
            "cancelled after a retry was queued": task_row("cancelled", attempt_count=1),
            "single attempt allowed": task_row("running", max_attempts=1),
            "largest limit": task_row("queued", max_attempts=10, attempt_count=9),
        }
        for label, row in rows.items():
            with self.subTest(label):
                self.assertEqual(violated(Task, row), set())

    def test_each_inconsistent_row_violates_the_expected_constraint(self):
        cases = [
            ("unknown state", task_row("queued", state="ready"), "tasks_task_state_valid"),
            ("running without a token", task_row("running", lease_token=None), "tasks_task_lease_matches_state"),
            ("running without an expiry", task_row("running", lease_expires_at=None), "tasks_task_lease_matches_state"),
            ("queued holding a token", task_row("queued", lease_token=TOKEN), "tasks_task_lease_matches_state"),
            ("queued holding an expiry", task_row("queued", lease_expires_at=T0), "tasks_task_lease_matches_state"),
            ("succeeded holding a lease", task_row("succeeded", lease_token=TOKEN, lease_expires_at=T0), "tasks_task_lease_matches_state"),
            ("more attempts than allowed", task_row("running", attempt_count=4), "tasks_task_attempts_within_limit"),
            ("a limit of zero", task_row("queued", max_attempts=0), "tasks_task_attempts_within_limit"),
            ("a limit above the maximum", task_row("queued", max_attempts=11), "tasks_task_attempts_within_limit"),
            ("running with no attempt made", task_row("running", attempt_count=0), "tasks_task_started_states_have_attempts"),
            ("succeeded with no attempt made", task_row("succeeded", attempt_count=0), "tasks_task_started_states_have_attempts"),
            ("failed with no attempt made", task_row("failed", attempt_count=0), "tasks_task_started_states_have_attempts"),
            ("succeeded without a finish time", task_row("succeeded", finished_at=None), "tasks_task_finished_at_matches_state"),
            ("cancelled without a finish time", task_row("cancelled", finished_at=None), "tasks_task_finished_at_matches_state"),
            ("queued with a finish time", task_row("queued", finished_at=T0), "tasks_task_finished_at_matches_state"),
            ("running with a finish time", task_row("running", finished_at=T0), "tasks_task_finished_at_matches_state"),
            ("failed without a code", task_row("failed", failure_code=""), "tasks_task_failure_code_matches_state"),
            ("succeeded with a failure code", task_row("succeeded", failure_code="rejected"), "tasks_task_failure_code_matches_state"),
            ("a requeued task with a failure code", task_row("queued", attempt_count=1, failure_code="handler_error"), "tasks_task_failure_code_matches_state"),
            ("a fingerprint that is not SHA-256 hex", task_row("queued", request_fingerprint="xyz"), "tasks_task_fingerprint_is_sha256_hex"),
            ("an upper-case fingerprint", task_row("queued", request_fingerprint="A" * 64), "tasks_task_fingerprint_is_sha256_hex"),
        ]
        for label, row, expected in cases:
            with self.subTest(label):
                self.assertIn(expected, violated(Task, row))

    def test_the_interpreter_would_notice_a_wrong_constraint(self):
        # Guard against an interpreter that accepts everything.
        self.assertNotEqual(violated(Task, task_row("running", lease_token=None)), set())

    def test_the_state_constraint_accepts_exactly_the_policy_states(self):
        for state in policy.TaskState:
            self.assertNotIn("tasks_task_state_valid", violated(Task, task_row("queued", state=state.value)))


class AttemptConstraintLogicTests(SimpleTestCase):
    def test_legitimate_attempt_rows(self):
        for outcome in ("running", "succeeded", "failed", "abandoned"):
            with self.subTest(outcome):
                self.assertEqual(violated(TaskAttempt, attempt_row(outcome)), set())

    def test_inconsistent_attempt_rows(self):
        cases = [
            ("attempt number zero", attempt_row("running", attempt_number=0), "tasks_attempt_number_at_least_1"),
            ("unknown outcome", attempt_row("running", outcome="cancelled"), "tasks_attempt_outcome_valid"),
            ("a running attempt that has ended", attempt_row("running", ended_at=T0), "tasks_attempt_ended_at_matches_outcome"),
            ("a finished attempt without an end time", attempt_row("succeeded", ended_at=None), "tasks_attempt_ended_at_matches_outcome"),
            ("a failed attempt without a code", attempt_row("failed", failure_code=""), "tasks_attempt_failure_code_matches_outcome"),
            ("an abandoned attempt without a code", attempt_row("abandoned", failure_code=""), "tasks_attempt_failure_code_matches_outcome"),
            ("a succeeded attempt with a code", attempt_row("succeeded", failure_code="handler_error"), "tasks_attempt_failure_code_matches_outcome"),
        ]
        for label, row, expected in cases:
            with self.subTest(label):
                self.assertIn(expected, violated(TaskAttempt, row))


class MigrationTests(SimpleTestCase):
    def test_models_match_migrations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph)
        self.assertEqual(changes, {}, f"Missing migrations for: {sorted(changes)}")

    def test_the_initial_migration_uses_only_schema_operations_and_depends_on_nothing(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("tasks", "0001_initial")]
        self.assertTrue(migration.initial)
        self.assertEqual({type(op).__name__ for op in migration.operations}, {"CreateModel"})  # no RunSQL or RunPython
        self.assertEqual(list(migration.dependencies), [])

    def test_the_migration_carries_the_constraints(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("tasks", "0001_initial")]
        declared = set()
        for operation in migration.operations:
            declared |= {c.name for c in operation.options.get("constraints", [])}
        self.assertEqual(declared, {c.name for m in (Task, TaskAttempt) for c in m._meta.constraints})

    def test_the_existing_cases_migration_state_is_untouched(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertIn(("cases", "0001_initial"), loader.disk_migrations)
        self.assertEqual({k for k in loader.disk_migrations if k[0] == "tasks"}, {("tasks", "0001_initial")})


# --- static source boundaries -------------------------------------------------------------------------

PURE_MODULES = ("policy.py", "handlers.py")
PURE_ALLOWED_IMPORTS = {"__future__", "collections", "dataclasses", "datetime", "enum", "hashlib", "json", "random", "re", "types", "typing"}
FORBIDDEN_IMPORTS = {
    "subprocess", "importlib", "socket", "ssl", "urllib", "http", "requests", "ctypes", "pickle", "marshal", "shutil",
    "sqlite3", "ftplib", "smtplib", "anthropic", "openai", "workflows",
}  # fmt: skip
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals"}
FORBIDDEN_OS_ATTRIBUTES = {"system", "popen", "execv", "execl", "execvp", "spawnl", "spawnv", "startfile", "remove", "unlink", "rmdir", "rename"}


def app_sources():
    return sorted(p for p in APP_DIR.rglob("*.py") if "migrations" not in p.parts and "__pycache__" not in p.parts)


class SourceBoundaryTests(SimpleTestCase):
    def test_the_app_has_the_expected_source_files(self):
        names = {p.relative_to(APP_DIR).as_posix() for p in app_sources()}
        self.assertEqual(
            names,
            {
                "__init__.py", "apps.py", "errors.py", "handlers.py", "models.py", "policy.py", "services.py", "worker.py",
                "management/__init__.py", "management/commands/__init__.py", "management/commands/runworker.py",
            },
        )  # fmt: skip

    def test_the_pure_modules_import_only_the_standard_library(self):
        for name in PURE_MODULES:
            with self.subTest(module=name):
                unexpected = imported_modules(APP_DIR / name) - PURE_ALLOWED_IMPORTS
                self.assertEqual(unexpected, set())

    def test_the_pure_modules_have_no_relative_imports_beyond_policy(self):
        for name in PURE_MODULES:
            tree = ast.parse((APP_DIR / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    self.assertEqual((node.level, node.module), (1, "policy"), name)

    def test_no_module_imports_network_process_dynamic_import_or_business_code(self):
        for path in app_sources():
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & FORBIDDEN_IMPORTS, set())

    def test_no_module_imports_the_cases_app_or_the_workflow_domain(self):
        for path in app_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertFalse(node.module.startswith(("apps.cases", "workflows")), f"{path.name}: {node.module}")
                if isinstance(node, ast.ImportFrom) and node.level == 2:
                    self.assertNotEqual((node.module or "").split(".")[0], "cases", path.name)

    def test_no_module_names_eval_exec_or_file_builtins(self):
        for path in app_sources():
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                self.assertEqual(names & FORBIDDEN_NAMES, set())

    def test_no_module_calls_process_or_file_functions_on_os(self):
        for path in app_sources():
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os"}
                self.assertEqual(used & FORBIDDEN_OS_ATTRIBUTES, set())

    def test_the_handler_is_never_resolved_from_a_string_by_attribute_or_import(self):
        for path in app_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"getattr", "setattr", "import_string", "import_module"}, path.name)

    def test_the_checkers_detect_forbidden_code(self):
        import tempfile

        from tests.test_request_intake_boundaries import named_builtins

        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe.py"
            probe.write_text("import subprocess\nimport os\nos.system('x')\neval('1')\n", encoding="utf-8")
            self.assertEqual(imported_modules(probe) & FORBIDDEN_IMPORTS, {"subprocess"})
            self.assertEqual(named_builtins(probe, FORBIDDEN_NAMES), {"eval"})
