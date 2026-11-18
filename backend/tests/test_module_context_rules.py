"""Pure tests for the ModuleContext rules (MOD-1b S6): capability declaration, task kinds, idempotency keys, workflows, audit events, permissions, availability.

No Django and no database: they run with ``python -m unittest tests.test_module_context_rules`` and through ``manage.py test``. The rules read a real
``ModuleManifest`` and a real ``ModuleRegistry`` (built from synthetic modules in tests/module_context_support.py), never a mock.

This file is ASCII only.
"""

import unittest

from apps.module_host import context_rules as rules
from apps.module_host import rules as host_rules
from apps.module_host.errors import CONTEXT_CODES, ModuleContextError
from apps.modules.manifest import PermissionDeclaration
from tests import module_context_support as support
from tests.module_support import named


def refused(test, code, function, *args, **kwargs):
    with test.assertRaises(ModuleContextError) as caught:
        function(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, str(caught.exception))
    return caught.exception


class CapabilityTests(unittest.TestCase):
    def test_a_declared_capability_with_a_facade_passes(self):
        manifest = support.ctx_module()
        for capability in support.ALL_CAPABILITIES:
            rules.check_capability(manifest, capability)

    def test_an_undeclared_capability_is_refused(self):
        manifest = support.ctx_module(capabilities_required=("task.enqueue",))
        rules.check_capability(manifest, "task.enqueue")
        for capability in ("case.run", "audit.record", "permissions.check"):
            with self.subTest(capability=capability):
                refused(self, "capability_not_declared", rules.check_capability, manifest, capability)

    def test_a_module_with_no_capability_gets_nothing(self):
        manifest = named("bare_mod")
        for capability in support.ALL_CAPABILITIES:
            refused(self, "capability_not_declared", rules.check_capability, manifest, capability)

    def test_a_capability_without_a_facade_is_not_available_even_when_declared(self):
        manifest = support.ctx_module(capabilities_required=(*support.ALL_CAPABILITIES, "revision.approval"))
        refused(self, "capability_not_available", rules.check_capability, manifest, "revision.approval")
        for bad in ("documents.read", "ctx_mod.service", "", None, 5, b"task.enqueue"):
            with self.subTest(bad=bad):
                refused(self, "capability_not_available", rules.check_capability, manifest, bad)

    def test_the_facades_are_exactly_the_four_core_capabilities_with_a_service_behind_them(self):
        self.assertEqual(rules.FACADE_CAPABILITIES, {"task.enqueue", "case.run", "audit.record", "permissions.check"})


class TaskKindTests(unittest.TestCase):
    def setUp(self):
        self.registry = support.registry()
        self.manifest = self.registry.get(support.CTX)

    def test_a_declared_kind_passes(self):
        rules.check_task_kind(self.registry, self.manifest, support.KIND)

    def test_another_modules_kind_is_refused_as_not_owned(self):
        refused(self, "task_kind_not_owned", rules.check_task_kind, self.registry, self.manifest, support.OTHER_KIND)

    def test_an_undeclared_kind_in_the_modules_own_namespace_is_refused(self):
        refused(self, "task_kind_not_declared", rules.check_task_kind, self.registry, self.manifest, "ctx_mod.other_work")

    def test_a_core_or_legacy_or_unknown_kind_is_refused(self):
        for kind in ("test.add_numbers", "request_intake.extract", "os.system", "", None, 5, ["ctx_mod.work"]):
            with self.subTest(kind=kind):
                refused(self, "task_kind_not_declared", rules.check_task_kind, self.registry, self.manifest, kind)


class IdempotencyKeyTests(unittest.TestCase):
    def setUp(self):
        self.manifest = support.ctx_module()  # owns ctx_mod and intake

    def test_no_key_is_allowed(self):
        rules.check_idempotency_key(self.manifest, None)

    def test_a_key_in_an_owned_namespace_passes(self):
        for key in ("ctx_mod.work:abc:1", "intake.extract:1", "ctx_mod.x"):
            with self.subTest(key=key):
                rules.check_idempotency_key(self.manifest, key)

    def test_a_key_outside_the_modules_namespaces_is_refused(self):
        for key in ("other_mod.work:1", "request_intake.extract:1", "ctx_modx.work", "ctx_mod", "ctx_mod:work", ".ctx_mod.work", "work.ctx_mod", "", "plain", 5, b"ctx_mod.x"):
            with self.subTest(key=key):
                refused(self, "idempotency_key_not_namespaced", rules.check_idempotency_key, self.manifest, key)

    def test_ownership_is_exact_not_a_prefix(self):
        refused(self, "idempotency_key_not_namespaced", rules.check_idempotency_key, self.manifest, "ctx.work")
        refused(self, "idempotency_key_not_namespaced", rules.check_idempotency_key, self.manifest, "ctx_mod_extra.work")


class WorkflowTests(unittest.TestCase):
    def test_a_declared_workflow_returns_its_declaration_and_declared_version(self):
        manifest = support.ctx_module()
        declaration = rules.declared_workflow(manifest, support.FLOW)
        self.assertEqual((declaration.workflow_key, declaration.definition_version), (support.FLOW, support.FLOW_VERSION))

    def test_an_undeclared_workflow_is_refused(self):
        manifest = support.ctx_module()
        for key in ("other_mod.flow", "ctx_mod.other", "request_intake", "", None, 3):
            with self.subTest(key=key):
                refused(self, "workflow_not_declared", rules.declared_workflow, manifest, key)

    def test_a_run_of_another_module_is_refused(self):
        manifest = support.ctx_module()
        rules.check_run_owner(manifest, support.CTX)
        for owner in ("other_mod", "request_intake", None, ""):
            with self.subTest(owner=owner):
                refused(self, "run_not_owned", rules.check_run_owner, manifest, owner)


class AuditEventTests(unittest.TestCase):
    CORE = ("request.submitted", "revision.created", "run.state_changed", "module.enabled")

    def setUp(self):
        self.manifest = support.ctx_module()

    def test_a_declared_action_with_declared_keys_passes(self):
        rules.check_audit_event(self.manifest, self.CORE, support.ACTION, "run", {"count": 1, "reason_code": "x"})
        rules.check_audit_event(self.manifest, self.CORE, support.ACTION, "revision", {})

    def test_a_core_action_is_refused_before_anything_else(self):
        for action in self.CORE:
            with self.subTest(action=action):
                refused(self, "audit_action_core_owned", rules.check_audit_event, self.manifest, self.CORE, action, "run", {})

    def test_an_undeclared_or_foreign_action_is_refused(self):
        for action in (support.OTHER_ACTION, "ctx_mod.other", "unknown.thing", "", None, 4):
            with self.subTest(action=action):
                refused(self, "audit_action_not_declared", rules.check_audit_event, self.manifest, self.CORE, action, "run", {})

    def test_only_the_declared_detail_keys_are_allowed(self):
        refused(self, "audit_detail_key_not_declared", rules.check_audit_event, self.manifest, self.CORE, support.ACTION, "run", {"count": 1, "note": "x"})
        refused(self, "audit_detail_key_not_declared", rules.check_audit_event, self.manifest, self.CORE, support.ACTION, "run", {"module_id": "x"})

    def test_a_module_may_not_use_the_module_subject_or_an_unknown_one(self):
        for subject in ("module", "case", "", None, "RUN"):
            with self.subTest(subject=subject):
                refused(self, "audit_subject_type_not_allowed", rules.check_audit_event, self.manifest, self.CORE, support.ACTION, subject, {})
        self.assertEqual(rules.MODULE_SUBJECT_TYPES, ("run", "revision", "approval", "rejection"))


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.manifest = support.ctx_module()

    def test_scopes_map_to_the_generic_rule_inputs(self):
        self.assertEqual(rules.access_sets(self.manifest, (support.P_VIEW_OWN,)), (frozenset(), frozenset({support.P_VIEW_OWN})))
        self.assertEqual(rules.access_sets(self.manifest, (support.P_VIEW_ANY,)), (frozenset({support.P_VIEW_ANY}), frozenset()))
        self.assertEqual(rules.access_sets(self.manifest, [support.P_SUBMIT]), (frozenset({support.P_SUBMIT}), frozenset()))  # global reaches no particular record: any
        self.assertEqual(
            rules.access_sets(self.manifest, (support.P_VIEW_OWN, support.P_VIEW_ANY)), (frozenset({support.P_VIEW_ANY}), frozenset({support.P_VIEW_OWN}))
        )

    def test_a_permitted_core_permission_is_any_scope(self):
        self.assertEqual(rules.access_sets(self.manifest, ("audit.view_auditevent",)), (frozenset({"audit.view_auditevent"}), frozenset()))
        self.assertEqual(rules.PERMITTED_CORE_PERMISSIONS, {"audit.view_auditevent"})

    def test_other_permissions_are_refused(self):
        for permission in ("intake.approve_request", "other_mod.do_it", "auth.add_user", "ctx_mod.undeclared", "", None, 7):
            with self.subTest(permission=permission):
                refused(self, "permission_not_declared", rules.access_sets, self.manifest, (permission,))

    def test_one_bad_name_refuses_the_whole_request(self):
        refused(self, "permission_not_declared", rules.access_sets, self.manifest, (support.P_VIEW_ANY, "intake.approve_request"))

    def test_naming_nothing_is_refused(self):
        for value in ((), [], frozenset(), "intake.view_own_request", b"x", None, 5, {}):
            with self.subTest(value=value):
                refused(self, "no_permission_requested", rules.access_sets, self.manifest, value)

    def test_a_declared_permission_cannot_be_widened_by_the_caller(self):
        manifest = support.ctx_module(permissions=(PermissionDeclaration(support.P_VIEW_OWN, "own", "only own"),), role_grants=())
        any_set, own_set = rules.access_sets(manifest, (support.P_VIEW_OWN,))
        self.assertEqual((any_set, own_set), (frozenset(), frozenset({support.P_VIEW_OWN})))


class AvailabilityTests(unittest.TestCase):
    def test_available_passes(self):
        rules.check_available("ctx_mod", host_rules.Availability(True, host_rules.ENABLED))
        rules.check_available("ctx_mod", host_rules.Availability(True, host_rules.LEGACY_UNSYNCED))

    def test_each_reason_has_its_stable_code(self):
        expected = {
            host_rules.DISABLED: "module_disabled",
            host_rules.VERSION_MISMATCH: "module_version_mismatch",
            host_rules.MISSING_STATE: "module_state_missing",
            host_rules.DEPENDENCY_UNAVAILABLE: "module_dependency_unavailable",
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason):
                refused(self, code, rules.check_available, "ctx_mod", host_rules.Availability(False, reason))

    def test_an_unknown_reason_still_refuses(self):
        refused(self, "module_disabled", rules.check_available, "ctx_mod", host_rules.Availability(False, "something_new"))


class ErrorTests(unittest.TestCase):
    def test_every_raised_code_is_declared(self):
        self.assertIn("unknown_module", CONTEXT_CODES)
        with self.assertRaises(ValueError):
            ModuleContextError("not_a_code", "x")

    def test_messages_never_echo_values(self):
        manifest = support.ctx_module()
        for call in (
            lambda: rules.check_idempotency_key(manifest, "SECRET-key-value"),
            lambda: rules.declared_workflow(manifest, "secret.flow"),
            lambda: rules.access_sets(manifest, ("secret.permission",)),
            lambda: rules.check_audit_event(manifest, (), "secret.action", "run", {}),
        ):
            with self.assertRaises(ModuleContextError) as caught:
                call()
            self.assertNotIn("secret", str(caught.exception).lower())


class PurityTests(unittest.TestCase):
    def test_the_rules_import_only_the_standard_library_and_the_host_rules(self):
        import ast
        from pathlib import Path

        path = Path(rules.__file__)
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * node.level) + (node.module or ""))
        self.assertEqual(names, {"collections.abc", ".errors", ".rules"})


if __name__ == "__main__":
    unittest.main()
