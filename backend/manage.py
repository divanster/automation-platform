#!/usr/bin/env python
"""Django's command-line utility. Defaults to the development settings."""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
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
