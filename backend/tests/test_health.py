"""DB-free tests for the health endpoints.

Readiness tests use a MOCKED connection. They check the view's behavior only;
they are not a PostgreSQL connectivity test. A real connectivity check lives in
tests/test_postgres_required.py and needs a database.
"""

from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase
from rest_framework.permissions import AllowAny

from apps.health import views

SENSITIVE_TEXT = 'connection to server at "db.internal.invalid" failed: password authentication failed for user "svc_account" secret-pw-123'


class LivenessTests(SimpleTestCase):
    url = "/api/health/live/"

    def test_returns_minimal_ok(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Content-Type"], "application/json")

    def test_does_not_access_the_database(self):
        with mock.patch("apps.health.views.connections") as connections:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(connections.mock_calls, [])
        # SimpleTestCase also forbids real queries, so any leak would error.

    def test_is_explicitly_public(self):
        self.assertEqual(views.LivenessView.authentication_classes, [])
        self.assertEqual(views.LivenessView.permission_classes, [AllowAny])

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_discloses_no_configuration(self):
        body = self.client.get(self.url).content.decode()
        for forbidden in ("version", "path", "debug", "database", "settings"):
            self.assertNotIn(forbidden, body.lower())


class ReadinessTests(SimpleTestCase):
    url = "/api/health/ready/"

    @staticmethod
    def _cursor(connections):
        return connections.__getitem__.return_value.cursor.return_value.__enter__.return_value

    @mock.patch("apps.health.views.connections")
    def test_ready_when_query_succeeds(self, connections):
        cursor = self._cursor(connections)
        cursor.fetchone.return_value = (1,)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        cursor.execute.assert_called_once_with("SELECT 1")

    @mock.patch("apps.health.views.connections")
    def test_unavailable_when_connection_fails(self, connections):
        connections.__getitem__.return_value.cursor.side_effect = OperationalError(SENSITIVE_TEXT)
        with self.assertLogs("apps.health.views", level="WARNING"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    @mock.patch("apps.health.views.connections")
    def test_unavailable_when_query_fails(self, connections):
        self._cursor(connections).execute.side_effect = OperationalError(SENSITIVE_TEXT)
        with self.assertLogs("apps.health.views", level="WARNING"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)

    @mock.patch("apps.health.views.connections")
    def test_error_response_hides_sensitive_details(self, connections):
        connections.__getitem__.return_value.cursor.side_effect = OperationalError(SENSITIVE_TEXT)
        with self.assertLogs("apps.health.views", level="WARNING"):
            body = self.client.get(self.url).content.decode()
        for forbidden in ("db.internal", "svc_account", "secret-pw", "OperationalError", "Traceback", "password"):
            self.assertNotIn(forbidden, body)

    @mock.patch("apps.health.views.connections")
    def test_log_records_only_the_exception_type(self, connections):
        connections.__getitem__.return_value.cursor.side_effect = OperationalError(SENSITIVE_TEXT)
        with self.assertLogs("apps.health.views", level="WARNING") as logs:
            self.client.get(self.url)
        output = "\n".join(logs.output)
        self.assertIn("OperationalError", output)
        self.assertNotIn("secret-pw", output)
        self.assertNotIn("db.internal", output)

    @mock.patch("apps.health.views.connections")
    def test_unexpected_exception_types_are_also_generic(self, connections):
        connections.__getitem__.return_value.cursor.side_effect = RuntimeError(SENSITIVE_TEXT)
        with self.assertLogs("apps.health.views", level="WARNING"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-pw", response.content.decode())

    def test_is_explicitly_public(self):
        self.assertEqual(views.ReadinessView.authentication_classes, [])
        self.assertEqual(views.ReadinessView.permission_classes, [AllowAny])
