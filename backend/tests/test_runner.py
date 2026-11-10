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
