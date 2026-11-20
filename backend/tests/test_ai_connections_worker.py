"""DB-free tests of the worker side of AI-SET-2: how the stored selection of a named connection becomes the one extractor a worker uses, and that every missing, disabled, invalid or
unapproved configuration stops the worker at start-up instead of falling back. The adapters, the gateway, the policy rule and Request Intake's output conversion are the REAL ones; the
transport is a fake that records the request. No database, no network, no real credential."""

import dataclasses
import json
from unittest import mock

from django.test import SimpleTestCase

from apps.ai_settings.rules import ExtractionValues, validate_connections
from apps.ai_settings.services import StoredSettings
from apps.intake import handlers as intake_handlers
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import AIExtractionError, ExtractionConfigurationError
from config import ai_workbench, task_registry
from config.intake_extraction import build_catalog, build_extractor_source
from tests import ai_ollama_support
from tests.ai_connections_support import (
    OPERATOR_ENV,
    REF_CORP,
    SECRET_A,
    SECRET_B,
    SECRET_WORKBENCH,
    all_connections,
    cloud_connection,
    corp_connection,
    local_openai_connection,
    ollama_connection,
    workbench_connection,
)
from tests.intake_ai_support import VALID_OUTPUT, VALID_TEXT, FakeTransport, ai_environ, answer_with, module_gate, status

CATALOG = build_catalog()


def stored(connection_id="corp-llm", profile="corp-small", policy="company_endpoint", timeout=30, revision=5, connections=None, mode="ai"):
    typed = validate_connections(all_connections() if connections is None else connections, CATALOG)
    provider = next((c.provider_type for c in typed if c.connection_id == connection_id), None)
    if mode == "deterministic":
        return StoredSettings(ExtractionValues("deterministic"), revision, typed)
    return StoredSettings(ExtractionValues(mode, provider, profile, policy, timeout, connection_id), revision, typed)


def build(row, environ=None, lease=60, transport=None, gate=True):
    return build_extractor_source(
        OPERATOR_ENV if environ is None else environ, module_gate=module_gate() if gate is True else gate, lease_seconds=lease,
        transport=FakeTransport() if transport is None else transport, stored=row,
    )  # fmt: skip


class ConnectionSelectionTests(SimpleTestCase):
    def test_the_selected_connection_and_profile_build_the_ai_extractor(self):
        source = build(stored())
        self.assertIsInstance(source.extractor, AIRequestExtractor)
        self.assertEqual(source.extractor.profile_id, "corp-small")
        self.assertEqual(source.extractor._policy.mode.value, "company_endpoint")
        self.assertEqual((source.settings.stored_revision, source.settings.connection.connection_id), (5, "corp-llm"))
        self.assertEqual(source.effective_timeout_seconds, 30.0)

    def test_only_the_selected_connections_endpoint_model_and_key_are_used_for_extraction(self):
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        source = build(stored(profile="corp-large"), transport=transport)
        result = source().extract(VALID_TEXT)
        self.assertEqual(type(result).__name__, "ExtractionResult")
        (request,) = transport.requests
        self.assertEqual(request.url, "https://llm.corp.example.invalid:443/v1/chat/completions")
        self.assertEqual(json.loads(request.body)["model"], "corp-model-70b")
        self.assertIn(SECRET_A, repr(request.headers))
        for other in (SECRET_B, SECRET_WORKBENCH):
            self.assertNotIn(other, repr(request.headers))

    def test_each_provider_type_is_selectable_and_reaches_its_own_endpoint(self):
        cases = (
            ("corp-llm", "corp-small", "company_endpoint", answer_with(VALID_OUTPUT), "https://llm.corp.example.invalid:443/v1/chat/completions"),
            ("vendor", "vendor-chat", "approved_cloud", answer_with(VALID_OUTPUT), "https://api.vendor.example.invalid:443/v1/chat/completions"),
            ("local-server", "local-chat", "local_only", answer_with(VALID_OUTPUT), "http://127.0.0.1:8000/v1/chat/completions"),
            ("ollama-local", "llama", "local_only", ai_ollama_support.answer(json.dumps(VALID_OUTPUT)), "http://127.0.0.1:11434/api/chat"),
            ("workbench", "workbench-gpt5", "company_endpoint", answer_with(VALID_OUTPUT), "https://api.workbench.kpmg/genai/azure/openai/deployments/"),
        )
        for connection_id, profile, policy, reply, url in cases:
            with self.subTest(connection_id):
                transport = FakeTransport(reply)
                source = build(stored(connection_id, profile, policy), transport=transport)
                source().extract(VALID_TEXT)
                self.assertTrue(transport.last.url.startswith(url), transport.last.url)

    def test_the_description_names_the_connection_the_profile_and_no_secret_or_endpoint(self):
        line = build(stored(revision=9)).describe()
        self.assertIn("connection corp-llm", line)
        self.assertIn("profile corp-small", line)
        self.assertIn("revision 9", line)
        for text in (SECRET_A, "llm.corp", REF_CORP):
            self.assertNotIn(text, line)

    def test_an_unrelated_invalid_connection_does_not_stop_the_worker(self):
        row = stored()
        retired = dataclasses.replace(row.connections[1], connection_id="retired", provider_type="retired_provider")
        mixed = StoredSettings(row.values, row.revision, (retired, *row.connections))
        self.assertEqual(build(mixed).settings.connection.connection_id, "corp-llm")  # only the SELECTED connection is examined


class FailClosedTests(SimpleTestCase):
    """Every refusal is an ``ExtractionConfigurationError`` (the worker does not start). None of them falls back to the environment's Workbench, to another connection, to
    another model or to the deterministic parser, and none names a value."""

    def refuse(self, row, environ=None, **kwargs):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            build(row, environ, **kwargs)
        message = str(caught.exception)
        for text in (SECRET_A, SECRET_B, SECRET_WORKBENCH, "llm.corp", "example.invalid", REF_CORP, "127.0.0.1"):
            self.assertNotIn(text, message)
        return message

    def test_a_missing_connection_stops_the_worker_even_with_a_valid_workbench_environment(self):
        message = self.refuse(stored("gone"), {**OPERATOR_ENV, **ai_environ()})
        self.assertIn("connection_id: unknown_connection", message)

    def test_a_disabled_connection_stops_the_worker(self):
        row = stored(connections=[workbench_connection(), corp_connection(enabled=False)])
        self.assertIn("connection_id: connection_disabled", self.refuse(row, {**OPERATOR_ENV, **ai_environ()}))

    def test_a_profile_the_connection_does_not_have_stops_the_worker(self):
        self.assertIn("profile_id: unknown_profile", self.refuse(stored(profile="vendor-chat")))

    def test_the_data_policy_still_applies_to_a_stored_connection(self):
        self.assertIn("policy_forbids_provider", self.refuse(stored(policy="local_only")))
        self.assertIn("policy_forbids_provider", self.refuse(stored("vendor", "vendor-chat", "company_endpoint")))

    def test_an_unset_credential_variable_stops_the_worker(self):
        env = {k: v for k, v in OPERATOR_ENV.items() if k != REF_CORP}
        message = self.refuse(stored(), env)
        self.assertIn("missing_credential_reference", message)

    def test_an_endpoint_the_operator_has_not_approved_stops_the_worker(self):
        message = self.refuse(stored(), {**OPERATOR_ENV, "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://other.example.invalid"})
        self.assertIn("endpoint_not_approved", message)

    def test_a_selected_connection_that_no_longer_passes_the_catalog_stops_the_worker(self):
        row = stored()
        retired = dataclasses.replace(next(c for c in row.connections if c.connection_id == "corp-llm"), provider_type="retired_provider")
        others = tuple(c for c in row.connections if c.connection_id != "corp-llm")
        self.assertIn("invalid_connection", self.refuse(StoredSettings(row.values, row.revision, (retired, *others))))

    def test_a_workbench_profile_that_is_no_longer_approved_stops_the_worker(self):
        row = stored("workbench", "workbench-gpt5")
        from config import intake_extraction

        with mock.patch.object(intake_extraction, "APPROVED_INTAKE_AI_PROFILES", frozenset()):
            self.assertIn("invalid_connection", self.refuse(row))

    def test_the_lease_must_still_cover_the_deadline(self):
        with self.assertRaises(ExtractionConfigurationError):
            build(stored(timeout=46), lease=60)

    def test_ai_mode_still_needs_the_module_gate(self):
        with self.assertRaises(ExtractionConfigurationError):
            build(stored(), gate=None)

    def test_an_ollama_connection_whose_operator_origin_is_not_loopback_stops_the_worker(self):
        self.refuse(stored("ollama-local", "llama", "local_only"), {**OPERATOR_ENV, "AP_AI_OLLAMA_ALLOWED_ORIGINS": "http://10.0.0.5:11434"})

    def test_a_row_that_is_deterministic_builds_no_gateway_and_reads_no_credential(self):
        with mock.patch("config.ai_connections.build_connection_gateway") as builder:
            source = build(stored(mode="deterministic"), {})
        builder.assert_not_called()
        self.assertIsNone(source())

    def test_there_is_no_fallback_to_the_environment_provider_or_to_another_connection(self):
        row = stored("gone")
        for environ in ({}, ai_environ(), {**OPERATOR_ENV, **ai_environ()}):
            with self.subTest(list(environ)[:1]), self.assertRaises(ExtractionConfigurationError):
                build(row, environ)


class LegacyBehaviourTests(SimpleTestCase):
    def test_a_row_without_a_connection_is_the_environment_configured_workbench_exactly_as_before(self):
        row = StoredSettings(ExtractionValues("ai", "kpmg_workbench", ai_workbench.PROFILE_GPT5, "company_endpoint", 30), 3, validate_connections(all_connections(), CATALOG))
        source = build(row, {"AP_AI_WORKBENCH_API_KEY": SECRET_WORKBENCH})
        self.assertIsNone(source.settings.connection)
        self.assertEqual(source.extractor.profile_id, ai_workbench.PROFILE_GPT5)
        self.assertEqual(source.describe(), "Request Intake extraction mode: ai (profile workbench-gpt5, data policy company_endpoint, call deadline 30s). Source: stored settings, revision 3.")

    def test_a_row_stored_by_the_previous_version_still_loads(self):
        old = StoredSettings(ExtractionValues("ai", "kpmg_workbench", ai_workbench.PROFILE_GPT5, "company_endpoint", 45), 2)
        self.assertEqual(build(old, {"AP_AI_WORKBENCH_API_KEY": SECRET_WORKBENCH}).settings.stored_revision, 2)

    def test_with_no_row_the_environment_decides_as_before(self):
        source = build_extractor_source(ai_environ(), module_gate=module_gate(), lease_seconds=60, transport=FakeTransport(), stored=None)
        self.assertIsNone(source.settings.connection)
        self.assertEqual(source.extractor.profile_id, ai_workbench.PROFILE_GPT5)


class ModuleGateAndFailureTests(SimpleTestCase):
    def test_a_disabled_module_stops_the_call_before_any_request_leaves(self):
        transport = FakeTransport(answer_with(VALID_OUTPUT))
        source = build(stored(), transport=transport, gate=lambda: frozenset())
        with self.assertRaises(AIExtractionError) as caught:
            source().extract(VALID_TEXT)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("module_unavailable", True))
        self.assertEqual(transport.requests, [])

    def test_provider_failures_keep_their_retry_classification(self):
        for reply, code, retryable in ((status(503), "provider_unavailable", True), (status(429), "rate_limit", True), (status(401), "authentication_failure", False), (status(404), "unsupported_model", False)):
            with self.subTest(code):
                source = build(stored(), transport=FakeTransport(*[reply] * 4))
                with mock.patch("time.sleep"), self.assertRaises(AIExtractionError) as caught:
                    source().extract(VALID_TEXT)
                self.assertEqual((caught.exception.code, caught.exception.retryable), (code, retryable))
                for secret in (SECRET_A, VALID_TEXT):
                    self.assertNotIn(secret, str(caught.exception) + repr(caught.exception.reason))

    def test_an_invalid_answer_is_a_candidate_failure_never_an_approval(self):
        source = build(stored(), transport=FakeTransport(answer_with({"not": "the schema"})))
        with self.assertRaises(AIExtractionError) as caught:
            source().extract(VALID_TEXT)
        self.assertFalse(caught.exception.retryable)


class PrepareWorkerTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(None)

    def test_the_stored_connection_is_read_once_at_start_up_and_installed(self):
        row = stored(revision=11)
        with mock.patch.object(task_registry, "load_stored_settings", return_value=row) as load:
            line = task_registry.prepare_worker(60, OPERATOR_ENV, transport=FakeTransport())
        load.assert_called_once_with()
        self.assertIn("connection corp-llm", line)
        self.assertIn("revision 11", line)
        self.assertIsInstance(intake_handlers._extractor_source(), AIRequestExtractor)

    def test_an_invalid_selection_stops_the_worker_and_installs_nothing(self):
        for row, environ in ((stored("gone"), OPERATOR_ENV), (stored(), {}), (stored(connections=[corp_connection(enabled=False)]), OPERATOR_ENV)):
            with self.subTest(row.values), mock.patch.object(task_registry, "load_stored_settings", return_value=row):
                with self.assertRaises(task_registry.WorkerConfigurationError):
                    task_registry.prepare_worker(60, environ, transport=FakeTransport())
            self.assertIsNone(intake_handlers._extractor_source)

    def test_a_running_worker_does_not_re_read_a_changed_selection(self):
        with mock.patch.object(task_registry, "load_stored_settings", return_value=stored()) as load:
            task_registry.prepare_worker(60, OPERATOR_ENV, transport=FakeTransport())
            installed = intake_handlers._extractor_source
            load.return_value = stored("ollama-local", "llama", "local_only", revision=6)
            self.assertEqual(installed().profile_id, "corp-small")
            self.assertEqual(load.call_count, 1)

    def test_the_worker_composition_root_does_not_know_connections(self):
        text = open(task_registry.__file__, encoding="utf-8").read()
        self.assertNotIn("ai_connections", text)  # it hands the stored row to the extraction composition unchanged
