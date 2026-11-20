"""DB-free tests of the audit event for a successful AI settings change.

The real services and the real event builder (``apps.audit.events.user_event``, so the real vocabulary and detail-value checks) run; the recorder, the row and
the transaction are the simulations in ``tests/ai_settings_support.py``. Whether a REAL rollback undoes the row, whether the CHECK constraints accept the event
and whether the migration applies are in ``tests/test_ai_settings_postgres.py`` and are NOT run in the development VM. No network, no Workbench, no real secret.
"""

import json
import os
import re
import uuid
from pathlib import Path
from unittest import mock

from django.db import IntegrityError
from django.test import SimpleTestCase

from apps.accounts.errors import AuthorizationError
from apps.ai_settings import services
from apps.ai_settings.errors import AISettingsConflictError, AISettingsValidationError
from apps.ai_settings.models import SETTINGS_SUBJECT_ID
from apps.audit import events as audit_events
from tests.test_ai_settings_api import (
    ALL_ROLE_PERMISSIONS,
    CATALOG,
    CHARGE_SENTINEL,
    KEY_SENTINEL,
    PROFILE,
    PROVIDER,
    PROXY_SENTINEL,
    ADMIN_ID,
    ApiCase,
    FakeStore,
    actor,
    ai_body,
    fake_user,
    patched_services,
    stored_row,
)

BACKEND = Path(__file__).resolve().parent.parent
SENTINELS = (KEY_SENTINEL, PROXY_SENTINEL, CHARGE_SENTINEL)
DETERMINISTIC = {"extraction_mode": "deterministic"}
LEGACY = {"connection_id": None, "connection_count": 0, "connections_changed": False}  # what a save without connections records about them


def ai_data(**changes):
    return {k: v for k, v in ai_body(**changes).items() if k != "expected_revision"}


def update(store, data, expected=0, who=None, environ=None):
    env = {"AP_AI_WORKBENCH_API_KEY": KEY_SENTINEL, "AP_AI_WORKBENCH_PROXY_URL": PROXY_SENTINEL, "AP_AI_WORKBENCH_CHARGE_CODE": CHARGE_SENTINEL, **(environ or {})}
    with mock.patch.dict(os.environ, env), patched_services(store, who or actor(is_superuser=True)):
        return services.update_extraction_settings(user=fake_user(), data=data, expected_revision=expected, catalog=CATALOG, lease_seconds=60)


class SuccessfulUpdateTests(SimpleTestCase):
    def test_a_first_ai_save_records_exactly_one_event_with_the_contract_fields(self):
        store = FakeStore()
        update(store, ai_data())
        (event,) = store.events
        self.assertEqual(event.actor_kind, "user")
        self.assertEqual(event.actor_public_id, ADMIN_ID)
        self.assertEqual((event.action, event.subject_type, event.subject_id), ("ai_settings.updated", "ai_settings", SETTINGS_SUBJECT_ID))
        self.assertEqual((event.case_id, event.run_id), (None, None))
        self.assertEqual(
            event.details,
            {**LEGACY, "revision": 1, "extraction_mode": "ai", "provider_id": PROVIDER, "profile_id": PROFILE, "policy_mode": "company_endpoint", "timeout_seconds": 45},
        )

    def test_the_event_carries_the_new_revision_not_the_expected_one(self):
        store = FakeStore(stored_row(revision=6, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=30))
        update(store, ai_data(timeout_seconds=40), expected=6)
        (event,) = store.events
        self.assertEqual((event.details["revision"], event.details["timeout_seconds"]), (7, 40))

    def test_a_deterministic_save_records_no_ai_identifiers(self):
        store = FakeStore(stored_row(revision=2, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=45))
        update(store, DETERMINISTIC, expected=2)
        (event,) = store.events
        self.assertEqual(
            event.details, {**LEGACY, "revision": 3, "extraction_mode": "deterministic", "provider_id": None, "profile_id": None, "policy_mode": None, "timeout_seconds": None}
        )

    def test_the_actor_is_the_authorized_administrator_whose_public_id_is_recorded(self):
        other = actor(is_superuser=True, public_id=uuid.UUID(int=ADMIN_ID.int ^ 1))
        store = FakeStore()
        update(store, ai_data(), who=other)
        self.assertEqual(store.events[0].actor_public_id, other.public_id)

    def test_every_successful_save_adds_one_event_in_order(self):
        store = FakeStore()
        update(store, ai_data())
        update(store, DETERMINISTIC, expected=1)
        update(store, ai_data(timeout_seconds=20), expected=2)
        self.assertEqual([e.details["revision"] for e in store.events], [1, 2, 3])
        self.assertEqual([e.details["extraction_mode"] for e in store.events], ["ai", "deterministic", "ai"])

    def test_the_event_is_recorded_inside_the_transaction_and_after_the_row_write(self):
        store = FakeStore()
        seen = []
        real = store.record_event

        def watching(event):
            seen.append((store.depth, store.row is not None and store.row.revision))
            real(event)

        with mock.patch.object(store, "record_event", watching):
            update(store, ai_data())
        self.assertEqual(seen, [(1, 1)])

    def test_the_event_passes_the_real_vocabulary_checks(self):
        store = FakeStore()
        update(store, ai_data())
        details = store.events[0].details
        rebuilt = audit_events.user_event(
            ADMIN_ID, audit_events.ACTION_AI_SETTINGS_UPDATED, audit_events.SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID, **details
        )
        self.assertEqual(rebuilt, store.events[0])


class NoEventTests(SimpleTestCase):
    def assert_nothing_recorded(self, store, revision):
        self.assertEqual(store.events, [])
        self.assertEqual(None if store.row is None else store.row.revision, revision)

    def test_a_validation_failure_records_nothing(self):
        store = FakeStore(stored_row(revision=1))
        bad = (
            {"extraction_mode": "ai"},
            ai_data(policy_mode="local_only"),
            ai_data(profile_id="nope"),
            ai_data(provider_id="nope"),
            ai_data(timeout_seconds=60),
            {"extraction_mode": "deterministic", "provider_id": PROVIDER},
            {"extraction_mode": "nonsense"},
        )
        for data in bad:
            with self.subTest(data), self.assertRaises(AISettingsValidationError):
                update(store, data, expected=1)
        self.assert_nothing_recorded(store, 1)

    def test_an_unauthorized_caller_records_nothing_whatever_role_they_hold(self):
        for permissions in (frozenset(), ALL_ROLE_PERMISSIONS):
            store = FakeStore(stored_row(revision=1))
            with self.subTest(permissions=bool(permissions)), self.assertLogs("apps.accounts.authz", "WARNING"), self.assertRaises(AuthorizationError):
                update(store, ai_data(), expected=1, who=actor(is_superuser=False, permissions=permissions))
            self.assert_nothing_recorded(store, 1)

    def test_an_unauthenticated_or_inactive_caller_records_nothing(self):
        for reason in ("not_authenticated", "inactive"):
            store = FakeStore()
            with self.subTest(reason), self.assertRaises(AuthorizationError):
                update(store, ai_data(), who=AuthorizationError(reason))
            self.assert_nothing_recorded(store, None)

    def test_a_stale_revision_records_nothing_and_changes_nothing(self):
        store = FakeStore(stored_row(revision=3))
        for expected in (0, 2, 4):
            with self.subTest(expected), self.assertRaises(AISettingsConflictError):
                update(store, ai_data(), expected=expected)
        self.assert_nothing_recorded(store, 3)

    def test_a_stale_revision_does_not_even_build_an_audit_event(self):
        store = FakeStore(stored_row(revision=3))
        with mock.patch.object(services, "user_event") as build, self.assertRaises(AISettingsConflictError):
            update(store, ai_data(), expected=2)
        build.assert_not_called()

    def test_losing_the_race_to_create_the_row_records_nothing(self):
        store = FakeStore(race=True)
        with self.assertRaises(AISettingsConflictError):
            update(store, ai_data())
        self.assert_nothing_recorded(store, None)

    def test_reading_the_settings_records_nothing(self):
        store = FakeStore(stored_row(revision=1))
        with patched_services(store, actor(is_superuser=True)):
            services.get_extraction_settings(user=fake_user(), catalog=CATALOG)
            services.load_stored_settings()
        self.assert_nothing_recorded(store, 1)


class RollbackTests(SimpleTestCase):
    def test_an_audit_failure_undoes_the_settings_change_and_propagates(self):
        for error in (RuntimeError("audit down"), IntegrityError("constraint")):
            store = FakeStore(stored_row(revision=2, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=45), audit_error=error)
            with self.subTest(type(error).__name__), self.assertRaises(type(error)):
                update(store, DETERMINISTIC, expected=2)
            self.assertTrue(store.rolled_back)
            self.assertEqual((store.row.revision, store.row.extraction_mode, store.row.profile_id), (2, "ai", PROFILE))
            self.assertEqual(store.events, [])

    def test_a_failed_first_save_leaves_no_row_and_no_event(self):
        store = FakeStore(audit_error=RuntimeError("audit down"))
        with self.assertRaises(RuntimeError):
            update(store, ai_data())
        self.assertIsNone(store.row)
        self.assertEqual(store.events, [])

    def test_an_audit_failure_is_not_reported_as_a_stale_revision_or_a_validation_error(self):
        store = FakeStore(stored_row(revision=1), audit_error=IntegrityError("x"))
        with self.assertRaises(IntegrityError) as caught:
            update(store, DETERMINISTIC, expected=1)
        self.assertNotIsInstance(caught.exception, (AISettingsConflictError, AISettingsValidationError))

    def test_the_success_log_line_is_not_written_when_the_audit_write_fails(self):
        store = FakeStore(audit_error=RuntimeError("audit down"))
        with self.assertRaises(RuntimeError), self.assertNoLogs(services.logger, "INFO"):
            update(store, ai_data())

    def test_a_recorder_outside_a_transaction_is_refused_like_the_real_one(self):
        from apps.audit.recorder import AuditTransactionError

        store = FakeStore()
        event = audit_events.user_event(ADMIN_ID, audit_events.ACTION_AI_SETTINGS_UPDATED, audit_events.SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID, revision=1)
        with self.assertRaises(AuditTransactionError):
            store.record_event(event)


class SecretLeakageTests(SimpleTestCase):
    def test_no_secret_reaches_the_event_whatever_the_environment_holds(self):
        store = FakeStore()
        update(store, ai_data())
        text = json.dumps([e.__dict__ for e in store.events], default=str)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_the_event_holds_only_the_documented_keys_and_scalar_values(self):
        store = FakeStore()
        update(store, ai_data())
        details = store.events[0].details
        self.assertEqual(set(details), audit_events.DETAIL_KEYS[audit_events.ACTION_AI_SETTINGS_UPDATED])
        for value in details.values():
            self.assertIsInstance(value, (str, int, type(None)))

    def test_the_event_builder_refuses_a_secret_shaped_or_free_text_value(self):
        build = audit_events.user_event
        args = (ADMIN_ID, audit_events.ACTION_AI_SETTINGS_UPDATED, audit_events.SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID)
        for bad in ({"api_key": "x"}, {"profile_id": "has space"}, {"provider_id": KEY_SENTINEL + " extra"}, {"proxy_url": "http://u:p@h"}, {"profile_id": {"nested": 1}}):
            with self.subTest(bad), self.assertRaises(Exception):
                build(*args, revision=1, **bad)

    def test_a_secret_in_a_validated_field_is_refused_before_anything_is_written(self):
        store = FakeStore()
        with self.assertRaises(AISettingsValidationError):
            update(store, ai_data(profile_id=KEY_SENTINEL))
        self.assertEqual(store.events, [])
        self.assertIsNone(store.row)

    def test_the_services_source_never_reads_a_secret_setting_for_the_event(self):
        text = (BACKEND / "apps" / "ai_settings" / "services.py").read_text(encoding="utf-8")
        start = text.index("        record_event(")
        call = text[start : text.index("    logger.info(", start)]
        self.assertNotRegex(call.lower(), r"environ|api_key|proxy|charge|secret|token|password")


class ApiAuditTests(ApiCase):
    def test_put_200_records_one_event_for_the_session_user(self):
        store = FakeStore()
        response, text = self.call("put", store, actor(is_superuser=True), ai_body())
        self.assertEqual(response.status_code, 200)
        (event,) = store.events
        self.assertEqual((event.action, event.actor_public_id, event.details["revision"], event.details["profile_id"]), ("ai_settings.updated", ADMIN_ID, 1, PROFILE))
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
            self.assertNotIn(sentinel, json.dumps(event.details))

    def test_get_records_nothing(self):
        store = FakeStore(stored_row(revision=1))
        response, _ = self.call("get", store, actor(is_superuser=True))
        self.assertEqual((response.status_code, store.events), (200, []))

    def test_403_409_400_and_an_audit_failure_record_nothing_and_say_so(self):
        cases = (
            ("403", FakeStore(stored_row(revision=1)), actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS), ai_body(expected_revision=1), 403),
            ("409", FakeStore(stored_row(revision=5)), actor(is_superuser=True), ai_body(expected_revision=4), 409),
            ("400", FakeStore(stored_row(revision=1)), actor(is_superuser=True), ai_body(expected_revision=1, policy_mode="local_only"), 400),
        )
        for name, store, who, body, status in cases:
            with self.subTest(name):
                if status == 403:
                    with self.assertLogs("apps.accounts.authz", "WARNING"):
                        response, _ = self.call("put", store, who, body)
                else:
                    response, _ = self.call("put", store, who, body)
                self.assertEqual(response.status_code, status)
                self.assertEqual(store.events, [])

    def test_an_audit_failure_is_a_500_with_no_event_and_no_settings_change(self):
        store = FakeStore(stored_row(revision=1), audit_error=RuntimeError(KEY_SENTINEL))
        with self.assertLogs("apps.api.errors", "ERROR"):
            self.client.raise_request_exception = False
            response, text = self.call("put", store, actor(is_superuser=True), ai_body(expected_revision=1))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(KEY_SENTINEL, text)
        self.assertEqual((store.events, store.row.revision), ([], 1))


class ContractPinTests(SimpleTestCase):
    def test_the_audit_contract_is_pinned(self):
        self.assertEqual(audit_events.ACTION_AI_SETTINGS_UPDATED, "ai_settings.updated")
        self.assertEqual(audit_events.SUBJECT_AI_SETTINGS, "ai_settings")
        self.assertEqual(
            audit_events.DETAIL_KEYS[audit_events.ACTION_AI_SETTINGS_UPDATED],
            frozenset({"revision", "extraction_mode", "provider_id", "profile_id", "policy_mode", "timeout_seconds", "connection_id", "connection_count", "connections_changed"}),
        )
        self.assertIn(audit_events.ACTION_AI_SETTINGS_UPDATED, audit_events.ACTIONS)
        self.assertIn(audit_events.SUBJECT_AI_SETTINGS, audit_events.SUBJECT_TYPES)

    def test_the_subject_id_is_a_fixed_deterministic_value(self):
        self.assertEqual(SETTINGS_SUBJECT_ID, uuid.uuid5(uuid.NAMESPACE_URL, "urn:automation-platform:ai-settings:extraction"))
        self.assertEqual(SETTINGS_SUBJECT_ID.version, 5)

    def test_the_only_place_that_records_the_event_is_the_update_service(self):
        hits = [p.relative_to(BACKEND).as_posix() for p in (BACKEND / "apps").rglob("*.py") if "ACTION_AI_SETTINGS_UPDATED" in p.read_text(encoding="utf-8")]
        self.assertEqual(sorted(hits), ["apps/ai_settings/services.py", "apps/audit/events.py"])

    def test_the_event_is_recorded_exactly_once_in_the_services(self):
        text = (BACKEND / "apps" / "ai_settings" / "services.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"record_event\(", text)), 2)  # the settings event, and the per-connection event in a loop
        self.assertEqual(len(re.findall(r"SUBJECT_AI_SETTINGS, SETTINGS_SUBJECT_ID,", text)), 1)
        self.assertEqual(len(re.findall(r"SUBJECT_AI_SETTINGS, connection_subject_id\(", text)), 1)
