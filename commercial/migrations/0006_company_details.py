"""Contact phone, address and description: optional, filled in after setup."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0005_company_industry_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='address',
            field=models.CharField(blank=True, db_default='', default='', max_length=255),
        ),
        migrations.AddField(
            model_name='company',
            name='contact_phone',
            field=models.CharField(blank=True, db_default='', default='', max_length=16, validators=[django.core.validators.RegexValidator(message='Enter the number in international format, e.g. +639171234567.', regex='^\\+[1-9]\\d{7,14}$')]),
        ),
        migrations.AddField(
            model_name='company',
            name='description',
            field=models.CharField(blank=True, db_default='', default='', max_length=500),
        ),
    ]
