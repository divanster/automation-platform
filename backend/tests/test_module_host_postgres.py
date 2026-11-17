"""Tests for the module host that REQUIRE a real PostgreSQL server (MOD-1b S4).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and
docs/development/runbook.md). When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; the mixin they reuse refuses any
other name.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project owner runs them locally they are NOT RUN.
Their DB-free counterparts are tests/test_module_host_rules.py, _services.py, _gate.py, _cli.py and _boundaries.py (those pass in the VM).

What they cover: the migration (additive, round trip, existing data untouched); the stored constraints; namespace ownership that persists and cannot be
reassigned or deleted; sync, enable and disable against real rows, with the row locks and the audit event in one transaction; rollback when the audit write
fails; concurrent enables, disables and syncs; and the worker with the module gate on committed data: tasks held (never failed) while a module is disabled,
resumed after it is enabled, a running attempt finishing, recovery and retry unchanged, version mismatch, missing state after the sync, and Request
Intake's legacy task kind working with the production composition. The existing suites (test_tasks_postgres, test_tasks_gating_postgres,
test_tasks_recovery_postgres, test_intake_postgres, test_api_postgres, test_audit_vocabulary_postgres) must still pass UNCHANGED.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_module_host_postgres --tag requires_postgres

This file is ASCII only.
"""

import uuid
from types import MappingProxyType
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext

from apps.audit import events
from apps.audit.models import AuditEvent
from apps.audit.recorder import record_event
from apps.cases.errors import ImmutableRecordError
from apps.cases.models import WorkflowRun
from apps.intake.submission import submit_request
from apps.module_host import gate
from apps.module_host import services as host
from apps.module_host.errors import ModuleHostError
from apps.module_host.models import ModuleHostMarker, ModuleNamespaceClaim, ModuleState
from apps.modules.manifest import TaskHandlerDeclaration
from apps.modules.registry import ModuleRegistry
from apps.tasks import handlers, worker
from apps.tasks import services as task_services
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.policy import TaskRetryableError
from config import task_registry
from tests import test_tasks_postgres as base
from tests.intake_support import VALID_TEXT
from tests.module_support import FakeHandler, dep, named

requires_postgres = base.requires_postgres
T0, at = base.T0, base.at
HEX = "b" * 64


def mod(module_id, *, kinds=(), deps=(), **overrides):
    return named(
        module_id,
        task_handlers=tuple(TaskHandlerDeclaration(k, FakeHandler(k), "does work") for k in kinds),
        dependencies=tuple(dep(d) for d in deps),
        **overrides,
    )


BASE, LEAF, SOLO = "base_host", "leaf_host", "solo_host"
KIND_BASE, KIND_LEAF, KIND_SOLO = "base_host.work", "leaf_host.work", "solo_host.work"
MODULE_KINDS = (KIND_BASE, KIND_LEAF, KIND_SOLO)
LEGACY = frozenset({"test.add_numbers"})
UNKNOWN = "os.system"
REG = ModuleRegistry.build([mod(BASE, kinds=(KIND_BASE,)), mod(LEAF, kinds=(KIND_LEAF,), deps=(BASE,)), mod(SOLO, kinds=(KIND_SOLO,))])


def module_events():
    return list(AuditEvent.objects.filter(subject_type="module").order_by("id"))


def state(module_id):
    return ModuleState.objects.get(module_id=module_id)


def synced(now=T0):
    host.sync_modules(REG, now=now)


def untouched(task):
    row = Task.objects.get(pk=task.pk)
    return (row.state, row.attempt_count, row.lease_token, TaskAttempt.objects.filter(task=row).count()) == ("queued", 0, None, 0)


def valid_state_fields(**changes):
    fields = {
        "module_id": "alpha_host",
        "installed_version": "1.0.0",
        "enabled": True,
        "registry_fingerprint": HEX,
        "created_at": T0,
        "updated_at": T0,
        "enabled_changed_at": T0,
    }
    fields.update(changes)
    return fields


# --- schema and migration ------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SchemaTests(base.DedicatedTestDatabaseMixin, TestCase):
    def test_a_valid_state_row_is_stored_and_read_back(self):
        row = ModuleState.objects.create(**valid_state_fields())
        stored = ModuleState.objects.get(pk=row.pk)
        self.assertEqual((stored.module_id, stored.installed_version, stored.enabled, stored.configuration_version), ("alpha_host", "1.0.0", True, None))
        self.assertIsInstance(stored.pk, uuid.UUID)

    def test_the_module_id_is_unique(self):
        ModuleState.objects.create(**valid_state_fields())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModuleState.objects.create(**valid_state_fields())

    def test_enabled_has_no_default_in_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModuleState.objects.create(**valid_state_fields(enabled=None))

    def test_malformed_ids_versions_and_fingerprints_are_refused_by_the_database(self):
        bad = [
            {"module_id": "Alpha_Host"},
            {"module_id": "ab"},
            {"module_id": "has space"},
            {"module_id": "x" * 41},
            {"installed_version": "1.0"},
            {"installed_version": "01.0.0"},
            {"installed_version": "1.0.0-beta"},
            {"installed_version": "v1.0.0"},
            {"installed_version": ""},
            {"registry_fingerprint": "A" * 64},
            {"registry_fingerprint": "b" * 63},
            {"registry_fingerprint": ""},
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ModuleState.objects.create(**valid_state_fields(**changes))

    def test_valid_extremes_are_accepted(self):
        for index, version in enumerate(("0.0.0", "999999999.999999999.999999999", "1.10.0")):
            ModuleState.objects.create(**valid_state_fields(module_id=f"ext_host_{index}", installed_version=version))
        self.assertEqual(ModuleState.objects.count(), 3)

    def test_a_namespace_can_be_claimed_once_whoever_asks(self):
        ModuleNamespaceClaim.objects.create(namespace="legacy_ns", module_id="alpha_host", claimed_at=T0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModuleNamespaceClaim.objects.create(namespace="legacy_ns", module_id="beta_host", claimed_at=T0)
        self.assertEqual(ModuleNamespaceClaim.objects.get(namespace="legacy_ns").module_id, "alpha_host")

    def test_malformed_claims_are_refused_by_the_database(self):
        for changes in ({"namespace": "A"}, {"namespace": "x"}, {"namespace": "has space"}, {"module_id": "AB"}, {"module_id": "ab"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ModuleNamespaceClaim.objects.create(**{"namespace": "legacy_ns", "module_id": "alpha_host", "claimed_at": T0, **changes})

    def test_a_namespace_claim_cannot_be_changed_or_removed_through_the_orm(self):
        claim = ModuleNamespaceClaim.objects.create(namespace="legacy_ns", module_id="alpha_host", claimed_at=T0)
        claim.module_id = "beta_host"
        with self.assertRaises(ImmutableRecordError):
            claim.save()
        with self.assertRaises(ImmutableRecordError):
            claim.delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleNamespaceClaim.objects.filter(namespace="legacy_ns").update(module_id="beta_host")
        with self.assertRaises(ImmutableRecordError):
            ModuleNamespaceClaim.objects.all().delete()
        self.assertEqual(ModuleNamespaceClaim.objects.get(namespace="legacy_ns").module_id, "alpha_host")

    def test_a_state_row_cannot_be_deleted_through_the_orm_but_can_be_updated_by_the_services(self):
        row = ModuleState.objects.create(**valid_state_fields())
        with self.assertRaises(ImmutableRecordError):
            row.delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleState.objects.all().delete()
        self.assertEqual(ModuleState.objects.count(), 1)

    def test_the_marker_is_one_row_the_database_refuses_a_second_or_a_malformed_one(self):
        ModuleHostMarker.objects.create(initialized_at=T0, initial_registry_fingerprint=HEX)
        self.assertEqual(ModuleHostMarker.objects.get().pk, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModuleHostMarker.objects.create(initialized_at=at(5), initial_registry_fingerprint=HEX)  # the same primary key
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModuleHostMarker.objects.create(id=2, initialized_at=T0, initial_registry_fingerprint=HEX)  # CHECK id = 1
        self.assertEqual(ModuleHostMarker.objects.count(), 1)

    def test_a_malformed_marker_fingerprint_is_refused_by_the_database(self):
        for bad in ("", "A" * 64, "b" * 63):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                ModuleHostMarker.objects.create(initialized_at=T0, initial_registry_fingerprint=bad)

    def test_the_marker_cannot_be_reset_changed_or_deleted_through_the_orm(self):
        marker = ModuleHostMarker.objects.create(initialized_at=T0, initial_registry_fingerprint=HEX)
        marker.initialized_at = at(99)
        with self.assertRaises(ImmutableRecordError):
            marker.save()
        with self.assertRaises(ImmutableRecordError):
            marker.delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleHostMarker.objects.all().delete()
        with self.assertRaises(ImmutableRecordError):
            ModuleHostMarker.objects.all().update(initialized_at=at(99))
        self.assertEqual(ModuleHostMarker.objects.get().initialized_at, T0)

    def test_the_tables_hold_no_foreign_key(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.table_constraints WHERE constraint_type = 'FOREIGN KEY' AND table_name IN ('module_host_modulestate', 'module_host_modulenamespaceclaim', 'module_host_modulehostmarker')"
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_new_audit_actions_pass_the_existing_database_shape_check(self):
        with transaction.atomic():
            for action in ("module.enabled", "module.disabled"):
                record_event(
                    events.system_event(
                        action, "module", uuid.uuid4(), module_id="alpha_host", installed_version="1.0.0", registry_fingerprint=HEX, cause="admin_command"
                    )
                )
        self.assertEqual(AuditEvent.objects.filter(subject_type="module").count(), 2)


@tag("requires_postgres")
@requires_postgres
class MigrationTests(base.DedicatedTestDatabaseMixin, TransactionTestCase):
    TABLES = {"module_host_modulestate", "module_host_modulenamespaceclaim", "module_host_modulehostmarker"}

    def tables(self):
        return set(connection.introspection.table_names())

    def test_the_migration_is_applied_and_creates_exactly_the_three_tables(self):
        executor = MigrationExecutor(connection)
        self.assertIn(("module_host", "0001_initial"), executor.loader.applied_migrations)
        self.assertTrue(self.TABLES <= self.tables())
        with connection.cursor() as cursor:
            columns = {c.name for c in connection.introspection.get_table_description(cursor, "module_host_modulestate")}
        self.assertEqual(columns, {"id", "module_id", "installed_version", "enabled", "configuration_version", "registry_fingerprint", "created_at", "updated_at", "enabled_changed_at"})

    def test_reversing_and_reapplying_touches_no_other_table_and_no_other_row(self):
        user = get_user_model().objects.create_user(username="keeper")
        task = task_services.enqueue_task(kind="test.add_numbers", payload={"values": [1]}, now=T0).task
        with transaction.atomic():
            record_event(events.system_event("run.state_changed", "run", uuid.uuid4(), from_state="a", to_state="b", cause="test"))
        audit_before = list(AuditEvent.objects.values())
        others_before = self.tables() - self.TABLES

        try:
            MigrationExecutor(connection).migrate([("module_host", None)])
            self.assertEqual(self.tables() & self.TABLES, set())  # only the host's own tables went
            self.assertEqual(self.tables() - self.TABLES, others_before)
            self.assertEqual(list(AuditEvent.objects.values()), audit_before)
        finally:
            MigrationExecutor(connection).migrate([("module_host", "0001_initial")])
        self.assertTrue(self.TABLES <= self.tables())
        self.assertEqual(ModuleState.objects.count(), 0)
        self.assertEqual(list(AuditEvent.objects.values()), audit_before)
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
        self.assertEqual(Task.objects.get(pk=task.pk).state, "queued")

    def test_the_audit_and_task_migrations_were_not_touched(self):
        applied = MigrationExecutor(connection).loader.applied_migrations
        for key in (("audit", "0001_initial"), ("audit", "0002_extensible_audit_vocabulary"), ("tasks", "0001_initial")):
            self.assertIn(key, applied)


# --- the services against real rows ---------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SyncTests(base.DedicatedTestDatabaseMixin, TestCase):
    def test_the_first_sync_creates_state_claims_and_one_event_per_module(self):
        report = host.sync_modules(REG, now=T0)
        self.assertTrue(report.initial and report.ok)
        self.assertEqual(sorted(ModuleState.objects.values_list("module_id", "installed_version", "enabled")), [(BASE, "1.0.0", True), (LEAF, "1.0.0", True), (SOLO, "1.0.0", True)])
        self.assertEqual(dict(ModuleNamespaceClaim.objects.values_list("namespace", "module_id")), dict(REG.namespace_owners))
        stored = state(BASE)
        self.assertEqual((stored.created_at, stored.updated_at, stored.enabled_changed_at, stored.registry_fingerprint, stored.configuration_version), (T0, T0, T0, REG.fingerprint, None))
        marker = ModuleHostMarker.objects.get()  # written by the same transaction
        self.assertEqual((marker.pk, marker.initialized_at, marker.initial_registry_fingerprint), (1, T0, REG.fingerprint))
        recorded = module_events()
        self.assertEqual([(e.action, e.actor_kind, e.actor_public_id, e.subject_type) for e in recorded], [("module.enabled", "system", None, "module")] * 3)
        self.assertEqual({e.subject_id for e in recorded}, set(ModuleState.objects.values_list("pk", flat=True)))
        for event in recorded:
            self.assertEqual(set(event.details), {"module_id", "installed_version", "registry_fingerprint", "cause"})
            self.assertEqual((event.details["cause"], event.details["registry_fingerprint"]), ("sync", REG.fingerprint))

    def test_a_second_sync_changes_nothing_and_records_nothing(self):
        synced()
        rows_before = list(ModuleState.objects.order_by("module_id").values())
        claims_before = list(ModuleNamespaceClaim.objects.order_by("namespace").values())
        events_before = module_events()
        report = host.sync_modules(REG, now=at(500))
        self.assertFalse(report.initial)
        self.assertEqual((report.created, report.refreshed, report.claims_added), ((), (), ()))
        self.assertEqual(list(ModuleState.objects.order_by("module_id").values()), rows_before)
        self.assertEqual(list(ModuleNamespaceClaim.objects.order_by("namespace").values()), claims_before)
        self.assertEqual(module_events(), events_before)
        self.assertEqual(list(ModuleHostMarker.objects.values()), [{"id": 1, "initialized_at": T0, "initial_registry_fingerprint": REG.fingerprint}])  # never rewritten

    def test_a_disabled_module_stays_disabled_across_a_sync_that_refreshes_its_fingerprint(self):
        synced()
        host.disable_module(REG, SOLO, now=at(10))
        ModuleState.objects.filter(module_id=SOLO).update(registry_fingerprint=HEX)  # the registry has changed since
        report = host.sync_modules(REG, now=at(20))
        self.assertEqual(report.refreshed, (SOLO,))
        stored = state(SOLO)
        self.assertEqual((stored.enabled, stored.registry_fingerprint, stored.enabled_changed_at), (False, REG.fingerprint, at(10)))
        self.assertEqual(stored.updated_at, at(20))
        self.assertEqual([e.action for e in module_events()].count("module.enabled"), 3)  # the sync enabled nothing

    def test_a_new_module_next_to_a_disabled_dependency_is_created_disabled(self):
        only_base = ModuleRegistry.build([mod(BASE, kinds=(KIND_BASE,))])
        host.sync_modules(only_base, now=T0)
        host.disable_module(only_base, BASE, now=at(1))
        report = host.sync_modules(REG, now=at(2))
        self.assertEqual(dict(report.created), {LEAF: False, SOLO: True})
        self.assertEqual((state(LEAF).enabled, state(BASE).enabled), (False, False))
        by_module = {e.details["module_id"]: (e.action, e.details["cause"]) for e in module_events()[-2:]}
        self.assertEqual(by_module, {LEAF: ("module.disabled", "sync"), SOLO: ("module.enabled", "sync")})

    def test_a_version_mismatch_is_reported_and_nothing_about_it_changes(self):
        synced()
        ModuleState.objects.filter(module_id=SOLO).update(installed_version="0.9.0")
        events_before = len(module_events())
        report = host.sync_modules(REG, now=at(5))
        self.assertFalse(report.ok)
        self.assertEqual([(m.module_id, m.installed_version, m.registered_version) for m in report.version_mismatches], [(SOLO, "0.9.0", "1.0.0")])
        stored = state(SOLO)
        self.assertEqual((stored.installed_version, stored.enabled), ("0.9.0", True))
        self.assertEqual(len(module_events()), events_before)

    def test_a_namespace_that_changed_owner_is_refused_and_everything_rolls_back(self):
        synced()
        claims_before = list(ModuleNamespaceClaim.objects.order_by("namespace").values())
        rows_before = list(ModuleState.objects.order_by("module_id").values())
        hijack = ModuleRegistry.build([mod(BASE, kinds=(KIND_BASE,)), mod(LEAF, kinds=(KIND_LEAF,), deps=(BASE,)), mod("thief_host", extra_namespaces=(SOLO,))])
        with self.assertRaises(ModuleHostError) as caught:
            host.sync_modules(hijack, now=at(5))
        self.assertEqual(caught.exception.code, "namespace_ownership_changed")
        self.assertEqual(list(ModuleNamespaceClaim.objects.order_by("namespace").values()), claims_before)
        self.assertEqual(list(ModuleState.objects.order_by("module_id").values()), rows_before)
        self.assertFalse(ModuleState.objects.filter(module_id="thief_host").exists())
        self.assertEqual(len(module_events()), 3)

    def test_a_released_namespace_is_refused(self):
        synced()
        without_solo = ModuleRegistry.build([mod(BASE, kinds=(KIND_BASE,)), mod(LEAF, kinds=(KIND_LEAF,), deps=(BASE,))])
        with self.assertRaises(ModuleHostError) as caught:
            host.sync_modules(without_solo, now=at(5))
        self.assertEqual(caught.exception.code, "namespace_released")
        self.assertEqual(ModuleNamespaceClaim.objects.filter(module_id=SOLO).count(), 1)  # never deleted
        self.assertEqual(ModuleState.objects.filter(module_id=SOLO).count(), 1)

    def test_namespaces_are_claimed_for_additions_without_touching_existing_claims(self):
        only_base = ModuleRegistry.build([mod(BASE, kinds=(KIND_BASE,))])
        host.sync_modules(only_base, now=T0)
        first = ModuleNamespaceClaim.objects.get(namespace=BASE)
        host.sync_modules(REG, now=at(9))
        self.assertEqual(ModuleNamespaceClaim.objects.get(namespace=BASE), first)  # same row, same claimed_at
        self.assertEqual(ModuleNamespaceClaim.objects.get(namespace=BASE).claimed_at, T0)
        self.assertEqual(ModuleNamespaceClaim.objects.count(), 3)

    def test_a_failing_audit_write_rolls_the_whole_sync_back(self):
        calls = []

        def fail_on_second(event):
            calls.append(event)
            if len(calls) == 2:
                raise OperationalError("audit write failed")
            return record_event(event)

        with mock.patch.object(host, "record_event", side_effect=fail_on_second):
            with self.assertRaises(OperationalError):
                host.sync_modules(REG, now=T0)
        self.assertEqual((ModuleState.objects.count(), ModuleNamespaceClaim.objects.count(), len(module_events()), ModuleHostMarker.objects.count()), (0, 0, 0, 0))

    def test_a_refused_sync_writes_no_marker(self):
        thief = ModuleRegistry.build([mod("thief_host", extra_namespaces=(SOLO,))])
        ModuleNamespaceClaim.objects.create(namespace=SOLO, module_id=SOLO, claimed_at=T0)
        with self.assertRaises(ModuleHostError):
            host.sync_modules(thief, now=at(1))
        self.assertEqual((ModuleHostMarker.objects.count(), ModuleState.objects.count()), (0, 0))

    def test_rows_without_a_marker_are_repaired_by_the_next_sync(self):
        synced()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulehostmarker")  # an inconsistent database (raw SQL; the ORM refuses)
        self.assertTrue(host.get_module_status(REG).synced)  # fails closed meanwhile
        report = host.sync_modules(REG, now=at(7))
        self.assertTrue(report.initial)
        self.assertEqual(ModuleHostMarker.objects.get().initialized_at, at(7))
        self.assertEqual(len(module_events()), 3)  # nothing else changed

    def test_an_empty_registry_sync_writes_the_marker_so_a_later_module_without_a_row_is_held(self):
        empty = ModuleRegistry.build([])
        report = host.sync_modules(empty, now=T0)
        self.assertTrue(report.initial)
        self.assertEqual((ModuleHostMarker.objects.count(), ModuleState.objects.count()), (1, 0))
        status = host.get_module_status(REG)  # a release that registers modules arrives; nobody ran the sync for it
        self.assertTrue(status.synced)
        self.assertEqual({(m.claimable, m.reason) for m in status.modules}, {(False, "missing_state")})
        host.sync_modules(REG, now=at(1))  # the upgrade routine releases them
        self.assertEqual({(m.claimable, m.reason) for m in host.get_module_status(REG).modules}, {(True, "enabled")})

    def test_the_sync_takes_the_advisory_lock_and_locks_the_rows(self):
        synced()
        with CaptureQueriesContext(connection) as queries:
            host.sync_modules(REG, now=at(3))
        statements = [q["sql"] for q in queries]
        self.assertTrue(any("pg_advisory_xact_lock" in s for s in statements))
        self.assertTrue(any("FOR UPDATE" in s and "module_host_modulestate" in s for s in statements))

    def test_an_empty_registry_syncs_to_nothing(self):
        report = host.sync_modules(ModuleRegistry.build([]), now=T0)
        self.assertEqual((report.created, report.claims_added, ModuleState.objects.count(), ModuleNamespaceClaim.objects.count()), ((), (), 0, 0))
        self.assertTrue(report.ok)

    def test_existing_core_audit_events_are_untouched_by_a_sync(self):
        with transaction.atomic():
            record_event(events.system_event("run.state_changed", "run", uuid.uuid4(), from_state="a", to_state="b", cause="test"))
        before = list(AuditEvent.objects.exclude(subject_type="module").values())
        synced()
        self.assertEqual(list(AuditEvent.objects.exclude(subject_type="module").values()), before)


@tag("requires_postgres")
@requires_postgres
class EnableDisableTests(base.DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        super().setUp()
        synced()
        self.baseline = len(module_events())

    def test_disabling_and_enabling_change_the_row_and_record_one_event_each(self):
        self.assertTrue(host.disable_module(REG, SOLO, now=at(10)).changed)
        stored = state(SOLO)
        self.assertEqual((stored.enabled, stored.enabled_changed_at, stored.updated_at, stored.created_at), (False, at(10), at(10), T0))
        self.assertTrue(host.enable_module(REG, SOLO, now=at(20)).changed)
        stored = state(SOLO)
        self.assertEqual((stored.enabled, stored.enabled_changed_at), (True, at(20)))
        recorded = module_events()[self.baseline:]
        self.assertEqual([(e.action, e.details["module_id"], e.details["cause"]) for e in recorded], [("module.disabled", SOLO, "admin_command"), ("module.enabled", SOLO, "admin_command")])
        self.assertEqual({e.subject_id for e in recorded}, {stored.pk})
        self.assertEqual({(e.actor_kind, e.actor_public_id, e.case_id, e.run_id) for e in recorded}, {("system", None, None, None)})

    def test_idempotent_calls_write_nothing_and_record_nothing(self):
        before = list(ModuleState.objects.order_by("module_id").values())
        self.assertFalse(host.enable_module(REG, SOLO, now=at(10)).changed)
        host.disable_module(REG, SOLO, now=at(11))
        mid = list(ModuleState.objects.order_by("module_id").values())
        self.assertFalse(host.disable_module(REG, SOLO, now=at(12)).changed)
        self.assertEqual(list(ModuleState.objects.order_by("module_id").values()), mid)
        self.assertEqual(len(module_events()), self.baseline + 1)
        self.assertNotEqual(before, mid)

    def test_the_audit_events_agree_with_the_state_after_any_sequence(self):
        sequence = [("disable", SOLO), ("disable", SOLO), ("enable", SOLO), ("disable", LEAF), ("disable", BASE), ("enable", BASE), ("enable", LEAF), ("enable", LEAF)]
        changes = 0
        for step, (verb, module_id) in enumerate(sequence):
            result = (host.disable_module if verb == "disable" else host.enable_module)(REG, module_id, now=at(100 + step))
            changes += result.changed
        recorded = module_events()[self.baseline:]
        self.assertEqual(len(recorded), changes)
        replayed = {}
        for event in recorded:
            replayed[event.details["module_id"]] = event.action == "module.enabled"
        for module_id, enabled in replayed.items():
            self.assertEqual(state(module_id).enabled, enabled, module_id)

    def test_disabling_is_refused_while_an_enabled_module_depends_on_it(self):
        with self.assertRaises(ModuleHostError) as caught:
            host.disable_module(REG, BASE, now=at(10))
        self.assertEqual(caught.exception.code, "dependents_enabled")
        self.assertTrue(state(BASE).enabled)
        self.assertEqual(len(module_events()), self.baseline)

    def test_enabling_is_refused_while_a_dependency_is_disabled(self):
        host.disable_module(REG, LEAF, now=at(10))
        host.disable_module(REG, BASE, now=at(11))
        with self.assertRaises(ModuleHostError) as caught:
            host.enable_module(REG, LEAF, now=at(12))
        self.assertEqual(caught.exception.code, "dependency_not_enabled")
        self.assertFalse(state(LEAF).enabled)
        host.enable_module(REG, BASE, now=at(13))
        host.enable_module(REG, LEAF, now=at(14))
        self.assertTrue(state(LEAF).enabled)

    def test_an_unknown_module_and_a_missing_row_are_refused_without_a_change(self):
        with self.assertRaises(ModuleHostError) as caught:
            host.enable_module(REG, "ghost_host", now=at(10))
        self.assertEqual(caught.exception.code, "unknown_module")
        ModuleState.objects.filter(module_id=SOLO).update(installed_version="0.9.0")
        with self.assertRaises(ModuleHostError) as caught:
            host.enable_module(REG, SOLO, now=at(11))
        self.assertEqual(caught.exception.code, "version_mismatch")
        self.assertEqual(len(module_events()), self.baseline)

    def test_a_version_mismatched_module_can_still_be_disabled(self):
        ModuleState.objects.filter(module_id=SOLO).update(installed_version="0.9.0")
        self.assertTrue(host.disable_module(REG, SOLO, now=at(10)).changed)
        self.assertFalse(state(SOLO).enabled)

    def test_a_failing_audit_write_rolls_the_state_change_back(self):
        for verb, function, expected in (("disable", host.disable_module, True), ("enable", host.enable_module, False)):
            if verb == "enable":
                host.disable_module(REG, SOLO, now=at(50))
            before = list(ModuleState.objects.order_by("module_id").values())
            events_before = len(module_events())
            with self.subTest(verb=verb), mock.patch.object(host, "record_event", side_effect=OperationalError("audit down")):
                with self.assertRaises(OperationalError):
                    function(REG, SOLO, now=at(60))
            self.assertEqual(list(ModuleState.objects.order_by("module_id").values()), before)
            self.assertEqual(len(module_events()), events_before)
            self.assertEqual(state(SOLO).enabled, expected)

    def test_the_operation_locks_every_state_row(self):
        with CaptureQueriesContext(connection) as queries:
            host.disable_module(REG, SOLO, now=at(10))
        locking = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        self.assertEqual(len(locking), 1)
        self.assertIn("module_host_modulestate", locking[0])
        self.assertIn("ORDER BY", locking[0])

    def test_no_task_is_touched_by_disabling_or_enabling(self):
        tasks = [task_services.enqueue_task(kind="test.add_numbers", payload={"values": [i]}, now=T0).task for i in range(2)]
        before = [base.snapshot(t) for t in tasks]
        host.disable_module(REG, SOLO, now=at(10))
        host.enable_module(REG, SOLO, now=at(11))
        self.assertEqual([base.snapshot(t) for t in tasks], before)

    def test_status_matches_what_the_operations_did(self):
        host.disable_module(REG, SOLO, now=at(10))
        status = host.get_module_status(REG)
        self.assertTrue(status.synced)
        self.assertEqual((status.module(SOLO).enabled, status.module(SOLO).claimable, status.module(SOLO).reason), (False, False, "disabled"))
        self.assertEqual((status.module(BASE).enabled, status.module(BASE).claimable), (True, True))
        self.assertEqual(status.module(SOLO).enabled_changed_at, at(10).isoformat())
        self.assertEqual([m.module_id for m in status.modules], list(REG.module_ids))

    def test_status_before_any_sync_says_modules_are_treated_as_enabled(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate")  # the sync never ran: simulate with raw SQL (the ORM refuses); the marker goes too
            cursor.execute("DELETE FROM module_host_modulehostmarker")
        status = host.get_module_status(REG)
        self.assertFalse(status.synced)
        self.assertEqual({(m.claimable, m.reason) for m in status.modules}, {(True, "legacy_unsynced")})

    def test_deleting_every_state_row_does_not_reopen_the_compatibility_window(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate")  # the marker stays
        status = host.get_module_status(REG)
        self.assertTrue(status.synced)
        self.assertEqual({(m.claimable, m.reason) for m in status.modules}, {(False, "missing_state")})

    def test_status_after_the_sync_fails_closed_for_a_row_that_is_gone(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate WHERE module_id = %s", [SOLO])
        status = host.get_module_status(REG)
        self.assertEqual((status.module(SOLO).claimable, status.module(SOLO).reason), (False, "missing_state"))


# --- concurrency -----------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(base.ThreadedMixin, base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """Committed data visible across connections, hence TransactionTestCase; one connection per thread."""

    def test_concurrent_syncs_create_everything_exactly_once(self):
        results = self.run_threads([lambda: host.sync_modules(REG, now=T0) for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(len(report.created) for _, report in results), 3)
        self.assertEqual(sum(1 for _, report in results if report.initial), 1)
        self.assertEqual((ModuleState.objects.count(), ModuleNamespaceClaim.objects.count(), len(module_events())), (3, 3, 3))
        self.assertEqual(ModuleHostMarker.objects.count(), 1)

    def test_concurrent_syncs_of_an_empty_registry_write_one_marker(self):
        empty = ModuleRegistry.build([])
        results = self.run_threads([lambda: host.sync_modules(empty, now=T0) for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(1 for _, report in results if report.initial), 1)
        self.assertEqual(ModuleHostMarker.objects.count(), 1)

    def test_concurrent_enables_of_one_module_change_it_once_and_audit_it_once(self):
        synced()
        host.disable_module(REG, SOLO, now=at(1))
        baseline = len(module_events())
        results = self.run_threads([lambda: host.enable_module(REG, SOLO, now=at(2)) for _ in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(1 for _, r in results if r.changed), 1)
        self.assertEqual(len(module_events()) - baseline, 1)
        self.assertTrue(state(SOLO).enabled)

    def test_concurrent_enables_and_disables_leave_events_and_state_consistent(self):
        synced()
        baseline = len(module_events())
        targets = [(lambda: host.disable_module(REG, SOLO)) if i % 2 == 0 else (lambda: host.enable_module(REG, SOLO)) for i in range(8)]
        results = self.run_threads(targets)
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        recorded = [e for e in module_events()[baseline:]]
        self.assertEqual(len(recorded), sum(1 for _, r in results if r.changed))
        for earlier, later in zip(recorded, recorded[1:]):
            self.assertNotEqual(earlier.action, later.action)  # every recorded event is a real change: they strictly alternate
        if recorded:
            self.assertEqual(state(SOLO).enabled, recorded[-1].action == "module.enabled")

    def test_enabling_a_dependent_while_its_dependency_is_disabled_never_leaves_an_enabled_dependent_on_a_disabled_dependency(self):
        synced()
        for round_number in range(5):
            with self.subTest(round=round_number):
                ModuleState.objects.all().update(enabled=True)  # (a test-only reset through the ORM, which allows updates)
                host.disable_module(REG, LEAF)
                results = self.run_threads([lambda: host.enable_module(REG, LEAF), lambda: host.disable_module(REG, BASE)])
                leaf, base_row = state(LEAF), state(BASE)
                self.assertFalse(leaf.enabled and not base_row.enabled, "an enabled module depends on a disabled one")
                outcomes = sorted(value.code if kind == "error" and isinstance(value, ModuleHostError) else kind for kind, value in results)
                # Either the enable won (and the disable is then refused) or the disable won (and the enable is then refused): never both.
                self.assertIn(outcomes, (["dependents_enabled", "ok"], ["dependency_not_enabled", "ok"]), results)

    def test_a_sync_next_to_enables_and_disables_keeps_every_row_consistent(self):
        synced()
        results = self.run_threads(
            [lambda: host.sync_modules(REG, now=at(5)), lambda: host.disable_module(REG, SOLO), lambda: host.enable_module(REG, SOLO), lambda: host.sync_modules(REG, now=at(6))]
        )
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual((ModuleState.objects.count(), ModuleNamespaceClaim.objects.count()), (3, 3))
        recorded = [e for e in module_events() if e.details["cause"] == "admin_command"]
        if recorded:
            self.assertEqual(state(SOLO).enabled, recorded[-1].action == "module.enabled")


# --- the worker gate on committed data -------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class GatedWorkerTests(base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """``process_one`` with the module gate installed, on committed data, with an injected clock."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        self.ran = []
        self.registry = self.build_registry()
        worker.install_composition(worker.Composition(registry=self.registry, kind_gate=gate.make_kind_gate(REG, LEGACY)))

    def build_registry(self, overrides=None):
        runs = {kind: (lambda payload, kind=kind: self.ran.append(kind) or {"ok": 1}) for kind in MODULE_KINDS}
        runs.update(overrides or {})
        extra = {kind: handlers.TaskHandler(kind=kind, validate=lambda p: p, run=run, is_test_handler=True) for kind, run in runs.items()}
        return MappingProxyType({**handlers.HANDLERS, **extra})

    def reinstall(self, overrides):
        self.registry = self.build_registry(overrides)
        worker.install_composition(worker.Composition(registry=self.registry, kind_gate=gate.make_kind_gate(REG, LEGACY)))

    def enqueue(self, kind, payload=None, **changes):
        changes.setdefault("now", T0)
        return task_services.enqueue_task(kind=kind, payload={} if payload is None else payload, registry=self.registry, **changes).task

    def process(self, seconds=0):
        return worker.process_one(worker_id="w-host", lease_seconds=60, clock=lambda: at(seconds))

    def test_enabled_modules_run_their_tasks(self):
        synced()
        tasks = [self.enqueue(kind) for kind in MODULE_KINDS]
        statuses = [self.process(i).status for i in range(3)]
        self.assertEqual(statuses, [worker.ProcessStatus.SUCCEEDED] * 3)
        self.assertEqual(sorted(self.ran), sorted(MODULE_KINDS))
        self.assertEqual({Task.objects.get(pk=t.pk).state for t in tasks}, {"succeeded"})

    def test_a_disabled_modules_queued_task_is_held_not_failed_and_runs_after_the_enable(self):
        synced()
        held = self.enqueue(KIND_SOLO)
        host.disable_module(REG, SOLO, now=at(1))
        for seconds in (2, 3, 4):
            self.assertEqual(self.process(seconds).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))  # still queued, attempt_count 0, no attempt row, no failure
        self.assertEqual(Task.objects.get(pk=held.pk).failure_code, "")
        host.enable_module(REG, SOLO, now=at(5))
        self.assertEqual(self.process(6).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=held.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("succeeded", 1))
        self.assertEqual(list(TaskAttempt.objects.filter(task=held).values_list("attempt_number", "outcome")), [(1, "succeeded")])
        self.assertEqual(self.ran, [KIND_SOLO])

    def test_the_original_order_is_kept_when_held_tasks_are_released(self):
        synced()
        host.disable_module(REG, SOLO, now=at(1))
        first = self.enqueue(KIND_SOLO, {"n": 1})
        second = self.enqueue(KIND_SOLO, {"n": 2}, now=at(1))  # created later: its position is after the first
        self.process(2)
        host.enable_module(REG, SOLO, now=at(3))
        claimed = [self.process(4).task_id, self.process(5).task_id]
        self.assertEqual(claimed, [first.pk, second.pk])

    def test_only_the_disabled_modules_tasks_are_held_others_and_legacy_kinds_keep_running(self):
        synced()
        host.disable_module(REG, SOLO, now=at(1))
        held = self.enqueue(KIND_SOLO)
        other = self.enqueue(KIND_BASE)
        legacy = self.enqueue("test.add_numbers", {"values": [1, 2]})
        statuses = sorted(self.process(i).status.value for i in range(2, 5))
        self.assertEqual(statuses, ["idle", "succeeded", "succeeded"])
        self.assertTrue(untouched(held))
        self.assertEqual((Task.objects.get(pk=other.pk).state, Task.objects.get(pk=legacy.pk).state), ("succeeded", "succeeded"))

    def test_a_running_attempt_finishes_after_its_module_is_disabled(self):
        synced()

        def run_and_get_disabled(payload):
            host.disable_module(REG, SOLO, now=at(1))  # the module is disabled while its own attempt is running
            return {"done": 1}

        self.reinstall({KIND_SOLO: run_and_get_disabled})
        running = self.enqueue(KIND_SOLO)
        queued_behind = self.enqueue(KIND_SOLO, {"n": 2}, now=at(1))  # created later, so the first task is the one claimed
        self.assertEqual(self.process(0).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(Task.objects.get(pk=running.pk).state, "succeeded")
        self.assertFalse(state(SOLO).enabled)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)  # the next one is held
        self.assertTrue(untouched(queued_behind))

    def test_an_expired_lease_of_a_disabled_module_is_recovered_and_then_held(self):
        synced()
        crashed = self.enqueue(KIND_SOLO)
        claimed = task_services.claim_next_task(worker_id="crashed", lease_seconds=60, now=T0)  # an unrestricted claim, then the process "dies"
        self.assertEqual(claimed.task_id, crashed.pk)
        host.disable_module(REG, SOLO, now=at(1))
        self.assertEqual(self.process(10000).status, worker.ProcessStatus.IDLE)  # recovery ran (the lease expired); the claim is held
        stored = Task.objects.get(pk=crashed.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("queued", 1))
        self.assertEqual(list(TaskAttempt.objects.filter(task=crashed).values_list("outcome", "failure_code")), [("abandoned", "lease_expired")])
        host.enable_module(REG, SOLO, now=at(10001))
        self.assertEqual(self.process(100000).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(Task.objects.get(pk=crashed.pk).attempt_count, 2)

    def test_a_retry_waits_while_the_module_is_disabled_and_runs_as_the_next_attempt_after_the_enable(self):
        synced()
        calls = []

        def flaky(payload):
            calls.append(1)
            if len(calls) == 1:
                raise TaskRetryableError()
            return {"calls": len(calls)}

        self.reinstall({KIND_SOLO: flaky})
        task = self.enqueue(KIND_SOLO)
        self.assertEqual(self.process(0).status, worker.ProcessStatus.REQUEUED)
        host.disable_module(REG, SOLO, now=at(1))
        self.assertEqual(self.process(100000).status, worker.ProcessStatus.IDLE)
        self.assertEqual(Task.objects.get(pk=task.pk).attempt_count, 1)
        host.enable_module(REG, SOLO, now=at(100001))
        self.assertEqual(self.process(100001).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=task.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("succeeded", 2))
        self.assertEqual(list(TaskAttempt.objects.filter(task=task).order_by("attempt_number").values_list("outcome", flat=True)), ["failed", "succeeded"])

    def test_a_version_mismatched_module_is_held(self):
        synced()
        ModuleState.objects.filter(module_id=SOLO).update(installed_version="0.9.0")
        held = self.enqueue(KIND_SOLO)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))
        ModuleState.objects.filter(module_id=SOLO).update(installed_version="1.0.0")
        self.assertEqual(self.process(2).status, worker.ProcessStatus.SUCCEEDED)  # the version matches again: released

    def test_a_module_without_state_after_the_sync_is_held(self):
        synced()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate WHERE module_id = %s", [SOLO])
        held = self.enqueue(KIND_SOLO)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))

    def test_after_the_sync_with_every_row_gone_the_modules_are_held_and_a_legacy_task_still_runs(self):
        synced()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate")  # the marker stays: the compatibility window must not reopen
        held = self.enqueue(KIND_SOLO)
        legacy = self.enqueue("test.add_numbers", {"values": [1]})
        statuses = sorted(self.process(i).status.value for i in (1, 2))
        self.assertEqual(statuses, ["idle", "succeeded"])
        self.assertTrue(untouched(held))
        self.assertEqual(Task.objects.get(pk=legacy.pk).state, "succeeded")

    def test_a_module_registered_after_an_empty_sync_is_held_until_the_sync_runs(self):
        host.sync_modules(ModuleRegistry.build([]), now=T0)  # initialization happened before any module existed
        held = self.enqueue(KIND_SOLO)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))
        synced(at(2))
        self.assertEqual(self.process(3).status, worker.ProcessStatus.SUCCEEDED)

    def test_before_the_initial_sync_every_registered_module_runs_as_before(self):
        self.assertEqual((ModuleState.objects.count(), ModuleHostMarker.objects.count()), (0, 0))
        task = self.enqueue(KIND_SOLO)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(Task.objects.get(pk=task.pk).state, "succeeded")

    def test_a_dependent_of_a_hand_disabled_dependency_is_held_too(self):
        synced()
        ModuleState.objects.filter(module_id=BASE).update(enabled=False)  # a database edit that bypassed the services
        held = self.enqueue(KIND_LEAF)
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(held))

    def test_a_task_of_an_unknown_kind_is_left_queued_not_failed(self):
        synced()
        stranger = base.raw_task(kind=UNKNOWN, payload={})
        self.assertEqual(self.process(1).status, worker.ProcessStatus.IDLE)
        self.assertTrue(untouched(stranger))

    def test_a_database_error_from_the_state_read_claims_nothing(self):
        synced()
        task = self.enqueue(KIND_SOLO)
        with mock.patch.object(gate, "read_host_state", side_effect=OperationalError("down")):
            with self.assertRaises(OperationalError):
                self.process(1)
        self.assertTrue(untouched(task))
        self.assertEqual(self.process(2).status, worker.ProcessStatus.SUCCEEDED)  # the database is back: normal service

    def test_a_disable_takes_effect_on_the_next_iteration_without_a_restart(self):
        synced()
        first, second = self.enqueue(KIND_SOLO, {"n": 1}), self.enqueue(KIND_SOLO, {"n": 2}, now=at(1))
        self.assertEqual(self.process(1).status, worker.ProcessStatus.SUCCEEDED)
        host.disable_module(REG, SOLO, now=at(2))
        self.assertEqual(self.process(3).status, worker.ProcessStatus.IDLE)
        self.assertEqual((Task.objects.get(pk=first.pk).state, Task.objects.get(pk=second.pk).state), ("succeeded", "queued"))


@tag("requires_postgres")
@requires_postgres
class RequestIntakeRegressionTests(base.DedicatedTestDatabaseMixin, TransactionTestCase):
    """Request Intake is not a module: the production composition (with its gate) runs its task exactly as before, whatever the module state says."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        worker.install_composition(task_registry.COMPOSITION)
        self.user = get_user_model().objects.create_user(username="requester")

    def test_the_extraction_task_runs_with_no_module_state_at_all(self):
        result = submit_request(text=VALID_TEXT, submitted_by=self.user)
        self.assertEqual(ModuleState.objects.count(), 0)
        outcome = worker.process_one(worker_id="w-intake", lease_seconds=60)
        self.assertEqual(outcome.status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(outcome.kind, "request_intake.extract")
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).state, "needs_review")

    def test_the_extraction_task_runs_even_when_every_unrelated_module_is_disabled(self):
        synced()
        for module_id in (LEAF, SOLO, BASE):
            host.disable_module(REG, module_id, now=at(1))
        result = submit_request(text=VALID_TEXT, submitted_by=self.user)
        self.assertEqual(worker.process_one(worker_id="w-intake", lease_seconds=60).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).state, "needs_review")

    def test_the_test_handler_task_still_runs_through_the_production_composition(self):
        task = task_services.enqueue_task(kind="test.add_numbers", payload={"values": [2, 3]}).task
        self.assertEqual(worker.process_one(worker_id="w", lease_seconds=60).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(Task.objects.get(pk=task.pk).result, {"count": 2, "sum": 5})

    def test_the_extraction_task_runs_when_the_state_rows_are_gone_but_the_marker_exists(self):
        synced()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate")
        result = submit_request(text=VALID_TEXT, submitted_by=self.user)
        self.assertEqual(worker.process_one(worker_id="w-intake", lease_seconds=60).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).state, "needs_review")

    def test_the_production_gate_reads_no_module_state(self):
        synced()
        with mock.patch.object(gate, "read_host_state", side_effect=AssertionError("must not read")):
            self.assertEqual(task_registry.COMPOSITION.kind_gate(), task_registry.LEGACY_UNGATED_KINDS)
