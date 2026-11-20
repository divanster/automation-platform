"""Errors of the AI settings. Codes only: a message never contains a submitted or stored value."""


class AISettingsError(Exception):
    """Base class of the errors below."""


class AISettingsValidationError(AISettingsError):
    """The settings are not acceptable. ``fields`` maps a field name to a list of stable error codes (``unknown_profile``, ``out_of_range``, ...)."""

    def __init__(self, fields: dict):
        self.fields = {name: list(codes) for name, codes in fields.items()}
        super().__init__("The AI settings are not valid (" + ", ".join(sorted(self.fields)) + ").")


class AISettingsConflictError(AISettingsError):
    """``expected_revision`` is not the stored revision: somebody saved first, or the client's copy is stale."""

    reason = "stale_settings_revision"

    def __init__(self):
        super().__init__("The settings were changed since they were read.")


class ConnectionNotFoundError(AISettingsError):
    """The connection to test is not among the saved connections."""

    def __init__(self):
        super().__init__("There is no such saved connection.")


class ConnectionTestBusyError(AISettingsError):
    """Too many connection tests are running in this process at once; nothing was sent. The caller may retry shortly."""

    def __init__(self):
        super().__init__("Too many connection tests are running.")


class ConnectionTestRefusedError(AISettingsError):
    """The connection cannot be tested as saved, and nothing was sent. ``code`` is ``connection_disabled`` or ``connection_not_usable``; for the latter ``issue`` is one of
    ``rules.PROVIDER_ISSUES`` (``missing_api_key``, ``endpoint_not_approved``, ``invalid_configuration``)."""

    def __init__(self, code: str, issue: str | None = None):
        self.code = code
        self.issue = issue
        super().__init__("The connection cannot be tested as saved.")
