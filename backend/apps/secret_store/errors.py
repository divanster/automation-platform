"""Stable error codes for the secret store. A message is fixed text: it never contains a reference, a value, a path or a backend's own message."""

CODE_INVALID_REFERENCE = "invalid_reference"
CODE_INVALID_SECRET = "invalid_secret"
CODE_NOT_FOUND = "secret_not_found"
CODE_ALREADY_EXISTS = "secret_already_exists"
CODE_DISABLED = "secret_store_disabled"
CODE_UNAVAILABLE = "secret_store_unavailable"

_MESSAGES = {
    CODE_INVALID_REFERENCE: "The secret reference is not valid.",
    CODE_INVALID_SECRET: "The secret value is not acceptable.",
    CODE_NOT_FOUND: "No secret is stored under that reference.",
    CODE_ALREADY_EXISTS: "A secret is already stored under that reference.",
    CODE_DISABLED: "The secret store is disabled.",
    CODE_UNAVAILABLE: "The secret store is unavailable.",
}
CODES = tuple(_MESSAGES)


class SecretStoreError(Exception):
    """A refused or failed secret operation. ``code`` is one of ``CODES``; the message is fixed by the code. Backends must raise this and nothing that carries a value."""

    def __init__(self, code: str):
        if code not in _MESSAGES:
            code = CODE_UNAVAILABLE
        super().__init__(_MESSAGES[code])
        self.code = code

    def __reduce__(self):
        return (SecretStoreError, (self.code,))
