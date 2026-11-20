"""Who may do what to a document: the permission an action needs and the scope it applies to. Pure rules over facts the caller has already read; no database, no Django.

Like Request Intake (``apps.intake.access``) this is only a table of permissions per action; every decision is delegated to Core's own / any / superuser rule (``apps.accounts.access``):

* UPLOAD needs ``documents.upload_document``; the uploader becomes the owner, so there is no scope part.
* VIEW (metadata, the list) needs ``view_any_document`` (any document) or ``view_own_document`` (documents the actor uploaded).
* DOWNLOAD (the original bytes) needs ``download_any_document`` or ``download_own_document``. Reading metadata never grants the content.

A superuser passes both parts and nothing else is bypassed. ``missing_permission`` is decided BEFORE any document is looked up; ``out_of_scope`` (a document that exists but is somebody else's) is
mapped by the API to the same 404 as an unknown document, so it never says which it was.
"""

import enum

from apps.accounts import access as core
from apps.accounts.access import AccessRule
from apps.accounts.roles import (
    PERM_DOC_DOWNLOAD_ANY,
    PERM_DOC_DOWNLOAD_OWN,
    PERM_DOC_UPLOAD,
    PERM_DOC_VIEW_ANY,
    PERM_DOC_VIEW_OWN,
)


class Action(str, enum.Enum):
    UPLOAD = "upload"
    VIEW = "view"
    DOWNLOAD = "download"


_RULES = {
    Action.UPLOAD: AccessRule(any_permissions=frozenset({PERM_DOC_UPLOAD})),
    Action.VIEW: AccessRule(any_permissions=frozenset({PERM_DOC_VIEW_ANY}), own_permissions=frozenset({PERM_DOC_VIEW_OWN})),
    Action.DOWNLOAD: AccessRule(any_permissions=frozenset({PERM_DOC_DOWNLOAD_ANY}), own_permissions=frozenset({PERM_DOC_DOWNLOAD_OWN})),
}


def permission_problem(action: Action, *, is_superuser: bool, permissions) -> str | None:
    """``missing_permission`` when the actor holds no permission for ``action`` (any scope), else None."""
    return core.permission_problem(_RULES[Action(action)], is_superuser=is_superuser, permissions=permissions)


def scope_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, owner_pk) -> str | None:
    """``out_of_scope`` when the actor holds only the own-document permission and is not the owner, else None. Call it after ``permission_problem`` passed."""
    return core.scope_problem(_RULES[Action(action)], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=owner_pk)


def access_problem(action: Action, *, is_superuser: bool, permissions, actor_pk, owner_pk=None) -> str | None:
    """Both parts at once, permission first."""
    return core.access_problem(_RULES[Action(action)], is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=owner_pk)
