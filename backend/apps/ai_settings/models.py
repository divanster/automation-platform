"""The one row of AI extraction settings.

A CURRENT-STATE row: ``revision`` increases with every save (optimistic concurrency for the API), ``updated_at`` and ``updated_by_public_id`` say when and who. The
history is in ``apps.audit``: every successful save writes one append-only ``ai_settings.updated`` event in the same transaction (``apps.ai_settings.services``; the
event's subject id is ``SETTINGS_SUBJECT_ID``). Audit immutability is ORM-level only (see ``apps.audit.models``).

NO SECRET. No column can hold a credential, and none is ever written: the provider's key stays in the process environment. ``connections`` holds only the
configuration of the named connections (type, locality, endpoint, model names, enabled flag) and, for a connection that needs a key, a credential REFERENCE: the
NAME of a backend environment variable (``AP_AI_CRED_*``), never its value. The CHECK constraints below are
the database's last line of defence for the ALL-OR-NOTHING rule of ``apps.ai_settings.rules`` (deterministic: no AI field; ai: all of them, ``connection_id``
excepted: no connection means the legacy, environment-configured provider); which providers,
profiles and policies are acceptable is the composition root's catalog, enforced in Python by the API and again by the worker at start-up. Raw SQL or a
migration can store any shape the constraints allow and nothing detects it. The connections live in this ONE row so that they share its revision, its row lock and
its transaction with the selection: a save changes both or neither.
"""

import uuid

from django.db import models
from django.db.models import Q

from .rules import MODE_AI, MODE_DETERMINISTIC, MODES

SINGLETON_ID = 1
SETTINGS_SUBJECT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "urn:automation-platform:ai-settings:extraction")  # the audit subject id of the singleton row: a fixed constant


def connection_subject_id(connection_id: str) -> uuid.UUID:
    """The audit subject id of one connection: derived from its identifier, so it is stable and needs no row of its own."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:automation-platform:ai-settings:connection:{connection_id}")


class AIExtractionSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_ID)
    revision = models.PositiveIntegerField(default=1)
    extraction_mode = models.CharField(max_length=16, default=MODE_DETERMINISTIC)
    provider_id = models.CharField(max_length=64, null=True, blank=True)
    connection_id = models.CharField(max_length=64, null=True, blank=True)  # the selected stored connection; null: the legacy environment-configured provider
    profile_id = models.CharField(max_length=64, null=True, blank=True)
    policy_mode = models.CharField(max_length=32, null=True, blank=True)
    timeout_seconds = models.PositiveSmallIntegerField(null=True, blank=True)
    connections = models.JSONField(default=list, blank=True)  # a list of connection objects (``rules.ConnectionValues.as_mapping``); no secret
    updated_at = models.DateTimeField(auto_now=True)
    updated_by_public_id = models.UUIDField(null=True, blank=True)  # User.public_id; no foreign key, so the row outlives the user

    class Meta:
        default_permissions = ()  # nothing is granted through Django's model permissions; the service checks for an administrator
        constraints = [
            models.CheckConstraint(condition=Q(id=SINGLETON_ID), name="ai_settings_singleton"),
            models.CheckConstraint(condition=Q(extraction_mode__in=list(MODES)), name="ai_settings_mode_valid"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ai_settings_revision_positive"),
            models.CheckConstraint(
                condition=(
                    Q(extraction_mode=MODE_DETERMINISTIC, provider_id__isnull=True, connection_id__isnull=True, profile_id__isnull=True, policy_mode__isnull=True, timeout_seconds__isnull=True)
                    | Q(extraction_mode=MODE_AI, provider_id__isnull=False, profile_id__isnull=False, policy_mode__isnull=False, timeout_seconds__isnull=False)
                ),
                name="ai_settings_all_or_nothing_v2",
            ),
        ]

    def __str__(self):
        return f"AI extraction settings (revision {self.revision})"
