"""Step 1 of 3: add ``User.public_id`` as a nullable column (no default, no constraint).

Adding a nullable column is a metadata-only change in PostgreSQL: existing rows get NULL and nothing is
rewritten. The values are filled by ``0003_backfill_user_public_id`` and the column is tightened by
``0004_user_public_id_constraints``. Each step is its own migration, so each is its own PostgreSQL
transaction: the data update never shares a transaction with a table alteration.

Run these migrations with the application stopped: between 0002 and 0004 a user created by code that does
not know the column would get NULL, and 0004 would then fail (it would not damage anything).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(editable=False, null=True),
        ),
    ]
