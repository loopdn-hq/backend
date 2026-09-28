"""Features for app areas that already exist but had no row.

Broadcast, Activity, Find groups and Analytics are real, shipped pages listed
in the nav rail, and step 6 needs a feature key to gate each menu item on.

Deliberately NOT added: `/calls/` and `/starred/` render ComingSoon, the four
entries in HIDDEN_DESTINATIONS are not built, and the enterprise Plan Builder
is `permission_classes = []` - an anonymous visitor configuring a quote has no
role to check. A row for any of those would be a promise with no check behind
it, which is what the catalogue-in-code rule exists to prevent.

Additive only: `ensure_catalogue` creates what is missing and modifies nothing,
so an operator's corrections to the existing 48 cells survive this.
"""
from django.db import migrations


def add(apps, schema_editor):
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0003_backfill_user_roles')]

    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
