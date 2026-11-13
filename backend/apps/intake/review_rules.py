"""The rules of editing, approving and rejecting, as pure functions over facts already read.

No database, no Django. ``apps.intake.review`` reads the facts under the run lock and calls these; each
returns a stable refusal code (or None), checked in the fixed order documented on the function, so the
refusal a caller sees never depends on incidental details. Hashing, validation and canonicalization are NOT
here: they stay in ``workflows`` (this module compares two hashes it is given, nothing more).

INITIAL POLICY DECISIONS (docs/development/review-services-slice.md, data-model.md open question 2.3):

* Edit is allowed only in ``needs_review`` and ``approved``. ``exporting`` is refused (export does not
  exist yet and its cancellation protocol is not built); every other state is refused.
* Approval and rejection are allowed only in ``needs_review``.
* SELF-APPROVAL IS FORBIDDEN: a user may not approve a run if they submitted the request or authored any
  revision of it. A superuser is bound by this too.
* A revision that has been rejected cannot be approved; a new revision (an edit) is needed.
"""

from apps.cases.run_state import RunState

from .errors import refusal
from .policy import REJECTION_REASONS

EDITABLE_STATES = frozenset({RunState.NEEDS_REVIEW.value, RunState.APPROVED.value})
REVIEWABLE_STATES = frozenset({RunState.NEEDS_REVIEW.value})


def edit_problem(*, run_state, current_revision_id, base_revision_id, has_invalid_outcome) -> str | None:
    """Why an edit must be refused, or None. Checked in this order:

    1. ``run_state_not_editable``: the run is not in ``needs_review`` or ``approved``.
    2. ``stale_base_revision``: the revision the caller based the edit on is not the run's current one.
       ``base_revision_id`` is None exactly when the caller edits a run that has no revision yet.
    3. ``nothing_to_edit``: the run has no revision and no invalid extraction outcome to start from.
    """
    if run_state not in EDITABLE_STATES:
        return "run_state_not_editable"
    if base_revision_id != current_revision_id:
        return "stale_base_revision"
    if current_revision_id is None and not has_invalid_outcome:
        return "nothing_to_edit"
    return None


def is_self_approval(*, actor_pk, submitter_pk, author_pks) -> bool:
    """True when the actor submitted the request or authored any revision of the run."""
    return actor_pk == submitter_pk or actor_pk in set(author_pks)


def normalize_warning_pairs(pairs) -> frozenset:
    """``{(code, path), ...}`` from an iterable of two-string pairs; anything else is ``ValueError``.

    A bare string is refused (it would be read as characters). The result is a set: order and repeats do
    not matter.
    """
    if isinstance(pairs, (str, bytes)) or not hasattr(pairs, "__iter__"):
        raise ValueError("acknowledged warnings must be an iterable of (code, path) pairs")
    result = set()
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or not all(isinstance(part, str) for part in pair):
            raise ValueError("each acknowledged warning must be a (code, path) pair of strings")
        result.add((pair[0], pair[1]))
    return frozenset(result)


def approval_problem(
    *,
    run_state,
    current_revision_id,
    revision_id,
    current_hash,
    submitted_hash,
    self_approval,
    revision_rejected,
    current_warnings,
    acknowledged_warnings,
) -> tuple[str, dict] | None:
    """Why an approval must be refused, as ``(code, details)``, or None. Checked in this order:

    1. ``run_not_in_review``: the run is not in ``needs_review``.
    2. ``no_current_revision``: the run has no revision to approve.
    3. ``self_approval_forbidden``: ``self_approval`` is true (the caller computed it with ``is_self_approval``).
    4. ``stale_revision``: ``revision_id`` is not the run's current revision.
    5. ``content_hash_mismatch``: ``submitted_hash`` is not the current revision's hash.
    6. ``revision_rejected``: the revision has a recorded rejection.
    7. ``warnings_not_acknowledged``: some current warning is missing from the acknowledgement; or
       ``unknown_warning_acknowledged``: the acknowledgement names a warning the revision does not have.
       The acknowledgement must equal the current warnings exactly. ``details`` carries the missing and
       unexpected (code, path) pairs.
    """
    if run_state not in REVIEWABLE_STATES:
        return "run_not_in_review", {}
    if current_revision_id is None:
        return "no_current_revision", {}
    if self_approval:
        return "self_approval_forbidden", {}
    if revision_id != current_revision_id:
        return "stale_revision", {}
    if submitted_hash != current_hash:
        return "content_hash_mismatch", {}
    if revision_rejected:
        return "revision_rejected", {}
    current, acknowledged = frozenset(current_warnings), frozenset(acknowledged_warnings)
    missing, unexpected = tuple(sorted(current - acknowledged)), tuple(sorted(acknowledged - current))
    if missing:
        return "warnings_not_acknowledged", {"missing": missing, "unexpected": unexpected}
    if unexpected:
        return "unknown_warning_acknowledged", {"missing": missing, "unexpected": unexpected}
    return None


def rejection_problem(*, run_state, current_revision_id, revision_id, current_hash, submitted_hash, reason_code) -> str | None:
    """Why a rejection must be refused, or None. Checked in this order: ``run_not_in_review``,
    ``no_current_revision``, ``stale_revision``, ``content_hash_mismatch``, ``unknown_reason_code``.
    """
    if run_state not in REVIEWABLE_STATES:
        return "run_not_in_review"
    if current_revision_id is None:
        return "no_current_revision"
    if revision_id != current_revision_id:
        return "stale_revision"
    if submitted_hash != current_hash:
        return "content_hash_mismatch"
    if reason_code not in REJECTION_REASONS:
        return "unknown_reason_code"
    return None


def raise_if_refused(problem) -> None:
    """Raise the matching ``ReviewRefusedError`` for an ``edit_problem``/``rejection_problem`` code or an
    ``approval_problem`` pair; do nothing for None."""
    if problem is None:
        return
    reason, details = problem if isinstance(problem, tuple) else (problem, {})
    raise refusal(reason, **details)
