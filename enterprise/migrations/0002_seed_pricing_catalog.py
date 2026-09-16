"""Seed catalog version 1 from the Plan Builder PRD's placeholder numbers -
the same figures the frontend currently hardcodes in
`src/lib/enterprise/catalog.ts` as a stand-in until this endpoint existed.

These are explicitly placeholders pending confirmation from product/finance,
not final pricing - see the frontend file's own comments.
"""
from django.db import migrations


def seed(apps, schema_editor):
    PricingCatalogVersion = apps.get_model('enterprise', 'PricingCatalogVersion')
    PricingOption = apps.get_model('enterprise', 'PricingOption')
    PlanFeature = apps.get_model('enterprise', 'PlanFeature')
    BillingIntervalOption = apps.get_model('enterprise', 'BillingIntervalOption')
    EnterpriseBaseFee = apps.get_model('enterprise', 'EnterpriseBaseFee')

    catalog, _ = PricingCatalogVersion.objects.update_or_create(
        version=1, defaults={'is_active': True},
    )

    EnterpriseBaseFee.objects.update_or_create(
        catalog_version=catalog, defaults={'amount_cents': 6_000},
    )

    options = [
        # category, value, label, price_cents, sort_order
        ('max_groups', '50', '50 groups', 0, 0),
        ('max_groups', '100', '100 groups', 3_000, 1),
        ('max_groups', '300', '300 groups', 9_000, 2),
        ('max_groups', '500', '500 groups', 15_000, 3),

        ('max_members_per_group', '100', '100 members / group', 0, 0),
        ('max_members_per_group', '250', '250 members / group', 2_400, 1),
        ('max_members_per_group', '500', '500 members / group', 6_000, 2),
        ('max_members_per_group', '1000', '1,000 members / group', 10_500, 3),

        ('max_admins_per_group', '20', '20 admins / group', 0, 0),
        ('max_admins_per_group', '50', '50 admins / group', 1_800, 1),
        ('max_admins_per_group', '100', '100 admins / group', 3_900, 2),

        ('storage_gb', '50', '50 GB storage', 0, 0),
        ('storage_gb', '100', '100 GB storage', 1_500, 1),
        ('storage_gb', '500', '500 GB storage', 4_500, 2),
        ('storage_gb', '1000', '1,000 GB storage', 7_500, 3),

        ('analytics', 'none', 'None', 0, 0),
        ('analytics', 'basic', 'Basic', 1_500, 1),
        ('analytics', 'advanced', 'Advanced', 4_500, 2),

        ('credit_package', '10000', '10,000 messages / mo', 4_900, 0),
        ('credit_package', '30000', '30,000 messages / mo', 12_900, 1),
        ('credit_package', '50000', '50,000 messages / mo', 19_900, 2),
        ('credit_package', '100000', '100,000 messages / mo', 34_900, 3),
        ('credit_package', '500000', '500,000 messages / mo', 149_900, 4),
    ]
    for category, value, label, price_cents, sort_order in options:
        PricingOption.objects.update_or_create(
            catalog_version=catalog, category=category, value=value,
            defaults={'label': label, 'price_cents': price_cents, 'sort_order': sort_order, 'is_active': True},
        )

    features = [
        (
            'extended_message_history', 'Extended message history',
            'Keep full message history beyond the standard retention window.', 900,
        ),
        (
            'search', 'Search',
            'Full-text search across messages, files, and members.', 900,
        ),
        (
            'advanced_notifications', 'Advanced notifications',
            'Custom notification rules, digests, and escalation routing.', 1_200,
        ),
        (
            'moderation_tools', 'Moderation tools',
            'Content filters, admin review queues, and audit trails.', 1_800,
        ),
    ]
    for key, label, description, price_cents in features:
        PlanFeature.objects.update_or_create(
            catalog_version=catalog, key=key,
            defaults={'label': label, 'description': description, 'price_cents': price_cents, 'is_active': True},
        )

    intervals = [
        ('monthly', 'Monthly', 1, 0),
        ('quarterly', 'Quarterly', 3, 5),
        ('annual', 'Annual', 12, 15),
    ]
    for code, label, months, discount_percent in intervals:
        BillingIntervalOption.objects.update_or_create(
            catalog_version=catalog, code=code,
            defaults={'label': label, 'months': months, 'discount_percent': discount_percent},
        )


def unseed(apps, schema_editor):
    PricingCatalogVersion = apps.get_model('enterprise', 'PricingCatalogVersion')
    PricingCatalogVersion.objects.filter(version=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('enterprise', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
