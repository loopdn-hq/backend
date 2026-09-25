"""Add calling to the Plan Builder catalog: two priced, toggleable features on
the existing catalog version - calls (audio and video together) and
recording.

One `calls` feature, not two: `billing.Plan.has_calls` is a single flag that
grants both audio and video, so a custom plan has no reason to buy them
separately either.

Prices are PLACEHOLDERS, same as 0002_seed_pricing_catalog's own and pending
the same confirmation from product/finance - they exist so the feature is
priced at all, not because $25/$20 are numbers anyone has signed off on.

Added to catalog version 1 in place, not a new version 2 - see the same
reasoning in this migration's own history (0002 is still the only version
seeded, and nothing has bought either of these keys yet).
"""
from django.db import migrations


def seed(apps, schema_editor):
    PricingCatalogVersion = apps.get_model('enterprise', 'PricingCatalogVersion')
    PlanFeature = apps.get_model('enterprise', 'PlanFeature')

    catalog = PricingCatalogVersion.objects.filter(version=1).first()
    if catalog is None:
        # A fresh database seeds 0002 first (an earlier dependency), so this
        # is only reachable if that seed was rolled back independently.
        return

    features = [
        (
            'calls', 'Calls',
            'Voice and video calls, one-to-one and in groups, on LiveKit.', 2_500,
        ),
        (
            'call_recording', 'Call recording',
            'Record calls to a private, downloadable file. Kept 90 days.', 2_000,
        ),
    ]
    for key, label, description, price_cents in features:
        PlanFeature.objects.update_or_create(
            catalog_version=catalog, key=key,
            defaults={'label': label, 'description': description, 'price_cents': price_cents, 'is_active': True},
        )


def unseed(apps, schema_editor):
    PlanFeature = apps.get_model('enterprise', 'PlanFeature')
    PlanFeature.objects.filter(
        catalog_version__version=1, key__in=['calls', 'call_recording'],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('enterprise', '0003_alter_enterprisestripebilling_stripe_checkout_session_id_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
