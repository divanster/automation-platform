"""DOC-1 composition: which document storage this process uses, chosen explicitly from the backend environment. Reads the environment only when a function is called.

``AP_DOCUMENT_STORAGE_DIRECTORY`` is an absolute directory outside the repository (and outside any static or media root) that an operator prepared with ``manage.py prepare_document_storage`` under the
account the processes run as. It has NO default: unset means document storage is DISABLED and every upload and download fails closed with ``document_storage_disabled``. ``AP_DOCUMENT_MAX_BYTES`` is
the size limit of one document (default 20 MiB; between 1 KiB and the 100 MiB hard maximum; anything else is a configuration error, not a clamp).

The directory policy is the platform's: on POSIX ``apps.documents.storage.PosixDirectoryPolicy`` (owner, mode ``0700`` / ``0600``); on Windows the hardened policy of the secret store
(``config/secret_store_dpapi.WindowsDirectoryPolicy``: a protected DACL for the current identity, SYSTEM and Administrators, local fixed ACL-capable volume, no links), wrapped so its errors become the
document storage's. Windows optionally takes ``AP_DOCUMENT_STORAGE_APPROVED_IDENTITY_SIDS`` (comma-separated built-in service identities the operator allows) and
``AP_DOCUMENT_STORAGE_EXPECTED_IDENTITY_SID`` (the one SID this process must run as; set it identically for every process that serves documents). Any other platform fails closed.

Every misconfiguration is ``DocumentStorageError('document_storage_unavailable')`` (or ``..._disabled`` when unset): a fixed message that names no variable, path or value.
"""

import os
import sys
from pathlib import Path

from django.conf import settings

from apps.documents.errors import CODE_DISABLED, CODE_UNAVAILABLE, DocumentStorageError
from apps.documents.rules import DEFAULT_MAX_BYTES, is_valid_max_bytes
from apps.documents.storage import DirectoryPolicy, DocumentStore, PosixDirectoryPolicy, prepare_directory

ENV_DIRECTORY = "AP_DOCUMENT_STORAGE_DIRECTORY"
ENV_MAX_BYTES = "AP_DOCUMENT_MAX_BYTES"
ENV_APPROVED_SIDS = "AP_DOCUMENT_STORAGE_APPROVED_IDENTITY_SIDS"
ENV_EXPECTED_SID = "AP_DOCUMENT_STORAGE_EXPECTED_IDENTITY_SID"


def _read(environ, name):
    return environ.get(name, "").strip() or None  # an empty value counts as unset, the convention of config/env.py


def forbidden_roots() -> tuple:
    """Where the storage must never be: the repository (the source tree) and any static or media root the settings name."""
    roots = [Path(settings.BASE_DIR).parent]
    roots += [Path(value) for value in (getattr(settings, "STATIC_ROOT", None), getattr(settings, "MEDIA_ROOT", None)) if value]
    return tuple(str(root) for root in roots)


def configured_max_bytes(environ=None) -> int:
    environ = os.environ if environ is None else environ
    text = _read(environ, ENV_MAX_BYTES)
    if text is None:
        return DEFAULT_MAX_BYTES
    try:
        value = int(text)
    except ValueError:
        raise DocumentStorageError(CODE_UNAVAILABLE) from None
    if not is_valid_max_bytes(value):
        raise DocumentStorageError(CODE_UNAVAILABLE)
    return value


class _WindowsPolicy(DirectoryPolicy):
    """The secret store's Windows directory policy, with its errors translated: the document storage has its own error type."""

    def __init__(self, environ):
        from config.secret_store_dpapi import SecretStoreError, WindowsDirectoryPolicy

        self._error = SecretStoreError
        approved_text = _read(environ, ENV_APPROVED_SIDS)
        approved = () if approved_text is None else tuple(part.strip() for part in approved_text.split(","))
        try:
            self._inner = WindowsDirectoryPolicy(approved, _read(environ, ENV_EXPECTED_SID))
        except SecretStoreError:
            raise DocumentStorageError(CODE_UNAVAILABLE) from None

    def _run(self, operation, *args):
        try:
            return operation(*args)
        except self._error:
            raise DocumentStorageError(CODE_UNAVAILABLE) from None

    def create(self, path):
        self._run(self._inner.create, path)

    def verify(self, path):
        self._run(self._inner.verify, path)

    def verify_file(self, descriptor):
        self._run(self._inner.verify_file, descriptor)


def _platform_policy(environ) -> DirectoryPolicy:
    if sys.platform == "win32":
        return _WindowsPolicy(environ)
    if os.name == "posix":
        return PosixDirectoryPolicy()
    raise DocumentStorageError(CODE_UNAVAILABLE)


def _directory(environ):
    directory = _read(environ, ENV_DIRECTORY)
    if directory is None:
        raise DocumentStorageError(CODE_DISABLED)
    return directory


def build_document_store(environ=None, policy=None) -> DocumentStore:
    """The document store this process is configured to use. ``policy`` is for tests; production takes the platform's."""
    environ = os.environ if environ is None else environ
    directory = _directory(environ)
    limit = configured_max_bytes(environ)
    return DocumentStore(directory, _platform_policy(environ) if policy is None else policy, max_bytes=limit, forbidden=forbidden_roots())


def prepare_document_storage(environ=None, policy=None) -> None:
    """The operator's explicit set-up of the storage directory (repeatable; see ``apps.documents.storage.prepare_directory``)."""
    environ = os.environ if environ is None else environ
    directory = _directory(environ)
    prepare_directory(directory, _platform_policy(environ) if policy is None else policy, forbidden_roots())
