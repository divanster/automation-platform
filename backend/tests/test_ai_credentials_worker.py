"""DB-free tests of how a MANAGED credential reaches the worker and the status probe (AI-SEC-2): the connection's explicit credential source, the one read at start-up, the absence of any
environment fallback, the unchanged provider, locality and policy rules, and the composition of the secret store from the backend environment.

The adapters, the gateway, the policy rule and Request Intake's output conversion are the REAL ones; the transport is a fake that records the request; the secret store is an in-memory
stand-in (and, on Windows only, the real hardened DPAPI store in a dedicated temporary directory). No database, no network, no real credential."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from django.test import SimpleTestCase

from apps.ai_gateway import AIGatewayError
from apps.ai_settings.rules import CREDENTIAL_SOURCE_MANAGED, ExtractionValues, parse_stored_connection, validate_connections
from apps.ai_settings.services import StoredSettings
from apps.intake.errors import ExtractionConfigurationError
from apps.secret_store import (
    CODE_BUSY,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    DisabledSecretStore,
    SecretStoreError,
)
from config import ai_connections
from config import secret_store as composition
from config.intake_extraction import build_catalog, build_extractor_source
from tests.ai_connections_support import (
    OPERATOR_ENV,
    REF_CLOUD,
    REF_CORP,
    REF_WORKBENCH,
    SECRET_A,
    SECRET_WORKBENCH,
    all_connections,
    cloud_connection,
    corp_connection,
    workbench_connection,
)
from tests.ai_credentials_support import ENV_SENTINEL, SECRET_ONE, SECRET_TWO, MemoryStore
from tests.intake_ai_support import VALID_OUTPUT, VALID_TEXT, FakeTransport, answer_with, module_gate

CATALOG = build_catalog()
IS_WINDOWS = sys.platform == "win32"


def managed(**changes):
    return corp_connection(credential_source="managed", **changes)


def stored(connections, connection_id="corp-llm", profile="corp-small", policy="company_endpoint"):
    typed = validate_connections(connections, CATALOG)
    provider = next(c.provider_type for c in typed if c.connection_id == connection_id)
    return StoredSettings(ExtractionValues("ai", provider, profile, policy, 30, connection_id), 5, typed)


def build(row, environ=None, store=None, transport=None):
    return build_extractor_source(
        OPERATOR_ENV if environ is None else environ, module_gate=module_gate(), lease_seconds=60,
        transport=FakeTransport() if transport is None else transport, stored=row, secret_store=store,
    )  # fmt: skip


def refusal(row, environ=None, store=None):
    with SimpleTestCase().assertRaises(ExtractionConfigurationError) as caught:
        build(row, environ, store)
    return str(caught.exception)


class UntouchableStore(MemoryStore):
    """A store that fails the test if anything at all is asked of it."""

    def _get(self, reference):
        raise AssertionError("the secret store was read")

    _put = _delete = _exists = _get


class RulesTests(SimpleTestCase):
    def test_the_source_is_environment_unless_the_connection_says_managed(self):
        (connection,) = validate_connections([corp_connection()], CATALOG)
        self.assertEqual(connection.credential_source, "environment")
        legacy = {key: value for key, value in corp_connection().items() if key != "credential_source"}  # a row stored before the field existed
        self.assertEqual(parse_stored_connection(legacy).credential_source, "environment")
        (explicit,) = validate_connections([managed()], CATALOG)
        self.assertEqual(explicit.credential_source, CREDENTIAL_SOURCE_MANAGED)
        self.assertEqual(explicit.as_mapping()["credential_source"], "managed")

    def test_an_unknown_source_is_refused_with_a_code(self):
        from apps.ai_settings.errors import AISettingsValidationError

        for bad in ("vault", "", "MANAGED", None, 5, ["managed"]):
            with self.subTest(bad=repr(bad)), self.assertRaises(AISettingsValidationError) as caught:
                validate_connections([corp_connection(credential_source=bad)], CATALOG)
            self.assertEqual(caught.exception.fields, {"connections[0].credential_source": ["invalid_value"]})

    def test_a_managed_connection_needs_a_reference_and_a_provider_that_takes_one(self):
        from apps.ai_settings.errors import AISettingsValidationError
        from tests.ai_connections_support import ollama_connection

        with self.assertRaises(AISettingsValidationError) as caught:
            validate_connections([managed(credential_ref=None)], CATALOG)
        self.assertIn("required", caught.exception.fields["connections[0].credential_ref"])
        with self.assertRaises(AISettingsValidationError) as caught:
            validate_connections([ollama_connection(credential_source="managed")], CATALOG)
        self.assertEqual(caught.exception.fields["connections[0].credential_source"], ["not_allowed"])

    def test_the_reference_of_a_managed_connection_has_the_same_shape_as_any_other(self):
        from apps.ai_settings.errors import AISettingsValidationError

        for bad in ("AP_SECRET_KEY", "ap_ai_cred_x", "DJANGO_SECRET_KEY"):
            with self.assertRaises(AISettingsValidationError):
                validate_connections([managed(credential_ref=bad)], CATALOG)


class SettingsSourceTests(SimpleTestCase):
    """How the AI settings service treats ``credential_source`` (the settings rows are the in-memory stand-in of ``tests/ai_settings_support.py``)."""

    def update(self, store, data, expected):
        from apps.ai_settings import services as settings_services
        from tests.ai_settings_support import actor, patched_services

        with patched_services(store, actor(is_superuser=True)):
            return settings_services.update_extraction_settings(user=object(), data=data, expected_revision=expected, catalog=CATALOG, lease_seconds=60)

    def row(self, **connection_changes):
        from tests.ai_settings_support import FakeStore, stored_row

        return FakeStore(stored_row(revision=2, connections=[corp_connection(**connection_changes)]))

    def test_an_item_that_omits_the_source_keeps_the_stored_managed_source(self):
        store = self.row(credential_source="managed")
        omitted = {key: value for key, value in corp_connection().items() if key != "credential_source"}
        self.update(store, {"extraction_mode": "deterministic", "connections": [omitted]}, 2)
        self.assertEqual(store.row.connections[0]["credential_source"], "managed")

    def test_switching_back_to_the_environment_is_explicit(self):
        store = self.row(credential_source="managed")
        self.update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="environment")]}, 2)
        self.assertEqual(store.row.connections[0]["credential_source"], "environment")

    def test_a_new_connection_that_omits_the_source_is_an_environment_connection(self):
        store = self.row(credential_source="managed")
        omitted = {key: value for key, value in corp_connection(connection_id="second").items() if key != "credential_source"}
        self.update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="managed"), omitted]}, 2)
        self.assertEqual([c["credential_source"] for c in store.row.connections], ["managed", "environment"])

    def test_the_connection_event_records_the_source_and_no_reference(self):
        store = self.row(credential_source="environment")
        self.update(store, {"extraction_mode": "deterministic", "connections": [corp_connection(credential_source="managed")]}, 2)
        (event,) = [e for e in store.events if e.action == "ai_settings.connection_changed"]
        self.assertEqual((event.details["change"], event.details["credential_source"], event.details["has_credential_ref"]), ("updated", "managed", True))
        self.assertNotIn(REF_CORP, repr(event))

    def test_the_users_of_a_reference_are_the_managed_connections_that_name_it(self):
        from apps.ai_settings import services as settings_services
        from tests.ai_settings_support import FakeStore, stored_row, patched_services, actor

        store = FakeStore(stored_row(connections=[
            corp_connection(credential_source="managed"), corp_connection(connection_id="env-copy"), corp_connection(connection_id="other", credential_ref="AP_AI_CRED_OTHER", credential_source="managed"),
        ]))  # fmt: skip
        with patched_services(store, actor(is_superuser=True)):
            self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP), ("corp-llm",))
            self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP, lock=True), ("corp-llm",))
            self.assertTrue(store.locked)
            self.assertEqual(settings_services.connections_using_managed_reference("AP_AI_CRED_NONE"), ())
        with patched_services(FakeStore(), actor(is_superuser=True)):
            self.assertEqual(settings_services.connections_using_managed_reference(REF_CORP), ())


class ManagedCredentialReachesTheWorkerTests(SimpleTestCase):
    def test_the_key_comes_from_the_secret_store_and_never_from_the_environment_variable_of_the_same_name(self):
        store = MemoryStore().seed(REF_CORP, SECRET_TWO)
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        source = build(stored([managed()]), store=store, transport=transport)  # the environment holds REF_CORP=SECRET_A, the store SECRET_TWO
        source().extract(VALID_TEXT)
        (request,) = transport.requests
        self.assertIn(SECRET_TWO, repr(request.headers))
        self.assertNotIn(SECRET_A, repr(request.headers))
        self.assertEqual(os.environ.get(REF_CORP), None)

    def test_the_workbench_key_of_a_managed_connection_replaces_the_environment_key(self):
        store = MemoryStore().seed(REF_WORKBENCH, SECRET_TWO)
        row = stored([workbench_connection(credential_ref=REF_WORKBENCH, credential_source="managed")], "workbench", "workbench-gpt5")
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        build(row, store=store, transport=transport)().extract(VALID_TEXT)
        (request,) = transport.requests
        self.assertIn(SECRET_TWO, repr(request.headers))
        self.assertNotIn(SECRET_WORKBENCH, repr(request.headers))  # AP_AI_WORKBENCH_API_KEY is in the environment and is not used

    def test_an_environment_connection_never_asks_the_secret_store(self):
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        source = build(stored(all_connections()), store=UntouchableStore(), transport=transport)
        source().extract(VALID_TEXT)
        (request,) = transport.requests
        self.assertIn(SECRET_A, repr(request.headers))

    def test_the_credential_is_read_once_at_start_up_and_a_later_change_needs_a_restart(self):
        store = MemoryStore().seed(REF_CORP, SECRET_ONE)
        first, second = FakeTransport(answer_with(VALID_OUTPUT)), FakeTransport(answer_with(VALID_OUTPUT))
        source = build(stored([managed()]), store=store, transport=first)
        reads = [call for call in store.calls if call[0] == "get"]
        self.assertEqual(len(reads), 1)
        store.items[REF_CORP] = store.items[REF_CORP].__class__(SECRET_TWO)  # an administrator replaces the credential
        source().extract(VALID_TEXT)
        self.assertEqual(len([call for call in store.calls if call[0] == "get"]), 1)
        self.assertIn(SECRET_ONE, repr(first.requests[0].headers))
        self.assertNotIn(SECRET_TWO, repr(first.requests[0].headers))
        build(stored([managed()]), store=store, transport=second)().extract(VALID_TEXT)  # a restarted worker reads the new one
        self.assertIn(SECRET_TWO, repr(second.requests[0].headers))

    def test_the_provider_locality_and_policy_rules_are_unchanged_for_a_managed_credential(self):
        store = MemoryStore().seed(REF_CORP, SECRET_ONE).seed(REF_CLOUD, SECRET_TWO)
        self.assertIn("policy_forbids_provider", refusal(stored([managed()], policy="local_only"), store=store))
        cloud = cloud_connection(credential_source="managed")
        self.assertIn("policy_forbids_provider", refusal(stored([cloud], "vendor", "vendor-chat", "company_endpoint"), store=store))
        unapproved = managed(endpoint="https://not-approved.example.invalid/v1")
        self.assertIn("endpoint_not_approved", refusal(stored([unapproved]), store=store))


class NoFallbackTests(SimpleTestCase):
    def test_a_missing_managed_credential_stops_the_worker_even_though_the_environment_has_one(self):
        message = refusal(stored([managed()]), {**OPERATOR_ENV, "AP_AI_OPENAI_API_KEY": ENV_SENTINEL, REF_CORP: SECRET_A}, MemoryStore())
        self.assertIn("missing_credential_reference", message)
        for secret in (SECRET_A, ENV_SENTINEL, REF_CORP):
            self.assertNotIn(secret, message)

    def test_every_secret_store_failure_stops_the_worker_with_a_fixed_reason(self):
        for store in (
            DisabledSecretStore(),
            MemoryStore(fail={"get": CODE_UNAVAILABLE}).seed(REF_CORP, SECRET_ONE),
            MemoryStore(fail={"get": CODE_BUSY}).seed(REF_CORP, SECRET_ONE),
            MemoryStore(fail={"get": CODE_UNREADABLE}).seed(REF_CORP, SECRET_ONE),
        ):
            with self.subTest(store=type(store).__name__, fail=getattr(store, "fail", None)):
                message = refusal(stored([managed()]), {**OPERATOR_ENV, "AP_AI_OPENAI_API_KEY": ENV_SENTINEL}, store)
                self.assertIn("credential_store_unavailable", message)
                for secret in (SECRET_A, SECRET_ONE, ENV_SENTINEL, REF_CORP):
                    self.assertNotIn(secret, message)

    def test_without_an_injected_store_the_one_the_environment_configures_is_used_and_by_default_it_is_disabled(self):
        message = refusal(stored([managed()]), {**OPERATOR_ENV, REF_CORP: SECRET_A})
        self.assertIn("credential_store_unavailable", message)  # AP_SECRET_STORE_BACKEND is unset: disabled, and the environment value is not used instead
        self.assertNotIn(SECRET_A, message)

    def test_a_misconfigured_store_variable_stops_the_worker_and_names_nothing(self):
        for environ in ({"AP_SECRET_STORE_BACKEND": "vault"}, {"AP_SECRET_STORE_BACKEND": "windows_dpapi"}, {"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": "relative"}):
            with self.subTest(environ=environ):
                message = refusal(stored([managed()]), {**OPERATOR_ENV, **environ})
                self.assertIn("credential_store_unavailable", message)
                self.assertNotIn("relative", message)

    def test_an_unreferenced_managed_connection_is_refused_not_given_the_providers_environment_key(self):
        connection = validate_connections([managed()], CATALOG)[0]
        broken = connection.__class__(**{**connection.__dict__, "credential_ref": None})  # impossible through the rules; the composition still refuses it
        with self.assertRaises(AIGatewayError) as caught:
            ai_connections.connection_environ(broken, "m", OPERATOR_ENV, secret_store=MemoryStore())
        self.assertEqual(caught.exception.reason, "missing_credential_reference")

    def test_the_process_environment_is_never_modified(self):
        before = dict(os.environ)
        build(stored([managed()]), store=MemoryStore().seed(REF_CORP, SECRET_ONE))
        self.assertEqual(dict(os.environ), before)


class StatusProbeTests(SimpleTestCase):
    def status(self, connection, store, environ=None):
        (typed,) = validate_connections([connection], CATALOG)
        return ai_connections.connection_status(typed, OPERATOR_ENV if environ is None else environ, store)

    def test_a_stored_managed_credential_is_configured(self):
        status = self.status(managed(), MemoryStore().seed(REF_CORP, SECRET_ONE))
        self.assertEqual((status.configured, status.issue), (True, None))

    def test_a_missing_one_is_missing_api_key_whatever_the_environment_holds(self):
        status = self.status(managed(), MemoryStore(), {**OPERATOR_ENV, REF_CORP: SECRET_A})
        self.assertEqual((status.configured, status.issue), (False, "missing_api_key"))

    def test_an_unusable_store_is_an_invalid_configuration_with_no_other_detail(self):
        for store in (DisabledSecretStore(), MemoryStore(fail={"get": CODE_UNREADABLE}).seed(REF_CORP, SECRET_ONE)):
            status = self.status(managed(), store)
            self.assertEqual((status.configured, status.issue), (False, "invalid_configuration"))

    def test_an_environment_connection_is_judged_by_the_environment_alone(self):
        status = self.status(corp_connection(), UntouchableStore())
        self.assertEqual((status.configured, status.issue), (True, None))
        status = self.status(corp_connection(), UntouchableStore(), {k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP})
        self.assertEqual((status.configured, status.issue), (False, "missing_api_key"))

    def test_the_status_never_carries_a_secret(self):
        status = self.status(managed(), MemoryStore().seed(REF_CORP, SECRET_ONE))
        self.assertNotIn(SECRET_ONE, repr(status))


class SecretStoreCompositionTests(SimpleTestCase):
    def test_the_default_and_the_explicit_disabled_backend_are_the_disabled_store(self):
        for environ in ({}, {"AP_SECRET_STORE_BACKEND": ""}, {"AP_SECRET_STORE_BACKEND": "disabled"}, {"AP_SECRET_STORE_BACKEND": " disabled "}):
            store = composition.build_secret_store(environ)
            self.assertIsInstance(store, DisabledSecretStore)
            with self.assertRaises(SecretStoreError) as caught:
                store.get(REF_CORP)
            self.assertEqual(caught.exception.code, "secret_store_disabled")

    def test_an_unknown_backend_or_a_missing_directory_is_unavailable_with_a_fixed_message(self):
        for environ in (
            {"AP_SECRET_STORE_BACKEND": "vault"},
            {"AP_SECRET_STORE_BACKEND": "windows_dpapi"},
            {"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": ""},
            {"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": "relative\\path"},
        ):
            with self.subTest(environ=environ), self.assertRaises(SecretStoreError) as caught:
                composition.build_secret_store(environ)
            self.assertEqual(caught.exception.code, CODE_UNAVAILABLE)
            self.assertNotIn("relative", str(caught.exception) + repr(caught.exception))

    def test_it_reads_the_given_environment_only_and_never_the_process_environment_for_a_value(self):
        with mock.patch.dict(os.environ, {"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": "C:\\nowhere"}):
            self.assertIsInstance(composition.build_secret_store({}), DisabledSecretStore)

    @unittest.skipUnless(IS_WINDOWS, "the DPAPI store is Windows only")
    def test_malformed_optional_variables_fail_closed(self):
        base = {"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": os.path.join(tempfile.gettempdir(), "ap-secret-composition-never-created")}
        for extra in ({"AP_SECRET_STORE_TIMEOUT_SECONDS": "soon"}, {"AP_SECRET_STORE_TIMEOUT_SECONDS": "0"}, {"AP_SECRET_STORE_APPROVED_IDENTITY_SIDS": "SY"}, {"AP_SECRET_STORE_EXPECTED_IDENTITY_SID": "nobody"}):
            with self.subTest(extra=extra), self.assertRaises(SecretStoreError) as caught:
                composition.build_secret_store({**base, **extra})
            self.assertEqual(caught.exception.code, CODE_UNAVAILABLE)

    @unittest.skipUnless(IS_WINDOWS, "the DPAPI store is Windows only")
    def test_the_dpapi_backend_is_built_from_the_environment_and_names_itself(self):
        base = os.path.join(tempfile.gettempdir(), "ap-secret-composition-never-created")
        store = composition.build_secret_store({"AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": base})
        self.assertEqual(store.backend_id, "windows_dpapi")
        self.assertFalse(os.path.exists(base), "building a store creates nothing")


@unittest.skipUnless(IS_WINDOWS, "the DPAPI store is Windows only")
class RealDpapiStoreTests(SimpleTestCase):
    """The hardened Windows store, in a dedicated temporary directory with sentinel secrets only: the credential services write it, the worker composition reads it from the environment."""

    def setUp(self):
        from config.secret_store_dpapi import prepare_windows_secret_directory

        base = tempfile.mkdtemp(prefix="ap-secret-managed-")
        self.addCleanup(shutil.rmtree, base, True)
        self.directory = os.path.join(base, "store")
        prepare_windows_secret_directory(self.directory)
        self.environ = {**OPERATOR_ENV, "AP_SECRET_STORE_BACKEND": "windows_dpapi", "AP_SECRET_STORE_DIRECTORY": self.directory}

    def test_a_credential_written_through_the_services_is_the_key_the_worker_sends(self):
        from apps.ai_credentials import services
        from tests.ai_credentials_support import FakeStore, actor, patched_credentials

        db = FakeStore()
        with patched_credentials(db, actor(is_superuser=True)):
            services.create_credential(user="u", reference=REF_CORP, value=SECRET_ONE, store_factory=lambda: composition.build_secret_store(self.environ))
            status = services.get_status(user="u", reference=REF_CORP, store_factory=lambda: composition.build_secret_store(self.environ))
        self.assertEqual((status.status, status.store), ("set", "windows_dpapi"))
        self.assertEqual([e.details for e in db.events], [{"store": "windows_dpapi"}])
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        build(stored([managed()]), self.environ, transport=transport)().extract(VALID_TEXT)  # no secret_store argument: the worker builds its own from the environment
        self.assertIn(SECRET_ONE, repr(transport.requests[0].headers))
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    self.assertNotIn(SECRET_ONE.encode(), handle.read())

    def test_a_worker_with_nothing_stored_a_pinned_other_identity_or_an_unprepared_directory_stops(self):
        self.assertIn("missing_credential_reference", refusal(stored([managed()]), self.environ))
        from config.secret_store_dpapi import WindowsDpapiSecretStore

        WindowsDpapiSecretStore(self.directory).put(REF_CORP, SECRET_ONE)
        pinned_elsewhere = {**self.environ, "AP_SECRET_STORE_EXPECTED_IDENTITY_SID": "S-1-5-21-1-2-3-1999"}
        self.assertIn("credential_store_unavailable", refusal(stored([managed()]), pinned_elsewhere))
        unprepared = {**self.environ, "AP_SECRET_STORE_DIRECTORY": os.path.join(os.path.dirname(self.directory), "other")}
        os.mkdir(unprepared["AP_SECRET_STORE_DIRECTORY"])
        self.assertIn("credential_store_unavailable", refusal(stored([managed()]), unprepared))

    def test_the_identity_pin_accepts_exactly_the_current_identity(self):
        from config.secret_store_dpapi import WindowsDirectoryPolicy, WindowsDpapiSecretStore

        sid = WindowsDirectoryPolicy().current_user_sid()
        WindowsDpapiSecretStore(self.directory).put(REF_CORP, SECRET_ONE)
        pinned = {**self.environ, "AP_SECRET_STORE_EXPECTED_IDENTITY_SID": sid}
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        build(stored([managed()]), pinned, transport=transport)().extract(VALID_TEXT)
        self.assertIn(SECRET_ONE, repr(transport.requests[0].headers))
