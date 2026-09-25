"""Give the four seeded plans their calling limits.

Only the seeded rows are touched, by code. A per-contract Enterprise plan is
written by `enterprise.services._sync_account_plan`, and anything an admin has
since edited on these four is left for them to own - so this runs once, here,
and is not re-applied.
"""
from django.db import migrations

CALL_LIMITS = {
    'free': dict(
        max_call_participants=4, has_screen_share=False,
        has_call_recording=False, call_recording_retention_days=None,
    ),
    'premium': dict(
        max_call_participants=25, has_screen_share=True,
        has_call_recording=True, call_recording_retention_days=30,
    ),
    'business': dict(
        max_call_participants=100, has_screen_share=True,
        has_call_recording=True, call_recording_retention_days=90,
    ),
    'enterprise': dict(
        max_call_participants=None, has_screen_share=True,
        has_call_recording=True, call_recording_retention_days=None,
    ),
}


def seed(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    for code, fields in CALL_LIMITS.items():
        Plan.objects.filter(code=code).update(**fields)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_plan_call_limits'),
    ]

    operations = [
        # Nothing to undo: dropping the columns (0005 reversed) discards the
        # values with them.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
