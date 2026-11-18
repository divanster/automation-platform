"""Persistence for cases, workflow runs, result revisions, approvals, and rejections.

Entity meanings follow docs/architecture/data-model.md. This slice stores only what the pure
revision/approval policy (``workflows.revision_approval``) needs; everything else the data model
describes for these entities (run state, definition and configuration snapshots, revision origin,
parent, and author, approval decisions other than "approved", and so on) is deferred and will be
added to these same tables by later migrations.

WHERE IMMUTABILITY IS ENFORCED. ``ResultRevision``, ``Approval`` and ``Rejection`` rows are immutable at the
Django ORM and service boundary only. These paths are guarded and raise ``ImmutableRecordError``:

* ``save()`` and ``save_base()`` on a row that already exists, or with ``update_fields`` or
  ``force_update``; a new row is always inserted (never silently overwriting a primary key,
  including through ``loaddata``, which calls ``save_base(raw=True)``);
* ``delete()``, ``QuerySet.update()``, ``bulk_update()``, ``QuerySet.delete()``, and
  ``bulk_create(update_conflicts=True)`` on the default manager (``objects``) and on related
  managers such as ``run.revisions``;
* ``update_or_create`` (it ends in ``save()`` or ``update()``);
* foreign keys use ``PROTECT``, so deleting a run or case that has revisions is refused.

There is NO database-level immutability: no trigger, no revoked UPDATE/DELETE privilege. These
paths are NOT guarded: raw SQL and ``connection.cursor()``; ``QuerySet.raw()``; private ORM
internals (``QuerySet._raw_delete``, ``QuerySet._update``, ``Model._do_update``); the
``_base_manager`` (a plain manager, not ``objects``); a database administrator; and a migration.
``WorkflowRun.current_revision`` is an ordinary updatable column, so ``run.save()`` can point it
anywhere. The database enforces uniqueness, foreign keys, and the CHECK constraints declared
below, and nothing else.

Rows are re-verified when loaded (the domain recomputes the SHA-256 of the stored bytes and
compares it with the stored hash). That detects accidental corruption and a change to the bytes
or to the hash alone. It is NOT tamper protection: whoever can write the table can change the
payload and its hash together and the row stays consistent.
"""

import uuid

from django.db import models

from .attribution import ATTRIBUTION_FIELDS, MAX_WORKFLOW_VERSION, MODULE_ID_PATTERN, WORKFLOW_KEY_PATTERN, WorkflowAttribution
from .errors import ImmutableAttributionError, ImmutableRecordError
from .run_state import FAILED_STEPS, STATES, RunState

_HASH_PATTERN = r"^[0-9a-f]{64}$"
REASON_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"  # the shape of a stable reason code


class ImmutableQuerySet(models.QuerySet):
    """A queryset that refuses bulk modification and deletion."""

    def update(self, **kwargs):
        raise ImmutableRecordError("These records are immutable: update is refused.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableRecordError("These records are immutable: bulk_update is refused.")

    def delete(self):
        raise ImmutableRecordError("These records are immutable: delete is refused.")

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
        if update_conflicts:
            raise ImmutableRecordError("These records are immutable: an upsert is refused.")
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts)


class ImmutableRecord(models.Model):
    """Abstract base: rows are inserted once and never modified or deleted through the ORM."""

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(f"{type(self).__name__} rows are immutable: an existing row cannot be saved again.")
        if kwargs.get("update_fields") is not None or kwargs.get("force_update"):
            raise ImmutableRecordError(f"{type(self).__name__} rows are immutable: an update is refused.")
        # Always INSERT: an existing primary key raises IntegrityError instead of being overwritten.
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def save_base(self, raw=False, force_insert=False, force_update=False, using=None, update_fields=None):
        # Every save path ends here (save(), loaddata's save_base(raw=True), direct calls).
        if not self._state.adding or force_update or update_fields is not None:
            raise ImmutableRecordError(f"{type(self).__name__} rows are immutable: an existing row cannot be saved again.")
        super().save_base(raw=raw, force_insert=True, using=using, update_fields=update_fields)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} rows are immutable: delete is refused.")


class Case(models.Model):
    """A business matter (data-model.md, Case). Minimal: identity and a readable reference."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=100, unique=True, help_text="Human-readable case reference, unique in the installation.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Case {self.pk}"


def _refuse_attribution_names(names, operation: str) -> None:
    refused = sorted(set(names) & set(ATTRIBUTION_FIELDS))
    if refused:
        raise ImmutableAttributionError(f"A run's workflow attribution is immutable: {operation} {', '.join(refused)} is refused.")


class WorkflowRunQuerySet(models.QuerySet):
    """A queryset that refuses to change a run's workflow attribution in bulk. Everything else on a run stays updatable."""

    def update(self, **kwargs):
        _refuse_attribution_names(kwargs, "an update of")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        _refuse_attribution_names(fields, "a bulk_update of")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
        if update_conflicts:
            raise ImmutableAttributionError("A run's workflow attribution is immutable: an upsert is refused.")
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts)


class WorkflowRun(models.Model):
    """One run for one case (data-model.md, WorkflowRun). Minimal: state and workflow attribution, no snapshot yet.

    ``current_revision`` is the run's pointer to its current ``ResultRevision``. It is changed
    only by ``apps.cases.services.record_revision`` while the run row is locked. The database
    does not check that the pointed-to revision belongs to this run; the service does.

    ``state`` and ``failed_step`` are changed only by ``apps.cases.services.transition_run`` while the
    run row is locked, which applies the transition table in ``apps.cases.run_state``. The database
    checks only that ``state`` is a known state and that ``failed_step`` is set exactly when the run
    is ``failed``; it does not check which transition happened. State is business state, not task state.

    ``module_id``, ``workflow_key`` and ``workflow_version`` say which module, workflow and workflow definition
    version the run was created under (``apps.cases.attribution``). They are written once, by
    ``apps.cases.services.create_run``, and never change. The database checks that all three are present and
    shaped like the manifest's identifiers and that the version is a positive integer; it does not know which
    modules or workflows exist (that is the registry's knowledge). Nothing about a run's behaviour reads them.
    The attribution does not change a run's state transitions, tasks, revisions, hashes or approvals.

    WHERE THE ATTRIBUTION IS PROTECTED. At the Django ORM and service boundary only, like the immutable records
    above: ``save()`` and ``save_base()`` on an existing row refuse a changed attribution value and any
    ``update_fields`` that names an attribution column; a new row is always inserted (never silently overwriting
    a row, ``loaddata`` included); ``QuerySet.update()``, ``bulk_update()`` and ``bulk_create(update_conflicts=True)``
    on the default manager refuse it. NOT guarded: raw SQL, ``QuerySet.raw()``, private ORM internals, the
    ``_base_manager``, a database administrator and a migration. There is no trigger.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="runs")
    current_revision = models.ForeignKey(
        "cases.ResultRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    state = models.CharField(max_length=16, default=RunState.QUEUED.value)
    failed_step = models.CharField(max_length=16, blank=True, default="", help_text="Set only while the run is failed.")
    module_id = models.CharField(max_length=40, help_text="The module that owns the workflow this run was created under. Never changes.")
    workflow_key = models.CharField(max_length=64, help_text="The workflow this run belongs to. Never changes.")
    workflow_version = models.PositiveIntegerField(help_text="The workflow definition version the run started on. Never changes.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkflowRunQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(state__in=list(STATES)), name="cases_run_state_valid"),
            models.CheckConstraint(condition=models.Q(failed_step__in=["", *FAILED_STEPS]), name="cases_run_failed_step_valid"),
            models.CheckConstraint(
                condition=(
                    (models.Q(state=RunState.FAILED.value) & ~models.Q(failed_step=""))
                    | (~models.Q(state=RunState.FAILED.value) & models.Q(failed_step=""))
                ),
                name="cases_run_failed_step_matches_state",
            ),
            models.CheckConstraint(condition=models.Q(module_id__regex=MODULE_ID_PATTERN), name="cases_run_module_id_shape"),
            models.CheckConstraint(condition=models.Q(workflow_key__regex=WORKFLOW_KEY_PATTERN), name="cases_run_workflow_key_shape"),
            models.CheckConstraint(
                condition=models.Q(workflow_version__gte=1) & models.Q(workflow_version__lte=MAX_WORKFLOW_VERSION),
                name="cases_run_workflow_version_positive",
            ),
        ]

    def __str__(self):
        return f"WorkflowRun {self.pk}"

    @property
    def attribution(self) -> WorkflowAttribution:
        """The run's attribution as a validated value (raises ``WorkflowAttributionError`` if the stored values are not valid)."""
        return WorkflowAttribution(self.module_id, self.workflow_key, self.workflow_version)

    # --- the attribution guard (ORM level; see the class docstring) -------------------------------------------

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._remember_attribution()
        return instance

    def refresh_from_db(self, using=None, fields=None, **kwargs):
        super().refresh_from_db(using=using, fields=fields, **kwargs)
        # Only what was just read from the database is remembered: a value changed in memory is never laundered by a partial refresh.
        self._remember_attribution(None if fields is None else set(fields))

    def _remember_attribution(self, only=None) -> None:
        stored = dict(getattr(self, "_stored_attribution", {}))
        for name in ATTRIBUTION_FIELDS:
            if (only is None or name in only) and name in self.__dict__:
                stored[name] = self.__dict__[name]
        self._stored_attribution = stored

    def _refuse_attribution_change(self, update_fields) -> None:
        if update_fields is not None:
            _refuse_attribution_names(update_fields, "a save with update_fields naming")
            return
        stored = getattr(self, "_stored_attribution", None)
        for name in ATTRIBUTION_FIELDS:
            if name in self.__dict__ and (stored is None or name not in stored or stored[name] != self.__dict__[name]):
                raise ImmutableAttributionError(f"A run's workflow attribution is immutable: {name} cannot be changed on an existing run.")

    def save_base(self, raw=False, force_insert=False, force_update=False, using=None, update_fields=None):
        # Every save path ends here (save(), loaddata's save_base(raw=True), direct calls).
        if self._state.adding:
            force_insert = True  # a new row is always INSERTed: an existing row's attribution is never overwritten
        else:
            self._refuse_attribution_change(update_fields)
        super().save_base(raw=raw, force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)
        self._remember_attribution()


class ResultRevision(ImmutableRecord):
    """One immutable result revision of a run: the exact canonical bytes and their SHA-256.

    The hash and format version are produced by the domain's canonicalization and re-verified
    by the domain on every load (``adapters.to_domain_revision``); this model never computes
    a hash. ``findings`` are review material outside the hashed bytes.
    """

    id = models.UUIDField(primary_key=True, editable=False)  # supplied by the caller; never defaulted
    run = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    canonical_format_version = models.PositiveIntegerField()
    canonical_bytes = models.BinaryField()
    content_hash = models.CharField(max_length=64)
    findings = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "revision_number"], name="cases_revision_number_unique_per_run"),
            models.CheckConstraint(condition=models.Q(revision_number__gte=1), name="cases_revision_number_at_least_1"),
            models.CheckConstraint(condition=models.Q(canonical_format_version__gte=1), name="cases_revision_format_version_at_least_1"),
            models.CheckConstraint(condition=models.Q(content_hash__regex=_HASH_PATTERN), name="cases_revision_hash_is_sha256_hex"),
        ]

    def __str__(self):
        return f"ResultRevision {self.pk}"


class Approval(ImmutableRecord):
    """An immutable record that one revision was approved (decision "approved" only).

    Bound to its revision by a foreign key (and so, through the revision, to its run and
    case) and to the revision's content hash at decision time. ``approver_id`` is stored as
    an unverified domain actor identifier at this layer: it is NOT a foreign key and nothing in
    ``apps.cases`` authenticates or authorizes it. Any API that exposes approval operations must go through
    ``apps.intake.review.approve_request``, which derives the value from the authenticated user's
    ``User.public_id`` after the role and policy checks. Rejections are the separate ``Rejection`` model. Decision time semantics beyond ``created_at`` belong to later slices.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ResultRevision, on_delete=models.PROTECT, related_name="approvals")
    content_hash = models.CharField(max_length=64)
    approver_id = models.UUIDField()
    acknowledged_warnings = models.JSONField(default=list)  # [[finding code, field path], ...]
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(content_hash__regex=_HASH_PATTERN), name="cases_approval_hash_is_sha256_hex"),
        ]

    def __str__(self):
        return f"Approval {self.pk}"


class Rejection(ImmutableRecord):
    """An immutable record that one revision was rejected by a reviewer.

    Bound exactly like an approval: to its revision by a foreign key (and so, through the revision, to
    its run and case) and to the revision's content hash at decision time. ``rejected_by`` is the
    deciding user's ``User.public_id`` stored as a plain UUID (not a foreign key, like
    ``Approval.approver_id``); ``apps.cases`` does not check who the user is or what they may do,
    the application service that records the rejection does. ``reason_code`` is a short stable code, never free text: the
    database checks only its shape, and the workflow that records the rejection checks it against its own
    list (for request intake, ``apps.intake.policy.REJECTION_REASONS``). A rejection does not change the
    revision, any approval, or the run's current revision, and rejections are never updated or deleted
    through the ORM (the same boundary as every ``ImmutableRecord``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ResultRevision, on_delete=models.PROTECT, related_name="rejections")
    content_hash = models.CharField(max_length=64)
    rejected_by = models.UUIDField()
    reason_code = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(content_hash__regex=_HASH_PATTERN), name="cases_rejection_hash_is_sha256_hex"),
            models.CheckConstraint(condition=models.Q(reason_code__regex=REASON_CODE_PATTERN), name="cases_rejection_reason_code_shape"),
        ]

    def __str__(self):
        return f"Rejection {self.pk}"
