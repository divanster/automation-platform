"""Pure-domain tests (standard-library unittest, no Django, no database) for permission scopes, default role grants and role derivation.

Includes the golden equivalence: a synthetic Request Intake manifest derives EXACTLY ``apps.accounts.roles.ROLE_PERMISSIONS``. Only
``apps.accounts.roles`` and ``apps.intake.access`` (both pure Python, no Django) are imported here, never ``apps.intake`` models or
services, and never from ``apps.modules``.
"""

import dataclasses
import itertools
import unittest
from types import MappingProxyType

from apps.accounts import roles as accounts_roles
from apps.intake import access as intake_access
from apps.modules.errors import ManifestError, RegistryError
from apps.modules.manifest import (
    KNOWN_ROLES,
    PERMISSION_SCOPES,
    RESERVED_NAMESPACES,
    PermissionDeclaration,
    RoleGrant,
)
from apps.modules.registry import CORE_PERMISSIONS, ModuleRegistry
from apps.modules.roles import CORE_ROLE_GRANTS, derive_all_permissions, derive_role_permissions

from .module_support import ContractTestCase, full_manifest, intake_like_manifest, manifest, named


def registry_of(*manifests) -> ModuleRegistry:
    return ModuleRegistry.build(manifests)


class PermissionScopeTests(ContractTestCase):
    def test_the_scopes_are_global_own_and_any(self):
        self.assertEqual(set(PERMISSION_SCOPES), {"global", "own", "any"})
        for scope in PERMISSION_SCOPES:
            with self.subTest(scope=scope):
                self.assertEqual(PermissionDeclaration("demo_module.do_it", scope).scope, scope)

    def test_the_default_scope_is_any_as_before(self):
        self.assertEqual(PermissionDeclaration("demo_module.do_it").scope, "any")

    def test_anything_else_is_refused(self):
        for bad in ("ANY", "Own", "owner", "everyone", "", " any", "any ", None, 1, True, ["any"], b"any"):
            with self.subTest(scope=bad):
                self.assertCode("invalid_identifier", ManifestError, PermissionDeclaration, "demo_module.do_it", bad)

    def test_a_declaration_is_immutable(self):
        declaration = PermissionDeclaration("demo_module.do_it", "own")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            declaration.scope = "any"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            declaration.permission_id = "demo_module.other"

    def test_permission_ids_must_belong_to_an_owned_namespace(self):
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, permissions=(PermissionDeclaration("other_module.do_it"),))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, permissions=(PermissionDeclaration("audit.view_auditevent"),))
        # The existing intake.* identifiers stay possible through an explicit extra namespace, and only through it.
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, module_id="request_intake", permissions=(PermissionDeclaration("intake.submit_request"),))
        self.assertEqual(len(intake_like_manifest().permissions), 6)

    def test_permission_ids_are_exactly_namespace_dot_codename(self):
        for bad in ("demo_module", "demo_module.", ".do_it", "demo_module.do.it", "Demo_module.do_it", "demo_module.Do_it", "demo_module.do it"):
            with self.subTest(permission=bad):
                self.assertCode("invalid_identifier", ManifestError, PermissionDeclaration, bad)

    def test_two_permissions_with_the_same_id_are_refused(self):
        self.assertCode("duplicate_permission", ManifestError, manifest, permissions=(PermissionDeclaration("demo_module.a", "own"), PermissionDeclaration("demo_module.a", "any")))

    def test_the_declared_scopes_are_part_of_the_fingerprint(self):
        own = manifest(permissions=(PermissionDeclaration("demo_module.a", "own"),))
        any_ = manifest(permissions=(PermissionDeclaration("demo_module.a", "any"),))
        self.assertNotEqual(own.fingerprint(), any_.fingerprint())


class RoleGrantTests(ContractTestCase):
    PERMS = (PermissionDeclaration("demo_module.view_it", "own"), PermissionDeclaration("demo_module.do_it", "any"), PermissionDeclaration("demo_module.admin_it", "global"))

    def with_grants(self, *grants, permissions=None):
        return manifest(permissions=self.PERMS if permissions is None else permissions, role_grants=grants)

    def test_the_roles_are_the_three_existing_groups(self):
        self.assertEqual(KNOWN_ROLES, ("Operator", "Approver", "Auditor"))
        self.assertEqual(KNOWN_ROLES, accounts_roles.ROLES)  # the contract's role vocabulary cannot drift from the real groups

    def test_a_valid_grant_is_stored_sorted_and_immutable(self):
        m = self.with_grants(RoleGrant("Operator", ("demo_module.view_it", "demo_module.do_it")))
        (grant,) = m.role_grants
        self.assertEqual(grant.permissions, ("demo_module.do_it", "demo_module.view_it"))
        self.assertIsInstance(m.role_grants, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            grant.role = "Auditor"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.role_grants = ()

    def test_grants_are_ordered_by_role_whatever_the_declaration_order(self):
        a = self.with_grants(RoleGrant("Auditor", ("demo_module.view_it",)), RoleGrant("Operator", ("demo_module.do_it",)))
        b = self.with_grants(RoleGrant("Operator", ("demo_module.do_it",)), RoleGrant("Auditor", ("demo_module.view_it",)))
        self.assertEqual([g.role for g in a.role_grants], ["Auditor", "Operator"])
        self.assertEqual(a.role_grants, b.role_grants)
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_unknown_roles_are_refused(self):
        for bad in ("operator", "OPERATOR", "Admin", "Superuser", "Viewer", "", " Operator", "Operator "):
            with self.subTest(role=bad):
                self.assertCode("unknown_role", ManifestError, RoleGrant, bad, ("demo_module.do_it",))
        for bad in (None, 5, b"Operator", ["Operator"]):
            with self.subTest(role=bad):
                self.assertCode("invalid_type", ManifestError, RoleGrant, bad, ("demo_module.do_it",))

    def test_a_module_cannot_define_a_role(self):
        # There is no way to add a role: the only vocabulary is KNOWN_ROLES, and a grant to any other name is an error.
        self.assertCode("unknown_role", ManifestError, RoleGrant, "Reviewer", ("demo_module.do_it",))
        self.assertNotIn("Reviewer", KNOWN_ROLES)
        self.assertEqual(tuple(derive_role_permissions(registry_of(intake_like_manifest()))), KNOWN_ROLES)  # derivation adds none either

    def test_a_grant_must_name_at_least_one_permission(self):
        self.assertCode("empty_role_grant", ManifestError, RoleGrant, "Operator", ())
        self.assertCode("empty_role_grant", ManifestError, RoleGrant, "Operator")

    def test_a_permission_is_not_granted_twice_to_one_role(self):
        self.assertCode("duplicate_role_grant", ManifestError, RoleGrant, "Operator", ("demo_module.do_it", "demo_module.do_it"))

    def test_a_role_is_not_granted_twice_by_one_module(self):
        self.assertCode(
            "duplicate_role_grant",
            ManifestError,
            self.with_grants,
            RoleGrant("Operator", ("demo_module.do_it",)),
            RoleGrant("Operator", ("demo_module.view_it",)),
        )

    def test_a_grant_may_only_name_permissions_the_module_declares(self):
        self.assertCode("role_grant_undeclared_permission", ManifestError, self.with_grants, RoleGrant("Operator", ("demo_module.never_declared",)))
        self.assertCode("role_grant_undeclared_permission", ManifestError, manifest, role_grants=(RoleGrant("Operator", ("demo_module.do_it",)),))  # no permissions at all

    def test_a_module_cannot_grant_another_modules_permission_or_a_core_one(self):
        other = named("other_mod", permissions=(PermissionDeclaration("other_mod.use_it"),))
        self.assertEqual(len(other.permissions), 1)
        for foreign in ("other_mod.use_it", "audit.view_auditevent", "intake.view_any_request"):
            with self.subTest(permission=foreign):
                self.assertCode("role_grant_undeclared_permission", ManifestError, self.with_grants, RoleGrant("Auditor", (foreign,)))

    def test_a_grant_cannot_use_a_malformed_permission_id(self):
        for bad in ("demo_module", "demo_module.do.it", "Demo.x", "", "demo module.x"):
            with self.subTest(permission=bad):
                self.assertCode("invalid_identifier", ManifestError, RoleGrant, "Operator", (bad,))

    def test_grants_work_for_permissions_in_an_extra_namespace(self):
        m = manifest(
            module_id="request_intake",
            extra_namespaces=("intake",),
            permissions=(PermissionDeclaration("intake.view_any_request"),),
            role_grants=(RoleGrant("Auditor", ("intake.view_any_request",)),),
        )
        self.assertEqual(m.role_grants[0].permissions, ("intake.view_any_request",))

    def test_declaring_a_permission_grants_it_to_nobody(self):
        m = self.with_grants()  # three permissions, no grants
        self.assertEqual(m.role_grants, ())
        derived = derive_role_permissions(registry_of(m))
        for role in KNOWN_ROLES:
            self.assertFalse({p.permission_id for p in m.permissions} & derived[role], role)

    def test_the_scope_of_a_permission_never_grants_it_to_a_role(self):
        # "any" or "own" permissions are not handed to roles by their scope; only an explicit grant does.
        m = manifest(permissions=(PermissionDeclaration("demo_module.view_any", "any"), PermissionDeclaration("demo_module.view_own", "own")), role_grants=(RoleGrant("Operator", ("demo_module.view_own",)),))
        derived = derive_role_permissions(registry_of(m))
        self.assertEqual(derived["Operator"], frozenset({"demo_module.view_own"}))
        self.assertNotIn("demo_module.view_any", set().union(*derived.values()))

    def test_grants_are_part_of_the_fingerprint_and_absent_grants_change_nothing(self):
        without = manifest(permissions=self.PERMS)
        with_grant = self.with_grants(RoleGrant("Operator", ("demo_module.do_it",)))
        self.assertNotEqual(without.fingerprint(), with_grant.fingerprint())
        self.assertNotIn("role_grants", without.to_dict())  # an unused optional field leaves the canonical form as it was
        self.assertNotIn("maintenance_hooks", without.to_dict())

    def test_a_manifest_without_the_new_fields_keeps_its_original_canonical_keys(self):
        original_keys = {
            "manifest_version", "module_id", "module_kind", "module_version", "core_api_compatibility", "license_feature", "display_name",
            "extra_namespaces", "dependencies", "capabilities_required", "capabilities_provided", "workflows", "task_handlers",
            "permissions", "audit_actions", "configuration_schema", "ui_contributions", "api_contributions", "update",
        }  # fmt: skip
        self.assertEqual(set(manifest().to_dict()), original_keys)

    def test_the_reserved_namespaces_now_include_permissions(self):
        self.assertIn("permissions", RESERVED_NAMESPACES)


class RoleDerivationTests(ContractTestCase):
    def test_an_empty_registry_yields_only_cores_own_grants(self):
        derived = derive_role_permissions(registry_of())
        self.assertEqual(tuple(derived), KNOWN_ROLES)
        self.assertEqual(derived["Operator"], frozenset())
        self.assertEqual(derived["Approver"], frozenset())
        self.assertEqual(derived["Auditor"], frozenset({"audit.view_auditevent"}))

    def test_the_result_is_immutable_and_has_the_roles_in_a_fixed_order(self):
        derived = derive_role_permissions(registry_of(intake_like_manifest()))
        self.assertIsInstance(derived, MappingProxyType)
        self.assertEqual(tuple(derived), ("Operator", "Approver", "Auditor"))
        for role in KNOWN_ROLES:
            self.assertIsInstance(derived[role], frozenset)
        with self.assertRaises(TypeError):
            derived["Operator"] = frozenset()

    def test_grants_of_several_modules_are_merged(self):
        a = named("alpha_mod", permissions=(PermissionDeclaration("alpha_mod.a"),), role_grants=(RoleGrant("Operator", ("alpha_mod.a",)),))
        b = named("beta_mod", permissions=(PermissionDeclaration("beta_mod.b"), PermissionDeclaration("beta_mod.c")), role_grants=(RoleGrant("Operator", ("beta_mod.b",)), RoleGrant("Auditor", ("beta_mod.c",))))
        derived = derive_role_permissions(registry_of(a, b))
        self.assertEqual(derived["Operator"], frozenset({"alpha_mod.a", "beta_mod.b"}))
        self.assertEqual(derived["Auditor"], frozenset({"audit.view_auditevent", "beta_mod.c"}))
        self.assertEqual(derived["Approver"], frozenset())

    def test_the_result_does_not_depend_on_the_order_the_modules_were_given(self):
        modules = [
            named("alpha_mod", permissions=(PermissionDeclaration("alpha_mod.a"),), role_grants=(RoleGrant("Approver", ("alpha_mod.a",)),)),
            named("beta_mod", permissions=(PermissionDeclaration("beta_mod.b"),), role_grants=(RoleGrant("Approver", ("beta_mod.b",)),)),
            intake_like_manifest(),
        ]
        results = {tuple((role, tuple(sorted(perms))) for role, perms in derive_role_permissions(registry_of(*order)).items()) for order in itertools.permutations(modules)}
        self.assertEqual(len(results), 1)

    def test_only_registered_modules_count_and_enabled_state_is_not_considered(self):
        self.assertEqual(derive_role_permissions(registry_of())["Operator"], frozenset())
        self.assertIn("intake.submit_request", derive_role_permissions(registry_of(intake_like_manifest()))["Operator"])

    def test_cores_grants_are_a_separate_explicit_input(self):
        registry = registry_of(intake_like_manifest())
        self.assertNotIn("audit.view_auditevent", derive_role_permissions(registry, core_grants={})["Auditor"])
        custom = derive_role_permissions(registry, core_grants={"Approver": ["audit.view_auditevent"]})
        self.assertIn("audit.view_auditevent", custom["Approver"])
        self.assertNotIn("audit.view_auditevent", custom["Auditor"])
        self.assertEqual(CORE_ROLE_GRANTS, {"Auditor": frozenset({"audit.view_auditevent"})})
        self.assertIsInstance(CORE_ROLE_GRANTS, MappingProxyType)

    def test_invalid_core_grants_are_refused(self):
        registry = registry_of()
        for bad in (None, [], "Auditor", {"Admin": ["audit.view_auditevent"]}, {"Auditor": "audit.view_auditevent"}, {"Auditor": [5]}, {"Auditor": ["not a permission"]}, {"Auditor": ["single"]}, {5: []}, {"Auditor": None}):
            with self.subTest(grants=bad):
                self.assertCode("invalid_core_grants", RegistryError, derive_role_permissions, registry, core_grants=bad)

    def test_only_a_registry_is_accepted(self):
        for bad in (None, [], (intake_like_manifest(),), "request_intake"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    derive_role_permissions(bad)
                with self.assertRaises(TypeError):
                    derive_all_permissions(bad)

    def test_all_permissions_are_the_declared_ones_plus_cores(self):
        self.assertEqual(derive_all_permissions(registry_of()), CORE_PERMISSIONS)
        everything = derive_all_permissions(registry_of(full_manifest("full_module"), intake_like_manifest()))
        self.assertIn("full_module.do_it", everything)
        self.assertIn("legacy_ns.view_it", everything)
        self.assertIn("intake.approve_request", everything)
        self.assertIn("audit.view_auditevent", everything)
        self.assertIsInstance(everything, frozenset)

    def test_a_declared_permission_nobody_is_granted_still_counts_as_a_known_permission(self):
        m = manifest(permissions=(PermissionDeclaration("demo_module.admin_it", "global"),))
        self.assertIn("demo_module.admin_it", derive_all_permissions(registry_of(m)))
        self.assertNotIn("demo_module.admin_it", set().union(*derive_role_permissions(registry_of(m)).values()))


class GoldenRequestIntakeEquivalenceTests(ContractTestCase):
    """The synthetic Request Intake manifest must reproduce today's role matrix EXACTLY. If either side changes, this fails."""

    def derived(self):
        return derive_role_permissions(registry_of(intake_like_manifest()))

    def test_the_derived_role_matrix_equals_the_existing_role_permissions(self):
        derived = self.derived()
        self.assertEqual(dict(derived), dict(accounts_roles.ROLE_PERMISSIONS))
        self.assertEqual(tuple(derived), tuple(accounts_roles.ROLE_PERMISSIONS))
        for role in accounts_roles.ROLES:
            with self.subTest(role=role):
                self.assertEqual(derived[role], accounts_roles.ROLE_PERMISSIONS[role])

    def test_every_permission_set_is_exactly_equal_not_just_a_superset(self):
        derived = self.derived()
        self.assertEqual(derived["Operator"], frozenset({accounts_roles.PERM_SUBMIT, accounts_roles.PERM_VIEW_OWN, accounts_roles.PERM_EDIT_OWN}))
        self.assertEqual(derived["Approver"], frozenset({accounts_roles.PERM_VIEW_ANY, accounts_roles.PERM_APPROVE, accounts_roles.PERM_REJECT}))
        self.assertEqual(derived["Auditor"], frozenset({accounts_roles.PERM_VIEW_ANY, accounts_roles.PERM_VIEW_AUDIT}))

    def test_the_known_permissions_equal_the_existing_all_permissions(self):
        self.assertEqual(derive_all_permissions(registry_of(intake_like_manifest())), accounts_roles.ALL_PERMISSIONS)

    def test_cores_own_grants_equal_the_corresponding_part_of_the_existing_matrix(self):
        # The Auditor's audit permission is declared by no module; CORE_ROLE_GRANTS must stay equal to what accounts.roles says.
        module_perms = {p.permission_id for p in intake_like_manifest().permissions}
        for role in accounts_roles.ROLES:
            core_part = accounts_roles.ROLE_PERMISSIONS[role] - module_perms
            self.assertEqual(core_part, CORE_ROLE_GRANTS.get(role, frozenset()), role)

    def test_the_declared_scopes_match_how_the_existing_authorization_rules_use_each_permission(self):
        scopes = {p.permission_id: p.scope for p in intake_like_manifest().permissions}
        own_in_rules = set().union(*intake_access._OWN.values())
        any_in_rules = set().union(*(perms for action, perms in intake_access._ANY.items() if action is not intake_access.Action.SUBMIT and action is not intake_access.Action.VIEW_AUDIT))
        self.assertEqual({p for p, s in scopes.items() if s == "own"}, own_in_rules)
        self.assertEqual({p for p, s in scopes.items() if s == "any"}, any_in_rules)
        self.assertEqual({p for p, s in scopes.items() if s == "global"}, intake_access._ANY[intake_access.Action.SUBMIT])
        self.assertEqual(set(scopes), set(accounts_roles.ALL_PERMISSIONS) - {accounts_roles.PERM_VIEW_AUDIT})

    def test_the_golden_manifest_uses_the_legacy_namespace_explicitly(self):
        m = intake_like_manifest()
        self.assertEqual(m.namespaces, ("intake", "request_intake"))
        self.assertTrue(all(p.permission_id.startswith("intake.") for p in m.permissions))
        self.assertEqual(ModuleRegistry.build([m]).namespace_owners, {"intake": "request_intake", "request_intake": "request_intake"})

    def test_the_golden_manifest_would_break_if_a_grant_changed(self):
        # A guard for the guard: change one grant and the equivalence must fail.
        altered = intake_like_manifest(
            role_grants=(
                RoleGrant("Operator", ("intake.submit_request", "intake.view_own_request", "intake.edit_own_request", "intake.approve_request")),
                RoleGrant("Approver", ("intake.view_any_request", "intake.approve_request", "intake.reject_request")),
                RoleGrant("Auditor", ("intake.view_any_request",)),
            )
        )
        self.assertNotEqual(dict(derive_role_permissions(registry_of(altered))), dict(accounts_roles.ROLE_PERMISSIONS))

    def test_no_role_in_the_golden_manifest_can_both_submit_or_edit_and_approve(self):
        # The existing policy (tests/test_review_roles.py) holds for the derived matrix too.
        derived = self.derived()
        for role, permissions in derived.items():
            with self.subTest(role=role):
                changes = permissions & {accounts_roles.PERM_SUBMIT, accounts_roles.PERM_EDIT_OWN}
                decides = permissions & {accounts_roles.PERM_APPROVE, accounts_roles.PERM_REJECT}
                self.assertFalse(changes and decides)


if __name__ == "__main__":
    unittest.main()
