"""Give runs that already have a current revision the state ``needs_review``.

Before this migration a run had no state. A run that already holds a current revision is, by the
state machine, waiting for a human, so ``needs_review`` is the only consistent state for it. A run
without a revision keeps the column default (``queued``). Only the ``state`` of rows whose state is
still the default and that point at a revision is updated; the migration touches nothing else and is
safe to run twice. Reversing it changes nothing (the column is dropped by reversing 0002 instead).

It does NOT decide what a pre-existing run "really" was: no approval or export state is inferred.
Those runs were created by the persistence slice's own tests or by hand; see the slice document.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    WorkflowRun = apps.get_model("cases", "WorkflowRun")
    WorkflowRun.objects.filter(state="queued", current_revision__isnull=False).update(state="needs_review")


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0002_workflowrun_state'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
