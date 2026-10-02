"""Rename the feature `ads.access` to `commercial.access`.

It is the door to the commercial account's dashboard - companies now,
campaigns and billing later - and advertising is one thing done inside it. So
the gate is named for the area, and ad-specific features keep `ads.*`
(`ads.campaign.publish`, when campaigns arrive).

THE OLD ROW WINS
----------------
Every catalogue migration from 0002 on calls `ensure_catalogue()`, which reads
the live registry - so a database built from scratch on this code never had
`ads.access` and already has `commercial.access`, and there is nothing to do.
On one that had `ads.access`, those same migrations and any post_migrate may
have seeded a fresh `commercial.access` beside it before this runs. The old row
wins, as in 0008: an operator may have changed its grants on the permissions
screen, and the fresh one holds only registry defaults. It keeps its id, so
its grants and its audit entries stay attached.

RoleAuditLog.feature_key is not touched. It is append-only history, and a grant
changed while the feature was called `ads.access` was changed under that name.
"""
from django.db import migrations

OLD = {
    'key': 'ads.access',
    'label': 'Open the advertising area',
    'area': 'Advertising',
    'description': (
        'Reach the Advertise section. The campaign features themselves arrive '
        'with the ads app.'
    ),
}
NEW = {
    'key': 'commercial.access',
    'label': 'Use the commercial dashboard',
    'area': 'Commercial',
    'description': (
        'Reach the commercial dashboard and its API: companies now, campaigns '
        'and billing as they arrive.'
    ),
}


def _rename(apps, old, new):
    from rbac.resolver import invalidate_grants

    Feature = apps.get_model('rbac', 'Feature')

    feature = Feature.objects.filter(key=old['key']).first()
    if feature is None:
        return

    # A freshly seeded twin: defaults only. Its grants go with it (CASCADE).
    Feature.objects.filter(key=new['key']).delete()

    feature.key = new['key']
    feature.label = new['label']
    feature.area = new['area']
    feature.description = new['description']
    feature.save(update_fields=['key', 'label', 'area', 'description'])

    # Grants are cached by key for a minute; the old key's entry is now wrong.
    invalidate_grants()


def forwards(apps, schema_editor):
    _rename(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _rename(apps, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ('rbac', '0009_company_scope'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
