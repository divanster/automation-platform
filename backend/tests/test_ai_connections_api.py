"""DB-free tests of saving and reading named AI connections through the services and ``/api/ai-settings/extraction/``: one revision, one transaction, authorization, optimistic
concurrency, the audit events, and that no credential is ever stored, returned, logged or audited. The services run for real; only their database touch points are replaced
(``tests/ai_settings_support.py``). What needs a real database is in ``tests/test_ai_connections_postgres.py`` and is NOT run here."""

import json
import logging
import os
import uuid
from unittest import mock

from django.test import SimpleTestCase

from apps.accounts.errors import AuthorizationError
from apps.ai_settings import services
from apps.ai_settings.errors import AISettingsConflictError, AISettingsValidationError
from apps.ai_settings.models import SETTINGS_SUBJECT_ID, connection_subject_id
from apps.audit import events as audit_events
from tests.ai_connections_support import (
    CORP_ENDPOINT,
    OPERATOR_ENV,
    REF_CORP,
    SECRET_A,
    SECRET_B,
    SECRET_WORKBENCH,
    all_connections,
    cloud_connection,
    corp_connection,
    ollama_connection,
    workbench_connection,
)
from tests.test_ai_settings_api import ADMIN_ID, ALL_ROLE_PERMISSIONS, CATALOG, URL, ApiCase, FakeStore, actor, fake_user, patched_services, stored_row

SENTINELS = (SECRET_A, SECRET_B, SECRET_WORKBENCH)


def selection(**changes):
    values = {"extraction_mode": "ai", "connection_id": "corp-llm", "profile_id": "corp-small", "policy_mode": "company_endpoint", "timeout_seconds": 30}
    values.update(changes)
    return values


def update(store, data, expected=0, who=None, environ=None):
    with mock.patch.dict(os.environ, OPERATOR_ENV if environ is None else environ), patched_services(store, who or actor(is_superuser=True)):
        return services.update_extraction_settings(user=fake_user(), data=data, expected_revision=expected, catalog=CATALOG, lease_seconds=60)


def read(store, environ=None):
    with mock.patch.dict(os.environ, OPERATOR_ENV if environ is None else environ, clear=True), patched_services(store, actor(is_superuser=True)):
        return services.get_extraction_settings(user=fake_user(), catalog=CATALOG)


def row_with_connections(revision=3, connections=None, **kwargs):
    return stored_row(revision=revision, connections=list(all_connections() if connections is None else connections), **kwargs)


class SaveConnectionsTests(SimpleTestCase):
    def test_several_connections_and_a_selection_are_saved_together_as_revision_one(self):
        store = FakeStore()
        view = update(store, {**selection(), "connections": all_connections()})
        self.assertEqual((view.record.revision, view.record.values.connection_id, view.record.values.provider_id), (1, "corp-llm", "openai_compatible"))
        self.assertEqual([c["connection_id"] for c in store.row.connections], ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"])
        self.assertEqual(store.row.connections[1], corp_connection())
        self.assertEqual((store.row.connection_id, store.row.provider_id, store.row.profile_id), ("corp-llm", "openai_compatible", "corp-small"))
        self.assertEqual(len(view.connections), 5)

    def test_saving_without_connections_keeps_the_stored_list(self):
        row = row_with_connections(revision=2)
        store = FakeStore(row)
        view = update(store, selection(profile_id="corp-large"), expected=2)
        self.assertEqual(store.row.connections, all_connections())
        self.assertNotIn("connections", store.row.saved_with)
        self.assertEqual((view.record.revision, store.row.profile_id), (3, "corp-large"))

    def test_a_deterministic_save_with_connections_stores_them_and_selects_none(self):
        store = FakeStore()
        update(store, {"extraction_mode": "deterministic", "connections": [corp_connection()]})
        self.assertEqual((store.row.extraction_mode, store.row.connection_id, len(store.row.connections)), ("deterministic", None, 1))

    def test_an_empty_list_removes_every_connection(self):
        store = FakeStore(row_with_connections(revision=1))
        update(store, {"extraction_mode": "deterministic", "connections": []}, expected=1)
        self.assertEqual(store.row.connections, [])

    def test_the_selection_is_checked_against_the_list_in_the_same_save(self):
        store = FakeStore()
        with self.assertRaises(AISettingsValidationError) as caught:
            update(store, {**selection(connection_id="vendor", profile_id="vendor-chat"), "connections": [corp_connection()]})
        self.assertEqual(caught.exception.fields, {"connection_id": ["unknown_connection"]})
        self.assertIsNone(store.row)

    def test_a_selection_without_a_list_is_checked_against_the_stored_list(self):
        store = FakeStore(row_with_connections(revision=1))
        update(store, selection(connection_id="vendor", profile_id="vendor-chat", policy_mode="approved_cloud"), expected=1)
        self.assertEqual(store.row.connection_id, "vendor")
        with self.assertRaises(AISettingsValidationError) as caught:
            update(FakeStore(stored_row(revision=1)), selection(), expected=1)
        self.assertEqual(caught.exception.fields, {"connection_id": ["unknown_connection"]})

    def test_a_connection_that_is_selected_cannot_be_removed_or_disabled(self):
        removed = [c for c in all_connections() if c["connection_id"] != "corp-llm"]
        for connections, code in ((removed, "unknown_connection"), ([corp_connection(enabled=False)], "connection_disabled")):
            with self.subTest(code):
                store = FakeStore(row_with_connections(revision=4, mode="ai", provider="openai_compatible", profile="corp-small", policy="company_endpoint", timeout=30, connection="corp-llm"))
                with self.assertRaises(AISettingsValidationError) as caught:
                    update(store, {**selection(), "connections": connections}, expected=4)
                self.assertEqual(caught.exception.fields, {"connection_id": [code]})
                self.assertEqual((store.row.revision, store.events), (4, []))

    def test_switching_to_another_connection_or_profile_is_an_explicit_save_with_no_fallback(self):
        store = FakeStore(row_with_connections(revision=1, mode="ai", provider="openai_compatible", profile="corp-small", policy="company_endpoint", timeout=30, connection="corp-llm"))
        update(store, selection(connection_id="ollama-local", profile_id="llama", policy_mode="local_only"), expected=1)
        self.assertEqual((store.row.connection_id, store.row.provider_id, store.row.profile_id, store.row.policy_mode), ("ollama-local", "ollama", "llama", "local_only"))

    def test_invalid_connections_are_refused_before_any_row_is_locked_and_nothing_is_stored(self):
        for bad in ([{**corp_connection(), "api_key": SECRET_A}], [corp_connection(provider_type="plugin")], [corp_connection(locality="local")], [corp_connection(endpoint="http://x.invalid")]):
            with self.subTest(str(bad)[:60]):
                store = FakeStore(stored_row(revision=1))
                with self.assertRaises(AISettingsValidationError) as caught:
                    update(store, {"extraction_mode": "deterministic", "connections": bad}, expected=1)
                self.assertFalse(store.locked)
                self.assertEqual((store.row.revision, store.row.connections, store.events), (1, [], []))
                self.assertNotIn(SECRET_A, repr(caught.exception.fields))

    def test_the_stale_revision_check_covers_the_connections(self):
        store = FakeStore(row_with_connections(revision=5))
        for expected in (0, 4, 6):
            with self.subTest(expected), self.assertRaises(AISettingsConflictError):
                update(store, {"extraction_mode": "deterministic", "connections": [corp_connection()]}, expected=expected)
        self.assertEqual((store.row.revision, store.row.connections, store.events), (5, all_connections(), []))

    def test_losing_the_race_to_create_the_row_is_a_conflict_and_stores_nothing(self):
        store = FakeStore(race=True)
        with self.assertRaises(AISettingsConflictError):
            update(store, {"extraction_mode": "deterministic", "connections": [corp_connection()]})
        self.assertEqual(store.events, [])

    def test_two_administrators_cannot_overwrite_each_other_unknowingly(self):
        store = FakeStore(row_with_connections(revision=1, connections=[corp_connection()]))
        update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(), ollama_connection()]}, expected=1)  # the first one wins
        with self.assertRaises(AISettingsConflictError):
            update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(display_name="Mine")]}, expected=1)  # the second one saw revision 1
        self.assertEqual([c["connection_id"] for c in store.row.connections], ["corp-llm", "ollama-local"])

    def test_a_non_administrator_is_refused_before_anything_is_validated_or_read(self):
        store = FakeStore(row_with_connections(revision=1))
        with self.assertRaises(AuthorizationError) as caught, self.assertLogs("apps.accounts.authz", "WARNING"):
            update(store, {"extraction_mode": "deterministic", "connections": [{"junk": 1}]}, expected=1, who=actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS))
        self.assertEqual(caught.exception.reason, "missing_permission")
        self.assertFalse(store.locked)
        self.assertEqual(store.events, [])


class ConnectionAuditTests(SimpleTestCase):
    def events(self, store, action):
        return [e for e in store.events if e.action == action]

    def test_a_first_save_records_one_settings_event_and_one_event_per_created_connection(self):
        store = FakeStore()
        update(store, {**selection(), "connections": [workbench_connection(), corp_connection(), cloud_connection()]})
        (updated,) = self.events(store, "ai_settings.updated")
        self.assertEqual(
            updated.details,
            {"revision": 1, "extraction_mode": "ai", "provider_id": "openai_compatible", "profile_id": "corp-small", "policy_mode": "company_endpoint", "timeout_seconds": 30,
             "connection_id": "corp-llm", "connection_count": 3, "connections_changed": True},
        )  # fmt: skip
        self.assertEqual(updated.subject_id, SETTINGS_SUBJECT_ID)
        created = self.events(store, "ai_settings.connection_changed")
        self.assertEqual([e.details["connection_id"] for e in created], ["workbench", "corp-llm", "vendor"])
        self.assertEqual({e.details["change"] for e in created}, {"created"})
        self.assertEqual(
            created[1].details,
            {"revision": 1, "change": "created", "connection_id": "corp-llm", "provider_type": "openai_compatible", "locality": "company_endpoint", "enabled": True,
             "has_credential_ref": True, "credential_source": "environment", "profile_count": 2},
        )  # fmt: skip
        self.assertEqual([e.subject_id for e in created], [connection_subject_id(c) for c in ("workbench", "corp-llm", "vendor")])
        self.assertTrue(all(e.actor_public_id == ADMIN_ID and e.subject_type == "ai_settings" for e in store.events))

    def test_a_change_and_a_removal_are_recorded_with_what_they_were(self):
        store = FakeStore(row_with_connections(revision=2, connections=[corp_connection(), cloud_connection(), ollama_connection()]))
        update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(enabled=False), ollama_connection()]}, expected=2)
        changes = {e.details["connection_id"]: e.details for e in self.events(store, "ai_settings.connection_changed")}
        self.assertEqual(set(changes), {"corp-llm", "vendor"})
        self.assertEqual((changes["corp-llm"]["change"], changes["corp-llm"]["enabled"]), ("updated", False))
        self.assertEqual((changes["vendor"]["change"], changes["vendor"]["locality"]), ("removed", "cloud"))
        self.assertEqual(self.events(store, "ai_settings.updated")[0].details["connection_count"], 2)

    def test_an_unchanged_list_records_no_connection_event(self):
        store = FakeStore(row_with_connections(revision=2, connections=[corp_connection()]))
        update(store, {"extraction_mode": "deterministic", "connections": [corp_connection()]}, expected=2)
        self.assertEqual(self.events(store, "ai_settings.connection_changed"), [])
        self.assertFalse(self.events(store, "ai_settings.updated")[0].details["connections_changed"])

    def test_a_save_without_connections_records_exactly_one_event(self):
        store = FakeStore(row_with_connections(revision=1))
        update(store, selection(), expected=1)
        (event,) = store.events
        self.assertEqual((event.action, event.details["connection_count"], event.details["connections_changed"]), ("ai_settings.updated", 5, False))

    def test_the_events_are_recorded_inside_the_transaction_and_roll_back_with_it(self):
        store = FakeStore(row_with_connections(revision=1, connections=[corp_connection()]), audit_error=RuntimeError(SECRET_A))
        with self.assertRaises(RuntimeError):
            update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(), ollama_connection()]}, expected=1)
        self.assertTrue(store.rolled_back)
        self.assertEqual((store.row.revision, [c["connection_id"] for c in store.row.connections], store.events), (1, ["corp-llm"], []))

    def test_a_failing_second_event_rolls_back_the_first_and_the_row(self):
        store = FakeStore(row_with_connections(revision=1, connections=[]))
        real = store.record_event
        calls = []

        def second_fails(event):
            calls.append(event.action)
            if len(calls) == 2:
                raise RuntimeError("audit down")
            real(event)

        with mock.patch.object(store, "record_event", second_fails), self.assertRaises(RuntimeError):
            update(store, {"extraction_mode": "deterministic", "connections": [corp_connection()]}, expected=1)
        self.assertEqual((store.rolled_back, store.row.connections, store.row.revision, store.events), (True, [], 1, []))

    def test_no_event_carries_an_endpoint_a_reference_a_name_or_a_secret(self):
        store = FakeStore()
        update(store, {**selection(), "connections": all_connections()})
        text = json.dumps([e.details for e in store.events])
        for forbidden in (CORP_ENDPOINT, "llm.corp", REF_CORP, "AP_AI_CRED", "Company model server", "KPMG Workbench", *SENTINELS):
            self.assertNotIn(forbidden, text)
        for event in store.events:
            for value in event.details.values():
                self.assertIsInstance(value, (str, int, bool, type(None)))

    def test_the_event_builder_refuses_free_text_in_the_connection_event(self):
        args = (ADMIN_ID, audit_events.ACTION_AI_SETTINGS_CONNECTION_CHANGED, audit_events.SUBJECT_AI_SETTINGS, connection_subject_id("x"))
        good = dict(revision=1, change="created", connection_id="x", provider_type="ollama", locality="local", enabled=True, has_credential_ref=False, profile_count=1)
        audit_events.user_event(*args, **good)
        for bad in ({"endpoint": "http://x"}, {"credential_ref": REF_CORP}, {"display_name": "A name"}, {"connection_id": "has space"}, {"change": {"x": 1}}):
            with self.subTest(bad), self.assertRaises(Exception):
                audit_events.user_event(*args, **{**good, **bad})

    def test_a_validation_error_conflict_or_refusal_records_nothing(self):
        for who, data, expected in (
            (actor(is_superuser=True), {"extraction_mode": "deterministic", "connections": [{"x": 1}]}, 1),
            (actor(is_superuser=True), {"extraction_mode": "deterministic", "connections": [corp_connection()]}, 9),
        ):
            store = FakeStore(row_with_connections(revision=1))
            with self.assertRaises((AISettingsValidationError, AISettingsConflictError)):
                update(store, data, expected=expected, who=who)
            self.assertEqual(store.events, [])


class ReadConnectionsTests(SimpleTestCase):
    def test_a_read_returns_the_connections_with_a_safe_status_and_the_catalog_of_types(self):
        view = read(FakeStore(row_with_connections(revision=2)))
        self.assertEqual([c.values.connection_id for c in view.connections], ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"])
        self.assertTrue(all(c.status.configured and c.status.issue is None for c in view.connections))
        self.assertEqual([t.provider_type for t in view.provider_types], ["kpmg_workbench", "openai_compatible", "ollama"])
        self.assertEqual(dict(view.locality_policies)["cloud"], ("approved_cloud",))

    def test_the_status_is_that_of_the_process_that_answers(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k not in (REF_CORP, "AP_AI_OLLAMA_ALLOWED_ORIGINS")}
        by_id = {c.values.connection_id: c.status for c in read(FakeStore(row_with_connections(revision=2)), env).connections}
        self.assertEqual((by_id["corp-llm"].configured, by_id["corp-llm"].issue), (False, "missing_api_key"))
        self.assertEqual((by_id["ollama-local"].configured, by_id["ollama-local"].issue), (False, "invalid_configuration"))
        self.assertTrue(by_id["vendor"].configured)

    def test_a_status_probe_that_raises_is_invalid_configuration_without_its_text(self):
        entry = CATALOG.provider_types[1]
        broken = type(entry)(**{**entry.__dict__, "status": mock.Mock(side_effect=RuntimeError(SECRET_A))})
        catalog = type(CATALOG)(**{**CATALOG.__dict__, "provider_types": (CATALOG.provider_types[0], broken, CATALOG.provider_types[2])})
        with mock.patch.dict(os.environ, OPERATOR_ENV), patched_services(FakeStore(row_with_connections(revision=2)), actor(is_superuser=True)), self.assertLogs(services.logger, "ERROR") as logs:
            view = services.get_extraction_settings(user=fake_user(), catalog=catalog)
        status = next(c.status for c in view.connections if c.values.connection_id == "corp-llm")
        self.assertEqual((status.configured, status.issue), (False, "invalid_configuration"))
        self.assertNotIn(SECRET_A, "\n".join(logs.output))

    def test_a_damaged_stored_item_does_not_break_the_read_and_is_counted(self):
        raw = [corp_connection(), {"junk": True}, "x", None]
        view = read(FakeStore(stored_row(revision=1, connections=raw)))
        self.assertEqual(([c.values.connection_id for c in view.connections], view.record.unreadable_connections), (["corp-llm"], 3))
        view = read(FakeStore(stored_row(revision=1, connections="not a list")))
        self.assertEqual((view.connections, view.record.unreadable_connections), ((), 1))

    def test_the_worker_read_returns_the_readable_connections_and_the_row_revision(self):
        with patched_services(FakeStore(row_with_connections(revision=7)), AssertionError("no actor lookup")):
            stored = services.load_stored_settings()
        self.assertEqual((stored.revision, [c.connection_id for c in stored.connections]), (7, ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"]))

    def test_a_pre_existing_row_has_no_connections_and_reads_as_the_legacy_provider(self):
        with patched_services(FakeStore(stored_row(revision=2, mode="ai", provider="kpmg_workbench", profile="workbench-gpt5", policy="company_endpoint", timeout=45)), AssertionError("no actor")):
            stored = services.load_stored_settings()
        self.assertEqual((stored.values.connection_id, stored.connections), (None, ()))


class ApiConnectionTests(ApiCase):
    def put(self, store, data, who=None, environ=None):
        return self.call("put", store, who or actor(is_superuser=True), data, environ={**OPERATOR_ENV, **(environ or {})})

    def body(self, **changes):
        body = {**selection(), "connections": all_connections(), "expected_revision": 0}
        body.update(changes)
        return body

    def test_put_saves_the_connections_and_returns_them_with_a_status_and_no_secret(self):
        store = FakeStore()
        response, text = self.put(store, self.body())
        self.assertEqual(response.status_code, 200, text)
        data = response.json()
        self.assertEqual((data["revision"], data["apply"], data["settings"]["connection_id"], data["settings"]["provider_id"]), (1, "worker_restart_required", "corp-llm", "openai_compatible"))
        corp = next(c for c in data["connections"] if c["connection_id"] == "corp-llm")
        self.assertEqual(corp, {**corp_connection(), "status": {"configured": True, "issue": None}})
        self.assertEqual(corp["credential_ref"], REF_CORP)  # the NAME of the variable, never its value
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_get_returns_the_connections_and_the_catalog_of_provider_types(self):
        response, text = self.call("get", FakeStore(row_with_connections(revision=2)), actor(is_superuser=True), environ=OPERATOR_ENV)
        data = response.json()
        self.assertEqual([c["connection_id"] for c in data["connections"]], ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"])
        catalog = data["catalog"]
        self.assertEqual(catalog["credential_ref_prefix"], "AP_AI_CRED_")
        self.assertEqual(catalog["locality_policies"], [
            {"locality": "local", "policy_modes": ["local_only", "company_endpoint", "approved_cloud"]},
            {"locality": "company_endpoint", "policy_modes": ["company_endpoint", "approved_cloud"]},
            {"locality": "cloud", "policy_modes": ["approved_cloud"]},
        ])  # fmt: skip
        types = {t["provider_type"]: t for t in catalog["provider_types"]}
        self.assertEqual(set(types), {"kpmg_workbench", "openai_compatible", "ollama"})
        self.assertEqual((types["ollama"]["localities"], types["ollama"]["credential"], types["ollama"]["needs_endpoint"]), (["local"], "none", True))
        self.assertEqual([p["profile_id"] for p in types["kpmg_workbench"]["profiles"]], ["workbench-gpt5"])
        self.assertEqual(types["openai_compatible"]["profile_source"], "declared")
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_a_credential_cannot_be_sent_in_a_connection_and_is_never_echoed(self):
        for name in ("api_key", "secret", "token", "password", "authorization", "proxy_url"):
            with self.subTest(name):
                store = FakeStore()
                response, text = self.put(store, self.body(connections=[{**corp_connection(), name: SECRET_A}]))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["details"], {"fields": {f"connections[0].{name}": ["unknown_field"]}})
                self.assertNotIn(SECRET_A, text)
                self.assertIsNone(store.row)

    def test_a_reference_outside_the_prefix_is_a_400_and_the_value_is_not_read(self):
        store = FakeStore()
        response, text = self.put(store, self.body(connections=[corp_connection(credential_ref="AP_SECRET_KEY")]), environ={"AP_SECRET_KEY": SECRET_A})
        self.assertEqual((response.status_code, response.json()["error"]["details"]), (400, {"fields": {"connections[0].credential_ref": ["invalid_value"]}}))
        self.assertNotIn(SECRET_A, text)

    def test_domain_refusals_are_400_with_indexed_field_paths(self):
        expectations = (
            (corp_connection(provider_type="plugin"), {"connections[0].provider_type": ["unknown_provider_type"]}),
            (corp_connection(locality="local"), {"connections[0].endpoint": ["endpoint_locality_mismatch"]}),
            (ollama_connection(locality="cloud"), {"connections[0].locality": ["locality_not_allowed_for_provider"]}),
            (ollama_connection(endpoint="http://10.1.1.1:11434"), {"connections[0].endpoint": ["endpoint_not_loopback"]}),
            (corp_connection(endpoint="http://llm.invalid/v1"), {"connections[0].endpoint": ["plain_http_not_allowed"]}),
            (corp_connection(profiles=[{"profile_id": "p", "model_id": "has space"}]), {"connections[0].profiles[0].model_id": ["invalid_value"]}),
            (workbench_connection(profiles=[{"profile_id": "workbench-gpt41", "model_id": "gpt-4.1"}]), {"connections[0].profiles[0].profile_id": ["unknown_profile"]}),
        )
        for connection, fields in expectations:
            with self.subTest(fields):
                store = FakeStore()
                response, _ = self.put(store, {"extraction_mode": "deterministic", "connections": [connection], "expected_revision": 0})
                self.assertEqual((response.status_code, response.json()["error"]["details"]), (400, {"fields": fields}))
                self.assertIsNone(store.row)

    def test_a_locality_policy_mismatch_is_a_400(self):
        for body, fields in (
            (self.body(policy_mode="local_only"), {"policy_mode": ["policy_forbids_provider"]}),
            (self.body(connection_id="vendor", profile_id="vendor-chat", policy_mode="company_endpoint"), {"policy_mode": ["policy_forbids_provider"]}),
            (self.body(connection_id="nope"), {"connection_id": ["unknown_connection"]}),
        ):
            with self.subTest(fields):
                response, _ = self.put(FakeStore(), body)
                self.assertEqual((response.status_code, response.json()["error"]["details"]), (400, {"fields": fields}))

    def test_shape_errors_are_400_and_never_echo_a_value(self):
        sentinel = "SHAPE-SENTINEL-9a"
        for connections in ([{**corp_connection(), "enabled": "true"}], [{**corp_connection(), "profiles": "x"}], [{**corp_connection(), "display_name": 5}], "x", [sentinel], [{**corp_connection(), "endpoint": [sentinel]}]):
            with self.subTest(str(connections)[:50]), mock.patch.object(services, "update_extraction_settings") as service:
                response, text = self.put(FakeStore(), self.body(connections=connections))
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "validation_error"))
                self.assertNotIn(sentinel, text)
                service.assert_not_called()

    def test_a_stale_revision_is_a_409_and_changes_nothing(self):
        store = FakeStore(row_with_connections(revision=5))
        response, _ = self.put(store, self.body(expected_revision=4))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "stale_settings_revision"))
        self.assertEqual((store.row.revision, store.row.connections), (5, all_connections()))

    def test_a_non_administrator_is_refused_and_sees_no_connection(self):
        store = FakeStore(row_with_connections(revision=2))
        for method, data in (("get", None), ("put", self.body(expected_revision=2))):
            with self.subTest(method), self.assertLogs("apps.accounts.authz", "WARNING"):
                response, text = self.call(method, store, actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS), data)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "permission_denied"))
            self.assertNotIn("corp-llm", text)
        self.assertEqual(store.row.revision, 2)

    def test_the_legacy_body_without_connections_still_works_and_keeps_the_stored_list(self):
        store = FakeStore(row_with_connections(revision=1))
        body = {"extraction_mode": "ai", "provider_id": "kpmg_workbench", "profile_id": "workbench-gpt5", "policy_mode": "company_endpoint", "timeout_seconds": 45, "expected_revision": 1}
        response, _ = self.put(store, body)
        self.assertEqual((response.status_code, response.json()["settings"]["connection_id"], len(response.json()["connections"])), (200, None, 5))
        self.assertEqual(store.row.connections, all_connections())

    def test_no_response_log_or_audit_event_holds_a_secret_for_any_call(self):
        store = FakeStore()
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("apps.test").debug("start")
            for method, data in (("put", self.body()), ("get", None), ("put", self.body(expected_revision=1, timeout_seconds=40)), ("put", self.body(expected_revision=99)), ("put", self.body(connections=[{**corp_connection(), "api_key": SECRET_A}]))):
                _, text = self.put(store, data) if method == "put" else self.call("get", store, actor(is_superuser=True), environ=OPERATOR_ENV)
                for sentinel in SENTINELS:
                    self.assertNotIn(sentinel, text)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, "\n".join(logs.output))
            self.assertNotIn(sentinel, json.dumps([e.details for e in store.events]))

    def test_there_is_no_fallback_field(self):
        for name in ("fallback_connection_id", "fallback_profile_id", "fallback", "auto", "default_connection_id"):
            with self.subTest(name):
                response, _ = self.put(FakeStore(), self.body(**{name: "x"}))
                self.assertEqual(response.json()["error"]["details"], {"fields": {name: ["unknown_field"]}})
        self.assertEqual(URL, "/api/ai-settings/extraction/")

    def test_the_id_namespace_of_a_connection_subject_is_stable(self):
        self.assertEqual(connection_subject_id("corp-llm"), uuid.uuid5(uuid.NAMESPACE_URL, "urn:automation-platform:ai-settings:connection:corp-llm"))
        self.assertNotEqual(connection_subject_id("a"), connection_subject_id("b"))
        self.assertNotEqual(connection_subject_id("extraction"), SETTINGS_SUBJECT_ID)
