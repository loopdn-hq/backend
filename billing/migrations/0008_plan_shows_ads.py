"""Plan.shows_ads: whether a plan's groups may carry ads.

Only Free does. The paid plans are ad-free, and the default is False so any
plan written after this - a per-contract Enterprise plan - is ad-free unless
somebody decides otherwise. As in 0006, only the seeded Free row is touched.
"""
from django.db import migrations, models


def free_shows_ads(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    Plan.objects.filter(code='free').update(shows_ads=True)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_plan_call_toggle'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='shows_ads',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(free_shows_ads, migrations.RunPython.noop),
    ]
