"""DB-free tests for the extraction MODES: the settings, the composition, the handler's failure mapping and the worker start-up.

Nothing here opens a database connection (``SimpleTestCase``) or a socket, and no real credential or Workbench is involved. The PostgreSQL behaviour is in
``tests/test_intake_ai_postgres.py`` (NOT run in the development VM).
"""

import io
import logging
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from apps.cases.errors import RecordNotFoundError
from apps.intake import handlers as intake_handlers
from apps.intake import services
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import AIExtractionError, ExtractionConfigurationError
from apps.intake.policy import KIND_EXTRACT
from apps.tasks import worker
from apps.tasks.policy import FailureCode, TaskPermanentError, TaskRetryableError, classify_exception
from config import intake_extraction, task_registry
from config.intake_extraction import MODE_AI, MODE_DETERMINISTIC, build_extractor_source, load_intake_extraction_settings
from tests.intake_ai_support import SECRET_SENTINEL, FakeTransport, ai_environ, module_gate

BACKEND_DIR = Path(__file__).resolve().parent.parent
RUN_ID = uuid.UUID("1234abcd-12ab-56cd-92ef-5678abcd1234")
SUMMARY = services.ExtractionSummary("valid", uuid.UUID(int=1), uuid.UUID(int=2), True)


def source(**env):
    return build_extractor_source(ai_environ(**env), module_gate=module_gate(), lease_seconds=60, transport=FakeTransport())


class SettingsTests(SimpleTestCase):
    def test_nothing_set_means_the_deterministic_mode(self):
        for environ in ({}, {"AP_INTAKE_EXTRACTION_MODE": ""}, {"AP_INTAKE_EXTRACTION_MODE": "   "}):
            with self.subTest(environ):
                settings = load_intake_extraction_settings(environ)
                self.assertEqual((settings.mode, settings.policy_mode), (MODE_DETERMINISTIC, None))

    def test_the_default_does_not_depend_on_any_ai_variable(self):
        settings = load_intake_extraction_settings({"AP_AI_POLICY_MODE": "company_endpoint", "AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL})
        self.assertEqual(settings.mode, MODE_DETERMINISTIC)  # configuring a provider never switches the mode on

    def test_ai_must_be_asked_for_by_name(self):
        settings = load_intake_extraction_settings(ai_environ())
        self.assertEqual((settings.mode, settings.policy_mode.value, settings.timeout_seconds), (MODE_AI, "company_endpoint", 45))

    def test_any_other_mode_is_refused_naming_the_variable_and_not_the_value(self):
        for value in ("AI", "Ai", "true", "1", "auto", "fallback", "deterministic,ai", "ai ai", "off"):
            with self.subTest(value), self.assertRaises(ExtractionConfigurationError) as caught:
                load_intake_extraction_settings({"AP_INTAKE_EXTRACTION_MODE": value})
            self.assertIn("AP_INTAKE_EXTRACTION_MODE", str(caught.exception))
            self.assertNotIn(value + ".", str(caught.exception).replace("deterministic, ai.", ""))

    def test_ai_mode_has_no_default_policy(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            load_intake_extraction_settings(ai_environ(AP_AI_POLICY_MODE=None))
        self.assertIn("AP_AI_POLICY_MODE", str(caught.exception))
        with self.assertRaises(ExtractionConfigurationError):
            load_intake_extraction_settings(ai_environ(AP_AI_POLICY_MODE="  "))

    def test_an_unknown_policy_is_refused_without_echoing_it(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            load_intake_extraction_settings(ai_environ(AP_AI_POLICY_MODE="whatever-SECRET"))
        self.assertNotIn("whatever-SECRET", str(caught.exception))

    def test_the_three_policy_modes_are_the_gateways(self):
        for mode in ("local_only", "company_endpoint", "approved_cloud"):
            self.assertEqual(load_intake_extraction_settings(ai_environ(AP_AI_POLICY_MODE=mode)).policy_mode.value, mode)

    def test_the_timeout_is_bounded(self):
        self.assertEqual(load_intake_extraction_settings(ai_environ(AP_INTAKE_AI_TIMEOUT_SECONDS="5")).timeout_seconds, 5)
        self.assertEqual(load_intake_extraction_settings(ai_environ(AP_INTAKE_AI_TIMEOUT_SECONDS="300")).timeout_seconds, 300)
        for value in ("4", "301", "0", "-1", "abc", "4.5", "1e2"):
            with self.subTest(value), self.assertRaises(ExtractionConfigurationError) as caught:
                load_intake_extraction_settings(ai_environ(AP_INTAKE_AI_TIMEOUT_SECONDS=value))
            self.assertIn("AP_INTAKE_AI_TIMEOUT_SECONDS", str(caught.exception))

    def test_the_settings_are_read_from_the_environment_passed_in_not_from_the_process(self):
        with mock.patch.dict(os.environ, {"AP_INTAKE_EXTRACTION_MODE": "ai"}):
            self.assertEqual(load_intake_extraction_settings({}).mode, MODE_DETERMINISTIC)

    def test_the_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, ai_environ(), clear=False):
            self.assertEqual(load_intake_extraction_settings().mode, MODE_AI)


class DeterministicModeTests(SimpleTestCase):
    def test_it_builds_no_gateway_reads_no_key_and_returns_no_extractor(self):
        with mock.patch.object(intake_extraction.ai_workbench, "build_workbench_gateway", side_effect=AssertionError("gateway built")) as gateway:
            result = build_extractor_source({}, module_gate=None, lease_seconds=60)
        gateway.assert_not_called()
        self.assertIsNone(result())
        self.assertIsNone(result.extractor)
        self.assertIn("deterministic", result.describe())
        self.assertIn("no AI provider is called", result.describe())

    def test_a_key_in_the_environment_changes_nothing_in_the_default_mode(self):
        with mock.patch.object(intake_extraction.ai_workbench, "build_workbench_gateway", side_effect=AssertionError("gateway built")):
            self.assertIsNone(build_extractor_source({"AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL, "AP_AI_POLICY_MODE": "company_endpoint"})())

    def test_an_invalid_mode_does_not_start_a_default_mode_instead(self):
        with self.assertRaises(ExtractionConfigurationError):
            build_extractor_source({"AP_INTAKE_EXTRACTION_MODE": "nonsense"})


class AIModeBuildTests(SimpleTestCase):
    def test_it_builds_the_workbench_extractor_for_the_approved_profile(self):
        built = source()
        self.assertIsInstance(built(), AIRequestExtractor)
        self.assertEqual(built().profile_id, "workbench-gpt5")
        self.assertEqual(intake_extraction.INTAKE_AI_PROFILE_ID, "workbench-gpt5")
        self.assertEqual(intake_extraction.APPROVED_INTAKE_AI_PROFILES, frozenset({"workbench-gpt5"}))
        self.assertNotIn("workbench-gpt41", intake_extraction.APPROVED_INTAKE_AI_PROFILES)

    def test_there_is_no_variable_that_selects_a_profile(self):
        built = source(AP_INTAKE_AI_PROFILE="workbench-gpt41", AP_AI_PROFILE="workbench-gpt41")
        self.assertEqual(built().profile_id, "workbench-gpt5")

    def test_a_missing_key_fails_closed_naming_the_variable_and_never_a_value(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            source(AP_AI_WORKBENCH_API_KEY=None)
        self.assertIn("missing_ap_ai_workbench_api_key", str(caught.exception))
        self.assertNotIn(SECRET_SENTINEL, str(caught.exception))

    def test_an_invalid_provider_setting_fails_closed_without_echoing_it(self):
        for name, value in (("AP_AI_WORKBENCH_ENDPOINT", "https://evil.example/" + SECRET_SENTINEL), ("AP_AI_WORKBENCH_API_KEY", "bad key " + SECRET_SENTINEL), ("AP_AI_WORKBENCH_MAX_RETRIES", "99")):
            with self.subTest(name), self.assertRaises(ExtractionConfigurationError) as caught:
                source(**{name: value})
            self.assertNotIn(SECRET_SENTINEL, str(caught.exception))
            self.assertNotIn("evil.example", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_a_policy_that_forbids_a_company_endpoint_stops_the_start_up(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            source(AP_AI_POLICY_MODE="local_only")
        self.assertIn("policy_violation", str(caught.exception))

    def test_the_module_gate_is_required_so_the_module_can_be_rechecked_before_sending(self):
        for gate in (None, "gate", 5):
            with self.subTest(repr(gate)), self.assertRaises(ExtractionConfigurationError):
                build_extractor_source(ai_environ(), module_gate=gate, lease_seconds=60, transport=FakeTransport())

    def test_building_sends_nothing(self):
        transport = FakeTransport()
        build_extractor_source(ai_environ(), module_gate=module_gate(), lease_seconds=60, transport=transport)
        self.assertEqual(transport.requests, [])

    def test_the_description_names_the_mode_profile_policy_and_deadline_and_no_secret(self):
        text = source(AP_INTAKE_AI_TIMEOUT_SECONDS="30").describe()
        self.assertIn("ai", text)
        for fragment in ("workbench-gpt5", "company_endpoint", "30s"):
            self.assertIn(fragment, text)
        self.assertNotIn(SECRET_SENTINEL, text)

    def test_the_source_is_immutable(self):
        built = source()
        with self.assertRaises(AttributeError):
            built.extractor = None

    def test_an_extractor_exists_exactly_in_ai_mode(self):
        deterministic = load_intake_extraction_settings({})
        with self.assertRaises(ExtractionConfigurationError):
            intake_extraction.IntakeExtractorSource(deterministic, object())
        with self.assertRaises(ExtractionConfigurationError):
            intake_extraction.IntakeExtractorSource(load_intake_extraction_settings(ai_environ()), None)


class LeaseTests(SimpleTestCase):
    def test_the_defaults_fit(self):
        source()  # lease 60 >= call deadline 45 + margin 15

    def test_a_lease_shorter_than_the_deadline_plus_the_margin_stops_the_start_up(self):
        for lease in (5, 30, 59):
            with self.subTest(lease), self.assertRaises(ExtractionConfigurationError) as caught:
                build_extractor_source(ai_environ(), module_gate=module_gate(), lease_seconds=lease, transport=FakeTransport())
            self.assertIn("AP_WORKER_LEASE_SECONDS", str(caught.exception))

    def test_a_shorter_deadline_makes_a_shorter_lease_acceptable(self):
        build_extractor_source(ai_environ(AP_INTAKE_AI_TIMEOUT_SECONDS="10"), module_gate=module_gate(), lease_seconds=25, transport=FakeTransport())

    def test_the_lower_of_the_two_timeouts_is_what_the_lease_must_cover(self):
        # the profile's own timeout (30) is lower than the intake one (45): the deadline is 30, so a lease of 45 is enough
        built = build_extractor_source(ai_environ(AP_AI_WORKBENCH_TIMEOUT_SECONDS="30"), module_gate=module_gate(), lease_seconds=45, transport=FakeTransport())
        self.assertEqual(built.effective_timeout_seconds, 30.0)

    def test_the_deterministic_mode_has_no_lease_requirement(self):
        build_extractor_source({}, lease_seconds=5)


class ImportTests(SimpleTestCase):
    def test_importing_the_composition_reads_no_setting_and_makes_no_call(self):
        code = "import os; os.environ['AP_INTAKE_EXTRACTION_MODE']='nonsense'; os.environ['AP_AI_POLICY_MODE']='x'; import socket; fail = lambda *a, **k: 1 / 0; socket.getaddrinfo = fail; socket.create_connection = fail; import config.intake_extraction as m; print(m.MODES)"
        clean = {key: value for key, value in os.environ.items() if not key.startswith("AP_")}
        completed = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, env=clean, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        self.assertIn("deterministic", completed.stdout)


class HandlerWiringTests(SimpleTestCase):
    HANDLER = intake_handlers.EXTRACT_HANDLER

    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(None)

    def test_with_nothing_installed_the_call_is_exactly_the_deterministic_call_of_before(self):
        with mock.patch.object(intake_handlers, "run_extraction", return_value=SUMMARY) as run:
            self.HANDLER.run({"run_id": str(RUN_ID)})
        run.assert_called_once_with(RUN_ID)

    def test_a_source_answering_none_is_the_deterministic_call_too(self):
        intake_handlers.install_extractor_source(lambda: None)
        with mock.patch.object(intake_handlers, "run_extraction", return_value=SUMMARY) as run:
            self.HANDLER.run({"run_id": str(RUN_ID)})
        run.assert_called_once_with(RUN_ID)

    def test_a_source_answering_an_extractor_passes_it_on(self):
        extractor = object()
        intake_handlers.install_extractor_source(lambda: extractor)
        with mock.patch.object(intake_handlers, "run_extraction", return_value=SUMMARY) as run:
            result = self.HANDLER.run({"run_id": str(RUN_ID)})
        run.assert_called_once_with(RUN_ID, extractor=extractor)
        self.assertEqual(result, SUMMARY.as_task_result())  # the task result is the same small, content-free one

    def test_only_callables_or_none_can_be_installed(self):
        for bad in ("x", 5, object()):
            with self.subTest(repr(bad)), self.assertRaises(TypeError):
                intake_handlers.install_extractor_source(bad)

    def test_the_handler_object_and_kind_are_unchanged(self):
        self.assertIs(intake_handlers.EXTRACT_HANDLER, self.HANDLER)
        self.assertEqual((self.HANDLER.kind, self.HANDLER.is_test_handler), (KIND_EXTRACT, False))


class FailureSemanticsTests(SimpleTestCase):
    HANDLER = intake_handlers.EXTRACT_HANDLER

    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(lambda: object())

    def run_with(self, error):
        with mock.patch.object(intake_handlers, "run_extraction", side_effect=error):
            with self.assertRaises((TaskRetryableError, TaskPermanentError)) as caught:
                self.HANDLER.run({"run_id": str(RUN_ID)})
        return caught.exception

    def test_a_retryable_ai_failure_is_a_temporary_failure_of_the_queue(self):
        for code in ("provider_unavailable", "timeout", "rate_limit", "module_unavailable"):
            with self.subTest(code):
                raised = self.run_with(AIExtractionError(code, "reason", retryable=True))
                self.assertIsInstance(raised, TaskRetryableError)
                failure = classify_exception(raised)
                self.assertEqual((failure.code, failure.retryable), (FailureCode.TEMPORARY_FAILURE, True))

    def test_every_other_ai_failure_is_a_permanent_rejection(self):
        for code in ("policy_violation", "authentication_failure", "unsupported_model", "configuration_error", "invalid_structured_output", "invalid_ai_output", "provider_error", "invalid_request", "capability_missing"):
            with self.subTest(code):
                raised = self.run_with(AIExtractionError(code, "reason"))
                self.assertIsInstance(raised, TaskPermanentError)
                failure = classify_exception(raised)
                self.assertEqual((failure.code, failure.retryable), (FailureCode.REJECTED, False))

    def test_a_configuration_error_is_a_permanent_rejection(self):
        raised = self.run_with(ExtractionConfigurationError("AP_AI_POLICY_MODE is required"))
        self.assertIsInstance(raised, TaskPermanentError)

    def test_no_new_failure_code_exists(self):
        self.assertEqual(
            {code.value for code in FailureCode},
            {"invalid_payload", "unknown_kind", "invalid_result", "rejected", "temporary_failure", "handler_error", "lease_expired"},
        )

    def test_the_task_failure_carries_nothing_from_the_error(self):
        secret = f"SECRET {SECRET_SENTINEL}"
        raised = self.run_with(AIExtractionError("provider_error", secret, retryable=False))
        rendered = str(raised) + repr(raised) + "".join(traceback.format_exception(raised))
        self.assertNotIn("SECRET", rendered)
        self.assertEqual(str(raised), "rejected")
        self.assertIsNone(raised.__cause__)
        self.assertTrue(raised.__suppress_context__)

    def test_the_log_line_holds_the_run_id_and_stable_tokens_only(self):
        with self.assertLogs("apps.intake.handlers", level="WARNING") as captured:
            self.run_with(AIExtractionError("invalid_structured_output", "schema_violation"))
        line = "\n".join(captured.output)
        self.assertIn(str(RUN_ID), line)
        self.assertIn("code=invalid_structured_output", line)
        self.assertIn("reason=schema_violation", line)
        self.assertIn("retryable=False", line)

    def test_other_failures_are_classified_as_before(self):
        raised = None
        with mock.patch.object(intake_handlers, "run_extraction", side_effect=RecordNotFoundError("The run does not exist.")):
            with self.assertRaises(TaskPermanentError) as caught:
                self.HANDLER.run({"run_id": str(RUN_ID)})
            raised = caught.exception
        self.assertEqual(raised.code, FailureCode.REJECTED)
        with mock.patch.object(intake_handlers, "run_extraction", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.HANDLER.run({"run_id": str(RUN_ID)})  # unexpected: retryable within the attempt limit, as before


class PrepareWorkerTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(None)

    def test_the_default_installs_a_source_that_leaves_the_handler_deterministic(self):
        line = task_registry.prepare_worker(60, {})
        self.assertIn("deterministic", line)
        self.assertIsNone(intake_handlers._extractor_source())

    def test_ai_mode_installs_the_ai_extractor(self):
        line = task_registry.prepare_worker(60, ai_environ(), transport=FakeTransport())
        self.assertIn("ai", line)
        self.assertIsInstance(intake_handlers._extractor_source(), AIRequestExtractor)

    def test_the_gate_used_for_the_recheck_is_the_workers_own_kind_gate(self):
        with mock.patch.object(task_registry, "build_extractor_source") as build:
            build.return_value = source()
            task_registry.prepare_worker(60, {})
        self.assertIs(build.call_args.kwargs["module_gate"], task_registry.COMPOSITION.kind_gate)

    def test_a_bad_setting_raises_and_installs_nothing(self):
        for environ in ({"AP_INTAKE_EXTRACTION_MODE": "ai"}, ai_environ(AP_AI_WORKBENCH_API_KEY=None), {"AP_INTAKE_EXTRACTION_MODE": "x"}):
            with self.subTest(environ), self.assertRaises(task_registry.WorkerConfigurationError):
                task_registry.prepare_worker(60, environ, transport=FakeTransport())
            self.assertIsNone(intake_handlers._extractor_source)

    def test_a_lease_that_is_too_short_raises(self):
        with self.assertRaises(task_registry.WorkerConfigurationError):
            task_registry.prepare_worker(30, ai_environ(), transport=FakeTransport())

    def test_the_composition_itself_is_unchanged_by_it(self):
        from apps.intake.registry import PRODUCTION_REGISTRY

        before = (task_registry.COMPOSITION.registry, task_registry.COMPOSITION.kind_gate, task_registry.COMPOSITION.after_iteration)
        task_registry.prepare_worker(60, ai_environ(), transport=FakeTransport())
        self.assertIs(task_registry.COMPOSITION.registry, PRODUCTION_REGISTRY)
        self.assertEqual((task_registry.COMPOSITION.registry, task_registry.COMPOSITION.kind_gate, task_registry.COMPOSITION.after_iteration), before)
        self.assertEqual(set(PRODUCTION_REGISTRY), {"test.add_numbers", "request_intake.extract"})  # no second kind, queue or worker


class RunWorkerStartUpTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(None)
        patcher = mock.patch("apps.tasks.management.commands.runworker.connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("runworker", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def idle(self):
        return mock.patch.object(worker, "process_one", return_value=worker.ProcessOutcome(worker.ProcessStatus.IDLE))

    def test_the_default_start_is_unchanged_and_prints_nothing_new(self):
        with self.idle(), mock.patch.dict(os.environ, {}, clear=True):
            out, _ = self.run_command("--once")
        self.assertEqual(out.strip(), "No task available.")

    def test_a_bad_ai_setting_stops_the_start_before_any_task_is_processed(self):
        with mock.patch.dict(os.environ, {"AP_INTAKE_EXTRACTION_MODE": "ai"}, clear=True), mock.patch.object(worker, "process_one") as process:
            with self.assertRaises(CommandError) as caught:
                self.run_command("--once")
        process.assert_not_called()
        self.assertIn("AP_AI_POLICY_MODE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_a_missing_key_stops_the_start_and_the_error_never_holds_a_value(self):
        env = ai_environ(AP_AI_WORKBENCH_API_KEY=None, AP_AI_WORKBENCH_CHARGE_CODE="CHARGE-SECRET")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(worker, "process_one") as process:
            with self.assertRaises(CommandError) as caught:
                self.run_command("--once")
        process.assert_not_called()
        self.assertNotIn("CHARGE-SECRET", str(caught.exception))

    def test_a_lease_that_cannot_cover_the_ai_deadline_stops_the_start(self):
        with mock.patch.dict(os.environ, ai_environ(), clear=True), mock.patch.object(worker, "process_one") as process:
            with self.assertRaises(CommandError) as caught:
                self.run_command("--once", "--lease-seconds", "30")
        process.assert_not_called()
        self.assertIn("AP_WORKER_LEASE_SECONDS", str(caught.exception))

    def test_ai_mode_starts_installs_the_extractor_and_logs_the_mode_line_without_printing_it(self):
        env = ai_environ()
        stream = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), self.idle(), mock.patch.object(task_registry, "build_extractor_source", wraps=lambda *a, **k: build_extractor_source(*a, **{**k, "transport": FakeTransport()})):
            handler = logging.StreamHandler(stream)
            logging.getLogger("apps.tasks").addHandler(handler)
            self.addCleanup(logging.getLogger("apps.tasks").removeHandler, handler)
            out, err = self.run_command("--once")
        self.assertEqual(out.strip(), "No task available.")
        self.assertIsInstance(intake_handlers._extractor_source(), AIRequestExtractor)
        self.assertIn("Request Intake extraction mode: ai (profile workbench-gpt5", stream.getvalue())
        self.assertNotIn(SECRET_SENTINEL, stream.getvalue() + out + err)

    def test_the_gateway_and_handler_loggers_reach_the_console_while_the_command_runs(self):
        from apps.tasks.management.commands import runworker

        stream = io.StringIO()
        before = {name: list(logging.getLogger(name).handlers) for name in ("apps.tasks", "apps.intake", "apps.ai_gateway")}
        with runworker.console_logging(stream):
            logging.getLogger("apps.ai_gateway").info("ai_gateway call ok profile=workbench-gpt5")
            logging.getLogger("apps.intake.handlers").warning("request_intake.extract run=1 code=timeout")
            logging.getLogger("apps.tasks.worker").info("task t succeeded")
        logging.getLogger("apps.ai_gateway").info("after")
        text = stream.getvalue()
        for fragment in ("call ok profile=workbench-gpt5", "code=timeout", "task t succeeded"):
            self.assertIn(fragment, text)
        self.assertNotIn("after", text)
        for name, handlers in before.items():
            self.assertEqual(logging.getLogger(name).handlers, handlers)  # nothing is left attached after the command
