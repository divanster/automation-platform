"""Create or update the Operator, Approver and Auditor groups. Safe to run again.

    python manage.py setup_roles

Needs a migrated database: Django creates the permissions when ``migrate`` runs, and this command only
looks them up. For each role in ``apps.accounts.roles`` it creates the group if it is missing and sets the
group's permissions to EXACTLY the role's set (so a permission removed from a role is removed from the
group on the next run, and one added to it is added). It never creates a permission, never touches users or
their group memberships, never touches any other group, and prints only role names and counts.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.roles import ROLE_PERMISSIONS, ROLES, split_permission


class Command(BaseCommand):
    help = "Create or update the Operator, Approver and Auditor groups with their permissions (repeatable)."

    def handle(self, *args, **options):
        with transaction.atomic():
            found, missing = {}, []
            for name in sorted({name for names in ROLE_PERMISSIONS.values() for name in names}):
                app_label, codename = split_permission(name)
                permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
                if permission is None:
                    missing.append(name)
                else:
                    found[name] = permission
            if missing:
                raise CommandError("These permissions do not exist (run 'python manage.py migrate' first): " + ", ".join(missing))

            for role in ROLES:
                group, created = Group.objects.get_or_create(name=role)
                group.permissions.set([found[name] for name in sorted(ROLE_PERMISSIONS[role])])
                self.stdout.write(f"{role}: {'created' if created else 'updated'}, {len(ROLE_PERMISSIONS[role])} permissions")
