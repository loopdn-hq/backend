"""`platform.access`: the door to Loopd'n's own admin site.

admin.loopdn.com serves admin tools, the way commercial.loopdn.com serves
companies; this is its `commercial.access`. Every admin endpoint checks it
alongside its own feature. Account admins only, by default.
"""
from django.db import migrations


def add(apps, schema_editor):
    from rbac.resolver import invalidate_grants
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()
    # Grants are cached for a minute; without this the admin site stays shut
    # for up to a minute after the deploy that opens it.
    invalidate_grants()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0010_rename_ads_access_to_commercial_access')]

    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
