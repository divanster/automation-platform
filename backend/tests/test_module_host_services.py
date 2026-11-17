"""DB-free tests for MOD-1b S4: the module host services (``apps.module_host.services``).

``SimpleTestCase`` refuses every database query, so a test that passes here never touched a database. The models, the row lock and the transaction are
replaced by recording stand-ins: these tests pin WHAT the services decide, write and audit, and in what order. What PostgreSQL does with the locks, the
constraints, concurrency and rollback is in tests/test_module_host_postgres.py and is NOT run in the development VM.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase

from apps.audit import events
from apps.module_host import rules, services
from apps.module_host.errors import ModuleHostError
from apps.modules.manifest import TaskHandlerDeclaration
from apps.modules.registry import ModuleRegistry
from tests.module_support import FakeHandler, dep, named

NOW = datetime(2031, 5, 1, 9, 30, tzinfo=timezone.utc)
EARLIER = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)


def mod(module_id, *, kinds=(), deps=(), version="1.0.0"):
    handlers = tuple(TaskHandlerDeclaration(k, FakeHandler(k), "does work") for k in kinds)
    return named(module_id, module_version=version, task_handlers=handlers, dependencies=tuple(dep(d) for d in deps))


REGISTRY = ModuleRegistry.build(
    [
        mod("base_mod", kinds=("base_mod.work",)),
        mod("leaf_mod", kinds=("leaf_mod.work",), deps=("base_mod",)),
        mod("alone_mod", kinds=("alone_mod.work",)),
    ]
)


class Row:
    """A stand-in for a ModuleState row that records its saves."""

    def __init__(self, module_id, *, enabled=True, version="1.0.0", fingerprint=None, log=None):
        self.pk = uuid.uuid4()
        self.module_id = module_id
        self.installed_version = version
        self.enabled = enabled
        self.configuration_version = None
        self.registry_fingerprint = REGISTRY.fingerprint if fingerprint is None else fingerprint
        self.created_at = self.updated_at = self.enabled_changed_at = EARLIER
        self.saves = []
        self._log = log

    def save(self, update_fields=None):
        self.saves.append(tuple(update_fields))
        if self._log is not None:
            self._log.append(("save", self.module_id, tuple(update_fields)))


class Harness:
    """Patches the transaction, the row lock, the models and the recorder; records everything in order."""

    def __init__(self, test, rows=(), claims=None, marker=None):
        # ``marker``: whether the initialization marker exists. None means a CONSISTENT database: it exists exactly when there are rows (a synced installation).
        self.marker = bool(rows) if marker is None else marker
        self.markers_created = []
        self.log = []
        self.events = []
        self.created = []
        self.claims_created = []
        self.rows = list(rows)
        for row in self.rows:
            row._log = self.log
        self.claims = {} if claims is None else dict(claims)
        self.cursor = mock.MagicMock()
        self.cursor.__enter__.return_value = self.cursor

        @contextmanager
        def atomic():
            self.log.append("begin")
            yield
            self.log.append("commit")  # an exception skips this line: the real atomic would roll back

        def create_row(**values):
            row = Row(values["module_id"], enabled=values["enabled"], version=values["installed_version"], fingerprint=values["registry_fingerprint"], log=self.log)
            row.created_at, row.updated_at, row.enabled_changed_at = values["created_at"], values["updated_at"], values["enabled_changed_at"]
            self.created.append(row)
            self.log.append(("create", row.module_id, row.enabled))
            return row

        def create_claim(**values):
            self.claims_created.append((values["namespace"], values["module_id"], values["claimed_at"]))
            self.log.append(("claim", values["namespace"]))

        def record(event):
            self.events.append(event)
            self.log.append(("event", event.action, event.details["module_id"]))

        state = mock.MagicMock()
        state.objects.create.side_effect = create_row
        state.objects.all.side_effect = lambda: list(self.rows)
        def create_marker(**values):
            self.markers_created.append(values)
            self.marker = True
            self.log.append("marker")

        marker_model = mock.MagicMock()
        marker_model.objects.exists.side_effect = lambda: self.marker
        marker_model.objects.create.side_effect = create_marker
        claim = mock.MagicMock()
        claim.objects.values_list.side_effect = lambda *a: list(self.claims.items())
        claim.objects.create.side_effect = create_claim
        patches = [
            mock.patch.object(services, "transaction", SimpleNamespace(atomic=atomic)),
            mock.patch.object(services, "connection", SimpleNamespace(cursor=lambda: self.cursor)),
            mock.patch.object(services, "_lock_rows", side_effect=lambda: (self.log.append("lock"), list(self.rows))[1]),
            mock.patch.object(services, "ModuleState", state),
            mock.patch.object(services, "ModuleNamespaceClaim", claim),
            mock.patch.object(services, "ModuleHostMarker", marker_model),
            mock.patch.object(services, "record_event", side_effect=record),
        ]
        for patcher in patches:
            patcher.start()
            test.addCleanup(patcher.stop)

    def row(self, module_id):
        return next(r for r in self.rows + self.created if r.module_id == module_id)


class EnableTests(SimpleTestCase):
    def test_enabling_a_disabled_module_writes_the_row_and_one_audit_event_in_one_transaction(self):
        h = Harness(self, [Row("alone_mod", enabled=False)])
        result = services.enable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertEqual((result.module_id, result.changed, result.enabled), ("alone_mod", True, True))
        row = h.row("alone_mod")
        self.assertTrue(row.enabled)
        self.assertEqual((row.enabled_changed_at, row.updated_at), (NOW, NOW))
        self.assertEqual(row.saves, [("enabled", "enabled_changed_at", "updated_at")])
        # the order inside the one transaction: lock the rows, save the change, record the event, commit
        self.assertEqual(h.log, ["begin", "lock", ("save", "alone_mod", ("enabled", "enabled_changed_at", "updated_at")), ("event", "module.enabled", "alone_mod"), "commit"])

    def test_the_event_is_a_system_event_about_the_module_with_identifiers_only(self):
        h = Harness(self, [Row("alone_mod", enabled=False)])
        services.enable_module(REGISTRY, "alone_mod", now=NOW)
        (event,) = h.events
        self.assertEqual((event.actor_kind, event.actor_public_id, event.action, event.subject_type), ("system", None, "module.enabled", "module"))
        self.assertEqual(event.subject_id, h.row("alone_mod").pk)
        self.assertEqual(event.details, {"module_id": "alone_mod", "installed_version": "1.0.0", "registry_fingerprint": REGISTRY.fingerprint, "cause": "admin_command"})
        self.assertEqual((event.case_id, event.run_id), (None, None))

    def test_enabling_an_enabled_module_is_a_no_op_without_a_write_or_an_event(self):
        h = Harness(self, [Row("alone_mod", enabled=True)])
        result = services.enable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertEqual((result.changed, result.enabled), (False, True))
        self.assertEqual(h.row("alone_mod").saves, [])
        self.assertEqual(h.events, [])
        self.assertEqual(h.row("alone_mod").enabled_changed_at, EARLIER)

    def test_an_unknown_module_is_refused_before_a_transaction_is_opened(self):
        h = Harness(self, [Row("alone_mod")])
        with self.assertRaises(ModuleHostError) as caught:
            services.enable_module(REGISTRY, "ghost_mod", now=NOW)
        self.assertEqual(caught.exception.code, "unknown_module")
        self.assertEqual(h.log, [])

    def test_a_missing_state_row_is_refused(self):
        h = Harness(self, [])
        with self.assertRaises(ModuleHostError) as caught:
            services.enable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertEqual(caught.exception.code, "state_missing")
        self.assertEqual(h.events, [])
        self.assertNotIn("commit", h.log)

    def test_a_version_mismatch_is_refused_even_for_a_module_that_is_already_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                h = Harness(self, [Row("alone_mod", enabled=enabled, version="0.9.0")])
                with self.assertRaises(ModuleHostError) as caught:
                    services.enable_module(REGISTRY, "alone_mod", now=NOW)
                self.assertEqual(caught.exception.code, "version_mismatch")
                self.assertEqual((h.events, h.row("alone_mod").saves), ([], []))

    def test_a_module_with_a_disabled_dependency_is_refused_and_nothing_is_written(self):
        h = Harness(self, [Row("base_mod", enabled=False), Row("leaf_mod", enabled=False)])
        with self.assertRaises(ModuleHostError) as caught:
            services.enable_module(REGISTRY, "leaf_mod", now=NOW)
        self.assertEqual(caught.exception.code, "dependency_not_enabled")
        self.assertEqual((h.events, h.row("leaf_mod").saves, h.row("leaf_mod").enabled), ([], [], False))

    def test_enabling_the_dependency_first_then_the_dependent_works(self):
        h = Harness(self, [Row("base_mod", enabled=False), Row("leaf_mod", enabled=False)])
        services.enable_module(REGISTRY, "base_mod", now=NOW)
        services.enable_module(REGISTRY, "leaf_mod", now=NOW)
        self.assertEqual([e.details["module_id"] for e in h.events], ["base_mod", "leaf_mod"])

    def test_an_audit_failure_propagates_so_the_transaction_rolls_back(self):
        h = Harness(self, [Row("alone_mod", enabled=False)])
        with mock.patch.object(services, "record_event", side_effect=OperationalError("boom")):
            with self.assertRaises(OperationalError):
                services.enable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertNotIn("commit", h.log)  # the atomic block was left by the exception: a real one rolls the save back

    def test_a_naive_clock_and_a_foreign_registry_are_refused(self):
        Harness(self, [Row("alone_mod", enabled=False)])
        with self.assertRaises(ModuleHostError) as caught:
            services.enable_module(REGISTRY, "alone_mod", now=datetime(2031, 1, 1))
        self.assertEqual(caught.exception.code, "invalid_clock")
        for bad in (None, (), [], "registry", object()):
            with self.subTest(bad=bad), self.assertRaises(ModuleHostError) as caught:
                services.enable_module(bad, "alone_mod", now=NOW)
            self.assertEqual(caught.exception.code, "invalid_registry")

    def test_garbage_ids_are_refused(self):
        Harness(self, [])
        for bad in (None, 5, "", "Alone_Mod", "../x", "alone_mod ", b"alone_mod"):
            with self.subTest(bad=bad), self.assertRaises(ModuleHostError) as caught:
                services.enable_module(REGISTRY, bad, now=NOW)
            self.assertEqual(caught.exception.code, "invalid_module_id")


class DisableTests(SimpleTestCase):
    def test_disabling_writes_the_row_and_one_audit_event_in_one_transaction(self):
        h = Harness(self, [Row("alone_mod", enabled=True)])
        result = services.disable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertEqual((result.changed, result.enabled), (True, False))
        self.assertFalse(h.row("alone_mod").enabled)
        self.assertEqual(h.row("alone_mod").enabled_changed_at, NOW)
        self.assertEqual(h.log, ["begin", "lock", ("save", "alone_mod", ("enabled", "enabled_changed_at", "updated_at")), ("event", "module.disabled", "alone_mod"), "commit"])
        self.assertEqual(h.events[0].details["cause"], "admin_command")

    def test_disabling_a_disabled_module_is_a_no_op(self):
        h = Harness(self, [Row("alone_mod", enabled=False)])
        result = services.disable_module(REGISTRY, "alone_mod", now=NOW)
        self.assertEqual((result.changed, result.enabled), (False, False))
        self.assertEqual((h.events, h.row("alone_mod").saves), ([], []))

    def test_a_module_an_enabled_module_depends_on_cannot_be_disabled(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod")])
        with self.assertRaises(ModuleHostError) as caught:
            services.disable_module(REGISTRY, "base_mod", now=NOW)
        self.assertEqual(caught.exception.code, "dependents_enabled")
        self.assertIn("leaf_mod", str(caught.exception))
        self.assertEqual((h.events, h.row("base_mod").saves, h.row("base_mod").enabled), ([], [], True))

    def test_the_dependent_first_then_the_dependency_works(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod")])
        services.disable_module(REGISTRY, "leaf_mod", now=NOW)
        services.disable_module(REGISTRY, "base_mod", now=NOW)
        self.assertEqual([e.action for e in h.events], ["module.disabled", "module.disabled"])

    def test_unknown_missing_state_and_garbage_are_refused(self):
        h = Harness(self, [])
        for module_id, code in (("ghost_mod", "unknown_module"), ("alone_mod", "state_missing"), ("X", "invalid_module_id")):
            with self.subTest(module_id=module_id), self.assertRaises(ModuleHostError) as caught:
                services.disable_module(REGISTRY, module_id, now=NOW)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(h.events, [])

    def test_a_version_mismatch_does_not_prevent_disabling(self):
        h = Harness(self, [Row("alone_mod", enabled=True, version="3.0.0")])
        self.assertTrue(services.disable_module(REGISTRY, "alone_mod", now=NOW).changed)
        self.assertEqual(len(h.events), 1)

    def test_the_services_touch_no_task(self):
        # Disabling leaves tasks alone: this module knows no task model at all (also pinned by the architecture tests).
        self.assertFalse(hasattr(services, "Task"))
        self.assertNotIn("tasks", {name.split(".")[1] for name in vars(services) if name.startswith("apps.")})


class SyncTests(SimpleTestCase):
    def test_the_first_sync_claims_namespaces_creates_state_and_records_one_event_per_row(self):
        h = Harness(self, [])
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertTrue(report.initial)
        self.assertTrue(report.ok)
        self.assertEqual(report.created, tuple((module_id, True) for module_id in REGISTRY.module_ids))  # registration order: a dependency first
        self.assertEqual({c[0]: c[1] for c in h.claims_created}, dict(REGISTRY.namespace_owners))
        self.assertEqual([e.action for e in h.events], ["module.enabled"] * 3)
        self.assertEqual({e.details["cause"] for e in h.events}, {"sync"})
        h.cursor.execute.assert_called_once()
        self.assertIn("pg_advisory_xact_lock", h.cursor.execute.call_args.args[0])
        self.assertEqual(h.log[:2], ["begin", "lock"])  # the advisory lock comes first, then the row lock, all inside the transaction
        self.assertEqual(h.log[-1], "commit")
        self.assertEqual(h.log.count("marker"), 1)
        self.assertLess(h.log.index("marker"), len(h.log) - 1)  # written inside the transaction, before the commit
        self.assertEqual(h.markers_created, [{"initialized_at": NOW, "initial_registry_fingerprint": REGISTRY.fingerprint}])
        for row in h.created:
            self.assertEqual((row.created_at, row.enabled_changed_at, row.registry_fingerprint, row.installed_version), (NOW, NOW, REGISTRY.fingerprint, "1.0.0"))

    def test_a_second_sync_changes_nothing_and_records_nothing(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod"), Row("alone_mod")], claims=dict(REGISTRY.namespace_owners))
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertFalse(report.initial)
        self.assertEqual(h.markers_created, [])  # the marker is written once and never again
        self.assertEqual((report.created, report.refreshed, report.claims_added), ((), (), ()))
        self.assertEqual(sorted(report.unchanged), ["alone_mod", "base_mod", "leaf_mod"])
        self.assertEqual((h.events, h.created, h.claims_created), ([], [], []))
        self.assertEqual([r.saves for r in h.rows], [[], [], []])

    def test_a_previously_disabled_module_is_not_enabled_by_the_sync(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod"), Row("alone_mod", enabled=False, fingerprint="0" * 64)], claims=dict(REGISTRY.namespace_owners))
        report = services.sync_modules(REGISTRY, now=NOW)
        row = h.row("alone_mod")
        self.assertFalse(row.enabled)
        self.assertEqual(report.refreshed, ("alone_mod",))
        self.assertEqual(row.saves, [("registry_fingerprint", "updated_at")])  # never "enabled"
        self.assertEqual(row.registry_fingerprint, REGISTRY.fingerprint)
        self.assertEqual(h.events, [])

    def test_a_new_module_next_to_a_disabled_dependency_is_created_disabled_and_audited_as_such(self):
        h = Harness(self, [Row("base_mod", enabled=False)], claims={"base_mod": "base_mod"})
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertEqual(dict(report.created), {"leaf_mod": False, "alone_mod": True})
        self.assertEqual({e.details["module_id"]: e.action for e in h.events}, {"leaf_mod": "module.disabled", "alone_mod": "module.enabled"})

    def test_a_version_mismatch_is_reported_and_the_stored_version_is_kept(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod"), Row("alone_mod", version="0.5.0")], claims=dict(REGISTRY.namespace_owners))
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertFalse(report.ok)
        self.assertEqual([(m.module_id, m.installed_version, m.registered_version) for m in report.version_mismatches], [("alone_mod", "0.5.0", "1.0.0")])
        self.assertEqual(h.row("alone_mod").installed_version, "0.5.0")
        self.assertEqual(h.row("alone_mod").saves, [])

    def test_a_namespace_that_now_belongs_to_another_module_is_refused_and_nothing_changes(self):
        h = Harness(self, [], claims={"alone_mod": "someone_else"})
        with self.assertRaises(ModuleHostError) as caught:
            services.sync_modules(REGISTRY, now=NOW)
        self.assertEqual(caught.exception.code, "namespace_ownership_changed")
        self.assertEqual((h.created, h.claims_created, h.events), ([], [], []))
        self.assertNotIn("commit", h.log)

    def test_a_namespace_nobody_claims_any_more_is_refused(self):
        h = Harness(self, [Row("base_mod")], claims={"retired_mod": "retired_mod", **REGISTRY.namespace_owners})
        with self.assertRaises(ModuleHostError) as caught:
            services.sync_modules(REGISTRY, now=NOW)
        self.assertEqual(caught.exception.code, "namespace_released")
        self.assertEqual((h.created, h.claims_created, h.events), ([], [], []))

    def test_existing_claims_are_never_rewritten(self):
        h = Harness(self, [], claims={"base_mod": "base_mod"})
        services.sync_modules(REGISTRY, now=NOW)
        self.assertNotIn("base_mod", [c[0] for c in h.claims_created])
        self.assertEqual(sorted(c[0] for c in h.claims_created), ["alone_mod", "leaf_mod"])

    def test_a_state_row_without_a_registry_entry_is_reported_and_kept(self):
        # (the continuity check would normally refuse such a module first; this is the plan-level safety net for a row without a claim)
        h = Harness(self, [Row("base_mod"), Row("leaf_mod"), Row("alone_mod"), Row("ghost_mod")], claims=dict(REGISTRY.namespace_owners))
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertEqual(report.unregistered, ("ghost_mod",))
        self.assertFalse(report.ok)
        self.assertEqual(h.row("ghost_mod").saves, [])

    def test_with_no_registered_module_a_sync_creates_no_state_but_does_write_the_marker(self):
        h = Harness(self, [])
        empty = ModuleRegistry.build([])
        report = services.sync_modules(empty, now=NOW)
        self.assertEqual((report.created, report.claims_added, h.events, h.created), ((), (), [], []))
        self.assertTrue(report.ok and report.initial)
        self.assertEqual(h.markers_created, [{"initialized_at": NOW, "initial_registry_fingerprint": empty.fingerprint}])
        again = services.sync_modules(empty, now=NOW)
        self.assertFalse(again.initial)
        self.assertEqual(len(h.markers_created), 1)

    def test_a_sync_that_is_refused_writes_no_marker(self):
        h = Harness(self, [], claims={"alone_mod": "someone_else"}, marker=False)
        with self.assertRaises(ModuleHostError):
            services.sync_modules(REGISTRY, now=NOW)
        self.assertEqual((h.markers_created, h.marker), ([], False))

    def test_a_sync_that_fails_after_the_marker_was_written_leaves_no_commit(self):
        h = Harness(self, [], marker=False)
        with mock.patch.object(services, "record_event", side_effect=OperationalError("audit down")):
            with self.assertRaises(OperationalError):
                services.sync_modules(REGISTRY, now=NOW)
        self.assertNotIn("commit", h.log)  # the marker, the rows and the claims share the one transaction that did not commit

    def test_rows_without_a_marker_are_repaired_by_the_next_sync_and_count_as_initialized_meanwhile(self):
        h = Harness(self, [Row("base_mod"), Row("leaf_mod"), Row("alone_mod")], claims=dict(REGISTRY.namespace_owners), marker=False)
        status = services.get_module_status(REGISTRY)
        self.assertTrue(status.synced)  # an inconsistent database is never read as "never synced"
        report = services.sync_modules(REGISTRY, now=NOW)
        self.assertTrue(report.initial)  # the repair of a missing marker
        self.assertEqual(len(h.markers_created), 1)
        self.assertEqual((report.created, h.events), ((), []))  # nothing else changed

    def test_the_marker_survives_with_no_rows_so_a_module_added_later_fails_closed(self):
        # an empty-registry sync wrote the marker; later a registry has modules but no row exists: they must be held, not enabled-by-default
        h = Harness(self, [], marker=True)
        status = services.get_module_status(REGISTRY)
        self.assertTrue(status.synced)
        self.assertEqual({(m.claimable, m.reason) for m in status.modules}, {(False, rules.MISSING_STATE)})
        self.assertEqual(h.markers_created, [])

    def test_the_report_is_stable_json(self):
        Harness(self, [])
        document = services.sync_modules(REGISTRY, now=NOW).to_dict()
        text = json.dumps(document, sort_keys=True)
        self.assertEqual(json.loads(text), document)
        self.assertEqual(set(document), {"format", "ok", "initial_sync", "registry_fingerprint", "created", "refreshed", "unchanged", "version_mismatches", "unregistered", "namespace_claims_added"})
        self.assertEqual(document["format"], 1)

    def test_a_database_error_propagates_and_nothing_is_committed(self):
        h = Harness(self, [])
        h.cursor.execute.side_effect = OperationalError("down")
        with self.assertRaises(OperationalError):
            services.sync_modules(REGISTRY, now=NOW)
        self.assertNotIn("commit", h.log)


class StatusTests(SimpleTestCase):
    def status(self, rows):
        Harness(self, rows)
        return services.get_module_status(REGISTRY)

    def test_status_of_an_unsynced_installation_says_so_and_treats_modules_as_available(self):
        status = self.status([])
        self.assertFalse(status.synced)
        for item in status.modules:
            self.assertEqual((item.enabled, item.installed_version, item.claimable, item.reason), (None, None, True, rules.LEGACY_UNSYNCED))
            self.assertIsNone(item.registry_fingerprint_current)

    def test_status_after_the_sync_fails_closed_for_a_module_without_a_row(self):
        status = self.status([Row("base_mod"), Row("leaf_mod")])
        self.assertTrue(status.synced)
        self.assertEqual((status.module("alone_mod").claimable, status.module("alone_mod").reason), (False, rules.MISSING_STATE))

    def test_status_reports_enabled_disabled_mismatch_and_dependency_reasons(self):
        status = self.status([Row("base_mod", enabled=False), Row("leaf_mod"), Row("alone_mod", version="2.0.0")])
        self.assertEqual(status.module("base_mod").reason, rules.DISABLED)
        self.assertEqual(status.module("leaf_mod").reason, rules.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(status.module("alone_mod").reason, rules.VERSION_MISMATCH)
        self.assertEqual(status.module("alone_mod").installed_version, "2.0.0")
        self.assertEqual(status.module("alone_mod").registered_version, "1.0.0")

    def test_status_agrees_with_the_gate_rule_for_every_module(self):
        rows = [Row("base_mod"), Row("leaf_mod", enabled=False), Row("alone_mod")]
        status = self.status(rows)
        verdict = rules.assess_modules(REGISTRY, {r.module_id: rules.StateFacts(r.module_id, r.installed_version, r.enabled, r.registry_fingerprint) for r in rows}, initialized=True)
        for item in status.modules:
            self.assertEqual((item.claimable, item.reason), (verdict[item.module_id].available, verdict[item.module_id].reason))

    def test_unregistered_rows_are_listed_and_an_unknown_module_is_an_error(self):
        status = self.status([Row("base_mod"), Row("ghost_mod")])
        self.assertEqual(status.unregistered, ("ghost_mod",))
        with self.assertRaises(ModuleHostError):
            status.module("ghost_mod")

    def test_status_json_is_stable_and_free_of_free_text(self):
        status = self.status([Row("base_mod"), Row("leaf_mod", fingerprint="0" * 64), Row("alone_mod")])
        document = status.to_dict()
        self.assertEqual(json.loads(json.dumps(document, sort_keys=True)), document)
        self.assertEqual(set(document), {"format", "registry_fingerprint", "synced", "modules", "unregistered_state"})
        self.assertEqual([m["module_id"] for m in document["modules"]], list(REGISTRY.module_ids))
        self.assertEqual(
            set(document["modules"][0]),
            {"module_id", "module_kind", "registered_version", "installed_version", "enabled", "claimable", "reason", "dependencies", "task_kinds", "configuration_version", "registry_fingerprint_current", "enabled_changed_at"},
        )
        by_id = {m["module_id"]: m for m in document["modules"]}
        self.assertTrue(by_id["base_mod"]["registry_fingerprint_current"])
        self.assertFalse(by_id["leaf_mod"]["registry_fingerprint_current"])
        self.assertEqual(by_id["leaf_mod"]["dependencies"], ["base_mod"])
        self.assertEqual(by_id["alone_mod"]["task_kinds"], ["alone_mod.work"])
        self.assertEqual(by_id["alone_mod"]["enabled_changed_at"], EARLIER.isoformat())

    def test_status_reads_only_and_takes_no_lock(self):
        h = Harness(self, [Row("base_mod")])
        services.get_module_status(REGISTRY)
        self.assertEqual(h.log, [])
        self.assertEqual(h.events, [])

    def test_a_status_of_an_empty_registry(self):
        Harness(self, [])
        status = services.get_module_status(ModuleRegistry.build([]))
        self.assertEqual((status.modules, status.synced, status.unregistered), ((), False, ()))


class ReadHostStateTests(SimpleTestCase):
    """What the worker gate reads: the rows and the durable marker, read without a lock and with the rows FIRST."""

    def test_the_marker_alone_means_initialized_even_with_no_row(self):
        Harness(self, [], marker=True)
        state = services.read_host_state()
        self.assertEqual((state.facts, state.initialized), ({}, True))

    def test_no_marker_and_no_row_means_not_initialized(self):
        Harness(self, [], marker=False)
        state = services.read_host_state()
        self.assertEqual((state.facts, state.initialized), ({}, False))

    def test_rows_without_a_marker_fail_closed_as_initialized(self):
        Harness(self, [Row("base_mod")], marker=False)
        self.assertTrue(services.read_host_state().initialized)

    def test_the_rows_are_read_before_the_marker(self):
        order = []
        h = Harness(self, [Row("base_mod")], marker=True)
        services.ModuleState.objects.all.side_effect = lambda: order.append("rows") or list(h.rows)
        services.ModuleHostMarker.objects.exists.side_effect = lambda: order.append("marker") or True
        services.read_host_state()
        self.assertEqual(order, ["rows", "marker"])  # a sync committing in between can only make the answer more closed

    def test_the_state_is_read_without_a_lock_or_a_transaction(self):
        h = Harness(self, [Row("base_mod")])
        services.read_host_state()
        self.assertEqual(h.log, [])

    def test_the_gate_answer_follows_the_marker(self):
        from apps.module_host import gate

        Harness(self, [], marker=True)
        self.assertEqual(gate.make_kind_gate(REGISTRY, {"test.add_numbers"})(), frozenset({"test.add_numbers"}))  # held: the window stays closed
        Harness(self, [], marker=False)
        self.assertEqual(gate.make_kind_gate(REGISTRY, {"test.add_numbers"})(), frozenset({"test.add_numbers", "base_mod.work", "leaf_mod.work", "alone_mod.work"}))


class AuditVocabularyTests(SimpleTestCase):
    def test_the_two_actions_are_core_actions_and_nothing_had_to_be_installed(self):
        self.assertIsNone(events._installed, "a previous test leaked an installed audit vocabulary")
        vocabulary = events.current_vocabulary()
        self.assertIs(vocabulary, events.CORE_VOCABULARY)
        for action in ("module.enabled", "module.disabled"):
            self.assertTrue(vocabulary.allows_action(action))
            self.assertIsNone(vocabulary.owner_of(action))  # Core's, not a module's
        self.assertTrue(vocabulary.allows_subject_type("module"))
        self.assertEqual(vocabulary.module_actions, {})

    def test_a_module_cannot_declare_the_module_namespace(self):
        # "module" is reserved for Core: this is why the two actions are Core's own.
        from apps.modules.errors import ManifestError
        from apps.modules.manifest import AuditActionDeclaration

        with self.assertRaises(ManifestError):
            named("demo_module", audit_actions=(AuditActionDeclaration("module.enabled", ()),))

    def test_the_event_details_hold_no_free_text_and_unknown_keys_are_refused(self):
        events.system_event("module.enabled", "module", uuid.uuid4(), module_id="alone_mod", installed_version="1.0.0", registry_fingerprint="a" * 64, cause="admin_command")
        for bad in ({"note": "x"}, {"name": "x"}, {"module_id": "has a space"}):
            with self.subTest(bad=bad), self.assertRaises(events.AuditEventError):
                events.system_event("module.enabled", "module", uuid.uuid4(), **bad)

    def test_the_vocabulary_keys_are_exactly_the_ones_the_services_write(self):
        self.assertEqual(events.DETAIL_KEYS["module.enabled"], frozenset({"module_id", "installed_version", "registry_fingerprint", "cause"}))
        self.assertEqual(events.DETAIL_KEYS["module.disabled"], events.DETAIL_KEYS["module.enabled"])
