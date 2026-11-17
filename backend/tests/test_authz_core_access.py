"""MOD-1b / S1b: the generic own / any / superuser algorithm now lives in ``apps.accounts.access``; Request Intake delegates to it.

PURE tests (plain ``unittest``, no Django, no database). The refactor must not change one authorization outcome, so the evidence is:

1. BASELINE. ``tests/authz_oracle.py`` is a frozen copy of the old ``apps.intake.access``. A truth table over EVERY combination of action,
   permission subset, superuser flag and (actor, submitter) scenario was recorded from the OLD code before the refactor
   (``BASELINE_DIGEST``). The oracle, the refactored ``apps.intake.access`` and Core with the Request Intake rules must all reproduce it,
   row by row.
2. COMPLETE COMPARISON, not sampling: 6 actions x 512 permission subsets x 2 superuser flags x 6 scenarios = 36 864 rows, three functions each.
3. The refusals, exceptions and precedence the services rely on.
4. Core is generic: it knows nothing about Request Intake and works with any rule.
5. Structure: Core is pure; Request Intake keeps only its action table and delegates; the algorithm exists once.
"""

import ast
import unittest
from pathlib import Path

from apps.accounts import access as core
from apps.accounts.access import MISSING_PERMISSION, OUT_OF_SCOPE, AccessRule
from apps.accounts.roles import ROLE_PERMISSIONS, ROLES
from apps.intake import access as intake

from . import authz_oracle as oracle
from . import authz_truth_table as table

BACKEND = Path(__file__).resolve().parent.parent

# Computed from the code BEFORE the refactor (rows:sha256). If this fails, an authorization outcome changed.
BASELINE_DIGEST = "36864:b5147acdb84902c36ad2aa71f9e8337da1d2b5e376c55083f5565607d21d9c57"


def core_with_intake_rules():
    """Core called directly with rules built from the ORACLE's tables (independent of ``apps.intake.access``), in the old call shape."""
    rules = {action.value: AccessRule(oracle._ANY[action], oracle._OWN[action]) for action in oracle.Action}

    def permission_problem(action, *, is_superuser, permissions):
        return core.permission_problem(rules[oracle.Action(action).value], is_superuser=is_superuser, permissions=permissions)

    def scope_problem(action, *, is_superuser, permissions, actor_pk, submitter_pk):
        return core.scope_problem(rules[oracle.Action(action).value], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=submitter_pk)

    def access_problem(action, *, is_superuser, permissions, actor_pk, submitter_pk=None):
        return core.access_problem(rules[oracle.Action(action).value], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=submitter_pk)

    return permission_problem, scope_problem, access_problem


IMPLEMENTATIONS = {
    "oracle (old code)": (oracle.permission_problem, oracle.scope_problem, oracle.access_problem),
    "apps.intake.access (delegating)": (intake.permission_problem, intake.scope_problem, intake.access_problem),
    "apps.accounts.access (Core, oracle rules)": core_with_intake_rules(),
}


class BaselineTests(unittest.TestCase):
    def test_the_truth_table_has_the_expected_size(self):
        self.assertEqual(len(ROLE_PERMISSIONS), 3)
        self.assertEqual(sum(1 for _ in table.rows(*IMPLEMENTATIONS["oracle (old code)"])), 6 * 512 * 2 * 6)

    def test_every_implementation_reproduces_the_recorded_baseline(self):
        for name, functions in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertEqual(table.digest(*functions), BASELINE_DIGEST)

    def test_every_row_is_identical_to_the_old_code(self):
        expected = list(table.rows(*IMPLEMENTATIONS["oracle (old code)"]))
        for name, functions in IMPLEMENTATIONS.items():
            actual = list(table.rows(*functions))
            self.assertEqual(len(actual), len(expected), name)
            mismatches = [(e, a) for e, a in zip(expected, actual) if e != a]
            self.assertEqual(mismatches[:3], [], f"{name}: {len(mismatches)} rows differ")

    def test_the_action_table_is_unchanged(self):
        self.assertEqual({a.value: s for a, s in intake._ANY.items()}, {a.value: s for a, s in oracle._ANY.items()})
        self.assertEqual({a.value: s for a, s in intake._OWN.items()}, {a.value: s for a, s in oracle._OWN.items()})
        self.assertEqual([a.value for a in intake.Action], [a.value for a in oracle.Action])
        self.assertEqual((intake.MISSING_PERMISSION, intake.OUT_OF_SCOPE), (oracle.MISSING_PERMISSION, oracle.OUT_OF_SCOPE))
        self.assertEqual((core.MISSING_PERMISSION, core.OUT_OF_SCOPE), (oracle.MISSING_PERMISSION, oracle.OUT_OF_SCOPE))

    def test_the_rules_are_built_from_the_same_table(self):
        for action in intake.Action:
            with self.subTest(action=action.value):
                self.assertEqual(intake._RULES[action], AccessRule(intake._ANY[action], intake._OWN[action]))


class RoleOutcomeTests(unittest.TestCase):
    """The same comparison, by product role, so a reader sees the outcomes that matter (also covered by the full table)."""

    def test_every_role_action_and_ownership_matches_the_old_code(self):
        for role, held, superuser, (actor, submitter) in [
            (r, ROLE_PERMISSIONS[r] if r else frozenset(), su, sc)
            for r in (None, *ROLES)
            for su in (False, True)
            for sc in table.SCENARIOS
        ]:
            for action in oracle.Action:
                kw = {"is_superuser": superuser, "permissions": held, "actor_pk": actor, "submitter_pk": submitter}
                with self.subTest(role=role, action=action.value, superuser=superuser, actor=actor, submitter=submitter):
                    self.assertEqual(intake.access_problem(action, **kw), oracle.access_problem(action, **kw))

    def test_a_user_holding_two_roles_matches_the_old_code(self):
        held = ROLE_PERMISSIONS["Operator"] | ROLE_PERMISSIONS["Approver"]
        for action in oracle.Action:
            for actor, submitter in table.SCENARIOS:
                kw = {"is_superuser": False, "permissions": held, "actor_pk": actor, "submitter_pk": submitter}
                with self.subTest(action=action.value, actor=actor, submitter=submitter):
                    self.assertEqual(intake.access_problem(action, **kw), oracle.access_problem(action, **kw))


class OutcomeTests(unittest.TestCase):
    """Specific outcomes the services and the API depend on, asserted on the delegating module and on the oracle alike."""

    def check(self, expected, action, *, held=(), superuser=False, actor=1, submitter=1):
        kw = {"is_superuser": superuser, "permissions": set(held), "actor_pk": actor, "submitter_pk": submitter}
        for name, (_, _, access) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertEqual(access(action, **kw), expected)

    def test_a_superuser_bypasses_roles_and_the_own_scope_for_every_action(self):
        for action in oracle.Action:
            self.check(None, action, superuser=True, submitter=2)

    def test_no_permission_is_missing_permission_even_for_somebody_elses_record(self):
        self.check(MISSING_PERMISSION, oracle.Action.VIEW, submitter=2)  # not out_of_scope: the actor holds nothing, so learns nothing

    def test_an_own_permission_reaches_only_the_actors_own_record(self):
        self.check(None, oracle.Action.EDIT, held={oracle.PERM_EDIT_OWN}, submitter=1)
        self.check(OUT_OF_SCOPE, oracle.Action.EDIT, held={oracle.PERM_EDIT_OWN}, submitter=2)
        self.check(OUT_OF_SCOPE, oracle.Action.EDIT, held={oracle.PERM_EDIT_OWN}, submitter=None)  # an unknown owner never matches

    def test_an_any_permission_reaches_every_record_and_beats_the_own_one(self):
        self.check(None, oracle.Action.VIEW, held={oracle.PERM_VIEW_ANY}, submitter=2)
        self.check(None, oracle.Action.VIEW, held={oracle.PERM_VIEW_ANY, oracle.PERM_VIEW_OWN}, submitter=2)

    def test_approve_and_reject_have_no_own_form(self):
        for action in (oracle.Action.APPROVE, oracle.Action.REJECT):
            self.check(MISSING_PERMISSION, action, held={oracle.PERM_EDIT_OWN, oracle.PERM_VIEW_OWN}, submitter=1)

    def test_pk_zero_is_a_real_owner_and_none_is_not(self):
        self.check(None, oracle.Action.EDIT, held={oracle.PERM_EDIT_OWN}, actor=0, submitter=0)
        self.check(OUT_OF_SCOPE, oracle.Action.EDIT, held={oracle.PERM_EDIT_OWN}, actor=None, submitter=None)

    def test_the_permission_part_comes_first_and_the_scope_part_alone_still_refuses(self):
        kw = {"is_superuser": False, "permissions": set(), "actor_pk": 1, "submitter_pk": 2}
        for name, (permission, scope, access) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertEqual(access(oracle.Action.EDIT, **kw), permission(oracle.Action.EDIT, is_superuser=False, permissions=set()))
                self.assertEqual(scope(oracle.Action.EDIT, **kw), MISSING_PERMISSION)  # called without the first check, it does not let anyone through

    def test_permissions_may_be_any_reiterable_collection(self):
        held = [oracle.PERM_VIEW_OWN]
        for kind in (list, tuple, set, frozenset, lambda items: dict.fromkeys(items)):
            for name, (_, _, access) in IMPLEMENTATIONS.items():
                with self.subTest(implementation=name, kind=getattr(kind, "__name__", "dict")):
                    self.assertIsNone(access(oracle.Action.VIEW, is_superuser=False, permissions=kind(held), actor_pk=1, submitter_pk=1))


class ExceptionTests(unittest.TestCase):
    def test_an_unknown_action_is_a_value_error_even_for_a_superuser(self):
        for name, (permission, scope, access) in IMPLEMENTATIONS.items():
            for bad in ("delete", "", "VIEW", None, 3):
                with self.subTest(implementation=name, action=bad):
                    with self.assertRaises(ValueError):
                        permission(bad, is_superuser=True, permissions=())
                    with self.assertRaises(ValueError):
                        scope(bad, is_superuser=True, permissions=(), actor_pk=1, submitter_pk=1)
                    with self.assertRaises(ValueError):
                        access(bad, is_superuser=True, permissions=(), actor_pk=1, submitter_pk=1)

    def test_an_action_may_be_given_as_its_plain_value(self):
        for name, (_, _, access) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertIsNone(access("view", is_superuser=True, permissions=(), actor_pk=1, submitter_pk=1))

    def test_a_superuser_never_looks_at_the_permissions(self):
        for name, (permission, scope, access) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertIsNone(permission("view", is_superuser=True, permissions=None))
                self.assertIsNone(scope("view", is_superuser=True, permissions=None, actor_pk=1, submitter_pk=1))
                self.assertIsNone(access("view", is_superuser=True, permissions=None, actor_pk=1, submitter_pk=1))

    def test_a_non_superuser_with_no_permission_collection_is_a_type_error_in_both(self):
        for name, (permission, _, _) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                with self.assertRaises(TypeError):
                    permission("view", is_superuser=False, permissions=None)

    def test_the_arguments_stay_keyword_only(self):
        for name, (permission, scope, access) in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                with self.assertRaises(TypeError):
                    permission("view", False, ())
                with self.assertRaises(TypeError):
                    access("view", False, (), 1)

    def test_the_public_signatures_of_intake_access_are_unchanged(self):
        import inspect

        expected = {
            "permission_problem": ["action", "is_superuser", "permissions"],
            "scope_problem": ["action", "is_superuser", "permissions", "actor_pk", "submitter_pk"],
            "access_problem": ["action", "is_superuser", "permissions", "actor_pk", "submitter_pk"],
        }
        for name, parameters in expected.items():
            signature = inspect.signature(getattr(intake, name))
            with self.subTest(function=name):
                self.assertEqual(list(signature.parameters), parameters)
        self.assertIsNone(inspect.signature(intake.access_problem).parameters["submitter_pk"].default)


class CoreIsGenericTests(unittest.TestCase):
    """Core has no idea what a request is: another module's permissions work with the same three functions."""

    RULE = AccessRule(any_permissions={"billing.view_any_invoice"}, own_permissions={"billing.view_own_invoice", "billing.edit_own_invoice"})

    def test_any_own_and_superuser_work_for_a_rule_that_is_not_request_intake(self):
        rule = self.RULE
        kw = {"actor_pk": 7, "owner_pk": 7}
        self.assertIsNone(core.access_problem(rule, is_superuser=False, permissions={"billing.view_own_invoice"}, **kw))
        self.assertEqual(core.access_problem(rule, is_superuser=False, permissions={"billing.view_own_invoice"}, actor_pk=7, owner_pk=8), OUT_OF_SCOPE)
        self.assertIsNone(core.access_problem(rule, is_superuser=False, permissions={"billing.view_any_invoice"}, actor_pk=7, owner_pk=8))
        self.assertEqual(core.access_problem(rule, is_superuser=False, permissions={"intake.view_any_request"}, **kw), MISSING_PERMISSION)
        self.assertIsNone(core.access_problem(rule, is_superuser=True, permissions=(), actor_pk=7, owner_pk=8))

    def test_an_owner_argument_is_optional_and_defaults_to_unknown(self):
        self.assertEqual(core.access_problem(self.RULE, is_superuser=False, permissions={"billing.view_own_invoice"}, actor_pk=7), OUT_OF_SCOPE)

    def test_a_rule_with_no_permissions_refuses_everyone_but_a_superuser(self):
        empty = AccessRule()
        self.assertEqual(core.access_problem(empty, is_superuser=False, permissions={"billing.view_any_invoice"}, actor_pk=1, owner_pk=1), MISSING_PERMISSION)
        self.assertIsNone(core.access_problem(empty, is_superuser=True, permissions=(), actor_pk=1, owner_pk=1))

    def test_a_rule_is_immutable_and_normalizes_its_permissions(self):
        rule = AccessRule(any_permissions=["a.x", "a.x"], own_permissions=("a.y",))
        self.assertEqual(rule.any_permissions, frozenset({"a.x"}))
        self.assertIsInstance(rule.own_permissions, frozenset)
        with self.assertRaises(Exception):
            rule.any_permissions = frozenset()  # frozen dataclass
        source = AccessRule(any_permissions={"a.x"})
        self.assertEqual(source, AccessRule(any_permissions=frozenset({"a.x"})))

    def test_a_rule_refuses_what_is_not_a_collection_of_permission_names(self):
        for bad in ("a.x", b"a.x", 5, None, [1], [("a", "x")], [["a.x"]]):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    AccessRule(any_permissions=bad)
                with self.assertRaises(TypeError):
                    AccessRule(own_permissions=bad)

    def test_the_functions_refuse_something_that_is_not_a_rule(self):
        for bad in (None, "intake.view_any_request", {"any_permissions": {"a.x"}}, intake.Action.VIEW, frozenset({"a.x"})):
            with self.subTest(bad=bad):
                for call in (
                    lambda: core.permission_problem(bad, is_superuser=True, permissions=()),
                    lambda: core.scope_problem(bad, is_superuser=True, permissions=(), actor_pk=1, owner_pk=1),
                    lambda: core.access_problem(bad, is_superuser=True, permissions=(), actor_pk=1, owner_pk=1),
                ):
                    with self.assertRaises(TypeError):
                        call()

    def test_the_rule_functions_do_not_modify_their_inputs(self):
        held = {"billing.view_own_invoice"}
        before = set(held)
        core.access_problem(self.RULE, is_superuser=False, permissions=held, actor_pk=1, owner_pk=1)
        self.assertEqual(held, before)


def source_of(*parts) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def imports_of(path_parts) -> set:
    found = set()
    for node in ast.walk(ast.parse(source_of(*path_parts))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def identifiers_of(path_parts) -> set:
    """Every name, attribute, argument and function name in the code (docstrings and comments are not code)."""
    found = set()
    for node in ast.walk(ast.parse(source_of(*path_parts))):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found.add(node.name)
    return found


class StructureTests(unittest.TestCase):
    def test_core_access_is_pure_standard_library(self):
        self.assertEqual(imports_of(("apps", "accounts", "access.py")), {"dataclasses"})
        used = identifiers_of(("apps", "accounts", "access.py"))
        self.assertEqual(used & {"logger", "logging", "print", "eval", "exec", "compile", "__import__", "open", "getattr", "setattr"} - {"setattr"}, set())

    def test_core_access_names_no_business_object(self):
        for identifier in identifiers_of(("apps", "accounts", "access.py")):
            for word in ("request", "revision", "approv", "submit", "intake", "case", "PERM_"):
                with self.subTest(identifier=identifier, word=word):
                    self.assertNotIn(word.lower(), identifier.lower())

    def test_intake_access_is_still_pure_and_only_adds_core(self):
        imports = imports_of(("apps", "intake", "access.py"))
        self.assertEqual(imports, {"enum", "apps.accounts", "apps.accounts.access", "apps.accounts.roles"})

    def test_intake_access_delegates_and_holds_no_copy_of_the_algorithm(self):
        tree = ast.parse(source_of("apps", "intake", "access.py"))
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(set(functions), {"permission_problem", "scope_problem", "access_problem"})
        for name, node in functions.items():
            statements = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]  # without the docstring
            with self.subTest(function=name):
                self.assertEqual(len(statements), 1)  # one return, no logic of its own
                self.assertIsInstance(statements[0], ast.Return)
                body_source = ast.unparse(statements[0])
                self.assertIn(f"core.{name}(", body_source)
                for banned in ("frozenset(", " & ", "held", " or ", " and ", " if "):
                    self.assertNotIn(banned, body_source)

    def test_the_algorithm_exists_once_in_the_application(self):
        needles = ('"out_of_scope"', '"missing_permission"', "held &")
        holders = {}
        for path in (BACKEND / "apps").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text and "tests" not in path.parts:
                    holders.setdefault(needle, set()).add(path.relative_to(BACKEND).as_posix())
        self.assertEqual(holders['"out_of_scope"'], {"apps/accounts/access.py", "apps/api/errors.py"})  # api/errors only maps a code to an HTTP answer
        self.assertEqual(holders['"missing_permission"'], {"apps/accounts/access.py", "apps/api/errors.py"})
        self.assertEqual(holders["held &"], {"apps/accounts/access.py"})

    def test_business_rules_did_not_move_into_core(self):
        core_source = source_of("apps", "accounts", "access.py")
        review_rules = source_of("apps", "intake", "review_rules.py")
        for name in ("is_self_approval", "approval_problem", "edit_problem", "rejection_problem"):
            self.assertIn(f"def {name}", review_rules)
            self.assertNotIn(name, core_source)

    def test_the_files_that_must_not_change_do_not_mention_the_new_module(self):
        for parts in (
            ("apps", "accounts", "management", "commands", "setup_roles.py"),
            ("apps", "accounts", "profile.py"),
            ("apps", "accounts", "roles.py"),
            ("apps", "tasks", "worker.py"),
            ("apps", "intake", "authorization.py"),
            ("apps", "intake", "review.py"),
            ("apps", "intake", "submission.py"),
            ("apps", "intake", "queries.py"),
        ):
            with self.subTest(file="/".join(parts)):
                self.assertNotIn("accounts.access", source_of(*parts))
                self.assertNotIn("accounts import access", source_of(*parts))

    def test_apps_modules_does_not_import_core_access(self):
        for path in (BACKEND / "apps" / "modules").glob("*.py"):
            self.assertNotIn("accounts.access", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
