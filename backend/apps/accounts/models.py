import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Project user model.

    Django's default user plus ONE added field, ``public_id``: a random UUID that is the user's stable
    public identity. The integer primary key is unchanged and stays the foreign-key target; ``public_id``
    is what domain records that store a plain UUID actor identifier use (``Approval.approver_id``,
    ``Rejection.rejected_by``, audit events). It is generated here, never supplied by a caller, and it is
    not a credential: knowing it grants nothing. Roles and permissions are Django Groups and permissions
    (``apps.accounts.roles``); see docs/development/review-services-slice.md.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
