"""Static boundary tests of the managed AI credential feature (AI-SEC-2): no database, no network, no secret store.

They pin the structure that keeps it small and safe: the rules and errors are pure, the services never read the environment or log a value, the views are thin adapters, only the composition
roots and the credential service touch the secret store, the audit vocabulary holds no value, and no response field can hold a secret.
"""

import ast
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "apps" / "ai_credentials"
VIEWS = BACKEND / "apps" / "api" / "views_ai_credentials.py"


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


class PackageBoundaryTests(SimpleTestCase):
    def test_the_package_is_not_an_installed_app_and_has_no_model_or_migration(self):
        self.assertEqual({p.name for p in APP.iterdir()} - {"__pycache__"}, {"__init__.py", "errors.py", "rules.py", "services.py"})
        self.assertNotIn("apps.ai_credentials", settings.INSTALLED_APPS)

    def test_the_rules_and_errors_are_pure_standard_library(self):
        for name in ("rules.py", "errors.py"):
            for imported in imports_of(APP / name):
                with self.subTest(file=name, imported=imported):
                    self.assertIn(imported, {"re", "uuid"})

    def test_the_services_import_no_composition_no_api_no_gateway_no_native_or_network_capability(self):
        for imported in imports_of(APP / "services.py"):
            with self.subTest(imported):
                self.assertFalse(imported.startswith(("config", "apps.api", "apps.ai_gateway", "rest_framework")), imported)
                self.assertNotIn(imported.split(".")[0], {"os", "ctypes", "hashlib", "socket", "subprocess", "urllib", "http", "requests", "winreg", "pickle"})

    def test_the_services_read_no_environment_print_nothing_and_log_only_through_one_logger(self):
        text = source(APP / "services.py")
        for needle in ("os.environ", "getenv", "print(", "environ["):
            self.assertNotIn(needle, text)
        self.assertEqual(len(re.findall(r"logging\.getLogger", text)), 1)
        for call in re.findall(r"logger\.\w+\((.*)", text):
            self.assertNotIn("value", call.lower().replace("values", ""))
            self.assertNotIn("reference", call.replace("subject", ""))

    def test_no_function_of_the_services_returns_a_secret_value(self):
        text = source(APP / "services.py")
        for needle in ("reveal(", ".get(reference)\n        return", "return secret", "return value"):
            self.assertNotIn(needle, text)

    def test_the_only_secret_store_calls_are_the_four_operations(self):
        used = set(re.findall(r"store\.(\w+)\(", source(APP / "services.py")))
        self.assertLessEqual(used, {"get", "put", "delete"})


class ViewBoundaryTests(SimpleTestCase):
    def test_the_view_has_no_database_access_no_logging_no_environment_access_and_no_secret_store_import(self):
        text = source(VIEWS)
        for needle in ("objects.", "transaction", "import logging", "getLogger", "logger.", "os.environ", "getenv", "settings.", "secret_store", "hashlib"):
            with self.subTest(needle):
                self.assertNotIn(needle, text)

    def test_each_view_method_calls_exactly_one_service(self):
        from apps.api import views_ai_credentials as module

        expected = {("AICredentialCollectionView", "post"): "create_credential", ("AICredentialDetailView", "get"): "get_status", ("AICredentialDetailView", "put"): "replace_credential", ("AICredentialDetailView", "delete"): "delete_credential"}
        tree = ast.parse(source(VIEWS))
        found = {}
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for function in (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("get", "post", "put", "delete")):
                found[(cls.name, function.name)] = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "services"})
        self.assertEqual(found, {key: [name] for key, name in expected.items()})
        self.assertTrue(hasattr(module, "AICredentialDetailView"))

    def test_the_actor_is_always_the_authenticated_user_and_never_read_from_the_body(self):
        tree = ast.parse(source(VIEWS))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "services"):
            user = next(k for k in call.keywords if k.arg == "user")
            self.assertEqual(ast.unparse(user.value), "request.user")
        for token in (r"\bactor\b", "public_id", "user_id"):
            self.assertIsNone(re.search(token, source(VIEWS)), token)

    def test_every_view_that_takes_a_value_hides_it_from_error_reports(self):
        text = source(VIEWS)
        self.assertEqual(text.count('@method_decorator(sensitive_post_parameters("value"), name="dispatch")'), 2)

    def test_the_views_keep_the_project_authentication_and_permission_defaults(self):
        from apps.api import base, views_ai_credentials

        for cls in (views_ai_credentials.AICredentialCollectionView, views_ai_credentials.AICredentialDetailView):
            self.assertTrue(issubclass(cls, base.ApiView))
            self.assertNotIn("authentication_classes", cls.__dict__)
            self.assertNotIn("permission_classes", cls.__dict__)


class NoSecretFieldTests(SimpleTestCase):
    def test_the_response_has_fixed_fields_and_none_can_hold_the_secret(self):
        from apps.api.serializers import AICredentialStatusOutput

        names = set(AICredentialStatusOutput().fields)
        self.assertEqual(names, {"reference", "source", "status", "store", "connections", "apply"})
        for name in names:
            self.assertIsNone(re.search(r"secret|value|password|token|key", name, re.IGNORECASE), name)

    def test_the_input_serializers_accept_exactly_a_reference_and_a_value(self):
        from apps.api.serializers import AICredentialCreateInput, AICredentialValueInput

        self.assertEqual(set(AICredentialCreateInput().fields), {"reference", "value"})
        self.assertEqual(set(AICredentialValueInput().fields), {"value"})
        self.assertFalse(AICredentialCreateInput().fields["value"].trim_whitespace)

    def test_the_audit_details_of_the_credential_actions_hold_only_the_store_identifier(self):
        from apps.audit import events

        for action in (events.ACTION_AI_CREDENTIAL_CREATED, events.ACTION_AI_CREDENTIAL_REPLACED, events.ACTION_AI_CREDENTIAL_DELETED):
            self.assertEqual(events.DETAIL_KEYS[action], frozenset({"store"}))
            self.assertLessEqual(len(action), 32)  # the database column

    def test_the_credential_actions_are_core_actions_no_module_can_claim(self):
        from apps.audit.events import CORE_VOCABULARY

        self.assertTrue(CORE_VOCABULARY.allows_action("ai_credential.created"))
        self.assertIn("ai_credential", {a.split(".")[0] for a in CORE_VOCABULARY.actions})

    def test_no_django_setting_holds_a_managed_credential_or_the_store_location(self):
        names = [n for n in dir(settings) if n.isupper()]
        self.assertEqual([n for n in names if re.search(r"SECRET_STORE|CREDENTIAL|DPAPI", n)], [])

    def test_the_new_variables_are_documented_with_no_value_in_the_example_file(self):
        text = (BACKEND.parent / ".env.example").read_text(encoding="utf-8")
        for name in ("AP_SECRET_STORE_BACKEND", "AP_SECRET_STORE_DIRECTORY", "AP_SECRET_STORE_TIMEOUT_SECONDS", "AP_SECRET_STORE_EXPECTED_IDENTITY_SID", "AP_SECRET_STORE_APPROVED_IDENTITY_SIDS"):
            with self.subTest(name):
                lines = [line for line in text.splitlines() if name in line]
                self.assertTrue(lines, name)
                for line in lines:
                    self.assertTrue(line.lstrip().startswith("#") and line.strip().endswith("="), line)  # commented out and empty: nothing switches a store on or names a path
