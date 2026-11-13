"""DB-free tests: the role and permission matrix, scope, and the setup_roles command's inputs.

Nothing here opens a database connection. The services that apply these rules to real rows are tested in
tests/test_review_postgres.py (PostgreSQL only).
"""

import itertools
from types import MappingProxyType

from django.test import SimpleTestCase

from apps.accounts import roles
from apps.accounts.roles import (
    ALL_PERMISSIONS,
    APPROVER,
    AUDITOR,
    OPERATOR,
    PERM_APPROVE,
    PERM_EDIT_OWN,
    PERM_REJECT,
    PERM_SUBMIT,
    PERM_VIEW_ANY,
    PERM_VIEW_AUDIT,
    PERM_VIEW_OWN,
    ROLE_PERMISSIONS,
)
from apps.audit.models import AuditEvent
from apps.intake.access import MISSING_PERMISSION, OUT_OF_SCOPE, Action, access_problem, permission_problem, scope_problem
from apps.intake.models import RequestSource

ME, OTHER = 1, 2


def problem(action, role=None, *, superuser=False, submitter=ME, actor=ME, extra=()):
    permissions = set(ROLE_PERMISSIONS[role]) if role else set()
    return access_problem(action, is_superuser=superuser, permissions=permissions | set(extra), actor_pk=actor, submitter_pk=submitter)


class RoleDefinitionTests(SimpleTestCase):
    def test_there_are_exactly_three_roles(self):
        self.assertEqual(roles.ROLES, ("Operator", "Approver", "Auditor"))
        self.assertEqual(set(ROLE_PERMISSIONS), set(roles.ROLES))

    def test_the_matrix_is_read_only(self):
        self.assertIsInstance(ROLE_PERMISSIONS, MappingProxyType)
        for permissions in ROLE_PERMISSIONS.values():
            self.assertIsInstance(permissions, frozenset)

    def test_operator_permissions(self):
        self.assertEqual(ROLE_PERMISSIONS[OPERATOR], {PERM_SUBMIT, PERM_VIEW_OWN, PERM_EDIT_OWN})

    def test_approver_permissions(self):
        self.assertEqual(ROLE_PERMISSIONS[APPROVER], {PERM_VIEW_ANY, PERM_APPROVE, PERM_REJECT})

    def test_auditor_is_read_only(self):
        self.assertEqual(ROLE_PERMISSIONS[AUDITOR], {PERM_VIEW_ANY, PERM_VIEW_AUDIT})
        for name in ROLE_PERMISSIONS[AUDITOR]:
            self.assertTrue(name.split(".")[1].startswith("view_"), name)

    def test_no_role_can_both_submit_or_edit_and_approve(self):
        for role, permissions in ROLE_PERMISSIONS.items():
            with self.subTest(role=role):
                changes = permissions & {PERM_SUBMIT, PERM_EDIT_OWN}
                decides = permissions & {PERM_APPROVE, PERM_REJECT}
                self.assertFalse(changes and decides)

    def test_every_permission_is_in_some_role_and_nothing_else_is(self):
        self.assertEqual(set().union(*ROLE_PERMISSIONS.values()), set(ALL_PERMISSIONS))

    def test_split_permission(self):
        self.assertEqual(roles.split_permission("intake.approve_request"), ("intake", "approve_request"))
        for bad in ("approve_request", ".x", "x.", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                roles.split_permission(bad)


class DeclaredPermissionTests(SimpleTestCase):
    """The roles module and the models that make Django create the permissions cannot drift apart."""

    def test_every_intake_permission_is_declared_on_the_request_source_model(self):
        declared = {f"intake.{codename}" for codename, _ in RequestSource._meta.permissions}
        self.assertEqual(declared, {p for p in ALL_PERMISSIONS if p.startswith("intake.")})

    def test_the_holder_model_has_no_default_permissions(self):
        self.assertEqual(tuple(RequestSource._meta.default_permissions), ())

    def test_the_audit_permission_is_djangos_view_permission_and_it_is_the_only_one(self):
        self.assertEqual(tuple(AuditEvent._meta.default_permissions), ("view",))
        self.assertEqual(PERM_VIEW_AUDIT, "audit.view_auditevent")

    def test_every_permission_name_is_app_dot_codename(self):
        for name in ALL_PERMISSIONS:
            app_label, codename = roles.split_permission(name)
            self.assertTrue(app_label and codename)


class PermissionMatrixTests(SimpleTestCase):
    """Every role against every action, on the actor's own request and on somebody else's."""

    EXPECTED_OWN = {
        OPERATOR: {Action.SUBMIT: None, Action.VIEW: None, Action.EDIT: None, Action.APPROVE: MISSING_PERMISSION, Action.REJECT: MISSING_PERMISSION, Action.VIEW_AUDIT: MISSING_PERMISSION},
        APPROVER: {Action.SUBMIT: MISSING_PERMISSION, Action.VIEW: None, Action.EDIT: MISSING_PERMISSION, Action.APPROVE: None, Action.REJECT: None, Action.VIEW_AUDIT: MISSING_PERMISSION},
        AUDITOR: {Action.SUBMIT: MISSING_PERMISSION, Action.VIEW: None, Action.EDIT: MISSING_PERMISSION, Action.APPROVE: MISSING_PERMISSION, Action.REJECT: MISSING_PERMISSION, Action.VIEW_AUDIT: None},
    }  # fmt: skip

    def test_own_request(self):
        for role, expected in self.EXPECTED_OWN.items():
            for action, reason in expected.items():
                with self.subTest(role=role, action=action.value):
                    self.assertEqual(problem(action, role, submitter=ME, actor=ME), reason)

    def test_somebody_elses_request_narrows_only_the_own_scope_permissions(self):
        narrowed = {
            (OPERATOR, Action.VIEW): OUT_OF_SCOPE,
            (OPERATOR, Action.EDIT): OUT_OF_SCOPE,
        }
        for role, expected in self.EXPECTED_OWN.items():
            for action, reason in expected.items():
                want = narrowed.get((role, action), reason)
                with self.subTest(role=role, action=action.value):
                    self.assertEqual(problem(action, role, submitter=OTHER, actor=ME), want)

    def test_a_user_with_no_role_can_do_nothing(self):
        for action in Action:
            with self.subTest(action=action.value):
                self.assertEqual(problem(action, None), MISSING_PERMISSION)

    def test_an_approver_pool_is_installation_wide(self):
        for submitter in (ME, OTHER, None):
            self.assertIsNone(problem(Action.APPROVE, APPROVER, submitter=submitter))
            self.assertIsNone(problem(Action.REJECT, APPROVER, submitter=submitter))
            self.assertIsNone(problem(Action.VIEW, AUDITOR, submitter=submitter))

    def test_own_scope_without_a_known_submitter_is_out_of_scope(self):
        self.assertEqual(problem(Action.EDIT, OPERATOR, submitter=None), OUT_OF_SCOPE)

    def test_roles_combine_by_union(self):
        both = set(ROLE_PERMISSIONS[OPERATOR]) | set(ROLE_PERMISSIONS[APPROVER])
        for action in (Action.SUBMIT, Action.EDIT, Action.APPROVE, Action.REJECT):
            self.assertIsNone(access_problem(action, is_superuser=False, permissions=both, actor_pk=ME, submitter_pk=ME))

    def test_a_single_permission_grants_only_its_own_action(self):
        grants = {
            PERM_SUBMIT: {Action.SUBMIT},
            PERM_VIEW_OWN: {Action.VIEW},
            PERM_EDIT_OWN: {Action.EDIT},
            PERM_VIEW_ANY: {Action.VIEW},
            PERM_APPROVE: {Action.APPROVE},
            PERM_REJECT: {Action.REJECT},
            PERM_VIEW_AUDIT: {Action.VIEW_AUDIT},
        }
        self.assertEqual(set(grants), set(ALL_PERMISSIONS))
        for permission, allowed in grants.items():
            for action in Action:
                with self.subTest(permission=permission, action=action.value):
                    result = access_problem(action, is_superuser=False, permissions={permission}, actor_pk=ME, submitter_pk=ME)
                    self.assertEqual(result is None, action in allowed)

    def test_a_permission_of_another_app_or_a_lookalike_grants_nothing(self):
        for name in ("other.approve_request", "intake.approve_requests", "approve_request", "intake.APPROVE_REQUEST", ""):
            with self.subTest(name=name):
                self.assertEqual(problem(Action.APPROVE, None, extra={name}), MISSING_PERMISSION)

    def test_unknown_actions_are_refused_loudly(self):
        with self.assertRaises(ValueError):
            permission_problem("delete", is_superuser=True, permissions=())


class SuperuserTests(SimpleTestCase):
    def test_a_superuser_bypasses_role_membership_and_own_scope(self):
        for action in Action:
            with self.subTest(action=action.value):
                self.assertIsNone(problem(action, None, superuser=True, submitter=OTHER))

    def test_the_bypass_is_only_in_the_access_rules(self):
        # Self-approval, state guards, stale and hash checks live in review_rules and take no is_superuser input.
        import inspect

        from apps.intake import review_rules

        for name in ("edit_problem", "approval_problem", "rejection_problem", "is_self_approval"):
            with self.subTest(function=name):
                self.assertNotIn("superuser", " ".join(inspect.signature(getattr(review_rules, name)).parameters))

    def test_permission_and_scope_functions_agree_with_the_combined_one(self):
        for action, role, superuser, submitter in itertools.product(Action, (None, *roles.ROLES), (False, True), (ME, OTHER)):
            permissions = set(ROLE_PERMISSIONS[role]) if role else set()
            kw = {"is_superuser": superuser, "permissions": permissions}
            first = permission_problem(action, **kw)
            expected = first or scope_problem(action, actor_pk=ME, submitter_pk=submitter, **kw)
            with self.subTest(action=action.value, role=role, superuser=superuser, submitter=submitter):
                self.assertEqual(access_problem(action, actor_pk=ME, submitter_pk=submitter, **kw), expected)
