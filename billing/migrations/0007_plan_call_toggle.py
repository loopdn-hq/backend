from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_seed_call_limits'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='has_calls',
            field=models.BooleanField(db_default=True, default=True),
        ),
    ]
