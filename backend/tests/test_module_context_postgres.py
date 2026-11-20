"""Tests for the ModuleContext that REQUIRE a real PostgreSQL server (MOD-1b S6).

Excluded from the default run and skipped unless ``AP_RUN_DB_TESTS=1`` is set explicitly (see test_postgres_required.py and docs/development/runbook.md). When
they run, Django creates and later destroys a separate ``test_<AP_DB_NAME>`` database; every class refuses to run against anything else.

STATUS: written on a development VM that has no PostgreSQL and NEVER RUN there or anywhere. Until the project owner runs them locally they are NOT RUN, and nothing
about PostgreSQL behaviour is proven by this file. Their DB-free counterparts are tests/test_module_context_rules.py and tests/test_module_context.py (these pass
in the VM, with the database replaced by recording stand-ins).

What they cover: a real task enqueued through the context (queue semantics, idempotency and the claim unchanged); a real run created with its attribution and moved
through the state machine, and another module's run and a Request Intake run refused and left alone; audit events recorded inside, and rolled back with, the caller's
transaction, and the ORM immutability of events untouched; permission checks with real Django users and the real roles, compared with Request Intake's own
authorization; module enable, disable, version mismatch, missing state and the pre-sync transition enforced against real rows and a real shared row lock; concurrent
operations (one idempotency key, parallel runs, a disable that waits for an in-flight call, calls that do not block each other); and the legacy Request Intake path
unchanged. The existing suites (test_module_host_postgres, test_tasks_postgres, test_intake_postgres, test_review_postgres, test_api_postgres,
test_audit_vocabulary_postgres, test_authz_core_postgres, test_cases_persistence_postgres) must still pass UNCHANGED.

Run (from backend\\, after the runbook's database setup):
    python manage.py test tests.test_module_context_postgres --tag requires_postgres -v 2

This file is ASCII only.
"""

import threading
import uuid
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext

from apps.accounts.errors import AuthorizationError
from apps.audit import events
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditTransactionError
from apps.cases import services as case_services
from apps.cases.attribution import WorkflowAttribution
from apps.cases.errors import ImmutableRecordError, InvalidTransitionError
from apps.cases.models import Case, WorkflowRun
from apps.cases.run_state import RunState
from apps.intake import authorization as intake_authorization
from apps.intake.access import Action
from apps.intake.submission import REQUEST_INTAKE_ATTRIBUTION, submit_request
from apps.module_host import services as host
from apps.module_host.context import SYSTEM_ACTOR, ModuleContext
from apps.module_host.errors import ModuleContextError, ModuleHostError
from apps.module_host.models import ModuleHostMarker, ModuleNamespaceClaim, ModuleState
from apps.tasks.handlers import TaskHandler
from apps.tasks.models import Task
from apps.tasks.policy import InvalidPayloadError
from apps.tasks.services import claim_next_task
from config.audit_vocabulary import build_audit_vocabulary
from config.task_registry import COMPOSITION
from tests import module_context_support as support
from tests import test_review_postgres as review_base
from tests.intake_support import VALID_TEXT
from tests.test_intake_postgres import DedicatedTestDatabaseMixin, make_user, requires_postgres, run_threads, submit

CTX, OTHER, KIND, OTHER_KIND, FLOW, ACTION = support.CTX, support.OTHER, support.KIND, support.OTHER_KIND, support.FLOW, support.ACTION


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) - {"n"}:
        raise InvalidPayloadError("payload must be an object with an optional n")
    return dict(payload)


HANDLERS = {
    KIND: TaskHandler(kind=KIND, validate=_validate, run=lambda payload: {}, is_test_handler=False),
    OTHER_KIND: TaskHandler(kind=OTHER_KIND, validate=_validate, run=lambda payload: {}, is_test_handler=False),
}


def raw(sql, params=()):
    """A statement the application never runs: only to put the test database into a state the services refuse to produce."""
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


class ContextDbCase(DedicatedTestDatabaseMixin, TestCase):
    """A synced registry of two synthetic modules (state rows, enabled), the audit vocabulary that registry composes, and a context for each."""

    registry_modules = None

    def setUp(self):
        super().setUp()
        self.registry = support.registry(*(self.registry_modules or ()))
        host.sync_modules(self.registry)
        patcher = mock.patch.object(events, "_installed", build_audit_vocabulary(self.registry))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = ModuleContext.open(self.registry, CTX, handler_registry=HANDLERS)
        self.other = ModuleContext.open(self.registry, OTHER, handler_registry=HANDLERS)

    def refused(self, code, function, *args, **kwargs):
        with self.assertRaises(ModuleContextError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def make_unavailable(self, reason):
        if reason == "disabled":
            host.disable_module(self.registry, CTX)
        elif reason == "version_mismatch":
            ModuleState.objects.filter(module_id=CTX).update(installed_version="0.0.1")
        elif reason == "missing_state":
            raw("DELETE FROM module_host_modulestate WHERE module_id = %s", [CTX])  # the marker stays: the first sync has completed
        else:
            raise AssertionError(reason)


UNAVAILABLE = (("disabled", "module_disabled"), ("version_mismatch", "module_version_mismatch"), ("missing_state", "module_state_missing"))


# --- task.enqueue -------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class TaskEnqueueTests(ContextDbCase):
    def test_a_task_is_enqueued_through_the_existing_service(self):
        result = self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:1")
        self.assertTrue(result.created)
        task = Task.objects.get(pk=result.task.pk)
        self.assertEqual((task.kind, task.payload, task.idempotency_key, task.state, task.attempt_count), (KIND, {"n": 1}, "ctx_mod.work:1", "queued", 0))

    def test_idempotency_is_the_queues_own(self):
        first = self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:1")
        again = self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:1")
        self.assertEqual((first.created, again.created, again.task.pk), (True, False, first.task.pk))
        from apps.tasks.errors import IdempotencyConflictError

        with self.assertRaises(IdempotencyConflictError):
            self.ctx.tasks.enqueue(kind=KIND, payload={"n": 2}, idempotency_key="ctx_mod.work:1")
        self.assertEqual(Task.objects.count(), 1)

    def test_a_task_without_a_key_is_allowed_and_not_deduplicated(self):
        self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1})
        self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1})
        self.assertEqual(Task.objects.count(), 2)

    def test_the_handler_still_validates_the_payload(self):
        with self.assertRaises(InvalidPayloadError):
            self.ctx.tasks.enqueue(kind=KIND, payload={"unexpected": 1})
        self.assertEqual(Task.objects.count(), 0)

    def test_the_enqueued_task_is_claimed_as_any_other(self):
        self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:7")
        claimed = claim_next_task(worker_id="w1", lease_seconds=60, allowed_kinds={KIND})
        self.assertEqual((claimed.kind, claimed.attempt_number, claimed.payload), (KIND, 1, {"n": 1}))
        self.assertEqual(Task.objects.get(pk=claimed.task_id).state, "running")

    def test_kinds_and_keys_outside_the_module_are_refused_and_nothing_is_stored(self):
        self.refused("task_kind_not_owned", self.ctx.tasks.enqueue, kind=OTHER_KIND, payload={})
        self.refused("task_kind_not_declared", self.ctx.tasks.enqueue, kind="test.add_numbers", payload={"values": [1]})
        self.refused("task_kind_not_declared", self.ctx.tasks.enqueue, kind="request_intake.extract", payload={"run_id": str(uuid.uuid4())})
        self.refused("idempotency_key_not_namespaced", self.ctx.tasks.enqueue, kind=KIND, payload={}, idempotency_key="other_mod.work:1")
        self.refused("idempotency_key_not_namespaced", self.ctx.tasks.enqueue, kind=KIND, payload={}, idempotency_key="plain")
        self.assertEqual(Task.objects.count(), 0)

    def test_another_module_enqueues_only_its_own_kind(self):
        self.other.tasks.enqueue(kind=OTHER_KIND, payload={}, idempotency_key="other_mod.work:1")
        self.refused("task_kind_not_owned", self.other.tasks.enqueue, kind=KIND, payload={})
        self.assertEqual(list(Task.objects.values_list("kind", flat=True)), [OTHER_KIND])

    def test_an_unavailable_module_enqueues_nothing(self):
        for reason, code in UNAVAILABLE:
            with self.subTest(reason=reason), transaction.atomic():
                sid = transaction.savepoint()
                self.make_unavailable(reason)
                self.refused(code, self.ctx.tasks.enqueue, kind=KIND, payload={})
                self.assertEqual(Task.objects.count(), 0)
                transaction.savepoint_rollback(sid)

    def test_enabling_again_allows_it_and_held_tasks_are_untouched(self):
        queued = self.ctx.tasks.enqueue(kind=KIND, payload={}, idempotency_key="ctx_mod.work:1").task
        host.disable_module(self.registry, CTX)
        self.refused("module_disabled", self.ctx.tasks.enqueue, kind=KIND, payload={})
        row = Task.objects.get(pk=queued.pk)
        self.assertEqual((row.state, row.attempt_count), ("queued", 0))  # held by the worker gate, not changed here
        host.enable_module(self.registry, CTX)
        self.assertTrue(self.ctx.tasks.enqueue(kind=KIND, payload={}, idempotency_key="ctx_mod.work:2").created)

    def test_before_the_first_sync_a_registered_module_counts_as_enabled(self):
        raw("DELETE FROM module_host_modulestate")
        raw("DELETE FROM module_host_modulehostmarker")  # the pre-sync installation: no rows, no marker
        self.assertFalse(ModuleHostMarker.objects.exists())
        self.assertTrue(self.ctx.tasks.enqueue(kind=KIND, payload={}).created)

    def test_the_task_and_the_callers_transaction_commit_or_roll_back_together(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.ctx.tasks.enqueue(kind=KIND, payload={}, idempotency_key="ctx_mod.work:rollback")
            raise RuntimeError("the business operation failed")
        self.assertEqual(Task.objects.count(), 0)

    def test_the_state_lock_is_taken_before_the_insert(self):
        with CaptureQueriesContext(connection) as queries:
            self.ctx.tasks.enqueue(kind=KIND, payload={}, idempotency_key="ctx_mod.work:sql")
        statements = [q["sql"] for q in queries.captured_queries]
        locks = [i for i, sql in enumerate(statements) if "FOR SHARE" in sql]
        inserts = [i for i, sql in enumerate(statements) if sql.startswith('INSERT INTO "tasks_task"')]
        self.assertEqual(len(locks), 1)
        self.assertEqual(len(inserts), 1)
        self.assertLess(locks[0], inserts[0])
        self.assertIn("module_host_modulestate", statements[locks[0]])


# --- case.run -----------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class CaseRunTests(ContextDbCase):
    def new_run(self, context=None):
        context = context or self.ctx
        case = context.cases.create_case(reference=f"ctx-{uuid.uuid4()}")
        return context.cases.create_run(case_id=case.pk, workflow_key=FLOW if context is self.ctx else "other_mod.flow")

    def test_a_run_carries_the_modules_attribution_at_the_declared_version(self):
        run = self.new_run()
        stored = WorkflowRun.objects.get(pk=run.pk)
        self.assertEqual(stored.attribution, WorkflowAttribution(CTX, FLOW, 3))
        self.assertEqual((stored.state, stored.failed_step, stored.current_revision_id), ("queued", "", None))

    def test_the_other_module_gets_its_own_attribution(self):
        self.assertEqual(WorkflowRun.objects.get(pk=self.new_run(self.other).pk).attribution, WorkflowAttribution(OTHER, "other_mod.flow", 1))

    def test_an_undeclared_workflow_creates_nothing(self):
        case = self.ctx.cases.create_case(reference="undeclared")
        for key in ("other_mod.flow", "ctx_mod.unknown", "request_intake"):
            self.refused("workflow_not_declared", self.ctx.cases.create_run, case_id=case.pk, workflow_key=key)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_a_missing_case_is_the_services_own_error(self):
        from apps.cases.errors import RecordNotFoundError

        with self.assertRaises(RecordNotFoundError):
            self.ctx.cases.create_run(case_id=uuid.uuid4(), workflow_key=FLOW)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_the_state_machine_is_the_cores(self):
        run = self.new_run()
        moved = self.ctx.cases.transition_run(run.pk, to=RunState.EXTRACTING)
        self.assertEqual((moved.state, WorkflowRun.objects.get(pk=run.pk).state), ("extracting", "extracting"))
        with self.assertRaises(InvalidTransitionError):
            self.ctx.cases.transition_run(run.pk, to=RunState.EXPORTED)  # not allowed from extracting
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).state, "extracting")
        failed = self.ctx.cases.transition_run(run.pk, to=RunState.FAILED, failed_step="extract")
        self.assertEqual((failed.state, failed.failed_step), ("failed", "extract"))
        with self.assertRaises(InvalidTransitionError):
            self.ctx.cases.transition_run(run.pk, to=RunState.NEEDS_REVIEW, allowed_from=(RunState.QUEUED,))  # state_mismatch, as before

    def test_a_transition_on_another_modules_run_is_refused_and_changes_nothing(self):
        theirs = self.new_run(self.other)
        self.refused("run_not_owned", self.ctx.cases.transition_run, theirs.pk, to=RunState.EXTRACTING)
        self.assertEqual(WorkflowRun.objects.get(pk=theirs.pk).state, "queued")

    def test_a_request_intake_run_cannot_be_moved_through_a_context(self):
        result = submit_request(text=VALID_TEXT, submitted_by=make_user("ri"))
        before = WorkflowRun.objects.values().get(pk=result.run.pk)
        self.refused("run_not_owned", self.ctx.cases.transition_run, result.run.pk, to=RunState.EXTRACTING)
        self.assertEqual(WorkflowRun.objects.values().get(pk=result.run.pk), before)
        self.assertEqual(WorkflowRun.objects.get(pk=result.run.pk).attribution, REQUEST_INTAKE_ATTRIBUTION)

    def test_a_module_cannot_tell_a_missing_run_from_another_modules_run(self):
        theirs = self.new_run(self.other)
        outcomes = set()
        for run_id in (theirs.pk, uuid.uuid4(), str(theirs.pk), None):
            with self.assertRaises(ModuleContextError) as caught:
                self.ctx.cases.transition_run(run_id, to=RunState.EXTRACTING)
            outcomes.add((caught.exception.code, str(caught.exception)))
        self.assertEqual(outcomes, {("run_not_owned", "that run was not created under this module")})
        self.assertEqual(WorkflowRun.objects.get(pk=theirs.pk).state, "queued")

    def test_an_own_run_keeps_the_cores_behaviour_for_an_authorized_module(self):
        mine = self.new_run()
        self.assertEqual(self.ctx.cases.transition_run(mine.pk, to=RunState.EXTRACTING).state, "extracting")
        with self.assertRaises(InvalidTransitionError):  # the Core service's own refusal, not hidden behind a module error
            self.ctx.cases.transition_run(mine.pk, to=RunState.EXPORTED)

    def test_a_disabled_module_learns_nothing_about_any_run(self):
        mine, theirs = self.new_run(), self.new_run(self.other)
        host.disable_module(self.registry, CTX)
        outcomes = set()
        for run_id in (mine.pk, theirs.pk, uuid.uuid4()):
            with self.assertRaises(ModuleContextError) as caught:
                self.ctx.cases.transition_run(run_id, to=RunState.EXTRACTING)
            outcomes.add((caught.exception.code, str(caught.exception)))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(next(iter(outcomes))[0], "module_disabled")
        self.assertEqual({WorkflowRun.objects.get(pk=r.pk).state for r in (mine, theirs)}, {"queued"})

    def test_the_context_takes_no_lock_on_a_run_it_does_not_own(self):
        theirs = self.new_run(self.other)
        with CaptureQueriesContext(connection) as queries:
            self.refused("run_not_owned", self.ctx.cases.transition_run, theirs.pk, to=RunState.EXTRACTING)
        run_statements = [q["sql"] for q in queries.captured_queries if "cases_workflowrun" in q["sql"]]
        self.assertEqual(len(run_statements), 1)  # one plain read of the immutable attribution
        self.assertNotIn("FOR UPDATE", run_statements[0])
        self.assertNotIn("FOR SHARE", run_statements[0])

    def test_an_unavailable_module_writes_nothing(self):
        case = self.ctx.cases.create_case(reference="before")
        run = self.new_run()
        for reason, code in UNAVAILABLE:
            with self.subTest(reason=reason), transaction.atomic():
                sid = transaction.savepoint()
                self.make_unavailable(reason)
                self.refused(code, self.ctx.cases.create_case, reference="after")
                self.refused(code, self.ctx.cases.create_run, case_id=case.pk, workflow_key=FLOW)
                self.refused(code, self.ctx.cases.transition_run, run.pk, to=RunState.EXTRACTING)
                self.assertEqual((Case.objects.count(), WorkflowRun.objects.count(), WorkflowRun.objects.get(pk=run.pk).state), (2, 1, "queued"))
                transaction.savepoint_rollback(sid)

    def test_a_run_and_the_callers_transaction_commit_or_roll_back_together(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.new_run()
            raise RuntimeError("the business operation failed")
        self.assertEqual((Case.objects.count(), WorkflowRun.objects.count()), (0, 0))

    def test_historical_runs_are_untouched(self):
        old = case_services.create_run(case_id=case_services.create_case(reference="historic").pk, attribution=WorkflowAttribution("old_mod", "old_mod.flow", 2))
        before = WorkflowRun.objects.values().get(pk=old.pk)
        self.new_run()
        self.assertEqual(WorkflowRun.objects.values().get(pk=old.pk), before)


# --- audit.record -------------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class AuditRecordTests(ContextDbCase):
    def setUp(self):
        super().setUp()
        case = self.ctx.cases.create_case(reference=f"audit-{uuid.uuid4()}")
        self.run = self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)
        self.user = make_user("auditor1")

    def record(self, **overrides):
        values = dict(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=self.run.pk, run_id=self.run.pk, case_id=self.run.case_id, count=3, reason_code="ok")
        values.update(overrides)
        with transaction.atomic():
            return self.ctx.audit.record(**values)

    def test_a_system_event_is_stored_by_the_one_recorder(self):
        event = self.record()
        stored = AuditEvent.objects.get(pk=event.pk)
        self.assertEqual((stored.actor_kind, stored.actor_public_id, stored.action, stored.subject_type, stored.subject_id), ("system", None, ACTION, "run", self.run.pk))
        self.assertEqual((stored.run_id, stored.case_id, stored.details), (self.run.pk, self.run.case_id, {"count": 3, "reason_code": "ok"}))

    def test_a_user_event_takes_the_public_id_from_the_stored_user(self):
        forged = type(self.user).objects.get(pk=self.user.pk)
        forged.public_id = uuid.uuid4()  # in memory only
        event = self.record(actor=forged)
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).actor_public_id, self.user.public_id)

    def test_an_inactive_user_cannot_be_the_actor(self):
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(AuthorizationError):
            self.record(actor=self.user)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_it_needs_the_callers_transaction(self):
        with mock.patch.object(connection, "in_atomic_block", False):
            with self.assertRaises(AuditTransactionError):
                self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=self.run.pk)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_core_and_undeclared_actions_and_keys_are_refused(self):
        self.refused("audit_action_core_owned", self.record, action="run.state_changed")
        self.refused("audit_action_core_owned", self.record, action="module.disabled")
        self.refused("audit_action_not_declared", self.record, action=support.OTHER_ACTION)
        self.refused("audit_detail_key_not_declared", self.record, note="x")
        self.refused("audit_subject_type_not_allowed", self.record, subject_type="module", subject_id=uuid.uuid4())
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_the_vocabulary_value_rules_are_core_s(self):
        for bad in ("has a space", "quote'd", "x" * 101):
            with self.subTest(value=bad), self.assertRaises(events.AuditEventError):
                self.record(reason_code=bad)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_a_run_of_another_module_or_a_missing_run_is_refused(self):
        other_case = self.other.cases.create_case(reference="theirs")
        theirs = self.other.cases.create_run(case_id=other_case.pk, workflow_key="other_mod.flow")
        self.refused("run_not_owned", self.record, run_id=theirs.pk)
        self.refused("run_not_owned", self.record, subject_id=theirs.pk)
        self.refused("run_not_owned", self.record, run_id=uuid.uuid4())
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_an_unauthorized_actor_learns_nothing_about_runs(self):
        from django.contrib.auth.models import AnonymousUser

        theirs = self.other.cases.create_run(case_id=self.other.cases.create_case(reference="theirs2").pk, workflow_key="other_mod.flow")
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        for actor, reason in ((self.user, "inactive"), (AnonymousUser(), "not_authenticated")):
            for run_id in (theirs.pk, uuid.uuid4(), self.run.pk):
                with self.subTest(actor=reason, run=str(run_id)[:8]):
                    with self.assertRaises(AuthorizationError) as caught:
                        self.record(actor=actor, run_id=run_id)
                    self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_a_missing_run_and_another_modules_run_are_the_same_refusal_for_a_valid_actor(self):
        theirs = self.other.cases.create_run(case_id=self.other.cases.create_case(reference="theirs3").pk, workflow_key="other_mod.flow")
        outcomes = set()
        for run_id in (theirs.pk, uuid.uuid4()):
            with self.assertRaises(ModuleContextError) as caught:
                self.record(actor=self.user, run_id=run_id)
            outcomes.add((caught.exception.code, str(caught.exception)))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_an_unavailable_module_records_nothing(self):
        for reason, code in UNAVAILABLE:
            with self.subTest(reason=reason), transaction.atomic():
                sid = transaction.savepoint()
                self.make_unavailable(reason)
                self.refused(code, self.ctx.audit.record, actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=self.run.pk)
                self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)
                transaction.savepoint_rollback(sid)

    def test_the_event_commits_and_rolls_back_with_the_operation(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=self.run.pk)
            raise RuntimeError("the business operation failed")
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 0)

    def test_a_failed_audit_write_rolls_the_whole_operation_back(self):
        with mock.patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("the insert failed")):
            with self.assertRaises(IntegrityError), transaction.atomic():
                case = self.ctx.cases.create_case(reference="doomed")
                run = self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)
                self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=run.pk, run_id=run.pk)
        self.assertFalse(Case.objects.filter(reference="doomed").exists())
        self.assertEqual(WorkflowRun.objects.count(), 1)  # only the setUp run

    def test_the_orm_immutability_of_events_is_untouched(self):
        event = self.record()
        stored = AuditEvent.objects.get(pk=event.pk)
        stored.action = "ctx_mod.changed"
        with self.assertRaises(ImmutableRecordError):
            stored.save()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(pk=event.pk).update(action="ctx_mod.changed")
        with self.assertRaises(ImmutableRecordError):
            stored.delete()

    def test_nothing_is_logged_about_the_event(self):
        with self.assertNoLogs(level="DEBUG"):
            self.record()


# --- permissions.check --------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class PermissionCheckTests(review_base.ReviewCase):
    """Real users, groups and permissions (``setup_roles``); the module declares the existing ``intake.*`` ids, so the real roles apply to it."""

    def setUp(self):
        super().setUp()
        self.registry = support.registry()
        host.sync_modules(self.registry)
        self.permissions = ModuleContext.open(self.registry, CTX).permissions

    def reason(self, user, *permissions, owner_pk=None):
        try:
            self.permissions.authorize(user, *permissions, owner_pk=owner_pk)
        except AuthorizationError as error:
            return error.reason
        return None

    VIEW = (support.P_VIEW_OWN, support.P_VIEW_ANY)

    def test_an_operator_reaches_only_their_own_records(self):
        self.assertIsNone(self.reason(self.operator, *self.VIEW, owner_pk=self.operator.pk))
        self.assertEqual(self.reason(self.operator, *self.VIEW, owner_pk=self.other_operator.pk), "out_of_scope")
        self.assertEqual(self.reason(self.operator, *self.VIEW, owner_pk=None), "out_of_scope")

    def test_an_auditor_and_an_approver_reach_any_record(self):
        for user in (self.auditor, self.approver):
            self.assertIsNone(self.reason(user, *self.VIEW, owner_pk=self.operator.pk))
            self.assertIsNone(self.reason(user, *self.VIEW, owner_pk=None))

    def test_a_superuser_passes_both_parts(self):
        self.assertIsNone(self.reason(self.superuser, *self.VIEW, owner_pk=self.operator.pk))
        self.assertIsNone(self.reason(self.superuser, support.P_SUBMIT))

    def test_a_user_with_no_role_learns_nothing_about_the_record(self):
        for owner in (None, self.operator.pk, self.nobody.pk, 10**9):
            self.assertEqual(self.reason(self.nobody, *self.VIEW, owner_pk=owner), "missing_permission")
        with self.assertRaises(AuthorizationError) as caught:
            self.permissions.require(self.nobody, *self.VIEW)
        self.assertEqual(caught.exception.reason, "missing_permission")

    def test_a_global_permission_needs_no_record(self):
        self.assertIsNone(self.reason(self.operator, support.P_SUBMIT))
        self.assertEqual(self.reason(self.approver, support.P_SUBMIT), "missing_permission")

    def test_an_inactive_user_and_a_deleted_user_are_refused_before_any_rule(self):
        stale = type(self.operator).objects.get(pk=self.operator.pk)
        type(self.operator).objects.filter(pk=self.operator.pk).update(is_active=False)
        self.assertEqual(self.reason(stale, support.P_SUBMIT), "inactive")
        type(self.operator).objects.filter(pk=self.operator.pk).delete()
        self.assertEqual(self.reason(stale, support.P_SUBMIT), "not_authenticated")

    def test_a_permission_change_is_honoured_at_once(self):
        from django.contrib.auth.models import Group

        self.assertIsNone(self.reason(self.operator, support.P_SUBMIT))
        self.operator.groups.remove(Group.objects.get(name="Operator"))
        self.assertEqual(self.reason(self.operator, support.P_SUBMIT), "missing_permission")  # the user object passed in is stale; the stored groups decide

    def test_core_permissions_are_limited_to_the_permitted_set(self):
        self.assertIsNone(self.reason(self.auditor, "audit.view_auditevent"))
        self.assertEqual(self.reason(self.operator, "audit.view_auditevent"), "missing_permission")
        for permission in ("intake.approve_request", "intake.reject_request", "auth.add_user"):
            with self.assertRaises(ModuleContextError) as caught:
                self.permissions.authorize(self.superuser, permission)  # not even a superuser can ask a module's context about them
            self.assertEqual(caught.exception.code, "permission_not_declared")

    def test_the_returned_actor_is_the_stored_one(self):
        actor = self.permissions.authorize(self.operator, support.P_SUBMIT)
        self.assertEqual((actor.pk, actor.public_id), (self.operator.pk, self.operator.public_id))

    def test_the_decision_equals_request_intakes_own_for_every_user_and_owner(self):
        """The module's rule over the same permissions gives the same answer as ``apps.intake.authorization`` (which delegates to the same Core rule)."""

        def intake_reason(user, action, owner):
            try:
                intake_authorization.authorize(user, action, submitter_pk=owner)
            except AuthorizationError as error:
                return error.reason
            return None

        hugo = self.hybrid
        users = (self.operator, self.other_operator, self.approver, self.auditor, self.nobody, self.superuser, hugo)
        for user in users:
            for owner in (None, self.operator.pk, self.other_operator.pk, hugo.pk):
                with self.subTest(user=user.username, owner=owner):
                    self.assertEqual(self.reason(user, *self.VIEW, owner_pk=owner), intake_reason(user, Action.VIEW, owner))
            self.assertEqual(self.reason(user, support.P_SUBMIT), intake_reason(user, Action.SUBMIT, None))

    def test_a_refusal_is_logged_by_code_and_public_id_only(self):
        with self.assertLogs("apps.accounts.authz", level="WARNING") as logs:
            self.reason(self.nobody, *self.VIEW, owner_pk=self.operator.pk)
        line = logs.output[0]
        self.assertIn("missing_permission", line)
        self.assertIn(str(self.nobody.public_id), line)
        for private in (self.nobody.username, str(self.operator.pk) + "x"):
            self.assertNotIn(private, line.replace(str(self.nobody.public_id), ""))

    def test_checking_a_permission_reads_no_module_state_and_takes_no_lock(self):
        host.disable_module(self.registry, CTX)
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNone(self.reason(self.operator, support.P_SUBMIT))
        self.assertFalse([q for q in queries.captured_queries if "module_host" in q["sql"] or "FOR SHARE" in q["sql"]])


# --- module state against real rows -----------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ModuleStateEnforcementTests(ContextDbCase):
    def test_disable_and_enable_switch_every_state_changing_capability(self):
        case = self.ctx.cases.create_case(reference="state")
        run = self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)
        host.disable_module(self.registry, CTX)
        calls = (
            lambda: self.ctx.tasks.enqueue(kind=KIND, payload={}),
            lambda: self.ctx.cases.create_case(reference="x"),
            lambda: self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW),
            lambda: self.ctx.cases.transition_run(run.pk, to=RunState.EXTRACTING),
            lambda: self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=run.pk),
        )
        for call in calls:
            with transaction.atomic():
                self.refused("module_disabled", call)
        self.assertEqual((Task.objects.count(), Case.objects.count(), WorkflowRun.objects.count(), AuditEvent.objects.filter(action=ACTION).count()), (0, 1, 1, 0))
        host.enable_module(self.registry, CTX)
        self.ctx.tasks.enqueue(kind=KIND, payload={})
        self.assertEqual(self.ctx.cases.transition_run(run.pk, to=RunState.EXTRACTING).state, "extracting")

    def test_one_modules_state_does_not_touch_the_others(self):
        host.disable_module(self.registry, OTHER)
        self.assertTrue(self.ctx.tasks.enqueue(kind=KIND, payload={}).created)
        self.refused("module_disabled", self.other.tasks.enqueue, kind=OTHER_KIND, payload={})

    def test_the_context_follows_the_stored_state_not_a_cached_one(self):
        context = ModuleContext.open(self.registry, CTX, handler_registry=HANDLERS)
        self.assertTrue(context.tasks.enqueue(kind=KIND, payload={}).created)
        host.disable_module(self.registry, CTX)
        self.refused("module_disabled", context.tasks.enqueue, kind=KIND, payload={})
        host.enable_module(self.registry, CTX)
        self.assertTrue(context.tasks.enqueue(kind=KIND, payload={}).created)

    def test_a_hand_edited_dependency_blocks_its_dependent(self):
        registry = support.registry(support.ctx_module(), support.other_module(), support.dependent("dep_mod", capabilities_required=("case.run",)))
        host.sync_modules(registry)
        dependent = ModuleContext.open(registry, "dep_mod")
        case = dependent.cases.create_case(reference="dep")
        ModuleState.objects.filter(module_id=CTX).update(enabled=False)  # a hand edit: the services refuse to disable a module that an enabled one needs
        self.refused("module_dependency_unavailable", dependent.cases.create_case, reference="dep2")
        self.assertEqual(Case.objects.filter(pk=case.pk).count(), 1)

    def test_a_module_added_by_a_release_is_held_until_its_state_is_synced(self):
        extra = support.registry(support.ctx_module(), support.other_module(), support.dependent("late_mod", on=CTX, capabilities_required=("case.run",)))
        late = ModuleContext.open(extra, "late_mod")  # a release that added a module; its state has not been synced
        self.refused("module_state_missing", late.cases.create_case, reference="x")
        host.sync_modules(extra)
        self.assertEqual(late.cases.create_case(reference="x").reference, "x")


# --- concurrency ----------------------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class ConcurrencyTests(DedicatedTestDatabaseMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.registry = support.registry()
        host.sync_modules(self.registry)
        patcher = mock.patch.object(events, "_installed", build_audit_vocabulary(self.registry))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = ModuleContext.open(self.registry, CTX, handler_registry=HANDLERS)
        # A second, real module context over the same synced registry (``ContextDbCase`` builds it for the TestCase classes; this class is a
        # TransactionTestCase and does not derive from it). Tests that need a run owned by another module create it through this context.
        self.other = ModuleContext.open(self.registry, OTHER, handler_registry=HANDLERS)

    def test_concurrent_enqueues_with_one_key_create_one_task(self):
        results = run_threads(self, [lambda: self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:race") for _ in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sum(1 for _, value in results if value.created), 1)
        self.assertEqual(len({value.task.pk for _, value in results}), 1)
        self.assertEqual(Task.objects.count(), 1)

    def test_concurrent_runs_each_keep_their_attribution(self):
        case = self.ctx.cases.create_case(reference="race")
        results = run_threads(self, [lambda: self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW) for _ in range(6)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual({run.attribution for run in WorkflowRun.objects.all()}, {WorkflowAttribution(CTX, FLOW, 3)})
        self.assertEqual(WorkflowRun.objects.count(), 6)

    def test_concurrent_transitions_of_one_run_apply_one(self):
        case = self.ctx.cases.create_case(reference="race2")
        run = self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)
        results = run_threads(self, [lambda: self.ctx.cases.transition_run(run.pk, to=RunState.EXTRACTING, allowed_from=(RunState.QUEUED,)) for _ in range(4)])
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "error", "error", "ok"])
        self.assertTrue(all(isinstance(value, InvalidTransitionError) for kind, value in results if kind == "error"))
        self.assertEqual(WorkflowRun.objects.get(pk=run.pk).state, "extracting")

    def in_flight(self, release, entered, operation=None):
        """A transaction that has used the context (so it holds the shared state lock) and stays open until ``release`` is set."""
        operation = operation or (lambda: self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:in-flight"))

        def work():
            try:
                with transaction.atomic():
                    operation()
                    entered.set()
                    release.wait(timeout=30)
            finally:
                entered.set()  # never leave the test waiting if the operation failed
                connection.close()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    def disable_in_thread(self, outcome, finished):
        def disable():
            try:
                raw("SET lock_timeout = '30s'")
                outcome["result"] = host.disable_module(self.registry, CTX)
            except Exception as error:  # reported by the assertions of the caller
                outcome["error"] = error
            finally:
                finished.set()
                connection.close()

        thread = threading.Thread(target=disable, daemon=True)
        thread.start()
        return thread

    def make_run(self):
        case = self.ctx.cases.create_case(reference=f"race-{uuid.uuid4()}")
        return self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)

    def test_calls_do_not_block_each_other(self):
        release, entered = threading.Event(), threading.Event()
        thread = self.in_flight(release, entered)
        try:
            self.assertTrue(entered.wait(timeout=30))
            raw("SET lock_timeout = '5s'")  # a blocked call becomes an error instead of a hang
            self.assertTrue(self.ctx.tasks.enqueue(kind=KIND, payload={"n": 2}, idempotency_key="ctx_mod.work:second").created)
        finally:
            raw("RESET lock_timeout")
            release.set()
            thread.join(timeout=30)
        self.assertEqual(Task.objects.count(), 2)

    def test_a_disable_waits_for_an_in_flight_call_and_later_calls_are_refused(self):
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        outcome = {}
        thread = self.in_flight(release, entered)

        def disable():
            try:
                raw("SET lock_timeout = '30s'")
                outcome["result"] = host.disable_module(self.registry, CTX)
            except Exception as error:  # reported by the assertions below
                outcome["error"] = error
            finally:
                finished.set()
                connection.close()

        disabler = threading.Thread(target=disable, daemon=True)
        try:
            self.assertTrue(entered.wait(timeout=30))
            disabler.start()
            self.assertFalse(finished.wait(timeout=1.5), "the disable must wait for the in-flight call: it holds the shared state lock")
            self.assertTrue(ModuleState.objects.get(module_id=CTX).enabled)
        finally:
            release.set()
            thread.join(timeout=30)
            disabler.join(timeout=30)
        self.assertNotIn("error", outcome)
        self.assertTrue(outcome["result"].changed)
        self.assertEqual(Task.objects.filter(idempotency_key="ctx_mod.work:in-flight").count(), 1)  # the in-flight call committed
        with self.assertRaises(ModuleContextError) as caught:
            self.ctx.tasks.enqueue(kind=KIND, payload={"n": 3}, idempotency_key="ctx_mod.work:late")
        self.assertEqual(caught.exception.code, "module_disabled")
        self.assertEqual(Task.objects.count(), 1)

    def test_a_disable_waits_for_every_kind_of_in_flight_call(self):
        """The shared lock is taken by each state-changing capability, so the disable cannot slip between its state check and its write."""
        run = self.make_run()
        operations = {
            "enqueue": lambda: self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}),
            "create_case": lambda: self.ctx.cases.create_case(reference=f"inflight-{uuid.uuid4()}"),
            "create_run": lambda: self.ctx.cases.create_run(case_id=run.case_id, workflow_key=FLOW),
            "transition_run": lambda: self.ctx.cases.transition_run(run.pk, to=RunState.EXTRACTING),
            "audit": lambda: self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=run.pk, run_id=run.pk, count=1),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                release, entered, finished, outcome = threading.Event(), threading.Event(), threading.Event(), {}
                worker = self.in_flight(release, entered, operation)
                disabler = None
                try:
                    self.assertTrue(entered.wait(timeout=30))
                    disabler = self.disable_in_thread(outcome, finished)
                    self.assertFalse(finished.wait(timeout=1.5), f"the disable must wait for the in-flight {name}")
                    self.assertTrue(ModuleState.objects.get(module_id=CTX).enabled)
                finally:
                    release.set()
                    worker.join(timeout=30)
                    if disabler is not None:
                        disabler.join(timeout=30)
                self.assertNotIn("error", outcome)
                self.assertFalse(ModuleState.objects.get(module_id=CTX).enabled)
                host.enable_module(self.registry, CTX)

    def test_a_call_that_meets_an_uncommitted_disable_waits_and_is_then_refused(self):
        release, entered, outcome, done = threading.Event(), threading.Event(), {}, threading.Event()

        def disabling():
            try:
                with transaction.atomic():
                    host.disable_module(self.registry, CTX)  # joins this transaction: the disable is not committed until release
                    entered.set()
                    release.wait(timeout=30)
            finally:
                entered.set()
                connection.close()

        def calling():
            try:
                raw("SET lock_timeout = '30s'")
                self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:blocked")
                outcome["result"] = "enqueued"
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()
                connection.close()

        disabler = threading.Thread(target=disabling, daemon=True)
        caller = threading.Thread(target=calling, daemon=True)
        disabler.start()
        try:
            self.assertTrue(entered.wait(timeout=30))
            caller.start()
            self.assertFalse(done.wait(timeout=1.5), "the call must wait for the uncommitted disable that holds the state rows")
        finally:
            release.set()
            disabler.join(timeout=30)
            caller.join(timeout=30)
        self.assertIsInstance(outcome.get("error"), ModuleContextError, outcome)
        self.assertEqual(outcome["error"].code, "module_disabled")
        self.assertEqual(Task.objects.count(), 0)

    def test_a_capability_blocked_by_the_state_lock_fails_with_the_database_error_and_writes_nothing(self):
        from django.db import OperationalError

        release, entered = threading.Event(), threading.Event()

        def holding():
            try:
                with transaction.atomic():
                    list(ModuleState.objects.select_for_update())  # what module_enable, module_disable and module_sync do
                    entered.set()
                    release.wait(timeout=30)
            finally:
                entered.set()
                connection.close()

        holder = threading.Thread(target=holding, daemon=True)
        holder.start()
        try:
            self.assertTrue(entered.wait(timeout=30))
            raw("SET lock_timeout = '1s'")
            with self.assertRaises(OperationalError):  # a DatabaseError, not a module refusal
                self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:timeout")
            with self.assertRaises(OperationalError):
                self.ctx.cases.create_case(reference="timeout")
            with self.assertRaises(OperationalError), transaction.atomic():
                self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="revision", subject_id=uuid.uuid4())
        finally:
            raw("RESET lock_timeout")
            release.set()
            holder.join(timeout=30)
        self.assertEqual((Task.objects.count(), Case.objects.count(), AuditEvent.objects.filter(action=ACTION).count()), (0, 0, 0))
        self.assertTrue(self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:after").created)  # the connection is usable again

    def test_a_refused_transition_on_another_modules_run_is_not_blocked_by_that_runs_lock(self):
        other_case = self.other.cases.create_case(reference="their-case")
        theirs = self.other.cases.create_run(case_id=other_case.pk, workflow_key="other_mod.flow")
        release, entered = threading.Event(), threading.Event()

        def holding():
            try:
                with transaction.atomic():
                    WorkflowRun.objects.select_for_update().get(pk=theirs.pk)
                    entered.set()
                    release.wait(timeout=30)
            finally:
                entered.set()
                connection.close()

        holder = threading.Thread(target=holding, daemon=True)
        holder.start()
        try:
            self.assertTrue(entered.wait(timeout=30))
            raw("SET lock_timeout = '3s'")  # a lock wait would be an error: the context must not wait on a run that is not its own
            with self.assertRaises(ModuleContextError) as caught:
                self.ctx.cases.transition_run(theirs.pk, to=RunState.EXTRACTING)
            self.assertEqual(caught.exception.code, "run_not_owned")
        finally:
            raw("RESET lock_timeout")
            release.set()
            holder.join(timeout=30)

    def test_no_state_lock_outlives_an_operation_that_opened_its_own_transaction(self):
        self.ctx.tasks.enqueue(kind=KIND, payload={"n": 1}, idempotency_key="ctx_mod.work:done")
        self.ctx.cases.create_case(reference="done")
        outcome, finished = {}, threading.Event()
        self.disable_in_thread(outcome, finished).join(timeout=30)
        self.assertTrue(finished.is_set())  # it did not have to wait for anything: the operations had already committed and released the lock
        self.assertNotIn("error", outcome)
        self.assertFalse(ModuleState.objects.get(module_id=CTX).enabled)

    def test_concurrent_audit_records_all_commit(self):
        case = self.ctx.cases.create_case(reference="audit-race")
        run = self.ctx.cases.create_run(case_id=case.pk, workflow_key=FLOW)

        def record():
            with transaction.atomic():
                return self.ctx.audit.record(actor=SYSTEM_ACTOR, action=ACTION, subject_type="run", subject_id=run.pk, run_id=run.pk, count=1)

        results = run_threads(self, [record for _ in range(5)])
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION).count(), 5)


# --- Request Intake, unchanged ----------------------------------------------------------------------------------------------


@tag("requires_postgres")
@requires_postgres
class LegacyRequestIntakeTests(review_base.ReviewCase):
    """The legacy path, with a synced module registry and a disabled module beside it: submission, extraction, review and approval are the ones they were."""

    def setUp(self):
        super().setUp()
        self.registry = support.registry()
        host.sync_modules(self.registry)

    def test_request_intake_gets_a_context_from_the_composition_root_since_mod_2_and_its_submission_still_does_not_use_it(self):
        from config.module_context import module_context

        self.assertEqual(module_context("request_intake").module_id, "request_intake")
        with self.assertRaises(ModuleContextError) as caught:
            module_context("intake")  # a legacy namespace, not a module id
        self.assertEqual(caught.exception.code, "unknown_module")

    def test_the_whole_workflow_runs_with_modules_synced_and_one_disabled(self):
        host.disable_module(self.registry, CTX)
        run = self.new_run()  # submit and extract
        self.assertEqual((self.state(run), WorkflowRun.objects.get(pk=run.pk).attribution), ("needs_review", REQUEST_INTAKE_ATTRIBUTION))
        self.assertEqual(self.approve(self.approver, run).run_state, "approved")

    def test_submission_takes_no_module_state_lock_and_reads_no_module_state(self):
        with CaptureQueriesContext(connection) as queries:
            submit(self.operator)
        self.assertFalse([q for q in queries.captured_queries if "module_host" in q["sql"] or "FOR SHARE" in q["sql"]])

    def test_the_production_worker_gate_holds_request_intake_and_namespace_continuity_refuses_the_production_sync_after_a_synthetic_one(self):
        from config.task_registry import MODULE_REGISTRY, UNGATED_KINDS

        # setUp synced a SYNTHETIC registry (modules ctx_mod and other_mod, namespaces ctx_mod, intake and other_mod): the initialization marker exists and Request
        # Intake has no state row, so the production gate fails closed for its kind (MOD-2).
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS)
        self.assertNotIn("request_intake.extract", COMPOSITION.kind_gate())
        before = (list(ModuleState.objects.values_list("module_id", flat=True).order_by("module_id")), list(ModuleNamespaceClaim.objects.values_list("namespace", "module_id").order_by("namespace")))
        # The production registry can never follow that sync: it releases ctx_mod and other_mod and gives `intake` to request_intake. Namespace continuity
        # (S4) refuses it as it must, changes nothing, and the gate stays closed. Real installations never synced a synthetic registry; the production
        # sync after an earlier empty-registry sync is tested in test_request_intake_module_postgres.py.
        with self.assertRaises(ModuleHostError) as caught:
            host.sync_modules(MODULE_REGISTRY)
        self.assertIn(caught.exception.code, ("namespace_released", "namespace_ownership_changed"))
        self.assertEqual(
            (list(ModuleState.objects.values_list("module_id", flat=True).order_by("module_id")), list(ModuleNamespaceClaim.objects.values_list("namespace", "module_id").order_by("namespace"))),
            before,
        )
        self.assertFalse(ModuleState.objects.filter(module_id="request_intake").exists())
        self.assertEqual(COMPOSITION.kind_gate(), UNGATED_KINDS)
