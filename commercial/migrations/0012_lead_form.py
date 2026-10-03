"""Slice 7a: Loopd'n's own lead form.

A creative gains the `lead_form` button, which opens the form instead of an
address; a lead campaign's ads carry it from now on. Existing creatives of
DRAFT lead campaigns move to it here, so a draft can still be submitted. A
lead campaign already submitted keeps the link button it was reviewed with.

Lead is new: what one member sent one company, as a snapshot.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def drafts_to_the_form(apps, schema_editor):
    Creative = apps.get_model('commercial', 'Creative')
    Creative.objects.filter(
        campaign__objective='lead', campaign__status='draft', cta_kind='link'
    ).update(cta_kind='lead_form', cta_target='')


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0011_click_counted'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='creative',
            name='commercial_creative_cta_target_matches_kind',
        ),
        migrations.AlterField(
            model_name='creative',
            name='cta_kind',
            field=models.CharField(
                choices=[
                    ('call', 'Call'),
                    ('text', 'Text'),
                    ('link', 'Open a link'),
                    ('group_join', 'Join a group'),
                    ('lead_form', 'Open a form'),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(drafts_to_the_form, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='creative',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(cta_kind__in=['call', 'text', 'lead_form'], cta_target='')
                    | (models.Q(cta_kind__in=['link', 'group_join']) & ~models.Q(cta_target=''))
                ),
                name='commercial_creative_cta_target_matches_kind',
            ),
        ),
        migrations.CreateModel(
            name='Lead',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('phone', models.CharField(blank=True, db_default='', default='', max_length=32)),
                ('email', models.EmailField(blank=True, db_default='', default='', max_length=254)),
                ('message', models.CharField(blank=True, db_default='', default='', max_length=500)),
                ('consent_text', models.CharField(max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='leads',
                    to='commercial.campaign',
                )),
                ('conversion', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='lead',
                    to='commercial.adconversion',
                )),
                ('member', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at', '-pk'],
                'indexes': [
                    models.Index(fields=['campaign', 'created_at'], name='commercial_lead_campaign_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('campaign', 'member'), name='commercial_lead_one_per_member'
                    ),
                ],
            },
        ),
    ]
