"""Reference and value types for the secret store. Pure standard library."""

import hmac
import re

from .errors import CODE_INVALID_REFERENCE, CODE_INVALID_SECRET, SecretStoreError

# An opaque reference: a bounded name the store looks up and never interprets, parses or derives anything from. It is not a secret, but it is kept out of errors anyway.
_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}")
MAX_SECRET_LENGTH = 4096
_REDACTED = "[redacted secret]"


def validate_reference(reference) -> str:
    """``reference`` unchanged when it is an acceptable opaque reference, else ``SecretStoreError('invalid_reference')``. Never coerces, strips or normalizes."""
    if not isinstance(reference, str) or _REFERENCE.fullmatch(reference) is None:
        raise SecretStoreError(CODE_INVALID_REFERENCE)
    return reference


class SecretValue:
    """A secret held in memory. Its text is only available through ``reveal()``, for the one caller that must hand it to a provider.

    ``repr``, ``str`` and ``format`` are redacted, it cannot be pickled or copied, it is unhashable, and equality is constant-time and only against another ``SecretValue``.
    This limits accidental exposure (logs, tracebacks, debug output); it does not erase memory and is not a defence against code that calls ``reveal()``."""

    __slots__ = ("_text",)

    def __init__(self, text):
        if not isinstance(text, str) or not text or len(text) > MAX_SECRET_LENGTH or not text.isprintable() or text != text.strip():
            raise SecretStoreError(CODE_INVALID_SECRET)
        object.__setattr__(self, "_text", text)

    def reveal(self) -> str:
        return self._text

    def __setattr__(self, name, value):
        raise AttributeError("SecretValue is immutable.")

    def __delattr__(self, name):
        raise AttributeError("SecretValue is immutable.")

    def __repr__(self):
        return f"<{_REDACTED}>"

    def __str__(self):
        return _REDACTED

    def __format__(self, spec):
        return _REDACTED

    def __eq__(self, other):
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self._text.encode(), other._text.encode())

    __hash__ = None

    def __reduce__(self):
        raise TypeError("SecretValue cannot be serialized or copied.")

    def __reduce_ex__(self, protocol):
        raise TypeError("SecretValue cannot be serialized or copied.")


def as_secret(value) -> SecretValue:
    """``value`` as a ``SecretValue`` (a ``str`` is validated and wrapped); anything else is ``invalid_secret``."""
    return value if isinstance(value, SecretValue) else SecretValue(value)
