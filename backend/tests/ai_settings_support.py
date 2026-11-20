"""Helpers for the DB-free tests of the AI settings services and API: an in-memory stand-in for the one settings row and for the actor lookup.

``apps.ai_settings.services`` is exercised for real; only its database touch points are replaced: ``load_actor`` (a database read of the user), ``AIExtractionSettings``
(the row), ``record_event`` (the audit write) and ``transaction`` (a simulated atomic block with rollback). The behaviour that needs a real database (the CHECK constraints,
row locking, the migrations, a true rollback) is in ``tests/test_ai_settings_postgres.py`` and is NOT run in the development VM.
"""

import contextlib
import copy
import uuid
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError

from apps.accounts import roles
from apps.accounts.authz import Actor
from apps.ai_settings import services
from apps.audit.events import NewAuditEvent
from apps.audit.recorder import AuditTransactionError

ADMIN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.saved_with = None

    def save(self, update_fields=None):
        self.saved_with = update_fields


class FakeManager:
    def __init__(self, store):
        self.store = store

    def select_for_update(self):
        self.store.locked = True
        return self

    def filter(self, pk):
        return SimpleNamespace(first=lambda: self.store.row if pk == 1 else None)

    def create(self, **fields):
        if self.store.row is not None or self.store.race:
            raise IntegrityError("duplicate")
        self.store.row = Row(updated_at=None, **fields)
        return self.store.row


class FakeStore:
    """The one row (or none) and the audit events written in the same (simulated) transaction.

    ``race``: another administrator inserts first, so our insert hits the primary key. ``audit_error``: the audit write fails. ``atomic()`` stands in for
    ``transaction.atomic``: when the OUTERMOST block ends with an exception the row is restored to what it was at the start and the events written in the
    block are dropped, which is what a real rollback does (the real behaviour is in ``tests/test_ai_settings_postgres.py``, NOT run in the VM).
    """

    def __init__(self, row=None, race=False, audit_error=None):
        self.row = row
        self.race = race
        self.audit_error = audit_error
        self.locked = False
        self.events = []  # committed audit events
        self.depth = 0
        self.rolled_back = False
        self.model = SimpleNamespace(objects=FakeManager(self))

    @contextlib.contextmanager
    def atomic(self):
        outermost = self.depth == 0
        if outermost:
            saved_row, saved_events = copy.copy(self.row), list(self.events)
        self.depth += 1
        try:
            yield
        except BaseException:
            if outermost:
                self.row, self.events, self.rolled_back = saved_row, saved_events, True
            raise
        finally:
            self.depth -= 1

    def record_event(self, event):
        """Stand-in for ``apps.audit.recorder.record_event``: refuses to run outside a transaction, like the real one."""
        if not isinstance(event, NewAuditEvent):
            raise TypeError("event must be a NewAuditEvent")
        if self.depth == 0:
            raise AuditTransactionError("An audit event must be recorded inside the transaction of the operation it describes.")
        if self.audit_error is not None:
            raise self.audit_error
        self.events.append(event)


def stored_row(revision=1, mode="deterministic", provider=None, profile=None, policy=None, timeout=None):
    return Row(revision=revision, extraction_mode=mode, provider_id=provider, profile_id=profile, policy_mode=policy, timeout_seconds=timeout, updated_at=None, updated_by_public_id=ADMIN_ID)


def actor(is_superuser=False, public_id=ADMIN_ID, permissions=frozenset()):
    return Actor(user=object(), pk=1, public_id=public_id, is_superuser=is_superuser, permissions=frozenset(permissions))


ALL_ROLE_PERMISSIONS = roles.ALL_PERMISSIONS  # every permission any role holds: none of them makes an administrator


@contextlib.contextmanager
def patched_services(store, who):
    """Run with the services' database touch points replaced. ``who`` is the Actor ``load_actor`` returns, or an exception to raise."""
    lookup = mock.Mock(side_effect=who) if isinstance(who, BaseException) else mock.Mock(return_value=who)
    with (
        mock.patch.object(services, "load_actor", lookup),
        mock.patch.object(services, "AIExtractionSettings", store.model),
        mock.patch.object(services, "record_event", store.record_event),
        mock.patch.object(services, "transaction") as tx,
    ):
        tx.atomic.side_effect = store.atomic
        yield lookup
