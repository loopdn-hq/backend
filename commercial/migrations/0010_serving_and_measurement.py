"""Slice 5's schema: a view record, daily totals, and billed views.

AdImpression is reshaped from "one delivery of a sponsored message" (the
earlier design: a message per group, SMS included) to "one card one member
saw" - section 11 of docs/campaigns_final_plan.md: the campaign and creative,
the placement, the group and member (SET_NULL), the delivery token's nonce
(unique, so a view is counted once), whether it is billable, when settlement
charged it, and when it was seen.

Nothing has ever written an AdImpression - no route did - so there are no rows
to convert. `refuse_old_rows` checks that rather than assuming it.

CampaignStatsDaily is new: a campaign's day in totals, which reports read.
Campaign.billed_views is what settlement has counted, so the charge is always
billed_views x cpm / 1000 even when that is a fraction of a cent per view.
A new campaign's pacing defaults to "as fast as possible": "spread evenly"
needs an end date or a daily cap, which a new draft does not have.
"""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def refuse_old_rows(apps, schema_editor):
    AdImpression = apps.get_model('commercial', 'AdImpression')
    if AdImpression.objects.exists():
        raise RuntimeError(
            'commercial/0010 expects no AdImpression rows yet; found some. '
            'Nothing wrote them before slice 5 - find out what did first.'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0033_conversation_region_index'),
        ('commercial', '0009_seed_pricing'),
    ]

    operations = [
        migrations.RunPython(refuse_old_rows, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='adimpression',
            name='commercial_adimpression_once_per_member_per_channel',
        ),
        migrations.RemoveIndex(
            model_name='adimpression',
            name='commercial_impression_idx',
        ),
        migrations.RemoveField(model_name='adimpression', name='message'),
        migrations.RemoveField(model_name='adimpression', name='channel'),
        migrations.RemoveField(model_name='adimpression', name='cost_cents'),
        migrations.RemoveField(model_name='adimpression', name='counterpart_hash'),
        migrations.RemoveField(model_name='adimpression', name='created_at'),
        migrations.RemoveField(model_name='adimpression', name='delivered_at'),
        migrations.AddField(
            model_name='adimpression',
            name='campaign',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='impressions',
                to='commercial.campaign',
            ),
        ),
        migrations.AddField(
            model_name='adimpression',
            name='placement',
            field=models.CharField(max_length=16),
        ),
        migrations.AddField(
            model_name='adimpression',
            name='delivery_token',
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AddField(
            model_name='adimpression',
            name='billable',
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name='adimpression',
            name='settled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='adimpression',
            name='viewed_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterModelOptions(
            name='adimpression',
            options={'ordering': ['-viewed_at']},
        ),
        migrations.AddIndex(
            model_name='adimpression',
            index=models.Index(fields=['campaign', 'viewed_at'], name='commercial_impression_idx'),
        ),
        migrations.AddIndex(
            model_name='adimpression',
            index=models.Index(
                condition=models.Q(settled_at__isnull=True),
                fields=['campaign'],
                name='commercial_unsettled_idx',
            ),
        ),
        migrations.AlterField(
            model_name='campaign',
            name='pacing',
            field=models.CharField(
                choices=[('standard', 'Spread evenly'), ('accelerated', 'As fast as possible')],
                db_default='accelerated',
                default='accelerated',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='billed_views',
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
        migrations.CreateModel(
            name='CampaignStatsDaily',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField()),
                ('served', models.PositiveIntegerField(db_default=0, default=0)),
                ('viewed', models.PositiveIntegerField(db_default=0, default=0)),
                ('reach', models.PositiveIntegerField(db_default=0, default=0)),
                ('reach_to_date', models.PositiveIntegerField(db_default=0, default=0)),
                ('clicks', models.PositiveIntegerField(db_default=0, default=0)),
                ('results', models.PositiveIntegerField(db_default=0, default=0)),
                ('spend_cents', models.PositiveIntegerField(db_default=0, default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='daily_stats',
                    to='commercial.campaign',
                )),
            ],
            options={
                'verbose_name_plural': 'campaign stats daily',
                'ordering': ['campaign', 'day'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('campaign', 'day'), name='commercial_stats_one_row_per_day'
                    ),
                ],
            },
        ),
    ]
