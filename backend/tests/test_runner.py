"""DB-free tests for the project test runner's safety default."""

import os
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from config.test_runner import DB_TAG, ProjectTestRunner


class ProjectTestRunnerTests(SimpleTestCase):
    def test_runner_is_configured(self):
        self.assertEqual(settings.TEST_RUNNER, "config.test_runner.ProjectTestRunner")

    def test_postgres_tests_excluded_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AP_RUN_DB_TESTS", None)
            self.assertIn(DB_TAG, ProjectTestRunner().exclude_tags)

    def test_postgres_tests_excluded_unless_exactly_enabled(self):
        for value in ("0", "true", "yes", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"AP_RUN_DB_TESTS": value}):
                self.assertIn(DB_TAG, ProjectTestRunner().exclude_tags)

    def test_postgres_tests_included_when_explicitly_enabled(self):
        with mock.patch.dict(os.environ, {"AP_RUN_DB_TESTS": "1"}):
            self.assertNotIn(DB_TAG, ProjectTestRunner().exclude_tags)


class AIEnvironmentIsolationTests(SimpleTestCase):
    """A developer's ``.env`` may hold the Workbench key or enable ai mode; no test may inherit that (AI-2a)."""

    NAMES = ("AP_INTAKE_EXTRACTION_MODE", "AP_AI_POLICY_MODE", "AP_INTAKE_AI_TIMEOUT_SECONDS", "AP_AI_WORKBENCH_API_KEY", "AP_AI_WORKBENCH_ENDPOINT", "AP_AI_ANYTHING")

    def test_the_runner_removes_every_ai_and_extraction_variable_from_the_process(self):
        with mock.patch.dict(os.environ, {name: "x" for name in self.NAMES}):
            ProjectTestRunner()
            self.assertEqual([name for name in self.NAMES if name in os.environ], [])

    def test_other_variables_are_left_alone(self):
        keep = {"AP_RUN_DB_TESTS": "1", "AP_DB_NAME": "kept", "AP_WORKER_LEASE_SECONDS": "60", "AP_INTAKEX": "kept", "UNRELATED": "kept"}
        with mock.patch.dict(os.environ, {**keep, "AP_AI_WORKBENCH_API_KEY": "x"}):
            ProjectTestRunner()
            self.assertEqual({name: os.environ.get(name) for name in keep}, keep)

    def test_the_prefixes_are_exactly_the_two_ai_families(self):
        from config.test_runner import AI_ENVIRONMENT_PREFIXES

        self.assertEqual(AI_ENVIRONMENT_PREFIXES, ("AP_INTAKE_", "AP_AI_", "AP_SECRET_STORE_", "AP_DOCUMENT_"))  # DOC-1 added the document storage family
