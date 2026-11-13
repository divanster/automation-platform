"""Add the run state and failed step to ``WorkflowRun`` (columns only; no constraint yet).

NON-DESTRUCTIVE. Both columns are added with a default, so every existing row stays valid: a run
that exists today becomes ``queued`` with no failed step. Nothing is dropped or rewritten. The data
backfill is migration 0003 and the CHECK constraints are migration 0004: separate migrations run in
separate transactions, so PostgreSQL never sees a data update and a table alteration in one
transaction. See docs/development/request-intake-orchestration-slice.md for the check to run on the
local database before applying these migrations.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='workflowrun',
            name='failed_step',
            field=models.CharField(blank=True, default='', help_text='Set only while the run is failed.', max_length=16),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='state',
            field=models.CharField(default='queued', max_length=16),
        ),
    ]
