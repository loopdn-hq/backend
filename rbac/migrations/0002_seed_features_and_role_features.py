"""Seed the feature catalogue and the role grants from `rbac/registry.py`.

Creates what is missing and modifies nothing, so re-running is harmless and an
operator who corrects a grant in the admin does not have it silently reverted.
The database is authoritative once seeded; a changed label, default or grant is
a new migration, never an edit to the registry and a redeploy.

`rbac/seeding.py` explains why the same work is also connected to post_migrate
rather than living here alone.
"""
from django.db import migrations


def seed(apps, schema_editor):
    # The same function the post_migrate hook uses, so there is one description
    # of what seeding means. It uses the real models rather than the historical
    # ones, which is safe here: this migration is the one that introduces the
    # data, and the fields it writes have not changed since 0001.
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()


def unseed(apps, schema_editor):
    from rbac.registry import all_features

    Feature = apps.get_model('rbac', 'Feature')
    # RoleFeature rows go with their feature - the FK cascades.
    Feature.objects.filter(
        key__in=[entry['key'] for entry in all_features()]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0001_initial')]

    operations = [migrations.RunPython(seed, unseed)]
