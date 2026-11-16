"""Pure-domain tests (standard-library unittest, no Django, no database): capability contracts, maintenance hook declarations and
direct dependency inspection. Nothing here executes a capability or a hook: the contract only declares and validates.
"""

import ast
import dataclasses
import itertools
import unittest
from pathlib import Path
from types import MappingProxyType

from apps.modules.errors import ManifestError, RegistryError
from apps.modules.manifest import RESERVED_NAMESPACES, MaintenanceHookDeclaration
from apps.modules.registry import CORE_CAPABILITIES, FUTURE_CAPABILITIES, ModuleRegistry, RegisteredHook

from .module_support import ContractTestCase, dep, full_manifest, intake_like_manifest, manifest, named

MODULES_DIR = Path(__file__).resolve().parent.parent / "apps" / "modules"


def registry_of(*manifests) -> ModuleRegistry:
    return ModuleRegistry.build(manifests)


def shared(module_id: str, **overrides):
    return named(module_id, extra_namespaces=("shared",), **overrides)


class CapabilityVocabularyTests(ContractTestCase):
    def test_core_provides_exactly_these_five_capabilities(self):
        self.assertEqual(CORE_CAPABILITIES, frozenset({"task.enqueue", "case.run", "audit.record", "revision.approval", "permissions.check"}))
        self.assertIsInstance(CORE_CAPABILITIES, frozenset)

    def test_each_core_capability_may_be_required(self):
        for capability in sorted(CORE_CAPABILITIES):
            with self.subTest(capability=capability):
                self.assertEqual(len(registry_of(named("alpha_mod", capabilities_required=(capability,)))), 1)

    def test_permissions_check_is_a_core_capability_that_a_module_cannot_provide(self):
        self.assertIn("permissions.check", CORE_CAPABILITIES)
        self.assertIn("permissions", RESERVED_NAMESPACES)
        # Providing it needs the "permissions" namespace, which no module may own, as its id or as an extra namespace.
        self.assertCode("reserved_namespace", ManifestError, manifest, module_id="permissions", license_feature="mod.perm_feature")
        self.assertCode("reserved_namespace", ManifestError, manifest, extra_namespaces=("permissions",))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, capabilities_provided=("permissions.check",))

    def test_no_other_core_capability_can_be_provided_by_a_module_either(self):
        for capability in sorted(CORE_CAPABILITIES):
            with self.subTest(capability=capability):
                self.assertCode("identifier_outside_namespace", ManifestError, manifest, capabilities_provided=(capability,))

    def test_unknown_capabilities_are_refused(self):
        for unknown in ("permissions.manage", "permissions.check_all", "task.dequeue", "case.delete", "audit.erase", "nothing.provides"):
            with self.subTest(capability=unknown):
                self.assertCode("missing_capability", RegistryError, registry_of, named("alpha_mod", capabilities_required=(unknown,)))

    def test_malformed_capability_ids_are_refused_before_the_registry(self):
        for bad in ("single", "Task.enqueue", "task..enqueue", ".enqueue", "task.", "task enqueue", "", "task.enqueue!"):
            with self.subTest(capability=bad):
                self.assertCode("invalid_identifier", ManifestError, manifest, capabilities_required=(bad,))

    def test_planned_capabilities_are_still_refused_with_their_own_code(self):
        self.assertEqual(FUTURE_CAPABILITIES, frozenset({"documents.read", "ai.invoke", "secrets.resolve"}))
        for capability in sorted(FUTURE_CAPABILITIES):
            self.assertCode("capability_not_yet_available", RegistryError, registry_of, named("alpha_mod", capabilities_required=(capability,)))

    def test_duplicate_capability_declarations_are_refused(self):
        self.assertCode("duplicate_capability", ManifestError, manifest, capabilities_required=("task.enqueue", "task.enqueue"))
        self.assertCode("duplicate_capability", ManifestError, manifest, capabilities_provided=("demo_module.a", "demo_module.a"))
        self.assertCode("duplicate_capability", RegistryError, registry_of, shared("alpha_mod", capabilities_provided=("shared.service",)), shared("beta_mod", capabilities_provided=("shared.service",)))

    def test_a_module_cannot_both_require_and_provide_a_capability(self):
        self.assertCode("capability_required_and_provided", ManifestError, manifest, capabilities_required=("demo_module.a",), capabilities_provided=("demo_module.a",))

    def test_a_provided_capability_must_be_in_an_owned_namespace(self):
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, capabilities_provided=("other_module.service",))
        self.assertEqual(manifest(extra_namespaces=("legacy_ns",), capabilities_provided=("legacy_ns.service",)).capabilities_provided, ("legacy_ns.service",))

    def test_a_dependency_does_not_grant_its_namespace_for_providing_capabilities(self):
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, dependencies=(dep("alpha_mod"),), capabilities_provided=("alpha_mod.service",))

    def test_the_intake_like_manifest_requires_the_five_core_capabilities_and_registers(self):
        m = intake_like_manifest()
        self.assertEqual(set(m.capabilities_required), set(CORE_CAPABILITIES))
        self.assertEqual(len(registry_of(m)), 1)

    def test_provider_dependencies_must_be_direct(self):
        provider = named("provider_mod", capabilities_provided=("provider_mod.service",))
        needs = ("provider_mod.service",)
        self.assertCode("capability_provider_not_dependency", RegistryError, registry_of, provider, named("consumer_mod", capabilities_required=needs))
        middle = named("middle_mod", dependencies=(dep("provider_mod"),))
        self.assertCode("capability_provider_not_dependency", RegistryError, registry_of, provider, middle, named("far_mod", dependencies=(dep("middle_mod"),), capabilities_required=needs))
        self.assertEqual(len(registry_of(provider, named("consumer_mod", dependencies=(dep("provider_mod"),), capabilities_required=needs))), 2)

    def test_no_capability_is_inherited_through_a_chain_and_role_grants_do_not_travel_either(self):
        provider = named("alpha_mod", capabilities_provided=("alpha_mod.service",))
        beta = named("beta_mod", dependencies=(dep("alpha_mod"),), capabilities_required=("alpha_mod.service",))
        gamma = named("gamma_mod", dependencies=(dep("beta_mod"),), capabilities_required=("alpha_mod.service",))
        self.assertCode("capability_provider_not_dependency", RegistryError, registry_of, provider, beta, gamma)

    def test_the_package_defines_no_way_to_execute_a_capability(self):
        forbidden = {"enqueue", "enqueue_task", "create_run", "create_case", "record_event", "require_permission", "authorize", "load_actor", "check_permission", "invoke", "execute", "call_capability", "grant", "run_hook"}
        for path in MODULES_DIR.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    with self.subTest(file=path.name, name=node.name):
                        self.assertNotIn(node.name, forbidden)


class MaintenanceHookDeclarationTests(ContractTestCase):
    def test_a_valid_declaration(self):
        hook = MaintenanceHookDeclaration("demo_module.sweep", True, "bookkeeping")
        self.assertEqual((hook.hook_id, hook.while_disabled, hook.description), ("demo_module.sweep", True, "bookkeeping"))
        self.assertIs(MaintenanceHookDeclaration("demo_module.sweep").while_disabled, False)

    def test_a_declaration_is_immutable(self):
        hook = MaintenanceHookDeclaration("demo_module.sweep")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            hook.while_disabled = True
        with self.assertRaises(dataclasses.FrozenInstanceError):
            hook.hook_id = "demo_module.other"

    def test_hook_ids_are_namespaced_stable_identifiers(self):
        for bad in ("sweep", "Demo.sweep", "demo_module.", ".sweep", "demo module.sweep", "demo_module.sweep!", "", "x" * 80):
            with self.subTest(hook_id=bad):
                self.assertCode("invalid_identifier", ManifestError, MaintenanceHookDeclaration, bad)
        for bad in (None, 5, b"demo_module.sweep"):
            with self.subTest(hook_id=bad):
                self.assertCode("invalid_type", ManifestError, MaintenanceHookDeclaration, bad)

    def test_a_hook_id_must_start_with_an_owned_namespace(self):
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, maintenance_hooks=(MaintenanceHookDeclaration("other_module.sweep"),))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, maintenance_hooks=(MaintenanceHookDeclaration("test.sweep"),))
        self.assertEqual(len(manifest(extra_namespaces=("legacy_ns",), maintenance_hooks=(MaintenanceHookDeclaration("legacy_ns.sweep"),)).maintenance_hooks), 1)

    def test_a_dependency_grants_no_hook_namespace(self):
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, dependencies=(dep("alpha_mod"),), maintenance_hooks=(MaintenanceHookDeclaration("alpha_mod.sweep"),))

    def test_while_disabled_must_be_a_real_boolean(self):
        for bad in (1, 0, "yes", "true", None, [], 1.0):
            with self.subTest(value=bad):
                self.assertCode("invalid_type", ManifestError, MaintenanceHookDeclaration, "demo_module.sweep", bad)

    def test_duplicate_hook_ids_are_refused_inside_a_module_and_across_modules(self):
        twice = (MaintenanceHookDeclaration("demo_module.sweep"), MaintenanceHookDeclaration("demo_module.sweep", True))
        self.assertCode("duplicate_maintenance_hook", ManifestError, manifest, maintenance_hooks=twice)
        a = shared("alpha_mod", maintenance_hooks=(MaintenanceHookDeclaration("shared.sweep"),))
        b = shared("beta_mod", maintenance_hooks=(MaintenanceHookDeclaration("shared.sweep"),))
        error = self.assertCode("duplicate_maintenance_hook", RegistryError, registry_of, a, b)
        self.assertIn("alpha_mod", str(error))
        self.assertIn("beta_mod", str(error))

    def test_a_hook_declaration_has_no_callable_no_path_and_nothing_to_execute(self):
        self.assertEqual({f.name for f in dataclasses.fields(MaintenanceHookDeclaration)}, {"hook_id", "while_disabled", "description"})
        self.assertEqual({f.name for f in dataclasses.fields(RegisteredHook)}, {"module_id", "hook_id", "while_disabled"})
        for text in ("apps.intake.services:reconcile_after_iteration", "apps.intake.services.reconcile_after_iteration", "C:\\hooks\\run.py"):
            with self.subTest(text=text):
                self.assertCode("invalid_identifier", ManifestError, MaintenanceHookDeclaration, text)
        with self.assertRaises(TypeError):
            MaintenanceHookDeclaration("demo_module.sweep", False, "", lambda: None)  # there is no field for a callable

    def test_hooks_are_stored_sorted_by_id_and_belong_to_the_fingerprint(self):
        a = manifest(maintenance_hooks=(MaintenanceHookDeclaration("demo_module.zeta"), MaintenanceHookDeclaration("demo_module.alpha")))
        b = manifest(maintenance_hooks=(MaintenanceHookDeclaration("demo_module.alpha"), MaintenanceHookDeclaration("demo_module.zeta")))
        self.assertEqual([h.hook_id for h in a.maintenance_hooks], ["demo_module.alpha", "demo_module.zeta"])
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), manifest().fingerprint())
        self.assertNotEqual(manifest(maintenance_hooks=(MaintenanceHookDeclaration("demo_module.a", False),)).fingerprint(), manifest(maintenance_hooks=(MaintenanceHookDeclaration("demo_module.a", True),)).fingerprint())


class RegisteredHookOrderingTests(ContractTestCase):
    def hooked(self, module_id, *names, while_disabled=False, **kw):
        return named(module_id, maintenance_hooks=tuple(MaintenanceHookDeclaration(f"{module_id}.{n}", while_disabled) for n in names), **kw)

    def test_a_registry_without_hooks_has_none(self):
        self.assertEqual(registry_of(named("alpha_mod")).maintenance_hooks, ())
        self.assertEqual(registry_of().maintenance_hooks, ())

    def test_hooks_follow_registration_order_then_hook_id(self):
        registry = registry_of(
            self.hooked("aaa_top", "x", dependencies=(dep("zzz_base"),)),
            self.hooked("zzz_base", "b", "a"),
            self.hooked("mmm_mid", "only"),
        )
        # Registration order is mmm_mid, zzz_base, aaa_top (a dependency before its dependents; ties by module id).
        self.assertEqual([h.hook_id for h in registry.maintenance_hooks], ["mmm_mid.only", "zzz_base.a", "zzz_base.b", "aaa_top.x"])
        self.assertEqual([h.module_id for h in registry.maintenance_hooks], ["mmm_mid", "zzz_base", "zzz_base", "aaa_top"])

    def test_the_order_does_not_depend_on_the_order_of_the_input(self):
        modules = [self.hooked("delta_mod", "two", "one"), self.hooked("alpha_mod", "z"), self.hooked("charlie_mod", "y", dependencies=(dep("delta_mod"),)), self.hooked("bravo_mod", "x")]
        orders = {tuple(h.hook_id for h in ModuleRegistry.build(p).maintenance_hooks) for p in itertools.permutations(modules)}
        self.assertEqual(len(orders), 1)
        (order,) = orders
        self.assertEqual(order, ("alpha_mod.z", "bravo_mod.x", "delta_mod.one", "delta_mod.two", "charlie_mod.y"))

    def test_while_disabled_is_carried_into_the_registered_hook(self):
        registry = registry_of(named("alpha_mod", maintenance_hooks=(MaintenanceHookDeclaration("alpha_mod.keep", True), MaintenanceHookDeclaration("alpha_mod.pause", False))))
        self.assertEqual({h.hook_id: h.while_disabled for h in registry.maintenance_hooks}, {"alpha_mod.keep": True, "alpha_mod.pause": False})

    def test_the_hook_list_is_immutable(self):
        registry = registry_of(self.hooked("alpha_mod", "a"))
        self.assertIsInstance(registry.maintenance_hooks, tuple)
        with self.assertRaises(AttributeError):
            registry.maintenance_hooks.append(RegisteredHook("alpha_mod", "alpha_mod.b", False))
        with self.assertRaises(AttributeError):
            registry.maintenance_hooks = ()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            registry.maintenance_hooks[0].while_disabled = True

    def test_the_intake_like_manifest_declares_the_reconciler_as_a_while_disabled_hook(self):
        (hook,) = registry_of(intake_like_manifest()).maintenance_hooks
        self.assertEqual(hook, RegisteredHook("request_intake", "request_intake.reconcile", True))


class DependencyInspectionTests(ContractTestCase):
    def chain(self):
        a = named("alpha_mod")
        b = named("beta_mod", dependencies=(dep("alpha_mod"),))
        c = named("gamma_mod", dependencies=(dep("beta_mod"),))
        return registry_of(c, a, b)

    def test_direct_dependencies_and_dependents_only(self):
        registry = self.chain()
        self.assertEqual(dict(registry.dependencies), {"alpha_mod": (), "beta_mod": ("alpha_mod",), "gamma_mod": ("beta_mod",)})
        self.assertEqual(dict(registry.dependents), {"alpha_mod": ("beta_mod",), "beta_mod": ("gamma_mod",), "gamma_mod": ()})
        self.assertEqual(registry.dependencies_of("gamma_mod"), ("beta_mod",))  # not ("alpha_mod", "beta_mod")
        self.assertEqual(registry.dependents_of("alpha_mod"), ("beta_mod",))  # not ("beta_mod", "gamma_mod")

    def test_a_diamond_lists_each_direct_edge_once_and_sorted(self):
        registry = registry_of(
            named("dddd_mod", dependencies=(dep("cccc_mod"), dep("bbbb_mod"))),
            named("cccc_mod", dependencies=(dep("aaaa_mod"),)),
            named("bbbb_mod", dependencies=(dep("aaaa_mod"),)),
            named("aaaa_mod"),
        )
        self.assertEqual(registry.dependencies_of("dddd_mod"), ("bbbb_mod", "cccc_mod"))
        self.assertEqual(registry.dependents_of("aaaa_mod"), ("bbbb_mod", "cccc_mod"))
        self.assertEqual(registry.dependents_of("bbbb_mod"), ("dddd_mod",))

    def test_every_registered_module_has_an_entry_and_a_module_is_never_its_own_dependent(self):
        registry = self.chain()
        self.assertEqual(set(registry.dependencies), set(registry.module_ids))
        self.assertEqual(set(registry.dependents), set(registry.module_ids))
        for module_id in registry.module_ids:
            self.assertNotIn(module_id, registry.dependencies_of(module_id))
            self.assertNotIn(module_id, registry.dependents_of(module_id))

    def test_the_two_views_agree_with_each_other(self):
        registry = self.chain()
        for module_id, deps in registry.dependencies.items():
            for dependency in deps:
                self.assertIn(module_id, registry.dependents_of(dependency))
        for module_id, dependents in registry.dependents.items():
            for dependent in dependents:
                self.assertIn(module_id, registry.dependencies_of(dependent))

    def test_a_module_that_nobody_uses_has_no_dependents_and_one_that_needs_nothing_has_no_dependencies(self):
        registry = registry_of(named("solo_mod"))
        self.assertEqual(registry.dependencies_of("solo_mod"), ())
        self.assertEqual(registry.dependents_of("solo_mod"), ())

    def test_an_unknown_module_is_an_error_not_an_empty_answer(self):
        registry = self.chain()
        for bad in ("ghost_mod", "", None, 5, b"alpha_mod", ("alpha_mod",)):
            with self.subTest(module=bad):
                self.assertCode("unknown_module", RegistryError, registry.dependencies_of, bad)
                self.assertCode("unknown_module", RegistryError, registry.dependents_of, bad)

    def test_the_answers_are_read_only(self):
        registry = self.chain()
        for mapping in (registry.dependencies, registry.dependents):
            self.assertIsInstance(mapping, MappingProxyType)
            with self.assertRaises(TypeError):
                mapping["alpha_mod"] = ()
            self.assertEqual(list(mapping), sorted(mapping))
        self.assertIsInstance(registry.dependencies_of("beta_mod"), tuple)
        for name in ("_dependencies", "_dependents", "dependencies", "dependents"):
            with self.assertRaises(AttributeError):
                setattr(registry, name, {})

    def test_there_is_no_transitive_query(self):
        names = " ".join(dir(self.chain())).lower()
        for word in ("transitive", "closure", "ancestor", "descendant", "all_dependencies", "inherit"):
            self.assertNotIn(word, names)

    def test_the_result_does_not_depend_on_input_order(self):
        modules = [named("alpha_mod"), named("beta_mod", dependencies=(dep("alpha_mod"),)), named("gamma_mod", dependencies=(dep("alpha_mod"), dep("beta_mod")))]
        results = {(tuple(sorted(r.dependencies.items())), tuple(sorted(r.dependents.items()))) for r in (ModuleRegistry.build(p) for p in itertools.permutations(modules))}
        self.assertEqual(len(results), 1)

    def test_the_inspection_adds_nothing_to_the_fingerprint(self):
        self.assertEqual(registry_of(named("alpha_mod")).fingerprint, registry_of(named("alpha_mod")).fingerprint)
        self.assertEqual(registry_of(full_manifest("full_module")).fingerprint, registry_of(full_manifest("full_module")).fingerprint)


if __name__ == "__main__":
    unittest.main()
