"""The generic own / any / superuser permission rule. Pure Python, standard library only: no Django, no database, no logging.

This is the algorithm that used to live inside ``apps.intake.access`` (MOD-1b / S1b moved it here with NO change in behaviour).
It knows nothing about requests, runs or any business object. A caller describes ONE action as an ``AccessRule`` (the permissions that
allow it on ANY record, and those that allow it only on the actor's OWN records) and asks three questions about facts it has already
read (``apps.accounts.authz.load_actor`` reads them; this module reads nothing):

* ``permission_problem``: does the actor hold ANY permission for the action? (needs no record, so a service runs it first);
* ``scope_problem``: does the permission reach THIS record? (call it after the first passed; ``owner_pk`` is the user who owns the record);
* ``access_problem``: both at once.

THE RULES (each pinned by tests/test_authz_equivalence.py against a frozen copy of the old code and a recorded truth table):

* SUPERUSER. ``is_superuser`` passes both parts. It bypasses role membership and the own-record scope, and nothing else: this module has
  no other rule for it to bypass. (State rules, self-approval bans and the like are business rules and are NOT here.)
* PERMISSION. Not a superuser: the actor must hold at least one permission of the rule, either kind; otherwise ``missing_permission``.
* SCOPE. Not a superuser: holding an ANY permission passes. Otherwise holding an OWN permission passes only when the record's owner is
  known (``owner_pk`` is not None) and equals ``actor_pk``. Holding an own permission for somebody else's record is ``out_of_scope``;
  holding none is ``missing_permission`` (so ``scope_problem`` called without the first check still refuses).
* PRECEDENCE. ``access_problem`` returns the permission problem first and the scope problem only when the permission part passed.
  A refusal never says whether a record exists: the existence check is the caller's, and the caller runs the permission part
  before it (``require_permission`` before the lookup, ``authorize`` after), so an actor with no permission learns nothing.

The actor must be active and authenticated BEFORE this module is reached (``load_actor`` refuses an inactive or unknown user); a
superuser flag on an inactive user never gets here.

What is NOT here, on purpose: which permissions an action needs (that is the caller's table, for Request Intake
``apps.intake.access``), the module that owns a permission, role membership (``apps.accounts.roles``), and logging
(``apps.accounts.authz.log_refusal``).
"""

from dataclasses import dataclass

MISSING_PERMISSION = "missing_permission"
OUT_OF_SCOPE = "out_of_scope"


def _permission_names(value, what: str) -> frozenset:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise TypeError(f"{what} must be a collection of permission names")
    names = frozenset(value)
    if not all(isinstance(name, str) for name in names):
        raise TypeError(f"{what} must contain only permission names (text)")
    return names


@dataclass(frozen=True)
class AccessRule:
    """What one action needs: ``any_permissions`` allow it on any record, ``own_permissions`` only on the actor's own records."""

    any_permissions: frozenset = frozenset()
    own_permissions: frozenset = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "any_permissions", _permission_names(self.any_permissions, "any_permissions"))
        object.__setattr__(self, "own_permissions", _permission_names(self.own_permissions, "own_permissions"))


def _checked(rule) -> AccessRule:
    if not isinstance(rule, AccessRule):
        raise TypeError("rule must be an apps.accounts.access.AccessRule")
    return rule


def permission_problem(rule: AccessRule, *, is_superuser: bool, permissions) -> str | None:
    """``missing_permission`` when the actor holds no permission for the action (any scope), else None."""
    rule = _checked(rule)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & (rule.any_permissions | rule.own_permissions):
        return None
    return MISSING_PERMISSION


def scope_problem(rule: AccessRule, *, is_superuser: bool, permissions, actor_pk, owner_pk) -> str | None:
    """``out_of_scope`` when the actor holds only an own-record permission and is not the record's owner, else None."""
    rule = _checked(rule)
    if is_superuser:
        return None
    held = frozenset(permissions)
    if held & rule.any_permissions:
        return None
    if held & rule.own_permissions and owner_pk is not None and actor_pk == owner_pk:
        return None
    return OUT_OF_SCOPE if held & rule.own_permissions else MISSING_PERMISSION


def access_problem(rule: AccessRule, *, is_superuser: bool, permissions, actor_pk, owner_pk=None) -> str | None:
    """Both parts, permission first: the stable reason code, or None when the actor may do the action to this record."""
    return permission_problem(rule, is_superuser=is_superuser, permissions=permissions) or scope_problem(
        rule, is_superuser=is_superuser, permissions=permissions, actor_pk=actor_pk, owner_pk=owner_pk
    )
