"""Seed the industries, and bring any existing company up to slice 0.

The list is for the businesses that advertise to local groups - the café, the
gym, the landlord - in the order the setup form shows it, "Other" last. Adding
one later is a row, not a migration.

Nothing could create a company before slice 0, so the backfill should find
none. It is here so the migration cannot fail if somebody made one by hand:
any company without an industry gets "Other", and `pending` - a status that no
longer exists (plan decision 18) - becomes `active`.
"""
from django.db import migrations

INDUSTRIES = [
    ('food-drink', 'Food and drink'),
    ('retail', 'Retail and shopping'),
    ('health-fitness', 'Health and fitness'),
    ('beauty', 'Beauty and personal care'),
    ('education', 'Education and training'),
    ('real-estate', 'Real estate and rentals'),
    ('home-services', 'Home services'),
    ('professional-services', 'Professional services'),
    ('travel', 'Travel and hospitality'),
    ('events', 'Events and entertainment'),
    ('automotive', 'Automotive'),
    ('technology', 'Technology'),
    ('finance', 'Finance and insurance'),
    ('nonprofit', 'Nonprofit or cause'),
    ('other', 'Other'),
]


def seed(apps, schema_editor):
    Industry = apps.get_model('commercial', 'Industry')
    Company = apps.get_model('commercial', 'Company')
    for position, (slug, label) in enumerate(INDUSTRIES):
        # get_or_create, so a row somebody already added by hand is kept.
        Industry.objects.get_or_create(
            slug=slug, defaults={'label': label, 'sort_order': position}
        )

    other = Industry.objects.get(slug='other')
    Company.objects.filter(industry__isnull=True).update(industry=other)
    Company.objects.filter(status='pending').update(status='active')


def unseed(apps, schema_editor):
    """Reversible, and deliberately a no-op: reversing 0003 drops the table."""


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0003_company_setup'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
