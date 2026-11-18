"""Step C: check that every run has a valid attribution before the constraints are added. Reads only; it changes nothing.

If any run has no attribution (a run Request Intake did not create, so 0007 left it alone), or has values that the shape rules refuse,
this migration RAISES and the migration run stops here with nothing changed by this step. It reports counts only (no identifier and no
business content). The owner then decides what those runs are: see docs/development/runbook.md, "Workflow attribution". Nothing is
ever deleted or guessed. Once every run is attributed, run ``migrate`` again: 0006 and 0007 do not repeat their work and this check
passes. The patterns below repeat ``apps.cases.attribution`` (a migration must not import application code; a test keeps them equal).
"""

import re

from django.db import migrations
from django.db.models import Q

MODULE_ID = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
WORKFLOW_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})*$")
MAX_WORKFLOW_VERSION = 2_147_483_647


class UnattributedRunsError(Exception):
    """Runs exist that have no valid workflow attribution; the constraints cannot be added until they have one."""


def validate(apps, schema_editor):
    WorkflowRun = apps.get_model("cases", "WorkflowRun")
    missing = WorkflowRun.objects.filter(Q(module_id__isnull=True) | Q(workflow_key__isnull=True) | Q(workflow_version__isnull=True)).count()
    malformed = 0
    present = WorkflowRun.objects.filter(module_id__isnull=False, workflow_key__isnull=False, workflow_version__isnull=False)
    for module_id, workflow_key, workflow_version in present.values_list("module_id", "workflow_key", "workflow_version").distinct():
        if not (MODULE_ID.fullmatch(module_id) and WORKFLOW_KEY.fullmatch(workflow_key) and 1 <= workflow_version <= MAX_WORKFLOW_VERSION):
            malformed += present.filter(module_id=module_id, workflow_key=workflow_key, workflow_version=workflow_version).count()
    if missing or malformed:
        raise UnattributedRunsError(
            f"{missing} workflow run(s) have no workflow attribution and {malformed} have an attribution that is not valid. "
            "Migration cases.0007 attributes only runs that have a Request Intake source; it does not guess. Nothing was changed by this step. "
            "See docs/development/runbook.md, 'Workflow attribution', before running migrate again."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0007_backfill_run_attribution'),
    ]

    operations = [
        migrations.RunPython(validate, migrations.RunPython.noop),
    ]
