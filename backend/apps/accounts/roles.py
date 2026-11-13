"""The initial roles and the permissions they hold: pure data, standard library only.

ROLES ARE DJANGO GROUPS. ``python manage.py setup_roles`` (repeatable) creates one group per role and
sets its permissions to exactly the set below. A user holds a role by being a member of the group; a user
may hold several. Nothing here reads a database.

PERMISSIONS CARRY THEIR SCOPE IN THEIR NAME. A Django permission is global: ``has_perm`` knows nothing
about which request a user may touch. So the request-specific permissions say what they cover, and the
application services (``apps.intake.review``) check the scope again against the stored record
(``apps.intake.access``):

* ``*_own_request``: only requests the user submitted;
* ``*_any_request``, ``approve_request``, ``reject_request``: any request in the installation (the
  approver and auditor pools are not narrowed per case yet; see the slice document).

The permission codenames are declared on ``apps.intake.models.RequestSource`` (Django creates a permission
only for a model) and ``audit.view_auditevent`` is Django's own view permission for the audit model; a test
compares this module with those declarations so the two cannot drift apart.

These role definitions are INITIAL DEVELOPMENT POLICY (docs/development/review-services-slice.md); the
architecture's role model is still "to be confirmed" (data-model.md, open question 2.3).
"""

from types import MappingProxyType

OPERATOR = "Operator"
APPROVER = "Approver"
AUDITOR = "Auditor"
ROLES = (OPERATOR, APPROVER, AUDITOR)

PERM_SUBMIT = "intake.submit_request"
PERM_VIEW_OWN = "intake.view_own_request"
PERM_EDIT_OWN = "intake.edit_own_request"
PERM_VIEW_ANY = "intake.view_any_request"
PERM_APPROVE = "intake.approve_request"
PERM_REJECT = "intake.reject_request"
PERM_VIEW_AUDIT = "audit.view_auditevent"

ALL_PERMISSIONS = frozenset(
    {PERM_SUBMIT, PERM_VIEW_OWN, PERM_EDIT_OWN, PERM_VIEW_ANY, PERM_APPROVE, PERM_REJECT, PERM_VIEW_AUDIT}
)

ROLE_PERMISSIONS = MappingProxyType(
    {
        # Submit, view and edit their own requests. Cannot approve, reject, or read the audit history.
        OPERATOR: frozenset({PERM_SUBMIT, PERM_VIEW_OWN, PERM_EDIT_OWN}),
        # View any request; approve or reject a request that is waiting for review. Cannot submit or edit.
        APPROVER: frozenset({PERM_VIEW_ANY, PERM_APPROVE, PERM_REJECT}),
        # Read only: view any request and the audit events. No permission that changes anything.
        AUDITOR: frozenset({PERM_VIEW_ANY, PERM_VIEW_AUDIT}),
    }
)


def split_permission(name: str) -> tuple[str, str]:
    """``"intake.approve_request"`` -> ``("intake", "approve_request")`` (app label, codename)."""
    app_label, _, codename = name.partition(".")
    if not app_label or not codename:
        raise ValueError("a permission name is '<app label>.<codename>'")
    return app_label, codename
