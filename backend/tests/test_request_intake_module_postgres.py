"""Tests for Request Intake as a registered module that REQUIRE a real PostgreSQL server (MOD-2).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md,
section 5.4). When they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; the mixin they reuse refuses any other name.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project owner runs them locally they are NOT RUN.
Their DB-free counterparts are tests/test_request_intake_module.py and the edited module host and gate tests (those pass in the VM).

What they cover, with the PRODUCTION registry (``config.task_registry.MODULE_REGISTRY``, Request Intake only) and the PRODUCTION worker composition:
``module_sync`` creating Request Intake's state and namespace claims (also after an earlier sync with an empty registry left the initialization marker
behind); the worker holding its tasks (never failing them, never claiming and discarding) while the module is disabled, version-mismatched or has no state;
re-enabling resuming them in order; a running attempt finishing after a disable; an expired lease recovered and then held; retry unchanged; historical run
attribution untouched; the role groups ``setup_roles`` writes and the profile permissions equal to the manifest's derivation; the ModuleContext decision equal to
Request Intake's own authorization for every user and owner; and one complete submit, extract, edit, approve flow with the module enabled. The existing suites
(test_intake_postgres, test_review_postgres, test_api_postgres, test_tasks_*_postgres, test_module_host_postgres, test_module_context_postgres,
test_workflow_attribution_postgres, test_audit_vocabulary_postgres) must still pass.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_request_intake_module_postgres --tag requires_postgres

This file is ASCII only.
"""

import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, tag
from django.utils import timezone

from apps.accounts.errors import AuthorizationError
from apps.accounts.profile import current_user_profile
from apps.accounts.roles import DOCUMENT_PERMISSIONS, DOCUMENT_ROLE_PERMISSIONS, ROLES
from apps.audit.models import AuditEvent
from apps.cases.models import WorkflowRun
from apps.intake import authorization as intake_authorization
from apps.intake import services as intake_services
from apps.intake.access import Action
from apps.intake.handlers import EXTRACT_HANDLER
from apps.intake.models import ExtractionOutcome
from apps.intake.module import MANIFEST
from apps.intake.policy import KIND_EXTRACT
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.submission import REQUEST_INTAKE_ATTRIBUTION
from apps.module_host import gate
from apps.module_host import services as host
from apps.module_host.models import ModuleHostMarker, ModuleNamespaceClaim, ModuleState
from apps.modules.roles import derive_role_permissions
from apps.tasks import worker
from apps.tasks.handlers import TaskHandler
from apps.tasks.models import Task, TaskAttempt
from apps.tasks.services import claim_next_task
from config.audit_vocabulary import build_audit_vocabulary
from config.module_context import module_context
from config.modules import build_registry
from config.task_registry import COMPOSITION, MODULE_REGISTRY, UNGATED_KINDS
from tests import test_review_postgres as review_base
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, make_user, requires_postgres, submit

MODULE_ID = "request_intake"
EMPTY_REGISTRY = build_registry(())  # what a sync before MOD-2 saw: no module at all


def state_row():
    return ModuleState.objects.get(module_id=MODULE_ID)


def task_untouched(task):
    row = Task.objects.get(pk=task.pk)
    return (row.state, row.attempt_count, row.lease_token, TaskAttempt.objects.filter(task=row).count(), row.failure_code) == ("queued", 0, None, 0, "")


def run_state(result):
    return WorkflowRun.objects.get(pk=result.run.pk).state


# --- module_sync ----------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class SyncTests(DedicatedTestDatabaseMixin, TestCase):
    def test_the_first_sync_creates_enabled_state_with_the_manifest_version_and_the_registry_fingerprint(self):
        self.assertEqual((ModuleState.objects.count(), ModuleHostMarker.objects.count()), (0, 0))
        report = host.sync_modules(MODULE_REGISTRY)
        self.assertEqual((report.initial, report.created, report.ok), (True, ((MODULE_ID, True),), True))
        row = state_row()
        self.assertEqual((row.installed_version, row.enabled, row.registry_fingerprint), ("1.0.0", True, MODULE_REGISTRY.fingerprint))
        self.assertEqual(ModuleHostMarker.objects.count(), 1)

    def test_the_namespace_claims_for_both_legacy_and_module_namespaces_are_recorded(self):
        host.sync_modules(MODULE_REGISTRY)
        self.assertEqual(dict(ModuleNamespaceClaim.objects.values_list("namespace", "module_id")), {"intake": MODULE_ID, "request_intake": MODULE_ID})

    def test_a_second_sync_changes_nothing(self):
        host.sync_modules(MODULE_REGISTRY)
        before = (list(ModuleState.objects.values()), list(ModuleNamespaceClaim.objects.values()), AuditEvent.objects.count())
        report = host.sync_modules(MODULE_REGISTRY)
        self.assertEqual((report.created, report.unchanged, report.claims_added, report.ok), ((), (MODULE_ID,), (), True))
        self.assertEqual((list(ModuleState.objects.values()), list(ModuleNamespaceClaim.objects.values()), AuditEvent.objects.count()), before)

    def test_the_sync_records_one_module_enabled_event_with_the_system_actor(self):
        host.sync_modules(MODULE_REGISTRY)
        events = list(AuditEvent.objects.filter(subject_type="module"))
        self.assertEqual([(e.action, e.details["module_id"], e.details["cause"], e.actor_kind) for e in events], [("module.enabled", MODULE_ID, "sync", "system")])

    def test_a_sync_after_an_earlier_empty_registry_sync_creates_the_state_the_marker_alone_does_not_enable_it(self):
        # the development database may already hold the marker from a sync that ran while no module was registered
        host.sync_modules(EMPTY_REGISTRY)
        self.assertEqual((ModuleState.objects.count(), ModuleHostMarker.objects.count()), (0, 1))
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS)  # missing state after initialization fails closed: not "enabled"
        report = host.sync_modules(MODULE_REGISTRY)
        self.assertEqual((report.initial, report.created), (False, ((MODULE_ID, True),)))
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS | {KIND_EXTRACT})

    def test_a_disabled_request_intake_is_never_re_enabled_by_a_sync(self):
        host.sync_modules(MODULE_REGISTRY)
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        report = host.sync_modules(MODULE_REGISTRY)
        self.assertEqual((report.created, report.unchanged), ((), (MODULE_ID,)))
        self.assertFalse(state_row().enabled)
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS)

    def test_a_version_mismatch_is_reported_and_holds_the_kind(self):
        host.sync_modules(MODULE_REGISTRY)
        ModuleState.objects.filter(module_id=MODULE_ID).update(installed_version="0.9.0")
        report = host.sync_modules(MODULE_REGISTRY)
        self.assertEqual((report.ok, [m.module_id for m in report.version_mismatches]), (False, [MODULE_ID]))
        self.assertEqual(state_row().installed_version, "0.9.0")  # never changed by a sync
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS)

    def test_no_state_row_without_the_marker_is_the_pre_sync_window_and_the_kind_is_claimable(self):
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS | {KIND_EXTRACT})  # an installation that has not synced yet keeps working

    def test_the_audit_vocabulary_composed_from_the_registry_is_cores_and_historical_events_stay_readable(self):
        user = make_user("historic")
        submit(user)
        self.assertEqual(build_audit_vocabulary(MODULE_REGISTRY).actions, build_audit_vocabulary(EMPTY_REGISTRY).actions)
        self.assertEqual(list(AuditEvent.objects.values_list("action", flat=True)), ["request.submitted"])
        host.sync_modules(MODULE_REGISTRY)
        self.assertIn("request.submitted", list(AuditEvent.objects.values_list("action", flat=True)))

    def test_historical_run_attribution_is_untouched_by_the_registration_and_the_sync(self):
        user = make_user("historic")
        result = submit(user)
        before = list(WorkflowRun.objects.values())
        host.sync_modules(MODULE_REGISTRY)
        self.assertEqual(list(WorkflowRun.objects.values()), before)
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).attribution, REQUEST_INTAKE_ATTRIBUTION)


# --- the worker -----------------------------------------------------------------------------------------------------------------


class WorkerCase(DedicatedTestDatabaseMixin, TransactionTestCase):
    """The production composition (handler allowlist, reconciler and the module gate) on committed data. The worker refuses to run a handler inside a transaction."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, worker, "_composition", worker._composition)
        worker.install_composition(COMPOSITION)
        self.user = make_user("requester")

    def process(self, **kwargs):
        return worker.process_one(worker_id="w-intake", lease_seconds=60, **kwargs)

    def sync(self):
        host.sync_modules(MODULE_REGISTRY)


@tag("requires_postgres")
@requires_postgres
class GatedWorkerTests(WorkerCase):
    def test_the_production_composition_is_the_one_with_the_real_handler_and_the_module_gate(self):
        self.assertIs(COMPOSITION.registry[KIND_EXTRACT], EXTRACT_HANDLER)  # DOC-2 adds Core's text extraction handler beside it
        self.assertIs(PRODUCTION_REGISTRY[KIND_EXTRACT], EXTRACT_HANDLER)
        self.assertIsNotNone(COMPOSITION.kind_gate)

    def test_an_enabled_module_runs_its_task_to_needs_review_exactly_as_before(self):
        self.sync()
        result = submit(self.user)
        outcome = self.process()
        self.assertEqual((outcome.status, outcome.kind), (worker.ProcessStatus.SUCCEEDED, KIND_EXTRACT))
        self.assertEqual(run_state(result), "needs_review")
        task = Task.objects.get(pk=result.task.pk)
        self.assertEqual((task.state, task.attempt_count), ("succeeded", 1))
        self.assertEqual(task.payload, {"run_id": str(result.run.pk)})  # still no text
        self.assertEqual(ExtractionOutcome.objects.filter(run=result.run).count(), 1)

    def test_before_any_sync_the_task_still_runs(self):
        result = submit(self.user)
        self.assertEqual(ModuleState.objects.count(), 0)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(run_state(result), "needs_review")

    def test_after_an_empty_registry_sync_the_task_is_held_until_the_production_sync(self):
        host.sync_modules(EMPTY_REGISTRY)
        result = submit(self.user)
        for _ in range(3):
            self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(task_untouched(result.task))  # queued, no attempt, no failure: nothing was claimed and discarded
        self.assertEqual(run_state(result), "queued")
        self.sync()
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(run_state(result), "needs_review")

    def test_a_disabled_modules_task_is_held_not_failed_and_runs_after_the_enable(self):
        self.sync()
        result = submit(self.user)
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        for _ in range(3):
            self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(task_untouched(result.task))
        self.assertEqual(run_state(result), "queued")  # the reconciler does not fail a run whose task is merely held
        host.enable_module(MODULE_REGISTRY, MODULE_ID)
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=result.task.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("succeeded", 1))
        self.assertEqual(list(TaskAttempt.objects.filter(task=stored).values_list("attempt_number", "outcome")), [(1, "succeeded")])
        self.assertEqual(run_state(result), "needs_review")

    def test_the_original_order_is_kept_when_held_tasks_are_released(self):
        self.sync()
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        first = submit(self.user, VALID_TEXT)
        second = submit(self.user, VALID_TEXT + "\nSecond request.")
        self.process()
        host.enable_module(MODULE_REGISTRY, MODULE_ID)
        claimed = [self.process().task_id, self.process().task_id]
        self.assertEqual(claimed, [first.task.pk, second.task.pk])

    def test_the_test_handler_kind_is_unaffected_by_the_module_state(self):
        from apps.tasks.services import enqueue_task

        self.sync()
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        task = enqueue_task(kind="test.add_numbers", payload={"values": [2, 3]}).task
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual(Task.objects.get(pk=task.pk).result, {"count": 2, "sum": 5})

    def test_a_missing_state_row_after_the_sync_fails_closed(self):
        self.sync()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM module_host_modulestate")  # raw SQL: the ORM refuses to delete state rows; only a hand edit can do this. The marker stays.
        self.assertEqual((ModuleState.objects.count(), ModuleHostMarker.objects.count()), (0, 1))  # the row is really gone and the initialization marker is intact
        result = submit(self.user)
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(task_untouched(result.task))

    def test_a_running_attempt_finishes_after_the_module_is_disabled_and_the_next_task_is_held(self):
        self.sync()

        def run_and_get_disabled(payload):
            host.disable_module(MODULE_REGISTRY, MODULE_ID)  # disabled while its own attempt is running
            return EXTRACT_HANDLER.run(payload)

        handler = TaskHandler(kind=KIND_EXTRACT, validate=EXTRACT_HANDLER.validate, run=run_and_get_disabled, is_test_handler=False)
        worker.install_composition(worker.Composition(registry={**PRODUCTION_REGISTRY, KIND_EXTRACT: handler}, after_iteration=COMPOSITION.after_iteration, kind_gate=COMPOSITION.kind_gate))
        running = submit(self.user)
        behind = submit(self.user, VALID_TEXT + "\nSecond request.")
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((Task.objects.get(pk=running.task.pk).state, run_state(running)), ("succeeded", "needs_review"))
        self.assertFalse(state_row().enabled)
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertTrue(task_untouched(behind.task))

    def test_an_expired_lease_of_the_disabled_module_is_recovered_then_held_and_runs_after_the_enable(self):
        self.sync()
        result = submit(self.user)
        claimed = claim_next_task(worker_id="crashed", lease_seconds=5)  # an unrestricted claim, then the process "dies"
        self.assertEqual(claimed.task_id, result.task.pk)
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        later = timezone.now() + timedelta(seconds=3600)
        self.assertEqual(self.process(clock=lambda: later).status, worker.ProcessStatus.IDLE)  # recovery ran; the claim is held
        stored = Task.objects.get(pk=result.task.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("queued", 1))
        self.assertEqual(list(TaskAttempt.objects.filter(task=stored).values_list("outcome", "failure_code")), [("abandoned", "lease_expired")])
        host.enable_module(MODULE_REGISTRY, MODULE_ID)
        self.assertEqual(self.process(clock=lambda: later + timedelta(seconds=3600)).status, worker.ProcessStatus.SUCCEEDED)
        stored = Task.objects.get(pk=result.task.pk)
        self.assertEqual((stored.state, stored.attempt_count), ("succeeded", 2))
        self.assertEqual(run_state(result), "needs_review")

    def test_retry_behaviour_is_unchanged_and_a_disable_between_attempts_holds_the_retry(self):
        self.sync()
        result = submit(self.user)
        with mock.patch.object(intake_services, "build_extraction", side_effect=RuntimeError("provider down")):
            outcome = self.process()
        self.assertEqual((outcome.status, outcome.failure_code), (worker.ProcessStatus.REQUEUED, "handler_error"))
        self.assertEqual(Task.objects.get(pk=result.task.pk).state, "queued")
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        later = timezone.now() + timedelta(seconds=3600)
        self.assertEqual(self.process(clock=lambda: later).status, worker.ProcessStatus.IDLE)
        self.assertEqual(Task.objects.get(pk=result.task.pk).attempt_count, 1)  # no attempt was started while held
        host.enable_module(MODULE_REGISTRY, MODULE_ID)
        self.assertEqual(self.process(clock=lambda: later).status, worker.ProcessStatus.SUCCEEDED)
        self.assertEqual((Task.objects.get(pk=result.task.pk).attempt_count, run_state(result)), (2, "needs_review"))

    def test_a_permanently_failed_task_still_moves_its_run_to_failed_whatever_the_module_state(self):
        self.sync()
        result = submit(self.user)
        host.disable_module(MODULE_REGISTRY, MODULE_ID)  # the reconciler is the worker's after-iteration hook: it keeps running for a disabled module
        claimed = claim_next_task(worker_id="w", lease_seconds=60)
        from apps.tasks.policy import Failure, FailureCode
        from apps.tasks.services import fail_task

        fail_task(task_id=claimed.task_id, lease_token=claimed.lease_token, failure=Failure(FailureCode.REJECTED, retryable=False))
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        run = WorkflowRun.objects.get(pk=result.run.pk)
        self.assertEqual((run.state, run.failed_step), ("failed", "extract"))

    def test_the_gate_reads_the_state_afresh_on_every_iteration_without_a_restart(self):
        self.sync()
        first = submit(self.user)
        second = submit(self.user, VALID_TEXT + "\nSecond request.")
        self.assertEqual(self.process().status, worker.ProcessStatus.SUCCEEDED)
        host.disable_module(MODULE_REGISTRY, MODULE_ID)
        self.assertEqual(self.process().status, worker.ProcessStatus.IDLE)
        self.assertEqual((Task.objects.get(pk=first.task.pk).state, Task.objects.get(pk=second.task.pk).state), ("succeeded", "queued"))

    def test_a_database_error_from_the_state_read_claims_nothing(self):
        from django.db import OperationalError

        self.sync()
        result = submit(self.user)
        with mock.patch.object(gate, "read_host_state", side_effect=OperationalError("down")):
            with self.assertRaises(OperationalError):
                self.process()
        self.assertTrue(task_untouched(result.task))


# --- permissions ----------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class RolesAndProfileTests(DedicatedTestDatabaseMixin, TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=io.StringIO())
        self.derived = derive_role_permissions(MODULE_REGISTRY)

    def stored(self, role):
        group = Group.objects.get(name=role)
        return {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.select_related("content_type")}

    def test_the_groups_setup_roles_writes_equal_the_manifests_derivation_exactly(self):
        # The Request Intake part is compared with the manifest derivation EXACTLY; Core's document permissions (DOC-1) are a separate matrix and are checked on their own;
        # nothing else may be present.
        for role in ROLES:
            with self.subTest(role=role):
                stored = self.stored(role)
                documents = {name for name in stored if name.startswith("documents.")}
                self.assertFalse(set(self.derived[role]) & DOCUMENT_PERMISSIONS)  # no module grants a Core document permission
                self.assertEqual(stored - documents, set(self.derived[role]))
                self.assertEqual(documents, set(DOCUMENT_ROLE_PERMISSIONS[role]))
                self.assertEqual(stored, set(self.derived[role]) | set(DOCUMENT_ROLE_PERMISSIONS[role]))

    def test_the_profile_of_a_user_in_each_role_reports_exactly_the_derived_permissions(self):
        for role in ROLES:
            with self.subTest(role=role):
                user = make_user(f"user-{role.lower()}")
                user.groups.add(Group.objects.get(name=role))
                profile = current_user_profile(user)
                self.assertEqual((profile.roles, profile.permissions), ((role,), tuple(sorted(self.derived[role]))))

    def test_a_user_with_no_role_and_a_superuser_report_what_they_always_did(self):
        nobody = make_user("nobody")
        self.assertEqual(current_user_profile(nobody).permissions, ())
        root = make_user("root")
        type(root).objects.filter(pk=root.pk).update(is_superuser=True)
        root.refresh_from_db()
        every = {p.permission_id for p in MANIFEST.permissions} | {"audit.view_auditevent"}
        self.assertEqual(set(current_user_profile(root).permissions), every)

    def test_registering_the_module_and_syncing_changes_no_group(self):
        before = {role: self.stored(role) for role in ROLES}
        host.sync_modules(MODULE_REGISTRY)
        self.assertEqual({role: self.stored(role) for role in ROLES}, before)


@tag("requires_postgres")
@requires_postgres
class ModuleContextAuthorizationTests(review_base.ReviewCase):
    """The registered module's ModuleContext decides exactly as Request Intake's own authorization, for every user and owner."""

    ACTION_PERMISSIONS = {
        Action.SUBMIT: ("intake.submit_request",),
        Action.VIEW: ("intake.view_own_request", "intake.view_any_request"),
        Action.EDIT: ("intake.edit_own_request",),
        Action.APPROVE: ("intake.approve_request",),
        Action.REJECT: ("intake.reject_request",),
    }

    def reason_of(self, call):
        try:
            call()
        except AuthorizationError as error:
            return error.reason
        return None

    def test_the_context_equals_the_intake_authorization_for_every_user_action_and_owner(self):
        permissions = module_context(MODULE_ID).permissions
        users = (self.operator, self.other_operator, self.approver, self.auditor, self.nobody, self.superuser, self.hybrid)
        for user in users:
            for action, ids in self.ACTION_PERMISSIONS.items():
                for owner in (None, self.operator.pk, self.other_operator.pk, self.hybrid.pk):
                    with self.subTest(user=user.username, action=action.value, owner=owner):
                        via_context = self.reason_of(lambda: permissions.authorize(user, *ids, owner_pk=owner))
                        via_intake = self.reason_of(lambda: intake_authorization.authorize(user, action, submitter_pk=owner))
                        self.assertEqual(via_context, via_intake)


# --- one complete flow with the module enabled ---------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class EndToEndTests(review_base.ReviewCase):
    def test_submit_extract_edit_and_approve_with_the_module_synced_and_enabled(self):
        host.sync_modules(MODULE_REGISTRY)
        run = self.new_run()  # submit and extract
        self.assertEqual((run.state, run.attribution), ("needs_review", REQUEST_INTAKE_ATTRIBUTION))
        self.edit(self.operator, run)
        self.assertEqual(self.approve(self.approver, run).run_state, "approved")
        actions = list(AuditEvent.objects.filter(run_id=run.pk).order_by("id").values_list("action", flat=True))
        for expected in ("request.submitted", "revision.created", "request.edited", "approval.recorded"):
            self.assertIn(expected, actions)
