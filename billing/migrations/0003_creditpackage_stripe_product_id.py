from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_seed_plans_and_credit_packages'),
    ]

    operations = [
        migrations.AddField(
            model_name='creditpackage',
            name='stripe_product_id',
            field=models.CharField(blank=True, db_default='', default='', max_length=255),
        ),
    ]
