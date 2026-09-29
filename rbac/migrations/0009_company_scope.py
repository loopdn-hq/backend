"""Rename the object scope `advertiser` to `company`.

It follows the model it points at: ads.Advertiser became commercial.Company
(docs/commercial_rename_plan.md). The registry defines no feature on this scope
yet, so the rename is the choice list alone; the update below only moves a row
somebody made by hand, and runs both ways.
"""
from django.db import migrations, models


def _move(apps, old, new):
    Feature = apps.get_model('rbac', 'Feature')
    Feature.objects.filter(axis=old).update(axis=new)


def forwards(apps, schema_editor):
    _move(apps, 'advertiser', 'company')


def backwards(apps, schema_editor):
    _move(apps, 'company', 'advertiser')


class Migration(migrations.Migration):

    dependencies = [
        ('rbac', '0008_rename_advertiser_role_to_commercial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='feature',
            name='axis',
            field=models.CharField(choices=[('account', 'Account'), ('conversation', 'Conversation'), ('company', 'Company')], max_length=16),
        ),
        migrations.RunPython(forwards, backwards),
    ]
