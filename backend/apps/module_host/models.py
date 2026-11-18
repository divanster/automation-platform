"""Persistent module state (MOD-1b S4): ``ModuleState`` and the append-only ``ModuleNamespaceClaim``.

``apps.modules`` is a pure package and gains no models; this separate Django app holds what must survive a restart. Nothing here
decides anything: the rules are ``apps.module_host.rules`` and every change goes through ``apps.module_host.services``.

WHERE THE GUARANTEES ARE ENFORCED. The database enforces what is declared below: a unique module id and namespace, and the SHAPE of ids, versions
and fingerprints. Everything else is the ORM and service boundary only, as for the audit and case records (``apps.cases.models``):

* ``ModuleState`` rows are never deleted through the ORM (``delete()`` and ``QuerySet.delete()`` raise ``ImmutableRecordError``); the
  ``enabled`` flag and the bookkeeping columns are changed only by the services, under a row lock. Disabling keeps the row, so the history
  of a module's state is never lost.
* ``ModuleNamespaceClaim`` is append-only (``ImmutableRecord``): a claim is inserted once and is never updated, reassigned or deleted, and
  a namespace can be claimed once (unique). Which module owns a namespace therefore cannot change without leaving the ORM.

``ModuleHostMarker`` is the durable "the first module sync has completed" record: one row, written by the sync in its own transaction, append-only through the
ORM (never updated, reset or deleted) and pinned to a single row by the database (id is always 1). The worker gate and the status read it instead of guessing
from the presence of ``ModuleState`` rows.

NOT PROTECTED: raw SQL, a migration, a database administrator and private ORM internals can change or remove these rows, and nothing here
detects it (no trigger, no revoked privilege). Code admitted to the process is not sandboxed (docs/architecture/module-contract.md, section 7).
"""

import uuid

from django.db import models
from django.db.models import Q

from apps.cases.errors import ImmutableRecordError
from apps.cases.models import ImmutableRecord

_MODULE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"
_NAMESPACE_PATTERN = r"^[a-z][a-z0-9_]{1,39}$"
_VERSION_PATTERN = r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
_FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"


class KeepRowsQuerySet(models.QuerySet):
    """A queryset that refuses to delete rows (updates stay possible: the services change the state under a lock)."""

    def delete(self):
        raise ImmutableRecordError("Module state rows are never deleted: disable the module instead.")


class ModuleState(models.Model):
    """What this installation has decided about one registered module: the version it holds data for, and whether it is enabled.

    There is no default for ``enabled``: whoever creates a row says so (the sync enables a new built-in module only when its dependencies
    are enabled). ``installed_version`` is the version the stored data belongs to; the sync never changes it, so a release with another
    version is a mismatch the administrator sees, and the module stays unavailable until a later slice (updates) deals with it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    module_id = models.CharField(max_length=40, unique=True)
    installed_version = models.CharField(max_length=32)
    enabled = models.BooleanField()
    configuration_version = models.PositiveIntegerField(null=True, blank=True, help_text="Reserved: no configuration is stored yet, so it is always null.")
    registry_fingerprint = models.CharField(max_length=64, help_text="The static registry's fingerprint when this row was last confirmed by a sync.")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    enabled_changed_at = models.DateTimeField(help_text="When ``enabled`` last changed; equal to ``created_at`` until it does.")

    objects = KeepRowsQuerySet.as_manager()

    class Meta:
        default_permissions = ()  # no Django permission exists for this table: it is changed by the administrator command only
        constraints = [
            models.CheckConstraint(condition=Q(module_id__regex=_MODULE_ID_PATTERN), name="module_state_module_id_shape"),
            models.CheckConstraint(condition=Q(installed_version__regex=_VERSION_PATTERN), name="module_state_version_shape"),
            models.CheckConstraint(condition=Q(registry_fingerprint__regex=_FINGERPRINT_PATTERN), name="module_state_fingerprint_shape"),
        ]

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Module state rows are never deleted: disable the module instead.")

    def __str__(self):
        return f"ModuleState {self.module_id}"


class ModuleHostMarker(ImmutableRecord):
    """Written once, by the first completed module sync, in the same transaction. Its existence ends the backward-compatibility window in which a registered
    module without a state row is treated as enabled; from then on a missing row fails closed. It holds no secret: a timestamp and a registry fingerprint."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    initialized_at = models.DateTimeField()
    initial_registry_fingerprint = models.CharField(max_length=64, help_text="The registry the first sync ran against (an empty registry has a fingerprint too).")

    class Meta:
        default_permissions = ()
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="module_marker_single_row"),
            models.CheckConstraint(condition=Q(initial_registry_fingerprint__regex=_FINGERPRINT_PATTERN), name="module_marker_fingerprint_shape"),
        ]

    def __str__(self):
        return "ModuleHostMarker"


class ModuleNamespaceClaim(ImmutableRecord):
    """One namespace and the module id that claimed it first. Inserted once; never changed, reassigned or deleted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    namespace = models.CharField(max_length=40, unique=True)
    module_id = models.CharField(max_length=40)
    claimed_at = models.DateTimeField()

    class Meta:
        default_permissions = ()
        indexes = [models.Index(fields=["module_id"], name="module_claim_module_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(namespace__regex=_NAMESPACE_PATTERN), name="module_claim_namespace_shape"),
            models.CheckConstraint(condition=Q(module_id__regex=_MODULE_ID_PATTERN), name="module_claim_module_id_shape"),
        ]

    def __str__(self):
        return f"ModuleNamespaceClaim {self.namespace} -> {self.module_id}"
