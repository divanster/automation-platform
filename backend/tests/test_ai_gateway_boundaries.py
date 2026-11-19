"""DB-free static tests: the AI Gateway stays pure, stays out of the business modules' way, and activates nothing in AI-1a.

These read source code with ``ast`` (nothing is imported for side effects). They are architecture checks, not security controls: they show
what the code CAN reach by import, not what it does at run time. No database is touched.
"""

import ast
import sys
import unittest
from pathlib import Path

from tests.test_module_boundaries import absolute_imports, non_test_sources

BACKEND = Path(__file__).resolve().parent.parent
GATEWAY = BACKEND / "apps" / "ai_gateway"
ADAPTERS = GATEWAY / "adapters"
STDLIB = set(sys.stdlib_module_names)

# Standard-library modules that could reach the network, run other code, load code by name, or change the process.
FORBIDDEN_STDLIB = {
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "socketserver", "asyncio", "selectors",
    "subprocess", "multiprocessing", "threading", "concurrent", "ctypes", "importlib", "pkgutil", "runpy", "imp", "zipimport", "pickle", "shelve",
    "marshal", "os", "sys", "shutil", "tempfile", "pathlib", "glob", "fnmatch", "sqlite3", "webbrowser", "code", "codeop", "ast",
}  # fmt: skip
FORBIDDEN_NAMES = ("importlib", "__import__", "pkgutil", "entry_points", "environ", "getenv", "subprocess", "socket", "urlopen", "getattr(__")


def gateway_sources():
    return [p for p in sorted(GATEWAY.rglob("*.py")) if "__pycache__" not in p.parts]


def code_only(path: Path) -> str:
    """The source without comments and docstrings, so words in explanations do not count."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def relative_imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            yield node.level, node.module or ""


class GatewayPackageIsPureTests(unittest.TestCase):
    def test_it_has_the_files_the_design_names(self):
        names = {p.relative_to(GATEWAY).as_posix() for p in gateway_sources()}
        self.assertEqual(
            names,
            {"__init__.py", "contracts.py", "errors.py", "gateway.py", "jsonvalue.py", "policy.py", "provider.py", "registry.py", "structured.py", "transport.py", "adapters/__init__.py", "adapters/deterministic.py", "adapters/workbench.py"},
        )

    def test_it_imports_only_the_standard_library_and_itself(self):
        for path in gateway_sources():
            for name in absolute_imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertTrue(name.split(".")[0] in STDLIB or name == "apps.ai_gateway" or name.startswith("apps.ai_gateway."), f"{path.name} imports {name}")

    def test_it_imports_no_django_no_other_project_package_and_no_business_workflow(self):
        forbidden = ("django", "rest_framework", "psycopg", "workflows", "config", "apps.intake", "apps.cases", "apps.tasks", "apps.audit", "apps.accounts", "apps.api", "apps.health", "apps.modules", "apps.module_host")
        for path in gateway_sources():
            for name in absolute_imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertFalse(any(name == f or name.startswith(f + ".") for f in forbidden), f"{path.name} imports {name}")

    def test_it_uses_no_network_process_import_or_environment_facility(self):
        for path in gateway_sources():
            for name in absolute_imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertNotIn(name.split(".")[0], FORBIDDEN_STDLIB)
            code = code_only(path)
            for token in FORBIDDEN_NAMES:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, code)

    def test_it_is_not_a_django_app(self):
        from django.apps import apps as django_apps
        from django.conf import settings

        present = {p.name for p in GATEWAY.iterdir()}
        for name in ("models.py", "migrations", "admin.py", "apps.py", "urls.py", "views.py", "management", "templates", "signals.py", "settings.py"):
            self.assertNotIn(name, present)
        self.assertNotIn("apps.ai_gateway", settings.INSTALLED_APPS)
        self.assertNotIn("ai_gateway", {config.label for config in django_apps.get_app_configs()})

    def test_the_core_of_the_gateway_never_imports_an_adapter(self):
        for path in gateway_sources():
            if path.parent == ADAPTERS:
                continue
            for level, module in relative_imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertFalse(module == "adapters" or module.startswith("adapters."))
            for name in absolute_imports(path):
                self.assertFalse(name.startswith("apps.ai_gateway.adapters"), f"{path.name} imports {name}")

    def test_adapters_import_only_the_gateway_contract_above_them(self):
        allowed = {"contracts", "errors", "jsonvalue", "policy", "provider", "transport"}
        for path in sorted(ADAPTERS.glob("*.py")):
            for level, module in relative_imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertEqual(level, 2)
                    self.assertIn(module, allowed)

    def test_the_package_exports_do_not_include_the_test_adapter(self):
        import apps.ai_gateway as package

        self.assertNotIn("DeterministicTestProvider", dir(package))
        self.assertNotIn("DeterministicTestProvider", package.__all__)

    def test_no_dynamic_provider_loading_exists(self):
        for path in gateway_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    with self.subTest(file=path.name, call=node.func.id):
                        self.assertNotIn(node.func.id, {"__import__", "eval", "exec", "compile", "open", "globals", "locals"})

    def test_it_defines_no_second_queue_or_workflow_engine(self):
        import re

        pattern = re.compile(r"Queue|Worker|Scheduler|Engine|Executor|Dispatcher|StateMachine|Retry|Fallback")
        for path in gateway_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    with self.subTest(file=path.name, name=node.name):
                        self.assertIsNone(pattern.search(node.name), f"{path.name}: {node.name}")


class GatewayStaysOutOfCoreAndBusinessModulesTests(unittest.TestCase):
    def test_only_the_composition_root_imports_the_gateway(self):
        importers = []
        for path in non_test_sources():
            if any(name == "apps.ai_gateway" or name.startswith("apps.ai_gateway.") for name in absolute_imports(path)):
                importers.append(path.relative_to(BACKEND).as_posix())
        outside = sorted(p for p in importers if not p.startswith("apps/ai_gateway/"))
        # AI-1b: the opt-in Workbench composition and the one real transport; the production root is still config/ai_gateway.py alone
        self.assertEqual(outside, ["config/ai_gateway.py", "config/ai_http_transport.py", "config/ai_workbench.py"])

    def test_nothing_imports_the_composition_root_yet(self):
        for path in non_test_sources():
            names = absolute_imports(path)
            with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                self.assertFalse({n for n in names if n == "config.ai_gateway" or n.startswith("config.ai_gateway.")})
                self.assertFalse({n for n in names if n.startswith(("config.ai_workbench", "config.ai_http_transport")) and path.name != "ai_workbench.py"})

    def test_request_intake_the_workflows_and_core_do_not_use_the_gateway(self):
        for package in ("intake", "tasks", "cases", "audit", "accounts", "api", "health", "modules", "module_host"):
            for path in BACKEND.joinpath("apps", package).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn("ai_gateway", text)
        for path in BACKEND.joinpath("workflows").rglob("*.py"):
            self.assertNotIn("ai_gateway", path.read_text(encoding="utf-8"), path.name)

    def test_request_intake_extraction_still_uses_its_own_fixture_parser(self):
        from apps.intake import extraction
        from workflows.request_intake import DeterministicTestExtractor

        self.assertIs(extraction.DeterministicTestExtractor, DeterministicTestExtractor)
        self.assertNotIn("ai_gateway", (BACKEND / "apps" / "intake" / "extraction.py").read_text(encoding="utf-8"))

    def test_nothing_outside_tests_and_the_adapter_package_names_the_test_adapter(self):
        for path in non_test_sources():
            if "adapters" in path.parts and "ai_gateway" in path.parts:
                continue
            with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                imported = {n for n in absolute_imports(path) if n.startswith("apps.ai_gateway.adapters")}
                if path.relative_to(BACKEND).as_posix() == "config/ai_workbench.py":
                    imported -= {"apps.ai_gateway.adapters", "apps.ai_gateway.adapters.workbench"}  # AI-1b: the one real adapter, named in the opt-in composition
                self.assertFalse(imported)

    def test_the_module_contract_still_marks_ai_invoke_as_a_future_capability(self):
        from apps.modules.registry import CORE_CAPABILITIES, FUTURE_CAPABILITIES

        self.assertIn("ai.invoke", FUTURE_CAPABILITIES)
        self.assertNotIn("ai.invoke", CORE_CAPABILITIES)

    def test_no_http_route_setting_or_environment_variable_was_added(self):
        from django.conf import settings

        self.assertFalse([name for name in dir(settings) if "AI_GATEWAY" in name.upper() or name.upper().startswith(("AI_", "AP_AI"))])
        for path in sorted((BACKEND / "config" / "settings").glob("*.py")) + [BACKEND / "config" / "env.py", BACKEND / "config" / "urls.py"]:
            self.assertNotIn("ai_gateway", path.read_text(encoding="utf-8").lower(), path.name)


class CompositionRootTests(unittest.TestCase):
    def test_the_builtin_tuples_are_static_and_empty(self):
        tree = ast.parse((BACKEND / "config" / "ai_gateway.py").read_text(encoding="utf-8"))
        assigned = {n.target.id: n.value for n in tree.body if isinstance(n, ast.AnnAssign)}
        self.assertEqual(set(assigned), {"BUILTIN_PROVIDERS", "BUILTIN_PROFILES"})
        for name, value in assigned.items():
            with self.subTest(name):
                self.assertIsInstance(value, ast.Tuple)
                self.assertEqual(value.elts, [])

    def test_the_composition_root_imports_only_the_gateway_package_and_reads_nothing_dynamic(self):
        path = BACKEND / "config" / "ai_gateway.py"
        for name in absolute_imports(path):
            with self.subTest(imported=name):
                self.assertTrue(name == "apps.ai_gateway" or name.startswith("apps.ai_gateway.") and not name.startswith("apps.ai_gateway.adapters"), name)
        code = code_only(path)
        for token in FORBIDDEN_NAMES + ("settings", "allow_test_providers"):
            self.assertNotIn(token, code, token)

    def test_the_production_gateway_has_no_provider_and_fails_closed(self):
        from apps.ai_gateway import AIGatewayError, GenerationRequest, PolicyContext, PolicyMode
        from config import ai_gateway as composition

        gateway = composition.build_gateway()
        self.assertEqual((composition.build_provider_registry().provider_ids, gateway.profile_ids), ((), ()))
        request = GenerationRequest("any-profile", "Extract.", "text", {"type": "object"})
        for mode in PolicyMode:
            with self.subTest(mode.value), self.assertRaises(AIGatewayError) as ctx:
                gateway.generate(request, policy=PolicyContext(mode))
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "unknown_profile"))

    def test_the_production_composition_refuses_a_test_provider_even_if_one_were_listed(self):
        from apps.ai_gateway import AIGateway, AIGatewayError, ProviderRegistry
        from apps.ai_gateway.adapters.deterministic import DeterministicTestProvider
        from config import ai_gateway as composition

        with self.assertRaises(AIGatewayError) as ctx:
            AIGateway(ProviderRegistry.build([DeterministicTestProvider()]), composition.BUILTIN_PROFILES)  # what the composition root would do
        self.assertEqual(ctx.exception.reason, "test_provider_not_allowed")
