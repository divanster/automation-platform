"""DB-free tests under the Django runner: the synthetic Request Intake manifest against the REAL model and role declarations.

``tests/test_module_permissions.py`` proves the pure equivalence without Django. This file adds the part that needs Django's model
metadata: the permission codenames the real ``RequestSource`` model declares. It reads class attributes only; no query is made.
"""

from pathlib import Path

from django.test import SimpleTestCase

from apps.accounts import roles as accounts_roles
from apps.intake.models import RequestSource
from apps.modules.registry import ModuleRegistry
from apps.modules.roles import derive_all_permissions, derive_role_permissions

from .module_support import intake_like_manifest

BACKEND = Path(__file__).resolve().parent.parent


class GoldenAgainstTheRealModelTests(SimpleTestCase):
    def test_the_declared_permissions_are_exactly_the_ones_the_real_model_declares(self):
        declared = {p.permission_id for p in intake_like_manifest().permissions}
        real = {f"{RequestSource._meta.app_label}.{codename}" for codename, _name in RequestSource._meta.permissions}
        self.assertEqual(declared, real)

    def test_the_real_model_still_carries_no_default_permissions(self):
        self.assertEqual(tuple(RequestSource._meta.default_permissions), ())

    def test_the_declared_descriptions_equal_the_real_permission_names(self):
        declared = {p.permission_id: p.description for p in intake_like_manifest().permissions}
        real = {f"{RequestSource._meta.app_label}.{codename}": name for codename, name in RequestSource._meta.permissions}
        self.assertEqual(declared, real)

    def test_the_derivation_still_equals_the_existing_matrix(self):
        registry = ModuleRegistry.build([intake_like_manifest()])
        self.assertEqual(dict(derive_role_permissions(registry)), dict(accounts_roles.ROLE_PERMISSIONS))
        self.assertEqual(derive_all_permissions(registry), accounts_roles.ALL_PERMISSIONS)

    def test_setup_roles_and_the_profile_still_read_the_existing_role_module_and_nothing_from_the_module_package(self):
        for relative in ("apps/accounts/management/commands/setup_roles.py", "apps/accounts/profile.py", "apps/accounts/roles.py", "apps/accounts/authz.py"):
            source = (BACKEND / relative).read_text(encoding="utf-8")
            with self.subTest(file=relative):
                self.assertNotIn("apps.modules", source)
                self.assertNotIn("config.modules", source)
        self.assertIn("from apps.accounts.roles import ROLE_PERMISSIONS", (BACKEND / "apps/accounts/management/commands/setup_roles.py").read_text(encoding="utf-8"))
