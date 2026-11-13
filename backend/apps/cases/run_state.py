"""Pure workflow-run state rules: the states, the allowed transitions, and the failed step.

Standard library only: no Django, no database. The vocabulary and the transition table follow
docs/architecture/first-workflow.md section 11, with the documented deviation that an attempt failure
that the queue retries does NOT move the run back to ``queued`` (the queue retries invisibly to the
run; see docs/development/request-intake-orchestration-slice.md). ``extracting -> extracting`` is
therefore allowed: a task that is claimed again after a crash or a retry finds the run already there.
``queued -> failed`` is allowed too, which the specification's table does not list: a task can fail for
good before its handler ever moved the run (an unknown kind or an invalid payload is refused by the
worker, not by the handler), and the run is then still ``queued``.

Run state is business state. It is not task state (``apps.tasks.policy.TaskState``): a task failing
for one attempt never changes a run, and only a task that failed for good is reconciled into
``failed``. Every change of ``WorkflowRun.state`` goes through ``apps.cases.services.transition_run``,
which applies ``transition_problem`` while the run row is locked.

Only some transitions are used by the code that exists today (see the slice document); the table
lists them all so the database CHECK constraint on the state never has to change later.
"""

import enum


class RunState(str, enum.Enum):
    QUEUED = "queued"  # run created; the extraction task is waiting or being retried
    EXTRACTING = "extracting"  # the extraction task has been claimed at least once
    NEEDS_REVIEW = "needs_review"  # an extraction outcome exists and awaits a human
    APPROVED = "approved"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailedStep(str, enum.Enum):
    EXTRACT = "extract"
    EXPORT = "export"


STATES = tuple(state.value for state in RunState)
FAILED_STEPS = tuple(step.value for step in FailedStep)
TERMINAL_STATES = frozenset({RunState.EXPORTED, RunState.CANCELLED})

ALLOWED_TRANSITIONS = {
    # queued -> failed: a task can fail for good before the handler ever moved the run (unknown kind, bad payload).
    RunState.QUEUED: frozenset({RunState.EXTRACTING, RunState.FAILED, RunState.CANCELLED}),
    RunState.EXTRACTING: frozenset({RunState.EXTRACTING, RunState.NEEDS_REVIEW, RunState.FAILED, RunState.CANCELLED}),
    RunState.NEEDS_REVIEW: frozenset({RunState.NEEDS_REVIEW, RunState.APPROVED, RunState.CANCELLED}),
    RunState.APPROVED: frozenset({RunState.NEEDS_REVIEW, RunState.EXPORTING, RunState.CANCELLED}),
    RunState.EXPORTING: frozenset({RunState.EXPORTED, RunState.NEEDS_REVIEW, RunState.FAILED, RunState.CANCELLED}),
    RunState.FAILED: frozenset({RunState.QUEUED, RunState.APPROVED, RunState.CANCELLED}),
    RunState.EXPORTED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

# A failed run leaves ``failed`` by a retry of the step that failed: extract -> queued, export -> approved.
_RETRY_TARGET = {FailedStep.EXTRACT: RunState.QUEUED, FailedStep.EXPORT: RunState.APPROVED}


def can_transition(current, target) -> bool:
    """True when the table allows ``current -> target`` (the failed step is checked separately)."""
    return RunState(target) in ALLOWED_TRANSITIONS[RunState(current)]


def transition_problem(current, target, *, current_failed_step: str = "", new_failed_step: str = "") -> str | None:
    """Why ``current -> target`` must be refused, or None when it is allowed.

    ``current_failed_step`` is the step recorded on the run now (empty unless it is failed);
    ``new_failed_step`` is the one the caller wants to record (required for ``failed``, empty
    otherwise). The reasons are stable codes: ``unknown_state``, ``not_allowed``,
    ``failed_step_required``, ``failed_step_unexpected``, ``wrong_retry_target``.
    """
    try:
        current, target = RunState(current), RunState(target)
    except ValueError:
        return "unknown_state"
    if target not in ALLOWED_TRANSITIONS[current]:
        return "not_allowed"
    if target is RunState.FAILED:
        if new_failed_step not in FAILED_STEPS:
            return "failed_step_required"
    elif new_failed_step != "":
        return "failed_step_unexpected"
    if current is RunState.FAILED and target is not RunState.CANCELLED:
        if current_failed_step not in FAILED_STEPS or _RETRY_TARGET[FailedStep(current_failed_step)] is not target:
            return "wrong_retry_target"
    return None
