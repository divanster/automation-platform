"""DB-free tests of the AI extraction settings RULES and of the catalog the composition root builds for them.

No database, network or credential. The catalog is checked against the gateway's own declarations so that a change to a profile, a policy rule or the
approved-profile list that is not reflected in the settings API fails here.
"""

from django.test import SimpleTestCase

from apps.ai_gateway import DataLocality, PolicyMode
from apps.ai_gateway.policy import locality_allowed
from apps.ai_settings import rules
from apps.ai_settings.errors import AISettingsValidationError
from apps.ai_settings.rules import MODE_AI, MODE_DETERMINISTIC, ExtractionValues, validate_values
from config import ai_workbench, intake_extraction
from config.intake_extraction import build_catalog

CATALOG = build_catalog()
PROVIDER = ai_workbench.workbench.PROVIDER_ID
PROFILE = ai_workbench.PROFILE_GPT5
LEASE = 60


def ai(**changes):
    body = {"extraction_mode": MODE_AI, "provider_id": PROVIDER, "profile_id": PROFILE, "policy_mode": "company_endpoint", "timeout_seconds": 45}
    body.update(changes)
    return body


def refused(body, **kwargs):
    with SimpleTestCase().assertRaises(AISettingsValidationError) as caught:
        validate_values(body, CATALOG, **kwargs)
    return caught.exception.fields


class CatalogTests(SimpleTestCase):
    def test_the_catalog_offers_exactly_the_workbench_and_the_approved_profile(self):
        self.assertEqual([p.provider_id for p in CATALOG.providers], [PROVIDER])
        self.assertEqual([p.profile_id for p in CATALOG.profiles], [PROFILE])
        self.assertEqual({p.profile_id for p in CATALOG.profiles}, set(intake_extraction.APPROVED_INTAKE_AI_PROFILES))

    def test_a_profile_the_workflow_does_not_approve_is_not_offered(self):
        self.assertNotIn(ai_workbench.PROFILE_GPT41, {p.profile_id for p in CATALOG.profiles})

    def test_the_provider_locality_is_the_gateways(self):
        self.assertEqual(CATALOG.providers[0].locality, DataLocality.COMPANY_ENDPOINT.value)

    def test_permitted_policy_modes_follow_the_gateways_locality_rule(self):
        expected = tuple(mode.value for mode in PolicyMode if locality_allowed(mode, DataLocality.COMPANY_ENDPOINT))
        self.assertEqual(CATALOG.profiles[0].permitted_policy_modes, expected)
        self.assertNotIn("local_only", expected)

    def test_all_three_policy_modes_are_listed_and_the_bounds_are_the_composition_roots(self):
        self.assertEqual(CATALOG.policy_modes, tuple(m.value for m in PolicyMode))
        self.assertEqual((CATALOG.timeout_min, CATALOG.timeout_max, CATALOG.timeout_default), (5, 300, 45))
        self.assertEqual(CATALOG.lease_margin_seconds, intake_extraction.LEASE_MARGIN_SECONDS)

    def test_the_catalog_is_built_without_reading_the_environment(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"AP_AI_WORKBENCH_API_KEY": "KEY-SENTINEL-1"}), mock.patch.object(ai_workbench, "load_workbench_settings") as load:
            build_catalog()
        load.assert_not_called()

    def test_the_catalog_has_no_field_that_could_hold_a_credential(self):
        import dataclasses

        names = {f.name for cls in (rules.ProviderEntry, rules.ProfileEntry, rules.ExtractionCatalog, rules.ProviderStatus) for f in dataclasses.fields(cls)}
        for name in names:
            for word in ("key", "secret", "token", "password", "header", "endpoint", "proxy", "charge"):
                self.assertNotIn(word, name)


class DeterministicTests(SimpleTestCase):
    def test_deterministic_with_no_ai_field_is_valid_and_is_the_default(self):
        self.assertEqual(validate_values({"extraction_mode": "deterministic"}, CATALOG), rules.DEFAULT_VALUES)
        self.assertEqual(rules.DEFAULT_VALUES, ExtractionValues(MODE_DETERMINISTIC, None, None, None, None))

    def test_explicit_nulls_are_the_same_as_absent(self):
        body = {"extraction_mode": "deterministic", "provider_id": None, "profile_id": None, "policy_mode": None, "timeout_seconds": None}
        self.assertEqual(validate_values(body, CATALOG), rules.DEFAULT_VALUES)

    def test_deterministic_refuses_every_ai_field(self):
        for name, value in (("provider_id", PROVIDER), ("profile_id", PROFILE), ("policy_mode", "company_endpoint"), ("timeout_seconds", 45)):
            with self.subTest(name):
                self.assertEqual(refused({"extraction_mode": "deterministic", name: value}), {name: ["not_allowed_in_deterministic_mode"]})


class AiModeTests(SimpleTestCase):
    def test_a_complete_valid_body_is_accepted_and_returns_the_values(self):
        values = validate_values(ai(), CATALOG, lease_seconds=LEASE)
        self.assertEqual(values, ExtractionValues(MODE_AI, PROVIDER, PROFILE, "company_endpoint", 45))

    def test_approved_cloud_is_accepted_for_a_company_endpoint_provider(self):
        self.assertEqual(validate_values(ai(policy_mode="approved_cloud"), CATALOG).policy_mode, "approved_cloud")

    def test_local_only_is_refused_because_the_policy_forbids_the_provider(self):
        self.assertEqual(refused(ai(policy_mode="local_only")), {"policy_mode": ["policy_forbids_provider"]})

    def test_every_ai_field_is_required_in_ai_mode(self):
        for name in ("provider_id", "profile_id", "policy_mode", "timeout_seconds"):
            with self.subTest(name):
                body = ai()
                del body[name]
                self.assertEqual(refused(body)[name], ["required"])
                self.assertEqual(refused({**ai(), name: None})[name], ["required"])

    def test_an_unknown_provider_is_refused(self):
        self.assertEqual(refused(ai(provider_id="some_other_cloud"))["provider_id"], ["unknown_provider"])

    def test_an_unknown_profile_is_refused(self):
        self.assertEqual(refused(ai(profile_id="made-up-profile"))["profile_id"], ["unknown_profile"])

    def test_a_profile_that_exists_but_is_not_approved_for_intake_is_unknown_here(self):
        self.assertEqual(refused(ai(profile_id=ai_workbench.PROFILE_GPT41))["profile_id"], ["unknown_profile"])

    def test_a_profile_of_another_provider_is_refused(self):
        other = rules.ExtractionCatalog(
            providers=CATALOG.providers + (rules.ProviderEntry("other", "Other", "cloud", lambda: rules.ProviderStatus(True)),),
            profiles=CATALOG.profiles,
            policy_modes=CATALOG.policy_modes,
            timeout_min=5, timeout_max=300, timeout_default=45, lease_margin_seconds=15,
        )  # fmt: skip
        with self.assertRaises(AISettingsValidationError) as caught:
            validate_values(ai(provider_id="other"), other)
        self.assertEqual(caught.exception.fields["profile_id"], ["profile_not_of_provider"])

    def test_an_unknown_policy_mode_is_refused(self):
        self.assertEqual(refused(ai(policy_mode="open_internet"))["policy_mode"], ["invalid_choice"])

    def test_the_timeout_bounds(self):
        for value in (5, 45, 300):
            self.assertEqual(validate_values(ai(timeout_seconds=value), CATALOG).timeout_seconds, value)
        for value in (0, 4, 301, -1, 10**9):
            with self.subTest(value):
                self.assertEqual(refused(ai(timeout_seconds=value))["timeout_seconds"], ["out_of_range"])

    def test_the_timeout_must_be_a_real_integer(self):
        for value in ("45", 45.0, 45.5, True, False, [45], {"a": 1}):
            with self.subTest(value):
                self.assertEqual(refused(ai(timeout_seconds=value))["timeout_seconds"], ["invalid_value"])

    def test_the_timeout_must_fit_the_worker_lease_with_the_margin(self):
        self.assertEqual(validate_values(ai(timeout_seconds=45), CATALOG, lease_seconds=60).timeout_seconds, 45)
        self.assertEqual(refused(ai(timeout_seconds=46), lease_seconds=60)["timeout_seconds"], ["exceeds_worker_lease"])
        self.assertEqual(refused(ai(timeout_seconds=300), lease_seconds=60)["timeout_seconds"], ["exceeds_worker_lease"])

    def test_without_a_lease_the_lease_is_not_checked(self):
        self.assertEqual(validate_values(ai(timeout_seconds=300), CATALOG).timeout_seconds, 300)

    def test_text_fields_must_be_non_empty_strings(self):
        for name in ("provider_id", "profile_id", "policy_mode"):
            for value in ("", 5, True, ["x"], "x" * 65):
                with self.subTest(name=name, value=value):
                    self.assertEqual(refused(ai(**{name: value}))[name], ["invalid_value"])


class ShapeTests(SimpleTestCase):
    def test_the_mode_is_required_and_closed(self):
        for body in ({}, {"extraction_mode": None}, {"extraction_mode": "AI"}, {"extraction_mode": "auto"}, {"extraction_mode": "fallback"}, {"extraction_mode": 1}):
            with self.subTest(body):
                self.assertEqual(refused(body)["extraction_mode"], ["invalid_choice"])

    def test_a_body_that_is_not_a_mapping_is_refused(self):
        for body in (None, [], "ai", 5):
            with self.subTest(body):
                self.assertEqual(refused(body), {"_": ["invalid_type"]})

    def test_unknown_fields_are_refused_and_never_echoed_unsafely(self):
        fields = refused({**ai(), "api_key": "KEY-SENTINEL-2", "endpoint": "https://evil.invalid"})
        self.assertEqual(fields["api_key"], ["unknown_field"])
        self.assertEqual(fields["endpoint"], ["unknown_field"])
        self.assertNotIn("KEY-SENTINEL-2", repr(fields))
        self.assertEqual(refused({**ai(), "not a name!": 1})["_unknown"], ["unknown_field"])

    def test_the_error_carries_codes_only_never_a_submitted_value(self):
        sentinel = "VALUE-SENTINEL-3"
        for body in (ai(provider_id=sentinel), ai(profile_id=sentinel), ai(policy_mode=sentinel), {"extraction_mode": sentinel}):
            with self.assertRaises(AISettingsValidationError) as caught:
                validate_values(body, CATALOG)
            self.assertNotIn(sentinel, repr(caught.exception.fields))
            self.assertNotIn(sentinel, str(caught.exception))

    def test_all_errors_are_reported_together(self):
        fields = refused(ai(provider_id="x", profile_id="y", policy_mode="z", timeout_seconds=1))
        self.assertEqual(set(fields), {"provider_id", "profile_id", "policy_mode", "timeout_seconds"})
