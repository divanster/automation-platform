"""Database glue for ``apps.intake.access``: read the actor fresh, apply the pure rules, refuse or return.

Two steps, used together by every service that changes or reads a request:

* ``require_permission`` runs BEFORE anything is looked up or locked: a user who holds no permission for the
  action at all is refused without touching the run (so they cannot even queue on its lock).
* ``authorize`` runs AFTER the run row is locked and the request's submitter is known: it re-reads the actor
  (a permission or group change committed while this call waited for the lock is honoured) and applies the
  scope rule.

Both raise ``apps.accounts.errors.AuthorizationError`` with a stable reason and log only the action, the
reason, and the actor's ``public_id``.
"""

from apps.accounts.authz import Actor, load_actor, log_refusal
from apps.accounts.errors import AuthorizationError

from .access import Action, access_problem, permission_problem


def require_permission(user, action: Action) -> Actor:
    """The actor, if they hold some permission for ``action``; else ``AuthorizationError``."""
    actor = load_actor(user)
    reason = permission_problem(action, is_superuser=actor.is_superuser, permissions=actor.permissions)
    if reason is not None:
        log_refusal(Action(action).value, reason, actor.public_id)
        raise AuthorizationError(reason)
    return actor


def authorize(user, action: Action, *, submitter_pk=None) -> Actor:
    """The actor, freshly read, if they may do ``action`` to the request ``submitter_pk`` submitted."""
    actor = load_actor(user)
    reason = access_problem(
        action,
        is_superuser=actor.is_superuser,
        permissions=actor.permissions,
        actor_pk=actor.pk,
        submitter_pk=submitter_pk,
    )
    if reason is not None:
        log_refusal(Action(action).value, reason, actor.public_id)
        raise AuthorizationError(reason)
    return actor
