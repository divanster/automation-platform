"""Pure tests for MOD-1b S4: the module host's decisions (``apps.module_host.rules``) over real registries and plain state facts.

No Django, no database. The registries are built from SYNTHETIC manifests (``tests/module_support.py``); no module exists in the application.
"""

import ast
import sys
from pathlib import Path

from apps.module_host import rules
from apps.module_host.errors import CODES, ModuleHostError
from apps.module_host.rules import (
    DEPENDENCY_UNAVAILABLE,
    DISABLED,
    ENABLED,
    LEGACY_UNSYNCED,
    MISSING_STATE,
    VERSION_MISMATCH,
    StateFacts,
)
from apps.modules.manifest import TaskHandlerDeclaration
from apps.modules.registry import ModuleRegistry
from tests.module_support import ContractTestCase, FakeHandler, dep, named

BACKEND = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)
FP = "a" * 64


def handler(kind):
    return TaskHandlerDeclaration(kind, FakeHandler(kind), "does work")


def mod(module_id, *, kinds=(), deps=(), version="1.0.0", **overrides):
    return named(module_id, module_version=version, task_handlers=tuple(handler(k) for k in kinds), dependencies=tuple(dep(d) for d in deps), **overrides)


def registry(*manifests):
    return ModuleRegistry.build(manifests)


def fact(module_id, *, enabled=True, version="1.0.0", fingerprint=FP):
    return StateFacts(module_id, version, enabled, fingerprint)


def facts(*items):
    return {item.module_id: item for item in items}


# base -> middle -> top, and a free-standing module
CHAIN = registry(
    mod("base_mod", kinds=("base_mod.work",)),
    mod("middle_mod", kinds=("middle_mod.work",), deps=("base_mod",)),
    mod("top_mod", kinds=("top_mod.work",), deps=("middle_mod",)),
    mod("alone_mod", kinds=("alone_mod.work", "alone_mod.other")),
)
ALL_ENABLED = facts(*(fact(m) for m in ("base_mod", "middle_mod", "top_mod", "alone_mod")))
CURRENT = {m: fact(m, fingerprint=CHAIN.fingerprint) for m in CHAIN.module_ids}  # every row confirmed against this registry
LEGACY = frozenset({"test.add_numbers", "request_intake.extract"})


class AvailabilityTests(ContractTestCase):
    def reasons(self, state, reg=CHAIN, initialized=True):
        return {module_id: (v.available, v.reason) for module_id, v in rules.assess_modules(reg, state, initialized=initialized).items()}

    def test_everything_enabled_and_version_matched_is_available(self):
        self.assertEqual(set(self.reasons(ALL_ENABLED).values()), {(True, ENABLED)})

    def test_before_the_initial_sync_a_missing_state_is_treated_as_enabled(self):
        self.assertEqual(set(self.reasons({}, initialized=False).values()), {(True, LEGACY_UNSYNCED)})
        self.assertFalse(rules.is_initialized({}, False))

    def test_after_the_sync_a_missing_state_fails_closed(self):
        state = dict(ALL_ENABLED)
        del state["alone_mod"]
        self.assertTrue(rules.is_initialized(state, True))
        self.assertEqual(self.reasons(state)["alone_mod"], (False, MISSING_STATE))
        self.assertEqual(self.reasons(state)["base_mod"], (True, ENABLED))

    def test_the_transition_ends_with_the_marker_not_with_the_rows(self):
        # the marker says the first sync completed: from then on every registered module without a row is closed, even when NO row is left
        # (deleted by hand, or a sync that ran with an empty registry before the modules existed)
        self.assertEqual(set(self.reasons({}, initialized=True).values()), {(False, MISSING_STATE)})
        reasons = self.reasons(facts(fact("alone_mod")))
        self.assertEqual(reasons["alone_mod"], (True, ENABLED))
        self.assertEqual({reasons[m] for m in ("base_mod", "middle_mod", "top_mod")}, {(False, MISSING_STATE)})

    def test_initialization_is_the_marker_and_rows_without_a_marker_count_too(self):
        self.assertFalse(rules.is_initialized({}, False))
        self.assertTrue(rules.is_initialized({}, True))  # the marker alone is enough: no row is needed
        self.assertTrue(rules.is_initialized(ALL_ENABLED, False))  # rows with no marker: an inconsistent database fails closed, never open
        self.assertTrue(rules.is_initialized(ALL_ENABLED, True))

    def test_initialized_must_be_stated_by_the_caller(self):
        with self.assertRaises(TypeError):
            rules.assess_modules(CHAIN, ALL_ENABLED)  # no default: an omission can never mean "unsynced"
        for bad in (None, 1, "yes", 0):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                rules.assess_modules(CHAIN, ALL_ENABLED, initialized=bad)

    def test_a_disabled_module_is_not_available(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", enabled=False))
        self.assertEqual(self.reasons(state)["alone_mod"], (False, DISABLED))

    def test_a_version_mismatch_is_not_available_even_when_enabled(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", version="1.0.1"))
        self.assertEqual(self.reasons(state)["alone_mod"], (False, VERSION_MISMATCH))
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", version="0.9.0"))
        self.assertEqual(self.reasons(state)["alone_mod"], (False, VERSION_MISMATCH))

    def test_a_dependent_is_unavailable_when_a_dependency_is_not_even_if_it_is_enabled_itself(self):
        state = dict(ALL_ENABLED, base_mod=fact("base_mod", enabled=False))
        reasons = self.reasons(state)
        self.assertEqual(reasons["base_mod"], (False, DISABLED))
        self.assertEqual(reasons["middle_mod"], (False, DEPENDENCY_UNAVAILABLE))
        self.assertEqual(reasons["top_mod"], (False, DEPENDENCY_UNAVAILABLE))  # through the chain
        self.assertEqual(reasons["alone_mod"], (True, ENABLED))

    def test_a_dependency_with_a_mismatched_version_closes_its_dependents(self):
        state = dict(ALL_ENABLED, base_mod=fact("base_mod", version="2.0.0"))
        reasons = self.reasons(state)
        self.assertEqual(reasons["base_mod"], (False, VERSION_MISMATCH))
        self.assertEqual(reasons["middle_mod"], (False, DEPENDENCY_UNAVAILABLE))

    def test_every_registered_module_is_assessed_in_registration_order(self):
        self.assertEqual(list(rules.assess_modules(CHAIN, ALL_ENABLED, initialized=True)), list(CHAIN.module_ids))

    def test_rows_of_unregistered_modules_change_nothing(self):
        state = dict(ALL_ENABLED, ghost_mod=fact("ghost_mod"))
        self.assertEqual(set(self.reasons(state).values()), {(True, ENABLED)})

    def test_an_empty_registry_is_assessed_without_state(self):
        self.assertEqual(rules.assess_modules(registry(), {}, initialized=False), {})
        self.assertEqual(rules.assess_modules(registry(), {}, initialized=True), {})


class AllowedKindsTests(ContractTestCase):
    def allowed(self, state, legacy=LEGACY, reg=CHAIN, initialized=True):
        return rules.allowed_kinds(reg, rules.assess_modules(reg, state, initialized=initialized), frozenset(legacy))

    def test_legacy_kinds_are_always_allowed_whatever_the_state(self):
        for state in ({}, ALL_ENABLED, facts(fact("alone_mod", enabled=False)), facts(fact("alone_mod", version="9.9.9"))):
            with self.subTest(rows=len(state)):
                self.assertTrue(LEGACY <= self.allowed(state))

    def test_the_kinds_of_enabled_modules_are_allowed(self):
        self.assertEqual(self.allowed(ALL_ENABLED), LEGACY | {"base_mod.work", "middle_mod.work", "top_mod.work", "alone_mod.work", "alone_mod.other"})

    def test_a_disabled_modules_kinds_are_excluded_and_nothing_else_changes(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", enabled=False))
        self.assertEqual(self.allowed(state), LEGACY | {"base_mod.work", "middle_mod.work", "top_mod.work"})

    def test_a_version_mismatched_modules_kinds_are_excluded(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", version="1.1.0"))
        self.assertNotIn("alone_mod.work", self.allowed(state))

    def test_missing_state_after_the_sync_excludes_the_kinds(self):
        state = dict(ALL_ENABLED)
        del state["alone_mod"]
        allowed = self.allowed(state)
        self.assertFalse({"alone_mod.work", "alone_mod.other"} & allowed)

    def test_before_the_sync_the_modules_kinds_are_allowed(self):
        self.assertEqual(self.allowed({}, initialized=False), LEGACY | {"base_mod.work", "middle_mod.work", "top_mod.work", "alone_mod.work", "alone_mod.other"})

    def test_after_initialization_with_no_rows_left_only_the_legacy_kinds_are_allowed(self):
        self.assertEqual(self.allowed({}, initialized=True), LEGACY)

    def test_an_unknown_kind_is_never_allowed(self):
        for state in ({}, ALL_ENABLED):
            allowed = self.allowed(state)
            for kind in ("ghost.work", "os.system", "", "base_mod.WORK", "base_mod"):
                self.assertNotIn(kind, allowed)

    def test_disabling_a_dependency_excludes_its_dependents_kinds_too(self):
        allowed = self.allowed(dict(ALL_ENABLED, base_mod=fact("base_mod", enabled=False)))
        self.assertFalse({"base_mod.work", "middle_mod.work", "top_mod.work"} & allowed)

    def test_the_answer_is_a_frozenset_of_text(self):
        allowed = self.allowed(ALL_ENABLED)
        self.assertIsInstance(allowed, frozenset)
        self.assertTrue(all(isinstance(kind, str) for kind in allowed))

    def test_an_empty_registry_allows_exactly_the_legacy_kinds(self):
        for initialized in (False, True):
            self.assertEqual(self.allowed({}, reg=registry(), initialized=initialized), LEGACY)

    def test_the_answer_does_not_depend_on_the_registration_order_of_the_input(self):
        shuffled = registry(*reversed(CHAIN.modules))
        self.assertEqual(self.allowed(ALL_ENABLED, reg=shuffled), self.allowed(ALL_ENABLED))


class LegacyKindTests(ContractTestCase):
    def test_a_valid_set_is_returned_as_a_frozenset(self):
        self.assertEqual(rules.check_legacy_kinds(CHAIN, ["test.add_numbers", "request_intake.extract"]), LEGACY)

    def test_a_kind_declared_by_a_registered_module_is_refused(self):
        error = self.assertCode("legacy_kind_claimed", ModuleHostError, rules.check_legacy_kinds, CHAIN, {"base_mod.work", "test.add_numbers"})
        self.assertIn("base_mod.work", str(error))

    def test_request_intake_cannot_be_both_legacy_and_a_module_kind(self):
        takeover = registry(mod("request_intake", kinds=("request_intake.extract",)))
        self.assertCode("legacy_kind_claimed", ModuleHostError, rules.check_legacy_kinds, takeover, LEGACY)
        rules.check_legacy_kinds(takeover, {"test.add_numbers"})  # dropping it from the legacy set is the explicit way

    def test_malformed_sets_are_refused(self):
        for bad in ("request_intake.extract", b"x", None, 5, {"ok.kind", 5}, {""}, {"a" * 65}, {"has space"}):
            with self.subTest(bad=bad):
                self.assertCode("invalid_legacy_kinds", ModuleHostError, rules.check_legacy_kinds, CHAIN, bad)


class SyncPlanTests(ContractTestCase):
    def plan(self, state, claims=None, reg=CHAIN):
        return rules.plan_sync(reg, state, {} if claims is None else claims)

    def test_the_first_sync_creates_every_module_enabled_in_registration_order(self):
        plan = self.plan({})
        self.assertEqual([(n.module_id, n.installed_version, n.enabled) for n in plan.create], [(m, "1.0.0", True) for m in CHAIN.module_ids])
        self.assertEqual((plan.refresh, plan.mismatched, plan.unregistered, plan.unchanged), ((), (), (), ()))
        self.assertTrue(plan.clean)

    def test_the_claims_to_add_are_every_namespace_not_claimed_yet(self):
        plan = self.plan({})
        self.assertEqual(plan.new_claims, tuple(sorted(CHAIN.namespace_owners.items())))
        done = self.plan({}, claims=dict(CHAIN.namespace_owners))
        self.assertEqual(done.new_claims, ())
        partial = self.plan({}, claims={"base_mod": "base_mod"})
        self.assertEqual([ns for ns, _ in partial.new_claims], ["alone_mod", "middle_mod", "top_mod"])

    def test_a_second_sync_with_nothing_new_changes_nothing(self):
        plan = self.plan(CURRENT, claims=dict(CHAIN.namespace_owners))
        self.assertEqual((plan.create, plan.refresh, plan.mismatched, plan.unregistered, plan.new_claims), ((), (), (), (), ()))
        self.assertEqual(sorted(plan.unchanged), sorted(CHAIN.module_ids))

    def test_a_previously_disabled_module_is_never_enabled_by_the_sync(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", enabled=False, fingerprint="b" * 64))
        plan = self.plan(state)
        self.assertEqual(plan.create, ())
        self.assertIn("alone_mod", plan.refresh)  # its fingerprint is refreshed; its enabled flag is not part of any plan field
        self.assertFalse(hasattr(plan, "enable"))

    def test_a_new_module_starts_disabled_when_a_dependency_is_disabled(self):
        state = facts(fact("base_mod", enabled=False))
        plan = self.plan(state)
        created = {n.module_id: n.enabled for n in plan.create}
        self.assertEqual(created, {"middle_mod": False, "top_mod": False, "alone_mod": True})

    def test_a_new_module_starts_disabled_when_a_dependency_has_a_version_mismatch(self):
        plan = self.plan(facts(fact("base_mod", version="2.0.0")))
        created = {n.module_id: n.enabled for n in plan.create}
        self.assertEqual(created["middle_mod"], False)

    def test_a_new_module_with_enabled_dependencies_starts_enabled(self):
        state = facts(fact("base_mod"))
        created = {n.module_id: n.enabled for n in self.plan(state).create}
        self.assertEqual(created, {"middle_mod": True, "top_mod": True, "alone_mod": True})

    def test_a_version_mismatch_is_reported_and_never_changed(self):
        plan = self.plan(dict(ALL_ENABLED, alone_mod=fact("alone_mod", version="0.9.0")))
        self.assertEqual([(m.module_id, m.installed_version, m.registered_version) for m in plan.mismatched], [("alone_mod", "0.9.0", "1.0.0")])
        self.assertFalse(plan.clean)
        self.assertNotIn("alone_mod", plan.refresh + plan.unchanged + tuple(n.module_id for n in plan.create))

    def test_a_state_row_without_a_registry_entry_is_reported_and_left_alone(self):
        plan = self.plan(dict(ALL_ENABLED, ghost_mod=fact("ghost_mod")))
        self.assertEqual(plan.unregistered, ("ghost_mod",))
        self.assertFalse(plan.clean)

    def test_a_stale_fingerprint_is_refreshed_when_the_version_matches(self):
        plan = self.plan(dict(CURRENT, base_mod=fact("base_mod", fingerprint="0" * 64)))
        self.assertEqual(plan.refresh, ("base_mod",))

    def test_the_current_fingerprint_needs_no_refresh(self):
        plan = self.plan(CURRENT)
        self.assertEqual((plan.refresh, sorted(plan.unchanged)), ((), sorted(CHAIN.module_ids)))

    def test_an_empty_registry_has_nothing_to_sync(self):
        plan = self.plan({}, reg=registry())
        self.assertEqual((plan.create, plan.refresh, plan.mismatched, plan.unregistered, plan.unchanged, plan.new_claims), ((),) * 6)
        self.assertTrue(plan.clean)

    def test_the_plan_is_deterministic(self):
        shuffled = registry(*reversed(CHAIN.modules))
        self.assertEqual(self.plan({}, reg=shuffled), self.plan({}))


class EnableDisableChecksTests(ContractTestCase):
    def test_enabling_needs_a_registered_module(self):
        self.assertCode("unknown_module", ModuleHostError, rules.check_can_enable, CHAIN, "ghost_mod", ALL_ENABLED)
        self.assertCode("unknown_module", ModuleHostError, rules.check_can_disable, CHAIN, "ghost_mod", ALL_ENABLED)

    def test_garbage_ids_are_refused_before_any_lookup(self):
        for bad in (None, 5, "", "A_B", "ab", "x" * 41, "has space", "a-b-c", b"base_mod", ["base_mod"], "base_mod\n"):
            with self.subTest(bad=bad):
                self.assertCode("invalid_module_id", ModuleHostError, rules.check_can_enable, CHAIN, bad, ALL_ENABLED)
                self.assertCode("invalid_module_id", ModuleHostError, rules.registered, CHAIN, bad)

    def test_a_missing_state_row_refuses_both_directions(self):
        self.assertCode("state_missing", ModuleHostError, rules.check_can_enable, CHAIN, "alone_mod", {})
        self.assertCode("state_missing", ModuleHostError, rules.check_can_disable, CHAIN, "alone_mod", {})

    def test_a_version_mismatch_refuses_enabling_but_not_disabling(self):
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", enabled=False, version="3.0.0"))
        self.assertCode("version_mismatch", ModuleHostError, rules.check_can_enable, CHAIN, "alone_mod", state)
        rules.check_can_disable(CHAIN, "alone_mod", state)

    def test_enabling_needs_every_direct_dependency_enabled(self):
        state = dict(ALL_ENABLED, base_mod=fact("base_mod", enabled=False), middle_mod=fact("middle_mod", enabled=False))
        error = self.assertCode("dependency_not_enabled", ModuleHostError, rules.check_can_enable, CHAIN, "middle_mod", state)
        self.assertIn("base_mod", str(error))
        rules.check_can_enable(CHAIN, "base_mod", state)  # no dependency: fine

    def test_enabling_refuses_a_dependency_without_state_or_with_a_mismatched_version(self):
        without = {k: v for k, v in ALL_ENABLED.items() if k != "base_mod"}
        self.assertCode("dependency_not_enabled", ModuleHostError, rules.check_can_enable, CHAIN, "middle_mod", dict(without, middle_mod=fact("middle_mod", enabled=False)))
        wrong = dict(ALL_ENABLED, base_mod=fact("base_mod", version="2.0.0"), middle_mod=fact("middle_mod", enabled=False))
        self.assertCode("dependency_not_enabled", ModuleHostError, rules.check_can_enable, CHAIN, "middle_mod", wrong)

    def test_disabling_is_refused_while_an_enabled_dependent_needs_the_module(self):
        error = self.assertCode("dependents_enabled", ModuleHostError, rules.check_can_disable, CHAIN, "base_mod", ALL_ENABLED)
        self.assertIn("middle_mod", str(error))
        self.assertNotIn("top_mod", str(error))  # direct dependents only

    def test_disabling_is_allowed_once_the_dependents_are_disabled(self):
        state = dict(ALL_ENABLED, middle_mod=fact("middle_mod", enabled=False))
        rules.check_can_disable(CHAIN, "base_mod", state)

    def test_disabling_a_leaf_is_allowed(self):
        rules.check_can_disable(CHAIN, "top_mod", ALL_ENABLED)
        rules.check_can_disable(CHAIN, "alone_mod", ALL_ENABLED)

    def test_a_dependent_without_state_does_not_block_disabling(self):
        state = {k: v for k, v in ALL_ENABLED.items() if k != "middle_mod"}
        rules.check_can_disable(CHAIN, "base_mod", state)


class StructureTests(ContractTestCase):
    def test_rules_and_errors_import_only_the_standard_library_and_the_package(self):
        for name in ("rules.py", "errors.py"):
            tree = ast.parse((BACKEND / "apps" / "module_host" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [] if node.level else [node.module]
                else:
                    continue
                for module in names:
                    with self.subTest(file=name, imported=module):
                        self.assertIn(module.split(".")[0], STDLIB)

    def test_every_declared_error_code_is_raised_somewhere_in_the_package(self):
        source = "".join(p.read_text(encoding="utf-8") for p in (BACKEND / "apps" / "module_host").rglob("*.py") if p.name != "errors.py" and "migrations" not in p.parts)
        for code in sorted(CODES):
            with self.subTest(code=code):
                self.assertIn(f'"{code}"', source, code)

    def test_an_undeclared_code_cannot_be_raised(self):
        with self.assertRaises(ValueError):
            ModuleHostError("made_up", "x")

    def test_error_messages_carry_ids_and_versions_only(self):
        secret_like = "SECRET-TOKEN-xyz"
        state = dict(ALL_ENABLED, alone_mod=fact("alone_mod", version="9.9.9", fingerprint=secret_like))
        error = self.assertCode("version_mismatch", ModuleHostError, rules.check_can_enable, CHAIN, "alone_mod", state)
        self.assertNotIn(secret_like, str(error))
