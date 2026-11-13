"""DB-free tests: the audit event vocabulary, the shape of an event, and the recorder's transaction guard."""

import uuid
from unittest import mock

from django.test import SimpleTestCase

from apps.audit import events, recorder
from apps.audit import models as audit_models
from apps.audit.events import AuditEventError, NewAuditEvent, system_event, user_event
from apps.audit.models import AuditEvent
from apps.cases.models import ImmutableRecord

ACTOR, SUBJECT, CASE, RUN = (uuid.uuid4() for _ in range(4))


def valid_event(action=events.ACTION_RUN_STATE_CHANGED, **details):
    details = details or {"from_state": "needs_review", "to_state": "approved", "cause": "approval"}
    return user_event(ACTOR, action, events.SUBJECT_RUN, SUBJECT, case_id=CASE, run_id=RUN, **details)


class VocabularyTests(SimpleTestCase):
    def test_the_actions_are_the_fixed_vocabulary(self):
        self.assertEqual(
            set(events.ACTIONS),
            {"request.submitted", "revision.created", "request.edited", "run.state_changed", "approval.recorded", "approval.invalidated", "rejection.recorded"},
        )
        self.assertEqual(len(set(events.ACTIONS)), len(events.ACTIONS))

    def test_every_action_has_a_detail_key_list_and_nothing_else_does(self):
        self.assertEqual(set(events.DETAIL_KEYS), set(events.ACTIONS))

    def test_the_database_constraints_list_the_same_vocabulary(self):
        by_name = {c.name: c for c in AuditEvent._meta.constraints}
        self.assertEqual(
            set(by_name),
            {"audit_event_action_valid", "audit_event_subject_type_valid", "audit_event_actor_kind_valid", "audit_event_actor_matches_kind"},
        )
        self.assertEqual(by_name["audit_event_action_valid"].condition.children[0][1], list(events.ACTIONS))
        self.assertEqual(by_name["audit_event_subject_type_valid"].condition.children[0][1], list(events.SUBJECT_TYPES))
        self.assertEqual(by_name["audit_event_actor_kind_valid"].condition.children[0][1], list(events.ACTOR_KINDS))

    def test_no_detail_key_can_hold_content_by_name(self):
        banned = {"text", "name", "email", "contact", "notes", "description", "message", "excerpt", "reason", "error", "password", "username"}
        for action, keys in events.DETAIL_KEYS.items():
            with self.subTest(action=action):
                self.assertEqual(set(keys) & banned, set())


class EventShapeTests(SimpleTestCase):
    def test_a_valid_user_event(self):
        event = valid_event()
        self.assertEqual((event.actor_kind, event.actor_public_id, event.action), ("user", ACTOR, "run.state_changed"))
        self.assertEqual((event.subject_type, event.subject_id, event.case_id, event.run_id), ("run", SUBJECT, CASE, RUN))

    def test_a_valid_system_event(self):
        event = system_event(events.ACTION_REVISION_CREATED, events.SUBJECT_REVISION, SUBJECT, case_id=CASE, run_id=RUN, revision_number=1, origin="extraction")
        self.assertEqual((event.actor_kind, event.actor_public_id), ("system", None))

    def test_the_event_is_immutable_and_copies_its_details(self):
        details = {"from_state": "a", "to_state": "b", "cause": "c"}
        event = user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, events.SUBJECT_RUN, SUBJECT, **details)
        details["cause"] = "changed-later"
        self.assertEqual(event.details["cause"], "c")
        with self.assertRaises(Exception):
            event.action = "approval.recorded"

    def test_actor_rules(self):
        with self.assertRaises(AuditEventError):
            NewAuditEvent("user", None, events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT)  # a user needs a public id
        with self.assertRaises(AuditEventError):
            NewAuditEvent("user", "not-a-uuid", events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT)
        with self.assertRaises(AuditEventError):
            NewAuditEvent("system", ACTOR, events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT)  # the system has none
        with self.assertRaises(AuditEventError):
            NewAuditEvent("robot", None, events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT)

    def test_unknown_action_and_subject_are_refused(self):
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, "request.deleted", "run", SUBJECT)
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, "document", SUBJECT)

    def test_identifiers_must_be_uuids(self):
        for kwargs in ({"case_id": "x"}, {"run_id": 5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AuditEventError):
                user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT, **kwargs)
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, "run", "not-a-uuid")

    def test_detail_keys_are_limited_per_action(self):
        with self.assertRaises(AuditEventError) as caught:
            user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, events.SUBJECT_RUN, SUBJECT, from_state="a", reason_text="x")
        self.assertIn("reason_text", str(caught.exception))
        # a key that is valid for another action is refused here
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, events.SUBJECT_RUN, SUBJECT, content_hash="a" * 64)

    def test_every_allowed_key_of_every_action_accepts_a_safe_value(self):
        for action, keys in events.DETAIL_KEYS.items():
            with self.subTest(action=action):
                user_event(ACTOR, action, events.SUBJECT_RUN, SUBJECT, **{key: "ok_value-1" for key in keys})

    def test_free_text_and_unsafe_values_are_refused(self):
        unsafe = ["two words", "Jane Doe", "jane@example.invalid", "line\nbreak", "quote'", 'q"uote', "a/b", "", "x" * 101, b"bytes", 1.5, ["list"], {"k": "v"}, uuid.uuid4()]
        for value in unsafe:
            with self.subTest(value=value), self.assertRaises(AuditEventError) as caught:
                user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, events.SUBJECT_RUN, SUBJECT, cause=value)
            self.assertNotIn("Jane", str(caught.exception))  # the message names the rule, never the value

    def test_safe_values(self):
        for value in (None, True, False, 0, 12, "needs_review", "a" * 64, str(uuid.uuid4()), "run.state_changed", "a:b-c_d.e"):
            with self.subTest(value=value):
                user_event(ACTOR, events.ACTION_RUN_STATE_CHANGED, events.SUBJECT_RUN, SUBJECT, cause=value)

    def test_details_must_be_an_object(self):
        with self.assertRaises(AuditEventError):
            NewAuditEvent("user", ACTOR, events.ACTION_RUN_STATE_CHANGED, "run", SUBJECT, details=["x"])


class ModelTests(SimpleTestCase):
    def test_audit_events_are_append_only_at_the_orm_boundary(self):
        self.assertTrue(issubclass(AuditEvent, ImmutableRecord))

    def test_no_foreign_keys_so_an_event_outlives_its_subjects_and_its_user(self):
        self.assertEqual([f.name for f in AuditEvent._meta.get_fields() if f.is_relation], [])

    def test_only_a_view_permission_exists(self):
        self.assertEqual(tuple(AuditEvent._meta.default_permissions), ("view",))

    def test_the_id_orders_events_by_insertion(self):
        self.assertEqual(AuditEvent._meta.pk.get_internal_type(), "BigAutoField")

    def test_the_docstring_does_not_claim_database_level_immutability(self):
        doc = " ".join(audit_models.__doc__.split())
        self.assertIn("NO database-level protection", doc)
        self.assertIn("no hash chain", doc)
        self.assertNotIn("tamper-proof", doc.lower())
        self.assertNotIn("tamper proof", doc.lower())


class RecorderGuardTests(SimpleTestCase):
    def test_recording_outside_a_transaction_is_refused_before_any_query(self):
        with mock.patch.object(recorder.AuditEvent.objects, "create") as create:
            with self.assertRaises(recorder.AuditTransactionError):
                recorder.record_event(valid_event())
            create.assert_not_called()

    def test_recording_inside_a_transaction_writes_the_row_with_the_event_fields(self):
        event = valid_event()
        with mock.patch.object(recorder.connection, "in_atomic_block", True), mock.patch.object(recorder.AuditEvent.objects, "create") as create:
            recorder.record_event(event)
        create.assert_called_once_with(
            actor_kind="user",
            actor_public_id=ACTOR,
            action="run.state_changed",
            subject_type="run",
            subject_id=SUBJECT,
            case_id=CASE,
            run_id=RUN,
            details=event.details,
        )

    def test_only_a_new_audit_event_can_be_recorded(self):
        with mock.patch.object(recorder.connection, "in_atomic_block", True):
            for bad in ({"action": "run.state_changed"}, None, "x"):
                with self.subTest(bad=bad), self.assertRaises(TypeError):
                    recorder.record_event(bad)
