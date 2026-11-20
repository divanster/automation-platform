"""Tests of the managed AI credentials that REQUIRE a real PostgreSQL server (AI-SEC-2).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and
later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: WRITTEN BUT NEVER RUN. They were written on a development VM that has no PostgreSQL and no connection to one. Nothing in them is evidence yet; the project owner runs them locally.
When they do pass, that covers the tested scenarios only. This file is ASCII only.

The secret store is an in-memory stand-in: no Windows API, no file and no real credential is touched, and nothing is sent to any provider.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_ai_credentials_postgres --tag requires_postgres -v 2
"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase, tag

from apps.ai_credentials import services
from apps.ai_credentials.errors import CredentialInUseError
from apps.ai_credentials.rules import credential_subject_id
from apps.ai_settings import services as settings_services
from apps.ai_settings.models import AIExtractionSettings
from apps.audit.models import AuditEvent
from apps.secret_store import CODE_BUSY
from config.intake_extraction import build_catalog
from tests import test_api_postgres as base
from tests.ai_connections_support import REF_CORP, corp_connection
from tests.ai_credentials_support import SECRET_ONE, SECRET_TWO, MemoryStore
from tests.test_intake_postgres import DedicatedTestDatabaseMixin

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

COLLECTION = "/api/ai-credentials/"
DETAIL = f"/api/ai-credentials/{REF_CORP}/"
SETTINGS_URL = "/api/ai-settings/extraction/"
CATALOG = build_catalog()


def post(browser, body):
    return browser.client.post(COLLECTION, body, format="json", HTTP_X_CSRFTOKEN=browser.token)


def put(browser, path, body):
    return browser.client.put(path, body, format="json", HTTP_X_CSRFTOKEN=browser.token)


def delete(browser, path):
    return browser.client.delete(path, format="json", HTTP_X_CSRFTOKEN=browser.token)


def all_audit_text():
    return json.dumps(list(AuditEvent.objects.values()), default=str)


def managed_settings_body(**changes):
    body = {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="managed")], "expected_revision": 0}
    body.update(changes)
    return body


class StoreMixin:
    """Make the route's store factory return an in-memory store."""

    def use_store(self, store=None):
        self.memory = MemoryStore() if store is None else store
        patcher = mock.patch("config.secret_store.DisabledSecretStore", return_value=self.memory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.memory


@tag("requires_postgres")
@requires_postgres
class CredentialApiTests(StoreMixin, base.ApiCase):
    def setUp(self):
        super().setUp()
        self.use_store()

    def test_every_non_administrator_is_refused_and_nothing_is_stored_or_audited(self):
        for user in (self.operator, self.approver, self.auditor, self.nobody, self.hybrid):
            with self.subTest(user=user.username):
                browser = self.as_(user)
                self.assertEqual(browser.client.get(DETAIL).status_code, 403)
                self.assertEqual(post(browser, {"reference": REF_CORP, "value": SECRET_ONE}).status_code, 403)
                self.assertEqual(put(browser, DETAIL, {"value": SECRET_ONE}).status_code, 403)
                self.assertEqual(delete(browser, DETAIL).status_code, 403)
        self.assertEqual((self.memory.items, AuditEvent.objects.count()), ({}, 0))

    def test_an_anonymous_client_and_a_missing_csrf_token_are_refused(self):
        self.assertIn(base.Browser().client.get(DETAIL).status_code, (401, 403))
        browser = self.as_(self.superuser)
        self.assertEqual(browser.client.post(COLLECTION, {"reference": REF_CORP, "value": SECRET_ONE}, format="json").status_code, 403)
        self.assertEqual(self.memory.items, {})

    def test_the_lifecycle_is_audited_with_the_actor_an_opaque_subject_and_no_secret(self):
        browser = self.as_(self.superuser)
        self.assertEqual(post(browser, {"reference": REF_CORP, "value": SECRET_ONE}).status_code, 201)
        self.assertEqual(post(browser, {"reference": REF_CORP, "value": SECRET_TWO}).status_code, 409)
        self.assertEqual(put(browser, DETAIL, {"value": SECRET_TWO}).status_code, 200)
        self.assertEqual(browser.client.get(DETAIL).json()["status"], "set")
        self.assertEqual(delete(browser, DETAIL).status_code, 200)
        events = list(AuditEvent.objects.order_by("id"))
        self.assertEqual([e.action for e in events], ["ai_credential.created", "ai_credential.replaced", "ai_credential.deleted"])
        for event in events:
            self.assertEqual((event.actor_public_id, event.subject_id, event.details), (self.superuser.public_id, credential_subject_id(REF_CORP), {"store": "memory"}))
            self.assertIsNotNone(event.occurred_at)
        text = all_audit_text()
        for forbidden in (SECRET_ONE, SECRET_TWO, REF_CORP):
            self.assertNotIn(forbidden, text)

    def test_a_refused_or_failed_operation_leaves_no_audit_row(self):
        browser = self.as_(self.superuser)
        self.assertEqual(delete(browser, DETAIL).status_code, 404)
        self.assertEqual(put(browser, DETAIL, {"value": SECRET_ONE}).status_code, 404)
        self.use_store(MemoryStore(fail={"put": CODE_BUSY}))
        self.assertEqual(post(browser, {"reference": REF_CORP, "value": SECRET_ONE}).status_code, 503)
        self.assertEqual(AuditEvent.objects.count(), 0)  # a real rollback: the event written before the store was asked is gone

    def test_a_failed_audit_write_stops_the_change(self):
        browser = self.as_(self.superuser)
        with mock.patch.object(services, "record_event", side_effect=RuntimeError("audit down")):
            response = post(browser, {"reference": REF_CORP, "value": SECRET_ONE})
        self.assertEqual(response.status_code, 500)
        self.assertEqual((self.memory.items, AuditEvent.objects.count()), ({}, 0))

    def test_no_response_and_no_table_holds_the_secret(self):
        browser = self.as_(self.superuser)
        bodies = [
            post(browser, {"reference": REF_CORP, "value": SECRET_ONE}).content,
            put(browser, DETAIL, {"value": " " + SECRET_TWO}).content,
            put(browser, DETAIL, {"value": SECRET_TWO}).content,
            browser.client.get(DETAIL).content,
        ]
        for body in bodies:
            self.assertNotIn(SECRET_ONE.encode(), body)
            self.assertNotIn(SECRET_TWO.encode(), body)
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names():
                cursor.execute(f'SELECT * FROM "{table}"')
                for row in cursor.fetchall():
                    self.assertNotIn(SECRET_ONE, repr(row))
                    self.assertNotIn(SECRET_TWO, repr(row))

    def test_a_credential_a_connection_reads_cannot_be_deleted_until_the_connection_no_longer_does(self):
        browser = self.as_(self.superuser)
        post(browser, {"reference": REF_CORP, "value": SECRET_ONE})
        saved = put(browser, SETTINGS_URL, managed_settings_body())
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["connections"][0]["credential_source"], "managed")
        self.assertEqual(browser.client.get(DETAIL).json()["connections"], ["corp-llm"])
        refused = delete(browser, DETAIL)
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (409, "credential_in_use"))
        self.assertEqual(self.memory.value_of(REF_CORP), SECRET_ONE)
        switched = put(browser, SETTINGS_URL, {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="environment")], "expected_revision": 1})
        self.assertEqual(switched.status_code, 200, switched.content)
        self.assertEqual(delete(browser, DETAIL).status_code, 200)
        self.assertEqual(self.memory.items, {})


@tag("requires_postgres")
@requires_postgres
class CredentialSourceSettingsTests(base.ApiCase):
    def test_the_source_is_stored_in_the_connections_column_and_an_omitted_source_keeps_the_stored_one(self):
        browser = self.as_(self.superuser)
        self.assertEqual(put(browser, SETTINGS_URL, managed_settings_body()).status_code, 200)
        self.assertEqual(AIExtractionSettings.objects.get().connections[0]["credential_source"], "managed")
        omitted = {key: value for key, value in corp_connection().items() if key != "credential_source"}
        self.assertEqual(put(browser, SETTINGS_URL, {"extraction_mode": "deterministic", "connections": [omitted], "expected_revision": 1}).status_code, 200)
        self.assertEqual(AIExtractionSettings.objects.get().connections[0]["credential_source"], "managed")  # leaving the field out cannot switch to the environment
        self.assertEqual(put(browser, SETTINGS_URL, {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="environment")], "expected_revision": 2}).status_code, 200)
        self.assertEqual(AIExtractionSettings.objects.get().connections[0]["credential_source"], "environment")

    def test_a_managed_connection_without_a_reference_is_refused_and_nothing_is_stored(self):
        browser = self.as_(self.superuser)
        response = put(browser, SETTINGS_URL, managed_settings_body(connections=[corp_connection(credential_source="managed", credential_ref=None)]))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AIExtractionSettings.objects.exists())

    def test_a_connection_event_records_the_source_and_no_reference(self):
        put(self.as_(self.superuser), SETTINGS_URL, managed_settings_body())
        (event,) = AuditEvent.objects.filter(action="ai_settings.connection_changed")
        self.assertEqual(event.details["credential_source"], "managed")
        self.assertNotIn(REF_CORP, all_audit_text())


@tag("requires_postgres")
@requires_postgres
class UsersOfAReferenceTests(DedicatedTestDatabaseMixin, TestCase):
    def test_the_lookup_finds_only_managed_connections_that_name_the_reference_and_locks_the_row_when_asked(self):
        AIExtractionSettings.objects.create(
            id=1, connections=[corp_connection(credential_source="managed"), corp_connection(connection_id="env-copy"), corp_connection(connection_id="other", credential_ref="AP_AI_CRED_OTHER", credential_source="managed")]
        )
        self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP), ("corp-llm",))
        with transaction.atomic():
            self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP, lock=True), ("corp-llm",))
        self.assertEqual(settings_services.connections_using_managed_reference("AP_AI_CRED_NONE"), ())

    def test_no_row_means_no_users(self):
        self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP), ())


@tag("requires_postgres")
@requires_postgres
class DeleteVersusSettingsSaveTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """A delete decides under the settings row's lock: a settings save that is in flight when the delete starts is either seen by it or waited for, never overtaken."""

    def setUp(self):
        cache.clear()
        call_command("setup_roles", stdout=__import__("io").StringIO())
        self.admin = base.make_user("root-a", superuser=True)
        self.memory = MemoryStore().seed(REF_CORP, SECRET_ONE)
        AIExtractionSettings.objects.create(id=1, revision=1, connections=[])

    def test_a_delete_waits_for_a_concurrent_save_that_adds_a_user_and_then_refuses(self):
        locked, release, result = threading.Event(), threading.Event(), {}

        def saver():
            with transaction.atomic():
                row = AIExtractionSettings.objects.select_for_update().get(pk=1)
                row.connections = [corp_connection(credential_source="managed")]
                row.save()
                locked.set()
                release.wait(10)
            connection.close()

        def deleter():
            try:
                services.delete_credential(user=self.admin, reference=REF_CORP, store_factory=lambda: self.memory)
                result["outcome"] = "deleted"
            except CredentialInUseError:
                result["outcome"] = "in_use"
            finally:
                connection.close()

        first = threading.Thread(target=saver)
        first.start()
        self.assertTrue(locked.wait(10))
        second = threading.Thread(target=deleter)
        second.start()
        second.join(1.0)
        self.assertTrue(second.is_alive(), "the delete must wait for the settings row lock")
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(result, {"outcome": "in_use"})
        self.assertEqual(self.memory.value_of(REF_CORP), SECRET_ONE)
        self.assertEqual(AuditEvent.objects.filter(action="ai_credential.deleted").count(), 0)


BACKEND = Path(__file__).resolve().parent.parent
LOGGER = "apps.ai_credentials.services"


class ThreadedCase(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Real transactions across threads: every thread closes its own database connection when it is done."""

    def setUp(self):
        cache.clear()
        call_command("setup_roles", stdout=io.StringIO())
        self.admins = [base.make_user(f"root-{n}", superuser=True) for n in "abc"]
        self.memory = MemoryStore().seed(REF_CORP, SECRET_ONE).seed("AP_AI_CRED_OTHER", SECRET_ONE)

    def run_thread(self, target, *args):
        failures = []

        def body():
            try:
                target(*args)
            except BaseException as error:  # noqa: BLE001
                failures.append(error)
            finally:
                connection.close()

        thread = threading.Thread(target=body)
        thread.start()
        return thread, failures

    def advisory_locks(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT granted, count(*) FROM pg_locks WHERE locktype = 'advisory' GROUP BY granted")
            return dict(cursor.fetchall())


@tag("requires_postgres")
@requires_postgres
class ConcurrentRotationTests(ThreadedCase):
    """The reference's transaction-level advisory lock is taken before the audit INSERT: operations on one reference run one after the other, so the audit order is the store write order."""

    def test_two_rotations_of_one_reference_are_serialized_and_audit_order_is_write_order(self):
        in_put, release, written = threading.Event(), threading.Event(), []
        real_put = self.memory._put

        def slow_put(reference, secret, replace):
            written.append((reference, secret.reveal()))
            if secret.reveal().endswith("-first"):
                in_put.set()
                release.wait(15)
            return real_put(reference, secret, replace)

        self.memory._put = slow_put

        def rotate(admin, reference, value):
            services.replace_credential(user=admin, reference=reference, value=value, store_factory=lambda: self.memory)

        first, first_failures = self.run_thread(rotate, self.admins[0], REF_CORP, SECRET_TWO + "-first")
        self.assertTrue(in_put.wait(15))
        second, second_failures = self.run_thread(rotate, self.admins[1], REF_CORP, SECRET_TWO + "-second")
        other, other_failures = self.run_thread(rotate, self.admins[2], "AP_AI_CRED_OTHER", SECRET_TWO + "-other")
        other.join(15)
        self.assertFalse(other.is_alive(), "a different reference must not wait")
        locks = self.advisory_locks()
        deadline = time.monotonic() + 10
        while locks.get(False, 0) < 1 and time.monotonic() < deadline:  # wait (bounded) until the second rotation is blocked on the reference's lock
            time.sleep(0.05)
            locks = self.advisory_locks()
        self.assertTrue(second.is_alive(), "the same reference must wait for the first rotation's transaction")
        self.assertEqual((locks.get(True, 0) >= 1, locks.get(False, 0)), (True, 1))  # one lock held, one waiting
        # READ COMMITTED: the first rotation's audit row is still in its open transaction, and the second has not inserted anything, so only the committed event of the other reference is visible
        self.assertEqual([e.actor_public_id for e in AuditEvent.objects.order_by("id")], [self.admins[2].public_id])
        release.set()
        first.join(15)
        second.join(15)
        self.assertEqual((first_failures, second_failures, other_failures), ([], [], []))
        order = [e.actor_public_id for e in AuditEvent.objects.order_by("id")]
        self.assertEqual(order, [self.admins[0].public_id, self.admins[2].public_id, self.admins[1].public_id])
        self.assertEqual([value for reference, value in written if reference == REF_CORP], [SECRET_TWO + "-first", SECRET_TWO + "-second"])
        self.assertEqual(self.memory.value_of(REF_CORP), SECRET_TWO + "-second")  # the last audited writer is the current value's writer
        self.assertEqual(self.advisory_locks(), {})  # released with each transaction

    def test_a_delete_and_a_settings_save_cannot_deadlock_with_a_rotation(self):
        AIExtractionSettings.objects.create(id=1, revision=1, connections=[])
        results = []

        def delete_other():
            try:
                services.delete_credential(user=self.admins[0], reference="AP_AI_CRED_OTHER", store_factory=lambda: self.memory)
                results.append("deleted")
            except CredentialInUseError:
                results.append("in_use")

        def rotate_ref():
            services.replace_credential(user=self.admins[1], reference=REF_CORP, value=SECRET_TWO, store_factory=lambda: self.memory)
            results.append("rotated")

        def save_settings():
            settings_services.update_extraction_settings(
                user=self.admins[2], data={"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="managed", credential_ref="AP_AI_CRED_OTHER")]},
                expected_revision=1, catalog=CATALOG, lease_seconds=60,
            )  # fmt: skip
            results.append("saved")

        threads = [self.run_thread(delete_other), self.run_thread(rotate_ref), self.run_thread(save_settings)]
        for thread, _ in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "a lock cycle would leave a thread waiting")
        self.assertEqual([failures for _, failures in threads], [[], [], []])
        self.assertTrue({"rotated", "saved"} <= set(results) and len(results) == 3, results)  # the delete either won ("deleted") or was refused as in use
        self.assertEqual(self.advisory_locks(), {})


@tag("requires_postgres")
@requires_postgres
class RealCommitFailureTests(ThreadedCase):
    """A real failure of COMMIT, definite (rolled back) and ambiguous (the server committed, the acknowledgement was lost): nothing is deleted or overwritten to guess, the original exception propagates and
    ONE fixed ERROR line is logged for the operator."""

    def commit_fails(self, ambiguous):
        real_commit = connection.commit

        def failing():
            if ambiguous:
                real_commit()  # the server commits ...
            raise OperationalError("connection lost during COMMIT " + SECRET_TWO)  # ... and the caller is told it failed

        return mock.patch.object(connection, "commit", side_effect=failing)

    def operation(self, name):
        factory = lambda: self.memory  # noqa: E731
        return {
            "create": lambda: services.create_credential(user=self.admins[0], reference="AP_AI_CRED_NEW", value=SECRET_TWO, store_factory=factory),
            "replace": lambda: services.replace_credential(user=self.admins[0], reference=REF_CORP, value=SECRET_TWO, store_factory=factory),
            "delete": lambda: services.delete_credential(user=self.admins[0], reference=REF_CORP, store_factory=factory),
        }[name]

    def expected_items(self, name):
        return {"create": {REF_CORP: SECRET_ONE, "AP_AI_CRED_OTHER": SECRET_ONE, "AP_AI_CRED_NEW": SECRET_TWO}, "replace": {REF_CORP: SECRET_TWO, "AP_AI_CRED_OTHER": SECRET_ONE},
                "delete": {"AP_AI_CRED_OTHER": SECRET_ONE}}[name]  # fmt: skip

    def test_a_commit_failure_never_changes_the_store_further_and_logs_one_fixed_line(self):
        for ambiguous in (False, True):
            for name in ("create", "replace", "delete"):
                with self.subTest(ambiguous=ambiguous, name=name):
                    self.memory = MemoryStore().seed(REF_CORP, SECRET_ONE).seed("AP_AI_CRED_OTHER", SECRET_ONE)
                    before = AuditEvent.objects.count()
                    with self.commit_fails(ambiguous), self.assertLogs(LOGGER, "ERROR") as logged:
                        with self.assertRaises(OperationalError):
                            self.operation(name)()
                    self.assertEqual({r: s.reveal() for r, s in self.memory.items.items()}, self.expected_items(name))
                    self.assertEqual([call for call in self.memory.calls if call[0] == "delete"], [("delete", REF_CORP)] if name == "delete" else [])
                    self.assertEqual(AuditEvent.objects.count() - before, 1 if ambiguous else 0)  # the real outcome differs; the code never guessed
                    self.assertEqual(len(logged.records), 1)
                    text = logged.output[0]
                    for forbidden in (REF_CORP, "AP_AI_CRED_NEW", SECRET_ONE, SECRET_TWO, "COMMIT"):
                        self.assertNotIn(forbidden, text)
                    self.assertIn("reconcile", text)
                    self.assertIn(str(credential_subject_id("AP_AI_CRED_NEW" if name == "create" else REF_CORP)), text)
                    self.assertEqual(self.advisory_locks(), {})

    def test_an_operation_that_fails_before_the_store_logs_nothing_uncertain_and_leaves_no_event(self):
        self.memory = MemoryStore(fail={"put": CODE_BUSY}).seed(REF_CORP, SECRET_ONE)
        with self.assertNoLogs(LOGGER, "ERROR"):
            with self.assertRaises(Exception):
                services.replace_credential(user=self.admins[0], reference=REF_CORP, value=SECRET_TWO, store_factory=lambda: self.memory)
        self.assertEqual((AuditEvent.objects.count(), self.memory.value_of(REF_CORP)), (0, SECRET_ONE))


@tag("requires_postgres")
@requires_postgres
class ProcessCrashTests(ThreadedCase):
    """A child process is killed (``os._exit(137)``) during an operation: the database connection drops and the server rolls the open transaction back. A crash BEFORE the store call leaves nothing; a
    crash AFTER the store changed and before the commit leaves the credential changed and UNAUDITED. That window is accepted and documented (docs/architecture/secret-store.md, section 9); the
    reconciliation is an audited replace or delete."""

    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp(prefix="ap-crash-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.store_file = os.path.join(directory, "store.json")

    def child(self, operation, point):
        env = dict(os.environ, AP_DB_NAME=connection.settings_dict["NAME"], AP_CRASH_STORE_FILE=self.store_file, AP_CRASH_USER_PK=str(self.admins[0].pk), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-m", "tests.credential_crash_worker", operation, point], cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=120)

    def file_items(self):
        try:
            with open(self.store_file, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def audit_count(self):
        return AuditEvent.objects.filter(action__startswith="ai_credential.").count()

    def test_a_crash_before_the_store_call_leaves_no_credential_and_no_event(self):
        result = self.child("create", "before_store")
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual((self.file_items(), self.audit_count()), ({}, 0))
        self.assertEqual(self.advisory_locks(), {})  # the dead session's lock is gone with its transaction

    def test_a_crash_after_the_store_changed_leaves_an_unaudited_credential_which_an_audited_replace_reconciles(self):
        result = self.child("create", "after_store")
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual(self.file_items(), {"AP_AI_CRED_CRASH_TEST": "CRASH-SENTINEL-one-4c1e"})
        self.assertEqual(self.audit_count(), 0)  # the accepted window: changed, not audited
        from tests.credential_crash_worker import FileStore

        store = FileStore(self.store_file)
        status = services.get_status(user=self.admins[0], reference="AP_AI_CRED_CRASH_TEST", store_factory=lambda: store)
        self.assertEqual(status.status, "set")  # visible to the operator
        services.replace_credential(user=self.admins[0], reference="AP_AI_CRED_CRASH_TEST", value="CRASH-SENTINEL-three-0a5d", store_factory=lambda: store)  # the reconciliation
        self.assertEqual([e.action for e in AuditEvent.objects.order_by("id")], ["ai_credential.replaced"])

    def test_a_crash_after_a_delete_leaves_the_credential_gone_and_unaudited(self):
        self.assertEqual(self.child("create", "after_store").returncode, 137)  # puts the credential in the file (and dies)
        result = self.child("delete", "after_store")
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual((self.file_items(), self.audit_count()), ({}, 0))
