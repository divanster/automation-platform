"""The secret store interface and the disabled backend. Pure standard library: no Django, no database, no environment, no logging, no network.

CONTRACT. Four operations on opaque references:

* ``put(reference, value, *, replace=False)``: with ``replace=False`` it CREATES and refuses an existing reference (``secret_already_exists``); with ``replace=True`` it
  REPLACES and refuses a missing one (``secret_not_found``). There is no silent upsert, so a rotation is always a deliberate ``replace=True``. Returns ``None``.
* ``get(reference) -> SecretValue``: the stored secret, or ``secret_not_found``. Never a default, never a value from another source.
* ``delete(reference)``: removes it, or ``secret_not_found``. Returns ``None``.
* ``exists(reference) -> bool``: whether a secret is stored. A store that cannot answer RAISES; it never answers ``False`` in place of an error.

The public methods validate the reference (and the value) and then call the backend hooks, so no backend sees an unvalidated reference. A backend raises ``SecretStoreError``
and nothing else that could carry a value; any other exception from a hook is replaced by ``secret_store_unavailable`` with the original cause dropped (its text could hold
a secret). There is no fallback between stores and none to the process environment: an unavailable secret is an error.

A ``SecretValue`` is returned for reading and accepted for writing; plain text is accepted only on ``put``. Nothing here logs, prints or persists a value, and no error or ``repr``
carries a reference or a value.
"""

from abc import ABC, abstractmethod

from .errors import CODE_DISABLED, CODE_UNAVAILABLE, SecretStoreError
from .values import SecretValue, as_secret, validate_reference


class SecretStore(ABC):
    def put(self, reference, value, *, replace: bool = False) -> None:
        reference = validate_reference(reference)
        secret = as_secret(value)
        self._guard(self._put, reference, secret, bool(replace))

    def get(self, reference) -> SecretValue:
        result = self._guard(self._get, validate_reference(reference))
        if not isinstance(result, SecretValue):
            raise SecretStoreError(CODE_UNAVAILABLE)
        return result

    def delete(self, reference) -> None:
        self._guard(self._delete, validate_reference(reference))

    def exists(self, reference) -> bool:
        result = self._guard(self._exists, validate_reference(reference))
        if not isinstance(result, bool):
            raise SecretStoreError(CODE_UNAVAILABLE)
        return result

    @staticmethod
    def _guard(hook, *args):
        try:
            return hook(*args)
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreError(CODE_UNAVAILABLE) from None

    @abstractmethod
    def _put(self, reference: str, secret: SecretValue, replace: bool) -> None:
        """Create (``replace`` false: refuse an existing reference) or replace (``replace`` true: refuse a missing one)."""

    @abstractmethod
    def _get(self, reference: str) -> SecretValue: ...

    @abstractmethod
    def _delete(self, reference: str) -> None: ...

    @abstractmethod
    def _exists(self, reference: str) -> bool: ...

    def __repr__(self):
        return f"<{type(self).__name__}>"


class DisabledSecretStore(SecretStore):
    """The safe default: every operation, including ``exists``, fails with ``secret_store_disabled``. It stores nothing and reads nothing, so a feature that needs a secret
    stops instead of continuing without one."""

    def _put(self, reference, secret, replace):
        raise SecretStoreError(CODE_DISABLED)

    def _get(self, reference):
        raise SecretStoreError(CODE_DISABLED)

    def _delete(self, reference):
        raise SecretStoreError(CODE_DISABLED)

    def _exists(self, reference):
        raise SecretStoreError(CODE_DISABLED)
