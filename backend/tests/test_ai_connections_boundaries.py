"""Static boundary tests for AI-SET-2 (no database, no network): where connections may be known, that the settings app stays free of providers, environment and credentials, and that nothing
on the task path learned about them."""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "apps" / "ai_settings"


def source(path):
    return path.read_text(encoding="utf-8")


def imports_of(path):
    names = []
    for node in ast.walk(ast.parse(source(path))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    return names


class SettingsAppStaysPureTests(SimpleTestCase):
    def test_the_settings_app_reads_no_environment_and_names_no_provider_or_variable(self):
        for path in APP.glob("*.py"):
            text = source(path)
            with self.subTest(path.name):
                for needle in ("os.environ", "getenv", "environ[", "environ.get"):
                    self.assertNotIn(needle, text)
                for provider in ("openai", "ollama", "kpmg", "AP_AI_OPENAI", "AP_AI_OLLAMA", "AP_AI_WORKBENCH"):
                    self.assertNotIn(provider, text.replace("openai_compatible", "").replace("OpenAI-compatible", "") if provider == "openai" else text)

    def test_the_only_credential_field_of_a_connection_is_a_reference_name(self):
        from apps.ai_settings.rules import CONNECTION_FIELDS

        self.assertEqual([name for name in CONNECTION_FIELDS if "credential" in name], ["credential_ref"])

    def test_the_rules_stay_pure_standard_library(self):
        allowed = {"dataclasses", "re", "typing", "collections.abc", "__future__", ".errors", "enum", "math"}
        for name in imports_of(APP / "rules.py"):
            self.assertIn(name, allowed)

    def test_the_provider_types_are_not_listed_in_the_app(self):
        # the static allowlist lives in the composition root; the app only applies a catalog it is given
        self.assertNotIn("PROVIDER_TYPES", source(APP / "rules.py"))


class CompositionBoundaryTests(SimpleTestCase):
    def test_only_the_composition_root_imports_the_connection_builders(self):
        holders = []
        for folder in ("apps", "config"):
            for path in (BACKEND / folder).rglob("*.py"):
                if "migrations" in path.parts:
                    continue
                tree = ast.parse(source(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and (node.module == "config.ai_connections" or (node.module == "config" and any(a.name == "ai_connections" for a in node.names))):
                        holders.append(path.relative_to(BACKEND).as_posix())
                    elif isinstance(node, ast.Import) and any(a.name == "config.ai_connections" for a in node.names):
                        holders.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(sorted(set(holders)), ["config/intake_extraction.py"])

    def test_nothing_on_the_task_path_or_in_the_app_packages_imports_the_connection_composition(self):
        for relative in ("apps/intake/handlers.py", "apps/intake/services.py", "apps/intake/ai_extraction.py", "apps/tasks/worker.py", "config/task_registry.py", "apps/api/views_ai_settings.py", "config/urls.py"):
            with self.subTest(relative):
                self.assertNotIn("ai_connections", source(BACKEND / relative))

    def test_importing_the_composition_reads_no_environment(self):
        text = source(BACKEND / "config" / "ai_connections.py")
        for node in ast.parse(text).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # the docstring
            segment = ast.get_source_segment(text, node) or ""
            self.assertNotIn("environ", segment)
            self.assertNotIn("getenv", segment)

    def test_the_composition_has_no_network_library_and_the_gateway_rules_are_reused_not_copied(self):
        text = source(BACKEND / "config" / "ai_connections.py")
        names = imports_of(BACKEND / "config" / "ai_connections.py")
        for forbidden in ("socket", "ssl", "urllib", "http", "requests", "ipaddress"):
            self.assertFalse(any(name == forbidden or name.startswith(forbidden + ".") for name in names), forbidden)
        self.assertNotIn("DENIED_NETWORKS", text)  # the denylist is network.py's alone
        self.assertNotIn("is_private", text)

    def test_the_worker_and_the_settings_variables_are_unchanged(self):
        text = source(BACKEND / "apps" / "tasks" / "management" / "commands" / "runworker.py")
        self.assertNotIn("ai_settings", text)
        self.assertNotIn("ai_connection", text)
        self.assertNotIn("connection_id", text)
        for relative in ("config/settings/base.py", "config/env.py"):
            self.assertNotIn("AP_AI_CRED", source(BACKEND / relative))  # a credential variable is read where it is resolved, never declared in the Django settings

    def test_the_credential_prefix_is_the_only_variable_namespace_added(self):
        found = set()
        for folder in ("apps", "config"):
            for path in (BACKEND / folder).rglob("*.py"):
                if path.name == "ai_connections.py" or path.name == "rules.py":
                    found.update(re.findall(r"\bAP_[A-Z0-9_]+", source(path)))
        added = {name for name in found if name.startswith("AP_AI_CRED")}
        self.assertEqual(added, {"AP_AI_CRED_"})
