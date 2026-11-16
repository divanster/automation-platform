#!/usr/bin/env python
"""Django's command-line utility. Defaults to the development settings.

For the development settings only, variables missing from the process environment are filled in from the git-ignored
``.env`` file in the repository root (``config/localenv.py``). Existing environment variables always win, and any other
settings module (``--settings`` or ``DJANGO_SETTINGS_MODULE``) reads no file.
"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    from config.localenv import LocalEnvError, load_local_env, requested_settings_module

    try:
        load_local_env(requested_settings_module(sys.argv, os.environ))
    except LocalEnvError as exc:
        sys.exit(f"Error in .env (see .env.example): {exc}")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not installed or the virtual environment is not active. "
            "See docs/development/runbook.md."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
