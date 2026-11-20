"""DB-free static tests: the module contract stays pure, stays out of Core's way, and activates nothing in MOD-1a.

These read source code with ``ast`` (nothing is imported for side effects) and a few settings values. They are architecture checks, not
security controls: they show what the code CAN reach by import, not what it does at run time. No database is touched.
"""

import ast
import re
import sys
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parent.parent
MODULES = BACKEND / "apps" / "modules"
STDLIB = set(sys.stdlib_module_names)


def _sources(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "migrations" in path.parts:
            continue
        yield path


def absolute_imports(path: Path) -> set:
    """Absolute module names imported anywhere in the file (relative imports are within the package and not listed)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def non_test_sources():
    for top in ("apps", "config", "workflows"):
        yield from _sources(BACKEND / top)


def imports_of(prefix: str):
    """Non-test files that import ``prefix`` (or something under it)."""
    hits = []
    for path in non_test_sources():
        if any(name == prefix or name.startswith(prefix + ".") for name in absolute_imports(path)):
            hits.append(path.relative_to(BACKEND).as_posix())
    return hits


class ModulePackageIsPureTests(SimpleTestCase):
    def test_it_imports_only_the_standard_library_and_itself(self):
        files = list(_sources(MODULES))
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            for name in absolute_imports(path):
                top = name.split(".")[0]
                with self.subTest(file=path.name, imported=name):
                    self.assertTrue(top in STDLIB or name == "apps.modules" or name.startswith("apps.modules."), f"{path.name} imports {name}")

    def test_it_imports_no_django_no_other_project_package_and_no_business_workflow(self):
        forbidden = ("django", "rest_framework", "psycopg", "workflows", "config", "apps.intake", "apps.cases", "apps.tasks", "apps.audit", "apps.accounts", "apps.ai_settings", "apps.api", "apps.health")
        for path in _sources(MODULES):
            for name in absolute_imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertFalse(any(name == f or name.startswith(f + ".") for f in forbidden), f"{path.name} imports {name}")

    def test_it_is_not_a_django_app(self):
        present = {p.name for p in MODULES.iterdir()}
        for name in ("models.py", "migrations", "admin.py", "apps.py", "urls.py", "views.py", "management", "templates", "signals.py"):
            self.assertNotIn(name, present)
        self.assertNotIn("apps.modules", settings.INSTALLED_APPS)
        self.assertNotIn("modules", {config.label for config in django_apps.get_app_configs()})

    def test_it_defines_no_second_queue_or_workflow_engine(self):
        pattern = re.compile(r"Queue|Worker|Scheduler|Engine|Executor|Dispatcher|StateMachine")
        for path in _sources(MODULES):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    with self.subTest(file=path.name, name=node.name):
                        self.assertIsNone(pattern.search(node.name), f"{path.name}: {node.name}")


class CoreStaysIndependentTests(SimpleTestCase):
    def test_only_the_composition_root_and_the_package_itself_import_the_module_package(self):
        importers = set(imports_of("apps.modules"))
        inside = {p for p in importers if p.startswith("apps/modules/")}
        # config/audit_vocabulary.py (MOD-1b S3) is the second composition root: it reads a registry and builds the audit vocabulary from it.
        # apps/module_host (MOD-1b S4) is the module host: it is handed a registry and checks it (the type, the namespace continuity rule, the errors).
        # apps/module_host/context.py (MOD-1b S6) is the ModuleContext: it is handed a registry and checks its type.
        host = {"apps/module_host/cli.py", "apps/module_host/context.py", "apps/module_host/gate.py", "apps/module_host/services.py"}
        # apps/intake/module.py (MOD-2) is Request Intake's manifest: it builds ModuleManifest objects, the one thing a module package may import from here.
        self.assertEqual(importers - inside, {"config/modules.py", "config/audit_vocabulary.py", "apps/intake/module.py"} | host)

    def test_nothing_in_the_application_imports_the_composition_root_yet(self):
        # MOD-1a activated nothing. S3 added the audit vocabulary composition root (imported by nothing in the application). S4 activates the registry in
        # exactly two kinds of place: the worker's composition root (the kind gate) and the four administrator commands of the module host. No view,
        # serializer, settings module or other entry point calls build_registry.
        expected = [
            "apps/module_host/management/commands/module_disable.py",
            "apps/module_host/management/commands/module_enable.py",
            "apps/module_host/management/commands/module_status.py",
            "apps/module_host/management/commands/module_sync.py",
            "config/audit_vocabulary.py",
            "config/module_context.py",  # MOD-1b S6: the ModuleContext composition root; nothing imports it yet (tests/test_module_context.py)
            "config/task_registry.py",
        ]
        self.assertEqual(sorted(imports_of("config.modules")), expected)
        self.assertEqual(imports_of("config.audit_vocabulary"), [])
        allowed = {Path(name).name for name in expected} | {"modules.py"}
        for path in non_test_sources():
            if path.name not in allowed:
                self.assertNotIn("build_registry", path.read_text(encoding="utf-8"), path.name)

    def test_the_composition_root_imports_only_the_module_package_the_manifests_and_the_standard_library(self):
        # MOD-2: the ONE non-library import besides apps.modules is Request Intake's manifest module, by its fixed name, and nothing else of apps.intake.
        for name in absolute_imports(BACKEND / "config" / "modules.py"):
            with self.subTest(imported=name):
                self.assertTrue(
                    name.split(".")[0] in STDLIB or name == "apps.modules" or name.startswith("apps.modules.") or name in ("apps.intake.module", "apps.intake.module.MANIFEST"),
                    name,
                )

    def test_the_composition_root_names_modules_only_in_a_static_tuple(self):
        source = (BACKEND / "config" / "modules.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigned = [n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "BUILTIN_MODULES"]
        self.assertEqual(len(assigned), 1)
        self.assertIsInstance(assigned[0].value, ast.Tuple)
        code = self._code_only(source)
        for forbidden in ("importlib", "__import__", "pkgutil", "listdir", "glob", "entry_points", "environ", "settings", "open("):
            self.assertNotIn(forbidden, code, forbidden)

    @staticmethod
    def _code_only(source: str) -> str:
        """The source without comments and docstrings, so words in explanations do not count."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    def test_core_packages_do_not_import_request_intake_or_the_api_layer(self):
        for package in ("accounts", "audit", "cases", "tasks", "health", "modules"):
            for path in _sources(BACKEND / "apps" / package):
                for name in absolute_imports(path):
                    with self.subTest(file=path.relative_to(BACKEND).as_posix(), imported=name):
                        self.assertFalse(
                            name == "apps.intake" or name.startswith("apps.intake.") or name == "apps.api" or name.startswith("apps.api.") or name.startswith("workflows.request_intake"),
                            f"{path.name} imports {name}",
                        )

    def test_the_task_queue_gained_no_dependency_on_any_business_module_or_the_module_package(self):
        allowed_project_imports = {"apps.tasks", "config.task_registry"}  # runworker.py imports the composition root, as before
        for path in _sources(BACKEND / "apps" / "tasks"):
            for name in absolute_imports(path):
                top = name.split(".")[0]
                if top in STDLIB or top == "django":
                    continue
                with self.subTest(file=path.name, imported=name):
                    self.assertTrue(any(name == a or name.startswith(a + ".") for a in allowed_project_imports), f"{path.name} imports {name}")
        self.assertEqual([p for p in imports_of("apps.modules") if p.startswith("apps/tasks/")], [])

    def test_only_request_intakes_manifest_imports_the_module_package_and_nothing_in_it_imports_the_composition_root(self):
        # MOD-2: apps/intake/module.py builds the manifest from apps.modules.manifest. No other file of the app imports the package, and none imports config.
        for path in _sources(BACKEND / "apps" / "intake"):
            names = absolute_imports(path)
            self.assertFalse({n for n in names if n == "config" or n.startswith("config.")}, path.name)
            package = {n for n in names if n == "apps.modules" or n.startswith("apps.modules.")}
            if path.name == "module.py":
                self.assertTrue(package and all(n == "apps.modules.manifest" or n.startswith("apps.modules.manifest.") for n in package), package)
            else:
                self.assertFalse(package, path.name)
        for path in _sources(BACKEND / "workflows"):
            self.assertFalse({n for n in absolute_imports(path) if n.startswith(("apps.modules", "config.modules"))}, path.name)


class ExistingBehaviorIsUnchangedTests(SimpleTestCase):
    """MOD-1a adds a package and a composition root; it does not touch how the worker, the API or Request Intake are composed."""

    def test_the_worker_composition_is_still_built_from_request_intake(self):
        from apps.intake.registry import PRODUCTION_REGISTRY
        from config.task_registry import COMPOSITION

        self.assertIs(COMPOSITION.registry, PRODUCTION_REGISTRY)
        self.assertEqual(set(PRODUCTION_REGISTRY), {"test.add_numbers", "request_intake.extract"})  # unchanged by MOD-2: the kind is a module's now, the handler is the same
        source = (BACKEND / "config" / "task_registry.py").read_text(encoding="utf-8")
        self.assertIn("from apps.intake.registry import PRODUCTION_REGISTRY", source)
        # MOD-1b S4: the composition root now also installs the module kind gate (config.modules is read there, and only there in this file); the
        # handler allowlist and the reconciler are exactly what they were, and the gate leaves the legacy kinds claimable (tests/test_module_host_gate.py).
        self.assertIn("from config.modules import build_registry", source)
        self.assertNotIn("apps.modules", source)

    def test_installed_apps_gained_nothing(self):
        self.assertEqual(
            [a for a in settings.INSTALLED_APPS if a.startswith("apps.")],
            ["apps.accounts", "apps.ai_settings", "apps.api", "apps.audit", "apps.cases", "apps.health", "apps.intake", "apps.module_host", "apps.tasks"],
        )

    def test_the_http_routes_are_the_ones_the_api_documents(self):
        from django.urls import get_resolver

        def names(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from names(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        routes = sorted(names(get_resolver().url_patterns))
        self.assertEqual(sum(1 for r in routes if r.startswith("api/requests/")), 6)
        self.assertEqual(sum(1 for r in routes if r.startswith("api/auth/")), 4)
        self.assertEqual(sum(1 for r in routes if r.startswith("api/health/")), 2)
        self.assertFalse([r for r in routes if "module" in r], "no module route exists yet")
