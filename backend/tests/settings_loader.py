"""Test helper: import a settings module fresh under a controlled environment."""

import importlib
import os
import sys
from unittest import mock

_SETTINGS_MODULES = (
    "config.settings.base",
    "config.settings.development",
    "config.settings.production",
)


def load_settings(module, env=None):
    """Import ``module`` with exactly the given ``AP_*`` variables set.

    Any ``AP_*`` variables from the real environment are removed first so a
    developer's local configuration cannot influence the result. Settings
    modules are removed from ``sys.modules`` before and after, so no state leaks
    between tests. Django's own active settings are not changed.
    """
    environment = {k: v for k, v in os.environ.items() if not k.startswith("AP_")}
    environment.update(env or {})
    with mock.patch.dict(os.environ, environment, clear=True):
        for name in _SETTINGS_MODULES:
            sys.modules.pop(name, None)
        try:
            return importlib.import_module(module)
        finally:
            for name in _SETTINGS_MODULES:
                sys.modules.pop(name, None)
