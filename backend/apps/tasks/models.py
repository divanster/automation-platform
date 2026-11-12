"""Persistence for the generic task queue: ``Task`` and ``TaskAttempt``.

Meanings follow docs/architecture/data-model.md and background-execution.md, trimmed to what this
slice needs (see docs/development/persistent-worker-slice.md). Task state describes execution
only; it is not ``WorkflowRun`` state, and a task holds no business revision or approval.

WHERE THE RULES ARE ENFORCED. Every change goes through ``apps.tasks.services``, which applies the
pure rules in ``apps.tasks.policy`` and fences worker mutations with a lease token. The ORM does
NOT stop code that calls ``Task.save()``, ``QuerySet.update()``, or raw SQL from breaking those
rules. The database enforces only what is declared below: unique idempotency keys, at most one
running attempt per task, unique attempt numbers, and CHECK constraints tying the lease fields,
``finished_at``, ``failure_code``, and the attempt count to the state. These constraints were
written without a PostgreSQL server and have not been run (see the slice document).
"""

import uuid

from django.db import models
from django.db.models import F, Q

from .policy import TERMINAL_STATES, AttemptOutcome, TaskState

_STATES = [state.value for state in TaskState]
_TERMINAL = sorted(state.value for state in TERMINAL_STATES)
_NON_TERMINAL = sorted(state.value for state in TaskState if state not in TERMINAL_STATES)
_NOT_RUNNING = sorted(state.value for state in TaskState if state is not TaskState.RUNNING)
_OUTCOMES = [outcome.value for outcome in AttemptOutcome]
_HASH_PATTERN = r"^[0-9a-f]{64}$"


class Task(models.Model):
    """One unit of background work: an allowlisted kind, a bounded payload, and one overall outcome."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=64, help_text="Handler allowlist key, checked at enqueue and again at claim.")
    idempotency_key = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Optional. Unique across the installation (not per kind); identity of a request is kind + payload.",
    )
    request_fingerprint = models.CharField(max_length=64, help_text="SHA-256 of the canonical kind and payload.")
    payload = models.JSONField(help_text="Validated, bounded input. Never logged.")
    state = models.CharField(max_length=16, default=TaskState.QUEUED.value)
    available_at = models.DateTimeField(help_text="Claimable once this instant has been reached (queued tasks).")
    max_attempts = models.PositiveSmallIntegerField()
    attempt_count = models.PositiveIntegerField(default=0, help_text="Number of claims so far.")
    lease_token = models.UUIDField(null=True, blank=True, help_text="Set only while running; new for every claim.")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True, help_text="Set only when succeeded. Never logged.")
    failure_code = models.CharField(max_length=64, blank=True, default="", help_text="A safe code (FailureCode); set only when failed.")
    created_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True, help_text="Set only in a terminal state.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="tasks_task_idempotency_key_unique",
            ),
            models.CheckConstraint(condition=Q(state__in=_STATES), name="tasks_task_state_valid"),
            models.CheckConstraint(
                condition=(
                    Q(state=TaskState.RUNNING.value, lease_token__isnull=False, lease_expires_at__isnull=False)
                    | Q(state__in=_NOT_RUNNING, lease_token__isnull=True, lease_expires_at__isnull=True)
                ),
                name="tasks_task_lease_matches_state",
            ),
            models.CheckConstraint(
                condition=Q(max_attempts__gte=1) & Q(max_attempts__lte=10) & Q(attempt_count__lte=F("max_attempts")),
                name="tasks_task_attempts_within_limit",
            ),
            models.CheckConstraint(
                condition=Q(state__in=[TaskState.QUEUED.value, TaskState.CANCELLED.value]) | Q(attempt_count__gte=1),
                name="tasks_task_started_states_have_attempts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state__in=_TERMINAL, finished_at__isnull=False)
                    | Q(state__in=_NON_TERMINAL, finished_at__isnull=True)
                ),
                name="tasks_task_finished_at_matches_state",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(state=TaskState.FAILED.value) & ~Q(failure_code=""))
                    | (~Q(state=TaskState.FAILED.value) & Q(failure_code=""))
                ),
                name="tasks_task_failure_code_matches_state",
            ),
            models.CheckConstraint(condition=Q(request_fingerprint__regex=_HASH_PATTERN), name="tasks_task_fingerprint_is_sha256_hex"),
        ]
        indexes = [
            models.Index(fields=["state", "available_at"], name="tasks_task_claim_idx"),
            models.Index(fields=["state", "lease_expires_at"], name="tasks_task_lease_idx"),
        ]

    def __str__(self):
        return f"Task {self.pk}"


class TaskAttempt(models.Model):
    """One claim and execution of a task (an automatic retry or a recovery is another attempt of the same task)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="attempts")
    attempt_number = models.PositiveIntegerField()
    worker_id = models.CharField(max_length=100)
    lease_token = models.UUIDField(help_text="The token this attempt was claimed under.")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, default=AttemptOutcome.RUNNING.value)
    failure_code = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "attempt_number"], name="tasks_attempt_number_unique_per_task"),
            models.UniqueConstraint(
                fields=["task"],
                condition=Q(outcome=AttemptOutcome.RUNNING.value),
                name="tasks_attempt_one_running_per_task",
            ),
            models.CheckConstraint(condition=Q(attempt_number__gte=1), name="tasks_attempt_number_at_least_1"),
            models.CheckConstraint(condition=Q(outcome__in=_OUTCOMES), name="tasks_attempt_outcome_valid"),
            models.CheckConstraint(
                condition=(
                    Q(outcome=AttemptOutcome.RUNNING.value, ended_at__isnull=True)
                    | (~Q(outcome=AttemptOutcome.RUNNING.value) & Q(ended_at__isnull=False))
                ),
                name="tasks_attempt_ended_at_matches_outcome",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(outcome__in=[AttemptOutcome.FAILED.value, AttemptOutcome.ABANDONED.value]) & ~Q(failure_code=""))
                    | (Q(outcome__in=[AttemptOutcome.RUNNING.value, AttemptOutcome.SUCCEEDED.value]) & Q(failure_code=""))
                ),
                name="tasks_attempt_failure_code_matches_outcome",
            ),
        ]

    def __str__(self):
        return f"TaskAttempt {self.pk}"
