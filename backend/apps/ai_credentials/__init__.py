"""Managed AI credentials (AI-SEC-2): the administrator's way to create, replace, delete and check the secrets AI connections read from the secret store.

Not a Django app: no model and no migration. A credential lives ONLY in the secret store (``apps.secret_store``, in production the hardened Windows DPAPI store); nothing here, in the
database, in an audit event, a log line, an error or a response holds its value. See ``services.py`` for the contract.
"""
