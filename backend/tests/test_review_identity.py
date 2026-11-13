"""DB-free tests: ``User.public_id`` and the three safe migration steps that add it.

The migrations are checked as code (operation types, order, reversibility) and the backfill function is run
against a small in-memory stand-in for the historical user model. Applying them to a real database with
existing users is tested in tests/test_review_postgres.py (PostgreSQL only).
"""

import importlib
import uuid
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from apps.accounts.models import User

STEP_1 = "apps.accounts.migrations.0002_user_public_id"
STEP_2 = "apps.accounts.migrations.0003_backfill_user_public_id"
STEP_3 = "apps.accounts.migrations.0004_user_public_id_constraints"


def load(path):
    return importlib.import_module(path).Migration


class PublicIdFieldTests(SimpleTestCase):
    def setUp(self):
        self.field = User._meta.get_field("public_id")

    def test_is_a_unique_non_null_uuid_that_nobody_can_edit(self):
        self.assertIsInstance(self.field, models.UUIDField)
        self.assertTrue(self.field.unique)
        self.assertFalse(self.field.null)
        self.assertFalse(self.field.editable)

    def test_defaults_to_a_fresh_random_uuid_per_user(self):
        self.assertIs(self.field.default, uuid.uuid4)
        first, second = User(username="a"), User(username="b")
        self.assertIsInstance(first.public_id, uuid.UUID)
        self.assertNotEqual(first.public_id, second.public_id)
        self.assertEqual(first.public_id.version, 4)

    def test_the_integer_primary_key_is_unchanged(self):
        pk = User._meta.pk
        self.assertEqual((pk.name, pk.get_internal_type()), ("id", "BigAutoField"))
        self.assertIs(get_user_model(), User)

    def test_the_public_id_is_not_a_credential_or_a_login_field(self):
        self.assertEqual(User.USERNAME_FIELD, "username")
        self.assertNotIn("public_id", User.REQUIRED_FIELDS)
        self.assertNotIn("public_id", (User.EMAIL_FIELD, User.USERNAME_FIELD))

    def test_the_stored_string_form_is_not_the_password_or_username(self):
        user = User(username="alice")
        self.assertNotIn("alice", str(user.public_id))
        self.assertEqual(user.password, "")


class MigrationStructureTests(SimpleTestCase):
    def test_three_steps_in_a_chain_after_the_initial_migration(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        names = [name for app, name in loader.disk_migrations if app == "accounts"]
        self.assertEqual(sorted(names), ["0001_initial", "0002_user_public_id", "0003_backfill_user_public_id", "0004_user_public_id_constraints"])
        self.assertEqual(load(STEP_1).dependencies, [("accounts", "0001_initial")])
        self.assertEqual(load(STEP_2).dependencies, [("accounts", "0002_user_public_id")])
        self.assertEqual(load(STEP_3).dependencies, [("accounts", "0003_backfill_user_public_id")])

    def test_step_1_only_adds_a_nullable_column_with_no_constraint_and_no_default(self):
        (operation,) = load(STEP_1).operations
        self.assertIsInstance(operation, migrations.AddField)
        self.assertEqual((operation.model_name, operation.name), ("user", "public_id"))
        self.assertTrue(operation.field.null)
        self.assertFalse(operation.field.unique)
        self.assertFalse(operation.field.has_default())

    def test_step_2_is_only_a_reversible_data_migration(self):
        (operation,) = load(STEP_2).operations
        self.assertIsInstance(operation, migrations.RunPython)
        self.assertTrue(operation.reversible)
        self.assertIs(operation.reverse_code, migrations.RunPython.noop)

    def test_step_3_only_tightens_the_column(self):
        (operation,) = load(STEP_3).operations
        self.assertIsInstance(operation, migrations.AlterField)
        field = operation.field
        self.assertTrue(field.unique)
        self.assertFalse(field.null)
        self.assertIs(field.default, uuid.uuid4)

    def test_the_data_step_and_the_schema_steps_are_separate_migrations(self):
        # Each migration is its own PostgreSQL transaction: data and DDL never share one.
        for path in (STEP_1, STEP_2, STEP_3):
            kinds = {type(op).__name__ for op in load(path).operations}
            self.assertEqual(len(kinds), 1, path)
        self.assertTrue(load(STEP_2).atomic)

    def test_the_final_state_equals_the_model(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        state = loader.project_state(("accounts", "0004_user_public_id_constraints"))
        field = state.models["accounts", "user"].fields["public_id"]
        model_field = User._meta.get_field("public_id")
        self.assertEqual(field.deconstruct()[1:], model_field.deconstruct()[1:])


class FakeUsers:
    """Just enough of the historical manager for the backfill: ``using``, ``filter``, ``values_list``, ``update``."""

    def __init__(self, rows):
        self.rows = rows  # {pk: public_id or None}
        self.updates = []
        self.lookups = {}

    def using(self, alias):
        return self

    def filter(self, **lookups):
        view = FakeUsers(self.rows)
        view.updates = self.updates
        view.lookups = lookups
        return view

    def _matching(self):
        pks = [self.lookups["pk"]] if "pk" in self.lookups else list(self.rows)
        if self.lookups.get("public_id__isnull"):
            pks = [pk for pk in pks if self.rows[pk] is None]
        return pks

    def values_list(self, *fields, flat=False):
        assert fields == ("pk",) and flat
        return self._matching()

    def update(self, **values):
        self.updates.append(dict(self.lookups))
        for pk in self._matching():
            self.rows[pk] = values["public_id"]
        return len(self._matching())


def run_backfill(rows):
    manager = FakeUsers(rows)
    historical = SimpleNamespace(_default_manager=manager)
    apps_stub = SimpleNamespace(get_model=lambda app, model: historical if (app, model) == ("accounts", "User") else None)
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
    importlib.import_module(STEP_2).backfill_public_ids(apps_stub, schema_editor)
    return manager


class BackfillTests(SimpleTestCase):
    def test_every_existing_user_gets_its_own_distinct_uuid(self):
        rows = {pk: None for pk in range(1, 51)}
        run_backfill(rows)
        values = list(rows.values())
        self.assertTrue(all(isinstance(v, uuid.UUID) and v.version == 4 for v in values))
        self.assertEqual(len(set(values)), 50)

    def test_users_are_updated_individually_never_with_one_shared_value(self):
        manager = run_backfill({pk: None for pk in range(1, 6)})
        self.assertEqual(len(manager.updates), 5)
        self.assertEqual({u["pk"] for u in manager.updates}, {1, 2, 3, 4, 5})

    def test_an_existing_value_is_kept(self):
        keep = uuid.uuid4()
        rows = {1: keep, 2: None}
        run_backfill(rows)
        self.assertEqual(rows[1], keep)
        self.assertIsInstance(rows[2], uuid.UUID)

    def test_running_it_again_changes_nothing(self):
        rows = {1: None, 2: None}
        run_backfill(rows)
        before = dict(rows)
        manager = run_backfill(rows)
        self.assertEqual(rows, before)
        self.assertEqual(manager.updates, [])

    def test_no_users_is_fine(self):
        self.assertEqual(run_backfill({}).updates, [])

    def test_each_update_only_touches_a_row_that_is_still_null(self):
        manager = run_backfill({1: None})
        self.assertEqual(manager.updates, [{"pk": 1, "public_id__isnull": True}])
