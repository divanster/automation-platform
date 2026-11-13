"""Errors raised by the authentication and authorization checks. Messages never include a name or a value."""


class AuthorizationError(Exception):
    """The actor is not authenticated, not active, or not permitted to do this.

    ``reason`` is a stable code: ``not_authenticated``, ``inactive``, ``missing_permission`` or
    ``out_of_scope`` (the actor holds the permission, but not for this record).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The action is not permitted ({reason}).")
