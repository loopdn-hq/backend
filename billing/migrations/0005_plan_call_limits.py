from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_plan_stripe_product_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='max_call_participants',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='plan',
            name='has_screen_share',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='plan',
            name='has_call_recording',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='plan',
            name='call_recording_retention_days',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
