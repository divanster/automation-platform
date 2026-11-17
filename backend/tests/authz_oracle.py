"""A frozen copy of the Request Intake access algorithm as it was BEFORE the generic part moved to ``apps.accounts.access`` (MOD-1b / S1b).

TEST SUPPORT ONLY; the application never imports it. It is the oracle of the equivalence tests: ``tests/test_authz_equivalence.py``
compares the public functions of ``apps.intake.access`` (which now delegate to Core) and the Core function ``apps.accounts.access`` with it
on a complete truth table. It deliberately imports NOTHING from ``apps`` (permission names are literals), so a change in either real
module cannot change what it answers. Do not "fix" or modernize it: its value is that it is the old code, byte for byte in behaviour
(the code is the ``apps/intake/access.py`` of commit b8299c2, minus its documentation).
"""

import enum

MISSING_PERMISSION = "missing_permission"
OUT_OF_SCOPE = "out_of_scope"

PERM_SUBMIT = "intake.submit_request"
PERM_VIEW_OWN = "intake.view_own_request"
PERM_EDIT_OWN = "intake.edit_own_request"
PERM_VIEW_ANY = "intake.view_any_request"
PERM_APPROVE = "intake.approve_request"
PERM_REJECT = "intake.reject_request"
PERM_VIEW_AUDIT = "audit.view_auditevent"


class Action(str, enum.Enum):
    SUBMIT = "submit"
    VIEW = "view"
    EDIT = "edit"
    APPROVE = "approve"
    REJECT = "reject"
    VIEW_AUDIT = "view_audit"


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


def permission_problem(action, *, is_superuser, permissions):
    action = Action(action)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & (_ANY[action] | _OWN[action]):
        return None
    return MISSING_PERMISSION


def scope_problem(action, *, is_superuser, permissions, actor_pk, submitter_pk):
    action = Action(action)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & _ANY[action]:
        return None
    if held & _OWN[action] and submitter_pk is not None and actor_pk == submitter_pk:
        return None
    return OUT_OF_SCOPE if held & _OWN[action] else MISSING_PERMISSION


def access_problem(action, *, is_superuser, permissions, actor_pk, submitter_pk=None):
    return permission_problem(action, is_superuser=is_superuser, permissions=permissions) or scope_problem(
        action, is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, submitter_pk=submitter_pk
    )
