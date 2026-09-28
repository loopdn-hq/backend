"""Give every existing account its roles.

`standard` for everyone, because that is what every existing user effectively
already is, and an account with no rows at all must stay a detectable bug
rather than an interpreted state.

`admin` additionally for `is_superuser`, because those are the founding team
and would otherwise open the app as ordinary users. A ONE-TIME backfill, and
deliberately not an ongoing sync: a signal keeping `is_superuser` and
`UserRole` in step would rebuild exactly the coupling this app exists to
remove. From here `is_staff` and `is_superuser` mean Django admin access and
nothing else.

`granted_by` is left null throughout - the system did this, and null here says
so. It is a different fact from "nobody knows".
"""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    UserRole = apps.get_model('rbac', 'UserRole')

    existing = set(UserRole.objects.values_list('user_id', 'role'))

    rows = []
    for user_id, is_superuser in User.objects.values_list('id', 'is_superuser'):
        if (user_id, 'standard') not in existing:
            rows.append(UserRole(user_id=user_id, role='standard'))
        if is_superuser and (user_id, 'admin') not in existing:
            rows.append(UserRole(user_id=user_id, role='admin'))

    UserRole.objects.bulk_create(rows, batch_size=500)


def unbackfill(apps, schema_editor):
    UserRole = apps.get_model('rbac', 'UserRole')
    # Only the rows this migration could have written. A role granted by hand
    # since - which carries a granted_by - is somebody's decision and is not
    # this migration's to undo.
    UserRole.objects.filter(granted_by__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('rbac', '0002_seed_features_and_role_features'),
        ('auth', '0001_initial'),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
