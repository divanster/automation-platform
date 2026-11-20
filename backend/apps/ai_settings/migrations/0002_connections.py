"""AI-SET-2: named AI connections stored in the one settings row, and the selected connection.

Additive and reversible. Two columns are added with safe defaults (``connection_id`` NULL, ``connections`` an empty list), so every existing row stays valid and
keeps meaning what it meant: no ``connection_id`` is the legacy, environment-configured provider. The all-or-nothing CHECK constraint is replaced by one that also
requires ``connection_id`` to be NULL in the deterministic mode; every existing row satisfies it (a deterministic row never had a provider, and ``connection_id`` is
NULL). No data is read, written or deleted, and the migration runs no Python and no raw SQL.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_settings', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='aiextractionsettings',
            name='connection_id',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='aiextractionsettings',
            name='connections',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RemoveConstraint(
            model_name='aiextractionsettings',
            name='ai_settings_all_or_nothing',
        ),
        migrations.AddConstraint(
            model_name='aiextractionsettings',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('connection_id__isnull', True), ('extraction_mode', 'deterministic'), ('policy_mode__isnull', True), ('profile_id__isnull', True), ('provider_id__isnull', True), ('timeout_seconds__isnull', True)), models.Q(('extraction_mode', 'ai'), ('policy_mode__isnull', False), ('profile_id__isnull', False), ('provider_id__isnull', False), ('timeout_seconds__isnull', False)), _connector='OR'), name='ai_settings_all_or_nothing_v2'),
        ),
    ]
