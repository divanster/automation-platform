"""Step B: attribute the runs Request Intake created to Request Intake, on evidence, and nothing else.

WHAT COUNTS AS EVIDENCE. Before this migration the application had exactly one way to create a run, ``apps.intake.submission``,
and it creates the run and its ``RequestSource`` row in one transaction (a run has at most one source, enforced by the database). A run
with a source row is therefore a Request Intake run, and it is given module ``request_intake``, workflow key ``request_intake`` and
definition version 1: the identifiers of the workflow as it has always been (a test keeps them equal to ``apps.intake.policy``).
A run WITHOUT a source row is not guessed at: it is left unattributed and migration 0008 stops, with a count, so the owner decides.

IDEMPOTENT. Only a run whose three columns are all still NULL is touched, so running the migration again (or after a partial manual
fix) changes nothing that is already attributed. Rows are updated, never inserted or deleted: ids, states, timestamps, cases, current
revisions, revisions, approvals, rejections, tasks and audit events are untouched, and no canonical bytes or hash is recomputed.
Reversing does nothing (the columns go when 0006 is reversed). A migration must not import application code, so the three values are
written here.
"""

from django.db import migrations

REQUEST_INTAKE = {"module_id": "request_intake", "workflow_key": "request_intake", "workflow_version": 1}


def backfill(apps, schema_editor):
    WorkflowRun = apps.get_model("cases", "WorkflowRun")
    RequestSource = apps.get_model("intake", "RequestSource")
    WorkflowRun.objects.filter(
        module_id__isnull=True,
        workflow_key__isnull=True,
        workflow_version__isnull=True,
        pk__in=RequestSource.objects.values("run_id"),
    ).update(**REQUEST_INTAKE)


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0006_workflowrun_attribution_columns'),
        ('intake', '0001_initial'),  # the table that is the evidence
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
