"""The installed extractor's identity, read from package metadata WITHOUT importing the library (so the web process never loads a PDF parser)."""

from importlib import metadata

from .rules import EXTRACTOR_NAME

UNKNOWN_VERSION = "unknown"


def installed_version() -> str:
    """The installed version of the extraction library, or ``unknown`` when it is not installed."""
    try:
        return metadata.version(EXTRACTOR_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION
