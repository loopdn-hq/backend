"""The first prices: ₱50 / $1 per 1,000 viewed ads in chat.

docs/campaigns_final_plan.md, decision 10. Chosen 2026-10-02 to start slice 3;
$1 is a placeholder until the US market is priced. A price change is a new
PricingRate row with a later `effective_from`, never an edit of these, so what
was charged when stays readable.
"""
from datetime import datetime, timezone

from django.db import migrations

STARTS = datetime(2026, 10, 1, tzinfo=timezone.utc)

RATES = [
    ('chat', 'PHP', 5_000, 'Launch price: ₱50 per 1,000 views.'),
    ('chat', 'USD', 100, 'Placeholder: $1 per 1,000 views until the US is priced.'),
]


def seed(apps, schema_editor):
    PricingRate = apps.get_model('commercial', 'PricingRate')
    for placement, currency, cpm_cents, note in RATES:
        PricingRate.objects.get_or_create(
            placement=placement,
            currency=currency,
            effective_from=STARTS,
            defaults={'cpm_cents': cpm_cents, 'note': note},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0008_money'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
