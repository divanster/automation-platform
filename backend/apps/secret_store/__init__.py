"""Backend-only secret store abstraction (AI-SEC-1, first step). Not a Django app: no model, no migration, no settings. See ``store.py`` for the contract."""

from .errors import (
    CODE_ALREADY_EXISTS,
    CODE_DISABLED,
    CODE_INVALID_REFERENCE,
    CODE_INVALID_SECRET,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODES,
    SecretStoreError,
)
from .store import DisabledSecretStore, SecretStore
from .values import MAX_SECRET_LENGTH, SecretValue, validate_reference

__all__ = [
    "CODES",
    "CODE_ALREADY_EXISTS",
    "CODE_DISABLED",
    "CODE_INVALID_REFERENCE",
    "CODE_INVALID_SECRET",
    "CODE_NOT_FOUND",
    "CODE_UNAVAILABLE",
    "MAX_SECRET_LENGTH",
    "DisabledSecretStore",
    "SecretStore",
    "SecretStoreError",
    "SecretValue",
    "validate_reference",
]
