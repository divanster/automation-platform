"""Project test runner.

Tests tagged ``requires_postgres`` are excluded unless ``AP_RUN_DB_TESTS=1`` is
set explicitly. Without this, a plain ``manage.py test`` would still ask Django
to set up (create) a test database on whatever server the environment points at,
even though the PostgreSQL tests themselves would only be skipped.
"""

import os

from django.test.runner import DiscoverRunner

DB_TAG = "requires_postgres"


class ProjectTestRunner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if os.environ.get("AP_RUN_DB_TESTS") != "1":
            self.exclude_tags = set(self.exclude_tags) | {DB_TAG}
