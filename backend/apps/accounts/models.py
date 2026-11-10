from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Project user model.

    Deliberately identical to Django's default user: no added fields, standard
    username and password behavior. It exists as a project-owned model from the
    first migration because AUTH_USER_MODEL cannot be changed cheaply later
    (docs/decisions/0007-technology-baseline.md). Roles and permissions for the
    product are a later phase.
    """
