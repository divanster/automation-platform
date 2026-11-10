"""Tests that REQUIRE a real PostgreSQL server.

These are excluded from the default run and are skipped unless
``AP_RUN_DB_TESTS=1`` is set explicitly. When they run, Django creates and later
destroys a separate ``test_<AP_DB_NAME>`` database; the role in ``AP_DB_USER``
therefore needs CREATEDB. Never point them at a shared or unknown server.

Run (see docs/development/runbook.md):
    python manage.py test --tag requires_postgres
"""

import os
import secrets
import unittest

from django.contrib.auth import get_user_model
from django.test import TestCase, tag

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)


@tag("requires_postgres")
@requires_postgres
class PostgresUserTests(TestCase):
    def test_user_can_be_created_and_authenticates(self):
        user_model = get_user_model()
        throwaway = secrets.token_urlsafe(16)  # generated per run, never stored
        user = user_model.objects.create_user(username="skeleton-test", password=throwaway)
        self.assertTrue(user.check_password(throwaway))
        self.assertFalse(user.check_password(throwaway + "x"))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)

    def test_username_is_unique(self):
        from django.db import IntegrityError, transaction

        user_model = get_user_model()
        user_model.objects.create_user(username="duplicate-check")
        with self.assertRaises(IntegrityError), transaction.atomic():
            user_model.objects.create_user(username="duplicate-check")


@tag("requires_postgres")
@requires_postgres
class PostgresReadinessTests(TestCase):
    def test_readiness_reports_ok_against_a_real_database(self):
        response = self.client.get("/api/health/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
