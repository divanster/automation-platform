"""DB-free static and model tests for MOD-1b S4: the module host stays small, additive and out of Core's way.

Source is read with ``ast`` (nothing is imported for side effects). Model behaviour that needs no query (refusing a delete or an update) is run directly;
``SimpleTestCase`` refuses every database query, so a pass here means no query was made. No database is touched. This file is ASCII only.
"""

import ast
import sys
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.test import SimpleTestCase

from apps.cases.errors import ImmutableRecordError
from apps.module_host import models, rules
from apps.module_host.models import ModuleHostMarker, ModuleNamespaceClaim, ModuleState
from apps.modules import manifest as module_manifest

BACKEND = Path(__file__).resolve().parent.parent
HOST = BACKEND / "apps" / "module_host"
STDLIB = set(sys.stdlib_module_names)


def sources(root: Path, *, migrations=False):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or (not migrations and "migrations" in path.parts):
            continue
        yield path


def absolute_imports(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def code_only(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def importers_of(prefix: str) -> list:
    hits = []
    for top in ("apps", "config", "workflows"):
        for path in sources(BACKEND / top):
            if any(name == prefix or name.startswith(prefix + ".") for name in absolute_imports(path)):
                hits.append(path.relative_to(BACKEND).as_posix())
    return hits


class AppRegistrationTests(SimpleTestCase):
    def test_the_app_is_installed_with_its_own_label(self):
        self.assertIn("apps.module_host", settings.INSTALLED_APPS)
        config = django_apps.get_app_config("module_host")
        self.assertEqual((config.name, config.label), ("apps.module_host", "module_host"))
        self.assertEqual({m.__name__ for m in config.get_models()}, {"ModuleState", "ModuleNamespaceClaim", "ModuleHostMarker"})

    def test_apps_modules_is_still_not_a_django_app(self):
        self.assertNotIn("apps.modules", settings.INSTALLED_APPS)
        self.assertNotIn("modules", {c.label for c in django_apps.get_app_configs()})

    def test_the_package_has_no_routes_views_or_urls(self):
        present = {p.name for p in HOST.iterdir()}
        for name in ("urls.py", "views.py", "serializers.py", "admin.py", "signals.py", "templates"):
            self.assertNotIn(name, present)

    def test_there_is_no_module_route_in_the_application(self):
        from django.urls import get_resolver

        def names(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from names(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        self.assertFalse([r for r in names(get_resolver().url_patterns) if "module" in r])

    def test_the_app_config_has_no_ready_hook(self):
        tree = ast.parse((HOST / "apps.py").read_text(encoding="utf-8"))
        self.assertEqual([n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)], [])


class ImportBoundaryTests(SimpleTestCase):
    def test_core_packages_do_not_import_the_module_host(self):
        for package in ("accounts", "audit", "cases", "tasks", "health", "intake", "api", "modules"):
            for path in sources(BACKEND / "apps" / package):
                for name in absolute_imports(path):
                    with self.subTest(file=path.relative_to(BACKEND).as_posix(), imported=name):
                        self.assertFalse(name == "apps.module_host" or name.startswith("apps.module_host."), f"{path.name} imports {name}")
        for path in sources(BACKEND / "workflows"):
            self.assertFalse({n for n in absolute_imports(path) if n.startswith("apps.module_host")}, path.name)

    def test_only_the_worker_composition_root_and_the_host_import_the_host(self):
        self.assertEqual(importers_of("apps.module_host"), ["apps/module_host/management/commands/module_disable.py", "apps/module_host/management/commands/module_enable.py", "apps/module_host/management/commands/module_status.py", "apps/module_host/management/commands/module_sync.py", "config/task_registry.py"])

    def test_the_host_imports_only_what_it_may(self):
        allowed_prefixes = ("django", "apps.module_host", "apps.audit", "apps.cases", "apps.modules", "config.modules")
        for path in sources(HOST):
            for name in absolute_imports(path):
                top = name.split(".")[0]
                with self.subTest(file=path.relative_to(BACKEND).as_posix(), imported=name):
                    self.assertTrue(top in STDLIB or any(name == a or name.startswith(a + ".") for a in allowed_prefixes), f"{path.name} imports {name}")

    def test_the_host_never_imports_the_task_queue_request_intake_the_api_or_a_workflow(self):
        forbidden = ("apps.tasks", "apps.intake", "apps.api", "apps.accounts", "workflows", "rest_framework")
        for path in sources(HOST):
            for name in absolute_imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertFalse(any(name == f or name.startswith(f + ".") for f in forbidden), f"{path.name} imports {name}")

    def test_the_services_and_the_gate_name_no_task_model_and_touch_no_task(self):
        for name in ("services.py", "gate.py", "rules.py"):
            code = code_only(HOST / name)
            for word in ("Task", "TaskAttempt", "claim_next_task", "fail_task", "complete_task", "enqueue_task", "recover_expired_leases"):
                with self.subTest(file=name, word=word):
                    self.assertNotIn(word, code)

    def test_only_config_modules_names_modules_and_the_host_does_not_build_a_registry_itself(self):
        for name in ("services.py", "gate.py", "rules.py", "models.py", "cli.py"):
            self.assertNotIn("build_registry", code_only(HOST / name), name)
            self.assertNotIn("BUILTIN_MODULES", code_only(HOST / name), name)


class NoDynamicBehaviourTests(SimpleTestCase):
    def test_no_dynamic_import_process_eval_file_or_network_use(self):
        banned_imports = {"importlib", "pkgutil", "subprocess", "os", "sys", "socket", "pickle", "ctypes", "runpy", "shutil", "urllib", "http", "glob", "pathlib", "tempfile"}
        banned_names = {"__import__", "eval", "exec", "open", "globals", "locals", "import_module", "import_string", "entry_points", "environ", "getenv", "system", "Popen", "listdir", "walk_packages", "call_command"}
        for path in sources(HOST):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            imported = {name.split(".")[0] for name in absolute_imports(path)}
            with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                self.assertEqual(names & banned_names, set())
                self.assertEqual(imported & banned_imports, set())
                compiled = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "compile"]
                self.assertEqual(compiled, [])  # (re.compile in rules.py is an attribute call and is fine)

    def test_nothing_runs_migrations_installs_packages_or_downloads(self):
        for path in sources(HOST):
            code = code_only(path)
            for word in ("migrate", "makemigrations", "pip ", "install_", "download", "requests", "urlopen", "MigrationExecutor"):
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, code)

    def test_raw_sql_is_one_advisory_lock_and_nothing_destructive_exists(self):
        sql_users = {}
        for path in sources(HOST):
            code = code_only(path)
            for word in (".raw(", "RunSQL", "TRUNCATE", "DROP ", "DELETE", "UPDATE ", ".bulk_update(", ".delete(", "_raw_delete", "cascade"):
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, code)
            if "execute(" in code:
                sql_users[path.name] = code.count("execute(")
        self.assertEqual(sql_users, {"services.py": 1})
        self.assertIn("pg_advisory_xact_lock", code_only(HOST / "services.py"))

    def test_the_services_change_state_only_inside_one_atomic_block_and_audit_in_it(self):
        text = (HOST / "services.py").read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ("sync_modules", "_change"):
            node = functions[name]
            atomic_blocks = [w for w in ast.walk(node) if isinstance(w, ast.With) and "transaction.atomic" in ast.unparse(w.items[0].context_expr)]
            self.assertEqual(len(atomic_blocks), 1, name)
            block = atomic_blocks[0]
            inside = ast.unparse(block)
            self.assertIn("_lock_rows()", inside, name)
            self.assertIn("_record_change(", inside, name)
            self.assertIn(".save(", inside + "ModuleState.objects.create", name)
            # every line of the function outside the atomic block, with the docstring left out
            body_start = node.body[1].lineno if isinstance(node.body[0], ast.Expr) else node.body[0].lineno
            outside = "\n".join(line for number, line in enumerate(lines, 1) if body_start <= number <= node.end_lineno and not block.lineno <= number <= block.end_lineno)
            for write in (".save(", ".create(", "_record_change(", "record_event("):
                self.assertNotIn(write, outside, f"{name} writes outside its transaction")
        self.assertIn("select_for_update()", ast.unparse(functions["_lock_rows"]))

    def test_the_services_can_only_insert_namespace_claims_and_the_marker_never_change_them(self):
        tree = ast.parse((HOST / "services.py").read_text(encoding="utf-8"))
        used = {"ModuleNamespaceClaim": set(), "ModuleHostMarker": set()}
        for node in ast.walk(tree):
            # <Model>.objects.<method>(...)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "objects" and isinstance(node.value.value, ast.Name):
                if node.value.value.id in used:
                    used[node.value.value.id].add(node.attr)
        self.assertEqual(used["ModuleNamespaceClaim"], {"values_list", "create"})  # read the history, add a claim: no filter, update, delete or upsert
        self.assertEqual(used["ModuleHostMarker"], {"exists", "create"})
        code = code_only(HOST / "services.py")
        for word in ("update_or_create", "get_or_create", "bulk_create", ".update(", "update_fields=[\"namespace\"", "claim.module_id ="):
            self.assertNotIn(word, code, word)

    def test_the_audit_event_is_built_by_core_with_the_system_actor_only(self):
        code = code_only(HOST / "services.py")
        self.assertIn("events.system_event(", code)
        self.assertNotIn("user_event", code)
        self.assertNotIn("NewAuditEvent", code)
        self.assertIn("record_event(", code)  # Core's one recorder

    def test_no_module_audit_vocabulary_is_installed_by_the_host(self):
        for path in sources(HOST):
            code = code_only(path)
            self.assertNotIn("install_vocabulary", code)
            self.assertNotIn("install_audit_vocabulary", code)
            self.assertNotIn("audit_vocabulary", code)


class ModelContractTests(SimpleTestCase):
    def test_module_state_has_exactly_the_documented_fields(self):
        fields = {f.name: f for f in ModuleState._meta.get_fields()}
        self.assertEqual(
            set(fields),
            {"id", "module_id", "installed_version", "enabled", "configuration_version", "registry_fingerprint", "created_at", "updated_at", "enabled_changed_at"},
        )
        self.assertTrue(fields["id"].primary_key)
        self.assertEqual(type(fields["id"]).__name__, "UUIDField")
        self.assertTrue(fields["module_id"].unique)
        self.assertTrue(fields["configuration_version"].null)
        for name in ("module_id", "installed_version", "enabled", "registry_fingerprint", "created_at", "updated_at", "enabled_changed_at"):
            self.assertFalse(fields[name].null, name)
        self.assertFalse(fields["enabled"].has_default(), "whoever creates a row must say whether it is enabled")

    def test_the_namespace_claim_has_the_documented_fields_and_a_unique_namespace(self):
        fields = {f.name: f for f in ModuleNamespaceClaim._meta.get_fields()}
        self.assertEqual(set(fields), {"id", "namespace", "module_id", "claimed_at"})
        self.assertTrue(fields["namespace"].unique)

    def test_the_marker_is_one_row_with_a_time_and_a_fingerprint(self):
        fields = {f.name: f for f in ModuleHostMarker._meta.get_fields()}
        self.assertEqual(set(fields), {"id", "initialized_at", "initial_registry_fingerprint"})
        self.assertTrue(fields["id"].primary_key)
        self.assertEqual({c.name for c in ModuleHostMarker._meta.constraints}, {"module_marker_single_row", "module_marker_fingerprint_shape"})
        self.assertEqual(ModuleHostMarker().pk, 1)  # the only primary key the database accepts

    def test_the_marker_can_never_be_reset_changed_or_deleted_through_the_orm(self):
        marker = ModuleHostMarker(initialized_at=None, initial_registry_fingerprint="a" * 64)
        marker._state.adding = False  # a row that exists
        for action in (marker.save, lambda: marker.save(update_fields=["initialized_at"]), marker.delete, ModuleHostMarker.objects.all().delete, lambda: ModuleHostMarker.objects.all().update(initialized_at=None)):
            with self.assertRaises(ImmutableRecordError):
                action()

    def test_no_foreign_key_exists_so_no_other_table_can_cascade_into_these(self):
        for model in (ModuleState, ModuleNamespaceClaim, ModuleHostMarker):
            self.assertEqual([f.name for f in model._meta.get_fields() if f.is_relation], [])

    def test_the_database_constraints_are_the_declared_shapes(self):
        state = {c.name: c for c in ModuleState._meta.constraints}
        self.assertEqual(set(state), {"module_state_module_id_shape", "module_state_version_shape", "module_state_fingerprint_shape"})
        claim = {c.name: c for c in ModuleNamespaceClaim._meta.constraints}
        self.assertEqual(set(claim), {"module_claim_namespace_shape", "module_claim_module_id_shape"})

    def test_the_stored_shapes_match_the_contract(self):
        # the model patterns are the manifest's own, anchored
        self.assertEqual(models._MODULE_ID_PATTERN, "^" + module_manifest._MODULE_ID.pattern + "$")
        self.assertEqual(models._NAMESPACE_PATTERN, "^" + module_manifest._NAMESPACE.pattern + "$")
        self.assertEqual(rules.MODULE_ID_PATTERN, models._MODULE_ID_PATTERN)

    def test_the_version_pattern_accepts_what_the_contract_accepts_and_nothing_else(self):
        import re

        from apps.modules.errors import VersionError
        from apps.modules.versions import parse_version

        pattern = re.compile(models._VERSION_PATTERN)
        for text in ("0.0.0", "1.0.0", "1.10.0", "10.20.30", "999999999.999999999.999999999", "1.2", "01.2.3", "v1.2.3", "1.2.3-beta", "1.2.3+5", " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3.4", "1.2.x", "", "1000000000.0.0", "-1.0.0"):
            try:
                parse_version(text)
                valid = True
            except VersionError:
                valid = False
            with self.subTest(text=text):
                self.assertEqual(pattern.fullmatch(text) is not None and not text.endswith("\n"), valid)

    def test_the_fingerprint_pattern_is_the_registrys_sha256_hex(self):
        import re

        from tests.module_support import named
        from apps.modules.registry import ModuleRegistry

        self.assertTrue(re.fullmatch(models._FINGERPRINT_PATTERN, ModuleRegistry.build([named("alpha_mod")]).fingerprint))

    def test_module_state_rows_cannot_be_deleted_through_the_orm(self):
        with self.assertRaises(ImmutableRecordError):
            ModuleState().delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleState.objects.all().delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleState.objects.filter(module_id="alpha_mod").delete()

    def test_namespace_claims_are_append_only_through_the_orm(self):
        claim = ModuleNamespaceClaim(namespace="alpha_mod", module_id="alpha_mod")
        claim._state.adding = False  # a row that exists
        with self.assertRaises(ImmutableRecordError):
            claim.save()
        with self.assertRaises(ImmutableRecordError):
            claim.save(update_fields=["module_id"])
        with self.assertRaises(ImmutableRecordError):
            claim.delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleNamespaceClaim.objects.all().delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleNamespaceClaim.objects.all().update(module_id="someone_else")  # a namespace cannot be reassigned
        with self.assertRaises(ImmutableRecordError):
            ModuleNamespaceClaim.objects.bulk_update([], ["module_id"])

    def test_no_django_permission_exists_for_either_table(self):
        for model in (ModuleState, ModuleNamespaceClaim, ModuleHostMarker):
            self.assertEqual(tuple(model._meta.default_permissions), ())
            self.assertEqual(tuple(model._meta.permissions), ())

    def test_roles_are_unchanged_by_the_new_tables(self):
        from apps.accounts.roles import ALL_PERMISSIONS

        self.assertFalse([p for p in ALL_PERMISSIONS if "module" in p])


class MigrationTests(SimpleTestCase):
    def test_the_migration_is_one_additive_initial_migration_with_no_dependency(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.get_migration("module_host", "0001_initial")
        self.assertTrue(migration.initial)
        self.assertEqual(migration.dependencies, [])
        self.assertEqual([type(op).__name__ for op in migration.operations], ["CreateModel"] * 3)
        self.assertEqual(sorted(op.name for op in migration.operations), ["ModuleHostMarker", "ModuleNamespaceClaim", "ModuleState"])
        self.assertEqual(sorted(k for app, k in loader.graph.leaf_nodes() if app == "module_host"), ["0001_initial"])

    def test_the_migration_file_never_deletes_alters_or_runs_sql(self):
        code = code_only(HOST / "migrations" / "0001_initial.py")
        for word in ("RunSQL", "RunPython", "DeleteModel", "RemoveField", "AlterField", "RenameField", "RenameModel", "AlterModelTable", ".delete(", "DELETE", "DROP", "TRUNCATE", "cursor", "execute"):
            self.assertNotIn(word, code, word)

    def test_model_state_and_the_migration_agree_without_a_database(self):
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(None, ignore_no_migrations=True)
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(django_apps), NonInteractiveMigrationQuestioner(specified_apps={"module_host"}))
        self.assertEqual(detector.changes(graph=loader.graph, trim_to_apps={"module_host"}), {})

    def test_no_existing_application_migration_was_given_a_new_dependency_on_the_host(self):
        for path in sources(BACKEND / "apps", migrations=True):
            if "migrations" in path.parts and "module_host" not in path.parts:
                self.assertNotIn("module_host", path.read_text(encoding="utf-8"), path.as_posix())

    def test_the_whole_project_has_no_pending_model_change(self):
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(None, ignore_no_migrations=True)
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(django_apps), NonInteractiveMigrationQuestioner())
        self.assertEqual(detector.changes(graph=loader.graph), {})
