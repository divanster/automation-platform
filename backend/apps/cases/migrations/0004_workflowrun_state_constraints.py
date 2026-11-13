"""CHECK constraints for the run state and failed step (after the backfill in 0003)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0003_backfill_run_state'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(('state__in', ['queued', 'extracting', 'needs_review', 'approved', 'exporting', 'exported', 'failed', 'cancelled'])), name='cases_run_state_valid'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(('failed_step__in', ['', 'extract', 'export'])), name='cases_run_failed_step_valid'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('state', 'failed'), models.Q(('failed_step', ''), _negated=True)), models.Q(models.Q(('state', 'failed'), _negated=True), ('failed_step', '')), _connector='OR'), name='cases_run_failed_step_matches_state'),
        ),
    ]
