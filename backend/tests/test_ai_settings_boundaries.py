"""Static boundary tests for the AI settings foundation (no database, no network).

They pin the structure that keeps the feature small and safe: the settings app and the API know nothing of the AI Gateway (the composition root alone
bridges them), the rules are pure, the view is a thin adapter, the model has no place for a credential, and the migration only adds one table.
"""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "apps" / "ai_settings"
FORBIDDEN_TERMS = ("ai_gateway", "ai_workbench", "workbench", "requests", "urllib", "http.client", "socket")
SECRET_WORDS = re.compile(r"secret|token|password|api_?key|credential|header|endpoint|url|prompt", re.IGNORECASE)


def source(path):
    return path.read_text(encoding="utf-8")


def imports_of(path):
    tree = ast.parse(source(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    return names


class SettingsAppBoundaryTests(SimpleTestCase):
    def test_the_app_and_the_view_import_nothing_from_the_gateway_or_a_network_library(self):
        for path in (*APP.rglob("*.py"), BACKEND / "apps" / "api" / "views_ai_settings.py"):
            text = source(path)
            for term in FORBIDDEN_TERMS:
                with self.subTest(path=path.name, term=term):
                    self.assertNotRegex(text.lower(), rf"\b{re.escape(term)}\b" if term.isalpha() else re.escape(term))

    def test_the_rules_are_pure_standard_library(self):
        allowed = {"dataclasses", "re", "typing", "collections.abc", "__future__", ".errors", "enum", "math"}
        for name in imports_of(APP / "rules.py"):
            with self.subTest(name):
                self.assertIn(name, allowed)

    def test_the_rules_import_no_django(self):
        for name in imports_of(APP / "rules.py"):
            self.assertFalse(name.startswith("django"), name)
            self.assertFalse(name.startswith("rest_framework"), name)

    def test_the_errors_are_pure(self):
        for name in imports_of(APP / "errors.py"):
            self.assertFalse(name.startswith(("django", "rest_framework")), name)

    def test_the_settings_app_does_not_import_the_api_or_the_composition_root(self):
        for path in APP.rglob("*.py"):
            for name in imports_of(path):
                with self.subTest(path=path.name, name=name):
                    self.assertFalse(name.startswith(("apps.api", "config", "rest_framework")), name)

    def test_the_urls_module_does_not_name_the_gateway(self):
        self.assertNotIn("ai_gateway", source(BACKEND / "config" / "urls.py"))

    def test_gateway_imports_stay_in_the_composition_root(self):
        holders = []
        for folder in ("apps", "config"):
            for path in (BACKEND / folder).rglob("*.py"):
                if "migrations" in path.parts:
                    continue
                if any(name == "apps.ai_gateway" or name.startswith("apps.ai_gateway.") for name in imports_of(path)):
                    holders.append(path.relative_to(BACKEND).as_posix())
        for path in holders:
            if path.startswith("apps/ai_gateway/"):
                continue
            with self.subTest(path):
                self.assertNotIn(path.split("/")[1], {"ai_settings", "api", "accounts", "tasks", "cases", "audit"})


class ViewIsThinTests(SimpleTestCase):
    def test_the_view_has_no_database_access_no_logging_and_no_environment_access(self):
        text = source(BACKEND / "apps" / "api" / "views_ai_settings.py")
        for needle in ("objects.", "transaction", "import logging", "getLogger", "logger.", "os.environ", "getenv", "settings."):
            with self.subTest(needle):
                self.assertNotIn(needle, text)

    def test_the_view_calls_only_the_two_services(self):
        text = source(BACKEND / "apps" / "api" / "views_ai_settings.py")
        self.assertEqual(sorted(set(re.findall(r"services\.(\w+)\(", text))), ["get_extraction_settings", "update_extraction_settings"])

    def test_the_view_allows_only_get_and_put(self):
        from apps.api.views_ai_settings import AIExtractionSettingsView

        methods = {name for name in ("get", "put", "post", "patch", "delete") if hasattr(AIExtractionSettingsView, name)}
        self.assertEqual(methods, {"get", "put"})


class ModelHasNoSecretTests(SimpleTestCase):
    def test_no_column_can_hold_a_credential(self):
        from apps.ai_settings.models import AIExtractionSettings

        names = [field.name for field in AIExtractionSettings._meta.get_fields()]
        self.assertEqual(
            sorted(names),
            [
                "connection_id", "connections", "extraction_mode", "id", "policy_mode", "profile_id", "provider_id", "revision", "timeout_seconds", "updated_at",
                "updated_by_public_id",
            ],
        )  # fmt: skip
        for name in names:
            with self.subTest(name):
                self.assertIsNone(SECRET_WORDS.search(name))

    def test_the_connections_column_is_the_only_new_free_form_column_and_its_stored_shape_has_no_secret_field(self):
        from apps.ai_settings.rules import CONNECTION_FIELDS, PROFILE_FIELDS

        for name in (*CONNECTION_FIELDS, *PROFILE_FIELDS):
            with self.subTest(name):
                # a connection holds a URL and the NAME of a variable; no field of it is called, or can hold, a key, token, password or header
                self.assertIsNone(re.search(r"secret|token|password|api_?key|header|prompt", name, re.IGNORECASE))
        self.assertIn("credential_ref", CONNECTION_FIELDS)

    def test_no_model_permission_is_created(self):
        from apps.ai_settings.models import AIExtractionSettings

        self.assertEqual(AIExtractionSettings._meta.default_permissions, ())

    def test_the_model_stores_no_foreign_key_to_the_user(self):
        from apps.ai_settings.models import AIExtractionSettings

        self.assertEqual([f.name for f in AIExtractionSettings._meta.get_fields() if f.is_relation], [])


class MigrationIsAdditiveTests(SimpleTestCase):
    def test_the_initial_migration_only_creates_one_model_and_the_connections_one_is_additive_and_reversible(self):
        files = sorted(p.name for p in (APP / "migrations").glob("0*.py"))
        self.assertEqual(files, ["0001_initial.py", "0002_connections.py"])
        text = source(APP / "migrations" / "0001_initial.py")
        self.assertEqual(re.findall(r"migrations\.(\w+)\(", text), ["CreateModel"])
        self.assertIn("dependencies = [\n    ]", text)
        self.assertNotIn("RunSQL", text)
        self.assertNotIn("RunPython", text)
        later = source(APP / "migrations" / "0002_connections.py")
        self.assertEqual(re.findall(r"migrations\.(\w+)\(", later), ["AddField", "AddField", "RemoveConstraint", "AddConstraint"])  # no data is read, written or deleted
        self.assertIn("('ai_settings', '0001_initial')", later)
        for forbidden in ("RunSQL", "RunPython", "DeleteModel", "RemoveField", "AlterField", "RenameField"):
            self.assertNotIn(forbidden, later)
        self.assertEqual(re.findall(r"default=(\w+)", later), ["list"])  # the new column has a default; an existing row stays valid and keeps its meaning

    def test_no_other_app_gained_a_migration_for_this_feature(self):
        for app in ("accounts", "cases", "tasks", "intake", "module_host"):
            with self.subTest(app):
                text = "\n".join(source(p) for p in (BACKEND / "apps" / app / "migrations").glob("0*.py"))
                self.assertNotIn("ai_settings", text)

    def test_the_only_other_migration_is_the_audit_subject_type_one(self):
        named = sorted(p.name for p in (BACKEND / "apps" / "audit" / "migrations").glob("0*.py") if "ai_settings" in source(p))
        self.assertEqual(named, ["0003_ai_settings_subject_type.py"])
        text = source(BACKEND / "apps" / "audit" / "migrations" / "0003_ai_settings_subject_type.py")
        self.assertNotIn("RunSQL", text)
        self.assertNotIn("DeleteModel", text)
        self.assertNotIn(".delete(", text)


class RunWorkerUnchangedTests(SimpleTestCase):
    def test_runworker_does_not_know_the_settings_app(self):
        text = source(BACKEND / "apps" / "tasks" / "management" / "commands" / "runworker.py")
        self.assertNotIn("ai_settings", text)
        self.assertNotIn("load_stored_settings", text)

    def test_no_setting_or_environment_variable_was_added_for_the_stored_settings(self):
        for relative in ("config/settings/base.py", "config/env.py"):
            with self.subTest(relative):
                self.assertNotIn("AI_SETTINGS", source(BACKEND / relative).upper().replace("APPS.AI_SETTINGS", ""))
