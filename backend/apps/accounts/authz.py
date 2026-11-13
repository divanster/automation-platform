"""Turn a caller-supplied user into trusted facts about an actor, read fresh from the database.

``load_actor`` never trusts the object it is given. It checks that the object is an authenticated user,
then reloads the row by primary key, so a user deactivated, demoted, or removed from a group since the
object was loaded is judged by what is stored NOW. The returned ``Actor`` is the only source of the actor's
``public_id`` for the review services: a caller can never supply one.

The decision itself (which permission an action needs, and what scope it has) is pure and lives in
``apps.intake.access``. This module only gathers facts, so the rules can be tested without a database.

LOGGING. A refusal is logged with the action, the stable reason code, and the actor's ``public_id`` (a
random identifier, not personal data). Never the username, name, email, password, or any request content.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth import get_user_model

from .errors import AuthorizationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    """Facts about a user at one moment. ``user`` is the freshly loaded row; the rest is copied from it."""

    user: object
    pk: int
    public_id: UUID
    is_superuser: bool
    permissions: frozenset


def load_actor(user) -> Actor:
    """The actor behind ``user``, read fresh. Raises ``AuthorizationError`` (``not_authenticated``, ``inactive``)."""
    user_model = get_user_model()
    if not isinstance(user, user_model) or not getattr(user, "is_authenticated", False) or user.pk is None:
        raise AuthorizationError("not_authenticated")
    fresh = user_model._default_manager.filter(pk=user.pk).first()
    if fresh is None:
        raise AuthorizationError("not_authenticated")
    if not fresh.is_active:
        log_refusal("authenticate", "inactive", fresh.public_id)
        raise AuthorizationError("inactive")
    return Actor(
        user=fresh,
        pk=fresh.pk,
        public_id=fresh.public_id,
        is_superuser=fresh.is_superuser,
        permissions=frozenset(fresh.get_all_permissions()),
    )


def log_refusal(action: str, reason: str, public_id: UUID | None) -> None:
    logger.warning("authorization refused: action=%s reason=%s actor=%s", action, reason, public_id)
