"""DB-free tests: the models and migrations added by the review-services slice.

They read the migration files and the model definitions; no database is touched. The constraints are
exercised against PostgreSQL in tests/test_review_postgres.py.
"""

from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase

from apps.audit.models import AuditEvent
from apps.cases.models import ImmutableRecord, Rejection, ResultRevision
from apps.intake.models import RequestSource, RevisionEdit


def loader():
    return MigrationLoader(None, ignore_no_migrations=True)


class MigrationTests(SimpleTestCase):
    def test_nothing_is_missing_a_migration(self):
        graph_loader = loader()
        changes = MigrationAutodetector(graph_loader.project_state(), ProjectState.from_apps(apps)).changes(graph=graph_loader.graph)
        self.assertEqual(changes, {})

    def test_the_new_migrations_only_create_models(self):
        for key, expected in {
            ("audit", "0001_initial"): {"CreateModel"},
            ("cases", "0005_rejection"): {"CreateModel"},
            ("intake", "0002_revisionedit_request_permissions"): {"CreateModel", "AlterModelOptions"},
        }.items():
            with self.subTest(migration=key):
                self.assertEqual({type(op).__name__ for op in loader().disk_migrations[key].operations}, expected)

    def test_no_existing_table_is_altered_by_this_slice_except_the_user_column(self):
        for app, names in {"cases": {"0001_initial", "0002_workflowrun_state", "0003_backfill_run_state", "0004_workflowrun_state_constraints", "0005_rejection"}, "intake": {"0001_initial", "0002_revisionedit_request_permissions"}}.items():
            self.assertEqual({n for a, n in loader().disk_migrations if a == app}, names)

    def test_intake_0002_follows_the_orchestration_migration(self):
        migration = loader().disk_migrations[("intake", "0002_revisionedit_request_permissions")]
        self.assertIn(("intake", "0001_initial"), migration.dependencies)
        self.assertIn(("cases", "0005_rejection"), migration.dependencies)

    def test_the_audit_migration_has_no_dependency_so_it_cannot_block_or_be_blocked(self):
        self.assertEqual(loader().disk_migrations[("audit", "0001_initial")].dependencies, [])

    def test_the_whole_graph_has_a_single_leaf_per_app(self):
        leaves = {}
        for app, name in loader().graph.leaf_nodes():
            leaves.setdefault(app, []).append(name)
        for app in ("accounts", "audit", "cases", "intake", "tasks"):
            self.assertEqual(len(leaves[app]), 1, app)


class RejectionModelTests(SimpleTestCase):
    def test_it_is_immutable_like_approvals(self):
        self.assertTrue(issubclass(Rejection, ImmutableRecord))

    def test_it_is_bound_to_a_revision_by_a_protected_foreign_key_and_to_its_hash(self):
        field = Rejection._meta.get_field("revision")
        self.assertIs(field.related_model, ResultRevision)
        self.assertEqual(field.remote_field.on_delete.__name__, "PROTECT")
        self.assertEqual(Rejection._meta.get_field("content_hash").max_length, 64)

    def test_the_rejecting_user_is_a_plain_uuid_not_a_foreign_key(self):
        field = Rejection._meta.get_field("rejected_by")
        self.assertEqual(field.get_internal_type(), "UUIDField")
        self.assertFalse(field.is_relation)

    def test_there_is_no_free_text_column(self):
        kinds = {f.name: f.get_internal_type() for f in Rejection._meta.get_fields() if not f.is_relation}
        self.assertNotIn("TextField", kinds.values())
        self.assertEqual(Rejection._meta.get_field("reason_code").max_length, 40)

    def test_the_constraints(self):
        self.assertEqual({c.name for c in Rejection._meta.constraints}, {"cases_rejection_hash_is_sha256_hex", "cases_rejection_reason_code_shape"})


class RevisionEditModelTests(SimpleTestCase):
    def test_it_is_immutable_and_one_per_revision(self):
        self.assertTrue(issubclass(RevisionEdit, ImmutableRecord))
        self.assertTrue(RevisionEdit._meta.get_field("revision").one_to_one)

    def test_base_revision_is_optional_for_the_first_edit_of_an_invalid_outcome(self):
        self.assertTrue(RevisionEdit._meta.get_field("base_revision").null)

    def test_the_author_is_a_protected_user_foreign_key(self):
        field = RevisionEdit._meta.get_field("edited_by")
        self.assertEqual(field.related_model._meta.label, "accounts.User")
        self.assertEqual(field.remote_field.on_delete.__name__, "PROTECT")

    def test_a_revision_cannot_be_its_own_base(self):
        self.assertEqual({c.name for c in RevisionEdit._meta.constraints}, {"intake_edit_base_is_another_revision"})


class PermissionHolderTests(SimpleTestCase):
    def test_the_holder_is_a_real_managed_model_so_dumpdata_and_fixtures_keep_working(self):
        # An unmanaged model with no table made `manage.py dumpdata` fail ("no such table"); this is the regression guard.
        self.assertTrue(RequestSource._meta.managed)
        self.assertFalse([m for m in apps.get_models() if not m._meta.managed and not m._meta.proxy and not m._meta.auto_created])

    def test_the_default_add_change_delete_view_permissions_are_switched_off_on_the_immutable_model(self):
        self.assertEqual(tuple(RequestSource._meta.default_permissions), ())

    def test_the_six_request_permissions(self):
        self.assertEqual(
            {codename for codename, _ in RequestSource._meta.permissions},
            {"submit_request", "view_own_request", "edit_own_request", "view_any_request", "approve_request", "reject_request"},
        )

    def test_no_model_other_than_the_audit_one_is_added_to_the_audit_app(self):
        self.assertEqual({m.__name__ for m in apps.get_app_config("audit").get_models()}, {AuditEvent.__name__})
