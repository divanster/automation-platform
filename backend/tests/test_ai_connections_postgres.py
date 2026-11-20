"""Tests of AI-SET-2 (named AI connections) that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md). When they run, Django
creates and later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet; the project owner runs them
locally. When they do pass, that covers the tested scenarios only. This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_ai_connections_postgres --tag requires_postgres -v 2

No AI request is made: every transport is a scripted fake, no endpoint is resolved or connected, and every credential is a sentinel.
"""

import io
import json
import os
import threading
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag

from apps.ai_settings import services
from apps.ai_settings.errors import AISettingsConflictError
from apps.ai_settings.models import SETTINGS_SUBJECT_ID, AIExtractionSettings, connection_subject_id
from apps.audit.models import AuditEvent
from apps.cases.models import ResultRevision, WorkflowRun
from apps.intake import handlers as intake_handlers
from apps.intake.models import ExtractionOutcome
from apps.tasks import worker
from config import task_registry
from config.intake_extraction import build_catalog
from tests import test_api_postgres as base
from tests.ai_connections_support import OPERATOR_ENV, SECRET_A, SECRET_B, SECRET_WORKBENCH, all_connections, corp_connection, ollama_connection
from tests.intake_ai_support import VALID_OUTPUT, FakeTransport, answer_with
from tests.test_intake_ai_postgres import AIWorkerCase
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, requires_postgres, run_threads, submit

URL = "/api/ai-settings/extraction/"
CATALOG = build_catalog()
SELECT_CORP = {"extraction_mode": "ai", "connection_id": "corp-llm", "profile_id": "corp-small", "policy_mode": "company_endpoint", "timeout_seconds": 30}
LEGACY = {"extraction_mode": "ai", "provider_id": "kpmg_workbench", "profile_id": "workbench-gpt5", "policy_mode": "company_endpoint", "timeout_seconds": 30}
SENTINELS = (SECRET_A, SECRET_B, SECRET_WORKBENCH)


def put(browser, body):
    return browser.client.put(URL, body, format="json", HTTP_X_CSRFTOKEN=browser.token)


def body(connections=None, expected=0, **selection):
    document = {**(selection or SELECT_CORP), "expected_revision": expected}
    if connections is not None:
        document["connections"] = connections
    return document


def settings_events(action=None):
    events = AuditEvent.objects.filter(subject_type="ai_settings").order_by("id")
    return list(events if action is None else events.filter(action=action))


# --- persistence through the API -------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConnectionPersistenceTests(base.ApiCase):
    def test_several_connections_and_the_selection_are_stored_in_one_row_and_read_back(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            response = put(browser, body(all_connections()))
            self.assertEqual(response.status_code, 200, response.content)
            row = AIExtractionSettings.objects.get()
            self.assertEqual((row.revision, row.connection_id, row.provider_id, row.profile_id), (1, "corp-llm", "openai_compatible", "corp-small"))
            self.assertEqual(row.connections, all_connections())
            read = browser.get(URL).json()
            self.assertEqual([c["connection_id"] for c in read["connections"]], ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"])
            self.assertEqual(read["settings"]["connection_id"], "corp-llm")
            self.assertEqual(services.load_stored_settings().revision, 1)
            self.assertEqual([c.connection_id for c in services.load_stored_settings().connections], [c["connection_id"] for c in all_connections()])

    def test_a_save_without_connections_keeps_them_and_a_replacement_replaces_them_as_a_whole(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            put(browser, body(all_connections()))
            self.assertEqual(put(browser, body(expected=1, **{**SELECT_CORP, "profile_id": "corp-large"})).status_code, 200)
            self.assertEqual(AIExtractionSettings.objects.get().connections, all_connections())
            self.assertEqual(put(browser, body([ollama_connection()], expected=2, extraction_mode="deterministic")).status_code, 200)
            row = AIExtractionSettings.objects.get()
            self.assertEqual((row.revision, row.connection_id, row.connections), (3, None, [ollama_connection()]))

    def test_invalid_connections_store_nothing_and_a_credential_is_never_stored_or_echoed(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            for bad in ([{**corp_connection(), "api_key": SECRET_A}], [corp_connection(provider_type="plugin")], [corp_connection(credential_ref="AP_SECRET_KEY")]):
                response = put(browser, body(bad))
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(SECRET_A.encode(), response.content)
            self.assertFalse(AIExtractionSettings.objects.exists())
            self.assertEqual(settings_events(), [])

    def test_every_non_administrator_is_refused(self):
        for user in (self.operator, self.approver, self.auditor, self.nobody, self.hybrid):
            with self.subTest(user=user.username):
                self.assertEqual(self.as_(user).get(URL).status_code, 403)
                self.assertEqual(put(self.as_(user), body(all_connections())).status_code, 403)
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_no_secret_is_in_the_row_the_responses_or_the_audit_history(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            responses = [put(browser, body(all_connections())).content, browser.get(URL).content]
        stored = repr(list(AIExtractionSettings.objects.values())) + repr(list(AuditEvent.objects.values()))
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, stored)
            for content in responses:
                self.assertNotIn(sentinel.encode(), content)


# --- atomic audit ----------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConnectionAuditTests(base.ApiCase):
    def test_one_settings_event_and_one_event_per_connection_commit_with_the_row(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            self.assertEqual(put(self.as_(self.superuser), body([corp_connection(), ollama_connection()])).status_code, 200)
        (updated,) = settings_events("ai_settings.updated")
        self.assertEqual((updated.details["connection_id"], updated.details["connection_count"], updated.details["connections_changed"]), ("corp-llm", 2, True))
        created = settings_events("ai_settings.connection_changed")
        self.assertEqual([(e.details["connection_id"], e.details["change"]) for e in created], [("corp-llm", "created"), ("ollama-local", "created")])
        self.assertEqual([e.subject_id for e in created], [connection_subject_id("corp-llm"), connection_subject_id("ollama-local")])
        self.assertEqual(updated.subject_id, SETTINGS_SUBJECT_ID)
        self.assertEqual(AIExtractionSettings.objects.get().revision, updated.details["revision"])

    def test_a_change_and_a_removal_are_recorded(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            put(browser, body([corp_connection(), ollama_connection()]))
            put(browser, body([corp_connection(enabled=False)], expected=1, extraction_mode="deterministic"))
        changes = [(e.details["connection_id"], e.details["change"]) for e in settings_events("ai_settings.connection_changed")]
        self.assertEqual(changes, [("corp-llm", "created"), ("ollama-local", "created"), ("corp-llm", "updated"), ("ollama-local", "removed")])

    def test_no_event_holds_an_endpoint_a_reference_a_name_or_a_secret(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            put(self.as_(self.superuser), body(all_connections()))
        text = repr([e.details for e in settings_events()])
        for forbidden in ("llm.corp", "AP_AI_CRED", "Company model server", *SENTINELS):
            self.assertNotIn(forbidden, text)

    def test_an_audit_failure_rolls_back_the_row_the_connections_and_the_first_event(self):
        browser = self.as_(self.superuser)
        browser.client.raise_request_exception = False
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            self.assertEqual(put(browser, body([corp_connection()])).status_code, 200)
            before = AIExtractionSettings.objects.get()
            calls = []
            real = services.record_event

            def second_fails(event):
                calls.append(event.action)
                if len(calls) == 2:
                    raise RuntimeError("audit down")
                real(event)

            with mock.patch.object(services, "record_event", second_fails), self.assertLogs("apps.api.errors", "ERROR"):
                response = put(browser, body([corp_connection(), ollama_connection()], expected=1, extraction_mode="deterministic"))
        self.assertEqual(response.status_code, 500)
        row = AIExtractionSettings.objects.get()
        self.assertEqual((row.revision, row.connections, row.connection_id), (before.revision, [corp_connection()], "corp-llm"))
        self.assertEqual(len(settings_events("ai_settings.updated")), 1)
        self.assertEqual(len(settings_events("ai_settings.connection_changed")), 1)

    def test_a_refusal_a_validation_error_and_a_stale_revision_write_no_event(self):
        with mock.patch.dict(os.environ, OPERATOR_ENV):
            browser = self.as_(self.superuser)
            self.assertEqual(put(self.as_(self.operator), body([corp_connection()])).status_code, 403)
            self.assertEqual(put(browser, body([corp_connection(provider_type="plugin")])).status_code, 400)
            self.assertEqual(settings_events(), [])
            put(browser, body([corp_connection()]))
            count = len(settings_events())
            self.assertEqual(put(browser, body([corp_connection(), ollama_connection()], expected=0)).status_code, 409)
            self.assertEqual(len(settings_events()), count)


# --- migration compatibility -----------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class MigrationCompatibilityTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """ai_settings 0002: an existing row stays valid and keeps its meaning; the reverse restores 0001. Returns to the latest migrations afterwards."""

    BEFORE = ("ai_settings", "0001_initial")
    AFTER = ("ai_settings", "0002_connections")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return MigrationExecutor(connection).loader.project_state([target]).apps

    def setUp(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes()))

    def test_a_row_written_before_the_migration_is_intact_and_reads_as_the_legacy_provider(self):
        old_apps = self.migrate(self.BEFORE)
        old = old_apps.get_model("ai_settings", "AIExtractionSettings")
        old.objects.create(id=1, revision=4, extraction_mode="ai", provider_id="kpmg_workbench", profile_id="workbench-gpt5", policy_mode="company_endpoint", timeout_seconds=40)
        self.migrate(self.AFTER)
        row = AIExtractionSettings.objects.get()
        self.assertEqual((row.revision, row.extraction_mode, row.provider_id, row.profile_id, row.timeout_seconds), (4, "ai", "kpmg_workbench", "workbench-gpt5", 40))
        self.assertEqual((row.connection_id, row.connections), (None, []))
        stored = services.load_stored_settings()
        self.assertEqual((stored.values.connection_id, stored.connections, stored.revision), (None, (), 4))

    def test_a_deterministic_row_survives_too(self):
        old_apps = self.migrate(self.BEFORE)
        old_apps.get_model("ai_settings", "AIExtractionSettings").objects.create(id=1, revision=2)
        self.migrate(self.AFTER)
        self.assertEqual(AIExtractionSettings.objects.get().extraction_mode, "deterministic")

    def test_the_new_constraint_is_enforced_by_the_database(self):
        self.migrate(self.AFTER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIExtractionSettings.objects.create(id=1, extraction_mode="deterministic", connection_id="corp-llm")
        AIExtractionSettings.objects.all().delete()
        AIExtractionSettings.objects.create(id=1, extraction_mode="ai", provider_id="openai_compatible", connection_id="corp-llm", profile_id="p", policy_mode="company_endpoint", timeout_seconds=30)
        AIExtractionSettings.objects.all().delete()
        AIExtractionSettings.objects.create(id=1, extraction_mode="ai", provider_id="kpmg_workbench", profile_id="p", policy_mode="company_endpoint", timeout_seconds=30)  # no connection: legacy
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIExtractionSettings.objects.filter(pk=1).update(provider_id=None)  # an ai row still needs every ai field

    def test_the_reverse_restores_the_first_schema_and_keeps_the_row(self):
        self.migrate(self.AFTER)
        AIExtractionSettings.objects.create(id=1, revision=3, extraction_mode="ai", provider_id="kpmg_workbench", profile_id="workbench-gpt5", policy_mode="company_endpoint", timeout_seconds=30)
        old_apps = self.migrate(self.BEFORE)
        old = old_apps.get_model("ai_settings", "AIExtractionSettings")
        self.assertEqual(old.objects.get().revision, 3)
        with connection.cursor() as cursor:
            columns = [c.name for c in connection.introspection.get_table_description(cursor, "ai_settings_aiextractionsettings")]
        self.assertNotIn("connections", columns)
        self.assertNotIn("connection_id", columns)


# --- concurrent updates ----------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConcurrentConnectionUpdateTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        cache.clear()
        call_command("setup_roles", stdout=io.StringIO())
        self.admins = [base.make_user("root-a", superuser=True), base.make_user("root-b", superuser=True)]

    def save(self, user, connections, expected):
        return lambda: services.update_extraction_settings(
            user=user, data={"extraction_mode": "deterministic", "connections": connections}, expected_revision=expected, catalog=CATALOG, lease_seconds=60
        )

    def outcomes(self, results):
        return sorted("saved" if kind == "ok" else type(value).__name__ for kind, value in results)

    def test_two_concurrent_first_saves_of_different_lists_store_exactly_one_of_them(self):
        results = run_threads(self, [self.save(self.admins[0], [corp_connection()], 0), self.save(self.admins[1], [ollama_connection()], 0)])
        self.assertEqual(self.outcomes(results), ["AISettingsConflictError", "saved"])
        row = AIExtractionSettings.objects.get()
        self.assertEqual((row.revision, len(row.connections)), (1, 1))
        winner = row.connections[0]["connection_id"]
        self.assertEqual([e.details["connection_id"] for e in settings_events("ai_settings.connection_changed")], [winner])
        self.assertEqual(len(settings_events("ai_settings.updated")), 1)

    def test_two_concurrent_updates_from_the_same_revision_do_not_overwrite_each_other(self):
        services.update_extraction_settings(user=self.admins[0], data={"extraction_mode": "deterministic", "connections": [corp_connection()]}, expected_revision=0, catalog=CATALOG, lease_seconds=60)
        results = run_threads(self, [self.save(self.admins[0], [corp_connection(display_name="One")], 1), self.save(self.admins[1], [corp_connection(display_name="Two")], 1)])
        self.assertEqual(self.outcomes(results), ["AISettingsConflictError", "saved"])
        row = AIExtractionSettings.objects.get()
        self.assertEqual(row.revision, 2)
        self.assertIn(row.connections[0]["display_name"], ("One", "Two"))
        self.assertEqual(len(settings_events("ai_settings.updated")), 2)
        self.assertEqual(len(settings_events("ai_settings.connection_changed")), 2)  # the first save created it, the winner of the race updated it

    def test_a_stale_save_after_a_concurrent_one_is_a_conflict(self):
        services.update_extraction_settings(user=self.admins[0], data={"extraction_mode": "deterministic", "connections": [corp_connection()]}, expected_revision=0, catalog=CATALOG, lease_seconds=60)
        services.update_extraction_settings(user=self.admins[1], data={"extraction_mode": "deterministic", "connections": [ollama_connection()]}, expected_revision=1, catalog=CATALOG, lease_seconds=60)
        with self.assertRaises(AISettingsConflictError):
            self.save(self.admins[0], [corp_connection(display_name="Late")], 1)()
        self.assertEqual([c["connection_id"] for c in AIExtractionSettings.objects.get().connections], ["ollama-local"])


# --- the worker selects the stored connection, and the existing extraction still works -------------------------------------------


@tag("requires_postgres")
@requires_postgres
class WorkerSelectionTests(AIWorkerCase):
    """The production composition reads the REAL stored row at start-up, then extracts through the selected connection over a scripted transport."""

    def store(self, **fields):
        base_fields = dict(id=1, revision=3, connections=[c for c in all_connections()])
        base_fields.update(fields)
        return AIExtractionSettings.objects.create(**base_fields)

    def prepare(self, *script, environ=None):
        transport = FakeTransport(*script)
        line = task_registry.prepare_worker(60, OPERATOR_ENV if environ is None else environ, transport=transport)
        return line, transport

    def test_the_worker_reads_the_stored_row_and_extracts_through_the_selected_connection_and_model(self):
        self.store(extraction_mode="ai", provider_id="openai_compatible", connection_id="corp-llm", profile_id="corp-large", policy_mode="company_endpoint", timeout_seconds=30)
        line, transport = self.prepare(answer_with(VALID_OUTPUT))
        self.assertIn("connection corp-llm", line)
        result = submit(self.user)
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60)
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        (request,) = transport.requests
        self.assertEqual(json.loads(request.body)["model"], "corp-model-70b")
        self.assertIn("corp-large", ExtractionOutcome.objects.get(run_id=result.run.pk).extractor_version)
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).state, "needs_review")
        self.assertEqual(ResultRevision.objects.filter(run=result.run).count(), 1)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, self.everything_stored_about_the_queue_and_audit())

    def test_a_missing_or_disabled_connection_stops_the_start_up_and_installs_nothing(self):
        intake_handlers.install_extractor_source(None)
        disabled = [corp_connection(enabled=False)]
        for fields in (
            dict(extraction_mode="ai", provider_id="openai_compatible", connection_id="gone", profile_id="corp-small", policy_mode="company_endpoint", timeout_seconds=30),
            dict(extraction_mode="ai", provider_id="openai_compatible", connection_id="corp-llm", profile_id="corp-small", policy_mode="company_endpoint", timeout_seconds=30, connections=disabled),
        ):
            with self.subTest(fields["connection_id"]):
                AIExtractionSettings.objects.all().delete()
                self.store(**fields)
                with self.assertRaises(task_registry.WorkerConfigurationError):
                    self.prepare()
                self.assertIsNone(intake_handlers._extractor_source)

    def test_a_legacy_row_with_no_connection_still_selects_the_environment_workbench(self):
        self.store(extraction_mode="ai", provider_id="kpmg_workbench", profile_id="workbench-gpt5", policy_mode="company_endpoint", timeout_seconds=30, connections=[])
        line, _ = self.prepare()
        self.assertIn("profile workbench-gpt5", line)
        self.assertNotIn("connection ", line)

    def test_a_stored_deterministic_row_keeps_the_fixture_parser_whatever_connections_exist(self):
        self.store(extraction_mode="deterministic")
        line, _ = self.prepare(environ={})
        self.assertIn("deterministic", line)
        self.assertIsNone(intake_handlers._extractor_source())
        result = submit(self.user)
        self.assertEqual(worker.process_one(worker_id="test-worker", lease_seconds=60).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(ExtractionOutcome.objects.get(run_id=result.run.pk).extractor_name, "deterministic-test-fixture-parser")

    def test_a_failing_provider_keeps_the_queues_retry_and_recovery_and_writes_nothing_else(self):
        from tests.intake_ai_support import status

        self.store(extraction_mode="ai", provider_id="openai_compatible", connection_id="corp-llm", profile_id="corp-small", policy_mode="company_endpoint", timeout_seconds=30)
        self.prepare(status(401))
        result = submit(self.user)
        outcome = worker.process_one(worker_id="test-worker", lease_seconds=60)
        self.assertNotEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(ResultRevision.objects.filter(run=result.run).count(), 0)
