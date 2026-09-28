"""Rename the account role `advertiser` to `commercial`.

"Business" is the plan tier companies buy for chat (billing.Plan); "commercial"
is the kind of user that buys ads. The role took the second word so the two
stop sounding like one thing. The advertiser *object* scope (Feature.Axis
ADVERTISER, and the ads.Advertiser model it points at) keeps its name.

THE OLD ROWS WIN
----------------
`ensure_catalogue()` reads the live registry, which already says `commercial`,
so 0007 - and any post_migrate on this code - may have written fresh default
grants for `commercial` beside the old `advertiser` ones before this runs. The
old rows win: an operator may have changed them on the permissions screen, and
the fresh ones are only registry defaults.

RoleAuditLog is not touched. It is append-only history, and a grant made while
the role was called `advertiser` was made under that name.
"""
from django.db import migrations, models


def _rename(apps, old, new):
    from rbac.resolver import invalidate_grants

    RoleFeature = apps.get_model('rbac', 'RoleFeature')
    UserRole = apps.get_model('rbac', 'UserRole')

    features = list(
        RoleFeature.objects.filter(role=old).values_list('feature_id', flat=True)
    )
    RoleFeature.objects.filter(role=new, feature_id__in=features).delete()
    RoleFeature.objects.filter(role=old).update(role=new)

    holders = list(UserRole.objects.filter(role=new).values_list('user_id', flat=True))
    UserRole.objects.filter(role=old, user_id__in=holders).delete()
    UserRole.objects.filter(role=old).update(role=new)

    # Grants are cached for a minute, and update() clears nothing.
    invalidate_grants()


def forwards(apps, schema_editor):
    _rename(apps, 'advertiser', 'commercial')


def backwards(apps, schema_editor):
    _rename(apps, 'commercial', 'advertiser')


class Migration(migrations.Migration):

    dependencies = [
        ('rbac', '0007_add_group_number_feature'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userrole',
            name='role',
            field=models.CharField(choices=[('standard', 'Standard'), ('commercial', 'Commercial'), ('admin', 'Account admin')], max_length=16),
        ),
        migrations.RunPython(forwards, backwards),
    ]
