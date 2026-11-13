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
"""

import enum

from apps.accounts.roles import PERM_APPROVE, PERM_EDIT_OWN, PERM_REJECT, PERM_SUBMIT, PERM_VIEW_ANY, PERM_VIEW_AUDIT, PERM_VIEW_OWN

MISSING_PERMISSION = "missing_permission"
OUT_OF_SCOPE = "out_of_scope"


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


def permission_problem(action: Action, *, is_superuser: bool, permissions) -> str | None:
    """``missing_permission`` when the actor holds no permission for ``action`` (any scope), else None.

    The check that needs no record: services run it first, before looking anything up.
    """
    action = Action(action)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & (_ANY[action] | _OWN[action]):
        return None
    return MISSING_PERMISSION


def scope_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, submitter_pk) -> str | None:
    """``out_of_scope`` when the actor holds only an own-record permission and is not the submitter, else None.

    Call it after ``permission_problem`` passed. ``submitter_pk`` is the user who submitted the request.
    """
    action = Action(action)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & _ANY[action]:
        return None
    if held & _OWN[action] and submitter_pk is not None and actor_pk == submitter_pk:
        return None
    return OUT_OF_SCOPE if held & _OWN[action] else MISSING_PERMISSION


def access_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, submitter_pk=None) -> str | None:
    """Both parts at once: the stable reason code, or None when the actor may do ``action`` to this request."""
    return permission_problem(action, is_superuser=is_superuser, permissions=permissions) or scope_problem(
        action, is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, submitter_pk=submitter_pk
    )
