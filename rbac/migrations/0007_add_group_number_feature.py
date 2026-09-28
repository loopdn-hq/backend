"""A feature for buying a group its phone number after creation failed to.

Not folded into `group.settings.policy`: this one spends money every month,
and an operator may reasonably let admins change a group's policy without
letting them buy numbers. Admins only by default, like the policy it sits
beside.
"""
from django.db import migrations


def add(apps, schema_editor):
    from rbac.resolver import invalidate_grants
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()
    # The grants are cached for a minute, and bulk_create saves nothing that
    # would clear them - so without this the new button stays hidden for up to
    # a minute after the deploy that adds it.
    invalidate_grants()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0006_add_permissions_edit_feature')]

    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
