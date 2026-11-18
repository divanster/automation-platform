"""Enable one registered module.

    python manage.py module_enable <module_id> [--json]

Refused for an unknown module, a missing state row (run module_sync), a version mismatch, or a dependency that is not enabled. Enabling an enabled module changes nothing and records nothing. Held tasks of the module become claimable again; nothing is re-run.
The change and its audit event (``module.enabled``, system actor) are one transaction. No public API exists for this in S4.
"""

from django.core.management.base import BaseCommand

from apps.module_host import cli
from apps.module_host.services import enable_module
from config.modules import build_registry


class Command(BaseCommand):
    help = "Enable one registered module. Transactional and idempotent; audited."

    def add_arguments(self, parser):
        parser.add_argument("module_id", type=cli.module_id_argument, help="The id of a registered module.")
        parser.add_argument("--json", action="store_true", dest="as_json", help=cli.JSON_HELP)

    def handle(self, *args, **options):
        with cli.guarded():
            result = enable_module(build_registry(), options["module_id"])
        if options["as_json"]:
            self.stdout.write(cli.to_json({"format": 1, "module_id": result.module_id, "changed": result.changed, "enabled": result.enabled}))
        else:
            self.stdout.write(f"{result.module_id}: {'enabled' if result.changed else 'already enabled (no change)'}.")
