"""Django-dependent checks for the request-intake package (run through ``manage.py test``).

The package itself is plain Python; its tests (test_request_intake_*.py) run with plain
``python -m unittest``. This small module is the only one that needs Django settings: it
checks that the package is not wired into the Django project.
"""

from django.conf import settings
from django.test import SimpleTestCase


class NotWiredIntoDjangoTests(SimpleTestCase):
    def test_workflows_are_not_installed_apps(self):
        self.assertFalse([app for app in settings.INSTALLED_APPS if "workflows" in app])

    def test_only_the_planned_project_apps_are_installed(self):
        self.assertEqual([a for a in settings.INSTALLED_APPS if a.startswith("apps.")], ["apps.accounts", "apps.health"])
