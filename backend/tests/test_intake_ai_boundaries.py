"""DB-free static tests: AI-2a adds ONE extractor to Request Intake and changes nothing around it.

``ast`` and source text only: these show what the code can reach by import and what it names, not what it does at run time. Behaviour is in the other
``test_intake_ai_*`` files. No database, no network.
"""

import ast
import re
import unittest
from pathlib import Path

from tests.test_module_boundaries import absolute_imports

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
INTAKE = BACKEND / "apps" / "intake"
GATEWAY = BACKEND / "apps" / "ai_gateway"


def sources(directory):
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts and "migrations" not in p.parts)


def code_only(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class AIFilesAreSmallAndSeparateTests(unittest.TestCase):
    def test_the_prompt_schema_and_conversion_are_pure_and_import_no_gateway_and_no_project_app(self):
        names = absolute_imports(INTAKE / "ai_output.py")
        self.assertEqual({n.split(".")[0] for n in names}, {"re", "collections", "decimal", "workflows"})
        self.assertFalse({n for n in names if n.startswith(("apps", "config", "django"))})

    def test_the_extractor_is_the_only_file_of_the_app_that_imports_the_gateway_and_imports_only_its_public_names(self):
        importers = {p.name for p in sources(INTAKE) if any(n == "apps.ai_gateway" or n.startswith("apps.ai_gateway.") for n in absolute_imports(p))}
        self.assertEqual(importers, {"ai_extraction.py"})
        tree = ast.parse((INTAKE / "ai_extraction.py").read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "apps.ai_gateway" for a in n.names}
        self.assertEqual(imported, {"AIGateway", "AIGatewayError", "GenerationRequest", "PolicyContext"})
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
        self.assertEqual({m for m in modules if m.startswith("apps.ai_gateway")}, {"apps.ai_gateway"})  # no adapter, transport or provider module

    def test_the_extractor_calls_the_gateway_and_nothing_else_for_data(self):
        code = code_only(INTAKE / "ai_extraction.py")
        self.assertIn("self._gateway.generate(", code)
        for forbidden in ("http", "socket", "urllib", "ssl", "requests", "send(", "environ", "getenv", "open(", "print(", "logging", "logger"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_nothing_in_the_app_touches_the_environment_or_a_credential(self):
        for path in sources(INTAKE):
            with self.subTest(path.name):
                imports = {n.split(".")[0] for n in absolute_imports(path)}
                self.assertFalse(imports & {"os", "config", "socket", "ssl", "http", "urllib", "requests", "subprocess"}, path.name)
                code = code_only(path)
                for token in ("environ", "getenv", "AP_AI_", "AP_INTAKE_", "api_key", "API_KEY", "Ocp-Apim", "charge_code", "workbench"):
                    self.assertNotIn(token, code, f"{path.name}: {token}")

    def test_the_app_still_logs_no_content(self):
        for path in sources(INTAKE):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                    with self.subTest(path.name, line=node.lineno):
                        names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)} | {n.attr for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Attribute)}
                        self.assertFalse(names & {"text", "payload", "draft", "output", "prompt", "response", "source", "answer", "message"}, names)


class GenericGatewayStaysGenericTests(unittest.TestCase):
    def test_the_gateway_package_knows_nothing_about_request_intake(self):
        for path in sources(GATEWAY):
            text = path.read_text(encoding="utf-8").lower()
            for word in ("request_intake", "requester", "apps.intake", "ambiguit", "purchase"):
                with self.subTest(path.name, word=word):
                    self.assertNotIn(word, text)

    def test_the_gateway_package_has_exactly_the_files_it_had(self):
        names = {p.relative_to(GATEWAY).as_posix() for p in sources(GATEWAY)}
        self.assertEqual(
            names,
            {
                "__init__.py", "adapters/__init__.py", "adapters/deterministic.py", "adapters/openai_compatible.py", "adapters/workbench.py", "contracts.py", "errors.py", "gateway.py",
                "jsonvalue.py", "network.py", "policy.py", "provider.py", "registry.py", "structured.py", "transport.py",
            },
        )  # fmt: skip  (AI-CON-1 added network.py and adapters/openai_compatible.py)

    def test_the_production_gateway_root_is_still_empty_and_the_workbench_is_still_opt_in(self):
        from config import ai_gateway

        self.assertEqual((ai_gateway.BUILTIN_PROVIDERS, ai_gateway.BUILTIN_PROFILES), ((), ()))
        for path in sorted((BACKEND / "config").glob("*.py")):
            names = absolute_imports(path)
            if path.name not in ("ai_workbench.py", "intake_extraction.py"):
                self.assertFalse({n for n in names if n.startswith("config.ai_workbench")}, path.name)


class NothingElseChangedTests(unittest.TestCase):
    def test_no_model_migration_or_database_field_was_added(self):
        self.assertEqual(sorted(p.name for p in (INTAKE / "migrations").glob("*.py")), ["0001_initial.py", "0002_revisionedit_request_permissions.py", "__init__.py"])
        text = (INTAKE / "models.py").read_text(encoding="utf-8").lower()
        for word in ("api_key", "credential", "secret", "password", "token", "extraction_mode", "policy_mode", "profile_id"):
            self.assertNotIn(word, text, word)

    def test_the_extraction_step_services_and_validation_do_not_mention_ai(self):
        for relative in ("apps/intake/extraction.py", "apps/intake/services.py", "apps/intake/draft_json.py", "apps/intake/review.py", "apps/intake/editing.py", "apps/intake/submission.py"):
            text = (BACKEND / relative).read_text(encoding="utf-8")
            with self.subTest(relative):
                self.assertNotRegex(text, r"(?i)ai_gateway|workbench|AIExtraction|AIRequestExtractor|ai_output")

    def test_the_workflow_packages_are_untouched_by_ai(self):
        for path in sources(BACKEND / "workflows"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotRegex(text, r"(?i)ai_gateway|workbench|AIExtraction|ai_output")

    def test_there_is_still_one_queue_one_worker_and_one_task_kind_for_extraction(self):
        names = {p.relative_to(BACKEND / "apps" / "tasks").as_posix() for p in sources(BACKEND / "apps" / "tasks")}
        self.assertEqual(names, {"__init__.py", "apps.py", "errors.py", "handlers.py", "management/__init__.py", "management/commands/__init__.py", "management/commands/runworker.py", "models.py", "policy.py", "services.py", "worker.py"})
        from apps.intake.registry import PRODUCTION_REGISTRY

        self.assertEqual(set(PRODUCTION_REGISTRY), {"test.add_numbers", "request_intake.extract"})

    def test_the_module_manifest_is_unchanged(self):
        from apps.intake.module import MANIFEST

        self.assertEqual(MANIFEST.module_version, "1.0.0")
        self.assertEqual(MANIFEST.capabilities_required, ("task.enqueue", "audit.record", "case.run", "revision.approval", "permissions.check"))
        self.assertNotIn("ai.invoke", MANIFEST.capabilities_required)  # the module does not claim an AI capability; the gateway is composed by the worker

    def test_the_audit_vocabulary_gained_nothing_for_extraction(self):
        text = (BACKEND / "apps" / "audit" / "events.py").read_text(encoding="utf-8").lower()
        text = re.sub(r"ai_settings\w*", "", text)  # the AI settings API's own action and subject type (not an extraction event) are tested in test_ai_settings_audit
        self.assertNotRegex(text, r"\bai[._]|extraction_failed|ai_extract")

    def test_no_django_setting_names_the_extraction_mode_a_provider_or_a_key(self):
        from django.conf import settings

        names = [n for n in dir(settings) if n.isupper()]
        self.assertEqual([n for n in names if re.search(r"EXTRACTION|WORKBENCH|AI_|API_KEY|POLICY_MODE", n)], [])

    def test_the_new_variables_are_documented_with_no_value_in_the_example_file(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        for name in ("AP_INTAKE_EXTRACTION_MODE", "AP_AI_POLICY_MODE", "AP_INTAKE_AI_TIMEOUT_SECONDS"):
            with self.subTest(name):
                lines = [line for line in text.splitlines() if name in line]
                self.assertTrue(lines, name)
                for line in lines:
                    self.assertTrue(line.lstrip().startswith("#") or line.strip().endswith("="), line)  # commented out, or empty: never a value that switches AI on
        self.assertNotRegex(text, r"(?m)^AP_INTAKE_EXTRACTION_MODE=\S")
        self.assertNotRegex(text, r"(?m)^AP_AI_POLICY_MODE=\S")


if __name__ == "__main__":
    unittest.main()
