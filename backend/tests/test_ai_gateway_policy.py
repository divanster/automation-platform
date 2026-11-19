"""DB-free tests of data-locality policy enforcement: local-only refusal, no silent fallback, policy before provider invocation, fail closed."""

import unittest

from apps.ai_gateway import ALLOWED_LOCALITIES, AIGatewayError, DataLocality, PolicyContext, PolicyMode, ProviderResult, enforce_policy
from apps.ai_gateway.policy import locality_allowed
from tests.ai_gateway_support import GOOD_OUTPUT, ScriptedProvider, local_policy, make_gateway, make_profile, make_request

L, E, C = DataLocality.LOCAL, DataLocality.COMPANY_ENDPOINT, DataLocality.CLOUD
LOCAL_ONLY, COMPANY, CLOUD = PolicyMode.LOCAL_ONLY, PolicyMode.COMPANY_ENDPOINT, PolicyMode.APPROVED_CLOUD


class PolicyTableTests(unittest.TestCase):
    def test_the_table_is_exactly_the_documented_one(self):
        self.assertEqual(dict(ALLOWED_LOCALITIES), {LOCAL_ONLY: {L}, COMPANY: {L, E}, CLOUD: {L, E, C}})

    def test_the_table_cannot_be_changed(self):
        with self.assertRaises(TypeError):
            ALLOWED_LOCALITIES[LOCAL_ONLY] = frozenset(DataLocality)
        self.assertIsInstance(ALLOWED_LOCALITIES[LOCAL_ONLY], frozenset)

    def test_local_only_permits_nothing_but_local(self):
        self.assertTrue(locality_allowed(LOCAL_ONLY, L))
        self.assertFalse(locality_allowed(LOCAL_ONLY, E))
        self.assertFalse(locality_allowed(LOCAL_ONLY, C))

    def test_every_mode_and_locality_pair(self):
        expected = {(LOCAL_ONLY, L): True, (LOCAL_ONLY, E): False, (LOCAL_ONLY, C): False, (COMPANY, L): True, (COMPANY, E): True, (COMPANY, C): False, (CLOUD, L): True, (CLOUD, E): True, (CLOUD, C): True}
        for (mode, locality), allowed in expected.items():
            with self.subTest(mode=mode.value, locality=locality.value):
                self.assertEqual(locality_allowed(mode, locality), allowed)

    def test_enforce_policy_needs_both_modes_to_allow_the_locality(self):
        approved = frozenset({"cloud-x"})
        # allowed: both permit
        enforce_policy(PolicyContext(CLOUD, approved), profile_mode=CLOUD, provider_id="cloud-x", locality=C)
        # refused: the company mode in force does not permit it
        with self.assertRaises(AIGatewayError) as ctx:
            enforce_policy(PolicyContext(COMPANY, approved), profile_mode=CLOUD, provider_id="cloud-x", locality=C)
        self.assertEqual(ctx.exception.code, "policy_violation")
        # refused: the profile's own mode does not permit it
        with self.assertRaises(AIGatewayError) as ctx:
            enforce_policy(PolicyContext(CLOUD, approved), profile_mode=COMPANY, provider_id="cloud-x", locality=C)
        self.assertEqual(ctx.exception.code, "policy_violation")

    def test_a_cloud_provider_must_also_be_explicitly_approved(self):
        with self.assertRaises(AIGatewayError) as ctx:
            enforce_policy(PolicyContext(CLOUD), profile_mode=CLOUD, provider_id="cloud-x", locality=C)
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("policy_violation", "cloud_provider_not_approved"))
        with self.assertRaises(AIGatewayError):
            enforce_policy(PolicyContext(CLOUD, frozenset({"another"})), profile_mode=CLOUD, provider_id="cloud-x", locality=C)

    def test_the_approved_list_does_not_matter_for_local_and_company_providers(self):
        enforce_policy(PolicyContext(CLOUD), profile_mode=CLOUD, provider_id="p", locality=L)
        enforce_policy(PolicyContext(CLOUD), profile_mode=CLOUD, provider_id="p", locality=E)


class FailClosedTests(unittest.TestCase):
    def test_a_missing_or_wrong_policy_context_is_a_configuration_error(self):
        for bad in (None, "local_only", PolicyMode.LOCAL_ONLY, {"mode": "local_only"}, object()):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                enforce_policy(bad, profile_mode=LOCAL_ONLY, provider_id="p", locality=L)
            self.assertEqual(ctx.exception.code, "configuration_error")
            self.assertEqual(ctx.exception.reason, "missing_policy_context")

    def test_an_invalid_mode_cannot_be_built(self):
        for bad in (None, "", "cloud", "approved_external", "LOCAL_ONLY", 0):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                PolicyContext(bad)
            self.assertEqual(ctx.exception.code, "configuration_error")

    def test_invalid_approved_provider_collections_are_refused(self):
        for bad in ("cloud-x", [1], None):
            with self.subTest(bad), self.assertRaises((AIGatewayError, TypeError)):
                PolicyContext(CLOUD, bad)

    def test_the_policy_is_a_required_keyword_of_every_call(self):
        gateway = make_gateway()
        with self.assertRaises(TypeError):
            gateway.generate(make_request())

    def test_the_gateway_refuses_a_call_with_no_valid_policy_before_calling_the_provider(self):
        provider = ScriptedProvider()
        gateway = make_gateway(provider)
        for bad in (None, "local_only"):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                gateway.generate(make_request(), policy=bad)
            self.assertEqual(ctx.exception.code, "configuration_error")
        self.assertEqual(provider.calls, [])


class EnforcementThroughTheGatewayTests(unittest.TestCase):
    def cloud_gateway(self, behaviour=None, **kwargs):
        provider = ScriptedProvider("cloud-x", C, behaviour=behaviour)
        profile = make_profile(provider_id="cloud-x", policy_mode=CLOUD)
        return provider, make_gateway(provider, profiles=(profile,), **kwargs)

    def test_local_only_never_calls_a_cloud_provider(self):
        provider, gateway = self.cloud_gateway()
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=PolicyContext(LOCAL_ONLY, frozenset({"cloud-x"})))  # even an approved cloud provider
        self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertEqual(provider.calls, [], "the provider must not be called when policy refuses")

    def test_local_only_never_calls_a_company_endpoint_provider(self):
        provider = ScriptedProvider("company-x", E)
        gateway = make_gateway(provider, profiles=(make_profile(provider_id="company-x", policy_mode=COMPANY),))
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=local_policy())
        self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertEqual(provider.calls, [])

    def test_company_endpoint_mode_refuses_cloud_and_permits_company_and_local(self):
        provider, gateway = self.cloud_gateway()
        with self.assertRaises(AIGatewayError):
            gateway.generate(make_request(), policy=PolicyContext(COMPANY, frozenset({"cloud-x"})))
        self.assertEqual(provider.calls, [])
        company = ScriptedProvider("company-x", E)
        gateway = make_gateway(company, profiles=(make_profile(provider_id="company-x", policy_mode=COMPANY),))
        gateway.generate(make_request(), policy=PolicyContext(COMPANY))
        self.assertEqual(len(company.calls), 1)

    def test_approved_cloud_calls_only_a_named_approved_provider(self):
        provider, gateway = self.cloud_gateway()
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=PolicyContext(CLOUD))
        self.assertEqual(ctx.exception.reason, "cloud_provider_not_approved")
        self.assertEqual(provider.calls, [])
        response = gateway.generate(make_request(), policy=PolicyContext(CLOUD, frozenset({"cloud-x"})))
        self.assertEqual(len(provider.calls), 1)
        self.assertIs(response.provenance.locality, C)
        self.assertIs(response.provenance.policy_mode, CLOUD)

    def test_policy_is_evaluated_on_every_call_against_the_current_mode(self):
        provider, gateway = self.cloud_gateway()
        approved = PolicyContext(CLOUD, frozenset({"cloud-x"}))
        gateway.generate(make_request(), policy=approved)
        with self.assertRaises(AIGatewayError) as ctx:  # an administrator tightened the mode between two calls
            gateway.generate(make_request(), policy=local_policy())
        self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(provider.calls), 1)

    def test_a_profile_whose_policy_mode_does_not_allow_its_provider_cannot_be_configured(self):
        provider = ScriptedProvider("cloud-x", C)
        for mode in (LOCAL_ONLY, COMPANY):
            with self.subTest(mode.value), self.assertRaises(AIGatewayError) as ctx:
                make_gateway(provider, profiles=(make_profile(provider_id="cloud-x", policy_mode=mode),))
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "profile_locality_mismatch"))

    def test_policy_is_enforced_before_limits_and_before_any_provider_code(self):
        provider, gateway = self.cloud_gateway()
        oversized = make_request(input_text="x" * 200_000)  # would also fail the input limit
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(oversized, policy=local_policy())
        self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertEqual(provider.calls, [])

    def test_the_policy_error_names_the_provider_and_profile_but_no_content(self):
        provider, gateway = self.cloud_gateway()
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(input_text="PRIVATE-DOCUMENT-TEXT"), policy=local_policy())
        error = ctx.exception
        self.assertEqual((error.provider_id, error.profile_id), ("cloud-x", "profile-a"))
        self.assertNotIn("PRIVATE-DOCUMENT-TEXT", f"{error} {error!r}")


class NoSilentFallbackTests(unittest.TestCase):
    def two_providers(self, primary_behaviour):
        primary = ScriptedProvider("cloud-x", C, behaviour=primary_behaviour)
        secondary = ScriptedProvider("local-y", L)
        profiles = (make_profile(provider_id="cloud-x", policy_mode=CLOUD), make_profile(profile_id="profile-b", provider_id="local-y", policy_mode=LOCAL_ONLY))
        return primary, secondary, make_gateway(primary, secondary, profiles=profiles)

    def test_a_refused_cloud_call_is_not_redirected_to_a_local_provider(self):
        primary, secondary, gateway = self.two_providers(None)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=local_policy())
        self.assertEqual(ctx.exception.code, "policy_violation")
        self.assertEqual((primary.calls, secondary.calls), ([], []))

    def test_a_failing_provider_is_not_replaced_by_another_one(self):
        def unavailable(call):
            raise AIGatewayError("provider_unavailable")

        primary, secondary, gateway = self.two_providers(unavailable)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=PolicyContext(CLOUD, frozenset({"cloud-x"})))
        self.assertEqual(ctx.exception.code, "provider_unavailable")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(secondary.calls, [], "no fallback, not even to a MORE restrictive provider")

    def test_a_local_failure_is_never_retried_against_a_cloud_provider(self):
        def unavailable(call):
            raise AIGatewayError("provider_unavailable")

        local = ScriptedProvider("local-y", L, behaviour=unavailable)
        cloud = ScriptedProvider("cloud-x", C)
        gateway = make_gateway(local, cloud, profiles=(make_profile(provider_id="local-y"), make_profile(profile_id="profile-b", provider_id="cloud-x", policy_mode=CLOUD)))
        with self.assertRaises(AIGatewayError):
            gateway.generate(make_request(), policy=PolicyContext(CLOUD, frozenset({"cloud-x"})))
        self.assertEqual(cloud.calls, [])

    def test_a_failing_model_is_never_replaced_by_another_model_of_the_same_provider(self):
        def refuses(call):
            raise AIGatewayError("unsupported_model")

        provider = ScriptedProvider(models=("model-a", "model-b"), behaviour=refuses)
        profiles = (make_profile(), make_profile(profile_id="profile-b", model_id="model-b"))
        gateway = make_gateway(provider, profiles=profiles)
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(), policy=local_policy())
        self.assertEqual(ctx.exception.code, "unsupported_model")
        self.assertEqual([call.model_id for call in provider.calls], ["model-a"], "exactly one call, with exactly the profile's model")

    def test_the_gateway_has_no_notion_of_a_fallback_or_default_provider(self):
        gateway = make_gateway()
        public = {name for name in dir(gateway) if not name.startswith("_")}
        self.assertEqual(public, {"generate", "profile", "profile_ids"})
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(make_request(profile_id="no-such-profile"), policy=local_policy())
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "unknown_profile"))

    def test_a_successful_local_call_still_works_under_every_mode(self):
        provider = ScriptedProvider(behaviour=lambda call: ProviderResult(GOOD_OUTPUT, "model-a"))
        gateway = make_gateway(provider)
        for mode in PolicyMode:
            with self.subTest(mode.value):
                gateway.generate(make_request(), policy=PolicyContext(mode))
        self.assertEqual(len(provider.calls), 3)
