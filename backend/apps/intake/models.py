"""Persistence for the request-intake orchestration: the source text, the task link, the outcome.

Three tables, all new; no existing table is changed by this app (``cases.WorkflowRun`` gained its
state in the ``cases`` migrations, and ``tasks`` is untouched). There is no second run hierarchy: every
row points at the existing ``cases.WorkflowRun``.

* ``RequestSource``: the submitted text, immutable, one per run.
* ``StepTask``: the link from a run's step to the queue ``Task`` that executes it. It lives HERE and not
  in ``apps.tasks`` because the queue must stay unaware of cases and runs.
* ``ExtractionOutcome``: the immutable result of the extraction step, one per run (the database
  enforces it). A valid draft also has a ``ResultRevision`` (revision number 1); an invalid draft has
  none, because a revision cannot hold blocking findings (open question 2.17).

IMMUTABILITY. ``RequestSource`` and ``ExtractionOutcome`` use ``apps.cases.models.ImmutableRecord`` and
are guarded at the Django ORM and service boundary only, exactly like ``ResultRevision`` and ``Approval``
(see the docstring of ``apps.cases.models``): raw SQL, ``QuerySet.raw()``, private ORM internals, a
database administrator, and a migration are NOT stopped. ``StepTask`` is an ordinary table.

PRIVACY. ``RequestSource.text`` and the ``draft`` and ``findings`` of an outcome are business content in
plaintext in PostgreSQL, like revision bytes; nothing here encrypts or access-controls them. Task
payloads and results never carry it.

These constraints were written without a PostgreSQL server and have not been run against one.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.cases.models import ImmutableRecord, ResultRevision, WorkflowRun
from apps.tasks.models import Task

from .policy import MAX_REQUEST_TEXT_BYTES, OUTCOME_INVALID, OUTCOME_STATUSES, OUTCOME_VALID, STEP_EXTRACT

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_STEPS = [STEP_EXTRACT]


class RequestSource(ImmutableRecord):
    """The exact text a user submitted for one run. Never modified; never sent to the queue."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(WorkflowRun, on_delete=models.PROTECT, related_name="request_source")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    text = models.TextField(help_text="Exactly as submitted: no trimming, no newline normalization. Never logged.")
    text_sha256 = models.CharField(max_length=64, help_text="SHA-256 of the UTF-8 bytes of the text (identity of the source, not of any revision).")
    byte_length = models.PositiveIntegerField(help_text="Length of the UTF-8 encoding.")
    client_request_id = models.UUIDField(null=True, blank=True, help_text="Optional caller-chosen id that makes a repeated submit return the same run.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submitted_by", "client_request_id"],
                condition=Q(client_request_id__isnull=False),
                name="intake_source_client_request_unique",
            ),
            models.CheckConstraint(condition=Q(text_sha256__regex=_HASH_PATTERN), name="intake_source_hash_is_sha256_hex"),
            models.CheckConstraint(
                condition=Q(byte_length__gte=1) & Q(byte_length__lte=MAX_REQUEST_TEXT_BYTES),
                name="intake_source_size_within_limit",
            ),
        ]

    def __str__(self):
        return f"RequestSource {self.pk}"


class StepTask(models.Model):
    """The queue task that executes one step of one run, per retry generation (first generation: 1)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="step_tasks")
    step = models.CharField(max_length=16)
    generation = models.PositiveSmallIntegerField(help_text="1 for the first task of the step; a deliberate retry would use the next number.")
    task = models.OneToOneField(Task, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "step", "generation"], name="intake_steptask_run_step_generation_unique"),
            models.CheckConstraint(condition=Q(step__in=_STEPS), name="intake_steptask_step_valid"),
            models.CheckConstraint(condition=Q(generation__gte=1), name="intake_steptask_generation_at_least_1"),
        ]

    def __str__(self):
        return f"StepTask {self.pk}"


class ExtractionOutcome(ImmutableRecord):
    """The one result of extracting a run. Written once, in the same transaction as revision 1 (if any)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(WorkflowRun, on_delete=models.PROTECT, related_name="extraction_outcome")
    status = models.CharField(max_length=8, help_text="valid: a revision exists; invalid: blocking findings, no revision.")
    revision = models.OneToOneField(ResultRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    extractor_name = models.CharField(max_length=100)
    extractor_version = models.CharField(max_length=50)
    draft = models.JSONField(help_text="Plain JSON copy of the extracted draft (apps.intake.draft_json); never hashed.")
    ambiguities = models.JSONField(default=list)
    findings = models.JSONField(default=list, help_text="Every finding as stored on revisions: code, field_path, blocking, message, excerpt.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(status__in=list(OUTCOME_STATUSES)), name="intake_outcome_status_valid"),
            models.CheckConstraint(
                condition=(
                    Q(status=OUTCOME_VALID, revision__isnull=False) | Q(status=OUTCOME_INVALID, revision__isnull=True)
                ),
                name="intake_outcome_revision_matches_status",
            ),
        ]

    def __str__(self):
        return f"ExtractionOutcome {self.pk}"
