"""Disable one registered module (read-only mode).

    python manage.py module_disable <module_id> [--json]

Refused for an unknown module, a missing state row, or while an enabled module depends on it. Disabling a disabled module changes nothing and records nothing. Queued tasks are held (not failed), running attempts finish, and no data or task is deleted.
The change and its audit event (``module.disabled``, system actor) are one transaction. No public API exists for this in S4.
"""

from django.core.management.base import BaseCommand

from apps.module_host import cli
from apps.module_host.services import disable_module
from config.modules import build_registry


class Command(BaseCommand):
    help = "Disable one registered module (read-only mode). Transactional and idempotent; audited."

    def add_arguments(self, parser):
        parser.add_argument("module_id", type=cli.module_id_argument, help="The id of a registered module.")
        parser.add_argument("--json", action="store_true", dest="as_json", help=cli.JSON_HELP)

    def handle(self, *args, **options):
        with cli.guarded():
            result = disable_module(build_registry(), options["module_id"])
        if options["as_json"]:
            self.stdout.write(cli.to_json({"format": 1, "module_id": result.module_id, "changed": result.changed, "enabled": result.enabled}))
        else:
            self.stdout.write(f"{result.module_id}: {'disabled' if result.changed else 'already disabled (no change)'}.")
