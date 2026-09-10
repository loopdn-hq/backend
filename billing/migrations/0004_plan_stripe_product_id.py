from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_creditpackage_stripe_product_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='stripe_product_id',
            field=models.CharField(blank=True, db_default='', default='', max_length=255),
        ),
    ]
