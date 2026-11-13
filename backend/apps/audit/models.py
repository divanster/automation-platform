"""Persistence for audit events (docs/architecture/data-model.md, AuditEvent).

APPEND-ONLY AT THE ORM AND SERVICE BOUNDARY ONLY. ``AuditEvent`` uses ``apps.cases.models.ImmutableRecord``:
``save()`` on an existing row, ``delete()``, ``QuerySet.update()``, ``QuerySet.delete()``, ``bulk_update()``
and upserts raise ``ImmutableRecordError``. There is NO database-level protection: no trigger and no
revoked UPDATE or DELETE privilege on the table. Raw SQL, ``QuerySet.raw()``, private ORM internals, a
database administrator, and a migration can change or remove events, and nothing here detects it (there is
no hash chain). The data model's proposal of an insert-and-select-only runtime database role is NOT
implemented (open question 2.5 and 2.18).

Rows carry identifiers, state names, counts, hashes and reason codes only (see ``apps.audit.events``).
``actor_public_id`` is ``User.public_id``; there is no foreign key to the user, so an event outlives its
user, and ``case_id`` and ``run_id`` are plain UUIDs for the same reason. An event is written by
``apps.audit.recorder.record_event`` inside the transaction of the operation it describes.
"""

from django.db import models
from django.db.models import Q

from apps.cases.models import ImmutableRecord

from .events import ACTIONS, ACTOR_KINDS, ACTOR_SYSTEM, ACTOR_USER, SUBJECT_TYPES


class AuditEvent(ImmutableRecord):
    id = models.BigAutoField(primary_key=True)  # increases with every insert: the order events were written in
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor_kind = models.CharField(max_length=8)
    actor_public_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=32)
    subject_type = models.CharField(max_length=16)
    subject_id = models.UUIDField()
    case_id = models.UUIDField(null=True, blank=True)
    run_id = models.UUIDField(null=True, blank=True)
    details = models.JSONField(default=dict)

    class Meta:
        default_permissions = ("view",)  # no add, change or delete permission exists for audit events
        indexes = [models.Index(fields=["run_id", "id"], name="audit_event_run_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(action__in=list(ACTIONS)), name="audit_event_action_valid"),
            models.CheckConstraint(condition=Q(subject_type__in=list(SUBJECT_TYPES)), name="audit_event_subject_type_valid"),
            models.CheckConstraint(condition=Q(actor_kind__in=list(ACTOR_KINDS)), name="audit_event_actor_kind_valid"),
            models.CheckConstraint(
                condition=(
                    (Q(actor_kind=ACTOR_USER) & Q(actor_public_id__isnull=False))
                    | (Q(actor_kind=ACTOR_SYSTEM) & Q(actor_public_id__isnull=True))
                ),
                name="audit_event_actor_matches_kind",
            ),
        ]

    def __str__(self):
        return f"AuditEvent {self.pk}"
