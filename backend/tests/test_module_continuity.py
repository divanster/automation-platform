"""Pure-domain tests (standard-library unittest, no Django, no database) for the namespace continuity rule (owner decision: ownership
is preserved across releases). The history is a plain mapping supplied by the caller; nothing is stored here.
"""

import unittest
from types import MappingProxyType

from apps.modules.continuity import check_namespace_continuity, next_namespace_history
from apps.modules.errors import RegistryError
from apps.modules.registry import ModuleRegistry

from .module_support import ContractTestCase, dep, intake_like_manifest, named


def registry_of(*manifests) -> ModuleRegistry:
    return ModuleRegistry.build(manifests)


class ContinuityCheckTests(ContractTestCase):
    def test_an_unchanged_release_passes(self):
        registry = registry_of(named("alpha_mod", extra_namespaces=("legacy_ns",)), named("beta_mod"))
        check_namespace_continuity({"alpha_mod": "alpha_mod", "legacy_ns": "alpha_mod", "beta_mod": "beta_mod"}, registry)

    def test_a_first_release_with_no_history_passes(self):
        check_namespace_continuity({}, registry_of(named("alpha_mod")))
        check_namespace_continuity(MappingProxyType({}), registry_of())

    def test_new_namespaces_and_new_modules_are_allowed(self):
        history = {"alpha_mod": "alpha_mod"}
        check_namespace_continuity(history, registry_of(named("alpha_mod", extra_namespaces=("added_ns",)), named("gamma_mod")))

    def test_a_namespace_handed_to_another_module_is_refused(self):
        history = {"alpha_mod": "alpha_mod", "legacy_ns": "alpha_mod"}
        moved = registry_of(named("alpha_mod"), named("beta_mod", extra_namespaces=("legacy_ns",)))
        error = self.assertCode("namespace_ownership_changed", RegistryError, check_namespace_continuity, history, moved)
        self.assertIn("legacy_ns", str(error))
        self.assertIn("alpha_mod", str(error))
        self.assertIn("beta_mod", str(error))

    def test_a_released_namespace_is_refused_even_though_nothing_claims_it(self):
        history = {"alpha_mod": "alpha_mod", "legacy_ns": "alpha_mod"}
        error = self.assertCode("namespace_released", RegistryError, check_namespace_continuity, history, registry_of(named("alpha_mod")))
        self.assertIn("legacy_ns", str(error))

    def test_a_module_removed_from_the_release_releases_its_namespaces(self):
        self.assertCode("namespace_released", RegistryError, check_namespace_continuity, {"alpha_mod": "alpha_mod", "beta_mod": "beta_mod"}, registry_of(named("alpha_mod")))

    def test_renaming_a_module_id_is_a_release_of_the_old_id(self):
        self.assertCode("namespace_released", RegistryError, check_namespace_continuity, {"alpha_mod": "alpha_mod"}, registry_of(named("alpha_renamed")))

    def test_a_module_cannot_take_over_the_namespace_of_a_module_that_left(self):
        history = {"alpha_mod": "alpha_mod", "beta_mod": "beta_mod"}
        takeover = registry_of(named("alpha_mod", extra_namespaces=("beta_mod",)), named("gamma_mod"))  # beta_mod is gone; alpha claims its id
        # The namespace is not released (someone claims it), it has CHANGED OWNER, which is the more specific refusal.
        error = self.assertCode("namespace_ownership_changed", RegistryError, check_namespace_continuity, history, takeover)
        self.assertIn("beta_mod", str(error))

    def test_the_request_intake_legacy_namespace_must_keep_being_declared(self):
        previous = {"intake": "request_intake", "request_intake": "request_intake"}
        check_namespace_continuity(previous, registry_of(intake_like_manifest()))
        without_legacy = intake_like_manifest(extra_namespaces=(), permissions=(), role_grants=())
        self.assertCode("namespace_released", RegistryError, check_namespace_continuity, previous, registry_of(without_legacy))

    def test_the_first_problem_follows_namespace_order_and_so_is_deterministic(self):
        history = {"zzz_ns": "alpha_mod", "aaa_ns": "alpha_mod", "alpha_mod": "alpha_mod"}
        error = self.assertCode("namespace_released", RegistryError, check_namespace_continuity, history, registry_of(named("alpha_mod")))
        self.assertIn("aaa_ns", str(error))

    def test_dependencies_between_modules_do_not_change_ownership(self):
        history = {"alpha_mod": "alpha_mod", "beta_mod": "beta_mod"}
        check_namespace_continuity(history, registry_of(named("alpha_mod"), named("beta_mod", dependencies=(dep("alpha_mod"),))))

    def test_a_malformed_history_is_refused(self):
        registry = registry_of(named("alpha_mod"))
        for bad in (None, [], "alpha_mod", [("a", "b")], {"": "alpha_mod"}, {"alpha_mod": ""}, {"alpha_mod": None}, {5: "alpha_mod"}, {"alpha_mod": 5}):
            with self.subTest(history=bad):
                self.assertCode("invalid_namespace_history", RegistryError, check_namespace_continuity, bad, registry)

    def test_only_a_registry_is_accepted(self):
        for bad in (None, [], (named("alpha_mod"),)):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    check_namespace_continuity({}, bad)
                with self.assertRaises(TypeError):
                    next_namespace_history({}, bad)

    def test_the_check_changes_nothing(self):
        history = {"alpha_mod": "alpha_mod"}
        before = dict(history)
        check_namespace_continuity(history, registry_of(named("alpha_mod"), named("beta_mod")))
        self.assertEqual(history, before)


class NextHistoryTests(ContractTestCase):
    def test_the_next_history_is_the_old_claims_plus_the_new_ones_sorted(self):
        previous = {"zzz_ns": "alpha_mod", "alpha_mod": "alpha_mod"}
        registry = registry_of(named("alpha_mod", extra_namespaces=("zzz_ns", "new_ns")), named("beta_mod"))
        history = next_namespace_history(previous, registry)
        self.assertEqual(dict(history), {"alpha_mod": "alpha_mod", "beta_mod": "beta_mod", "new_ns": "alpha_mod", "zzz_ns": "alpha_mod"})
        self.assertEqual(list(history), sorted(history))

    def test_the_result_is_read_only_and_the_input_is_not_changed(self):
        previous = {"alpha_mod": "alpha_mod"}
        history = next_namespace_history(previous, registry_of(named("alpha_mod"), named("beta_mod")))
        self.assertIsInstance(history, MappingProxyType)
        with self.assertRaises(TypeError):
            history["beta_mod"] = "alpha_mod"
        self.assertEqual(previous, {"alpha_mod": "alpha_mod"})

    def test_it_refuses_exactly_what_the_check_refuses(self):
        self.assertCode("namespace_released", RegistryError, next_namespace_history, {"gone_ns": "alpha_mod"}, registry_of(named("alpha_mod")))
        self.assertCode("namespace_ownership_changed", RegistryError, next_namespace_history, {"beta_mod": "alpha_mod"}, registry_of(named("alpha_mod"), named("beta_mod")))

    def test_a_namespace_once_remembered_stays_remembered_across_several_releases(self):
        release_1 = registry_of(named("alpha_mod", extra_namespaces=("legacy_ns",)))
        history = next_namespace_history({}, release_1)
        release_2 = registry_of(named("alpha_mod", extra_namespaces=("legacy_ns", "second_ns")))
        history = next_namespace_history(history, release_2)
        self.assertEqual(history["legacy_ns"], "alpha_mod")
        release_3_dropping = registry_of(named("alpha_mod", extra_namespaces=("second_ns",)))
        self.assertCode("namespace_released", RegistryError, next_namespace_history, history, release_3_dropping)


if __name__ == "__main__":
    unittest.main()
