"""DB-free tests for MOD-2: Request Intake as the first registered built-in module.

``SimpleTestCase`` refuses every database query. These tests prove that the manifest DECLARES what Request Intake already is, field by field, against the
existing code that is the source of truth (the model, ``apps.accounts.roles``, ``apps.intake.access``, the handler, the URL configuration, the React routes and
the audit vocabulary), that the registry and the worker composition are built from it, and that Core still imports nothing of the module. What the worker does
against PostgreSQL with the registered module (state creation, held and resumed tasks, a running attempt finishing, recovery) is in
tests/test_request_intake_module_postgres.py and is NOT run here.
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import reverse

from apps.accounts import roles as accounts_roles
from apps.audit import events
from apps.cases.attribution import WorkflowAttribution
from apps.intake import access
from apps.intake import module as intake_module
from apps.intake.handlers import EXTRACT_HANDLER
from apps.intake.models import RequestSource
from apps.intake.module import MANIFEST
from apps.intake.policy import KIND_EXTRACT, MODULE_ID, WORKFLOW_KEY, WORKFLOW_VERSION
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.submission import REQUEST_INTAKE_ATTRIBUTION
from apps.module_host import rules
from apps.modules.manifest import ModuleManifest
from apps.modules.registry import ModuleRegistry
from apps.modules.roles import derive_all_permissions, derive_role_permissions
from config import audit_vocabulary, module_context, modules, task_registry

from .module_support import intake_like_manifest

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend" / "src"
REGISTRY = modules.build_registry()


def child_environment(**extra) -> dict:
    """A minimal environment for a throw-away interpreter that imports project code from ``backend`` (the working directory): no settings, no secrets."""
    keep = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH") if name in os.environ}
    return {**keep, **extra}


def code_imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            names.add(base)
            names |= {f"{base}.{alias.name}" for alias in node.names}
    return names


# --- the manifest ---------------------------------------------------------------------------------------------------------------


class ManifestContentsTests(SimpleTestCase):
    def test_the_manifest_is_a_valid_business_module_with_the_existing_identity(self):
        self.assertIsInstance(MANIFEST, ModuleManifest)
        self.assertEqual(
            (MANIFEST.manifest_version, MANIFEST.module_id, MANIFEST.module_kind.value, MANIFEST.module_version),
            (1, "request_intake", "business", "1.0.0"),
        )
        self.assertEqual((MANIFEST.core_api_compatibility, MANIFEST.license_feature, MANIFEST.display_name), (">=1.0.0,<2.0.0", "mod.request_intake", "Request Intake"))

    def test_the_legacy_prefix_is_kept_through_extra_namespaces(self):
        self.assertEqual(MANIFEST.extra_namespaces, ("intake",))
        self.assertEqual(MANIFEST.namespaces, ("intake", "request_intake"))

    def test_the_workflow_and_the_task_kind_are_the_stored_and_queued_values(self):
        (workflow,) = MANIFEST.workflows
        self.assertEqual((workflow.workflow_key, workflow.definition_version, workflow.task_kinds), ("request_intake", 1, ("request_intake.extract",)))
        (declaration,) = MANIFEST.task_handlers
        self.assertEqual(declaration.kind, "request_intake.extract")
        self.assertIsNone(declaration.handler)  # the handler object stays in apps.intake.registry; the manifest imports no model
        # the values written to every WorkflowRun (S5) and to every queued task are the ones the manifest declares
        self.assertEqual((MODULE_ID, WORKFLOW_KEY, WORKFLOW_VERSION, KIND_EXTRACT), ("request_intake", "request_intake", 1, "request_intake.extract"))
        self.assertEqual(REQUEST_INTAKE_ATTRIBUTION, WorkflowAttribution(MANIFEST.module_id, workflow.workflow_key, workflow.definition_version))

    def test_the_required_capabilities_are_the_five_core_capabilities_and_nothing_is_provided_or_depended_on(self):
        self.assertEqual(set(MANIFEST.capabilities_required), {"task.enqueue", "audit.record", "case.run", "revision.approval", "permissions.check"})
        self.assertEqual((MANIFEST.capabilities_provided, MANIFEST.dependencies, MANIFEST.configuration_schema), ((), (), None))

    def test_no_audit_action_is_declared_because_every_action_request_intake_emits_is_cores(self):
        self.assertEqual(MANIFEST.audit_actions, ())
        for action in (
            "request.submitted", "revision.created", "request.edited", "run.state_changed", "approval.recorded", "approval.invalidated", "rejection.recorded",
        ):  # fmt: skip
            self.assertIn(action, events.ACTIONS)

    def test_no_module_hook_is_declared_the_reconciler_stays_the_after_iteration_hook_and_core_adds_one_maintenance_hook(self):
        self.assertEqual(MANIFEST.maintenance_hooks, ())
        self.assertEqual(REGISTRY.maintenance_hooks, ())
        self.assertIs(task_registry.COMPOSITION.after_iteration, task_registry.reconcile_after_iteration)
        # DOC-2: Core (not this module) added ONE maintenance hook, which closes text extractions whose task failed for good; Request Intake's reconciler is untouched.
        self.assertEqual([hook.hook_id for hook in task_registry.COMPOSITION.maintenance_hooks], ["documents.reconcile_text"])

    def test_the_manifest_matches_the_synthetic_one_the_contract_was_designed_against_apart_from_what_this_slice_adds(self):
        synthetic = intake_like_manifest()
        actual, expected = MANIFEST.to_dict(), synthetic.to_dict()
        for key in ("maintenance_hooks",):
            expected.pop(key, None)  # deliberately not declared (see the manifest's docstring)
        for key in ("ui_contributions", "api_contributions", "workflows", "task_handlers"):
            actual.pop(key)
            expected.pop(key)  # descriptions, and the screens, are this slice's; compared one by one elsewhere
        self.assertEqual(actual, expected)

    def test_the_manifest_module_is_pure(self):
        tree = ast.parse((BACKEND / "apps" / "intake" / "module.py").read_text(encoding="utf-8"))
        sources = {("." * node.level) + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        plain = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertEqual(plain, set())
        self.assertEqual(sources, {"apps.modules.manifest", ".policy"})  # the manifest classes, and the pure policy module (standard library only)

    def test_importing_the_manifest_in_a_fresh_interpreter_loads_no_django_and_no_models(self):
        code = (
            "import sys; import apps.intake.module; "
            "bad = sorted(m for m in sys.modules if m == 'django' or m.startswith('django.') or m.startswith('apps.intake.models') or m.startswith('apps.intake.services')); "
            "print(bad); sys.exit(1 if bad else 0)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=child_environment())
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr[-500:])

    def test_the_module_does_not_reach_for_dynamic_loading(self):
        source = (BACKEND / "apps" / "intake" / "module.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertEqual(names & {"__import__", "import_module", "importlib", "eval", "exec", "open", "environ", "settings"}, set())


# --- permissions and roles ----------------------------------------------------------------------------------------------------------


class PermissionsAndRolesTests(SimpleTestCase):
    def test_the_declared_permissions_are_exactly_the_models_with_the_same_names(self):
        declared = {p.permission_id: p.description for p in MANIFEST.permissions}
        real = {f"{RequestSource._meta.app_label}.{codename}": name for codename, name in RequestSource._meta.permissions}
        self.assertEqual(declared, real)

    def test_the_constants_equal_the_role_modules_constants(self):
        for name in ("PERM_SUBMIT", "PERM_VIEW_OWN", "PERM_EDIT_OWN", "PERM_VIEW_ANY", "PERM_APPROVE", "PERM_REJECT"):
            with self.subTest(name=name):
                self.assertEqual(getattr(intake_module, name), getattr(accounts_roles, name))

    def test_the_derived_role_permissions_equal_the_existing_matrix_exactly(self):
        self.assertEqual(dict(derive_role_permissions(REGISTRY)), dict(accounts_roles.ROLE_PERMISSIONS))
        self.assertEqual(derive_all_permissions(REGISTRY), accounts_roles.ALL_PERMISSIONS)  # what /api/auth/me/ may report

    def test_no_permission_was_broadened_or_added(self):
        self.assertEqual({p.permission_id for p in MANIFEST.permissions}, accounts_roles.ALL_PERMISSIONS - {accounts_roles.PERM_VIEW_AUDIT})
        granted = {permission for grant in MANIFEST.role_grants for permission in grant.permissions}
        self.assertEqual(granted, {p.permission_id for p in MANIFEST.permissions})  # every declared permission is held by some role, none beyond

    def test_the_declared_scopes_agree_with_the_access_table(self):
        own = {p.permission_id for p in MANIFEST.permissions if p.scope == "own"}
        any_scope = {p.permission_id for p in MANIFEST.permissions if p.scope in ("any", "global")}
        table_own = set().union(*access._OWN.values())
        table_any = set().union(*access._ANY.values()) - {accounts_roles.PERM_VIEW_AUDIT}  # the audit view permission is Core's
        self.assertEqual(own, table_own)
        self.assertEqual(any_scope, table_any)
        self.assertEqual({p.permission_id for p in MANIFEST.permissions if p.scope == "global"}, {accounts_roles.PERM_SUBMIT})

    def test_setup_roles_the_profile_and_the_authorization_glue_still_do_not_import_the_module_machinery(self):
        for relative in (
            "apps/accounts/management/commands/setup_roles.py", "apps/accounts/profile.py", "apps/accounts/roles.py", "apps/accounts/authz.py",
            "apps/accounts/access.py", "apps/intake/access.py", "apps/intake/authorization.py", "apps/intake/review.py", "apps/intake/submission.py",
        ):  # fmt: skip
            source = (BACKEND / relative).read_text(encoding="utf-8")
            with self.subTest(file=relative):
                self.assertNotIn("config.modules", source)
                self.assertNotIn("apps.module_host", source)
                self.assertNotIn("apps.intake.module", source)


# --- audit ----------------------------------------------------------------------------------------------------------------------------


class AuditVocabularyTests(SimpleTestCase):
    def test_the_vocabulary_composed_from_the_built_in_registry_equals_cores(self):
        built = audit_vocabulary.build_audit_vocabulary()
        self.assertEqual(built, events.CORE_VOCABULARY)
        self.assertEqual(built.fingerprint, events.CORE_VOCABULARY.fingerprint)

    def test_every_core_action_and_detail_key_is_unchanged(self):
        built = audit_vocabulary.build_audit_vocabulary()
        for action in events.ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(built.allows_action(action))
                self.assertEqual(built.detail_keys_of(action), events.CORE_VOCABULARY.detail_keys_of(action))
        self.assertEqual(built.actions, events.CORE_VOCABULARY.actions)
        self.assertEqual(dict(built.module_actions), {})

    def test_the_module_cannot_declare_a_core_action(self):
        from apps.audit.vocabulary import AuditVocabularyError, ModuleAuditAction

        with self.assertRaises(AuditVocabularyError):
            events.CORE_VOCABULARY.with_modules((ModuleAuditAction("request_intake", "request.submitted", ()),), REGISTRY.namespace_owners, frozenset())


# --- the registry ---------------------------------------------------------------------------------------------------------------------


class RegistryCompositionTests(SimpleTestCase):
    def test_the_registry_holds_exactly_request_intake_and_is_deterministic(self):
        self.assertEqual(REGISTRY.module_ids, ("request_intake",))
        rebuilt = ModuleRegistry.build([MANIFEST])
        self.assertEqual(rebuilt.fingerprint, REGISTRY.fingerprint)
        self.assertEqual(modules.build_registry().fingerprint, REGISTRY.fingerprint)
        self.assertRegex(REGISTRY.fingerprint, r"^[0-9a-f]{64}$")

    def test_the_fingerprint_is_the_same_in_another_process_with_another_hash_seed(self):
        code = "from config.modules import build_registry; print(build_registry().fingerprint)"
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=child_environment(PYTHONHASHSEED="12345"))
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertEqual(result.stdout.strip(), REGISTRY.fingerprint)

    def test_ownership_tables_hold_the_existing_identifiers_once(self):
        self.assertEqual(dict(REGISTRY.task_kinds), {"request_intake.extract": "request_intake"})
        self.assertEqual(set(REGISTRY.permissions), {p for p in accounts_roles.ALL_PERMISSIONS if p.startswith("intake.")})
        self.assertEqual(dict(REGISTRY.workflows), {"request_intake": "request_intake"})
        self.assertEqual(dict(REGISTRY.api_routes), {"requests": "request_intake"})
        self.assertEqual(dict(REGISTRY.namespace_owners), {"intake": "request_intake", "request_intake": "request_intake"})
        self.assertEqual(dict(REGISTRY.audit_actions), {})
        self.assertEqual(REGISTRY.dependencies_of("request_intake"), ())

    def test_core_provides_the_required_capabilities_and_core_namespaces_are_not_claimed(self):
        self.assertEqual(dict(REGISTRY.capability_providers), {})  # nothing a module provides; Core's five are required, not provided
        for namespace in ("request", "revision", "run", "approval", "rejection", "audit", "task", "case"):
            self.assertNotIn(namespace, REGISTRY.namespace_owners)

    def test_the_composition_root_names_the_manifest_statically(self):
        self.assertEqual(modules.BUILTIN_MODULES, (MANIFEST,))
        self.assertIs(modules.REQUEST_INTAKE_MANIFEST, MANIFEST)
        self.assertIsInstance(modules.BUILTIN_MODULES, tuple)


# --- the worker -----------------------------------------------------------------------------------------------------------------------


class WorkerCompositionTests(SimpleTestCase):
    def test_the_handler_is_the_existing_object_and_its_kind_is_the_declared_one(self):
        self.assertIs(PRODUCTION_REGISTRY[KIND_EXTRACT], EXTRACT_HANDLER)
        self.assertEqual(EXTRACT_HANDLER.kind, MANIFEST.task_handlers[0].kind)
        self.assertFalse(EXTRACT_HANDLER.is_test_handler)
        self.assertEqual(set(PRODUCTION_REGISTRY), {"test.add_numbers", "request_intake.extract"})
        self.assertEqual(set(task_registry.COMPOSITION.registry), set(PRODUCTION_REGISTRY) | {"documents.extract_text"})  # DOC-2 adds Core's one handler; Request Intake's are the same objects
        self.assertIs(task_registry.COMPOSITION.registry[KIND_EXTRACT], EXTRACT_HANDLER)

    def test_every_production_kind_is_owned_exactly_once(self):
        legacy, owned = task_registry.LEGACY_UNGATED_KINDS, frozenset(REGISTRY.task_kinds)
        self.assertEqual(legacy, {"test.add_numbers"})
        self.assertEqual(legacy & owned, frozenset())
        self.assertEqual(task_registry.CORE_UNGATED_KINDS & (legacy | owned), frozenset())
        self.assertEqual(task_registry.UNGATED_KINDS | owned, frozenset(task_registry.WORKER_REGISTRY))
        rules.check_handler_composition(REGISTRY, task_registry.WORKER_REGISTRY, task_registry.UNGATED_KINDS)

    def test_there_is_one_worker_composition_and_the_module_context_uses_the_same_registry_and_handlers(self):
        self.assertIs(task_registry.MODULE_REGISTRY.get("request_intake"), MANIFEST)
        self.assertEqual(module_context.REGISTRY.fingerprint, task_registry.MODULE_REGISTRY.fingerprint)
        self.assertEqual(sorted(p.name for p in (BACKEND / "config").glob("*registry*.py")), ["task_registry.py"])

    def test_the_worker_composition_does_not_import_the_manifest_or_name_a_module(self):
        source = (BACKEND / "config" / "task_registry.py").read_text(encoding="utf-8")
        self.assertNotIn("apps.intake.module", source)
        self.assertNotIn("EXTRACT_HANDLER", source)  # the handler is not named here any more: the composition is checked against the registry instead


# --- URLs and screens -----------------------------------------------------------------------------------------------------------------


def normalized(path: str) -> str:
    return re.sub(r":[A-Za-z_]+", ":param", path if path.startswith("/") else "/" + path)


# The dashboard (BE-DASH-1 / FE-6) is a Core screen and a Core API route: it is not a Request Intake contribution and is not in the
# manifest. Adding to these sets is a deliberate decision; an undeclared module route still fails the equality checks below.
CORE_API_PREFIXES = {"auth", "dashboard", "documents"}  # ``documents``: DOC-2's text extraction routes under /api/documents/<id>/ (Core, not a module)
CORE_SCREENS = {"/dashboard"}
CORE_NAVIGATION = {
    "/dashboard": ("intake.view_own_request", "intake.view_any_request"),  # any-of display hint, not a module permission
    "/settings/ai": None,  # AI-SET-3: a Core administrator screen; no permission hint, it is flagged administratorOnly in navigation.ts
}
CORE_ADMINISTRATOR_NAVIGATION = {"/settings/ai"}


def navigation_entries():
    """The sidebar entries of frontend/src/components/shell/navigation.ts as {to: (permission, anyPermission)}."""
    source = (FRONTEND / "components" / "shell" / "navigation.ts").read_text(encoding="utf-8")
    body = source.split("export const NAV_ITEMS", 1)[1]
    entries = {}
    for block in re.findall(r"\{\s*id:.*?isActive", body, flags=re.S):
        to = re.search(r'\bto:\s*"([^"]+)"', block).group(1)
        single = re.search(r'\bpermission:\s*"([^"]+)"', block)
        anyof = re.search(r"\banyPermission:\s*\[([^\]]*)\]", block)
        entries[to] = (single.group(1) if single else None, tuple(re.findall(r'"([^"]+)"', anyof.group(1))) if anyof else None)
    return entries


def administrator_only_entries():
    """The ``to`` of every sidebar entry of navigation.ts flagged ``administratorOnly: true``."""
    source = (FRONTEND / "components" / "shell" / "navigation.ts").read_text(encoding="utf-8")
    body = source.split("export const NAV_ITEMS", 1)[1]
    return {re.search(r'\bto:\s*"([^"]+)"', block).group(1) for block in re.findall(r"\{\s*id:.*?isActive", body, flags=re.S) if re.search(r"\badministratorOnly:\s*true", block)}


class RoutesTests(SimpleTestCase):
    def test_the_api_prefix_is_the_one_first_segment_the_api_serves_besides_auth(self):
        from apps.api import urls

        segments = {str(p.pattern).split("/")[0] for p in urls.urlpatterns}
        # Core routes are listed explicitly; any other first segment must be a declared module contribution.
        self.assertEqual(segments - CORE_API_PREFIXES, {c.route_prefix for c in MANIFEST.api_contributions})
        self.assertEqual(reverse("api:request-collection"), "/api/requests/")
        self.assertEqual(reverse("api:request-detail", args=["00000000-0000-0000-0000-000000000001"]), "/api/requests/00000000-0000-0000-0000-000000000001/")

    def test_no_declared_prefix_is_a_core_route(self):
        self.assertEqual({c.route_prefix for c in MANIFEST.api_contributions} & {"auth", "health", "modules", "admin"}, set())

    def test_the_declared_screens_equal_the_react_routes(self):
        app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        react = {normalized(p) for p in re.findall(r'<Route path="([^"]+)"', app)} - {"/login", "/*"}
        declared = {normalized(u.path) for u in MANIFEST.ui_contributions if u.kind == "route"}
        self.assertEqual(declared, react - CORE_SCREENS)  # every non-Core route is declared by the module, and nothing else is
        self.assertTrue(CORE_SCREENS <= react)  # a Core screen exists only while it really is a route
        self.assertFalse(declared & CORE_SCREENS)  # the module does not claim a Core screen
        self.assertEqual(len(declared), len([u for u in MANIFEST.ui_contributions if u.kind == "route"]))  # no duplicate route

    def test_the_declared_navigation_equals_the_layouts_links_and_its_permission_hint(self):
        entries = navigation_entries()  # the sidebar moved from Layout.tsx to shell/navigation.ts in FE-4
        declared = {u.path for u in MANIFEST.ui_contributions if u.kind == "navigation"}
        self.assertEqual(declared, set(entries) - set(CORE_NAVIGATION))
        self.assertFalse(declared & set(CORE_NAVIGATION))
        hints = {u.path: u.permission for u in MANIFEST.ui_contributions if u.kind == "navigation"}
        self.assertEqual(hints, {"/requests": None, "/requests/new": accounts_roles.PERM_SUBMIT})
        for path, hint in hints.items():  # the sidebar's hint is the manifest's, with no any-of list
            self.assertEqual(entries[path], (hint, None), path)
        for path, anyof in CORE_NAVIGATION.items():  # a Core entry's hint is exactly its any-of list (none for the administrator screen), and nothing else
            self.assertEqual(entries[path], (None, anyof), path)
        self.assertEqual(set(CORE_NAVIGATION["/dashboard"]), {accounts_roles.PERM_VIEW_OWN, accounts_roles.PERM_VIEW_ANY})
        self.assertEqual(administrator_only_entries(), CORE_ADMINISTRATOR_NAVIGATION)  # only the Core AI settings entry is administrator-only; no module entry is

    def test_the_frontend_was_not_given_dynamic_loading(self):
        for path in FRONTEND.rglob("*.ts*"):
            if ".test." in path.name or "test" in path.parts:
                continue  # tests may import lazily; the application code may not
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r"\bimport\(")  # no dynamic JavaScript loading


# --- Core stays business-agnostic -----------------------------------------------------------------------------------------------------


class CoreStaysIndependentTests(SimpleTestCase):
    def test_core_packages_and_the_module_host_never_import_request_intake(self):
        for package in ("accounts", "audit", "cases", "tasks", "modules", "module_host", "health"):
            for path in sorted((BACKEND / "apps" / package).rglob("*.py")):
                if "migrations" in path.parts:
                    continue
                with self.subTest(file=path.relative_to(BACKEND).as_posix()):
                    self.assertEqual({n for n in code_imports(path) if n == "apps.intake" or n.startswith("apps.intake.")}, set())

    def test_only_the_composition_roots_import_the_manifest(self):
        importers = []
        for path in sorted(BACKEND.rglob("*.py")):
            if "tests" in path.parts or "migrations" in path.parts or path.name == "module.py":
                continue
            if any(n == "apps.intake.module" or n.startswith("apps.intake.module.") for n in code_imports(path)):
                importers.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(importers, ["config/modules.py"])

    def test_the_request_intake_source_does_not_use_the_module_machinery(self):
        for path in sorted((BACKEND / "apps" / "intake").glob("*.py")):
            if path.name == "module.py":
                continue
            text = path.read_text(encoding="utf-8")
            for word in ("config.modules", "module_context", "ModuleContext", "apps.module_host", "build_registry"):
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, text)
