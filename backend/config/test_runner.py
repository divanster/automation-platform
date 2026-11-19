"""Project test runner.

Tests tagged ``requires_postgres`` are excluded unless ``AP_RUN_DB_TESTS=1`` is
set explicitly. Without this, a plain ``manage.py test`` would still ask Django
to set up (create) a test database on whatever server the environment points at,
even though the PostgreSQL tests themselves would only be skipped.

AI settings never reach a test (AI-2a). ``manage.py`` copies ``AP_*`` names from the developer's git-ignored ``.env`` into the process before the runner starts,
and the owner's ``.env`` may legitimately hold the Workbench key or even ``AP_INTAKE_EXTRACTION_MODE=ai`` for a live run. A test that starts the worker command, or one of
its child processes, would then build the real AI extractor and could send text to the real service. So the runner removes every ``AP_INTAKE_*`` and ``AP_AI_*`` variable
from the process at start-up; a test that needs one passes an explicit environment (the AI tests do) and the automated tests use a mocked transport only.
"""

import os

from django.test.runner import DiscoverRunner

DB_TAG = "requires_postgres"
AI_ENVIRONMENT_PREFIXES = ("AP_INTAKE_", "AP_AI_")


class ProjectTestRunner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in [name for name in os.environ if name.startswith(AI_ENVIRONMENT_PREFIXES)]:
            del os.environ[name]
        if os.environ.get("AP_RUN_DB_TESTS") != "1":
            self.exclude_tags = set(self.exclude_tags) | {DB_TAG}
