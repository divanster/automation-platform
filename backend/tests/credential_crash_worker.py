"""A test-only child process for the crash tests of ``tests/test_ai_credentials_postgres.py`` (AI-SEC-2 transaction hardening). Not a test module.

``python -m tests.credential_crash_worker <create|replace|delete> <before_store|after_store>`` runs ONE credential operation through the real services and the real database named by the
environment (the parent sets ``AP_DB_NAME`` to its test database and the path of a JSON file as the secret store in ``AP_CRASH_STORE_FILE``), and kills the process with ``os._exit(137)`` at the
named point: ``before_store`` right after the audit INSERT (the secret store untouched), ``after_store`` right after the secret store changed and before the commit. ``AP_CRASH_USER_PK`` is the
administrator. Every secret is a sentinel; nothing here touches a real credential, a provider or the network. This file is ASCII only.
"""

import json
import os
import sys

from apps.secret_store import CODE_ALREADY_EXISTS, CODE_NOT_FOUND, SecretStore, SecretStoreError

REFERENCE = "AP_AI_CRED_CRASH_TEST"
SECRET = "CRASH-SENTINEL-one-4c1e"
NEW_SECRET = "CRASH-SENTINEL-two-9b77"


class FileStore(SecretStore):
    """A JSON file as a secret store, so a SECOND process can see what the first one changed. ``after_store`` makes it kill the process right after it wrote."""

    backend_id = "file_test"

    def __init__(self, path, crash_after_change=False):
        self.path = path
        self.crash_after_change = crash_after_change

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def _save(self, items):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(items, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if self.crash_after_change:
            os._exit(137)

    def _put(self, reference, secret, replace):
        items = self._load()
        if replace and reference not in items:
            raise SecretStoreError(CODE_NOT_FOUND)
        if not replace and reference in items:
            raise SecretStoreError(CODE_ALREADY_EXISTS)
        items[reference] = secret.reveal()
        self._save(items)

    def _get(self, reference):
        from apps.secret_store import SecretValue

        try:
            return SecretValue(self._load()[reference])
        except KeyError:
            raise SecretStoreError(CODE_NOT_FOUND) from None

    def _delete(self, reference):
        items = self._load()
        if reference not in items:
            raise SecretStoreError(CODE_NOT_FOUND)
        del items[reference]
        self._save(items)

    def _exists(self, reference):
        return reference in self._load()


def main(operation, point):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    import django

    django.setup()
    from apps.accounts.models import User
    from apps.ai_credentials import services

    user = User.objects.get(pk=int(os.environ["AP_CRASH_USER_PK"]))
    store = FileStore(os.environ["AP_CRASH_STORE_FILE"], crash_after_change=(point == "after_store"))
    if point == "before_store":
        real_record = services.record_event

        def record_then_die(event):
            real_record(event)
            os._exit(137)  # the audit INSERT was sent inside the open transaction; the server rolls it back when the connection dies

        services.record_event = record_then_die
    if operation == "create":
        services.create_credential(user=user, reference=REFERENCE, value=SECRET, store_factory=lambda: store)
    elif operation == "replace":
        services.replace_credential(user=user, reference=REFERENCE, value=NEW_SECRET, store_factory=lambda: store)
    else:
        services.delete_credential(user=user, reference=REFERENCE, store_factory=lambda: store)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
