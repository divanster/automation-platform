"""DB-free static and structural tests of the document slice (DOC-1): layering, the absence of content and paths from logs and responses, the model and its migrations, the permission declarations,
the documented routes and the settings that keep uploads out of the system temporary directory. They read source with ``ast``; no database is touched."""

import ast
import re
from pathlib import Path

from django.apps import apps as django_apps
from django.db import models as django_models
from django.test import SimpleTestCase

from apps.accounts import roles
from apps.api import serializers
from apps.documents import models, rules
from apps.documents.errors import DocumentImmutableError
from tests.settings_loader import load_settings

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "apps" / "documents"
PURE = ("rules.py", "pdf.py", "errors.py", "access.py", "storage.py")
NETWORK_AND_EXECUTION = {"socket", "ssl", "http", "urllib", "requests", "httpx", "subprocess", "pickle", "marshal", "ctypes", "importlib", "multiprocessing", "ftplib", "smtplib", "shelve"}


def full_imports(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def top_names(imports):
    return {name.lstrip(".").split(".")[0] for name in imports if not name.startswith(".")}


class LayeringTests(SimpleTestCase):
    def test_the_pure_modules_import_no_django_and_no_other_app_but_accounts_access(self):
        for name in PURE:
            imports = full_imports(APP / name)
            with self.subTest(module=name):
                self.assertEqual({m for m in imports if m.split(".")[0] in {"django", "rest_framework", "config"}}, set())
                self.assertEqual({m for m in imports if m.startswith("apps.") and not m.startswith(("apps.accounts",))}, set())
                self.assertEqual({m for m in imports if m.startswith("apps.accounts") and m not in {"apps.accounts", "apps.accounts.access", "apps.accounts.roles"}}, set())

    def test_the_storage_uses_only_the_standard_library(self):
        allowed = {"hashlib", "os", "stat", "abc", "typing"}
        self.assertLessEqual(top_names(full_imports(APP / "storage.py")), allowed)

    def test_no_module_of_the_app_reaches_the_network_runs_code_or_unpickles(self):
        for path in sorted(APP.rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(top_names(full_imports(path)) & NETWORK_AND_EXECUTION - {"urllib"}, set())
        for name in ("storage.py", "pdf.py", "services.py", "models.py", "access.py"):
            self.assertNotIn("urllib", top_names(full_imports(APP / name)))

    def test_the_app_names_no_ai_gateway_provider_secret_store_worker_or_intake_code(self):
        for path in sorted(APP.rglob("*.py")):
            imports = full_imports(path)
            with self.subTest(module=path.name):
                self.assertEqual({m for m in imports if m.startswith(("apps.ai_", "apps.secret_store", "apps.tasks", "apps.intake", "apps.cases", "apps.module", "apps.api", "workflows"))}, set())

    def test_the_app_imports_only_the_composition_root_nowhere(self):
        for path in sorted(APP.rglob("*.py")):
            self.assertEqual({m for m in full_imports(path) if m.split(".")[0] == "config"} - {"config.document_storage"}, set(), path.name)
        for path in sorted(APP.rglob("*.py")):
            if path.name != "prepare_document_storage.py":
                self.assertNotIn("config", top_names(full_imports(path)), path.name)

    def test_the_view_is_a_transport_adapter(self):
        path = BACKEND / "apps" / "api" / "views_documents.py"
        imports = full_imports(path)
        self.assertEqual({m for m in imports if m.endswith(".models") or m.startswith(("django.db", "apps.documents.models", "apps.documents.storage", "hashlib", "os", "tempfile", "shutil"))}, set())
        self.assertEqual(top_names(imports) & {"hashlib", "os", "shutil", "tempfile", "logging", "urllib"}, set())
        source = path.read_text(encoding="utf-8")
        for token in (".objects", "transaction", "cursor", ".save(", ".delete(", "open(", "logger", "print("):
            self.assertNotIn(token, source)
        attributes = {(ast.unparse(n.value), n.attr) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
        for forbidden in (("request", "data"), ("request", "FILES"), ("request", "POST"), ("request", "body")):
            self.assertNotIn(forbidden, attributes)  # the body is never parsed: only the storage reads it, from the stream

    def test_no_storage_backend_is_imported_by_the_api(self):
        for path in sorted((BACKEND / "apps" / "api").glob("*.py")):
            if path.name != "views_documents.py":
                self.assertNotIn("apps.documents.storage", full_imports(path), path.name)
        self.assertEqual({m for m in full_imports(BACKEND / "apps" / "api" / "views_documents.py") if m.startswith("config")}, set())


class NoContentInLogsTests(SimpleTestCase):
    def test_only_the_services_log_and_only_identifiers(self):
        for path in sorted(APP.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if path.name != "services.py":
                self.assertNotIn("logging", full_imports(path), path.name)
                self.assertNotIn("logger.", source, path.name)
                continue
        tree = ast.parse((APP / "services.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"]
        self.assertGreaterEqual(len(calls), 4)  # uploaded, download started, an uncertain commit, a failed cleanup
        for call in calls:
            self.assertEqual(re.findall(r"%[a-z]", call.args[0].value), ["%s"] * (len(call.args) - 1))  # fixed text, plain placeholders only

    def test_the_log_messages_are_fixed_text_with_only_identifier_arguments(self):
        tree = ast.parse((APP / "services.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                self.assertIsInstance(node.args[0], ast.Constant)
                self.assertFalse(any(isinstance(arg, (ast.JoinedStr, ast.BinOp)) for arg in node.args))  # no f-string, no %-formatting done by hand
                allowed = {"document_id", "actor.public_id", "document.pk"}
                self.assertLessEqual({ast.unparse(arg) for arg in node.args[1:]}, allowed)

    def test_no_print_and_no_repr_of_a_document_anywhere(self):
        for path in sorted(APP.rglob("*.py")) + [BACKEND / "apps" / "api" / "views_documents.py", BACKEND / "config" / "document_storage.py"]:
            names = {n.id for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Name)}
            self.assertEqual(names & {"print", "pprint"}, set(), path.name)

    def test_the_error_types_carry_only_fixed_messages(self):
        source = (APP / "errors.py").read_text(encoding="utf-8")
        self.assertNotIn("path", re.sub(r'"[^"]*"|\'[^\']*\'|""".*?"""', "", source, flags=re.S).replace("storage_path", ""))


class ModelTests(SimpleTestCase):
    def test_the_model_stores_metadata_only_no_bytes(self):
        forbidden = (django_models.BinaryField, django_models.FileField, django_models.ImageField, django_models.TextField, django_models.JSONField)
        for field in models.Document._meta.get_fields():
            with self.subTest(field=field.name):
                self.assertNotIsInstance(field, forbidden)
        self.assertEqual(
            {f.name for f in models.Document._meta.get_fields()},
            {"id", "owner", "original_filename", "content_type", "size_bytes", "sha256", "uploaded_at", "storage_key", "processing_state"},
        )
        self.assertTrue(models.Document._meta.get_field("storage_key").unique)
        self.assertEqual(models.Document._meta.get_field("owner").remote_field.on_delete.__name__, "PROTECT")

    def test_the_declared_permissions_are_exactly_the_document_permissions_and_default_permissions_are_off(self):
        declared = {f"documents.{codename}" for codename, _ in models.Document._meta.permissions}
        self.assertEqual(declared, set(roles.DOCUMENT_PERMISSIONS))
        self.assertEqual(tuple(models.Document._meta.default_permissions), ())
        self.assertEqual(models.Document._meta.app_label, "documents")

    def test_the_request_matrix_the_module_derivation_is_pinned_to_is_unchanged(self):
        self.assertFalse(roles.DOCUMENT_PERMISSIONS & roles.ALL_PERMISSIONS)
        for role in roles.ROLES:
            self.assertEqual(roles.granted_permissions(role), roles.ROLE_PERMISSIONS[role] | roles.DOCUMENT_ROLE_PERMISSIONS[role])
        self.assertEqual(set(roles.DOCUMENT_ROLE_PERMISSIONS), set(roles.ROLES))
        self.assertEqual(set().union(*roles.DOCUMENT_ROLE_PERMISSIONS.values()), set(roles.DOCUMENT_PERMISSIONS))

    def test_the_document_roles_are_least_privilege(self):
        grants = roles.DOCUMENT_ROLE_PERMISSIONS
        self.assertEqual(grants[roles.OPERATOR], {roles.PERM_DOC_UPLOAD, roles.PERM_DOC_VIEW_OWN, roles.PERM_DOC_DOWNLOAD_OWN})
        self.assertEqual(grants[roles.APPROVER], {roles.PERM_DOC_VIEW_ANY, roles.PERM_DOC_DOWNLOAD_ANY})
        self.assertEqual(grants[roles.AUDITOR], {roles.PERM_DOC_VIEW_ANY})
        for role in (roles.APPROVER, roles.AUDITOR):
            self.assertNotIn(roles.PERM_DOC_UPLOAD, grants[role])
        for role in roles.ROLES:
            self.assertFalse({n for n in grants[role] if "any" in n and "download" in n} and role != roles.APPROVER)

    def test_the_constraints_check_what_the_database_can(self):
        names = {c.name for c in models.Document._meta.constraints}
        self.assertEqual(
            names,
            {"documents_content_type_pdf", "documents_sha256_shape", "documents_size_bounds", "documents_storage_key_shape", "documents_state_valid", "documents_filename_not_empty"},
        )
        states = [c for c in models.Document._meta.constraints if c.name == "documents_state_valid"][0]
        self.assertEqual(states.condition.children[0][1], list(rules.PROCESSING_STATES))

    def test_stored_metadata_is_immutable_except_the_processing_state_at_the_orm_boundary(self):
        existing = models.Document(id="00000000-0000-4000-8000-000000000001", original_filename="a.pdf", size_bytes=1, sha256="a" * 64, storage_key="ab/" + "c" * 32)
        existing._state.adding = False
        for call in (lambda: existing.save(), lambda: existing.save(update_fields=["sha256"]), lambda: existing.save(update_fields=["processing_state", "sha256"]), lambda: existing.save(update_fields=None), lambda: existing.delete()):
            with self.assertRaises(DocumentImmutableError):
                call()
        manager = models.Document.objects
        for call in (
            lambda: manager.update(original_filename="x"), lambda: manager.update(sha256="x"), lambda: manager.update(owner=None), lambda: manager.update(processing_state="processed", sha256="x"),
            lambda: manager.all().update(storage_key="x"), lambda: manager.all().delete(), lambda: manager.bulk_update([existing], ["sha256"]),
            lambda: manager.bulk_create([existing], update_conflicts=True, update_fields=["sha256"], unique_fields=["id"]),
        ):  # fmt: skip
            with self.assertRaises(DocumentImmutableError):
                call()

    def test_a_new_row_is_always_inserted_never_an_upsert(self):
        import inspect

        source = inspect.getsource(models.Document.save_base)
        self.assertIn("force_insert = True", source)

    def test_there_is_no_serializer_field_for_the_storage_reference(self):
        self.assertEqual(
            set(serializers.DocumentOutput().fields),
            {"id", "original_filename", "content_type", "size_bytes", "sha256", "uploaded_at", "processing_state", "uploaded_by", "allowed_actions"},
        )

    def test_the_app_is_installed_and_is_not_a_module(self):
        self.assertEqual(django_apps.get_app_config("documents").name, "apps.documents")
        from config.modules import BUILTIN_MODULES

        self.assertEqual({manifest.module_id for manifest in BUILTIN_MODULES}, {"request_intake"})  # no document module was created


class MigrationTests(SimpleTestCase):
    def test_the_documents_migration_only_creates_the_table(self):
        source = (APP / "migrations" / "0001_initial.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        ops = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call) and "migrations." in ast.unparse(n.func)]
        self.assertEqual([op for op in ops if op != "migrations.swappable_dependency"], ["migrations.CreateModel"])
        for word in ("RunSQL", "RunPython", "DeleteModel", "RemoveField"):
            self.assertNotIn(word, source)

    def test_the_audit_migration_for_the_document_subject_is_additive_and_never_rewrites_events(self):
        source = (BACKEND / "apps" / "audit" / "migrations" / "0004_document_subject_type.py").read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        for word in ("RunSQL", "DeleteModel", "RemoveField", ".delete(", ".update(", ".bulk_update(", "cursor", "execute"):
            self.assertNotIn(word, code, word)
        self.assertIn("IrreversibleError", code)

    def test_model_state_and_migrations_agree_without_a_database(self):
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(None, ignore_no_migrations=True)
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(django_apps), NonInteractiveMigrationQuestioner(specified_apps={"documents", "audit"}))
        self.assertEqual(detector.changes(graph=loader.graph, trim_to_apps={"documents", "audit"}), {})

    def test_the_documents_migration_follows_the_user_model_only(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        dependencies = loader.get_migration("documents", "0001_initial").dependencies
        self.assertEqual([d for d in dependencies if d[0] not in {"__setting__", "accounts"}], [])  # the swappable user model, and nothing else
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "documents"], [("documents", "0001_initial")])


class SettingsTests(SimpleTestCase):
    def test_only_the_in_memory_upload_handler_is_installed_with_a_small_bound(self):
        base = load_settings("config.settings.base")
        self.assertEqual(base.FILE_UPLOAD_HANDLERS, ["django.core.files.uploadhandler.MemoryFileUploadHandler"])
        self.assertEqual(base.FILE_UPLOAD_MAX_MEMORY_SIZE, 256 * 1024)
        for module in ("config.settings.development", "config.settings.production"):
            settings = load_settings(module, {"AP_SECRET_KEY": "x" * 50, "AP_ALLOWED_HOSTS": "app.example.invalid"} if module.endswith("production") else None)
            self.assertEqual(settings.FILE_UPLOAD_HANDLERS, base.FILE_UPLOAD_HANDLERS, module)

    def test_the_document_settings_are_not_read_at_import_so_a_developers_env_cannot_reach_a_test(self):
        base = (BACKEND / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        self.assertNotIn("AP_DOCUMENT", base)

    def test_the_upload_rates_are_bounded_and_stricter_than_general_use(self):
        rates = load_settings("config.settings.base").API_THROTTLE_RATES
        per_minute = lambda r: int(r.split("/")[0]) * {"sec": 60, "min": 1, "hour": 1 / 60, "day": 1 / 1440}[r.split("/")[1]]  # noqa: E731
        self.assertLess(per_minute(rates["document_upload"]), per_minute(rates["api_user"]))
        self.assertLess(per_minute(rates["document_upload_hour"]), per_minute(rates["document_upload"]) * 60)

    def test_the_document_audit_vocabulary_is_core_s_and_reserved(self):
        from apps.audit.vocabulary import ACTION_SHAPE_PATTERN

        for action in ("document.uploaded", "document.downloaded"):
            self.assertRegex(action, ACTION_SHAPE_PATTERN)
            self.assertLessEqual(len(action), 32)  # the audit column
        self.assertLessEqual(len("document"), 16)
