"""Errors of the text extraction slice. Pure: no Django import. A message never holds extracted text, a file name, a path or a hash."""


class ExtractionNotReadyError(Exception):
    """The extraction has no result yet (it is queued or running), so there is no text to read."""

    def __init__(self):
        super().__init__("The extraction has no result yet.")


class ExtractionRecordMissingError(LookupError):
    """The task names an extraction that does not exist. Retrying cannot change that."""

    def __init__(self):
        super().__init__("The extraction request does not exist.")


class ExtractionRuntimeMissingError(Exception):
    """The worker was started without the extraction runtime (the composition root installs it). Retrying in this process cannot change that."""

    def __init__(self):
        super().__init__("The text extraction runtime is not installed in this process.")


class ExtractionConfigurationError(ValueError):
    """An extraction setting is missing or invalid. The message names the setting, never its value."""
