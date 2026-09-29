"""Slice 0's schema: what "Set up your company" asks for, and the active company.

Three migrations rather than one, so this runs on a database that already has
companies in it. `industry` arrives nullable here, 0004 seeds the list and
gives any existing company one, and 0005 makes it required. The `PH`/`PHP`
defaults are for existing rows only (`preserve_default=False`): a company made
from now on always says where it is and what it pays in.
"""
import commercial.models.company
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0002_seed_ad_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Industry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=40, unique=True)),
                ('label', models.CharField(max_length=80)),
                ('active', models.BooleanField(db_default=True, default=True)),
                ('sort_order', models.PositiveSmallIntegerField(db_default=0, default=0)),
            ],
            options={
                'verbose_name_plural': 'industries',
                'ordering': ['sort_order', 'label'],
            },
        ),
        migrations.AddField(
            model_name='company',
            name='country',
            field=models.CharField(choices=[('PH', 'Philippines'), ('US', 'United States')], default='PH', max_length=2),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='company',
            name='currency',
            field=models.CharField(choices=[('PHP', 'Philippine peso'), ('USD', 'US dollar')], default='PHP', max_length=3),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='company',
            name='logo',
            field=models.ImageField(blank=True, upload_to=commercial.models.company.company_logo_upload_to),
        ),
        migrations.AlterField(
            model_name='company',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('suspended', 'Suspended')], db_default='active', default='active', max_length=16),
        ),
        migrations.CreateModel(
            name='CommercialPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('active_company', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='commercial.company')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='commercial_preference', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(
            model_name='company',
            name='industry',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='companies', to='commercial.industry'),
        ),
    ]
