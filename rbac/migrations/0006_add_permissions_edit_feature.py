"""A feature for CHANGING the matrix, separate from viewing it.

Step 7 shipped the read-only screen gated on `platform.permissions.view`.
Editing is a different question - a support role should be able to read who may
do what without being able to rewrite it - so it gets its own key rather than
widening the existing one.

Lockable, like the other two platform features: an editable matrix whose own
edit permission could be handed out is the escalation path the flag exists to
prevent.
"""
from django.db import migrations


def add(apps, schema_editor):
    from rbac.seeding import ensure_catalogue

    ensure_catalogue()


class Migration(migrations.Migration):

    dependencies = [('rbac', '0005_roleauditlog')]

    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
