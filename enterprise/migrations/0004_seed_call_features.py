"""Add calling to the Plan Builder catalog: three priced, toggleable features
on the existing catalog version - voice calls, video calls, and recording.

Prices are PLACEHOLDERS, same as 0002_seed_pricing_catalog's own and pending
the same confirmation from product/finance - they exist so the feature is
priced at all, not because $15/$25/$20 are numbers anyone has signed off on.

Added to catalog version 1 in place, not a new version 2, matching how this
catalog has been extended so far (0002 is still the only version seeded). A
version bump protects a contract that has already priced against a version
from a LATER price change to something it already bought - it is not needed
to ADD a feature nothing has bought yet. Safe here specifically: nothing here
changes an existing option's price, and `_sync_entitlements` /
`_sync_account_plan` read `configuration['features']` with `.get`, so a
contract frozen before this migration (there are already several in this
database) treats a key it has never heard of as "not bought" rather than
erroring the next time it is touched.
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
            'voice_calls', 'Voice calls',
            'One-to-one and group voice calls, on LiveKit.', 1_500,
        ),
        (
            'video_calls', 'Video calls',
            'Video, on top of voice calls. Requires voice calls.', 2_500,
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
        catalog_version__version=1,
        key__in=['voice_calls', 'video_calls', 'call_recording'],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('enterprise', '0003_alter_enterprisestripebilling_stripe_checkout_session_id_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
