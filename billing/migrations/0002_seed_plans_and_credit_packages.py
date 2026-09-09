"""Seed the four platform plans and the suggested Communication Credit
packages from the pricing document. `stripe_price_id` is left blank -
`manage.py sync_stripe_billing` fills it in once Stripe credentials exist.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    CreditPackage = apps.get_model('billing', 'CreditPackage')

    plans = [
        dict(
            code='free', name='Free', monthly_price_cents=0,
            max_groups=3, max_members_per_group=20, max_admins_per_group=1,
            storage_gb=1, has_whatsapp=False, has_telegram=False,
            is_custom_pricing=False, sort_order=0,
            features=[
                'Web messaging', 'Basic message history', 'Reactions',
                'Reply, edit and delete', 'Join links/codes',
                'Basic notifications',
                'SMS requires PAYG credits or sponsored campaign funding',
            ],
        ),
        dict(
            code='premium', name='Premium', monthly_price_cents=999,
            max_groups=25, max_members_per_group=250, max_admins_per_group=5,
            storage_gb=10, has_whatsapp=True, has_telegram=True,
            is_custom_pricing=False, sort_order=1,
            features=[
                'Extended message history', 'Search', 'Advanced notifications',
                'Moderation tools', 'Basic analytics', 'Ad-free',
                'SMS & Voice via Pay-As-You-Go Communication Credits',
            ],
        ),
        dict(
            code='business', name='Business', monthly_price_cents=2999,
            max_groups=100, max_members_per_group=1000, max_admins_per_group=10,
            storage_gb=None, has_whatsapp=True, has_telegram=True,
            is_custom_pricing=False, sort_order=2,
            features=[
                'Ad-free', 'Custom group branding', 'Advanced moderation',
                'Group management dashboard', 'Usage controls',
                'WhatsApp Business integration',
                'Communication wallet spending limits', 'Auto-recharge',
                'Priority support',
            ],
        ),
        dict(
            code='enterprise', name='Enterprise', monthly_price_cents=None,
            max_groups=None, max_members_per_group=None, max_admins_per_group=None,
            storage_gb=None, has_whatsapp=True, has_telegram=True,
            is_custom_pricing=True, sort_order=3,
            features=[
                'Workspaces', 'SSO', 'API access', 'Audit logs',
                'Custom retention policies', 'Dedicated account manager',
                'SLA', 'Custom volume pricing',
                'Postpaid communication billing option',
            ],
        ),
    ]
    for fields in plans:
        Plan.objects.update_or_create(code=fields['code'], defaults=fields)

    packages = [
        dict(name='Starter Package', price_cents=500, credit_cents=500, sort_order=0),
        dict(name='Standard Package', price_cents=1000, credit_cents=1000, sort_order=1),
        dict(name='Plus Package', price_cents=2500, credit_cents=2500, sort_order=2),
        dict(name='Business Package', price_cents=5000, credit_cents=5000, sort_order=3),
        dict(name='High Volume Package', price_cents=10000, credit_cents=10000, sort_order=4),
    ]
    for fields in packages:
        CreditPackage.objects.update_or_create(name=fields['name'], defaults=fields)


def unseed(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    CreditPackage = apps.get_model('billing', 'CreditPackage')
    Plan.objects.filter(
        code__in=['free', 'premium', 'business', 'enterprise']
    ).delete()
    CreditPackage.objects.filter(
        name__in=[
            'Starter Package', 'Standard Package', 'Plus Package',
            'Business Package', 'High Volume Package',
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
