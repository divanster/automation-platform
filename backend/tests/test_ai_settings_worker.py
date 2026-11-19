"""DB-free tests of how the worker uses the STORED AI extraction settings: precedence over the environment, validation again at start-up, the fail-closed
database read, the absence of any hot reload, and the unchanged environment-only behaviour when no row exists.

No database (the row is passed in or ``load_stored_settings`` is patched), no network (a fake transport), no real credential.
"""

import re
from pathlib import Path
from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase

from apps.ai_settings.rules import ExtractionValues
from apps.ai_settings.services import StoredSettings
from apps.intake import handlers as intake_handlers
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import ExtractionConfigurationError
from config import ai_workbench, intake_extraction, task_registry
from config.intake_extraction import IntakeExtractionSettings, build_extractor_source
from tests.intake_ai_support import SECRET_SENTINEL, FakeTransport, ai_environ, module_gate

BACKEND = Path(__file__).resolve().parent.parent
PROVIDER = ai_workbench.workbench.PROVIDER_ID
PROFILE = ai_workbench.PROFILE_GPT5
KEY_ONLY = {"AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL}


def stored(revision=3, mode="ai", provider=PROVIDER, profile=PROFILE, policy="company_endpoint", timeout=30):
    if mode == "deterministic":
        return StoredSettings(ExtractionValues("deterministic"), revision)
    return StoredSettings(ExtractionValues(mode, provider, profile, policy, timeout), revision)


def build(row, environ=None, lease=60, **kwargs):
    return build_extractor_source(KEY_ONLY if environ is None else environ, module_gate=module_gate(), lease_seconds=lease, transport=FakeTransport(), stored=row, **kwargs)


class StoredSettingsPrecedenceTests(SimpleTestCase):
    def test_a_stored_ai_row_builds_the_ai_extractor_with_its_own_values(self):
        source = build(stored(timeout=30, policy="approved_cloud"))
        self.assertIsInstance(source.extractor, AIRequestExtractor)
        self.assertEqual(source.extractor.profile_id, PROFILE)
        self.assertEqual(source.extractor._policy.mode.value, "approved_cloud")
        self.assertEqual(source.effective_timeout_seconds, 30.0)
        self.assertEqual((source.settings.stored_revision, source.settings.profile_id), (3, PROFILE))

    def test_the_stored_timeout_can_only_lower_the_profile_timeout_as_before(self):
        self.assertEqual(build(stored(timeout=300), lease=400).effective_timeout_seconds, 90.0)

    def test_with_a_row_the_three_mode_variables_are_not_read(self):
        environ = {**KEY_ONLY, "AP_INTAKE_EXTRACTION_MODE": "nonsense", "AP_AI_POLICY_MODE": "nonsense", "AP_INTAKE_AI_TIMEOUT_SECONDS": "nonsense"}
        self.assertEqual(build(stored(), environ).settings.mode, "ai")

    def test_a_stored_deterministic_row_wins_over_an_environment_that_asks_for_ai(self):
        source = build(stored(mode="deterministic"), ai_environ())
        self.assertIsNone(source())
        self.assertEqual((source.settings.mode, source.settings.stored_revision), ("deterministic", 3))

    def test_a_stored_deterministic_row_needs_no_credential_and_builds_no_gateway(self):
        with mock.patch.object(ai_workbench, "build_workbench_gateway") as gateway:
            source = build(stored(mode="deterministic"), {})
        gateway.assert_not_called()
        self.assertIsNone(source())

    def test_a_stored_ai_row_with_no_key_in_the_environment_stops_the_worker_without_naming_a_value(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            build(stored(), {})
        self.assertIn("configuration_error", str(caught.exception))

    def test_a_stored_ai_row_with_a_lease_that_cannot_cover_it_stops_the_worker(self):
        with self.assertRaises(ExtractionConfigurationError):
            build(stored(timeout=46), lease=60)

    def test_the_provider_key_always_comes_from_the_environment_never_from_the_row(self):
        row = stored()
        self.assertFalse(hasattr(row, "api_key"))
        self.assertFalse(hasattr(row.values, "api_key"))
        with self.assertRaises(ExtractionConfigurationError):
            build(row, {})  # no key in the environment: there is nowhere else to find one

    def test_ai_mode_still_needs_the_module_gate(self):
        with self.assertRaises(ExtractionConfigurationError):
            build_extractor_source(KEY_ONLY, module_gate=None, lease_seconds=60, transport=FakeTransport(), stored=stored())

    def test_the_description_names_the_source_and_revision_and_no_secret(self):
        line = build(stored(revision=7)).describe()
        self.assertIn("stored settings, revision 7", line)
        self.assertIn(f"profile {PROFILE}", line)
        self.assertNotIn(SECRET_SENTINEL, line)
        self.assertIn("stored settings, revision 2", build(stored(revision=2, mode="deterministic"), {}).describe())


class InvalidStoredRowTests(SimpleTestCase):
    """A row that no longer passes the rules (edited by hand, or written for an older catalog) is refused at start-up and never replaced by the environment."""

    def test_every_invalid_row_stops_the_worker_and_the_message_holds_no_value(self):
        sentinel = "STORED-VALUE-SENTINEL-77"
        rows = (
            stored(provider="some_other_cloud"),
            stored(provider=sentinel),
            stored(profile=ai_workbench.PROFILE_GPT41),
            stored(profile=sentinel),
            stored(policy="local_only"),
            stored(policy=sentinel),
            stored(timeout=4),
            stored(timeout=301),
            StoredSettings(ExtractionValues("ai", PROVIDER, PROFILE, "company_endpoint", None), 1),
            StoredSettings(ExtractionValues("ai"), 1),
            StoredSettings(ExtractionValues("auto", None, None, None, None), 1),
            StoredSettings(ExtractionValues("deterministic", PROVIDER, None, None, None), 1),
        )
        for row in rows:
            with self.subTest(row=row.values), self.assertRaises(ExtractionConfigurationError) as caught:
                build(row, ai_environ())  # a valid ai environment is available and must NOT be used instead
            self.assertNotIn(sentinel, str(caught.exception))
            self.assertIn("stored AI extraction settings", str(caught.exception))

    def test_local_only_never_reaches_a_company_endpoint_provider(self):
        with self.assertRaises(ExtractionConfigurationError) as caught:
            build(stored(policy="local_only"), ai_environ())
        self.assertIn("policy_forbids_provider", str(caught.exception))

    def test_an_invalid_row_never_falls_back_to_the_environment_or_to_deterministic(self):
        with self.assertRaises(ExtractionConfigurationError):
            build(stored(profile="nope"), ai_environ())
        with self.assertRaises(ExtractionConfigurationError):
            build(stored(profile="nope"), {})

    def test_a_profile_removed_from_the_approved_list_stops_the_worker(self):
        with mock.patch.object(intake_extraction, "APPROVED_INTAKE_AI_PROFILES", frozenset()), self.assertRaises(ExtractionConfigurationError):
            build(stored())


class PrepareWorkerStoredSettingsTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)
        intake_handlers.install_extractor_source(None)

    def prepare(self, environ, row=None, **kwargs):
        with mock.patch.object(task_registry, "load_stored_settings", return_value=row) as load:
            line = task_registry.prepare_worker(60, environ, transport=FakeTransport(), **kwargs)
        return line, load

    def test_the_stored_row_is_read_once_per_start_and_installed(self):
        line, load = self.prepare(KEY_ONLY, stored(revision=9))
        load.assert_called_once_with()
        self.assertIn("revision 9", line)
        self.assertIsInstance(intake_handlers._extractor_source(), AIRequestExtractor)

    def test_with_no_row_the_environment_decides_exactly_as_before(self):
        line, _ = self.prepare({})
        self.assertEqual(line, "Request Intake extraction mode: deterministic (fixture parser; no AI provider is called).")
        self.assertIsNone(intake_handlers._extractor_source())
        line, _ = self.prepare(ai_environ())
        self.assertEqual(line, "Request Intake extraction mode: ai (profile workbench-gpt5, data policy company_endpoint, call deadline 45s).")

    def test_the_environment_only_settings_are_unchanged(self):
        self.assertEqual(intake_extraction.load_intake_extraction_settings({}), IntakeExtractionSettings("deterministic"))
        settings = intake_extraction.load_intake_extraction_settings(ai_environ())
        self.assertEqual((settings.mode, settings.profile_id, settings.stored_revision), ("ai", None, None))

    def test_an_unreadable_database_stops_the_worker_and_does_not_fall_back_to_the_environment(self):
        error = OperationalError("connection to server at 10.0.0.5 failed: password=DB-PASSWORD-SENTINEL")
        with mock.patch.object(task_registry, "load_stored_settings", side_effect=error):
            with self.assertRaises(task_registry.WorkerConfigurationError) as caught:
                task_registry.prepare_worker(60, ai_environ(), transport=FakeTransport())
        self.assertNotIn("DB-PASSWORD-SENTINEL", str(caught.exception))
        self.assertNotIn("10.0.0.5", str(caught.exception))
        self.assertIn("OperationalError", str(caught.exception))
        self.assertIsNone(intake_handlers._extractor_source)  # nothing was installed

    def test_an_invalid_row_stops_the_worker_and_installs_nothing(self):
        with mock.patch.object(task_registry, "load_stored_settings", return_value=stored(policy="local_only")):
            with self.assertRaises(task_registry.WorkerConfigurationError):
                task_registry.prepare_worker(60, ai_environ(), transport=FakeTransport())
        self.assertIsNone(intake_handlers._extractor_source)

    def test_the_stored_row_is_passed_to_the_builder_and_the_gate_is_still_the_workers_own(self):
        row = stored()
        with mock.patch.object(task_registry, "build_extractor_source") as build_mock:
            build_mock.return_value = build(row)
            task_registry.prepare_worker(60, {}, stored_settings=row)
        self.assertIs(build_mock.call_args.kwargs["stored"], row)
        self.assertIs(build_mock.call_args.kwargs["module_gate"], task_registry.COMPOSITION.kind_gate)

    def test_a_stored_deterministic_row_keeps_the_handler_on_the_fixture_parser(self):
        line, _ = self.prepare(ai_environ(), stored(mode="deterministic"))
        self.assertIn("deterministic", line)
        self.assertIsNone(intake_handlers._extractor_source())


class NoHotReloadTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(intake_handlers.install_extractor_source, intake_handlers._extractor_source)

    def test_a_running_worker_keeps_what_it_read_at_start_up(self):
        with mock.patch.object(task_registry, "load_stored_settings", return_value=stored(mode="deterministic")) as load:
            task_registry.prepare_worker(60, KEY_ONLY, transport=FakeTransport())
            installed = intake_handlers._extractor_source
            load.return_value = stored(revision=4)  # an administrator saves a different mode afterwards
            self.assertIsNone(installed())  # the installed source still answers with the old decision
            self.assertEqual(load.call_count, 1)
            task_registry.prepare_worker(60, KEY_ONLY, transport=FakeTransport())  # only a restart reads it again
            self.assertIsInstance(intake_handlers._extractor_source(), AIRequestExtractor)

    def test_the_installed_source_is_immutable(self):
        source = build(stored())
        with self.assertRaises(AttributeError):
            source.settings = None
        with self.assertRaises(AttributeError):
            source.extractor = None

    def test_nothing_on_the_task_path_reads_the_stored_settings(self):
        for relative in ("apps/intake/handlers.py", "apps/intake/services.py", "apps/intake/ai_extraction.py", "apps/tasks/worker.py"):
            with self.subTest(relative):
                text = (BACKEND / relative).read_text(encoding="utf-8")
                self.assertNotIn("ai_settings", text)
                self.assertNotIn("load_stored_settings", text)

    def test_the_stored_row_is_read_in_exactly_one_place_in_the_worker_composition(self):
        holders = []
        for path in (BACKEND / "config").rglob("*.py"):
            if re.search(r"load_stored_settings\(", path.read_text(encoding="utf-8")):
                holders.append(path.name)
        self.assertEqual(holders, ["task_registry.py"])
