"""Show the persistent state of the registered modules (read only).

    python manage.py module_status [module_id] [--json]

Prints, for each registered module, the registered and installed versions, whether it is enabled, whether the worker gate lets its task kinds
through (``claimable``) and why. Changes nothing.
"""

from django.core.management.base import BaseCommand

from apps.module_host import cli
from apps.module_host.services import get_module_status
from config.modules import build_registry


class Command(BaseCommand):
    help = "Show module state: versions, enabled flag, and whether the worker may claim the module's task kinds (read only)."

    def add_arguments(self, parser):
        parser.add_argument("module_id", nargs="?", type=cli.module_id_argument, default=None, help="Show only this module.")
        parser.add_argument("--json", action="store_true", dest="as_json", help=cli.JSON_HELP)

    def handle(self, *args, **options):
        with cli.guarded():
            status = get_module_status(build_registry())
            selected = None if options["module_id"] is None else status.module(options["module_id"])
        if options["as_json"]:
            document = status.to_dict()
            if selected is not None:
                document["modules"] = [selected.to_dict()]
            self.stdout.write(cli.to_json(document))
            return
        self.stdout.write(f"Registry {cli.short(status.registry_fingerprint)}; synced: {'yes' if status.synced else 'no (modules without state are treated as enabled until the first module_sync)'}")
        modules = status.modules if selected is None else (selected,)
        if not modules:
            self.stdout.write("No modules are registered.")
        for item in modules:
            enabled = "no state" if item.enabled is None else ("enabled" if item.enabled else "disabled")
            installed = item.installed_version or "-"
            self.stdout.write(
                f"  {item.module_id}: {enabled}; registered {item.registered_version}, installed {installed}; "
                f"claimable {'yes' if item.claimable else 'no'} ({item.reason}); task kinds: {', '.join(item.task_kinds) or '-'}"
            )
        for module_id in status.unregistered if selected is None else ():
            self.stdout.write(f"  {module_id}: state row without a registry entry (kept, unavailable)")
