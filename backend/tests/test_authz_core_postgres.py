"""MOD-1b / S1b: Request Intake authorization on REAL users, compared with the frozen pre-refactor algorithm. Requires PostgreSQL.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and
docs/development/runbook.md). When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; the class
refuses to run against anything else.

STATUS: WRITTEN, NOT RUN. The development VM has no PostgreSQL and never connects to one. The owner is asked to run this file locally, and
the existing PostgreSQL suites listed in docs/architecture/module-contract.md (section 3.8), before accepting the slice.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_authz_core_postgres --tag requires_postgres -v 2

What it adds to the DB-free equivalence tests: the actor is read by the real ``load_actor`` (a fresh database read: groups, superuser flag,
inactive flag, deleted row), and the permissions come from the groups ``setup_roles`` really creates. Every outcome is compared with
``tests/authz_oracle.py``, which is the old algorithm. This file is ASCII only.
"""

import itertools
import os
import unittest

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, tag

from apps.accounts.authz import load_actor
from apps.accounts.errors import AuthorizationError
from apps.accounts.roles import APPROVER, AUDITOR, OPERATOR, ROLE_PERMISSIONS
from apps.intake import authorization
from apps.intake.access import Action, access_problem, permission_problem
from tests import authz_oracle as oracle
from tests.test_intake_postgres import DedicatedTestDatabaseMixin
from tests.test_review_postgres import make_user, setup_roles

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

User = get_user_model()


def outcome(call):
    """``None`` when the call returned, else the stable reason of the AuthorizationError it raised."""
    try:
        call()
    except AuthorizationError as error:
        return error.reason
    return None


@tag("requires_postgres")
@requires_postgres
class RealActorEquivalenceTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        setup_roles()
        self.users = {
            "operator": make_user("olive", OPERATOR),
            "other_operator": make_user("omar", OPERATOR),
            "approver": make_user("anna", APPROVER),
            "auditor": make_user("audrey", AUDITOR),
            "hybrid": make_user("hugo", OPERATOR, APPROVER),
            "superuser": make_user("root", superuser=True),
            "nobody": make_user("nina"),
        }

    def test_the_loaded_permissions_are_exactly_the_role_permissions(self):
        expected = {
            "operator": ROLE_PERMISSIONS[OPERATOR],
            "other_operator": ROLE_PERMISSIONS[OPERATOR],
            "approver": ROLE_PERMISSIONS[APPROVER],
            "auditor": ROLE_PERMISSIONS[AUDITOR],
            "hybrid": ROLE_PERMISSIONS[OPERATOR] | ROLE_PERMISSIONS[APPROVER],
            "nobody": frozenset(),
        }
        for name, permissions in expected.items():
            with self.subTest(user=name):
                self.assertEqual(load_actor(self.users[name]).permissions, permissions)

    def test_every_user_action_and_ownership_is_decided_as_the_old_algorithm_decided_it(self):
        for name, user in self.users.items():
            for submitter_name in (*self.users, None):
                submitter_pk = None if submitter_name is None else self.users[submitter_name].pk
                for action in Action:
                    actor = load_actor(user)
                    expected = oracle.access_problem(
                        action.value, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, submitter_pk=submitter_pk
                    )
                    with self.subTest(user=name, submitter=submitter_name, action=action.value):
                        self.assertEqual(outcome(lambda: authorization.authorize(user, action, submitter_pk=submitter_pk)), expected)
                        self.assertEqual(
                            access_problem(action, is_superuser=actor.is_superuser, permissions=actor.permissions, actor_pk=actor.pk, submitter_pk=submitter_pk),
                            expected,
                        )

    def test_require_permission_is_decided_as_the_old_algorithm_decided_it(self):
        for name, user in self.users.items():
            for action in Action:
                actor = load_actor(user)
                expected = oracle.permission_problem(action.value, is_superuser=actor.is_superuser, permissions=actor.permissions)
                with self.subTest(user=name, action=action.value):
                    self.assertEqual(outcome(lambda: authorization.require_permission(user, action)), expected)
                    self.assertEqual(permission_problem(action, is_superuser=actor.is_superuser, permissions=actor.permissions), expected)

    def test_every_pair_of_roles_is_decided_as_the_old_algorithm_decided_it(self):
        for number, roles in enumerate(itertools.chain.from_iterable(itertools.combinations((OPERATOR, APPROVER, AUDITOR), n) for n in (2, 3))):
            user = make_user(f"combo-{number}", *roles)
            for submitter_pk in (user.pk, self.users["operator"].pk, None):
                for action in Action:
                    actor = load_actor(user)
                    expected = oracle.access_problem(
                        action.value, is_superuser=False, permissions=actor.permissions, actor_pk=actor.pk, submitter_pk=submitter_pk
                    )
                    with self.subTest(roles=roles, action=action.value, own=submitter_pk == user.pk):
                        self.assertEqual(outcome(lambda: authorization.authorize(user, action, submitter_pk=submitter_pk)), expected)

    def test_inactive_users_are_refused_before_any_rule_even_with_every_role_and_as_superuser(self):
        for name, roles, superuser in (("dormant", (OPERATOR, APPROVER, AUDITOR), False), ("dormant-root", (), True)):
            user = make_user(name, *roles, superuser=superuser, active=False)
            for action in Action:
                with self.subTest(user=name, action=action.value):
                    self.assertEqual(outcome(lambda: authorization.require_permission(user, action)), "inactive")
                    self.assertEqual(outcome(lambda: authorization.authorize(user, action, submitter_pk=user.pk)), "inactive")

    def test_missing_unsaved_anonymous_and_non_user_callers_are_not_authenticated(self):
        gone = make_user("soon-gone", OPERATOR)
        User.objects.filter(pk=gone.pk).delete()
        callers = {"deleted": gone, "unsaved": User(username="ghost"), "anonymous": AnonymousUser(), "none": None, "string": "olive"}
        for name, caller in callers.items():
            for action in Action:
                with self.subTest(caller=name, action=action.value):
                    self.assertEqual(outcome(lambda: authorization.require_permission(caller, action)), "not_authenticated")
                    self.assertEqual(outcome(lambda: authorization.authorize(caller, action, submitter_pk=1)), "not_authenticated")

    def test_a_stale_user_object_is_judged_by_the_database_not_by_what_it_cached(self):
        stale = User.objects.get(pk=self.users["operator"].pk)
        self.assertTrue(stale.has_perm("intake.edit_own_request"))  # fills the per-object permission cache
        stale.groups.clear()
        self.assertTrue(stale.has_perm("intake.edit_own_request"))  # still cached on the stale object
        self.assertEqual(outcome(lambda: authorization.authorize(stale, Action.EDIT, submitter_pk=stale.pk)), oracle.MISSING_PERMISSION)

        demoted = User.objects.get(pk=self.users["superuser"].pk)
        User.objects.filter(pk=demoted.pk).update(is_superuser=False)
        self.assertEqual(outcome(lambda: authorization.authorize(demoted, Action.APPROVE, submitter_pk=1)), oracle.MISSING_PERMISSION)

        deactivated = User.objects.get(pk=self.users["approver"].pk)
        User.objects.filter(pk=deactivated.pk).update(is_active=False)
        self.assertEqual(outcome(lambda: authorization.authorize(deactivated, Action.APPROVE, submitter_pk=1)), "inactive")
