"""Helpers for reading configuration from process environment variables.

Settings read ``os.environ`` only. No ``.env`` file is read by the application;
see docs/development/runbook.md for how a developer supplies variables.
"""

import os

from django.core.exceptions import ImproperlyConfigured

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def get_str(name, default=None, *, required=False):
    """Return a stripped string setting; empty values count as unset."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return default


def get_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ImproperlyConfigured(f"Environment variable {name} must be a boolean (true/false).")


def get_int(name, default, *, minimum=None):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ImproperlyConfigured(f"Environment variable {name} must be an integer.") from None
    if minimum is not None and number < minimum:
        raise ImproperlyConfigured(f"Environment variable {name} must be at least {minimum}.")
    return number


def get_list(name, default=()):
    """Return a comma-separated setting as a list of non-empty stripped items."""
    value = os.environ.get(name, "")
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items if items else list(default)
