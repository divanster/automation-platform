"""Test-only helpers for the secret store: an in-memory backend that implements the contract (a future real backend must pass the same contract tests)."""

from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_NOT_FOUND,
    SecretStore,
    SecretStoreError,
    SecretValue,
)

SECRET_A = "sk-test-SENTINEL-alpha-1f9e"
SECRET_B = "sk-test-SENTINEL-bravo-7c2d"
REF_A = "AP_AI_CRED_CORP_LLM"
REF_B = "workbench.prod:key-1"
ALL_SENTINELS = (SECRET_A, SECRET_B)


class InMemorySecretStore(SecretStore):
    """Holds secrets in a dict. For tests only; production code must never use it."""

    def __init__(self):
        self._items = {}

    def _put(self, reference, secret, replace):
        present = reference in self._items
        if replace and not present:
            raise SecretStoreError(CODE_NOT_FOUND)
        if not replace and present:
            raise SecretStoreError(CODE_ALREADY_EXISTS)
        self._items[reference] = secret

    def _get(self, reference):
        try:
            return self._items[reference]
        except KeyError:
            raise SecretStoreError(CODE_NOT_FOUND) from None

    def _delete(self, reference):
        if reference not in self._items:
            raise SecretStoreError(CODE_NOT_FOUND)
        del self._items[reference]

    def _exists(self, reference):
        return reference in self._items


class RaisingSecretStore(SecretStore):
    """A backend whose hooks raise arbitrary exceptions that carry a secret, to prove the base class drops them."""

    def __init__(self, error):
        self.error = error

    def _put(self, reference, secret, replace):
        raise self.error

    def _get(self, reference):
        raise self.error

    def _delete(self, reference):
        raise self.error

    def _exists(self, reference):
        raise self.error


class WrongTypeSecretStore(SecretStore):
    """A broken backend that returns the wrong types: the base class must fail closed rather than pass them on."""

    def _put(self, reference, secret, replace):
        return None

    def _get(self, reference):
        return SECRET_A  # plain text instead of a SecretValue

    def _delete(self, reference):
        return None

    def _exists(self, reference):
        return "yes"


def reveal(value: SecretValue) -> str:
    return value.reveal()
