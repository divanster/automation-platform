"""DB-free static and structural tests of the text extraction slice (DOC-2): layering, what each module may import and do, where the PDF library is imported (only in the isolated child's extraction module,
never in the web or worker process), the absence of network, code execution, shells and unpickling, the absence of extracted text from logs and audit events, the models and their migration, the dependency pins and
the settings. They read source with ``ast``; no database is touched."""

import ast
import re
import subprocess
import sys
from pathlib import Path

from django.apps import apps as django_apps
from django.db import models as django_models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase

from apps.audit import events
from apps.cases.errors import ImmutableRecordError
from apps.cases.models import ImmutableRecord
from apps.document_text import models, rules
from tests.settings_loader import load_settings

BACKEND = Path(__file__).resolve().parent.parent
REPOSITORY = BACKEND.parent
APP = BACKEND / "apps" / "document_text"
PURE = ("rules.py", "errors.py", "extractor_identity.py", "child_limits.py")
NETWORK_AND_EXECUTION = {"socket", "ssl", "http", "urllib", "requests", "httpx", "pickle", "marshal", "shelve", "ftplib", "smtplib", "multiprocessing", "xmlrpc", "telnetlib", "asyncio", "concurrent"}
LOGGING_MODULES = {"services.py", "execution.py", "handlers.py"}


def full_imports(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def top_names(imports):
    return {name.lstrip(".").split(".")[0] for name in imports if not name.startswith(".")}


def app_modules():
    return sorted(APP.glob("*.py"))


class LayeringTests(SimpleTestCase):
    def test_the_pure_modules_import_no_django_and_no_application_code(self):
        for name in PURE:
            imports = full_imports(APP / name)
            with self.subTest(module=name):
                self.assertEqual({m for m in imports if m.split(".")[0] in {"django", "rest_framework", "config", "apps", "workflows"}}, set())

    def test_the_rules_use_only_the_standard_library_and_no_pdf_library(self):
        self.assertLessEqual(top_names(full_imports(APP / "rules.py")), {"hashlib", "json", "re", "unicodedata", "uuid", "dataclasses"})
        self.assertLessEqual(top_names(full_imports(APP / "child_limits.py")), {"os", "ctypes", "resource"})

    def test_the_pdf_library_is_imported_by_exactly_one_module_of_the_whole_backend(self):
        importers = []
        for path in sorted(BACKEND.rglob("*.py")):
            if ".venv" in path.parts or "migrations" in path.parts or "tests" in path.parts:
                continue
            if "pypdf" in top_names(full_imports(path)):
                importers.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(importers, ["apps/document_text/pdf_text.py"])

    def test_the_child_imports_the_extraction_module_only_inside_its_run_function(self):
        tree = ast.parse((APP / "child.py").read_text(encoding="utf-8"))
        module_level = {alias for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in ([node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names])}
        self.assertNotIn("apps.document_text.pdf_text", module_level)
        self.assertNotIn("pypdf", module_level)
        self.assertEqual(top_names(full_imports(APP / "child.py")) - {"os", "sys", "apps"}, set())

    def test_neither_the_web_process_nor_the_worker_process_loads_the_pdf_library(self):
        code = (
            "import os, sys; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development'); sys.path.insert(0, %r)\n"
            "import django; django.setup()\n"
            "import apps.document_text.services, apps.document_text.handlers, apps.document_text.execution, apps.document_text.isolation, apps.api.views_document_text\n"
            "import config.urls, config.task_registry, config.document_extraction\n"
            "from apps.tasks.management.commands import runworker\n"
            "print('pypdf' in sys.modules, any(name.startswith('pypdf') for name in sys.modules))\n" % str(BACKEND)
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(BACKEND), timeout=120, env={**__import__("os").environ, "DJANGO_SETTINGS_MODULE": "config.settings.development"})
        self.assertEqual(completed.returncode, 0, completed.stderr[-600:])
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "False False")

    def test_no_module_reaches_the_network_pickles_or_runs_code_from_a_string(self):
        for path in app_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            with self.subTest(module=path.name):
                self.assertEqual(top_names(full_imports(path)) & NETWORK_AND_EXECUTION, set())
                calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                self.assertEqual(calls & {"eval", "exec", "compile", "__import__", "open", "input"}, set())
                attributes = {f"{ast.unparse(n.value)}.{n.attr}" for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                self.assertEqual({a for a in attributes if a.split(".")[-1] in {"system", "popen", "spawn", "spawnl", "spawnv", "execv", "execl", "fork", "startfile"}}, set())
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("shell=True", source)

    def test_only_the_isolation_runs_processes_and_only_the_child_limits_use_ctypes(self):
        for path in app_modules():
            imports = top_names(full_imports(path))
            with self.subTest(module=path.name):
                self.assertEqual("subprocess" in imports, path.name == "isolation.py")
                self.assertEqual("ctypes" in imports, path.name == "child_limits.py")
        self.assertNotIn("ctypes", top_names(full_imports(APP / "isolation.py")))

    def test_the_app_names_no_ai_gateway_secret_store_request_intake_module_or_api_code(self):
        for path in app_modules():
            imports = full_imports(path)
            with self.subTest(module=path.name):
                self.assertEqual({m for m in imports if m.startswith(("apps.ai_", "apps.secret_store", "apps.intake", "apps.module", "apps.api", "workflows", "config"))}, set())
                self.assertEqual({m for m in imports if m.startswith("apps.") and m.split(".")[1] not in {"accounts", "audit", "cases", "documents", "tasks", "document_text"}}, set())

    def test_the_document_app_and_the_task_queue_know_nothing_of_extraction(self):
        for package in ("documents", "tasks", "audit", "accounts", "cases"):
            for path in sorted((BACKEND / "apps" / package).rglob("*.py")):
                with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                    self.assertFalse({m for m in full_imports(path) if m.startswith(("apps.document_text", "pypdf"))})

    def test_only_the_composition_root_and_the_api_wire_the_slice(self):
        for path in sorted(BACKEND.rglob("*.py")):
            if ".venv" in path.parts or "tests" in path.parts or "document_text" in path.parts or "migrations" in path.parts:
                continue
            uses = {m for m in full_imports(path) if m.startswith("apps.document_text")}
            if uses:
                self.assertIn(path.relative_to(BACKEND).as_posix(), {"config/task_registry.py", "config/document_extraction.py", "apps/api/views_document_text.py", "apps/api/errors.py", "apps/api/serializers.py"}, uses)

    def test_the_view_is_a_transport_adapter(self):
        path = BACKEND / "apps" / "api" / "views_document_text.py"
        imports = full_imports(path)
        self.assertEqual({m for m in imports if m.endswith(".models") or m.startswith(("django.db", "apps.document_text.models", "apps.documents", "hashlib", "os", "subprocess"))}, set())
        self.assertEqual(top_names(imports) & {"logging", "hashlib", "os", "shutil", "tempfile", "subprocess"}, set())
        source = path.read_text(encoding="utf-8")
        for token in (".objects", "transaction", "cursor", ".save(", ".delete(", "open(", "logger", "print(", "request.data", "request.body", "request.FILES", "request.POST"):
            self.assertNotIn(token, source)


class NoContentInLogsTests(SimpleTestCase):
    ALLOWED = {"extraction.pk", "extraction_id", "document.pk", "document_id", "actor.public_id", "job.extraction_id", "job.document_id", "result.status", "error.code", "extraction.document_id"}

    def test_only_the_services_the_executor_and_the_handler_log(self):
        for path in app_modules():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                if path.name in LOGGING_MODULES:
                    self.assertIn("logger", source)
                else:
                    self.assertNotIn("logging", top_names(full_imports(path)) - {"logging"} if path.name == "pdf_text.py" else top_names(full_imports(path)))
                    self.assertNotIn("logger.", source)

    def test_every_log_call_is_fixed_text_with_identifier_arguments_only(self):
        found = 0
        for name in LOGGING_MODULES:
            tree = ast.parse((APP / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                    found += 1
                    with self.subTest(module=name, line=node.lineno):
                        self.assertIsInstance(node.args[0], ast.Constant)
                        self.assertFalse(any(isinstance(arg, (ast.JoinedStr, ast.BinOp)) for arg in node.args))
                        self.assertEqual(re.findall(r"%[a-z]", node.args[0].value), ["%s"] * (len(node.args) - 1))
                        self.assertLessEqual({ast.unparse(arg) for arg in node.args[1:]}, self.ALLOWED)
        self.assertGreaterEqual(found, 7)

    def test_nothing_prints(self):
        for path in app_modules():
            names = {n.id for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Name)}
            self.assertEqual(names & {"print", "pprint"}, set(), path.name)

    def test_the_error_types_carry_fixed_messages(self):
        source = (APP / "errors.py").read_text(encoding="utf-8")
        for token in ("{", "%s", ".format"):
            self.assertNotIn(token, re.sub(r'""".*?"""', "", source, flags=re.S))

    def test_the_child_writes_only_its_answer(self):
        source = (APP / "child.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.stderr", source)
        self.assertEqual(source.count("stdout.write("), 3)  # a fatal answer for the limits, a fatal answer for memory, the answer


class ModelTests(SimpleTestCase):
    MODELS = (models.TextExtraction, models.TextExtractionResult, models.TextExtractionPage)

    def test_all_three_models_are_append_only_records(self):
        for model in self.MODELS:
            with self.subTest(model=model.__name__):
                self.assertTrue(issubclass(model, ImmutableRecord))
                self.assertEqual(model._meta.app_label, "document_text")

    def test_an_existing_row_cannot_be_saved_again_updated_or_deleted(self):
        for model in self.MODELS:
            with self.subTest(model=model.__name__):
                row = model(pk=uuid_for(model))
                row._state.adding = False
                with self.assertRaises(ImmutableRecordError):
                    row.save()
                with self.assertRaises(ImmutableRecordError):
                    row.save(update_fields=["status"] if hasattr(model, "status") else ["id"])
                with self.assertRaises(ImmutableRecordError):
                    row.delete()
                with self.assertRaises(ImmutableRecordError):
                    model.objects.update(id=1)
                with self.assertRaises(ImmutableRecordError):
                    model.objects.all().delete()
                with self.assertRaises(ImmutableRecordError):
                    model.objects.bulk_update([], ["id"])
                with self.assertRaises(ImmutableRecordError):
                    model.objects.bulk_create([], update_conflicts=True, update_fields=["id"], unique_fields=["id"])

    def test_every_foreign_key_protects_its_target(self):
        for model in self.MODELS:
            for field in model._meta.get_fields():
                if field.many_to_one or field.one_to_one:
                    if field.concrete:
                        with self.subTest(model=model.__name__, field=field.name):
                            self.assertIs(field.remote_field.on_delete, django_models.PROTECT)

    def test_the_result_is_one_to_one_with_the_request_and_the_request_with_its_task(self):
        self.assertIsInstance(models.TextExtractionResult._meta.get_field("extraction"), django_models.OneToOneField)
        self.assertIsInstance(models.TextExtraction._meta.get_field("task"), django_models.OneToOneField)
        self.assertEqual(models.TextExtractionResult._meta.get_field("extraction").remote_field.related_name, "result")

    def test_extracted_text_is_stored_in_exactly_one_column_and_no_bytes_anywhere(self):
        text_columns = [(m.__name__, f.name) for m in self.MODELS for f in m._meta.get_fields() if isinstance(f, (django_models.TextField, django_models.JSONField))]
        self.assertEqual(text_columns, [("TextExtractionPage", "text")])
        for model in self.MODELS:
            for field in model._meta.get_fields():
                self.assertNotIsInstance(field, (django_models.BinaryField, django_models.FileField, django_models.ImageField))

    def test_the_fields_are_exactly_the_documented_ones(self):
        names = lambda model: {f.name for f in model._meta.get_fields() if f.concrete}  # noqa: E731
        self.assertEqual(names(models.TextExtraction), {"id", "document", "requested_by", "task", "requested_at"})
        self.assertEqual(
            names(models.TextExtractionResult),
            {"id", "extraction", "status", "error_code", "extractor_name", "extractor_version", "page_count", "char_count", "source_sha256", "source_size_bytes", "text_sha256", "completed_at"},
        )
        self.assertEqual(names(models.TextExtractionPage), {"id", "result", "page_number", "status", "error_code", "text", "char_count"})

    def test_the_constraints_are_named_and_cover_the_vocabularies(self):
        result = {c.name for c in models.TextExtractionResult._meta.constraints}
        page = {c.name for c in models.TextExtractionPage._meta.constraints}
        self.assertEqual(result, {"doctext_result_status_valid", "doctext_result_code_shape", "doctext_result_code_matches_status", "doctext_result_page_count_known", "doctext_result_failed_has_no_text", "doctext_result_hash_shape"})
        self.assertEqual(page, {"doctext_page_number_unique", "doctext_page_number_at_least_1", "doctext_page_status_valid", "doctext_page_code_matches_status", "doctext_page_code_shape"})
        for constraint in models.TextExtractionResult._meta.constraints + models.TextExtractionPage._meta.constraints:
            self.assertLessEqual(len(constraint.name), 63)  # a PostgreSQL identifier
        status_check = next(c for c in models.TextExtractionResult._meta.constraints if c.name == "doctext_result_status_valid")
        self.assertEqual(set(status_check.condition.children[0][1]), set(rules.RESULT_STATUSES))

    def test_the_code_columns_fit_every_code(self):
        longest = max(len(code) for code in (*rules.RESULT_ERROR_CODES, *rules.PAGE_ERRORS))
        self.assertLessEqual(longest, models.TextExtractionResult._meta.get_field("error_code").max_length)
        self.assertLessEqual(longest, models.TextExtractionPage._meta.get_field("error_code").max_length)
        self.assertLessEqual(max(map(len, rules.RESULT_STATUSES)), models.TextExtractionResult._meta.get_field("status").max_length)
        self.assertLessEqual(max(map(len, rules.PAGE_STATUSES)), models.TextExtractionPage._meta.get_field("status").max_length)


def uuid_for(model):
    import uuid

    return 1 if model is models.TextExtractionPage else uuid.uuid4()


class MigrationTests(SimpleTestCase):
    def test_the_models_and_the_migration_agree(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(django_apps), NonInteractiveMigrationQuestioner(specified_apps={"document_text"}))
        self.assertEqual(detector.changes(graph=loader.graph, trim_to_apps={"document_text"}), {})

    def test_the_slice_adds_one_additive_migration_and_changes_no_existing_app(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "document_text"], [("document_text", "0001_text_extraction")])
        migration = loader.get_migration("document_text", "0001_text_extraction")
        self.assertIn(("documents", "0001_initial"), migration.dependencies)
        self.assertIn(("tasks", "0001_initial"), migration.dependencies)
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "documents"], [("documents", "0001_initial")])  # DOC-1's schema is untouched
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "audit"], [("audit", "0004_document_subject_type")])  # no audit migration: the actions are Python vocabulary, the subject is DOC-1's
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "tasks"], [("tasks", "0001_initial")])
        self.assertEqual({type(op).__name__ for op in migration.operations}, {"CreateModel", "AddIndex", "AddConstraint"})  # nothing is altered, removed or run


class AuditVocabularyTests(SimpleTestCase):
    def test_the_three_actions_are_core_actions_in_the_reserved_document_namespace(self):
        from apps.modules.manifest import RESERVED_NAMESPACES

        names = (events.ACTION_DOCUMENT_TEXT_REQUESTED, events.ACTION_DOCUMENT_TEXT_EXTRACTED, events.ACTION_DOCUMENT_TEXT_READ)
        self.assertEqual(names, ("document.text_requested", "document.text_extracted", "document.text_read"))
        for action in names:
            self.assertIn(action, events.ACTIONS)
            self.assertEqual(action.split(".")[0], "document")
            self.assertLessEqual(len(action), 32)  # AuditEvent.action's column
        self.assertIn("document", RESERVED_NAMESPACES)

    def test_the_detail_keys_carry_no_text_name_or_path(self):
        for action in (events.ACTION_DOCUMENT_TEXT_REQUESTED, events.ACTION_DOCUMENT_TEXT_EXTRACTED, events.ACTION_DOCUMENT_TEXT_READ):
            keys = events.DETAIL_KEYS[action]
            self.assertFalse(keys & {"text", "filename", "original_filename", "path", "storage_key", "page_text", "content"})
        self.assertEqual(events.DETAIL_KEYS[events.ACTION_DOCUMENT_TEXT_REQUESTED], {"extraction_id"})

    def test_the_subject_is_the_documents_existing_subject_type(self):
        self.assertEqual(events.SUBJECT_DOCUMENT, "document")
        self.assertIn("document", events.SUBJECT_TYPES)


class DependencyAndSettingsTests(SimpleTestCase):
    def test_the_library_is_pinned_exactly_in_the_direct_and_the_locked_requirements(self):
        direct = (REPOSITORY / "requirements" / "runtime.in").read_text(encoding="utf-8")
        locked = (REPOSITORY / "requirements" / "runtime.txt").read_text(encoding="utf-8")
        self.assertRegex(direct, r"(?m)^pypdf==\d+\.\d+\.\d+$")
        version = re.search(r"(?m)^pypdf==(\d+\.\d+\.\d+)$", direct).group(1)
        self.assertRegex(locked, rf"(?m)^pypdf=={re.escape(version)} \\\n    --hash=sha256:[0-9a-f]{{64}}$")
        development = (REPOSITORY / "requirements" / "development.in").read_text(encoding="utf-8")
        self.assertIn("-r runtime.in", development)

    def test_the_installed_library_is_the_pinned_one_and_no_decryption_package_is_present(self):
        from importlib import metadata, util

        direct = (REPOSITORY / "requirements" / "runtime.in").read_text(encoding="utf-8")
        self.assertEqual(metadata.version("pypdf"), re.search(r"(?m)^pypdf==(\S+)$", direct).group(1))
        for package in ("cryptography", "Cryptodome", "Crypto", "PIL", "fontTools", "brotli"):  # none of pypdf's optional extras: an encrypted file is refused, never decrypted
            self.assertIsNone(util.find_spec(package), package)

    def test_the_library_has_no_required_dependency_on_this_python(self):
        from importlib import metadata

        required = [r for r in (metadata.requires("pypdf") or []) if "extra ==" not in r and "python_version <" not in r]
        self.assertEqual(required, [])

    def test_the_app_is_installed_and_has_its_own_throttle_scopes(self):
        self.assertEqual(django_apps.get_app_config("document_text").name, "apps.document_text")
        base = load_settings("config.settings.base", {})
        self.assertIn("apps.document_text", base.INSTALLED_APPS)
        for scope in ("document_text", "document_text_hour"):
            self.assertIn(scope, base.API_THROTTLE_RATES)
        self.assertEqual({name for name in dir(base) if name.startswith("DOCUMENT_TEXT") or name.startswith("MEDIA")}, set())  # no new setting and still no media root

    def test_no_extraction_variable_is_a_django_setting(self):
        for path in (BACKEND / "config" / "settings").glob("*.py"):
            self.assertNotIn("AP_DOCUMENT_EXTRACTION", path.read_text(encoding="utf-8"), path.name)  # read only by the worker's composition root, at start-up
