"""Step 2 of 3: give every existing user its OWN random ``public_id``.

Each user is updated individually with a freshly generated ``uuid4``. A single default value must never
be applied to many rows: that would give every user the same identity and make the unique constraint in
step 3 fail. Only rows that are still NULL are touched, so the migration is safe to re-run, and a user
that already has a value (created between steps, or by a partial earlier run) keeps it.

Reversing is a no-op: the column itself is dropped by reversing step 1.
"""

import uuid

from django.db import migrations


def backfill_public_ids(apps, schema_editor):
    user_model = apps.get_model("accounts", "User")
    users = user_model._default_manager.using(schema_editor.connection.alias)
    missing = list(users.filter(public_id__isnull=True).values_list("pk", flat=True))
    for pk in missing:
        users.filter(pk=pk, public_id__isnull=True).update(public_id=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_public_id"),
    ]

    operations = [
        migrations.RunPython(backfill_public_ids, migrations.RunPython.noop),
    ]
