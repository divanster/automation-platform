"""AI-SEC-2 composition: which secret store this process uses, chosen explicitly from the backend environment. Reads the environment only when a function is called.

``AP_SECRET_STORE_BACKEND`` is ``disabled`` (the default: every operation fails with ``secret_store_disabled``) or ``windows_dpapi``. There is no other value, no automatic choice and no
fallback from one store to another or to the process environment: a managed credential that cannot be read is an error.

The ``windows_dpapi`` store (``config/secret_store_dpapi.py``) needs ``AP_SECRET_STORE_DIRECTORY``, an absolute directory outside the repository that an operator prepared with
``prepare_windows_secret_directory`` under the account the processes run as. Optional: ``AP_SECRET_STORE_TIMEOUT_SECONDS`` (the bound on waiting for the cross-process lock),
``AP_SECRET_STORE_APPROVED_IDENTITY_SIDS`` (comma-separated SIDs of built-in service identities the operator allows: ``S-1-5-18``, ``S-1-5-19``, ``S-1-5-20``) and
``AP_SECRET_STORE_EXPECTED_IDENTITY_SID`` (the one SID this process must run as; set it identically for Django and the worker so a process under any other account fails closed).

Every misconfiguration is ``SecretStoreError('secret_store_unavailable')``: a fixed message that names no variable, path or value.
"""

import os

from apps.secret_store import CODE_UNAVAILABLE, DisabledSecretStore, SecretStore, SecretStoreError

ENV_BACKEND = "AP_SECRET_STORE_BACKEND"
ENV_DIRECTORY = "AP_SECRET_STORE_DIRECTORY"
ENV_TIMEOUT = "AP_SECRET_STORE_TIMEOUT_SECONDS"
ENV_APPROVED_SIDS = "AP_SECRET_STORE_APPROVED_IDENTITY_SIDS"
ENV_EXPECTED_SID = "AP_SECRET_STORE_EXPECTED_IDENTITY_SID"
BACKEND_DISABLED = "disabled"
BACKEND_WINDOWS_DPAPI = "windows_dpapi"
BACKENDS = (BACKEND_DISABLED, BACKEND_WINDOWS_DPAPI)


def _read(environ, name):
    return environ.get(name, "").strip() or None  # an empty value counts as unset, the convention of config/env.py


def build_secret_store(environ=None) -> SecretStore:
    """The secret store this process is configured to use. Opens no file; the store checks its directory on every operation."""
    environ = os.environ if environ is None else environ
    backend = _read(environ, ENV_BACKEND) or BACKEND_DISABLED
    if backend == BACKEND_DISABLED:
        return DisabledSecretStore()
    if backend != BACKEND_WINDOWS_DPAPI:
        raise SecretStoreError(CODE_UNAVAILABLE)
    directory = _read(environ, ENV_DIRECTORY)
    if directory is None:
        raise SecretStoreError(CODE_UNAVAILABLE)
    from config.secret_store_dpapi import DEFAULT_TIMEOUT_SECONDS, WindowsDpapiSecretStore

    timeout_text = _read(environ, ENV_TIMEOUT)
    try:
        timeout = DEFAULT_TIMEOUT_SECONDS if timeout_text is None else float(timeout_text)
    except ValueError:
        raise SecretStoreError(CODE_UNAVAILABLE) from None
    approved_text = _read(environ, ENV_APPROVED_SIDS)
    approved = () if approved_text is None else tuple(part.strip() for part in approved_text.split(","))
    return WindowsDpapiSecretStore(directory, timeout=timeout, approved_identity_sids=approved, expected_identity_sid=_read(environ, ENV_EXPECTED_SID))
