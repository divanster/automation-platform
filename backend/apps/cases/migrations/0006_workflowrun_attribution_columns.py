"""Step A of the workflow attribution migrations: add the three attribution columns to ``WorkflowRun``, NULLABLE.

NON-DESTRUCTIVE. Nothing is dropped, rewritten or defaulted: every existing run keeps its id, state, timestamps, case and
current revision, and simply has no attribution yet (all three columns NULL). The sequence is

* 0006 (this file): add the columns, nullable (schema only);
* 0007: backfill the runs that Request Intake created, on evidence (data only);
* 0008: validate that every run has an attribution, and stop with a count if one does not (read only);
* 0009: make the columns NOT NULL and add the CHECK constraints (schema only).

Data and schema changes never share a migration, so PostgreSQL never sees an update and a table alteration of ``cases_workflowrun``
in one transaction (the convention of 0002 to 0004). Reversing this migration drops the three columns; nothing else is touched.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0005_rejection'),
    ]

    operations = [
        migrations.AddField(
            model_name='workflowrun',
            name='module_id',
            field=models.CharField(help_text='The module that owns the workflow this run was created under. Never changes.', max_length=40, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='workflow_key',
            field=models.CharField(help_text='The workflow this run belongs to. Never changes.', max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='workflow_version',
            field=models.PositiveIntegerField(help_text='The workflow definition version the run started on. Never changes.', null=True),
        ),
    ]
