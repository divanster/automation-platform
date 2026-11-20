"""Errors of the managed credentials. Codes only: a message never contains a reference, a value or a path."""


class CredentialError(Exception):
    """Base class of the errors below."""


class CredentialValidationError(CredentialError):
    """The request is not acceptable. ``fields`` maps a field name to a list of stable error codes (``invalid_reference``, ``invalid_secret``...). Never the offending value."""

    def __init__(self, fields: dict):
        self.fields = {name: list(codes) for name, codes in fields.items()}
        super().__init__("The credential request is not valid (" + ", ".join(sorted(self.fields)) + ").")


class CredentialNotFoundError(CredentialError):
    """No managed credential is stored under that reference."""

    reason = "credential_not_found"

    def __init__(self):
        super().__init__("No credential is stored under that reference.")


class CredentialAlreadyExistsError(CredentialError):
    """A credential is already stored under that reference: creating never overwrites; replacing is a separate, explicit operation."""

    reason = "credential_already_exists"

    def __init__(self):
        super().__init__("A credential is already stored under that reference.")


class CredentialInUseError(CredentialError):
    """A stored connection reads this credential: it cannot be deleted until no connection does. ``connections`` are connection identifiers (never names or values)."""

    reason = "credential_in_use"

    def __init__(self, connections: tuple):
        self.connections = tuple(connections)
        super().__init__("A connection uses this credential.")


class CredentialStoreError(CredentialError):
    """The secret store could not do it. ``reason`` is the secret store's own stable code (``secret_store_disabled``, ``secret_store_unavailable``, ``secret_store_busy``,
    ``secret_unreadable``). Nothing else is tried: there is no second store and no environment fallback."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("The secret store could not complete the operation.")
