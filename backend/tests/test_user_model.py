"""DB-free tests for the project user model and its migration state."""

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import AbstractUser
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase


class UserModelTests(SimpleTestCase):
    def test_auth_user_model_resolves_to_accounts_user(self):
        user_model = get_user_model()
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")
        self.assertEqual(user_model._meta.label, "accounts.User")
        self.assertEqual(user_model._meta.app_label, "accounts")

    def test_extends_abstract_user(self):
        self.assertTrue(issubclass(get_user_model(), AbstractUser))

    def test_has_no_added_fields(self):
        concrete_own = {f.name for f in get_user_model()._meta.local_fields}
        concrete_own |= {f.name for f in get_user_model()._meta.local_many_to_many}
        abstract_fields = {f.name for f in AbstractUser._meta.local_fields}
        abstract_fields |= {f.name for f in AbstractUser._meta.local_many_to_many}
        # Everything on accounts.User is either inherited from AbstractUser or
        # the automatic primary key.
        self.assertEqual(concrete_own - abstract_fields, {"id"})
        self.assertTrue(abstract_fields <= concrete_own)

    def test_standard_username_behavior(self):
        self.assertEqual(get_user_model().USERNAME_FIELD, "username")
        self.assertTrue(get_user_model()._meta.get_field("username").unique)

    def test_registered_with_standard_user_admin(self):
        self.assertIs(type(admin.site._registry[get_user_model()]), UserAdmin)

    def test_app_labels(self):
        self.assertEqual(apps.get_app_config("accounts").name, "apps.accounts")
        self.assertEqual(apps.get_app_config("health").name, "apps.health")


class MigrationStateTests(SimpleTestCase):
    def test_models_match_migrations(self):
        """No model change is missing a migration. Does not touch any database."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        autodetector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps))
        changes = autodetector.changes(graph=loader.graph)
        self.assertEqual(changes, {}, f"Missing migrations for: {sorted(changes)}")

    def test_initial_accounts_migration_exists(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertIn(("accounts", "0001_initial"), loader.disk_migrations)
