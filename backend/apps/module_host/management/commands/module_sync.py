"""Materialize persistent state for the registered modules (repeatable).

    python manage.py module_sync [--json]

Reads the approved static registry, records namespace claims, creates a state row for each registered module that has none (enabled, unless a
dependency is not), and reports version mismatches. It never enables or disables an existing module, never changes a stored version, never deletes
anything, runs no module migration and installs nothing. Exit status is non-zero when a version mismatch or an unregistered state row needs attention
(the report is still printed and the rest of the sync is kept). Run it after every upgrade, before starting the worker.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.module_host import cli
from apps.module_host.services import sync_modules
from config.modules import build_registry  # the composition root: the one place built-in modules are named


class Command(BaseCommand):
    help = "Create persistent state for the registered modules and report version mismatches (repeatable; changes no existing module's enabled state)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json", help=cli.JSON_HELP)

    def handle(self, *args, **options):
        with cli.guarded():
            report = sync_modules(build_registry())
        if options["as_json"]:
            self.stdout.write(cli.to_json(report.to_dict()))
        else:
            self.stdout.write(f"Registry {cli.short(report.registry_fingerprint)}: {'initial sync' if report.initial else 'sync'} done.")
            for module_id, enabled in report.created:
                self.stdout.write(f"  created   {module_id} ({'enabled' if enabled else 'disabled'})")
            for module_id in report.refreshed:
                self.stdout.write(f"  refreshed {module_id}")
            for module_id in report.unchanged:
                self.stdout.write(f"  unchanged {module_id}")
            for namespace, module_id in report.claims_added:
                self.stdout.write(f"  namespace {namespace} claimed by {module_id}")
            for mismatch in report.version_mismatches:
                self.stdout.write(f"  VERSION MISMATCH {mismatch.module_id}: installed {mismatch.installed_version}, registered {mismatch.registered_version} (unavailable)")
            for module_id in report.unregistered:
                self.stdout.write(f"  UNREGISTERED state {module_id}: kept, not changed")
            if not (report.created or report.refreshed or report.unchanged or report.version_mismatches or report.unregistered):
                self.stdout.write("  no modules are registered.")
        if not report.ok:
            raise CommandError("version_mismatch: a module needs attention (see above); nothing was changed for it.")
