"""Step 3 of 3: make ``User.public_id`` NOT NULL, UNIQUE, and defaulted to a random UUID.

Runs after the backfill, in its own transaction. Django's null-to-not-null step first updates rows that
are still NULL with ONE default value; after step 2 there are none, so that update touches no row. If a
NULL somehow remained, the unique constraint added here would refuse the duplicate values instead of
silently accepting them.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_backfill_user_public_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
