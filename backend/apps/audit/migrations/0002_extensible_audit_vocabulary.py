"""MOD-1b S3: let modules declare audit actions without a migration per action.

FORWARD. The fixed list of action names (``audit_event_action_valid``) is replaced by ``audit_event_action_shape``: a bounded, anchored pattern that
every Core action and every action a module manifest may declare matches (two or more lower-case dotted segments; the column's 32 characters bound
the length). The subject-type list gains ``module``. No row is read, changed or deleted by this migration; PostgreSQL checks the existing rows
against the two new constraints while it adds them (a table scan under a brief exclusive lock; the audit table is append-only and small today).

WHAT THE SHAPE CHECK IS NOT. It validates syntax, not membership: the database no longer knows which actions exist. The allowlist is the Python
vocabulary (``apps.audit.vocabulary``, enforced when an event is built). Raw SQL, a privileged migration or a database administrator can store any
well-formed action, and nothing detects it. Nothing here is database-level tamper protection.

REVERSE. Reversing restores the old fixed lists, which are only possible while every stored event satisfies them. If an event with a module action
or the ``module`` subject type exists, the reverse is REFUSED with ``IrreversibleError`` before any constraint is touched, naming counts only.
Events are NEVER deleted or rewritten to make a reverse succeed; the way back past such events is a restore from backup or a reviewed, owner-run
data decision outside this migration. The reverse guard runs first on a reverse (it is the last operation) and only reads.

The constraint literals below are frozen on purpose: a migration must not import application code. A test keeps them equal to the model's.
"""

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError

# The fixed vocabulary of migration 0001, needed only to refuse an unsafe reverse.
OLD_ACTIONS = [
    "request.submitted",
    "revision.created",
    "request.edited",
    "run.state_changed",
    "approval.recorded",
    "approval.invalidated",
    "rejection.recorded",
]
OLD_SUBJECT_TYPES = ["run", "revision", "approval", "rejection"]


def keep_events(apps, schema_editor):
    """Forward: nothing to do. (The operation exists only so that its reverse, the guard below, runs first on a reverse.)"""


def refuse_reverse_if_new_vocabulary_is_stored(apps, schema_editor):
    AuditEvent = apps.get_model("audit", "AuditEvent")
    events = AuditEvent.objects.using(schema_editor.connection.alias)
    other_actions = events.exclude(action__in=OLD_ACTIONS).count()
    other_subjects = events.exclude(subject_type__in=OLD_SUBJECT_TYPES).count()
    if other_actions or other_subjects:
        raise IrreversibleError(
            f"Cannot restore the fixed audit vocabulary: {other_actions} stored event(s) have an action and {other_subjects} have a subject type "
            "outside the old lists. Audit events are never deleted or rewritten to make a reverse migration succeed."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='auditevent',
            name='audit_event_action_valid',
        ),
        migrations.RemoveConstraint(
            model_name='auditevent',
            name='audit_event_subject_type_valid',
        ),
        migrations.AddConstraint(
            model_name='auditevent',
            constraint=models.CheckConstraint(condition=models.Q(('action__regex', '^[a-z][a-z0-9_]{0,39}(\\.[a-z][a-z0-9_]{0,39}){1,15}$')), name='audit_event_action_shape'),
        ),
        migrations.AddConstraint(
            model_name='auditevent',
            constraint=models.CheckConstraint(condition=models.Q(('subject_type__in', ['run', 'revision', 'approval', 'rejection', 'module'])), name='audit_event_subject_type_valid'),
        ),
        migrations.RunPython(keep_events, refuse_reverse_if_new_vocabulary_is_stored),
    ]
