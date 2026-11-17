"""Who may do what to a request: the permission an action needs and the scope it applies to.

Pure rules over facts the caller has already read (``apps.accounts.authz.load_actor``): no database, no
Django. The services in ``apps.intake.review`` and ``apps.intake.submission`` gather the facts, call
``access_problem``, and raise ``AuthorizationError`` for the code it returns, BEFORE anything is written.

THE CHECK HAS TWO PARTS, because a Django permission is global and knows nothing about records:

1. PERMISSION: the actor holds the permission the action needs (through a role group).
2. SCOPE: the permission applies to this record. ``edit`` and ``view`` through the ``*_own_request``
   permissions reach only requests the actor submitted; ``view`` through ``view_any_request``, ``approve``
   and ``reject`` reach any request (the approver and auditor pools are installation-wide in this slice).

SUPERUSER (initial policy). A superuser passes both parts (it bypasses role membership and the own-record
scope). It does NOT bypass anything else: the state rules, the stale-revision and content-hash checks, the
warning acknowledgement, and the self-approval ban (``apps.intake.review_rules``) apply to it unchanged.
An inactive user is refused before this module is reached.

Reason codes: ``missing_permission`` (no permission for this action at all) and ``out_of_scope`` (the actor
holds an own-record permission, but the request is somebody else's).

WHAT LIVES WHERE (MOD-1b / S1b). The generic own / any / superuser algorithm is Core's: ``apps.accounts.access``. This module keeps
only what is specific to Request Intake: the ``Action`` vocabulary and the table that says which permissions each action needs, and it
DELEGATES every decision to Core. Its three functions keep their names, signatures, return values and exceptions (an unknown action is
still a ``ValueError``), so ``apps.intake.authorization``, ``apps.intake.queries`` and the tests are unchanged.
"""

import enum

from apps.accounts import access as core
from apps.accounts.access import MISSING_PERMISSION, OUT_OF_SCOPE, AccessRule
from apps.accounts.roles import PERM_APPROVE, PERM_EDIT_OWN, PERM_REJECT, PERM_SUBMIT, PERM_VIEW_ANY, PERM_VIEW_AUDIT, PERM_VIEW_OWN


class Action(str, enum.Enum):
    SUBMIT = "submit"
    VIEW = "view"
    EDIT = "edit"
    APPROVE = "approve"
    REJECT = "reject"
    VIEW_AUDIT = "view_audit"


# For each action: the permissions that allow it on ANY request, and those that allow it on the actor's OWN.
_ANY = {
    Action.SUBMIT: frozenset({PERM_SUBMIT}),
    Action.VIEW: frozenset({PERM_VIEW_ANY}),
    Action.EDIT: frozenset(),
    Action.APPROVE: frozenset({PERM_APPROVE}),
    Action.REJECT: frozenset({PERM_REJECT}),
    Action.VIEW_AUDIT: frozenset({PERM_VIEW_AUDIT}),
}
_OWN = {
    Action.SUBMIT: frozenset(),
    Action.VIEW: frozenset({PERM_VIEW_OWN}),
    Action.EDIT: frozenset({PERM_EDIT_OWN}),
    Action.APPROVE: frozenset(),
    Action.REJECT: frozenset(),
    Action.VIEW_AUDIT: frozenset(),
}


# The same table as Core rules, one per action. Built once from the two tables above, so there is a single source of truth.
_RULES = {action: AccessRule(any_permissions=_ANY[action], own_permissions=_OWN[action]) for action in Action}


def permission_problem(action: Action, *, is_superuser: bool, permissions) -> str | None:
    """``missing_permission`` when the actor holds no permission for ``action`` (any scope), else None.

    The check that needs no record: services run it first, before looking anything up.
    """
    return core.permission_problem(_RULES[Action(action)], is_superuser=is_superuser, permissions=permissions)


def scope_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, submitter_pk) -> str | None:
    """``out_of_scope`` when the actor holds only an own-record permission and is not the submitter, else None.

    Call it after ``permission_problem`` passed. ``submitter_pk`` is the user who submitted the request.
    """
    return core.scope_problem(
        _RULES[Action(action)], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=submitter_pk
    )


def access_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, submitter_pk=None) -> str | None:
    """Both parts at once: the stable reason code, or None when the actor may do ``action`` to this request."""
    return core.access_problem(
        _RULES[Action(action)], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=submitter_pk
    )
