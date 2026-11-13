"""The signed-in user's own profile: identity, roles, and the product permissions they hold.

Read-only. It answers "who am I and what may I do", which a client needs to decide what to offer. It is
NOT an authorization decision: the services in ``apps.intake`` check permission and scope again on every
call, so a stale or tampered client copy of this profile grants nothing.

The user is reloaded from the database (``apps.accounts.authz.load_actor``), so a role removed a moment ago
is not reported. Nothing credential-like is returned: no password hash, no session data, no e-mail address.
"""

from dataclasses import dataclass
from uuid import UUID

from .authz import load_actor
from .roles import ALL_PERMISSIONS, ROLES


@dataclass(frozen=True)
class UserProfile:
    public_id: UUID
    username: str
    is_superuser: bool
    roles: tuple[str, ...]  # the product roles (groups) the user belongs to, in the fixed role order
    permissions: tuple[str, ...]  # the product permissions the user holds, sorted; a superuser holds them all


def current_user_profile(user) -> UserProfile:
    """The profile of ``user`` as stored now. Raises ``AuthorizationError`` for a non-user or an inactive user."""
    actor = load_actor(user)
    member_of = set(actor.user.groups.filter(name__in=ROLES).values_list("name", flat=True))
    permissions = ALL_PERMISSIONS if actor.is_superuser else actor.permissions & ALL_PERMISSIONS
    return UserProfile(
        public_id=actor.public_id,
        username=actor.user.get_username(),
        is_superuser=actor.is_superuser,
        roles=tuple(role for role in ROLES if role in member_of),
        permissions=tuple(sorted(permissions)),
    )
