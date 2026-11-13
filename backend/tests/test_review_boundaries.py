"""DB-free static tests: the review services reuse the domain and keep their authorization inside them.

These read source code (the ``ast`` module); nothing is imported with side effects and no database is
touched. They exist to catch a regression that a behavioural test could miss: a second implementation of
validation, hashing or approval, a service that forgets to authorize, or a caller-supplied approver.
"""

import ast
import inspect
from pathlib import Path

from django.test import SimpleTestCase

from apps.intake import review, submission
from tests.test_request_intake_boundaries import imported_modules

BACKEND = Path(__file__).resolve().parent.parent
INTAKE = BACKEND / "apps" / "intake"
ACCOUNTS = BACKEND / "apps" / "accounts"
AUDIT = BACKEND / "apps" / "audit"
CASES = BACKEND / "apps" / "cases"
NEW_INTAKE_MODULES = ("access.py", "authorization.py", "editing.py", "review.py", "review_rules.py")
PURE_INTAKE_MODULES = ("access.py", "editing.py", "review_rules.py", "errors.py", "policy.py")


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def names_used(path):
    t = tree(path)
    return {n.id for n in ast.walk(t) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(t) if isinstance(n, ast.Attribute)}


def called_names(function_node):
    names = []
    for node in ast.walk(function_node):
        if isinstance(node, ast.Call):
            func = node.func
            names.append(func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "")
    return names


def function(path, name):
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


class NoDuplicatedDomainLogicTests(SimpleTestCase):
    def test_the_new_modules_do_not_validate_canonicalize_or_hash(self):
        banned = {"canonicalize_request", "canonical_decimal_string", "validate_request", "dumps", "loads", "sha1", "sha256", "md5", "blake2b", "check_export_eligibility", "consistency_problems"}
        for name in NEW_INTAKE_MODULES:
            with self.subTest(module=name):
                self.assertEqual(names_used(INTAKE / name) & banned, set())
                self.assertEqual(imported_modules(INTAKE / name) & {"hashlib", "hmac", "json", "decimal"}, set())

    def test_the_edit_builder_goes_through_the_one_domain_entry_point(self):
        used = names_used(INTAKE / "editing.py")
        self.assertIn("create_request_intake_revision", used)
        source = (INTAKE / "editing.py").read_text(encoding="utf-8")
        self.assertIn("from workflows.revision_approval.request_intake_revision import create_request_intake_revision", source)

    def test_approve_revision_is_imported_from_the_domain_and_never_defined_here(self):
        source = (INTAKE / "review.py").read_text(encoding="utf-8")
        self.assertIn("from workflows.revision_approval import RevisionFinding, approve_revision, validate_uuid", source)
        defined = {n.name for n in ast.walk(tree(INTAKE / "review.py")) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(defined & {"approve_revision", "record_approval", "record_revision", "transition_run", "create_request_intake_revision"}, set())

    def test_the_approval_service_reuses_approve_revision_record_approval_and_transition_run(self):
        calls = called_names(function(INTAKE / "review.py", "approve_request"))
        for needed in ("approve_revision", "record_approval", "transition_run", "lock_run"):
            self.assertIn(needed, calls)

    def test_the_edit_service_reuses_record_revision_and_the_builder(self):
        calls = called_names(function(INTAKE / "review.py", "edit_request"))
        for needed in ("build_edit_revision", "record_revision", "lock_run"):
            self.assertIn(needed, calls)

    def test_the_rejection_service_reuses_record_rejection(self):
        self.assertIn("record_rejection", called_names(function(INTAKE / "review.py", "reject_request")))

    def test_run_state_is_changed_only_through_transition_run(self):
        stored = [
            n
            for n in ast.walk(tree(INTAKE / "review.py"))
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and n.attr in {"state", "failed_step", "current_revision", "current_revision_id"}
        ]
        self.assertEqual(stored, [])
        self.assertNotIn("save", names_used(INTAKE / "review.py"))
        self.assertNotIn("update_fields", (INTAKE / "review.py").read_text(encoding="utf-8"))

    def test_the_cases_app_knows_nothing_of_intake_audit_or_accounts_rules(self):
        for path in CASES.glob("*.py"):
            with self.subTest(module=path.name):
                for node in ast.walk(tree(path)):
                    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        self.assertFalse(node.module.startswith(("apps.intake", "apps.audit", "apps.accounts")), node.module)


class AuthorizationInsideTheServicesTests(SimpleTestCase):
    SERVICES = {
        "edit_request": "EDIT",
        "approve_request": "APPROVE",
        "reject_request": "REJECT",
        "get_review_summary": "VIEW",
        "list_audit_events": "VIEW_AUDIT",
    }

    def test_every_review_service_checks_the_permission_before_anything_else_is_read_or_locked(self):
        for name, action in self.SERVICES.items():
            node = function(INTAKE / "review.py", name)
            with self.subTest(service=name):
                source = ast.unparse(node)
                self.assertIn(f"require_permission(user, Action.{action})", source)
                self.assertLess(source.index("require_permission"), min(i for i in (source.find("lock_run"), source.find("WorkflowRun.objects"), source.find("list_run_events")) if i >= 0))

    def test_every_service_that_touches_a_request_applies_the_scope_check_under_the_lock(self):
        for name in ("edit_request", "approve_request", "reject_request", "get_review_summary"):
            source = ast.unparse(function(INTAKE / "review.py", name))
            with self.subTest(service=name):
                self.assertIn("authorize(user, Action.", source)
                self.assertIn("submitter_pk=source.submitted_by_id", source)

    def test_the_authorized_submit_requires_the_permission_and_uses_the_fresh_user(self):
        source = ast.unparse(function(INTAKE / "submission.py", "submit_request_as"))
        self.assertIn("require_permission(user, Action.SUBMIT)", source)
        self.assertIn("submitted_by=actor.user", source)

    def test_no_service_takes_an_approver_or_actor_identifier_from_the_caller(self):
        for name in self.SERVICES:
            parameters = set(inspect.signature(getattr(review, name)).parameters)
            with self.subTest(service=name):
                self.assertEqual(parameters & {"approver_id", "approver", "actor_id", "public_id", "rejected_by", "edited_by", "approver_public_id"}, set())
                self.assertIn("user", parameters)

    def test_the_services_take_the_acting_user_by_keyword_only(self):
        for name in list(self.SERVICES) + ["submit_request_as"]:
            module = submission if name == "submit_request_as" else review
            kinds = {p.kind for p in inspect.signature(getattr(module, name)).parameters.values()}
            with self.subTest(service=name):
                self.assertEqual(kinds, {inspect.Parameter.KEYWORD_ONLY})

    def test_the_approver_recorded_is_the_actors_public_id(self):
        source = ast.unparse(function(INTAKE / "review.py", "approve_request"))
        self.assertIn("approve_revision(current, approver_id=actor.public_id, warnings_reviewed=True)", source)
        self.assertIn("warnings_reviewed=True", source)
        self.assertEqual(source.count("warnings_reviewed"), 1)  # True appears once, after the acknowledgement check
        self.assertLess(source.index("raise_if_refused"), source.index("approve_revision(current"))

    def test_the_rejector_and_editor_recorded_come_from_the_loaded_actor(self):
        self.assertIn("rejected_by=actor.public_id", ast.unparse(function(INTAKE / "review.py", "reject_request")))
        self.assertIn("edited_by=actor.user", ast.unparse(function(INTAKE / "review.py", "edit_request")))

    def test_the_actor_is_always_reloaded_and_never_trusted(self):
        source = (ACCOUNTS / "authz.py").read_text(encoding="utf-8")
        self.assertIn("_default_manager.filter(pk=user.pk).first()", source)
        self.assertIn("is_authenticated", source)
        self.assertIn("is_active", source)

    def test_a_service_is_atomic_and_audits_inside_the_same_block(self):
        for name in ("edit_request", "approve_request", "reject_request"):
            node = function(INTAKE / "review.py", name)
            withs = [n for n in ast.walk(node) if isinstance(n, ast.With)]
            with self.subTest(service=name):
                self.assertEqual(len(withs), 1)
                inside = {call for stmt in withs[0].body for call in called_names(stmt)}
                self.assertIn("record_event", inside)
                self.assertIn("lock_run", inside)


class PureModuleTests(SimpleTestCase):
    def test_the_pure_modules_import_no_database_code(self):
        for name in ("access.py", "review_rules.py", "errors.py", "policy.py"):
            with self.subTest(module=name):
                self.assertEqual(imported_modules(INTAKE / name) & {"django", "psycopg", "rest_framework"}, set())

    def test_the_edit_builder_imports_no_django(self):
        self.assertEqual(imported_modules(INTAKE / "editing.py") & {"django", "psycopg", "rest_framework"}, set())

    def test_audit_events_and_roles_are_standard_library_only(self):
        self.assertEqual(imported_modules(AUDIT / "events.py") - {"re", "uuid", "dataclasses", "types"}, set())
        self.assertEqual(imported_modules(ACCOUNTS / "roles.py") - {"types"}, set())

    def test_no_new_module_executes_code_by_name_or_uses_the_network(self):
        forbidden = {"importlib", "subprocess", "socket", "ssl", "urllib", "http", "requests", "pickle", "marshal", "ctypes", "anthropic", "openai"}
        for path in [*(INTAKE / n for n in NEW_INTAKE_MODULES), *AUDIT.glob("*.py"), ACCOUNTS / "authz.py", ACCOUNTS / "roles.py"]:
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & forbidden, set())
                self.assertEqual({n.id for n in ast.walk(tree(path)) if isinstance(n, ast.Name)} & {"eval", "exec", "compile", "__import__", "open", "globals", "locals"}, set())


class LoggingTests(SimpleTestCase):
    def test_the_authorization_code_logs_only_codes_and_the_public_id(self):
        source = (ACCOUNTS / "authz.py").read_text(encoding="utf-8")
        log_lines = [line for line in source.splitlines() if "logger." in line]
        self.assertEqual(len(log_lines), 1)
        self.assertIn("action=%s reason=%s actor=%s", log_lines[0])
        for word in ("username", "email", "password", "first_name", "last_name"):
            self.assertNotIn(word, " ".join(log_lines))

    def test_no_new_module_logs_or_prints_request_content(self):
        for path in [*(INTAKE / n for n in NEW_INTAKE_MODULES), *AUDIT.glob("*.py")]:
            used = names_used(path)
            with self.subTest(module=path.name):
                self.assertEqual(used & {"print", "pprint", "logger", "logging"}, set() if path.name != "authorization.py" else used & {"logger"})
