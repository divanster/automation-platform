"""AI settings: let the subject-type list include ``ai_settings``, so a change of the AI extraction settings can be audited.

FORWARD. Only the subject-type CHECK (``audit_event_subject_type_valid``) changes: ``ai_settings`` is added to the list. The action CHECK is untouched (the
new action ``ai_settings.updated`` already matches ``audit_event_action_shape``; which actions exist is the Python vocabulary's rule, see migration 0002).
No row is read, changed or deleted; PostgreSQL checks the existing rows against the new constraint while it adds it (a scan under a brief exclusive lock;
the audit table is append-only and small today).

REVERSE. Restores the previous list, which is only possible while no stored event has the ``ai_settings`` subject type. If one exists the reverse is REFUSED
with ``IrreversibleError`` before any constraint is touched, naming a count only. Events are NEVER deleted or rewritten to make a reverse succeed. The
guard runs first on a reverse (it is the last operation) and only reads.

The constraint literals are frozen on purpose: a migration must not import application code. A test keeps them equal to the model's.
"""

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError

PREVIOUS_SUBJECT_TYPES = ["run", "revision", "approval", "rejection", "module"]


def keep_events(apps, schema_editor):
    """Forward: nothing to do. (The operation exists only so that its reverse, the guard below, runs first on a reverse.)"""


def refuse_reverse_if_ai_settings_events_exist(apps, schema_editor):
    AuditEvent = apps.get_model("audit", "AuditEvent")
    stored = AuditEvent.objects.using(schema_editor.connection.alias).exclude(subject_type__in=PREVIOUS_SUBJECT_TYPES).count()
    if stored:
        raise IrreversibleError(
            f"Cannot restore the previous audit subject types: {stored} stored event(s) have a subject type outside the previous list. "
            "Audit events are never deleted or rewritten to make a reverse migration succeed."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0002_extensible_audit_vocabulary'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='auditevent',
            name='audit_event_subject_type_valid',
        ),
        migrations.AddConstraint(
            model_name='auditevent',
            constraint=models.CheckConstraint(condition=models.Q(('subject_type__in', ['run', 'revision', 'approval', 'rejection', 'module', 'ai_settings'])), name='audit_event_subject_type_valid'),
        ),
        migrations.RunPython(keep_events, refuse_reverse_if_ai_settings_events_exist),
    ]
