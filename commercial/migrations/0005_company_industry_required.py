"""Every company says what it does. 0004 gave any that did not "Other"."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0004_seed_industries'),
    ]

    operations = [
        migrations.AlterField(
            model_name='company',
            name='industry',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='companies', to='commercial.industry'),
        ),
    ]
