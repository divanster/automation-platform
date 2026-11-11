"""Pure-domain STATIC boundary tests (standard-library unittest, no Django).

What this file proves, and what it does not:

* It reads the package's own source with ``ast`` and checks which modules are imported and
  which dangerous built-ins are named. That shows what the code *can* reach by import.
* It does NOT prove runtime behavior: a static scan cannot see calls made through objects
  that were passed in, and it says nothing about what actually executes. Runtime checks
  are in test_request_intake_runtime_boundaries.py, and they have limits of their own,
  stated there.
* It also checks that the pure test modules themselves do not import Django, so they can
  run under ``python -m unittest`` without Django being set up.
"""

import ast
import unittest
from pathlib import Path

import workflows
from workflows import request_intake

# Standard-library modules the package may import. Anything else (django, rest_framework,
# psycopg, os, sys, pathlib, io, socket, urllib, http, requests, sqlite3, subprocess,
# importlib, anthropic, openai, ...) fails the test and needs a deliberate decision.
# hashlib and json were added deliberately for canonical serialization and hashing (canonical.py).
ALLOWED_IMPORTS = {"__future__", "collections", "dataclasses", "decimal", "enum", "hashlib", "json", "math", "re", "typing"}

# Built-ins that read files, run code, or import dynamically.
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals", "vars", "getattr", "setattr", "delattr"}

PACKAGE_DIR = Path(request_intake.__file__).parent
TESTS_DIR = Path(__file__).parent

PURE_TEST_MODULES = [
    "test_request_intake_validation.py",
    "test_request_intake_quantity.py",
    "test_request_intake_canonical.py",
    "test_request_intake_extraction.py",
    "test_request_intake_contract.py",
    "test_request_intake_isolation.py",
    "test_request_intake_boundaries.py",
    "test_request_intake_runtime_boundaries.py",
    "intake_support.py",
]


def imported_modules(path):
    """Top-level module names imported by ``path``; relative imports count as local."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add((node.module or "").split(".")[0])
    return found


def named_builtins(path, names):
    """Names from ``names`` that appear as plain identifiers (calls or references) in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in names}


class ImportBoundaryTests(unittest.TestCase):
    def test_the_package_has_the_expected_source_files(self):
        names = {p.name for p in PACKAGE_DIR.glob("*.py")}
        self.assertEqual(
            names,
            {"__init__.py", "schema.py", "findings.py", "paths.py", "quantity.py", "validation.py", "extraction.py", "canonical.py"},
        )

    def test_only_allowed_standard_library_modules_are_imported(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                unexpected = imported_modules(path) - ALLOWED_IMPORTS
                self.assertEqual(unexpected, set(), f"{path.name} imports {sorted(unexpected)}")

    def test_no_file_execution_or_dynamic_import_builtins_are_named(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(named_builtins(path, FORBIDDEN_NAMES), set())

    def test_the_parent_package_imports_nothing(self):
        self.assertEqual(imported_modules(Path(workflows.__file__)), set())

    def test_the_checkers_detect_forbidden_code(self):
        # Guard against a silently broken checker.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe.py"
            probe.write_text("import django.db\nfrom os import path\nimport re\nopen('x')\neval('1')\n", encoding="utf-8")
            self.assertEqual(imported_modules(probe) - ALLOWED_IMPORTS, {"django", "os"})
            self.assertEqual(named_builtins(probe, FORBIDDEN_NAMES), {"open", "eval"})


class NotADjangoAppTests(unittest.TestCase):
    def test_the_packages_have_no_django_app_files(self):
        for directory in (PACKAGE_DIR, Path(workflows.__file__).parent):
            for forbidden in ("models.py", "migrations", "apps.py", "admin.py", "urls.py", "views.py", "management"):
                self.assertFalse((directory / forbidden).exists(), f"{directory.name}/{forbidden}")

    def test_the_public_api_is_exported(self):
        for name in (
            "StructuredRequest",
            "validate_request",
            "DeterministicTestExtractor",
            "FindingCode",
            "ValidationLimits",
            "to_decimal",
            "FINDINGS_CONTRACT_VERSION",
        ):
            self.assertIn(name, request_intake.__all__)
            self.assertTrue(hasattr(request_intake, name))


class PureTestsDoNotUseDjangoTests(unittest.TestCase):
    def test_the_pure_test_modules_exist(self):
        for name in PURE_TEST_MODULES:
            self.assertTrue((TESTS_DIR / name).exists(), name)

    def test_the_pure_test_modules_do_not_import_django_or_the_project_apps(self):
        for name in PURE_TEST_MODULES:
            with self.subTest(module=name):
                imports = imported_modules(TESTS_DIR / name)
                self.assertFalse(imports & {"django", "rest_framework", "psycopg", "apps", "config"}, sorted(imports))


if __name__ == "__main__":
    unittest.main()
