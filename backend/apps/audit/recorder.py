"""Write audit events, always inside the transaction of the operation they describe.

``record_event`` refuses to run outside ``transaction.atomic()``: an event written on its own could survive
an operation that was rolled back (an event for something that did not happen), or be lost when the
operation commits. Inside the caller's transaction the event commits with the operation or is rolled back
with it; there is no way to have one without the other. A failed insert (for example a constraint) raises
and so rolls the whole operation back, by design.

Refused and failed attempts are NOT audited: their transaction is rolled back, so an event written in it
would be too. Refusals are logged by code only (``apps.accounts.authz``), never with content.
"""

import uuid

from django.db import connection

from .events import NewAuditEvent
from .models import AuditEvent


class AuditTransactionError(RuntimeError):
    """An audit event was recorded outside a database transaction."""


def record_event(event: NewAuditEvent) -> AuditEvent:
    if not isinstance(event, NewAuditEvent):
        raise TypeError("event must be an apps.audit.events.NewAuditEvent")
    if not connection.in_atomic_block:
        raise AuditTransactionError("An audit event must be recorded inside the transaction of the operation it describes.")
    return AuditEvent.objects.create(
        actor_kind=event.actor_kind,
        actor_public_id=event.actor_public_id,
        action=event.action,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        case_id=event.case_id,
        run_id=event.run_id,
        details=event.details,
    )


def list_run_events(run_id: uuid.UUID) -> list[AuditEvent]:
    """Every event of the run, in the order written."""
    return list(AuditEvent.objects.filter(run_id=run_id).order_by("id"))
