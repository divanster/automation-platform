"""Step D: make the attribution columns NOT NULL and add their CHECK constraints (after the validation in 0008).

Schema only. Every existing run already has a valid attribution (0008 would have stopped otherwise), so PostgreSQL accepts the
constraints; if one did not, the database refuses the migration and nothing is changed. The constraints check shape only: module id and
workflow key are shaped like the manifest's identifiers and the version is between 1 and 2147483647. Which modules and workflows exist
is the registry's knowledge, not the database's. Reversing removes the constraints and makes the columns nullable again; the values stay.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0008_validate_run_attribution'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workflowrun',
            name='module_id',
            field=models.CharField(help_text='The module that owns the workflow this run was created under. Never changes.', max_length=40),
        ),
        migrations.AlterField(
            model_name='workflowrun',
            name='workflow_key',
            field=models.CharField(help_text='The workflow this run belongs to. Never changes.', max_length=64),
        ),
        migrations.AlterField(
            model_name='workflowrun',
            name='workflow_version',
            field=models.PositiveIntegerField(help_text='The workflow definition version the run started on. Never changes.'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(('module_id__regex', '^[a-z][a-z0-9_]{2,39}$')), name='cases_run_module_id_shape'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(('workflow_key__regex', '^[a-z][a-z0-9_]{0,39}(\\.[a-z][a-z0-9_]{0,39})*$')), name='cases_run_workflow_key_shape'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(('workflow_version__gte', 1), ('workflow_version__lte', 2147483647)), name='cases_run_workflow_version_positive'),
        ),
    ]
