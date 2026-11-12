"""DB-free tests for the revision/approval persistence layer (``apps.cases``).

Nothing here opens a database connection (``SimpleTestCase`` refuses queries). They cover the
model metadata and migration, the immutability guards that act before any SQL is issued, the
pure write rules, the row-to-domain adapters, and the persisted export-eligibility adapter with
its reads replaced by in-memory rows. The behavior that needs a real PostgreSQL (constraints,
transactions, locking) is in test_cases_persistence_postgres.py and is NOT run here.
This file is ASCII only.
"""

import ast
import hashlib
import uuid
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase

import workflows.revision_approval as policy
from apps.cases import adapters, services
from apps.cases.errors import (
    BindingMismatchError,
    ImmutableRecordError,
    NotCurrentRevisionError,
    RecordNotFoundError,
    RevisionNumberError,
    StoredDataError,
)
from apps.cases.models import Approval, Case, ResultRevision, WorkflowRun
from tests.test_request_intake_boundaries import imported_modules
from workflows.revision_approval import (
    ApprovalRecord,
    ExportDenialReason,
    InvalidIdentifierError,
    RevisionFinding,
    approve_revision,
    check_export_eligibility,
)
from workflows.revision_approval import ResultRevision as DomainRevision

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "apps" / "cases"
POLICY_DIR = Path(policy.__file__).parent


def ident(number):
    return uuid.UUID(int=number)


CASE, CASE_2 = ident(0xC1), ident(0xC2)
RUN, RUN_2 = ident(0xD1), ident(0xD2)
REV_A, REV_B = ident(0xA1), ident(0xA2)
USER = ident(0xE1)
WARNING = RevisionFinding("duplicate_item", "items[1].description", False)


def domain_revision(*, case=CASE, run=RUN, revision_id=REV_A, number=1, value=1, findings=()):
    payload = b'{"canonical_format_version":1,"payload":{"x":%d}}' % value
    return DomainRevision(
        case_id=case,
        run_id=run,
        revision_id=revision_id,
        revision_number=number,
        canonical_format_version=1,
        canonical_bytes=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
        findings=findings,
    )


def run_row(*, case=CASE, run=RUN, current=None):
    return WorkflowRun(id=run, case_id=case, current_revision_id=current)


def revision_row(revision, run):
    """A stored-style row for ``revision`` (in memory only; nothing is saved)."""
    row = ResultRevision(
        id=revision.revision_id,
        run=run,
        revision_number=revision.revision_number,
        canonical_format_version=revision.canonical_format_version,
        canonical_bytes=memoryview(revision.canonical_bytes),  # what PostgreSQL drivers return
        content_hash=revision.content_hash,
        findings=adapters.findings_to_json(revision.findings),
    )
    row._state.adding = False
    return row


def approval_row(record, revision_row_, approval_id=None):
    row = Approval(
        id=approval_id or uuid.uuid4(),
        revision=revision_row_,
        content_hash=record.content_hash,
        approver_id=record.approver_id,
        acknowledged_warnings=[list(pair) for pair in record.acknowledged_warnings],
    )
    row._state.adding = False
    return row


class ModelMetadataTests(SimpleTestCase):
    def test_the_app_is_registered_with_a_stable_label(self):
        config = apps.get_app_config("cases")
        self.assertEqual((config.name, config.label), ("apps.cases", "cases"))

    def test_primary_keys_are_uuids(self):
        for model in (Case, WorkflowRun, ResultRevision, Approval):
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model._meta.pk, models.UUIDField)

    def test_revision_and_approval_ids_are_supplied_not_defaulted_for_revisions(self):
        self.assertIs(ResultRevision._meta.pk.default, models.NOT_PROVIDED)  # the domain supplies the id

    def test_foreign_keys_protect_history(self):
        for model, name in ((WorkflowRun, "case"), (WorkflowRun, "current_revision"), (ResultRevision, "run"), (Approval, "revision")):
            with self.subTest(field=f"{model.__name__}.{name}"):
                self.assertIs(model._meta.get_field(name).remote_field.on_delete, models.PROTECT)

    def test_a_revision_is_unique_per_run_and_number_in_the_database(self):
        uniques = [c for c in ResultRevision._meta.constraints if isinstance(c, models.UniqueConstraint)]
        self.assertEqual([(c.name, tuple(c.fields)) for c in uniques], [("cases_revision_number_unique_per_run", ("run", "revision_number"))])

    def test_check_constraints_exist_for_numbers_and_hashes(self):
        names = {c.name for model in (ResultRevision, Approval) for c in model._meta.constraints if isinstance(c, models.CheckConstraint)}
        self.assertEqual(
            names,
            {
                "cases_revision_number_at_least_1",
                "cases_revision_format_version_at_least_1",
                "cases_revision_hash_is_sha256_hex",
                "cases_approval_hash_is_sha256_hex",
            },
        )

    def test_the_approver_is_data_not_a_user_link_and_there_is_no_human_approved_flag(self):
        field = Approval._meta.get_field("approver_id")
        self.assertIsInstance(field, models.UUIDField)
        self.assertFalse(field.is_relation)
        for model in (Approval, ResultRevision, WorkflowRun, Case):
            self.assertNotIn("human_approved", {f.name for f in model._meta.get_fields()})

    def test_no_second_hash_is_computed_in_the_models(self):
        for model in (ResultRevision, Approval):
            self.assertIsInstance(model._meta.get_field("content_hash"), models.CharField)
        self.assertEqual({f.name for f in ResultRevision._meta.local_fields}, {"id", "run", "revision_number", "canonical_format_version", "canonical_bytes", "content_hash", "findings", "created_at"})
        self.assertEqual({f.name for f in Approval._meta.local_fields}, {"id", "revision", "content_hash", "approver_id", "acknowledged_warnings", "created_at"})

    def test_only_revisions_and_approvals_are_immutable_records(self):
        self.assertTrue(issubclass(ResultRevision, models.Model) and ResultRevision.__mro__[1].__name__ == "ImmutableRecord")
        self.assertEqual(Approval.__mro__[1].__name__, "ImmutableRecord")
        self.assertNotEqual(WorkflowRun.__mro__[1].__name__, "ImmutableRecord")  # the run pointer must be updatable by the service
        self.assertNotEqual(Case.__mro__[1].__name__, "ImmutableRecord")


class MigrationTests(SimpleTestCase):
    def test_models_match_migrations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph)
        self.assertEqual(changes, {}, f"Missing migrations for: {sorted(changes)}")

    def test_the_initial_migration_exists_and_uses_only_schema_operations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("cases", "0001_initial")]
        self.assertTrue(migration.initial)
        allowed = {"CreateModel", "AddField", "AddConstraint"}
        self.assertEqual({type(op).__name__ for op in migration.operations} - allowed, set())  # no RunSQL or RunPython

    def test_the_migration_does_not_touch_other_apps(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("cases", "0001_initial")]
        self.assertEqual([dep for dep in migration.dependencies if dep[0] != "cases"], [])


class ImmutabilityGuardTests(SimpleTestCase):
    """The guards raise before any SQL; a database is never needed (and would be refused)."""

    def setUp(self):
        self.revision = domain_revision()
        self.row = revision_row(self.revision, run_row())

    def test_saving_an_existing_row_is_refused(self):
        with self.assertRaises(ImmutableRecordError):
            self.row.save()

    def test_an_update_is_refused_even_for_a_new_instance(self):
        fresh = ResultRevision(id=REV_B, run=run_row(), revision_number=2, canonical_format_version=1, canonical_bytes=b"x", content_hash="0" * 64)
        for kwargs in ({"update_fields": ["content_hash"]}, {"force_update": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ImmutableRecordError):
                fresh.save(**kwargs)

    def test_a_new_row_is_always_inserted_never_overwritten(self):
        fresh = ResultRevision(id=REV_B, run=run_row(), revision_number=2, canonical_format_version=1, canonical_bytes=b"x", content_hash="0" * 64)
        with mock.patch.object(models.Model, "save") as parent_save:
            fresh.save(force_insert=False)
        self.assertTrue(parent_save.call_args.kwargs["force_insert"])

    def test_deleting_an_instance_is_refused(self):
        with self.assertRaises(ImmutableRecordError):
            self.row.delete()
        approval = approval_row(approve_revision(self.revision, approver_id=USER), self.row)
        with self.assertRaises(ImmutableRecordError):
            approval.delete()

    def test_bulk_modification_through_the_manager_is_refused_before_any_query(self):
        for model in (ResultRevision, Approval):
            queryset = model.objects.all()
            with self.subTest(model=model.__name__):
                with self.assertRaises(ImmutableRecordError):
                    model.objects.update(content_hash="0" * 64)
                with self.assertRaises(ImmutableRecordError):
                    queryset.filter(pk=ident(1)).update(content_hash="0" * 64)
                with self.assertRaises(ImmutableRecordError):
                    queryset.delete()
                with self.assertRaises(ImmutableRecordError):
                    model.objects.filter(pk=ident(1)).delete()
                with self.assertRaises(ImmutableRecordError):
                    model.objects.bulk_update([], ["content_hash"])
                with self.assertRaises(ImmutableRecordError):
                    model.objects.bulk_create([], update_conflicts=True, update_fields=["content_hash"], unique_fields=["id"])

    def test_save_base_is_guarded_too_including_the_raw_loaddata_path(self):
        for kwargs in ({}, {"raw": True}, {"force_update": True}, {"update_fields": ["content_hash"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ImmutableRecordError):
                self.row.save_base(**kwargs)
        fresh = ResultRevision(id=REV_B, run=run_row(), revision_number=2, canonical_format_version=1, canonical_bytes=b"x", content_hash="0" * 64)
        with mock.patch.object(models.Model, "save_base") as parent_save_base:
            fresh.save_base(raw=True)  # what loaddata does for a new object
        self.assertTrue(parent_save_base.call_args.kwargs["force_insert"])
        with self.assertRaises(ImmutableRecordError):
            fresh.save_base(force_update=True)

    def test_related_managers_are_guarded_like_the_default_manager(self):
        run = run_row()
        with self.assertRaises(ImmutableRecordError):
            run.revisions.update(content_hash="0" * 64)
        with self.assertRaises(ImmutableRecordError):
            run.revisions.all().delete()
        revision = domain_revision()
        approval_manager = revision_row(revision, run).approvals
        with self.assertRaises(ImmutableRecordError):
            approval_manager.update(approver_id=USER)

    def test_the_models_document_the_orm_level_boundary_and_that_hash_checks_are_not_tamper_protection(self):
        doc = apps.get_app_config("cases").models_module.__doc__
        self.assertIn("NO database-level immutability", doc)
        self.assertIn("raw SQL", doc)
        self.assertIn("_base_manager", doc)
        self.assertIn("NOT tamper protection", doc)
        self.assertIn("unverified domain actor identifier", Approval.__doc__)
        self.assertIn("before any API exposes approval operations", " ".join(Approval.__doc__.split()))


class PureWriteRuleTests(SimpleTestCase):
    def test_a_first_revision_must_be_number_1(self):
        services.check_revision_write(run_case_id=CASE, revision=domain_revision(number=1), highest_number=None, current_number=None)
        with self.assertRaises(RevisionNumberError) as caught:
            services.check_revision_write(run_case_id=CASE, revision=domain_revision(number=2), highest_number=None, current_number=None)
        self.assertEqual(caught.exception.expected, 1)

    def test_the_next_number_is_the_highest_plus_one(self):
        services.check_revision_write(run_case_id=CASE, revision=domain_revision(number=3), highest_number=2, current_number=2)
        for number in (1, 2, 4, 10):
            with self.subTest(number=number), self.assertRaises(RevisionNumberError) as caught:
                services.check_revision_write(run_case_id=CASE, revision=domain_revision(number=number), highest_number=2, current_number=2)
            self.assertEqual(caught.exception.expected, 3)

    def test_the_same_number_is_allowed_in_a_different_run_because_numbers_are_per_run(self):
        # Each run's own highest number is what counts; another run's revisions never enter the rule.
        services.check_revision_write(run_case_id=CASE, revision=domain_revision(run=RUN_2, number=1), highest_number=None, current_number=None)

    def test_the_run_must_belong_to_the_revisions_case(self):
        with self.assertRaises(BindingMismatchError):
            services.check_revision_write(run_case_id=CASE_2, revision=domain_revision(), highest_number=None, current_number=None)

    def test_a_pointer_that_is_not_the_highest_revision_is_stored_data_corruption(self):
        for highest, current in ((2, 1), (2, None), (None, 1)):
            with self.subTest(highest=highest, current=current), self.assertRaises(StoredDataError):
                services.check_revision_write(run_case_id=CASE, revision=domain_revision(number=3), highest_number=highest, current_number=current)

    def approval_args(self, **changes):
        revision = domain_revision()
        base = {
            "run_case_id": CASE,
            "run_id": RUN,
            "current_revision_id": REV_A,
            "revision_row_run_id": RUN,
            "stored_hash": revision.content_hash,
            "approval": approve_revision(revision, approver_id=USER),
        }
        base.update(changes)
        return base

    def test_a_correctly_bound_approval_passes(self):
        services.check_approval_write(**self.approval_args())

    def test_each_binding_is_checked(self):
        for changes in ({"run_case_id": CASE_2}, {"run_id": RUN_2}, {"revision_row_run_id": RUN_2}, {"stored_hash": "0" * 64}):
            with self.subTest(changes=list(changes)), self.assertRaises(BindingMismatchError):
                services.check_approval_write(**self.approval_args(**changes))

    def test_an_approval_needs_the_current_revision(self):
        for current in (REV_B, None):
            with self.subTest(current=current), self.assertRaises(NotCurrentRevisionError):
                services.check_approval_write(**self.approval_args(current_revision_id=current))


class AdapterTests(SimpleTestCase):
    def test_a_revision_round_trips_through_a_row(self):
        for findings in ((), (WARNING,), (WARNING, RevisionFinding("x", "y", False, message="m", excerpt="e"))):
            original = domain_revision(findings=findings)
            row = revision_row(original, run_row())
            with self.subTest(findings=len(findings)):
                self.assertEqual(adapters.to_domain_revision(row), original)

    def test_binary_values_returned_as_memoryview_become_bytes(self):
        row = revision_row(domain_revision(), run_row())
        self.assertIsInstance(row.canonical_bytes, memoryview)
        self.assertIsInstance(adapters.to_domain_revision(row).canonical_bytes, bytes)

    def test_the_domain_re_verifies_the_hash_on_load(self):
        row = revision_row(domain_revision(), run_row())
        row.content_hash = "0" * 64
        with self.assertRaises(StoredDataError) as caught:
            adapters.to_domain_revision(row)
        self.assertIn("hash_mismatch", caught.exception.problems)

    def test_other_corruption_is_reported_with_the_domain_problem_codes(self):
        cases = {
            "format_version_mismatch": {"canonical_format_version": 2},
            "invalid_revision_number": {"revision_number": 0},
            "invalid_bytes": {"canonical_bytes": b"not json"},
        }
        for code, changes in cases.items():
            row = revision_row(domain_revision(), run_row())
            for name, value in changes.items():
                setattr(row, name, value)
            with self.subTest(code=code), self.assertRaises(StoredDataError) as caught:
                adapters.to_domain_revision(row)
            self.assertIn(code, caught.exception.problems)

    def test_blocking_findings_in_storage_are_refused_by_the_domain(self):
        row = revision_row(domain_revision(), run_row())
        row.findings = [{"code": "c", "field_path": "p", "blocking": True, "message": "", "excerpt": None}]
        with self.assertRaises(StoredDataError) as caught:
            adapters.to_domain_revision(row)
        self.assertIn("blocking_findings", caught.exception.problems)

    def test_findings_json_is_strict(self):
        good = {"code": "c", "field_path": "p", "blocking": False, "message": "", "excerpt": None}
        self.assertEqual(adapters.findings_from_json([good]), (RevisionFinding("c", "p", False),))
        for bad in (None, "x", {}, [1], [{}], [{**good, "extra": 1}], [{k: v for k, v in good.items() if k != "code"}], [{**good, "blocking": 1}], [{**good, "code": ""}]):
            with self.subTest(bad=repr(bad)[:40]), self.assertRaises(StoredDataError):
                adapters.findings_from_json(bad)

    def test_an_approval_round_trips_and_takes_case_and_run_from_the_revision(self):
        revision = domain_revision(findings=(WARNING,))
        record = approve_revision(revision, approver_id=USER, warnings_reviewed=True)
        row = approval_row(record, revision_row(revision, run_row()))
        self.assertEqual(adapters.to_domain_approval(row), record)
        stored = adapters.to_stored_approval(row)
        self.assertEqual((stored.record, stored.approval_id), (record, row.pk))

    def test_a_malformed_stored_approval_is_reported(self):
        revision = domain_revision()
        row = approval_row(approve_revision(revision, approver_id=USER), revision_row(revision, run_row()))
        for field, value in (("content_hash", "nope"), ("acknowledged_warnings", "x"), ("acknowledged_warnings", [["only-one"]]), ("approver_id", "not-a-uuid")):
            broken = approval_row(approve_revision(revision, approver_id=USER), revision_row(revision, run_row()))
            setattr(broken, field, value)
            with self.subTest(field=field, value=repr(value)), self.assertRaises(StoredDataError):
                adapters.to_domain_approval(broken)
        self.assertTrue(adapters.to_domain_approval(row))


class PersistedEligibilityTests(SimpleTestCase):
    """``check_persisted_export_eligibility`` with its three reads served from memory."""

    def setUp(self):
        self.runs = {}
        self.revisions = {}
        self.approvals = {}
        patches = [
            mock.patch.object(services, "_fetch_run_row", side_effect=self.fetch(self.runs)),
            mock.patch.object(services, "_fetch_revision_row", side_effect=self.fetch(self.revisions)),
            mock.patch.object(services, "_fetch_approval_row", side_effect=self.fetch(self.approvals)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def fetch(table):
        def lookup(key):
            if key not in table:
                raise RecordNotFoundError("missing")
            return table[key]

        return lookup

    def store(self, revision, *, current=True):
        run = self.runs.get(revision.run_id) or run_row(case=revision.case_id, run=revision.run_id)
        self.runs[run.pk] = run
        row = revision_row(revision, run)
        self.revisions[revision.revision_id] = row
        if current:
            run.current_revision_id = revision.revision_id
        return row

    def approve(self, revision, row, *, reviewed=True):
        record = approve_revision(revision, approver_id=USER, warnings_reviewed=reviewed)
        stored = approval_row(record, row)
        self.approvals[stored.pk] = stored
        return stored.pk

    def decide(self, revision, approval_id=None, case=CASE, run=RUN):
        return services.check_persisted_export_eligibility(expected_case_id=case, expected_run_id=run, requested_revision_id=revision.revision_id, approval_id=approval_id)

    def test_an_approved_current_revision_is_allowed(self):
        revision = domain_revision()
        approval_id = self.approve(revision, self.store(revision))
        decision = self.decide(revision, approval_id)
        self.assertTrue(decision.allowed)
        self.assertEqual((decision.case_id, decision.run_id, decision.revision_id), (CASE, RUN, REV_A))

    def test_no_approval_is_denied_with_the_policy_code(self):
        revision = domain_revision()
        self.store(revision)
        self.assertEqual(self.decide(revision).reasons, (ExportDenialReason.NO_APPROVAL,))

    def test_an_approval_from_another_run_is_denied_even_with_equal_case_payload_and_hash(self):
        other_run_revision = domain_revision(run=RUN_2, revision_id=REV_A)
        other_row = self.store(other_run_revision)
        foreign_id = self.approve(other_run_revision, other_row)
        mine = domain_revision(run=RUN, revision_id=ident(0xA5))  # same case, payload, hash; a different run
        self.assertEqual(mine.content_hash, other_run_revision.content_hash)
        self.store(mine)
        decision = self.decide(mine, foreign_id)
        self.assertTrue(decision.denied)
        self.assertEqual(
            decision.reasons,
            (ExportDenialReason.APPROVAL_RUN_MISMATCH, ExportDenialReason.APPROVAL_REVISION_MISMATCH),
        )

    def test_a_historical_approval_does_not_authorize_a_newer_revision(self):
        first = domain_revision()
        first_row = self.store(first)
        old_approval = self.approve(first, first_row)
        second = domain_revision(revision_id=REV_B, number=2, value=2)
        self.store(second)  # becomes the run's current revision
        self.assertEqual(self.decide(first, old_approval).reasons, (ExportDenialReason.NOT_CURRENT_REVISION,))
        self.assertEqual(
            self.decide(second, old_approval).reasons,
            (ExportDenialReason.APPROVAL_REVISION_MISMATCH, ExportDenialReason.APPROVAL_HASH_MISMATCH),
        )

    def test_the_wrong_expected_case_and_run_are_denied_in_policy_order(self):
        revision = domain_revision()
        approval_id = self.approve(revision, self.store(revision))
        self.runs[RUN_2] = run_row(run=RUN_2, current=None)
        other = domain_revision(run=RUN_2, revision_id=ident(0xA7), number=1, value=9)
        self.store(other)
        decision = self.decide(revision, approval_id, case=CASE_2, run=RUN_2)
        self.assertEqual(
            decision.reasons,
            (ExportDenialReason.CASE_MISMATCH, ExportDenialReason.RUN_MISMATCH, ExportDenialReason.NOT_CURRENT_REVISION),
        )

    def test_the_decision_is_exactly_the_pure_policy_decision(self):
        first = domain_revision()
        row = self.store(first)
        approval_id = self.approve(first, row)
        second = domain_revision(revision_id=REV_B, number=2, value=2, findings=(WARNING,))
        self.store(second)
        expected = check_export_eligibility(
            expected_case_id=CASE,
            expected_run_id=RUN,
            requested_revision=second,
            current_revision=second,
            approval=adapters.to_domain_approval(self.approvals[approval_id]),
        )
        self.assertEqual(self.decide(second, approval_id), expected)

    def test_a_stored_approval_whose_hash_differs_from_the_revision_is_denied_by_the_policy(self):
        revision = domain_revision()
        approval_id = self.approve(revision, self.store(revision))
        self.approvals[approval_id].content_hash = "1" * 64
        decision = self.decide(revision, approval_id)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_HASH_MISMATCH,))

    def test_a_run_without_a_current_revision_is_a_lookup_error_not_a_decision(self):
        revision = domain_revision()
        approval_id = self.approve(revision, self.store(revision))
        self.runs[RUN_2] = run_row(run=RUN_2)
        with self.assertRaises(RecordNotFoundError):
            self.decide(revision, approval_id, case=CASE, run=RUN_2)
        with self.assertRaises(RecordNotFoundError):  # an unknown run
            self.decide(revision, approval_id, case=CASE, run=ident(0x99))

    def test_all_reasons_arrive_in_the_fixed_policy_order(self):
        order = list(ExportDenialReason)
        requested = domain_revision(findings=(WARNING,))
        self.store(requested)
        other = domain_revision(case=CASE_2, run=RUN_2, revision_id=ident(0xA7), value=9)
        other_row = self.store(other)
        foreign = ApprovalRecord(case_id=CASE_2, run_id=RUN_2, revision_id=ident(0xA8), content_hash="1" * 64, approver_id=USER)
        stored = approval_row(foreign, other_row)
        self.approvals[stored.pk] = stored
        decision = self.decide(requested, stored.pk, case=CASE_2, run=RUN_2)
        self.assertEqual(
            decision.reasons,
            (
                ExportDenialReason.CASE_MISMATCH,
                ExportDenialReason.RUN_MISMATCH,
                ExportDenialReason.NOT_CURRENT_REVISION,
                ExportDenialReason.APPROVAL_CASE_MISMATCH,
                ExportDenialReason.APPROVAL_RUN_MISMATCH,
                ExportDenialReason.APPROVAL_REVISION_MISMATCH,
                ExportDenialReason.APPROVAL_HASH_MISMATCH,
                ExportDenialReason.WARNINGS_NOT_ACKNOWLEDGED,
            ),
        )
        self.assertEqual(list(decision.reasons), sorted(decision.reasons, key=order.index))

    def test_a_missing_revision_or_approval_is_a_lookup_error(self):
        revision = domain_revision()
        self.store(revision)
        with self.assertRaises(RecordNotFoundError):
            self.decide(domain_revision(revision_id=ident(0xFF)))
        with self.assertRaises(RecordNotFoundError):
            self.decide(revision, uuid.uuid4())

    def test_a_pointer_to_another_runs_revision_is_corruption(self):
        revision = domain_revision()
        self.store(revision)
        foreign = domain_revision(run=RUN_2, revision_id=ident(0xB1))
        self.store(foreign, current=False)
        self.runs[RUN].current_revision_id = foreign.revision_id
        with self.assertRaises(StoredDataError):
            self.decide(revision)

    def test_identifiers_must_be_uuids_not_strings(self):
        revision = domain_revision()
        self.store(revision)
        for kwargs in (
            {"expected_case_id": str(CASE)},
            {"expected_run_id": str(RUN)},
            {"requested_revision_id": str(REV_A)},
            {"approval_id": str(ident(5))},
        ):
            base = {"expected_case_id": CASE, "expected_run_id": RUN, "requested_revision_id": REV_A, "approval_id": None}
            base.update(kwargs)
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(InvalidIdentifierError):
                services.check_persisted_export_eligibility(**base)

    def test_reads_validate_uuid_arguments(self):
        for function in (services.load_revision, services.get_current_revision, services.list_revisions, services.load_approval, services.list_approvals):
            with self.subTest(function=function.__name__), self.assertRaises(InvalidIdentifierError):
                function(str(REV_A))

    def test_get_current_revision_is_none_for_a_run_without_revisions(self):
        self.runs[RUN] = run_row()
        self.assertIsNone(services.get_current_revision(RUN))


class WriteValidationTests(SimpleTestCase):
    def test_writes_reject_the_wrong_types_before_touching_the_database(self):
        with self.assertRaises(TypeError):
            services.record_revision("not a revision")
        with self.assertRaises(TypeError):
            services.record_approval("not an approval")
        with self.assertRaises(InvalidIdentifierError):
            services.record_approval(approve_revision(domain_revision(), approver_id=USER), approval_id=str(ident(3)))
        with self.assertRaises(InvalidIdentifierError):
            services.create_case(reference="R", case_id=str(CASE))
        with self.assertRaises(InvalidIdentifierError):
            services.create_run(case_id=str(CASE))

    def test_an_inconsistent_revision_is_refused_before_any_write(self):
        broken = domain_revision()
        object.__setattr__(broken, "content_hash", "0" * 64)
        with self.assertRaises(policy.InvalidRevisionError):
            services.record_revision(broken)


class BoundaryTests(SimpleTestCase):
    def test_the_generic_policy_modules_have_no_django_or_app_imports(self):
        for path in sorted(POLICY_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & {"django", "apps", "psycopg", "rest_framework"}, set())

    def test_the_persistence_app_uses_the_generic_policy_only(self):
        for path in sorted(APP_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 0:
                        module = node.module or ""
                        self.assertNotIn("request_intake", module)
                        if module.startswith("workflows"):
                            self.assertEqual(module, "workflows.revision_approval")
                    if isinstance(node, ast.Import):
                        self.assertFalse(any("request_intake" in alias.name for alias in node.names))

    def test_no_second_hashing_or_canonicalization_in_the_persistence_app(self):
        for path in sorted(APP_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & {"hashlib", "hmac"}, set())
                self.assertEqual(used & {"sha256", "sha1", "md5", "dumps", "canonicalize_request", "canonical_decimal_string"}, set())

    def test_no_user_model_or_authentication_is_used(self):
        for path in sorted(APP_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("get_user_model", source)
                self.assertNotIn("AUTH_USER_MODEL", source)
                self.assertNotIn("authenticate", source.replace("authenticates", "").replace("authenticated", ""))

    def test_revision_ids_are_never_generated_by_the_services(self):
        import inspect

        self.assertNotIn("uuid4", inspect.getsource(services.record_revision))
        self.assertNotIn("uuid4", inspect.getsource(services.check_revision_write))
        self.assertNotIn("uuid4", inspect.getsource(adapters))
