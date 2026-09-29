"""Seed the group categories from §2 of the product plan.

§2 ("Who it is for") is the list, in its order: families and friends; schools,
religious and community groups and neighborhoods; organizations, nonprofits and
associations; businesses and internal teams. Adding a category later is a row,
not another migration - that is why these are data and not choices.
"""
from django.db import migrations

CATEGORIES = [
    ('family-friends', 'Family and friends'),
    ('school', 'School'),
    ('religious', 'Religious group'),
    ('neighborhood', 'Neighborhood'),
    ('community', 'Community'),
    ('nonprofit', 'Nonprofit or association'),
    ('business', 'Business or team'),
]


def seed(apps, schema_editor):
    GroupCategory = apps.get_model('chat', 'GroupCategory')
    for position, (slug, label) in enumerate(CATEGORIES):
        # get_or_create, so re-running over a database where somebody already
        # added one by hand keeps their row rather than failing on the slug.
        GroupCategory.objects.get_or_create(
            slug=slug, defaults={'label': label, 'sort_order': position}
        )


def unseed(apps, schema_editor):
    """Reversible, and deliberately a no-op: reversing 0030 drops the table."""


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0030_group_category'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
