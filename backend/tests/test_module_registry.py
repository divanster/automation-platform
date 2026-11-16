"""Pure-domain tests (standard-library unittest, no Django, no database) for the static module registry."""

import hashlib
import itertools
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import MappingProxyType

from apps.modules.errors import RegistryError
from apps.modules.manifest import (
    ApiContribution,
    AuditActionDeclaration,
    ModuleManifest,
    PermissionDeclaration,
    TaskHandlerDeclaration,
    UiContribution,
    UpdateMetadata,
    WorkflowDeclaration,
)
from apps.modules.registry import CORE_CAPABILITIES, CORE_PERMISSIONS, FUTURE_CAPABILITIES, ModuleRegistry
from apps.modules.versions import Version

from .module_support import ContractTestCase, dep, full_manifest, named

BACKEND = Path(__file__).resolve().parent.parent


def build(*manifests, **kwargs) -> ModuleRegistry:
    return ModuleRegistry.build(manifests, **kwargs)


def shared(module_id: str, **overrides) -> ModuleManifest:
    """A module that also owns the namespace ``shared`` (to provoke clashes between modules that both use it)."""
    return named(module_id, extra_namespaces=("shared",), **overrides)


class ConstructionTests(ContractTestCase):
    def test_an_empty_registry_is_valid(self):
        registry = build()
        self.assertEqual(len(registry), 0)
        self.assertEqual(registry.modules, ())
        self.assertEqual(registry.module_ids, ())
        self.assertEqual(registry.fingerprint, hashlib.sha256(b'{"modules":[],"registry_format":1}').hexdigest())

    def test_a_registry_can_be_built_from_a_tuple_a_list_or_a_generator(self):
        modules = [named("alpha_mod"), named("beta_mod")]
        a, b, c = ModuleRegistry.build(tuple(modules)), ModuleRegistry.build(modules), ModuleRegistry.build(m for m in modules)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(b.fingerprint, c.fingerprint)

    def test_only_manifest_objects_are_accepted_never_names_paths_or_classes(self):
        for bad in ("apps.intake.module", b"apps", None, 5, {"module_id": "x"}):
            with self.subTest(bad=bad):
                self.assertCode("invalid_manifest_object", RegistryError, ModuleRegistry.build, bad)
        for entry in ("apps.intake.module.MANIFEST", ModuleManifest, {"module_id": "x"}, sys, None, 5):
            with self.subTest(entry=entry):
                self.assertCode("invalid_manifest_object", RegistryError, ModuleRegistry.build, [named("alpha_mod"), entry])

    def test_a_registry_is_created_only_by_build(self):
        with self.assertRaises(TypeError):
            ModuleRegistry()
        with self.assertRaises(TypeError):
            ModuleRegistry(_modules=())

    def test_read_access(self):
        a, b = named("alpha_mod"), named("beta_mod", dependencies=(dep("alpha_mod"),))
        registry = build(b, a)
        self.assertIs(registry.get("alpha_mod"), a)
        self.assertIsNone(registry.get("missing_mod"))
        self.assertIn("beta_mod", registry)
        self.assertNotIn("missing_mod", registry)
        self.assertEqual([m.module_id for m in registry], ["alpha_mod", "beta_mod"])
        self.assertEqual(registry.core_api_version, Version(1, 0, 0))
        self.assertIn("2 module(s)", repr(registry))

    def test_the_contribution_maps_name_the_owning_module(self):
        registry = build(full_manifest("full_module"), named("other_mod", capabilities_required=("task.enqueue",)))
        self.assertEqual(registry.task_kinds, {"full_module.run": "full_module"})
        self.assertEqual(registry.permissions, {"full_module.do_it": "full_module", "legacy_ns.view_it": "full_module"})
        self.assertEqual(registry.audit_actions, {"full_module.did_it": "full_module"})
        self.assertEqual(registry.workflows, {"full_module": "full_module"})
        self.assertEqual(registry.api_routes, {"full-module": "full_module"})
        self.assertEqual(registry.ui_contributions, {"full_module.nav": "full_module", "full_module.screen": "full_module"})
        self.assertEqual(registry.capability_providers, {"full_module.service": "full_module"})
        self.assertEqual(list(registry.permissions), sorted(registry.permissions))  # sorted keys: no incidental ordering


class ImmutabilityTests(ContractTestCase):
    def test_no_attribute_can_be_set_or_deleted(self):
        registry = build(named("alpha_mod"))
        for name in ("_modules", "modules", "_fingerprint", "fingerprint", "brand_new", "_by_id"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(registry, name, ())
                with self.assertRaises(AttributeError):
                    delattr(registry, name)

    def test_there_is_no_instance_dictionary_to_smuggle_state_into(self):
        self.assertFalse(hasattr(build(), "__dict__"))

    def test_collections_are_read_only(self):
        registry = build(full_manifest("full_module"))
        self.assertIsInstance(registry.modules, tuple)
        for mapping in (registry.task_kinds, registry.permissions, registry.audit_actions, registry.workflows, registry.api_routes, registry.ui_contributions, registry.capability_providers):
            self.assertIsInstance(mapping, MappingProxyType)
            with self.assertRaises(TypeError):
                mapping["x"] = "y"
        with self.assertRaises(AttributeError):
            registry.modules.append(named("alpha_mod"))

    def test_changing_the_input_after_building_changes_nothing(self):
        modules = [named("alpha_mod"), named("beta_mod")]
        registry = build(*modules)
        before = (registry.module_ids, registry.fingerprint)
        modules.append(named("gamma_mod"))
        modules.pop(0)
        self.assertEqual((registry.module_ids, registry.fingerprint), before)


class DeterminismTests(ContractTestCase):
    def test_the_result_does_not_depend_on_the_order_of_the_input(self):
        modules = [named("delta_mod"), named("alpha_mod"), named("charlie_mod", dependencies=(dep("delta_mod"),)), named("bravo_mod")]
        results = set()
        for permutation in itertools.permutations(modules):
            registry = ModuleRegistry.build(permutation)
            results.add((registry.module_ids, registry.fingerprint))
        self.assertEqual(len(results), 1)
        ((ids, _),) = results
        self.assertEqual(ids, ("alpha_mod", "bravo_mod", "delta_mod", "charlie_mod"))

    def test_independent_modules_are_ordered_by_id(self):
        self.assertEqual(build(named("zulu_mod"), named("alpha_mod"), named("mike_mod")).module_ids, ("alpha_mod", "mike_mod", "zulu_mod"))

    def test_a_dependency_always_comes_before_its_dependents(self):
        chain = build(named("cccc_mod", dependencies=(dep("bbbb_mod"),)), named("bbbb_mod", dependencies=(dep("aaaa_mod"),)), named("aaaa_mod"))
        self.assertEqual(chain.module_ids, ("aaaa_mod", "bbbb_mod", "cccc_mod"))
        diamond = build(
            named("dddd_mod", dependencies=(dep("bbbb_mod"), dep("cccc_mod"))),
            named("cccc_mod", dependencies=(dep("aaaa_mod"),)),
            named("bbbb_mod", dependencies=(dep("aaaa_mod"),)),
            named("aaaa_mod"),
        )
        self.assertEqual(diamond.module_ids, ("aaaa_mod", "bbbb_mod", "cccc_mod", "dddd_mod"))

    def test_ties_break_by_id_among_the_modules_that_are_ready_together(self):
        # "aaa_top" depends on "zzz_base", so it waits; "mmm_mid" and "zzz_base" are ready first and go in id order.
        registry = build(named("aaa_top", dependencies=(dep("zzz_base"),)), named("zzz_base"), named("mmm_mid"))
        self.assertEqual(registry.module_ids, ("mmm_mid", "zzz_base", "aaa_top"))

    def test_the_fingerprint_is_the_sha256_of_the_canonical_ordered_manifests(self):
        a, b = full_manifest("full_module"), named("other_mod", dependencies=())
        registry = build(b, a)
        document = {"registry_format": 1, "modules": [m.to_dict() for m in registry.modules]}
        expected = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(registry.fingerprint, expected)

    def test_the_fingerprint_changes_when_any_module_changes_and_not_otherwise(self):
        base = build(named("alpha_mod"), named("beta_mod")).fingerprint
        self.assertEqual(base, build(named("beta_mod"), named("alpha_mod")).fingerprint)
        self.assertNotEqual(base, build(named("alpha_mod"), named("beta_mod", module_version="1.0.1")).fingerprint)
        self.assertNotEqual(base, build(named("alpha_mod")).fingerprint)
        self.assertNotEqual(base, build(named("alpha_mod"), named("beta_mod"), named("gamma_mod")).fingerprint)

    def test_the_fingerprint_does_not_change_with_the_core_values_the_registry_was_built_for(self):
        modules = (named("alpha_mod", core_api_compatibility=">=1.0.0,<3.0.0"),)
        self.assertEqual(build(*modules).fingerprint, build(*modules, core_api_version=Version(2, 0, 0)).fingerprint)

    def test_the_same_modules_give_the_same_registry_in_other_processes_with_other_hash_seeds(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from apps.modules import *;"
            "mk = lambda i, **k: ModuleManifest(manifest_version=1, module_id=i, module_kind='business', module_version='1.2.3', core_api_compatibility='>=1.0.0,<2.0.0', license_feature='mod.'+i, **k);"
            "ms = [mk('zulu_mod'), mk('alpha_mod', capabilities_required=('task.enqueue',)), mk('mike_mod', dependencies=(ModuleDependency('zulu_mod', '>=1.0.0'),), extra_namespaces=('ns_q', 'ns_p', 'ns_r'))];"
            "r = ModuleRegistry.build(ms); print(r.fingerprint, ','.join(r.module_ids))"
        )
        outputs = set()
        for seed in ("0", "1", "2", "12345", "random"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            done = subprocess.run([sys.executable, "-c", script, str(BACKEND)], capture_output=True, text=True, env=env, cwd=BACKEND, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            outputs.add(done.stdout.strip())
        self.assertEqual(len(outputs), 1, outputs)

    def test_the_first_error_follows_the_documented_check_order(self):
        # Two problems at once: an incompatible Core API and a missing dependency. Core compatibility is checked first.
        bad = named("alpha_mod", core_api_compatibility=">=2.0.0,<3.0.0", dependencies=(dep("missing_mod"),))
        self.assertCode("incompatible_core_api", RegistryError, build, bad)
        # Duplicate ids are reported before anything else.
        self.assertCode("duplicate_module_id", RegistryError, build, named("alpha_mod"), named("alpha_mod", core_api_compatibility=">=2.0.0,<3.0.0"))


class CompatibilityTests(ContractTestCase):
    def test_core_api_compatibility(self):
        self.assertEqual(len(build(named("alpha_mod"))), 1)
        self.assertCode("incompatible_core_api", RegistryError, build, named("alpha_mod", core_api_compatibility=">=2.0.0,<3.0.0"))
        self.assertCode("incompatible_core_api", RegistryError, build, named("alpha_mod", core_api_compatibility=">1.0.0,<2.0.0"))

    def test_the_core_version_decides_at_the_boundaries(self):
        module = named("alpha_mod")  # >=1.0.0,<2.0.0
        self.assertEqual(len(build(module, core_api_version=Version(1, 9, 9))), 1)
        self.assertCode("incompatible_core_api", RegistryError, build, module, core_api_version=Version(2, 0, 0))
        self.assertCode("incompatible_core_api", RegistryError, build, module, core_api_version=Version(0, 9, 0))

    def test_update_protocol_metadata_this_core_cannot_honor_is_refused(self):
        needs_one = named("alpha_mod", update=UpdateMetadata(minimum_updater_protocol=1))
        self.assertCode("unsupported_updater_protocol", RegistryError, build, needs_one)
        self.assertEqual(len(build(needs_one, updater_protocol_version=1)), 1)
        self.assertCode("unsupported_updater_protocol", RegistryError, build, named("alpha_mod", update=UpdateMetadata(minimum_updater_protocol=3)), updater_protocol_version=2)
        self.assertEqual(len(build(named("alpha_mod", update=UpdateMetadata(data_schema_version=4)))), 1)  # a data schema version alone is metadata

    def test_independent_module_versions_are_just_data(self):
        registry = build(named("alpha_mod", module_version="7.3.1"), named("beta_mod", module_version="0.0.1"))
        self.assertEqual(registry.get("alpha_mod").module_version, "7.3.1")
        self.assertEqual(registry.get("beta_mod").module_version, "0.0.1")


class DependencyTests(ContractTestCase):
    def test_a_missing_dependency_is_refused(self):
        error = self.assertCode("missing_dependency", RegistryError, build, named("alpha_mod", dependencies=(dep("ghost_mod"),)))
        self.assertIn("ghost_mod", str(error))

    def test_an_incompatible_dependency_version_is_refused(self):
        base = named("base_mod", module_version="1.4.0")
        self.assertEqual(len(build(base, named("user_mod", dependencies=(dep("base_mod", ">=1.0.0,<2.0.0"),)))), 2)
        self.assertEqual(len(build(base, named("user_mod", dependencies=(dep("base_mod", "==1.4.0"),)))), 2)
        for needed in (">=2.0.0", ">=1.5.0", "<1.4.0", ">1.4.0", "==1.3.9"):
            with self.subTest(needed=needed):
                self.assertCode("incompatible_dependency_version", RegistryError, build, base, named("user_mod", dependencies=(dep("base_mod", needed),)))

    def test_dependency_cycles_are_refused(self):
        a = named("aaaa_mod", dependencies=(dep("bbbb_mod"),))
        b = named("bbbb_mod", dependencies=(dep("aaaa_mod"),))
        error = self.assertCode("dependency_cycle", RegistryError, build, a, b)
        self.assertIn("aaaa_mod", str(error))
        self.assertIn("bbbb_mod", str(error))
        three = [named("aaaa_mod", dependencies=(dep("bbbb_mod"),)), named("bbbb_mod", dependencies=(dep("cccc_mod"),)), named("cccc_mod", dependencies=(dep("aaaa_mod"),))]
        self.assertCode("dependency_cycle", RegistryError, build, *three)

    def test_a_cycle_is_reported_even_with_unrelated_and_dependent_modules_present(self):
        modules = [
            named("aaaa_mod", dependencies=(dep("bbbb_mod"),)),
            named("bbbb_mod", dependencies=(dep("aaaa_mod"),)),
            named("free_mod"),
            named("downstream_mod", dependencies=(dep("aaaa_mod"),)),
        ]
        error = self.assertCode("dependency_cycle", RegistryError, build, *modules)
        self.assertNotIn("free_mod", str(error))

    def test_a_module_without_cycles_but_with_a_shared_dependency_is_fine(self):
        registry = build(named("base_mod"), named("left_mod", dependencies=(dep("base_mod"),)), named("right_mod", dependencies=(dep("base_mod"),)))
        self.assertEqual(registry.module_ids, ("base_mod", "left_mod", "right_mod"))


class ClashTests(ContractTestCase):
    """Two modules may not claim the same identifier. Each clash is an error that names both modules, never a last-one-wins."""

    def test_duplicate_module_ids(self):
        error = self.assertCode("duplicate_module_id", RegistryError, build, named("alpha_mod"), named("alpha_mod"))
        self.assertIn("alpha_mod", str(error))

    def test_duplicate_task_kinds(self):
        a = shared("alpha_mod", task_handlers=(TaskHandlerDeclaration("shared.run"),))
        b = shared("beta_mod", task_handlers=(TaskHandlerDeclaration("shared.run"),))
        error = self.assertCode("duplicate_task_kind", RegistryError, build, a, b)
        self.assertIn("alpha_mod", str(error))
        self.assertIn("beta_mod", str(error))

    def test_duplicate_permissions(self):
        a = shared("alpha_mod", permissions=(PermissionDeclaration("shared.do_it"),))
        b = shared("beta_mod", permissions=(PermissionDeclaration("shared.do_it", "own"),))
        self.assertCode("duplicate_permission", RegistryError, build, a, b)

    def test_duplicate_audit_actions(self):
        a = shared("alpha_mod", audit_actions=(AuditActionDeclaration("shared.did_it"),))
        b = shared("beta_mod", audit_actions=(AuditActionDeclaration("shared.did_it"),))
        self.assertCode("duplicate_audit_action", RegistryError, build, a, b)

    def test_duplicate_workflow_keys(self):
        a = shared("alpha_mod", workflows=(WorkflowDeclaration("shared.flow", 1),))
        b = shared("beta_mod", workflows=(WorkflowDeclaration("shared.flow", 2),))
        self.assertCode("duplicate_workflow", RegistryError, build, a, b)

    def test_duplicate_api_routes(self):
        a = named("alpha_mod", api_contributions=(ApiContribution("requests"),))
        b = named("beta_mod", api_contributions=(ApiContribution("requests"),))
        self.assertCode("duplicate_api_route", RegistryError, build, a, b)

    def test_duplicate_ui_contribution_ids(self):
        a = shared("alpha_mod", ui_contributions=(UiContribution("shared.home", "route", "/alpha", "Alpha"),))
        b = shared("beta_mod", ui_contributions=(UiContribution("shared.home", "route", "/beta", "Beta"),))
        self.assertCode("duplicate_ui_contribution", RegistryError, build, a, b)

    def test_two_modules_cannot_serve_the_same_ui_route_but_may_share_a_navigation_path(self):
        a = named("alpha_mod", ui_contributions=(UiContribution("alpha_mod.page", "route", "/same", "A"),))
        b = named("beta_mod", ui_contributions=(UiContribution("beta_mod.page", "route", "/same", "B"),))
        self.assertCode("duplicate_ui_route", RegistryError, build, a, b)
        a_nav = named("alpha_mod", ui_contributions=(UiContribution("alpha_mod.nav", "navigation", "/same", "A"),))
        b_nav = named("beta_mod", ui_contributions=(UiContribution("beta_mod.nav", "navigation", "/same", "B"),))
        self.assertEqual(len(build(a_nav, b_nav)), 2)

    def test_duplicate_provided_capabilities(self):
        a = shared("alpha_mod", capabilities_provided=("shared.service",))
        b = shared("beta_mod", capabilities_provided=("shared.service",))
        self.assertCode("duplicate_capability", RegistryError, build, a, b)

    def test_namespaces_belong_to_one_module(self):
        self.assertCode("duplicate_namespace", RegistryError, build, shared("alpha_mod"), shared("beta_mod"))
        # A module's own id is a namespace too: another module may not claim it as an extra namespace.
        self.assertCode("duplicate_namespace", RegistryError, build, named("alpha_mod", extra_namespaces=("beta_mod",)), named("beta_mod"))

    def test_the_registry_exposes_who_owns_each_namespace_read_only(self):
        registry = build(named("alpha_mod", extra_namespaces=("legacy_ns", "other_ns")), named("beta_mod"))
        self.assertEqual(registry.namespace_owners, {"alpha_mod": "alpha_mod", "legacy_ns": "alpha_mod", "other_ns": "alpha_mod", "beta_mod": "beta_mod"})
        self.assertEqual(list(registry.namespace_owners), sorted(registry.namespace_owners))
        self.assertIsInstance(registry.namespace_owners, MappingProxyType)
        with self.assertRaises(TypeError):
            registry.namespace_owners["legacy_ns"] = "beta_mod"  # ownership cannot be reassigned
        with self.assertRaises(AttributeError):
            registry.namespace_owners = {}

    def test_ownership_does_not_depend_on_the_order_or_the_dependencies_between_modules(self):
        a = named("alpha_mod", extra_namespaces=("legacy_ns",))
        b = named("beta_mod", dependencies=(dep("alpha_mod"),))
        self.assertEqual(build(a, b).namespace_owners, build(b, a).namespace_owners)

    def test_a_dependent_cannot_claim_a_namespace_its_dependency_owns(self):
        owner = named("alpha_mod", extra_namespaces=("legacy_ns",))
        for claim in ("legacy_ns", "alpha_mod"):  # an extra namespace of the dependency, and the dependency's own id
            with self.subTest(claim=claim):
                dependent = named("beta_mod", dependencies=(dep("alpha_mod"),), extra_namespaces=(claim,))
                self.assertCode("duplicate_namespace", RegistryError, build, owner, dependent)

    def test_nothing_is_inherited_through_a_chain_of_dependencies(self):
        a = named("alpha_mod", extra_namespaces=("legacy_ns",))
        b = named("beta_mod", dependencies=(dep("alpha_mod"),))
        c = named("gamma_mod", dependencies=(dep("beta_mod"),))
        registry = build(a, b, c)
        self.assertEqual(registry.namespace_owners["legacy_ns"], "alpha_mod")
        self.assertEqual([ns for ns, owner in registry.namespace_owners.items() if owner == "gamma_mod"], ["gamma_mod"])

    def test_a_module_cannot_squat_another_modules_id_even_when_the_other_is_registered_later(self):
        squatter = named("alpha_mod", extra_namespaces=("beta_mod",))
        self.assertEqual(build(squatter).namespace_owners["beta_mod"], "alpha_mod")  # allowed while nobody else owns it...
        self.assertCode("duplicate_namespace", RegistryError, build, squatter, named("beta_mod"))  # ...refused the moment the real owner appears

    def test_the_most_specific_code_wins_over_the_namespace_clash(self):
        # Both own "shared" AND declare the same task kind: the identifier that collided is named, not just the namespace.
        a = shared("alpha_mod", task_handlers=(TaskHandlerDeclaration("shared.run"),))
        b = shared("beta_mod", task_handlers=(TaskHandlerDeclaration("shared.run"),))
        self.assertCode("duplicate_task_kind", RegistryError, build, a, b)

    def test_modules_with_disjoint_identifiers_register_together(self):
        a = full_manifest("full_module")
        b = full_manifest("other_full", extra_namespaces=("other_legacy",), permissions=(PermissionDeclaration("other_full.do_it"),), api_contributions=(ApiContribution("other-full"),))
        self.assertEqual(len(build(a, b)), 2)

    def test_nothing_is_silently_overwritten_even_when_the_clashing_declarations_are_identical(self):
        decl = TaskHandlerDeclaration("shared.run")
        self.assertCode("duplicate_task_kind", RegistryError, build, shared("alpha_mod", task_handlers=(decl,)), shared("beta_mod", task_handlers=(decl,)))


class CapabilityTests(ContractTestCase):
    def test_core_capabilities_may_be_required(self):
        registry = build(named("alpha_mod", capabilities_required=tuple(sorted(CORE_CAPABILITIES))))
        self.assertEqual(registry.capability_providers, {})  # Core capabilities are not module-provided

    def test_an_unknown_capability_is_refused(self):
        self.assertCode("missing_capability", RegistryError, build, named("alpha_mod", capabilities_required=("nothing.provides",)))

    def test_capabilities_that_are_planned_but_not_built_are_refused_with_their_own_code(self):
        for capability in sorted(FUTURE_CAPABILITIES):
            with self.subTest(capability=capability):
                self.assertCode("capability_not_yet_available", RegistryError, build, named("alpha_mod", capabilities_required=(capability,)))

    def test_the_core_capability_set_is_a_parameter_so_a_smaller_core_refuses_more(self):
        module = named("alpha_mod", capabilities_required=("task.enqueue",))
        self.assertEqual(len(build(module)), 1)
        self.assertCode("missing_capability", RegistryError, build, module, core_capabilities=frozenset())

    def test_a_capability_provided_by_a_dependency_is_available(self):
        provider = named("provider_mod", capabilities_provided=("provider_mod.service",))
        consumer = named("consumer_mod", dependencies=(dep("provider_mod"),), capabilities_required=("provider_mod.service",))
        registry = build(consumer, provider)
        self.assertEqual(registry.capability_providers, {"provider_mod.service": "provider_mod"})
        self.assertEqual(registry.module_ids, ("provider_mod", "consumer_mod"))

    def test_a_provider_that_is_not_a_direct_dependency_is_refused(self):
        provider = named("provider_mod", capabilities_provided=("provider_mod.service",))
        consumer = named("consumer_mod", capabilities_required=("provider_mod.service",))
        self.assertCode("capability_provider_not_dependency", RegistryError, build, provider, consumer)
        # Depending on it only indirectly is not enough either: the requirement must be explicit.
        middle = named("middle_mod", dependencies=(dep("provider_mod"),))
        far = named("far_mod", dependencies=(dep("middle_mod"),), capabilities_required=("provider_mod.service",))
        self.assertCode("capability_provider_not_dependency", RegistryError, build, provider, middle, far)

    def test_the_direct_dependency_rule_is_strict_in_every_shape(self):
        """Owner decision: a module needing a capability of another module must declare THAT module as a dependency, directly."""
        provider = named("provider_mod", capabilities_provided=("provider_mod.service",))
        needs = ("provider_mod.service",)
        # Depending on something else is not enough.
        other = named("other_mod")
        self.assertCode("capability_provider_not_dependency", RegistryError, build, provider, other, named("consumer_mod", dependencies=(dep("other_mod"),), capabilities_required=needs))
        # Depending on what the provider itself depends on is not enough (reverse direction of the transitive case).
        base = named("base_mod")
        provider_on_base = named("provider_mod", dependencies=(dep("base_mod"),), capabilities_provided=("provider_mod.service",))
        self.assertCode("capability_provider_not_dependency", RegistryError, build, base, provider_on_base, named("consumer_mod", dependencies=(dep("base_mod"),), capabilities_required=needs))
        # Depending on a module that depends on the provider (a transitive dependency) is not enough.
        middle = named("middle_mod", dependencies=(dep("provider_mod"),))
        self.assertCode("capability_provider_not_dependency", RegistryError, build, provider, middle, named("consumer_mod", dependencies=(dep("middle_mod"),), capabilities_required=needs))
        # Naming the provider directly, even next to other dependencies, is accepted.
        accepted = named("consumer_mod", dependencies=(dep("middle_mod"), dep("provider_mod")), capabilities_required=needs)
        self.assertEqual(build(provider, middle, accepted).module_ids, ("provider_mod", "middle_mod", "consumer_mod"))

    def test_the_direct_dependency_rule_does_not_apply_to_core_capabilities(self):
        self.assertEqual(len(build(named("consumer_mod", capabilities_required=("task.enqueue",)))), 1)

    def test_the_rule_is_checked_for_every_required_capability_not_just_the_first(self):
        a = named("aaaa_mod", capabilities_provided=("aaaa_mod.one",))
        b = named("bbbb_mod", capabilities_provided=("bbbb_mod.two",))
        consumer = named("consumer_mod", dependencies=(dep("aaaa_mod"),), capabilities_required=("aaaa_mod.one", "bbbb_mod.two"))
        error = self.assertCode("capability_provider_not_dependency", RegistryError, build, a, b, consumer)
        self.assertIn("bbbb_mod", str(error))

    def test_ui_permission_references(self):
        own = named("alpha_mod", permissions=(PermissionDeclaration("alpha_mod.view"),), ui_contributions=(UiContribution("alpha_mod.nav", "navigation", "/a", "A", "alpha_mod.view"),))
        self.assertEqual(len(build(own)), 1)
        core = named("alpha_mod", ui_contributions=(UiContribution("alpha_mod.nav", "navigation", "/a", "A", "audit.view_auditevent"),))
        self.assertEqual(len(build(core)), 1)
        self.assertIn("audit.view_auditevent", CORE_PERMISSIONS)
        # Another module's permission is not enough, and neither is an unknown one.
        other = named("beta_mod", permissions=(PermissionDeclaration("beta_mod.view"),))
        borrowed = named("alpha_mod", ui_contributions=(UiContribution("alpha_mod.nav", "navigation", "/a", "A", "beta_mod.view"),))
        self.assertCode("undeclared_ui_permission", RegistryError, build, borrowed, other)
        unknown = named("alpha_mod", ui_contributions=(UiContribution("alpha_mod.nav", "navigation", "/a", "A", "alpha_mod.nope"),))
        self.assertCode("undeclared_ui_permission", RegistryError, build, unknown)


class StaticCompositionRootTests(ContractTestCase):
    def test_the_builtin_tuple_is_static_and_empty_in_mod_1a(self):
        from config import modules as root

        self.assertEqual(root.BUILTIN_MODULES, ())
        self.assertIsInstance(root.BUILTIN_MODULES, tuple)
        registry = root.build_registry()
        self.assertEqual(len(registry), 0)
        self.assertEqual(registry.fingerprint, build().fingerprint)

    def test_the_composition_root_can_build_a_registry_from_given_manifests(self):
        from config import modules as root

        self.assertEqual(root.build_registry((named("alpha_mod"),)).module_ids, ("alpha_mod",))
        self.assertCode("duplicate_module_id", RegistryError, root.build_registry, (named("alpha_mod"), named("alpha_mod")))


if __name__ == "__main__":
    unittest.main()
