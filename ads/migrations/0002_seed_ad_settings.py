"""Create the ad kill switch, switched off.

AdSettings already fails closed without a row, so this changes no behaviour. It
exists so the switch is there to find in the Django admin, reading "serving
OFF", rather than having to be created the first time somebody needs it.
"""
from django.db import migrations


def seed(apps, schema_editor):
    AdSettings = apps.get_model('ads', 'AdSettings')
    AdSettings.objects.get_or_create(
        singleton=True,
        defaults={'serving_enabled': False, 'note': 'Off until ads are deliberately switched on.'},
    )


def unseed(apps, schema_editor):
    """Reversible, and deliberately a no-op: reversing 0001 drops the table."""


class Migration(migrations.Migration):

    dependencies = [
        ('ads', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
