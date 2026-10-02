"""Slice 3's schema: prices, and a ledger with two buckets.

A company's money is now AVAILABLE (`balance_cents`) and RESERVED (new
`reserved_cents`); a campaign records its own reservation, what it spent, and
the price it was submitted at. The ledger records how each bucket moved, and
its kinds are reshaped to section 9 of docs/campaigns_final_plan.md
(`usage_debit` becomes `campaign_spend`, plus reserve and release).

Nothing has ever written an AdTransaction - slices 0 and 1 had no route to -
so there are no rows to convert. `refuse_old_rows` checks that rather than
assuming it: rewriting money silently would be worse than stopping.
"""

import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models


def refuse_old_rows(apps, schema_editor):
    AdTransaction = apps.get_model('commercial', 'AdTransaction')
    if AdTransaction.objects.exists():
        raise RuntimeError(
            'commercial/0008 expects no AdTransaction rows yet; found some. '
            'Convert them to the two-bucket shape by hand first.'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0007_campaign_drafts_and_events'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(refuse_old_rows, migrations.RunPython.noop),
        migrations.CreateModel(
            name='PricingRate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('placement', models.CharField(max_length=16)),
                ('currency', models.CharField(choices=[('PHP', 'Philippine peso'), ('USD', 'US dollar')], max_length=3)),
                ('cpm_cents', models.PositiveIntegerField()),
                ('effective_from', models.DateTimeField()),
                ('note', models.CharField(blank=True, db_default='', default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['placement', 'currency', '-effective_from'],
            },
        ),
        migrations.AlterModelOptions(
            name='adtransaction',
            options={'ordering': ['-created_at', '-pk']},
        ),
        migrations.RemoveConstraint(
            model_name='adtransaction',
            name='commercial_adtransaction_sign_matches_kind',
        ),
        migrations.RemoveConstraint(
            model_name='adtransaction',
            name='commercial_adtransaction_usage_debit_names_campaign',
        ),
        migrations.RemoveConstraint(
            model_name='adtransaction',
            name='commercial_adtransaction_balance_after_non_negative',
        ),
        migrations.AddField(
            model_name='adtransaction',
            name='reserved_after_cents',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='adtransaction',
            name='reserved_change_cents',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='campaign',
            name='cpm_cents',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='campaign',
            name='reserved_cents',
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='campaign',
            name='spent_cents',
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='company',
            name='reserved_cents',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name='adtransaction',
            name='kind',
            field=models.CharField(choices=[('credit_purchase', 'Funds added'), ('campaign_reserve', 'Budget set aside'), ('campaign_spend', 'Spent'), ('campaign_release', 'Budget returned'), ('refund', 'Refund'), ('adjustment', 'Adjustment')], max_length=20),
        ),
        migrations.AddConstraint(
            model_name='adtransaction',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('amount_cents__gt', 0), ('kind', 'credit_purchase'), ('reserved_change_cents', 0)), models.Q(('amount_cents__lt', 0), ('kind', 'campaign_reserve'), ('reserved_change_cents', django.db.models.expressions.CombinedExpression(models.F('amount_cents'), '*', models.Value(-1)))), models.Q(('amount_cents', 0), ('kind', 'campaign_spend'), ('reserved_change_cents__lt', 0)), models.Q(('amount_cents__gt', 0), ('kind', 'campaign_release'), ('reserved_change_cents', django.db.models.expressions.CombinedExpression(models.F('amount_cents'), '*', models.Value(-1)))), models.Q(('amount_cents__lt', 0), ('kind', 'refund'), ('reserved_change_cents', 0)), models.Q(('kind', 'adjustment'), ('reserved_change_cents', 0), models.Q(('amount_cents', 0), _negated=True)), _connector='OR'), name='commercial_adtransaction_movement_matches_kind'),
        ),
        migrations.AddConstraint(
            model_name='adtransaction',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('kind__in', ['campaign_reserve', 'campaign_spend', 'campaign_release']), _negated=True), ('campaign__isnull', False), _connector='OR'), name='commercial_adtransaction_campaign_rows_name_it'),
        ),
        migrations.AddConstraint(
            model_name='adtransaction',
            constraint=models.CheckConstraint(condition=models.Q(('balance_after_cents__gte', 0), ('reserved_after_cents__gte', 0)), name='commercial_adtransaction_buckets_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='company',
            constraint=models.CheckConstraint(condition=models.Q(('reserved_cents__gte', 0)), name='commercial_company_reserved_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='pricingrate',
            constraint=models.CheckConstraint(condition=models.Q(('cpm_cents__gt', 0)), name='commercial_pricingrate_positive'),
        ),
        migrations.AddConstraint(
            model_name='pricingrate',
            constraint=models.UniqueConstraint(fields=('placement', 'currency', 'effective_from'), name='commercial_pricingrate_one_per_moment'),
        ),
    ]
