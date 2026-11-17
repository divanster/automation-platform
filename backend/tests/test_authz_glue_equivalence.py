"""MOD-1b / S1b: the database glue around the access rules behaves exactly as before the generic algorithm moved to Core.

Django-runner tests with NO database: ``apps.intake.authorization`` (the glue the review services call) is exercised with ``load_actor``
replaced by a function that returns a fabricated ``Actor``, and every decision is compared with the frozen oracle
(``tests/authz_oracle.py``, the old algorithm). What this proves: the glue still reads the actor FIRST, applies the same decision, refuses
with the same stable code, logs the same single line (action, reason, public id; nothing else) and returns the same actor when allowed.

What it cannot prove, because it needs PostgreSQL and the VM never connects to one: how ``load_actor`` reads a REAL user row (inactive,
deleted, demoted users) and what the services do inside their transactions. ``load_actor``, the services and their transactions are
byte-for-byte unchanged by this slice; their existing PostgreSQL tests are the evidence for those parts (listed in
docs/architecture/module-contract.md, section 3.8).
"""

import ast
import uuid
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.accounts.authz import Actor
from apps.accounts.errors import AuthorizationError
from apps.accounts.roles import ROLE_PERMISSIONS, ROLES
from apps.intake import authorization
from apps.intake.access import Action

from . import authz_oracle as oracle
from . import authz_truth_table as table

INTAKE = Path(__file__).resolve().parent.parent / "apps" / "intake"
LOGGER = "apps.accounts.authz"
LOG_FORMAT = "authorization refused: action=%s reason=%s actor=%s"


def actor(*, pk=1, superuser=False, permissions=frozenset()) -> Actor:
    return Actor(user=object(), pk=pk, public_id=uuid.uuid4(), is_superuser=superuser, permissions=frozenset(permissions))


def with_actor(fake):
    return mock.patch.object(authorization, "load_actor", return_value=fake)


class RequirePermissionEquivalenceTests(SimpleTestCase):
    def test_every_action_and_permission_set_is_decided_as_the_old_code_decided_it(self):
        for held in table.subsets():
            for superuser in (False, True):
                fake = actor(superuser=superuser, permissions=held)
                for action in Action:
                    expected = oracle.permission_problem(action.value, is_superuser=superuser, permissions=held)
                    with with_actor(fake):
                        if expected is None:
                            with self.assertNoLogs(LOGGER, level="WARNING"):
                                self.assertIs(authorization.require_permission(object(), action), fake)
                        else:
                            with self.assertLogs(LOGGER, level="WARNING") as logs, self.assertRaises(AuthorizationError) as caught:
                                authorization.require_permission(object(), action)
                            self.assertEqual(caught.exception.reason, expected)
                            self.assertEqual(len(logs.records), 1)
                            self.assertEqual(logs.records[0].msg, LOG_FORMAT)
                            self.assertEqual(logs.records[0].args, (action.value, expected, fake.public_id))

    def test_the_refusal_message_carries_only_the_stable_code(self):
        with with_actor(actor()), self.assertLogs(LOGGER, level="WARNING"), self.assertRaises(AuthorizationError) as caught:
            authorization.require_permission(object(), Action.APPROVE)
        self.assertEqual(str(caught.exception), "The action is not permitted (missing_permission).")


class AuthorizeEquivalenceTests(SimpleTestCase):
    def test_every_action_permission_set_and_ownership_is_decided_as_the_old_code_decided_it(self):
        for held in table.subsets():
            for superuser in (False, True):
                for actor_pk, submitter_pk in table.SCENARIOS:
                    fake = actor(pk=actor_pk, superuser=superuser, permissions=held)
                    for action in Action:
                        expected = oracle.access_problem(action.value, is_superuser=superuser, permissions=held, actor_pk=actor_pk, submitter_pk=submitter_pk)
                        with with_actor(fake):
                            if expected is None:
                                self.assertIs(authorization.authorize(object(), action, submitter_pk=submitter_pk), fake)
                            else:
                                with self.assertLogs(LOGGER, level="WARNING") as logs, self.assertRaises(AuthorizationError) as caught:
                                    authorization.authorize(object(), action, submitter_pk=submitter_pk)
                                self.assertEqual(caught.exception.reason, expected)
                                self.assertEqual([r.args for r in logs.records], [(action.value, expected, fake.public_id)])

    def test_the_product_roles_match_the_old_code_for_own_and_other_requests(self):
        for role in (None, *ROLES):
            held = ROLE_PERMISSIONS[role] if role else frozenset()
            for superuser in (False, True):
                for action in Action:
                    for submitter_pk in (1, 2, None):
                        fake = actor(pk=1, superuser=superuser, permissions=held)
                        expected = oracle.access_problem(action.value, is_superuser=superuser, permissions=held, actor_pk=1, submitter_pk=submitter_pk)
                        with self.subTest(role=role, superuser=superuser, action=action.value, submitter=submitter_pk), with_actor(fake):
                            if expected is None:
                                authorization.authorize(object(), action, submitter_pk=submitter_pk)
                            else:
                                with self.assertLogs(LOGGER, level="WARNING"), self.assertRaises(AuthorizationError) as caught:
                                    authorization.authorize(object(), action, submitter_pk=submitter_pk)
                                self.assertEqual(caught.exception.reason, expected)

    def test_submitter_defaults_to_unknown(self):
        fake = actor(permissions={oracle.PERM_EDIT_OWN})
        with with_actor(fake), self.assertLogs(LOGGER, level="WARNING"), self.assertRaises(AuthorizationError) as caught:
            authorization.authorize(object(), Action.EDIT)
        self.assertEqual(caught.exception.reason, oracle.OUT_OF_SCOPE)


class PrecedenceTests(SimpleTestCase):
    """The order in which things are refused, which the services and the API depend on."""

    def test_an_unauthenticated_or_inactive_user_is_refused_before_the_action_or_any_rule_is_looked_at(self):
        for reason in ("not_authenticated", "inactive"):
            for call in (
                lambda: authorization.require_permission(object(), "not-an-action"),
                lambda: authorization.authorize(object(), "not-an-action", submitter_pk=1),
                lambda: authorization.require_permission(object(), Action.VIEW),
                lambda: authorization.authorize(object(), Action.VIEW, submitter_pk=1),
            ):
                with self.subTest(reason=reason), mock.patch.object(authorization, "load_actor", side_effect=AuthorizationError(reason)):
                    with self.assertRaises(AuthorizationError) as caught:
                        call()
                    self.assertEqual(caught.exception.reason, reason)

    def test_the_actor_is_read_on_every_call_and_before_the_decision(self):
        events = []

        def fake_load(user):
            events.append("load")
            return actor(superuser=True)

        with mock.patch.object(authorization, "load_actor", side_effect=fake_load) as loader:
            authorization.require_permission("user-1", Action.VIEW)
            authorization.authorize("user-1", Action.VIEW, submitter_pk=2)
        self.assertEqual(events, ["load", "load"])
        self.assertEqual([call.args for call in loader.call_args_list], [("user-1",), ("user-1",)])

    def test_an_unknown_action_is_a_value_error_only_for_a_user_who_was_accepted(self):
        for call in (
            lambda: authorization.require_permission(object(), "not-an-action"),
            lambda: authorization.authorize(object(), "not-an-action", submitter_pk=1),
        ):
            with with_actor(actor(superuser=True)), self.assertNoLogs(LOGGER, level="WARNING"), self.assertRaises(ValueError):
                call()

    def test_no_permission_is_missing_permission_and_never_reveals_ownership(self):
        # An actor with no permission for the action gets the same answer for their own record, somebody else's and an unknown one.
        answers = set()
        for submitter_pk in (1, 2, None):
            with with_actor(actor(pk=1)), self.assertLogs(LOGGER, level="WARNING"), self.assertRaises(AuthorizationError) as caught:
                authorization.authorize(object(), Action.APPROVE, submitter_pk=submitter_pk)
            answers.add(caught.exception.reason)
        self.assertEqual(answers, {oracle.MISSING_PERMISSION})

    def test_an_own_permission_holder_gets_out_of_scope_not_a_hint_about_existence(self):
        # require_permission passes for a holder of an own permission whatever the record; only authorize looks at the record.
        fake = actor(pk=1, permissions={oracle.PERM_VIEW_OWN})
        with with_actor(fake):
            self.assertIs(authorization.require_permission(object(), Action.VIEW), fake)


def function_node(path: Path, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef) and n.name == name)


class ServiceOrderTests(SimpleTestCase):
    """The services must still refuse for permission BEFORE any lookup (so a user with no permission learns nothing about a record, and
    cannot queue on its lock), and re-check scope AFTER the lock. These AST checks pin the order in the unchanged services."""

    def test_permission_is_required_before_any_record_is_looked_up_or_locked(self):
        for name in ("edit_request", "approve_request", "reject_request", "get_review_summary"):
            source = ast.unparse(function_node(INTAKE / "review.py", name))
            with self.subTest(service=name):
                first_lookup = min(i for i in (source.find("lock_run("), source.find("objects.get(")) if i >= 0)
                self.assertGreaterEqual(source.find("require_permission(user, Action."), 0)
                self.assertLess(source.find("require_permission(user, Action."), first_lookup)

    def test_scope_is_checked_after_the_record_is_known(self):
        for name, action in (("edit_request", "EDIT"), ("approve_request", "APPROVE"), ("reject_request", "REJECT"), ("get_review_summary", "VIEW")):
            source = ast.unparse(function_node(INTAKE / "review.py", name))
            with self.subTest(service=name):
                self.assertIn(f"authorize(user, Action.{action}, submitter_pk=source.submitted_by_id)", source)
                self.assertLess(source.find("_source_of(run)"), source.find("authorize(user, Action."))

    def test_the_write_services_authorize_inside_the_lock_transaction(self):
        for name in ("edit_request", "approve_request", "reject_request"):
            node = function_node(INTAKE / "review.py", name)
            withs = [n for n in ast.walk(node) if isinstance(n, ast.With)]
            with self.subTest(service=name):
                self.assertEqual(len(withs), 1)
                inside = ast.unparse(withs[0])
                self.assertLess(inside.find("lock_run("), inside.find("authorize(user, Action."))

    def test_submission_requires_permission_before_the_transaction(self):
        source = ast.unparse(function_node(INTAKE / "submission.py", "submit_request_as"))
        self.assertIn("require_permission(user, Action.SUBMIT)", source)
        self.assertLess(source.find("require_permission"), source.find("submit_request("))
