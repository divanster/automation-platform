"""Tests for the revision/approval persistence layer that REQUIRE a real PostgreSQL server.

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see
test_postgres_required.py and docs/development/runbook.md). When they run, Django creates and
later destroys a separate ``test_<AP_DB_NAME>`` database. They exercise what the DB-free tests
(test_cases_persistence.py) cannot: constraints, transactions, rollback, and the run-row
locking that serializes competing writers.

STATUS: written on a development VM with no PostgreSQL. They have NOT been run there, so the
guarantees they check (database uniqueness, rollback, locking) are not verified until the owner
runs them against a local test database. This file is ASCII only.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_cases_persistence_postgres --tag requires_postgres
"""

import hashlib
import os
import threading
import unittest
import uuid
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase, TransactionTestCase, tag

from apps.cases import services
from apps.cases.errors import (
    ApprovalMismatchError,
    BindingMismatchError,
    ImmutableRecordError,
    NotCurrentRevisionError,
    RecordNotFoundError,
    RevisionNumberError,
    StoredDataError,
)
from apps.cases.models import Approval, Case, ResultRevision, WorkflowRun
from workflows.revision_approval import (
    ApprovalRecord,
    ExportDenialReason,
    RevisionFinding,
    approve_revision,
    check_export_eligibility,
)
from workflows.revision_approval import ResultRevision as DomainRevision

requires_postgres = unittest.skipUnless(
    os.environ.get("AP_RUN_DB_TESTS") == "1",
    "Requires PostgreSQL: set AP_RUN_DB_TESTS=1 with a dedicated development database.",
)

APPROVER = uuid.UUID(int=0xE1)
APPROVER_2 = uuid.UUID(int=0xE2)
WARNING = RevisionFinding("duplicate_item", "items[1].description", False)


def revision_for(run, number, *, value=None, findings=(), revision_id=None):
    value = number if value is None else value
    payload = b'{"canonical_format_version":1,"payload":{"x":%d}}' % value
    return DomainRevision(
        case_id=run.case_id,
        run_id=run.pk,
        revision_id=revision_id or uuid.uuid4(),
        revision_number=number,
        canonical_format_version=1,
        canonical_bytes=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
        findings=findings,
    )


class DedicatedTestDatabaseMixin:
    """Refuse to run against anything but the Django-created test database.

    Django points the default connection at ``test_<AP_DB_NAME>`` for these tests. The check is
    a second line of defence: if that ever stopped being true, fail before writing any data
    rather than touch a development, shared, or production database.
    """

    @classmethod
    def setUpClass(cls):
        name = connection.settings_dict["NAME"]
        if not str(name).startswith("test_"):
            raise AssertionError("PostgreSQL persistence tests must run against a dedicated test database (test_*).")
        super().setUpClass()


# Bounds for the competing-writer tests (seconds). A wrong locking design must surface as an
# error within these limits, not as a test run that hangs and keeps the test database busy.
BARRIER_TIMEOUT = 30
LOCK_TIMEOUT_SECONDS = 15
JOIN_TIMEOUT = 60


def new_run(reference):
    case = services.create_case(reference=reference)
    return services.create_run(case_id=case.pk)


@tag("requires_postgres")
@requires_postgres
class SchemaTests(DedicatedTestDatabaseMixin, TestCase):
    def test_the_database_has_the_unique_and_check_constraints(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, ResultRevision._meta.db_table)
            approval_constraints = connection.introspection.get_constraints(cursor, Approval._meta.db_table)
        unique = [c["columns"] for c in constraints.values() if c["unique"] and not c["primary_key"]]
        self.assertIn(["run_id", "revision_number"], unique)
        for name in ("cases_revision_number_at_least_1", "cases_revision_format_version_at_least_1", "cases_revision_hash_is_sha256_hex"):
            self.assertIn(name, constraints)
        self.assertIn("cases_approval_hash_is_sha256_hex", approval_constraints)

    def test_the_database_rejects_a_duplicate_revision_number_in_one_run(self):
        run = new_run("dup-db")
        services.record_revision(revision_for(run, 1))
        duplicate = revision_for(run, 1, value=99)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResultRevision.objects.create(
                id=duplicate.revision_id,
                run=run,
                revision_number=1,
                canonical_format_version=1,
                canonical_bytes=duplicate.canonical_bytes,
                content_hash=duplicate.content_hash,
            )
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 1)

    def test_the_database_rejects_invalid_numbers_and_hashes(self):
        run = new_run("checks-db")
        good = revision_for(run, 1)
        for changes in ({"revision_number": 0}, {"canonical_format_version": 0}, {"content_hash": "x" * 64}, {"content_hash": "A" * 64}, {"content_hash": "a" * 63}):
            values = {
                "id": uuid.uuid4(),
                "run": run,
                "revision_number": 1,
                "canonical_format_version": 1,
                "canonical_bytes": good.canonical_bytes,
                "content_hash": good.content_hash,
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ResultRevision.objects.create(**values)

    def test_an_existing_primary_key_is_never_overwritten_by_a_new_instance(self):
        run = new_run("pk-db")
        original = revision_for(run, 1)
        services.record_revision(original)
        clash = ResultRevision(
            id=original.revision_id,
            run=run,
            revision_number=2,
            canonical_format_version=1,
            canonical_bytes=b'{"canonical_format_version":1,"payload":{}}',
            content_hash="a" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            clash.save()
        self.assertEqual(ResultRevision.objects.get(pk=original.revision_id).content_hash, original.content_hash)

    def test_run_and_case_with_history_cannot_be_deleted(self):
        run = new_run("protect-db")
        services.record_revision(revision_for(run, 1))
        with self.assertRaises(ProtectedError), transaction.atomic():
            run.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            run.case.delete()


@tag("requires_postgres")
@requires_postgres
class RevisionPersistenceTests(DedicatedTestDatabaseMixin, TestCase):
    def test_a_revision_is_stored_bound_and_becomes_current(self):
        run = new_run("current-db")
        revision = revision_for(run, 1, findings=(WARNING,))
        row = services.record_revision(revision)
        run.refresh_from_db()
        self.assertEqual(run.current_revision_id, row.pk)
        self.assertEqual((row.run_id, row.run.case_id, row.revision_number, row.content_hash), (run.pk, run.case_id, 1, revision.content_hash))
        loaded = services.load_revision(revision.revision_id)
        self.assertEqual(loaded, revision)  # bytes, hash, ids, case, run, findings all preserved
        self.assertEqual(services.get_current_revision(run.pk), revision)

    def test_a_run_without_revisions_has_no_current_revision(self):
        self.assertIsNone(services.get_current_revision(new_run("empty-db").pk))

    def test_the_current_revision_moves_forward_with_each_revision(self):
        run = new_run("forward-db")
        revisions = [revision_for(run, n) for n in (1, 2, 3)]
        for revision in revisions:
            services.record_revision(revision)
            self.assertEqual(services.get_current_revision(run.pk), revision)
        self.assertEqual(services.list_revisions(run.pk), tuple(revisions))

    def test_the_same_revision_number_is_allowed_in_different_runs(self):
        first, second = new_run("per-run-1"), new_run("per-run-2")
        a, b = revision_for(first, 1), revision_for(second, 1)
        services.record_revision(a)
        services.record_revision(b)
        self.assertEqual(services.get_current_revision(first.pk), a)
        self.assertEqual(services.get_current_revision(second.pk), b)
        self.assertEqual(a.content_hash, b.content_hash)  # equal content, still separate revisions

    def test_a_duplicate_or_skipped_number_is_refused_and_changes_nothing(self):
        run = new_run("refuse-db")
        first = revision_for(run, 1)
        services.record_revision(first)
        for number in (1, 3):
            with self.subTest(number=number), self.assertRaises(RevisionNumberError) as caught:
                services.record_revision(revision_for(run, number, value=50 + number))
            self.assertEqual(caught.exception.expected, 2)
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 1)
        self.assertEqual(services.get_current_revision(run.pk), first)

    def test_the_revision_must_belong_to_the_runs_case(self):
        run = new_run("case-db")
        foreign = DomainRevision(**{**_fields(revision_for(run, 1)), "case_id": uuid.uuid4()})
        with self.assertRaises(BindingMismatchError):
            services.record_revision(foreign)
        self.assertEqual(ResultRevision.objects.count(), 0)

    def test_an_unknown_run_is_refused(self):
        run = new_run("unknown-db")
        orphan = DomainRevision(**{**_fields(revision_for(run, 1)), "run_id": uuid.uuid4()})
        with self.assertRaises(RecordNotFoundError):
            services.record_revision(orphan)

    def test_a_failure_after_the_insert_rolls_the_whole_write_back(self):
        run = new_run("rollback-db")
        with mock.patch.object(WorkflowRun, "save", side_effect=RuntimeError("pointer update failed")):
            with self.assertRaises(RuntimeError):
                services.record_revision(revision_for(run, 1))
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 0)
        run.refresh_from_db()
        self.assertIsNone(run.current_revision_id)

    def test_stored_rows_cannot_be_modified_or_deleted_through_the_orm(self):
        run = new_run("immutable-db")
        revision = revision_for(run, 1)
        row = services.record_revision(revision)
        with self.assertRaises(ImmutableRecordError):
            row.save()
        with self.assertRaises(ImmutableRecordError):
            row.delete()
        with self.assertRaises(ImmutableRecordError):
            ResultRevision.objects.filter(pk=row.pk).update(content_hash="0" * 64)
        with self.assertRaises(ImmutableRecordError):
            ResultRevision.objects.filter(pk=row.pk).delete()
        with self.assertRaises(ImmutableRecordError):
            run.revisions.update(content_hash="0" * 64)  # related managers are guarded too
        with self.assertRaises(ImmutableRecordError):
            ResultRevision.objects.update_or_create(id=row.pk, defaults={"content_hash": "0" * 64})
        with self.assertRaises(ImmutableRecordError):
            row.save_base(force_update=True)
        self.assertEqual(services.load_revision(row.pk), revision)  # the stored row is unchanged

    def test_a_raw_sql_change_is_a_documented_bypass_and_a_hash_only_change_is_detected_on_load(self):
        run = new_run("bypass-db")
        row = services.record_revision(revision_for(run, 1))
        with connection.cursor() as cursor:  # NOT prevented: immutability is not enforced in the database
            cursor.execute("UPDATE cases_resultrevision SET content_hash = %s WHERE id = %s", ["b" * 64, row.pk])
        with self.assertRaises(StoredDataError) as caught:
            services.load_revision(row.pk)
        self.assertIn("hash_mismatch", caught.exception.problems)
        with self.assertRaises(StoredDataError):
            services.get_current_revision(run.pk)

    def test_changing_the_payload_and_the_hash_together_is_not_detected(self):
        # Consistency checking is not tamper protection: whoever can write the table can keep
        # the bytes and the hash consistent with each other, and the domain accepts the result.
        run = new_run("bypass-both-db")
        row = services.record_revision(revision_for(run, 1))
        forged_bytes = b'{"canonical_format_version":1,"payload":{"x":999}}'
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE cases_resultrevision SET canonical_bytes = %s, content_hash = %s WHERE id = %s",
                [forged_bytes, hashlib.sha256(forged_bytes).hexdigest(), row.pk],
            )
        loaded = services.load_revision(row.pk)  # loads without error
        self.assertEqual(loaded.canonical_bytes, forged_bytes)

    def test_a_current_revision_pointer_that_is_not_the_highest_revision_is_detected(self):
        run = new_run("pointer-db")
        services.record_revision(revision_for(run, 1))
        services.record_revision(revision_for(run, 2))
        first = ResultRevision.objects.get(run=run, revision_number=1)
        WorkflowRun.objects.filter(pk=run.pk).update(current_revision=first)  # a bypass of the service
        with self.assertRaises(StoredDataError):
            services.record_revision(revision_for(run, 3))


@tag("requires_postgres")
@requires_postgres
class ApprovalPersistenceTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        self.run = new_run("approval-db")
        self.revision = revision_for(self.run, 1)
        services.record_revision(self.revision)

    def test_an_approval_is_stored_bound_to_case_run_revision_and_hash(self):
        record = approve_revision(self.revision, approver_id=APPROVER)
        row = services.record_approval(record)
        stored = services.load_approval(row.pk)
        self.assertEqual(stored.record, record)
        self.assertEqual((stored.record.case_id, stored.record.run_id, stored.record.revision_id, stored.record.content_hash), (self.run.case_id, self.run.pk, self.revision.revision_id, self.revision.content_hash))
        self.assertEqual(services.list_approvals(self.revision.revision_id), (stored,))

    def test_the_approver_is_stored_as_data_and_nothing_authenticates_it(self):
        record = approve_revision(self.revision, approver_id=uuid.uuid4())  # any UUID at all
        row = services.record_approval(record)
        self.assertEqual(Approval.objects.get(pk=row.pk).approver_id, record.approver_id)

    def test_warnings_acknowledgements_are_stored_and_enforced(self):
        revision = revision_for(self.run, 2, findings=(WARNING,))
        services.record_revision(revision)
        bare = ApprovalRecord(case_id=revision.case_id, run_id=revision.run_id, revision_id=revision.revision_id, content_hash=revision.content_hash, approver_id=APPROVER)
        with self.assertRaises(ApprovalMismatchError):
            services.record_approval(bare)
        proper = approve_revision(revision, approver_id=APPROVER, warnings_reviewed=True)
        row = services.record_approval(proper)
        self.assertEqual(Approval.objects.get(pk=row.pk).acknowledged_warnings, [["duplicate_item", "items[1].description"]])

    def test_an_approval_is_refused_for_a_revision_that_is_not_current(self):
        old_record = approve_revision(self.revision, approver_id=APPROVER)
        services.record_revision(revision_for(self.run, 2))
        with self.assertRaises(NotCurrentRevisionError):
            services.record_approval(old_record)
        self.assertEqual(Approval.objects.count(), 0)

    def test_historical_approvals_survive_new_revisions_unchanged(self):
        first = services.record_approval(approve_revision(self.revision, approver_id=APPROVER))
        before = services.load_approval(first.pk)
        second = revision_for(self.run, 2)
        services.record_revision(second)
        services.record_approval(approve_revision(second, approver_id=APPROVER_2))
        self.assertEqual(services.load_approval(first.pk), before)
        self.assertEqual(Approval.objects.count(), 2)

    def test_binding_mismatches_are_refused(self):
        record = approve_revision(self.revision, approver_id=APPROVER)
        other_run = new_run("approval-other")
        other_revision = revision_for(other_run, 1)
        services.record_revision(other_revision)
        for changes in ({"case_id": uuid.uuid4()}, {"run_id": other_run.pk}, {"content_hash": "0" * 64}):
            forged = ApprovalRecord(**{**_approval_fields(record), **changes})
            with self.subTest(changes=list(changes)), self.assertRaises((BindingMismatchError, RecordNotFoundError)):
                services.record_approval(forged)
        self.assertEqual(Approval.objects.count(), 0)

    def test_an_unknown_revision_is_refused(self):
        record = approve_revision(self.revision, approver_id=APPROVER)
        forged = ApprovalRecord(**{**_approval_fields(record), "revision_id": uuid.uuid4()})
        with self.assertRaises(RecordNotFoundError):
            services.record_approval(forged)

    def test_approval_rows_are_immutable_through_the_orm(self):
        row = services.record_approval(approve_revision(self.revision, approver_id=APPROVER))
        with self.assertRaises(ImmutableRecordError):
            row.save()
        with self.assertRaises(ImmutableRecordError):
            row.delete()
        with self.assertRaises(ImmutableRecordError):
            Approval.objects.filter(pk=row.pk).update(approver_id=APPROVER_2)
        with self.assertRaises(ImmutableRecordError):  # the approved revision cannot be deleted either
            ResultRevision.objects.get(pk=self.revision.revision_id).delete()

    def test_the_database_rejects_a_malformed_stored_hash(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Approval.objects.create(revision=ResultRevision.objects.get(pk=self.revision.revision_id), content_hash="nope", approver_id=APPROVER)


@tag("requires_postgres")
@requires_postgres
class PersistedEligibilityTests(DedicatedTestDatabaseMixin, TestCase):
    def test_the_persisted_decision_is_the_pure_policy_decision(self):
        run = new_run("elig-db")
        first = revision_for(run, 1)
        services.record_revision(first)
        old_approval = services.record_approval(approve_revision(first, approver_id=APPROVER))
        self.assertTrue(
            services.check_persisted_export_eligibility(
                expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=first.revision_id, approval_id=old_approval.pk
            ).allowed
        )
        second = revision_for(run, 2)
        services.record_revision(second)
        decision = services.check_persisted_export_eligibility(
            expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=second.revision_id, approval_id=old_approval.pk
        )
        expected = check_export_eligibility(
            expected_case_id=run.case_id,
            expected_run_id=run.pk,
            requested_revision=second,
            current_revision=second,
            approval=services.load_approval(old_approval.pk).record,
        )
        self.assertEqual(decision, expected)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_REVISION_MISMATCH, ExportDenialReason.APPROVAL_HASH_MISMATCH))

    def test_an_approval_from_another_run_with_equal_case_payload_and_hash_is_denied(self):
        case = services.create_case(reference="cross-run-db")
        run_one = services.create_run(case_id=case.pk)
        run_two = services.create_run(case_id=case.pk)  # the same case, two runs
        one = revision_for(run_one, 1)
        two = revision_for(run_two, 1)
        self.assertEqual(one.content_hash, two.content_hash)
        services.record_revision(one)
        services.record_revision(two)
        foreign = services.record_approval(approve_revision(one, approver_id=APPROVER))
        decision = services.check_persisted_export_eligibility(
            expected_case_id=case.pk, expected_run_id=run_two.pk, requested_revision_id=two.revision_id, approval_id=foreign.pk
        )
        self.assertTrue(decision.denied)
        self.assertIn(ExportDenialReason.APPROVAL_RUN_MISMATCH, decision.reasons)
        order = list(ExportDenialReason)
        self.assertEqual(list(decision.reasons), sorted(decision.reasons, key=order.index))

    def test_a_missing_approval_and_an_unknown_run_follow_the_documented_behavior(self):
        run = new_run("elig-none-db")
        revision = revision_for(run, 1)
        services.record_revision(revision)
        decision = services.check_persisted_export_eligibility(expected_case_id=run.case_id, expected_run_id=run.pk, requested_revision_id=revision.revision_id)
        self.assertEqual(decision.reasons, (ExportDenialReason.NO_APPROVAL,))
        with self.assertRaises(RecordNotFoundError):
            services.check_persisted_export_eligibility(expected_case_id=run.case_id, expected_run_id=uuid.uuid4(), requested_revision_id=revision.revision_id)


@tag("requires_postgres")
@requires_postgres
class WriterSerializationTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    """Competing writers on one run, each on its own database connection.

    These need committed data visible across connections, hence TransactionTestCase.
    """

    def run_threads(self, targets):
        results = [None] * len(targets)
        barrier = threading.Barrier(len(targets))

        def work(index, target):
            try:
                with connection.cursor() as cursor:  # this thread's own connection
                    cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'")  # a stuck lock becomes an error
                barrier.wait(timeout=BARRIER_TIMEOUT)
                results[index] = ("ok", target())
            except Exception as error:  # recorded and asserted on by the caller
                results[index] = ("error", error)
            finally:
                connection.close()  # always release the connection, success or failure

        # Daemon threads: a thread that somehow never returns cannot keep the test process alive.
        threads = [threading.Thread(target=work, args=(i, t), daemon=True) for i, t in enumerate(targets)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
        stuck = [thread for thread in threads if thread.is_alive()]
        self.assertEqual(stuck, [], "a writer did not finish within the bound (possible deadlock)")
        self.assertNotIn(None, results)
        return results

    def test_competing_writers_for_the_same_number_have_exactly_one_winner(self):
        run = new_run("race-numbers")
        candidates = [revision_for(run, 1, value=100 + i) for i in range(6)]
        results = self.run_threads([lambda c=c: services.record_revision(c) for c in candidates])
        winners = [r for r in results if r[0] == "ok"]
        losers = [r[1] for r in results if r[0] == "error"]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(all(isinstance(e, RevisionNumberError) for e in losers), losers)
        self.assertEqual(ResultRevision.objects.filter(run=run).count(), 1)
        stored = ResultRevision.objects.get(run=run)
        self.assertEqual(stored.pk, winners[0][1].pk)  # the stored row is the winner's
        self.assertEqual(stored.revision_number, 1)
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).current_revision_id, stored.pk)
        self.assertEqual(services.load_revision(stored.pk).revision_number, 1)

    def test_competing_writers_for_a_sequence_leave_a_gapless_history(self):
        run = new_run("race-sequence")
        services.record_revision(revision_for(run, 1))

        def writer(number):
            def write():
                # Each writer retries with the number the service says is next, as a caller would.
                for _ in range(20):
                    try:
                        return services.record_revision(revision_for(run, ResultRevision.objects.filter(run=run).count() + 1, value=number))
                    except RevisionNumberError:
                        continue
                raise AssertionError("could not record a revision")

            return write

        results = self.run_threads([writer(n) for n in range(4)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        numbers = list(ResultRevision.objects.filter(run=run).order_by("revision_number").values_list("revision_number", flat=True))
        self.assertEqual(numbers, list(range(1, 6)))
        current = WorkflowRun.objects.get(pk=run.pk).current_revision
        self.assertEqual(current.revision_number, 5)

    def test_an_approval_racing_a_new_revision_is_recorded_only_if_it_won(self):
        run = new_run("race-approval")
        first = revision_for(run, 1)
        services.record_revision(first)
        record = approve_revision(first, approver_id=APPROVER)
        second = revision_for(run, 2)
        results = self.run_threads([lambda: services.record_approval(record), lambda: services.record_revision(second)])
        approval_result, revision_result = results
        self.assertEqual(revision_result[0], "ok", results)  # the new revision always lands
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).current_revision_id, second.revision_id)
        if approval_result[0] == "ok":
            self.assertEqual(Approval.objects.filter(revision_id=first.revision_id).count(), 1)
        else:
            self.assertIsInstance(approval_result[1], NotCurrentRevisionError)
            self.assertEqual(Approval.objects.count(), 0)
        # Either way the approval, if stored, is historical: it does not authorize the new revision.
        decision_reasons = check_export_eligibility(
            expected_case_id=run.case_id,
            expected_run_id=run.pk,
            requested_revision=second,
            current_revision=second,
            approval=record,
        ).reasons
        self.assertIn(ExportDenialReason.APPROVAL_REVISION_MISMATCH, decision_reasons)


def _fields(revision):
    return {
        "case_id": revision.case_id,
        "run_id": revision.run_id,
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "canonical_format_version": revision.canonical_format_version,
        "canonical_bytes": revision.canonical_bytes,
        "content_hash": revision.content_hash,
        "findings": revision.findings,
    }


def _approval_fields(record):
    return {
        "case_id": record.case_id,
        "run_id": record.run_id,
        "revision_id": record.revision_id,
        "content_hash": record.content_hash,
        "approver_id": record.approver_id,
        "acknowledged_warnings": record.acknowledged_warnings,
    }
