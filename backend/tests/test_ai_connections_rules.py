"""DB-free tests of the pure connection rules (``apps.ai_settings.rules``) against the real catalog: structure, provider types, localities, endpoints, credential
references, profiles, selection and the data-policy combinations. No database, no environment, no network."""

from django.test import SimpleTestCase

from apps.ai_settings.errors import AISettingsValidationError
from apps.ai_settings.rules import (
    CREDENTIAL_REF_PREFIX,
    MAX_CONNECTIONS,
    ConnectionValues,
    parse_stored_connection,
    validate_connections,
    validate_values,
)
from config.intake_extraction import build_catalog
from tests.ai_connections_support import (
    CLOUD_ENDPOINT,
    CORP_ENDPOINT,
    SECRET_A,
    all_connections,
    cloud_connection,
    corp_connection,
    local_openai_connection,
    ollama_connection,
    workbench_connection,
)

CATALOG = build_catalog()


def problems(raw):
    with_error = None
    try:
        validate_connections(raw, CATALOG)
    except AISettingsValidationError as error:
        with_error = error
    return None if with_error is None else with_error.fields


def selection(**changes):
    values = {"extraction_mode": "ai", "connection_id": "corp-llm", "profile_id": "corp-small", "policy_mode": "company_endpoint", "timeout_seconds": 30}
    values.update(changes)
    return values


def select(raw_values, connections, **kwargs):
    try:
        return validate_values(raw_values, CATALOG, connections=validate_connections(connections, CATALOG), **kwargs)
    except AISettingsValidationError as error:
        return error.fields


class MultipleConnectionTests(SimpleTestCase):
    def test_several_connections_of_every_provider_type_are_accepted_together(self):
        connections = validate_connections(all_connections(), CATALOG)
        self.assertEqual([c.connection_id for c in connections], ["workbench", "corp-llm", "vendor", "local-server", "ollama-local"])
        self.assertEqual({c.provider_type for c in connections}, {"kpmg_workbench", "openai_compatible", "ollama"})
        self.assertTrue(all(isinstance(c, ConnectionValues) for c in connections))

    def test_an_empty_list_is_valid_and_removes_every_connection(self):
        self.assertEqual(validate_connections([], CATALOG), ())

    def test_a_connection_round_trips_through_its_mapping(self):
        for raw in all_connections():
            with self.subTest(raw["connection_id"]):
                (connection,) = validate_connections([raw], CATALOG)
                self.assertEqual(connection.as_mapping(), raw)
                self.assertEqual(parse_stored_connection(connection.as_mapping()), connection)

    def test_a_list_must_be_a_list_and_within_the_limit(self):
        self.assertEqual(problems({"a": 1}), {"connections": ["invalid_type"]})
        self.assertEqual(problems("x"), {"connections": ["invalid_type"]})
        many = [corp_connection(connection_id=f"c{i}") for i in range(MAX_CONNECTIONS + 1)]
        self.assertEqual(problems(many), {"connections": ["too_many"]})
        self.assertIsNone(problems(many[:MAX_CONNECTIONS]))

    def test_duplicate_connection_and_profile_identifiers_are_refused(self):
        self.assertEqual(problems([corp_connection(), corp_connection()]), {"connections[1].connection_id": ["duplicate_connection_id"]})
        twice = corp_connection(profiles=[{"profile_id": "p", "model_id": "m1"}, {"profile_id": "p", "model_id": "m2"}])
        self.assertEqual(problems([twice]), {"connections[0].profiles[1].profile_id": ["duplicate_profile_id"]})

    def test_the_same_profile_id_may_be_used_by_two_connections(self):
        first, second = corp_connection(), corp_connection(connection_id="corp-2")
        self.assertIsNone(problems([first, second]))

    def test_errors_name_the_connection_index_and_field_never_a_value(self):
        sentinel = "VALUE-SENTINEL-4d"
        fields = problems([corp_connection(), corp_connection(connection_id="b", display_name=sentinel + "\n", endpoint="ftp://" + sentinel)])
        self.assertNotIn(sentinel, repr(fields))
        self.assertTrue(all(name.startswith("connections[1]") for name in fields))


class StructureTests(SimpleTestCase):
    def test_every_required_field_is_reported_when_missing(self):
        fields = problems([{}])
        for name in ("connection_id", "display_name", "provider_type", "locality", "enabled", "profiles"):
            self.assertEqual(fields[f"connections[0].{name}"], ["required"], name)

    def test_an_item_that_is_not_an_object_is_refused(self):
        self.assertEqual(problems(["x"]), {"connections[0]": ["invalid_type"]})
        self.assertEqual(problems([None]), {"connections[0]": ["invalid_type"]})

    def test_unknown_fields_are_refused_and_a_credential_field_cannot_exist(self):
        for name in ("api_key", "secret", "token", "password", "header", "proxy_url", "charge_code", "authorization"):
            with self.subTest(name):
                fields = problems([{**corp_connection(), name: SECRET_A}])
                self.assertEqual(fields, {f"connections[0].{name}": ["unknown_field"]})
                self.assertNotIn(SECRET_A, repr(fields))

    def test_an_unknown_field_inside_a_profile_is_refused(self):
        raw = corp_connection(profiles=[{"profile_id": "p", "model_id": "m", "api_key": SECRET_A}])
        self.assertEqual(problems([raw]), {"connections[0].profiles[0].api_key": ["unknown_field"]})

    def test_value_shapes(self):
        bad = {
            "connection_id": ("Upper", "1abc", "has space", "x" * 49, 5, ""),
            "display_name": (" lead", "trail ", "", "x" * 81, "tab\there", 5),
            "provider_type": ("OpenAI", 5, ""),
            "enabled": ("true", 1, 0, None),
            "endpoint": ("", 5, "x" * 513),
            "credential_ref": ("lowercase", "AP_AI_CRED_", "AP_AI_CRED_lower", 5, "AP_AI_CRED_" + "X" * 49),
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    fields = problems([corp_connection(**{name: value})])
                    self.assertIn(f"connections[0].{name}", fields)

    def test_profile_shapes(self):
        for profile in ({"profile_id": "Bad", "model_id": "m"}, {"profile_id": "p", "model_id": "has space"}, {"profile_id": "p"}, {"model_id": "m"}, "x", {"profile_id": 5, "model_id": "m"}):
            with self.subTest(profile=profile):
                self.assertTrue(problems([corp_connection(profiles=[profile])]))
        self.assertEqual(problems([corp_connection(profiles=[])]), {"connections[0].profiles": ["required"]})
        self.assertEqual(problems([corp_connection(profiles="x")]), {"connections[0].profiles": ["invalid_type"]})
        self.assertEqual(problems([corp_connection(profiles=[{"profile_id": f"p{i}", "model_id": "m"} for i in range(11)])]), {"connections[0].profiles": ["too_many"]})


class ProviderTypeTests(SimpleTestCase):
    def test_the_provider_types_are_a_static_allowlist_of_three(self):
        self.assertEqual([t.provider_type for t in CATALOG.provider_types], ["kpmg_workbench", "openai_compatible", "ollama"])

    def test_an_unknown_provider_type_is_refused_whatever_it_looks_like(self):
        for name in ("anthropic", "openai", "custom", "builtins.exec", "os.system", "../plugin", "kpmg_workbench2", "Ollama"):
            with self.subTest(name):
                self.assertIn(problems([corp_connection(provider_type=name)])["connections[0].provider_type"][0], {"unknown_provider_type", "invalid_value"})

    def test_workbench_is_company_endpoint_only_with_no_endpoint_and_the_catalogs_profiles(self):
        self.assertIsNone(problems([workbench_connection()]))
        self.assertEqual(problems([workbench_connection(locality="local")]), {"connections[0].locality": ["locality_not_allowed_for_provider"]})
        self.assertEqual(problems([workbench_connection(locality="cloud")]), {"connections[0].locality": ["locality_not_allowed_for_provider"]})
        self.assertEqual(problems([workbench_connection(endpoint=CORP_ENDPOINT)]), {"connections[0].endpoint": ["not_allowed"]})
        self.assertIsNone(problems([workbench_connection(credential_ref="AP_AI_CRED_WB")]))  # optional: without it the legacy variable is read

    def test_workbench_profiles_are_only_the_approved_ones_with_their_deployment(self):
        gpt41 = [{"profile_id": "workbench-gpt41", "model_id": "gpt-4.1"}]
        self.assertEqual(problems([workbench_connection(profiles=gpt41)]), {"connections[0].profiles[0].profile_id": ["unknown_profile"]})
        wrong = [{"profile_id": "workbench-gpt5", "model_id": "some-other-deployment"}]
        self.assertEqual(problems([workbench_connection(profiles=wrong)]), {"connections[0].profiles[0].model_id": ["model_not_of_profile"]})
        made_up = [{"profile_id": "invented", "model_id": "x"}]
        self.assertEqual(problems([workbench_connection(profiles=made_up)]), {"connections[0].profiles[0].profile_id": ["unknown_profile"]})

    def test_ollama_is_local_only_with_a_loopback_endpoint_and_no_credential(self):
        self.assertIsNone(problems([ollama_connection()]))
        self.assertIsNone(problems([ollama_connection(endpoint="http://localhost:11434")]))
        for locality in ("company_endpoint", "cloud"):
            self.assertEqual(problems([ollama_connection(locality=locality)]), {"connections[0].locality": ["locality_not_allowed_for_provider"]})
        for endpoint in ("http://10.0.0.5:11434", "https://ollama.corp.example.invalid", "http://169.254.169.254"):
            with self.subTest(endpoint):
                self.assertEqual(problems([ollama_connection(endpoint=endpoint)]), {"connections[0].endpoint": ["endpoint_not_loopback"]})
        self.assertEqual(problems([ollama_connection(endpoint=None)]), {"connections[0].endpoint": ["required"]})
        self.assertEqual(problems([ollama_connection(credential_ref="AP_AI_CRED_X")]), {"connections[0].credential_ref": ["not_allowed"]})

    def test_ollama_declares_its_models_and_none_is_downloaded_or_listed(self):
        (connection,) = validate_connections([ollama_connection(profiles=[{"profile_id": "a", "model_id": "m1:7b"}, {"profile_id": "b", "model_id": "m2:13b"}])], CATALOG)
        self.assertEqual([p.model_id for p in connection.profiles], ["m1:7b", "m2:13b"])


class OpenAICompatibleLocalityTests(SimpleTestCase):
    def test_each_locality_accepts_its_own_kind_of_endpoint(self):
        self.assertIsNone(problems([corp_connection(), cloud_connection(), local_openai_connection()]))

    def test_a_local_connection_must_name_this_machine(self):
        self.assertEqual(problems([local_openai_connection(endpoint=CORP_ENDPOINT)]), {"connections[0].endpoint": ["endpoint_locality_mismatch"]})
        self.assertEqual(problems([local_openai_connection(endpoint="https://10.0.0.5/v1")]), {"connections[0].endpoint": ["endpoint_locality_mismatch"]})

    def test_a_company_or_cloud_connection_cannot_be_loopback_or_plain_http(self):
        for locality, ref in (("company_endpoint", "AP_AI_CRED_A"), ("cloud", "AP_AI_CRED_A")):
            with self.subTest(locality, kind="loopback"):
                self.assertEqual(problems([corp_connection(locality=locality, endpoint="http://127.0.0.1:8000/v1", credential_ref=ref)]), {"connections[0].endpoint": ["endpoint_locality_mismatch"]})
                self.assertEqual(problems([corp_connection(locality=locality, endpoint="https://localhost/v1", credential_ref=ref)]), {"connections[0].endpoint": ["endpoint_locality_mismatch"]})
            with self.subTest(locality, kind="http"):
                self.assertEqual(problems([corp_connection(locality=locality, endpoint="http://llm.example.invalid/v1", credential_ref=ref)]), {"connections[0].endpoint": ["plain_http_not_allowed"]})

    def test_a_cloud_connection_cannot_be_a_private_or_metadata_address(self):
        for host in ("10.0.0.5", "192.168.1.1", "169.254.169.254", "[::1]", "[fd00::1]", "100.64.0.1"):
            with self.subTest(host):
                fields = problems([cloud_connection(endpoint=f"https://{host}/v1")])
                self.assertEqual(list(fields), ["connections[0].endpoint"])

    def test_a_company_connection_that_is_an_address_literal_is_judged_against_the_operators_ranges_at_start_up(self):
        self.assertIsNone(problems([corp_connection(endpoint="https://10.1.2.3/v1")]))  # accepted here; the operator's CIDR list decides at start-up (see the composition tests)

    def test_urls_that_carry_credentials_a_query_or_a_fragment_are_refused(self):
        for endpoint in ("https://user:pass@llm.example.invalid/v1", "https://llm.example.invalid/v1?key=abc", "https://llm.example.invalid/v1#x", "https://llm.example.invalid/v1 ", "javascript:alert(1)", "llm.example.invalid", "file:///etc/passwd", "https://"):
            with self.subTest(endpoint):
                self.assertEqual(problems([corp_connection(endpoint=endpoint)]), {"connections[0].endpoint": ["invalid_endpoint"]})

    def test_an_unknown_locality_is_refused(self):
        self.assertEqual(problems([corp_connection(locality="anywhere")]), {"connections[0].locality": ["locality_not_allowed_for_provider"]})

    def test_the_endpoint_is_required_and_the_credential_is_required_unless_local(self):
        self.assertEqual(problems([corp_connection(endpoint=None)]), {"connections[0].endpoint": ["required"]})
        self.assertEqual(problems([corp_connection(credential_ref=None)]), {"connections[0].credential_ref": ["required"]})
        self.assertEqual(problems([cloud_connection(credential_ref=None)]), {"connections[0].credential_ref": ["required"]})
        self.assertIsNone(problems([local_openai_connection(credential_ref=None)]))
        self.assertIsNone(problems([local_openai_connection(credential_ref="AP_AI_CRED_LOCAL")]))


class CredentialReferenceTests(SimpleTestCase):
    def test_a_reference_must_name_an_ap_ai_cred_variable(self):
        self.assertEqual(CREDENTIAL_REF_PREFIX, "AP_AI_CRED_")
        for ref in ("AP_SECRET_KEY", "AP_AI_OPENAI_API_KEY", "AP_AI_WORKBENCH_API_KEY", "DATABASE_URL", "AP_DB_PASSWORD", "PATH", "AP_AI_CRED", "ap_ai_cred_x", "AP_AI_CRED_$(x)", "AP_AI_CRED_A B"):
            with self.subTest(ref):
                self.assertEqual(problems([corp_connection(credential_ref=ref)]), {"connections[0].credential_ref": ["invalid_value"]})

    def test_a_valid_reference_is_stored_as_a_name_only(self):
        (connection,) = validate_connections([corp_connection()], CATALOG)
        self.assertEqual(connection.credential_ref, "AP_AI_CRED_CORP_LLM")
        self.assertNotIn(SECRET_A, repr(connection))


class SelectionTests(SimpleTestCase):
    def setUp(self):
        self.connections = all_connections()

    def test_a_connection_and_one_of_its_profiles_are_selected_and_the_provider_type_is_derived(self):
        values = select(selection(), self.connections)
        self.assertEqual((values.connection_id, values.provider_id, values.profile_id, values.policy_mode), ("corp-llm", "openai_compatible", "corp-small", "company_endpoint"))

    def test_the_model_is_chosen_by_the_profile_and_two_profiles_of_one_connection_are_distinct(self):
        small = select(selection(profile_id="corp-small"), self.connections)
        large = select(selection(profile_id="corp-large"), self.connections)
        self.assertEqual((small.profile_id, large.profile_id), ("corp-small", "corp-large"))

    def test_an_explicit_provider_id_must_match_the_connections_type(self):
        self.assertEqual(select(selection(provider_id="openai_compatible"), self.connections).provider_id, "openai_compatible")
        self.assertEqual(select(selection(provider_id="ollama"), self.connections), {"provider_id": ["provider_not_of_connection"]})
        self.assertEqual(select(selection(provider_id="kpmg_workbench"), self.connections), {"provider_id": ["provider_not_of_connection"]})

    def test_an_unknown_connection_is_refused(self):
        self.assertEqual(select(selection(connection_id="nope"), self.connections), {"connection_id": ["unknown_connection"]})
        self.assertEqual(select(selection(connection_id="corp-llm"), []), {"connection_id": ["unknown_connection"]})

    def test_a_disabled_connection_cannot_be_selected(self):
        disabled = [workbench_connection(), corp_connection(enabled=False)]
        self.assertEqual(select(selection(), disabled), {"connection_id": ["connection_disabled"]})

    def test_a_profile_of_another_connection_is_refused(self):
        self.assertEqual(select(selection(profile_id="vendor-chat"), self.connections), {"profile_id": ["unknown_profile"]})
        self.assertEqual(select(selection(profile_id="workbench-gpt5"), self.connections), {"profile_id": ["unknown_profile"]})

    def test_a_selected_connection_that_no_longer_passes_the_catalog_is_refused(self):
        stale = ConnectionValues("old", "Old", "retired_provider", "local", None, None, True, ())
        values = {"extraction_mode": "ai", "connection_id": "old", "profile_id": "p", "policy_mode": "local_only", "timeout_seconds": 30}
        with self.assertRaises(AISettingsValidationError) as caught:
            validate_values(values, CATALOG, connections=(stale,))
        self.assertEqual(caught.exception.fields, {"connection_id": ["invalid_connection"]})

    def test_deterministic_mode_accepts_no_connection(self):
        self.assertEqual(select({"extraction_mode": "deterministic", "connection_id": "corp-llm"}, self.connections), {"connection_id": ["not_allowed_in_deterministic_mode"]})
        self.assertEqual(select({"extraction_mode": "deterministic"}, self.connections).extraction_mode, "deterministic")

    def test_the_legacy_selection_by_provider_still_works_with_connections_present(self):
        legacy = select({"extraction_mode": "ai", "provider_id": "kpmg_workbench", "profile_id": "workbench-gpt5", "policy_mode": "company_endpoint", "timeout_seconds": 30}, self.connections)
        self.assertEqual((legacy.connection_id, legacy.provider_id), (None, "kpmg_workbench"))

    def test_a_legacy_selection_cannot_name_a_connection_type_that_is_not_the_environment_provider(self):
        fields = select({"extraction_mode": "ai", "provider_id": "ollama", "profile_id": "llama", "policy_mode": "local_only", "timeout_seconds": 30}, self.connections)
        self.assertEqual(fields, {"provider_id": ["unknown_provider"], "profile_id": ["unknown_profile"]})

    def test_the_timeout_and_lease_rules_apply_to_a_connection_too(self):
        self.assertEqual(select(selection(timeout_seconds=4), self.connections), {"timeout_seconds": ["out_of_range"]})
        self.assertEqual(select(selection(timeout_seconds=50), self.connections, lease_seconds=60), {"timeout_seconds": ["exceeds_worker_lease"]})

    def test_a_connection_id_of_the_wrong_type_is_refused(self):
        self.assertEqual(select(selection(connection_id=5), self.connections), {"connection_id": ["invalid_value"]})


class PolicyLocalityTests(SimpleTestCase):
    """The locality-policy combinations. The rule is the gateway's own (``enforce_policy``); the catalog asks it, so there is one source."""

    def setUp(self):
        self.connections = all_connections()

    def test_the_catalog_lists_the_modes_the_gateway_permits_per_locality(self):
        self.assertEqual(
            dict(CATALOG.locality_policies),
            {"local": ("local_only", "company_endpoint", "approved_cloud"), "company_endpoint": ("company_endpoint", "approved_cloud"), "cloud": ("approved_cloud",)},
        )

    def test_local_only_reaches_only_a_local_connection(self):
        self.assertEqual(select(selection(connection_id="ollama-local", profile_id="llama", policy_mode="local_only"), self.connections).policy_mode, "local_only")
        self.assertEqual(select(selection(connection_id="local-server", profile_id="local-chat", policy_mode="local_only"), self.connections).policy_mode, "local_only")
        for connection_id, profile in (("corp-llm", "corp-small"), ("vendor", "vendor-chat"), ("workbench", "workbench-gpt5")):
            with self.subTest(connection_id):
                self.assertEqual(select(selection(connection_id=connection_id, profile_id=profile, policy_mode="local_only"), self.connections), {"policy_mode": ["policy_forbids_provider"]})

    def test_company_endpoint_never_reaches_a_cloud_connection(self):
        self.assertEqual(select(selection(connection_id="vendor", profile_id="vendor-chat", policy_mode="company_endpoint"), self.connections), {"policy_mode": ["policy_forbids_provider"]})
        self.assertEqual(select(selection(connection_id="corp-llm", policy_mode="company_endpoint"), self.connections).policy_mode, "company_endpoint")

    def test_a_cloud_connection_needs_approved_cloud(self):
        self.assertEqual(select(selection(connection_id="vendor", profile_id="vendor-chat", policy_mode="approved_cloud"), self.connections).policy_mode, "approved_cloud")

    def test_an_unknown_policy_mode_is_refused(self):
        self.assertEqual(select(selection(policy_mode="open_internet"), self.connections), {"policy_mode": ["invalid_choice"]})

    def test_a_cloud_endpoint_is_never_a_company_endpoint_by_declaration_alone(self):
        # The same URL declared as a company endpoint is accepted as a shape here (the operator's CIDR ranges decide at start-up, see the composition tests) but a
        # host that resolves to a public address is refused by the guarded transport; the declaration cannot change that.
        self.assertIsNone(problems([corp_connection(endpoint=CLOUD_ENDPOINT)]))
