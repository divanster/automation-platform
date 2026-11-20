"""Tests of the AI extraction settings that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and
docs/development/runbook.md). When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run
against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet;
the project owner runs them locally. When they do pass, that covers the tested scenarios only. This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_ai_settings_postgres --tag requires_postgres -v 2

No Workbench request is made: the transport is never built, and the provider status probe reads only whether a variable is set.
"""

import io
import os
import threading
import unittest
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag

from apps.ai_settings import services
from apps.ai_settings.errors import AISettingsConflictError
from apps.ai_settings.models import SETTINGS_SUBJECT_ID, AIExtractionSettings
from apps.ai_settings.rules import ExtractionValues
from apps.accounts.errors import AuthorizationError
from apps.accounts.roles import OPERATOR
from apps.audit.models import AuditEvent
from apps.cases.errors import ImmutableRecordError
from config.intake_extraction import build_catalog
from tests import test_api_postgres as base
from tests.test_intake_postgres import DedicatedTestDatabaseMixin

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

URL = "/api/ai-settings/extraction/"
CATALOG = build_catalog()
AI_BODY = {"extraction_mode": "ai", "provider_id": "kpmg_workbench", "profile_id": "workbench-gpt5", "policy_mode": "company_endpoint", "timeout_seconds": 30}


def put(browser, body, **extra):
    return browser.client.put(URL, body, format="json", HTTP_X_CSRFTOKEN=browser.token, **extra)


@tag("requires_postgres")
@requires_postgres
class SettingsApiTests(base.ApiCase):
    def test_every_non_administrator_is_refused_and_nothing_is_stored(self):
        for user in (self.operator, self.approver, self.auditor, self.nobody, self.hybrid):
            with self.subTest(user=user.username):
                self.assertEqual(self.as_(user).get(URL).status_code, 403)
                response = put(self.as_(user), {**AI_BODY, "expected_revision": 0})
                self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_an_anonymous_client_is_refused(self):
        self.assertIn(base.Browser().client.get(URL).status_code, (401, 403))

    def test_a_missing_csrf_token_is_refused(self):
        browser = self.as_(self.superuser)
        response = browser.client.put(URL, {**AI_BODY, "expected_revision": 0}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_the_default_is_deterministic_and_not_stored(self):
        data = self.as_(self.superuser).get(URL).json()
        self.assertEqual(data["settings"]["extraction_mode"], "deterministic")
        self.assertEqual((data["stored"], data["revision"], data["apply"]), (False, 0, "worker_restart_required"))

    def test_save_read_and_update_round_trip_with_revisions(self):
        browser = self.as_(self.superuser)
        first = put(browser, {**AI_BODY, "expected_revision": 0})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["revision"], 1)
        self.assertEqual(browser.get(URL).json()["settings"]["timeout_seconds"], 30)
        second = put(browser, {"extraction_mode": "deterministic", "expected_revision": 1})
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["revision"], 2)
        row = AIExtractionSettings.objects.get()
        self.assertEqual((row.extraction_mode, row.provider_id, row.timeout_seconds), ("deterministic", None, None))
        self.assertEqual(row.updated_by_public_id, self.superuser.public_id)

    def test_a_stale_revision_is_a_conflict_and_changes_nothing(self):
        browser = self.as_(self.superuser)
        self.assertEqual(put(browser, {**AI_BODY, "expected_revision": 0}).status_code, 200)
        response = put(browser, {"extraction_mode": "deterministic", "expected_revision": 0})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(AIExtractionSettings.objects.get().extraction_mode, "ai")

    def test_invalid_values_are_refused_and_nothing_is_stored(self):
        browser = self.as_(self.superuser)
        for patch in ({"provider_id": "other"}, {"profile_id": "workbench-gpt41"}, {"policy_mode": "local_only"}, {"timeout_seconds": 4}, {"timeout_seconds": 100}, {"api_key": "x"}):
            with self.subTest(patch=patch):
                self.assertEqual(put(browser, {**AI_BODY, **patch, "expected_revision": 0}).status_code, 400)
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_no_response_holds_a_secret(self):
        sentinel = "SECRET-SENTINEL-DB-TEST"
        with mock.patch.dict(os.environ, {"AP_AI_WORKBENCH_API_KEY": sentinel}):
            browser = self.as_(self.superuser)
            bodies = [browser.get(URL).content, put(browser, {**AI_BODY, "expected_revision": 0}).content, put(browser, {**AI_BODY, "api_key": sentinel, "expected_revision": 1}).content]
        for body in bodies:
            self.assertNotIn(sentinel.encode(), body)

    def test_saving_does_not_touch_a_worker_or_a_task(self):
        before = connection.introspection.table_names()
        put(self.as_(self.superuser), {**AI_BODY, "expected_revision": 0})
        self.assertEqual(connection.introspection.table_names(), before)


@tag("requires_postgres")
@requires_postgres
class ConstraintTests(DedicatedTestDatabaseMixin, TestCase):
    def insert(self, **fields):
        with transaction.atomic():
            return AIExtractionSettings.objects.create(**fields)

    def test_the_table_holds_at_most_one_row(self):
        self.insert(id=1)
        with self.assertRaises(IntegrityError):
            self.insert(id=2)
        with self.assertRaises(IntegrityError):
            self.insert(id=1)

    def test_all_or_nothing_is_enforced_by_the_database(self):
        for fields in (
            {"extraction_mode": "deterministic", "provider_id": "p"},
            {"extraction_mode": "ai"},
            {"extraction_mode": "ai", "provider_id": "p", "profile_id": "m", "policy_mode": "company_endpoint"},
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError):
                self.insert(id=1, **fields)

    def test_an_unknown_mode_and_a_zero_revision_are_refused(self):
        with self.assertRaises(IntegrityError):
            self.insert(id=1, extraction_mode="auto")
        with self.assertRaises(IntegrityError):
            self.insert(id=1, revision=0)

    def test_a_deterministic_row_and_a_complete_ai_row_are_accepted(self):
        self.insert(id=1)
        AIExtractionSettings.objects.all().delete()
        self.insert(id=1, extraction_mode="ai", provider_id="kpmg_workbench", profile_id="workbench-gpt5", policy_mode="company_endpoint", timeout_seconds=30)


@tag("requires_postgres")
@requires_postgres
class LoaderTests(DedicatedTestDatabaseMixin, TestCase):
    def test_no_row_means_none_and_nothing_is_created(self):
        self.assertIsNone(services.load_stored_settings())
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_the_stored_row_is_returned_with_its_revision(self):
        AIExtractionSettings.objects.create(id=1, revision=5, extraction_mode="ai", provider_id="kpmg_workbench", profile_id="workbench-gpt5", policy_mode="approved_cloud", timeout_seconds=20)
        stored = services.load_stored_settings()
        self.assertEqual(stored.revision, 5)
        self.assertEqual(stored.values, ExtractionValues("ai", "kpmg_workbench", "workbench-gpt5", "approved_cloud", 20))


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Two administrators saving from the same revision: exactly one wins, the other gets a conflict."""

    def setUp(self):
        cache.clear()
        call_command("setup_roles", stdout=io.StringIO())
        self.admins = [base.make_user("root-a", superuser=True), base.make_user("root-b", superuser=True)]

    def test_two_concurrent_first_saves_produce_one_row_and_one_conflict(self):
        results = []
        barrier = threading.Barrier(2)

        def save(user):
            from django.db import connection as thread_connection

            try:
                barrier.wait(timeout=10)
                services.update_extraction_settings(user=user, data={"extraction_mode": "deterministic"}, expected_revision=0, catalog=CATALOG, lease_seconds=60)
                results.append("saved")
            except AISettingsConflictError:
                results.append("conflict")
            except Exception as error:  # reported by the assertion below
                results.append(type(error).__name__)
            finally:
                thread_connection.close()

        threads = [threading.Thread(target=save, args=(user,)) for user in self.admins]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(results), ["conflict", "saved"])
        self.assertEqual(AIExtractionSettings.objects.get().revision, 1)

    def test_a_non_administrator_cannot_save_through_the_service(self):
        operator = base.make_user("olive", OPERATOR)
        with self.assertRaises(AuthorizationError):
            services.update_extraction_settings(user=operator, data={"extraction_mode": "deterministic"}, expected_revision=0, catalog=CATALOG, lease_seconds=60)
        self.assertFalse(AIExtractionSettings.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(subject_type="ai_settings").exists())


def settings_events():
    return list(AuditEvent.objects.filter(subject_type="ai_settings").order_by("id"))


@tag("requires_postgres")
@requires_postgres
class SettingsAuditApiTests(base.ApiCase):
    """The audit event for a successful change, through the real stack and a real database."""

    def test_a_successful_save_writes_one_event_with_the_contract_fields(self):
        browser = self.as_(self.superuser)
        self.assertEqual(put(browser, {**AI_BODY, "expected_revision": 0}).status_code, 200)
        (event,) = settings_events()
        self.assertEqual((event.actor_kind, event.actor_public_id, event.action, event.subject_id), ("user", self.superuser.public_id, "ai_settings.updated", SETTINGS_SUBJECT_ID))
        self.assertEqual((event.case_id, event.run_id), (None, None))
        self.assertIsNotNone(event.occurred_at)
        self.assertEqual(
            event.details,
            {
                "revision": 1, "extraction_mode": "ai", "provider_id": "kpmg_workbench", "profile_id": "workbench-gpt5", "policy_mode": "company_endpoint", "timeout_seconds": 30,
                "connection_id": None, "connection_count": 0, "connections_changed": False,
            },
        )  # fmt: skip

    def test_each_save_adds_one_event_and_the_event_revision_matches_the_row(self):
        browser = self.as_(self.superuser)
        put(browser, {**AI_BODY, "expected_revision": 0})
        put(browser, {"extraction_mode": "deterministic", "expected_revision": 1})
        events = settings_events()
        self.assertEqual([e.details["revision"] for e in events], [1, 2])
        self.assertEqual(events[-1].details["revision"], AIExtractionSettings.objects.get().revision)

    def test_a_refusal_a_validation_error_and_a_stale_revision_write_no_event(self):
        browser = self.as_(self.superuser)
        for user in (self.operator, self.approver, self.auditor, self.nobody, self.hybrid):
            self.assertEqual(put(self.as_(user), {**AI_BODY, "expected_revision": 0}).status_code, 403)
        self.assertEqual(put(browser, {**AI_BODY, "policy_mode": "local_only", "expected_revision": 0}).status_code, 400)
        self.assertEqual(settings_events(), [])
        self.assertEqual(put(browser, {**AI_BODY, "expected_revision": 0}).status_code, 200)
        self.assertEqual(put(browser, {"extraction_mode": "deterministic", "expected_revision": 0}).status_code, 409)
        self.assertEqual(len(settings_events()), 1)

    def test_an_audit_write_that_fails_rolls_the_settings_change_back(self):
        browser = self.as_(self.superuser)
        self.assertEqual(put(browser, {**AI_BODY, "expected_revision": 0}).status_code, 200)
        browser.client.raise_request_exception = False
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")), self.assertLogs("apps.api.errors", "ERROR"):
            response = put(browser, {"extraction_mode": "deterministic", "expected_revision": 1})
        self.assertEqual(response.status_code, 500)
        row = AIExtractionSettings.objects.get()
        self.assertEqual((row.revision, row.extraction_mode), (1, "ai"))
        self.assertEqual(len(settings_events()), 1)

    def test_a_failed_first_save_leaves_neither_a_row_nor_an_event(self):
        browser = self.as_(self.superuser)
        browser.client.raise_request_exception = False
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")), self.assertLogs("apps.api.errors", "ERROR"):
            self.assertEqual(put(browser, {**AI_BODY, "expected_revision": 0}).status_code, 500)
        self.assertFalse(AIExtractionSettings.objects.exists())
        self.assertEqual(settings_events(), [])

    def test_no_secret_reaches_the_stored_event(self):
        env = {"AP_AI_WORKBENCH_API_KEY": "KEY-SENTINEL-AUDIT-DB", "AP_AI_WORKBENCH_PROXY_URL": "PROXY-SENTINEL-AUDIT-DB", "AP_AI_WORKBENCH_CHARGE_CODE": "CHARGE-SENTINEL-AUDIT-DB"}
        with mock.patch.dict(os.environ, env):
            browser = self.as_(self.superuser)
            put(browser, {**AI_BODY, "api_key": env["AP_AI_WORKBENCH_API_KEY"], "expected_revision": 0})
            put(browser, {**AI_BODY, "expected_revision": 0})
        stored = repr([(e.action, e.subject_type, e.details) for e in AuditEvent.objects.all()])
        for value in env.values():
            self.assertNotIn(value, stored)

    def test_the_stored_event_cannot_be_updated_or_deleted_through_the_orm(self):
        put(self.as_(self.superuser), {**AI_BODY, "expected_revision": 0})
        event = settings_events()[0]
        event.details = {"revision": 99}
        with self.assertRaises(ImmutableRecordError):
            event.save()
        with self.assertRaises(ImmutableRecordError):
            event.delete()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(pk=event.pk).update(action="x.y")
        self.assertEqual(settings_events()[0].details["revision"], 1)


@tag("requires_postgres")
@requires_postgres
class SettingsAuditSchemaTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """audit 0003: the database accepts the ``ai_settings`` subject type, and a reverse past a stored event is refused. Returns to the latest migrations afterwards."""

    BEFORE = ("audit", "0002_extensible_audit_vocabulary")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return MigrationExecutor(connection)

    def setUp(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def constraint(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = 'audit_event_subject_type_valid'", [AuditEvent._meta.db_table])
            return cursor.fetchone()[0]

    def new_event(self, subject_type="ai_settings"):
        return AuditEvent.objects.create(
            actor_kind="user", actor_public_id=uuid.uuid4(), action="ai_settings.updated", subject_type=subject_type, subject_id=SETTINGS_SUBJECT_ID, details={"revision": 1}
        )

    def test_the_latest_schema_accepts_the_subject_type_and_still_refuses_unknown_ones(self):
        self.assertIn("'ai_settings'", self.constraint())
        self.new_event()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.new_event("ai_setting")

    def test_before_0003_the_subject_type_is_refused_and_after_it_accepted(self):
        self.migrate(self.BEFORE)
        self.assertNotIn("'ai_settings'", self.constraint())
        self.migrate(("audit", "0003_ai_settings_subject_type"))
        self.assertIn("'ai_settings'", self.constraint())

    def test_reverse_without_an_ai_settings_event_succeeds_and_keeps_every_row(self):
        self.new_event("run")
        before = list(AuditEvent.objects.order_by("id").values_list("id", "action", "subject_type", "details"))
        self.migrate(self.BEFORE)
        self.assertEqual(list(AuditEvent.objects.order_by("id").values_list("id", "action", "subject_type", "details")), before)

    def test_reverse_is_refused_when_an_event_exists_and_nothing_is_deleted(self):
        self.new_event()
        with self.assertRaises(IrreversibleError):
            self.migrate(self.BEFORE)
        self.assertEqual(AuditEvent.objects.filter(subject_type="ai_settings").count(), 1)
        self.assertIn("'ai_settings'", self.constraint())
