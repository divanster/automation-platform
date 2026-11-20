"""Helpers for the DB-free tests of the managed AI credentials (AI-SEC-2): an in-memory secret store that records what was asked of it, and the services' database touch points replaced.

``apps.ai_credentials.services`` is exercised for real; only ``load_actor`` (a database read), ``record_event`` and ``transaction`` (an audit write in a simulated atomic block with rollback,
reused from ``tests/ai_settings_support.py``) and ``connections_using_managed_reference`` (a read of the settings row) are replaced. What needs a real database (the row lock, a true rollback,
the audit table) is in ``tests/test_ai_credentials_postgres.py`` and is NOT run in the development VM. Every secret here is a sentinel.
"""

import contextlib
from unittest import mock

from django.db import DatabaseError

from apps.ai_credentials import services
from apps.secret_store import CODE_ALREADY_EXISTS, CODE_NOT_FOUND, SecretStore, SecretStoreError, SecretValue
from tests.ai_settings_support import ADMIN_ID, FakeStore, actor  # noqa: F401  (re-exported for the tests)

SECRET_ONE = "CRED-SENTINEL-ONE-5d1f"
SECRET_TWO = "CRED-SENTINEL-TWO-a82c"
ENV_SENTINEL = "ENV-SENTINEL-0b7e"
REF = "AP_AI_CRED_CORP_LLM"
OTHER_REF = "AP_AI_CRED_VENDOR"
SENTINELS = (SECRET_ONE, SECRET_TWO, ENV_SENTINEL)


class MemoryStore(SecretStore):
    """A secret store in a dict that records every operation. ``fail`` maps an operation name to the code it raises with (``unavailable``, ``busy``...)."""

    backend_id = "memory"

    def __init__(self, fail=None):
        self.items = {}
        self.calls = []
        self.fail = dict(fail or {})

    def _maybe_fail(self, operation):
        code = self.fail.get(operation)
        if code is not None:
            raise SecretStoreError(code)

    def _put(self, reference, secret, replace):
        self.calls.append(("put", reference, replace))
        self._maybe_fail("put")
        present = reference in self.items
        if replace and not present:
            raise SecretStoreError(CODE_NOT_FOUND)
        if not replace and present:
            raise SecretStoreError(CODE_ALREADY_EXISTS)
        self.items[reference] = secret

    def _get(self, reference):
        self.calls.append(("get", reference))
        self._maybe_fail("get")
        try:
            return self.items[reference]
        except KeyError:
            raise SecretStoreError(CODE_NOT_FOUND) from None

    def _delete(self, reference):
        self.calls.append(("delete", reference))
        self._maybe_fail("delete")
        if reference not in self.items:
            raise SecretStoreError(CODE_NOT_FOUND)
        del self.items[reference]

    def _exists(self, reference):
        self.calls.append(("exists", reference))
        self._maybe_fail("exists")
        return reference in self.items

    def seed(self, reference, value):
        self.items[reference] = SecretValue(value)
        return self

    def value_of(self, reference):
        return self.items[reference].reveal()


class Using:
    """Stand-in for ``connections_using_managed_reference``: the identifiers that read a reference, whether the settings row was locked, and at which transaction depth it was asked
    (``depths``: 0 would be a read outside any transaction). ``fail`` makes it raise (a database that went away)."""

    def __init__(self, mapping=None, fail=None):
        self.mapping = dict(mapping or {})
        self.locked = []
        self.depths = []
        self.fail = fail
        self.db = None  # set by ``patched_credentials``
        self.trace = None

    def __call__(self, reference, *, lock=False):
        self.locked.append(lock)
        self.depths.append(0 if self.db is None else self.db.depth)
        if self.trace is not None:
            self.trace.append(f"settings_read:lock={lock}")
        if self.fail is not None:
            raise self.fail
        return tuple(self.mapping.get(reference, ()))


class FakeConnection:
    """Stand-in for ``django.db.connection``: records every statement the services run on it (the advisory lock), with the transaction depth and the number of audit events written so far."""

    def __init__(self, db, trace=None, locks=None):
        self.db, self.trace, self.locks = db, trace, locks
        self.executed = []

    @contextlib.contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=None):
        params = tuple(params or ())
        self.executed.append((sql, params, self.db.depth, len(self.db.events)))
        if self.trace is not None:
            self.trace.append("advisory")
        if self.locks is not None:
            self.locks.acquire(params[0])


class AdvisoryLocks:
    """A stand-in for PostgreSQL transaction-level advisory locks between THREADS: ``acquire(key)`` blocks while another thread holds the key; ``release_all()`` (the end of the
    transaction) frees every key the calling thread holds."""

    def __init__(self):
        import threading

        self._guard = threading.Condition()
        self._held = {}
        self._mine = threading.local()

    def acquire(self, key):
        import threading

        me = threading.get_ident()
        with self._guard:
            if not self._guard.wait_for(lambda: self._held.get(key) in (None, me), timeout=10):
                raise AssertionError("an advisory lock was never released")
            self._held[key] = me
            self._mine.__dict__.setdefault("keys", set()).add(key)

    def release_all(self):
        with self._guard:
            for key in list(self._mine.__dict__.get("keys", ())):
                self._held.pop(key, None)
            self._mine.__dict__["keys"] = set()
            self._guard.notify_all()


def ambiguous_commit_of(db):
    """An atomic block (the simulated transaction of ``db``) whose body succeeds, whose COMMIT is applied by the server, and whose acknowledgement is then lost: the caller sees an error but
    the events stay committed."""

    @contextlib.contextmanager
    def atomic():
        with db.atomic():
            yield
        raise DatabaseError("connection lost during COMMIT " + SECRET_ONE)

    return atomic


def rolled_back_commit_of(db):
    """An atomic block whose COMMIT fails and is rolled back by the server: the caller sees an error and the events are gone."""

    @contextlib.contextmanager
    def atomic():
        with db.atomic():
            yield
            raise DatabaseError("rolled back at commit " + SECRET_ONE)

    return atomic


def interrupted_commit_of(db, exception=KeyboardInterrupt):
    """An atomic block interrupted (``KeyboardInterrupt``, ``SystemExit``...) right before the commit: the transaction is rolled back."""

    @contextlib.contextmanager
    def atomic():
        with db.atomic():
            yield
            raise exception()

    return atomic


@contextlib.contextmanager
def patched_credentials(db, who, using=None, atomic=None, trace=None, locks=None):
    """Run with the services' database touch points replaced. ``db`` is a ``FakeStore`` (events and the simulated transaction), ``who`` the Actor ``load_actor`` returns or an exception.
    ``trace`` (a list) receives ``advisory``, ``settings_read:lock=...``, ``audit``, ``begin`` and ``commit`` in the order they happen; ``locks`` (``AdvisoryLocks``) makes the advisory
    lock real between threads and is released when the transaction ends."""
    lookup = mock.Mock(side_effect=who) if isinstance(who, BaseException) or callable(who) else mock.Mock(return_value=who)
    reader = Using() if using is None else using
    reader.db, reader.trace = db, trace
    db.connection = FakeConnection(db, trace, locks)
    base_atomic = db.atomic if atomic is None else atomic

    @contextlib.contextmanager
    def traced_atomic():
        if trace is not None:
            trace.append("begin")
        try:
            with base_atomic():
                yield
        finally:
            if locks is not None:
                locks.release_all()
        if trace is not None:
            trace.append("commit")

    def traced_record(event):
        if trace is not None:
            trace.append("audit")
        return db.record_event(event)

    with (
        mock.patch.object(services, "load_actor", lookup),
        mock.patch.object(services, "record_event", traced_record),
        mock.patch.object(services, "transaction") as tx,
        mock.patch.object(services, "connection", db.connection),
        mock.patch.object(services, "connections_using_managed_reference", reader),
    ):
        tx.atomic.side_effect = traced_atomic
        yield lookup
