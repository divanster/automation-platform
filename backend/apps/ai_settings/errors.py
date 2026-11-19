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
