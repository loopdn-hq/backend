"""`platform.campaign.review`: approving or rejecting submitted campaigns.

Slice 2 of docs/campaigns_final_plan.md, on the admin site. Account admins
only, and lockable (decision 5): approving is what lets an ad reach members,
so it is never grantable below admin.
"""
from django.db import migrations


def add(apps, schema_editor):
    from rbac.resolver import invalidate_grants
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()
    invalidate_grants()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0011_add_platform_access')]

    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
