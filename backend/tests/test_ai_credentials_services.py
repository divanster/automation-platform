"""DB-free tests of the managed AI credential services (AI-SEC-2): authorization, validation, create / replace / delete / status, the audit events, the failure of the secret store and the
absence of any secret from errors, logs and events. The services are the real ones; the database touch points are the stand-ins of ``tests/ai_credentials_support.py``."""

import contextlib
import logging
import os
import threading
import traceback
import uuid
from unittest import mock

from django.db import DatabaseError
from django.test import SimpleTestCase

from apps.accounts.errors import AuthorizationError
from apps.ai_credentials import services
from apps.ai_credentials.errors import (
    CredentialAlreadyExistsError,
    CredentialInUseError,
    CredentialNotFoundError,
    CredentialStoreError,
    CredentialValidationError,
)
from apps.ai_credentials.rules import credential_lock_key, credential_subject_id
from apps.secret_store import (
    CODE_BUSY,
    CODE_DISABLED,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    DisabledSecretStore,
    SecretStoreError,
    SecretValue,
)
from tests.ai_credentials_support import (
    ADMIN_ID,
    ENV_SENTINEL,
    OTHER_REF,
    REF,
    SECRET_ONE,
    SECRET_TWO,
    SENTINELS,
    AdvisoryLocks,
    FakeStore,
    MemoryStore,
    Using,
    actor,
    ambiguous_commit_of,
    interrupted_commit_of,
    patched_credentials,
    rolled_back_commit_of,
)
from tests.ai_settings_support import ALL_ROLE_PERMISSIONS


def administrator():
    return actor(is_superuser=True)


def operations(store, user="user"):
    """Every operation as a zero-argument callable, over the secret store ``store``."""
    factory = lambda: store  # noqa: E731
    return {
        "status": lambda: services.get_status(user=user, reference=REF, store_factory=factory),
        "create": lambda: services.create_credential(user=user, reference=REF, value=SECRET_ONE, store_factory=factory),
        "replace": lambda: services.replace_credential(user=user, reference=REF, value=SECRET_TWO, store_factory=factory),
        "delete": lambda: services.delete_credential(user=user, reference=REF, store_factory=factory),
    }


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


class CredentialCase(SimpleTestCase):
    def setUp(self):
        self.db = FakeStore()
        self.store = MemoryStore()
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)

    def run_as(self, who, name, store=None, using=None, atomic=None):
        with patched_credentials(self.db, who, using=using, atomic=atomic):
            return operations(self.store if store is None else store)[name]()

    def admin(self, name, **kwargs):
        return self.run_as(administrator(), name, **kwargs)

    def failure_of(self, call):
        try:
            call()
        except BaseException as error:  # noqa: BLE001 - the tests include KeyboardInterrupt and SystemExit
            return error
        self.fail("the operation did not fail")

    def assertNoLeak(self, *texts):
        everything = "\n".join(texts + tuple(self.capture.lines))
        for secret in SENTINELS:
            self.assertNotIn(secret, everything)


class AuthorizationTests(CredentialCase):
    def test_a_non_administrator_is_refused_by_every_operation_and_nothing_happens(self):
        for name in ("status", "create", "replace", "delete"):
            with self.subTest(name), self.assertLogs("apps.accounts.authz", "WARNING"):
                error = self.failure_of(lambda: self.run_as(actor(is_superuser=False, permissions=ALL_ROLE_PERMISSIONS), name))
            self.assertIsInstance(error, AuthorizationError)
            self.assertEqual(error.reason, "missing_permission")
        self.assertEqual((self.store.calls, self.store.items, self.db.events), ([], {}, []))

    def test_the_refusal_comes_before_the_reference_and_the_value_are_looked_at(self):
        store = MemoryStore()
        factory = lambda: store  # noqa: E731
        calls = {
            "status": lambda: services.get_status(user="u", reference="not a reference", store_factory=factory),
            "create": lambda: services.create_credential(user="u", reference="not a reference", value="", store_factory=factory),
            "replace": lambda: services.replace_credential(user="u", reference="not a reference", value="", store_factory=factory),
            "delete": lambda: services.delete_credential(user="u", reference="not a reference", store_factory=factory),
        }
        for name, call in calls.items():
            with self.subTest(name), patched_credentials(self.db, actor(is_superuser=False)), self.assertLogs("apps.accounts.authz", "WARNING"):
                error = self.failure_of(call)
            self.assertIsInstance(error, AuthorizationError)
        self.assertEqual(store.calls, [])

    def test_an_unauthenticated_or_inactive_user_gets_the_authorization_error_of_the_lookup(self):
        for name in ("status", "create", "replace", "delete"):
            with self.subTest(name):
                error = self.failure_of(lambda: self.run_as(AuthorizationError("not_authenticated"), name))
                self.assertEqual(error.reason, "not_authenticated")
        self.assertEqual((self.store.calls, self.db.events), ([], []))

    def test_the_administrator_may_use_every_operation(self):
        self.assertEqual(self.admin("status").status, "not_set")
        self.admin("create")
        self.admin("replace")
        self.admin("delete")

    def test_the_actor_is_reloaded_on_every_call(self):
        with patched_credentials(self.db, administrator()) as lookup:
            operations(self.store, user="the-user")["status"]()
            operations(self.store, user="the-user")["status"]()
        self.assertEqual(lookup.call_count, 2)
        lookup.assert_called_with("the-user")


class ValidationTests(CredentialCase):
    BAD_REFERENCES = ("", " ", "ap_ai_cred_x", "AP_AI_CRED_", "AP_AI_CRED_lower", "AP_AI_CRED_" + "X" * 49, "AP_SECRET_KEY", "DJANGO_SECRET_KEY", "AP_AI_CRED_A B", "AP_AI_CRED_A\n", "AP_AI_CRED_../X", "ADMIN")
    BAD_VALUES = ("", " leading", "trailing ", "line\nbreak", "tab\there", "x" * 4097, "\u0000nul")

    def test_an_invalid_reference_is_a_field_error_and_touches_nothing(self):
        factory = lambda: self.store  # noqa: E731
        for bad in (*self.BAD_REFERENCES, None, 5, b"AP_AI_CRED_X", ["AP_AI_CRED_X"]):
            calls = {
                "status": lambda: services.get_status(user="u", reference=bad, store_factory=factory),
                "create": lambda: services.create_credential(user="u", reference=bad, value=SECRET_ONE, store_factory=factory),
                "replace": lambda: services.replace_credential(user="u", reference=bad, value=SECRET_ONE, store_factory=factory),
                "delete": lambda: services.delete_credential(user="u", reference=bad, store_factory=factory),
            }
            for name, method in calls.items():
                with self.subTest(reference=repr(bad)[:30], operation=name), patched_credentials(self.db, administrator()):
                    error = self.failure_of(method)
                self.assertIsInstance(error, CredentialValidationError)
                self.assertEqual(set(error.fields), {"reference"})
                self.assertIn(error.fields["reference"][0], ("invalid_reference", "invalid_type"))
        self.assertEqual((self.store.calls, self.db.events), ([], []))

    def test_an_invalid_value_is_a_field_error_that_never_contains_the_value(self):
        for bad in (*self.BAD_VALUES, None, 5, b"bytes", ["x"]):
            for replace in (False, True):
                with patched_credentials(self.db, administrator()):
                    function = services.replace_credential if replace else services.create_credential
                    error = self.failure_of(lambda: function(user="u", reference=REF, value=bad, store_factory=lambda: self.store))
                self.assertIsInstance(error, CredentialValidationError)
                self.assertEqual(set(error.fields), {"value"})
                self.assertIn(error.fields["value"][0], ("invalid_secret", "invalid_type", "too_long"))
                text = str(error) + repr(error) + "".join(traceback.format_exception(error))
                for fragment in ("leading", "trailing", "break", "bytes"):
                    self.assertNotIn(fragment, text.replace("invalid_secret", ""))
        self.assertEqual((self.store.calls, self.store.items, self.db.events), ([], {}, []))

    def test_the_value_is_accepted_exactly_as_given_with_no_trimming(self):
        self.admin("create")
        self.assertEqual(self.store.value_of(REF), SECRET_ONE)

    def test_a_valid_value_reaches_the_store_only_as_a_redacted_secret_value(self):
        seen = []
        real_put = self.store._put
        self.store._put = lambda reference, secret, replace: (seen.append(secret), real_put(reference, secret, replace))[1]
        self.admin("create")
        (secret,) = seen
        self.assertIsInstance(secret, SecretValue)
        self.assertNotIn(SECRET_ONE, repr(secret) + str(secret) + f"{secret}")


class OperationTests(CredentialCase):
    def test_create_stores_the_secret_and_reports_a_status_without_it(self):
        result = self.admin("create")
        self.assertEqual((result.reference, result.status, result.store, result.connections), (REF, "set", "memory", ()))
        self.assertEqual(self.store.value_of(REF), SECRET_ONE)
        self.assertNoLeak(repr(result))
        self.assertFalse(any(isinstance(value, SecretValue) for value in vars(result).values()))

    def test_create_never_overwrites_and_records_nothing_when_refused(self):
        self.store.seed(REF, SECRET_TWO)
        self.assertIsInstance(self.failure_of(lambda: self.admin("create")), CredentialAlreadyExistsError)
        self.assertEqual(self.store.value_of(REF), SECRET_TWO)
        self.assertEqual(self.db.events, [])
        self.assertTrue(self.db.rolled_back)

    def test_replace_changes_an_existing_secret_and_never_creates_one(self):
        self.store.seed(REF, SECRET_ONE)
        result = self.admin("replace")
        self.assertEqual((result.status, self.store.value_of(REF)), ("set", SECRET_TWO))
        self.assertIsInstance(self.failure_of(lambda: self.run_as(administrator(), "replace", store=MemoryStore())), CredentialNotFoundError)
        self.assertEqual(len(self.db.events), 1)

    def test_replace_of_a_missing_reference_creates_nothing_and_records_nothing(self):
        self.assertIsInstance(self.failure_of(lambda: self.admin("replace")), CredentialNotFoundError)
        self.assertEqual((self.store.items, self.db.events), ({}, []))

    def test_delete_removes_the_secret_and_a_second_delete_is_not_found(self):
        self.store.seed(REF, SECRET_ONE)
        result = self.admin("delete")
        self.assertEqual((result.status, result.connections, self.store.items), ("not_set", (), {}))
        self.assertIsInstance(self.failure_of(lambda: self.admin("delete")), CredentialNotFoundError)
        self.assertEqual(len(self.db.events), 1)

    def test_delete_is_refused_while_a_connection_reads_the_credential_and_changes_nothing(self):
        self.store.seed(REF, SECRET_ONE)
        using = Using({REF: ("corp-llm", "backup")})
        error = self.failure_of(lambda: self.admin("delete", using=using))
        self.assertIsInstance(error, CredentialInUseError)
        self.assertEqual(error.connections, ("corp-llm", "backup"))
        self.assertEqual((self.store.value_of(REF), self.db.events), (SECRET_ONE, []))
        self.assertEqual(using.locked, [True], "the settings row is locked while the decision is made")
        self.assertNotIn(("delete", REF), self.store.calls)

    def test_a_connection_that_reads_another_credential_does_not_block_a_delete(self):
        self.store.seed(REF, SECRET_ONE)
        self.admin("delete", using=Using({OTHER_REF: ("vendor",)}))
        self.assertEqual(self.store.items, {})

    def test_status_reports_set_not_set_and_unreadable_and_the_connections_that_use_it(self):
        self.assertEqual(self.admin("status").status, "not_set")
        self.store.seed(REF, SECRET_ONE)
        result = self.admin("status", using=Using({REF: ("corp-llm",)}))
        self.assertEqual((result.status, result.connections), ("set", ("corp-llm",)))
        unreadable = MemoryStore(fail={"get": CODE_UNREADABLE})
        self.assertEqual(self.admin("status", store=unreadable).status, "unreadable")
        self.assertEqual(self.db.events, [])  # a read is not audited

    def test_status_reads_the_secret_only_to_test_it_and_never_returns_it(self):
        self.store.seed(REF, SECRET_ONE)
        result = self.admin("status")
        self.assertNoLeak(repr(result), str(result), repr(vars(result)))
        self.assertEqual([call[0] for call in self.store.calls], ["get"])

    def test_references_are_independent(self):
        self.store.seed(OTHER_REF, SECRET_TWO)
        self.admin("create")
        self.admin("delete")
        self.assertEqual(self.store.value_of(OTHER_REF), SECRET_TWO)


class AuditTests(CredentialCase):
    def test_each_success_records_one_event_with_the_actor_an_opaque_subject_and_no_secret(self):
        self.admin("create")
        self.admin("replace")
        self.admin("delete")
        self.assertEqual([e.action for e in self.db.events], ["ai_credential.created", "ai_credential.replaced", "ai_credential.deleted"])
        for event in self.db.events:
            self.assertEqual((event.actor_kind, event.actor_public_id, event.subject_type), ("user", ADMIN_ID, "ai_settings"))
            self.assertEqual(event.subject_id, credential_subject_id(REF))
            self.assertEqual(event.details, {"store": "memory"})
            self.assertNoLeak(repr(event), str(event.details), str(event.subject_id))
            self.assertNotIn(REF, repr(event))

    def test_the_subject_id_is_stable_distinct_per_reference_and_does_not_contain_the_name(self):
        self.assertEqual(credential_subject_id(REF), credential_subject_id(REF))
        self.assertNotEqual(credential_subject_id(REF), credential_subject_id(OTHER_REF))
        self.assertNotIn("CORP", str(credential_subject_id(REF)).upper())

    def test_the_event_is_written_before_the_store_changes_and_inside_the_transaction(self):
        order = []

        class Watching(MemoryStore):
            def _put(inner, reference, secret, replace):
                order.append(("store", len(self.db.events), self.db.depth))
                return super()._put(reference, secret, replace)

        self.admin("create", store=Watching())
        self.assertEqual(order, [("store", 1, 1)])

    def test_a_refused_or_failed_operation_leaves_no_event(self):
        busy = MemoryStore(fail={"put": CODE_BUSY})
        self.assertIsInstance(self.failure_of(lambda: self.admin("create", store=busy)), CredentialStoreError)
        self.assertEqual(self.db.events, [])
        self.assertTrue(self.db.rolled_back)

    def test_a_failed_audit_write_stops_the_change(self):
        db = FakeStore(audit_error=RuntimeError("audit down"))
        store = MemoryStore()
        with patched_credentials(db, administrator()):
            self.failure_of(operations(store)["create"])
        self.assertEqual((store.items, [c for c in store.calls if c[0] == "put"]), ({}, []))

class TransactionSafetyTests(CredentialCase):
    """The secret store is not part of the database transaction, so the outcome of a failed commit is UNKNOWN: nothing may be deleted or overwritten to guess it, the original
    exception must propagate unchanged, and an operator must be told, in fixed words, that the outcome is uncertain."""

    LOGGER = "apps.ai_credentials.services"
    EXPECTED = {"create": {REF: SECRET_ONE}, "replace": {REF: SECRET_TWO}, "delete": {}}

    def fresh(self, name, store_class=MemoryStore, **kwargs):
        self.db = FakeStore()
        self.store = store_class(**kwargs)
        if name != "create":
            self.store.seed(REF, SECRET_ONE)
        return self.store

    def state(self):
        return {reference: secret.reveal() for reference, secret in self.store.items.items()}

    def deletes(self):
        return [call for call in self.store.calls if call[0] == "delete"]

    def assertFixedLine(self, line, verb):
        self.assertIn(f"AI credential {verb}: the secret store may have changed but the audit commit was not confirmed; reconcile", line)
        self.assertIn(f"actor={ADMIN_ID}", line)
        self.assertIn(f"subject={credential_subject_id(REF)}", line)
        for forbidden in (REF, *SENTINELS, "COMMIT", "rolled back at commit", "Database"):
            self.assertNotIn(forbidden, line)

    VERBS = {"create": "created", "replace": "replaced", "delete": "deleted"}

    def test_an_ambiguous_commit_keeps_the_event_and_the_store_change_and_deletes_nothing(self):
        for name in ("create", "replace", "delete"):
            with self.subTest(name):
                self.fresh(name)
                with self.assertLogs(self.LOGGER, "ERROR") as logged:
                    error = self.failure_of(lambda: self.admin(name, atomic=ambiguous_commit_of(self.db)))
                self.assertIsInstance(error, DatabaseError)
                self.assertEqual(self.state(), self.EXPECTED[name])
                self.assertEqual([e.action for e in self.db.events], [f"ai_credential.{self.VERBS[name]}"])  # the server committed it
                self.assertEqual(self.deletes(), [("delete", REF)] if name == "delete" else [])
                self.assertEqual(len(logged.records), 1)
                self.assertFixedLine(logged.output[0], self.VERBS[name])

    def test_a_commit_rolled_back_by_the_server_changes_nothing_further_and_is_logged_as_uncertain(self):
        for name in ("create", "replace", "delete"):
            with self.subTest(name):
                self.fresh(name)
                with self.assertLogs(self.LOGGER, "ERROR") as logged:
                    error = self.failure_of(lambda: self.admin(name, atomic=rolled_back_commit_of(self.db)))
                self.assertIsInstance(error, DatabaseError)
                self.assertEqual(self.state(), self.EXPECTED[name])  # the store keeps what it did: no compensation in either direction
                self.assertEqual(self.db.events, [])
                self.assertEqual(self.deletes(), [("delete", REF)] if name == "delete" else [])
                self.assertEqual(len(logged.records), 1)
                self.assertFixedLine(logged.output[0], self.VERBS[name])
                self.assertIsNone(logged.records[0].exc_info)  # the exception's own text (it carries a sentinel here) is never logged

    def test_the_original_exception_object_and_traceback_are_preserved(self):
        for name in ("create", "replace", "delete"):
            with self.subTest(name):
                self.fresh(name)
                original = DatabaseError("original " + SECRET_ONE)

                def make_atomic(db=self.db, original=original):
                    @contextlib.contextmanager
                    def atomic():
                        with db.atomic():
                            yield
                        raise original

                    return atomic

                with self.assertLogs(self.LOGGER, "ERROR"):
                    caught = self.failure_of(lambda: self.admin(name, atomic=make_atomic()))
                self.assertIs(caught, original)
                frames = [frame.name for frame in traceback.extract_tb(original.__traceback__)]
                self.assertIn("atomic", frames)  # where it was raised
                self.assertTrue({"_write", "delete_credential"} & set(frames))  # and through the service

    def test_base_exceptions_propagate_with_one_uncertain_log_and_no_compensation(self):
        for exception in (KeyboardInterrupt, SystemExit):
            for name in ("create", "replace", "delete"):
                with self.subTest(exception=exception.__name__, name=name):
                    self.fresh(name)
                    with self.assertLogs(self.LOGGER, "ERROR") as logged:
                        error = self.failure_of(lambda: self.admin(name, atomic=interrupted_commit_of(self.db, exception)))
                    self.assertIsInstance(error, exception)
                    self.assertEqual(self.state(), self.EXPECTED[name])
                    self.assertEqual(self.deletes(), [("delete", REF)] if name == "delete" else [])
                    self.assertEqual(len(logged.records), 1)
                    self.assertFixedLine(logged.output[0], self.VERBS[name])

    def test_an_interruption_during_the_store_call_is_uncertain_but_a_refusal_is_definite(self):
        class Interrupted(MemoryStore):
            def _put(self, reference, secret, replace):
                raise KeyboardInterrupt()

            def _delete(self, reference):
                raise KeyboardInterrupt()

        for name in ("create", "replace", "delete"):
            with self.subTest(name):
                self.fresh(name, Interrupted)
                with self.assertLogs(self.LOGGER, "ERROR") as logged:
                    self.assertIsInstance(self.failure_of(lambda: self.admin(name)), KeyboardInterrupt)
                self.assertFixedLine(logged.output[0], self.VERBS[name])
        for code in (CODE_BUSY, CODE_UNAVAILABLE, CODE_DISABLED):  # a definite refusal fails before any change
            for name in ("create", "replace", "delete"):
                with self.subTest(code=code, name=name), self.assertNoLogs(self.LOGGER, "ERROR"):
                    self.fresh(name, MemoryStore, fail={"put": code, "delete": code})
                    self.assertIsInstance(self.failure_of(lambda: self.admin(name)), CredentialStoreError)
                self.assertEqual(self.db.events, [])

    def test_every_failure_that_precedes_the_store_change_is_definite_and_logs_nothing_uncertain(self):
        cases = (
            lambda: self.run_as(actor(is_superuser=False), "create"),
            lambda: self.run_as(administrator(), "replace", store=MemoryStore()),  # not found
            lambda: self.run_as(administrator(), "create", store=MemoryStore().seed(REF, SECRET_ONE)),  # already exists
            lambda: self.run_as(administrator(), "delete", store=MemoryStore().seed(REF, SECRET_ONE), using=Using({REF: ("corp-llm",)})),  # in use
            lambda: self.run_as(administrator(), "create", using=Using(fail=DatabaseError("read failed"))),
        )
        for index, call in enumerate(cases):
            with self.subTest(index), self.assertNoLogs(self.LOGGER, "ERROR"), mock.patch("apps.accounts.authz.logger"):
                self.db = FakeStore()
                self.failure_of(call)
        db = FakeStore(audit_error=RuntimeError("audit down"))
        with patched_credentials(db, administrator()), self.assertNoLogs(self.LOGGER, "ERROR"):
            self.failure_of(operations(MemoryStore())["create"])

    def test_a_failing_log_never_replaces_the_original_exception(self):
        self.fresh("replace")
        with mock.patch.object(services.logger, "error", side_effect=RuntimeError("log down")):
            error = self.failure_of(lambda: self.admin("replace", atomic=ambiguous_commit_of(self.db)))
        self.assertIsInstance(error, DatabaseError)

    def test_nothing_in_the_services_deletes_or_overwrites_a_credential_to_compensate(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(services.__file__).read_text(encoding="utf-8"))
        owners = {}
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "store" and node.attr in ("put", "delete"):
                    owners.setdefault(function.name, []).append(node.attr)
        self.assertEqual(owners, {"_write": ["put"], "delete_credential": ["delete"]})  # only the requested operation; no ``except`` branch calls the store

    def test_the_lock_key_is_stable_per_reference_and_different_between_references(self):
        keys = {reference: credential_lock_key(reference) for reference in (REF, OTHER_REF, "AP_AI_CRED_A", "AP_AI_CRED_B", "AP_AI_CRED_" + "X" * 48)}
        self.assertEqual(len(set(keys.values())), len(keys))
        self.assertEqual(credential_lock_key(REF), keys[REF])
        for key in keys.values():
            self.assertTrue(-(2**63) <= key < 2**63)
        self.assertNotIn("CORP", str(keys[REF]))


class LockOrderingTests(CredentialCase):
    def traced(self, name, **kwargs):
        trace = []

        class Tracing(MemoryStore):
            def _put(self, reference, secret, replace):
                trace.append("store:put")
                return super()._put(reference, secret, replace)

            def _delete(self, reference):
                trace.append("store:delete")
                return super()._delete(reference)

        store = Tracing()
        if name != "create":
            store.seed(REF, SECRET_ONE)
        with patched_credentials(self.db, administrator(), trace=trace, **kwargs):
            try:
                operations(store)[name]()
            except Exception:
                pass
        return trace

    def test_the_reference_lock_is_taken_inside_the_transaction_before_the_audit_and_the_store(self):
        for name, settings_read, store_call in (("create", "lock=False", "store:put"), ("replace", "lock=False", "store:put"), ("delete", "lock=True", "store:delete")):
            with self.subTest(name):
                self.db = FakeStore()
                trace = self.traced(name)
                self.assertEqual(trace, ["begin", "advisory", f"settings_read:{settings_read}", "audit", store_call, "commit"])
                ((sql, params, depth, events_before),) = self.db.connection.executed
                self.assertEqual((sql, params, events_before), ("SELECT pg_advisory_xact_lock(%s)", (credential_lock_key(REF),), 0))
                self.assertGreaterEqual(depth, 1)  # inside the transaction: outside one the lock would be released at once

    def test_a_delete_takes_the_reference_lock_before_the_settings_row_lock_and_never_the_reverse(self):
        self.db = FakeStore()
        trace = self.traced("delete")
        self.assertLess(trace.index("advisory"), trace.index("settings_read:lock=True"))
        # a settings save takes only the row lock and no advisory lock, so no operation waits for a lock another holds while holding one that one needs: no cycle
        self.assertEqual(sum(1 for item in trace if item == "advisory"), 1)

    def test_an_operation_refused_before_the_transaction_takes_no_lock(self):
        for who, name in ((actor(is_superuser=False), "create"), (actor(is_superuser=False), "delete"), (administrator(), "status")):
            with self.subTest(name=name), mock.patch("apps.accounts.authz.logger"):
                self.db = FakeStore()
                trace = self.traced(name) if who.is_superuser else []
                if not who.is_superuser:
                    with patched_credentials(self.db, who, trace=trace):
                        self.failure_of(operations(MemoryStore())[name])
                self.assertEqual(self.db.connection.executed, [])
                self.assertNotIn("begin", trace)

    def test_an_in_use_refusal_stops_after_the_locks_and_writes_nothing(self):
        self.db = FakeStore()
        trace = []
        store = MemoryStore().seed(REF, SECRET_ONE)
        with patched_credentials(self.db, administrator(), using=Using({REF: ("corp-llm",)}), trace=trace):
            self.failure_of(operations(store)["delete"])
        self.assertEqual(trace, ["begin", "advisory", "settings_read:lock=True"])
        self.assertEqual((self.db.events, store.calls), ([], []))

    def test_concurrent_rotations_of_one_reference_are_serialized_and_other_references_are_independent(self):
        locks, trace = AdvisoryLocks(), []
        first, second, third = (uuid.UUID(int=n) for n in (1, 2, 3))
        by_user = {"one": actor(is_superuser=True, public_id=first), "two": actor(is_superuser=True, public_id=second), "three": actor(is_superuser=True, public_id=third)}
        store = MemoryStore().seed(REF, SECRET_ONE).seed(OTHER_REF, SECRET_ONE)
        in_put, release, written = threading.Event(), threading.Event(), []
        real_put = store._put

        def slow_put(reference, secret, replace):
            written.append((reference, secret.reveal()))
            if secret.reveal().endswith("-first"):
                in_put.set()
                release.wait(10)  # the first administrator is inside the store call, holding the reference's lock
            return real_put(reference, secret, replace)

        store._put = slow_put
        errors = []

        def admin(user, reference, value):
            try:
                services.replace_credential(user=user, reference=reference, value=value, store_factory=lambda: store)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        with patched_credentials(self.db, lambda user: by_user[user], trace=trace, locks=locks):
            one = threading.Thread(target=admin, args=("one", REF, SECRET_ONE + "-first"))
            one.start()
            self.assertTrue(in_put.wait(10))
            two = threading.Thread(target=admin, args=("two", REF, SECRET_TWO + "-second"))
            two.start()
            other = threading.Thread(target=admin, args=("three", OTHER_REF, SECRET_TWO + "-other"))
            other.start()
            other.join(5)
            self.assertFalse(other.is_alive(), "another reference must not wait for this one")
            two.join(0.5)
            self.assertTrue(two.is_alive(), "the same reference must wait")
            self.assertEqual([e.actor_public_id for e in self.db.events], [first, third])  # the second administrator has not even written its audit event
            self.assertEqual(written, [(REF, SECRET_ONE + "-first"), (OTHER_REF, SECRET_TWO + "-other")])
            release.set()
            one.join(10)
            two.join(10)
        self.assertEqual(errors, [])
        self.assertEqual([e.actor_public_id for e in self.db.events], [first, third, second])
        self.assertEqual([r for r, _ in written if r == REF], [REF, REF])
        self.assertEqual(written[-1], (REF, SECRET_TWO + "-second"))  # audit order = store write order for the one reference
        self.assertEqual(store.value_of(REF), SECRET_TWO + "-second")


class PostCommitTests(CredentialCase):
    def test_no_database_read_happens_after_the_commit_so_a_committed_success_cannot_become_an_error(self):
        for name in ("create", "replace", "delete"):
            with self.subTest(name):
                self.db, trace = FakeStore(), []
                using = Using({REF: ("corp-llm",)} if name != "delete" else {})
                store = MemoryStore().seed(REF, SECRET_ONE) if name != "create" else MemoryStore()
                with patched_credentials(self.db, administrator(), using=using, trace=trace):
                    result = operations(store)[name]()
                self.assertEqual(trace[-1], "commit")  # nothing touches the database after it
                self.assertTrue(all(depth >= 1 for depth in using.depths), using.depths)  # every read was inside the transaction
                self.assertEqual(result.reference, REF)

    def test_the_response_values_are_those_of_before_the_change(self):
        for name, status in (("create", "set"), ("replace", "set")):
            self.db = FakeStore()
            store = MemoryStore().seed(REF, SECRET_ONE) if name == "replace" else MemoryStore()
            with patched_credentials(self.db, administrator(), using=Using({REF: ("corp-llm", "backup")})):
                result = operations(store)[name]()
            self.assertEqual((result.status, result.connections, result.store), (status, ("corp-llm", "backup"), "memory"))

    def test_a_failing_read_stops_the_operation_before_anything_changes(self):
        for name in ("create", "replace"):
            with self.subTest(name), self.assertNoLogs("apps.ai_credentials.services", "ERROR"):
                self.db = FakeStore()
                store = MemoryStore().seed(REF, SECRET_ONE) if name == "replace" else MemoryStore()
                with patched_credentials(self.db, administrator(), using=Using(fail=DatabaseError("db went away"))):
                    error = self.failure_of(operations(store)[name])
                self.assertIsInstance(error, DatabaseError)
                self.assertEqual((store.calls, self.db.events), ([], []))
                self.assertEqual(store.items.get(REF).reveal() if REF in store.items else None, SECRET_ONE if name == "replace" else None)


class SecretStoreFailureTests(CredentialCase):
    def test_a_disabled_store_fails_closed_for_every_operation(self):
        for name in ("status", "create", "replace", "delete"):
            with self.subTest(name):
                error = self.failure_of(lambda: self.admin(name, store=DisabledSecretStore()))
                self.assertIsInstance(error, CredentialStoreError)
                self.assertEqual(error.reason, CODE_DISABLED)
        self.assertEqual(self.db.events, [])

    def test_each_store_failure_is_its_stable_code_and_changes_nothing(self):
        for code in (CODE_UNAVAILABLE, CODE_BUSY, CODE_DISABLED, CODE_UNREADABLE):
            for operation, name in (("put", "create"), ("get", "status"), ("delete", "delete")):
                self.store = MemoryStore(fail={operation: code}).seed(REF, SECRET_ONE) if name == "delete" else MemoryStore(fail={operation: code})
                with self.subTest(code=code, operation=name):
                    if name == "status" and code == CODE_UNREADABLE:
                        continue  # a status, not an error
                    error = self.failure_of(lambda: self.admin(name))
                    self.assertIsInstance(error, CredentialStoreError)
                    self.assertEqual(error.reason, code)
        self.assertEqual(self.db.events, [])

    def test_a_store_that_cannot_be_built_is_unavailable_and_is_not_replaced_by_anything(self):
        def broken():
            raise SecretStoreError(CODE_UNAVAILABLE)

        for name in ("status", "create", "replace", "delete"):
            with self.subTest(name), patched_credentials(self.db, administrator()):
                function = {
                    "status": lambda: services.get_status(user="u", reference=REF, store_factory=broken),
                    "create": lambda: services.create_credential(user="u", reference=REF, value=SECRET_ONE, store_factory=broken),
                    "replace": lambda: services.replace_credential(user="u", reference=REF, value=SECRET_ONE, store_factory=broken),
                    "delete": lambda: services.delete_credential(user="u", reference=REF, store_factory=broken),
                }[name]
                error = self.failure_of(function)
            self.assertEqual((type(error), error.reason), (CredentialStoreError, CODE_UNAVAILABLE))
        self.assertEqual(self.db.events, [])

    def test_the_environment_is_never_a_secret_source_or_destination(self):
        with mock.patch.dict(os.environ, {REF: ENV_SENTINEL, "AP_AI_OPENAI_API_KEY": ENV_SENTINEL}):
            self.assertEqual(self.admin("status").status, "not_set")  # an environment variable of that name does not make it "set"
            self.admin("create")
            self.assertEqual(self.store.value_of(REF), SECRET_ONE)
            self.admin("replace")
            self.assertEqual(os.environ[REF], ENV_SENTINEL)  # nothing was written there
            self.admin("delete")
            self.assertEqual(self.admin("status").status, "not_set")  # and it does not reappear from the environment
        self.assertNoLeak()


class RedactionTests(CredentialCase):
    def test_no_error_log_line_or_event_holds_a_secret_a_reference_or_a_path(self):
        texts = []
        cases = [
            lambda: self.admin("create", store=MemoryStore(fail={"put": CODE_UNAVAILABLE})),
            lambda: self.admin("create", store=DisabledSecretStore()),
            lambda: self.run_as(actor(is_superuser=False), "create"),
            lambda: self.admin("replace"),
            lambda: self.admin("delete"),
        ]
        for call in cases:
            error = self.failure_of(call)
            texts += [str(error), repr(error), "".join(traceback.format_exception(error))]
        self.admin("create")
        self.admin("replace")
        self.admin("delete")
        self.assertNoLeak(*texts)
        for text in texts + self.capture.lines:
            self.assertNotIn(REF, text)

    def test_success_logs_carry_only_identifiers(self):
        self.admin("create")
        lines = [line for line in self.capture.lines if "AI credential" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(str(ADMIN_ID), lines[0])
        self.assertIn(str(credential_subject_id(REF)), lines[0])
        self.assertNotIn(REF, lines[0])
