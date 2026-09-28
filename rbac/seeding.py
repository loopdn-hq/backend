"""Making sure the catalogue exists, whatever has happened to the database.

WHY THIS IS NOT JUST A DATA MIGRATION
-------------------------------------
The grants are REQUIRED CONFIGURATION: with an empty `RoleFeature` table,
`can()` denies everything and the product stops working. A data migration puts
them there once, and that is not enough, because Django truncates tables in
two situations a migration never sees again:

    TransactionTestCase   flushes every table between tests
    `manage.py flush`     the same thing, deliberately

Both emit `post_migrate` afterwards, which is exactly why `auth.Permission` and
`contenttypes.ContentType` survive them - they are recreated by post_migrate
receivers rather than by migrations. This is the same pattern for the same
reason.

CREATE MISSING, NEVER MODIFY EXISTING
-------------------------------------
One rule, so it stays predictable. An operator who corrects a grant in the
admin must not have it silently reverted by the next deploy - the database is
authoritative once seeded. So a changed label, a changed default or a changed
grant is a MIGRATION, never an edit here and a redeploy.
"""
import logging

logger = logging.getLogger(__name__)


def ensure_catalogue(**_kwargs):
    """Create any registry feature or grant that has no row yet.

    Idempotent and cheap: two queries when everything is already present, which
    is the common case on every `migrate` and between every TransactionTestCase.

    Accepts and ignores keyword arguments so it can be connected directly to
    `post_migrate`, which sends `sender`, `app_config`, `using` and more.
    """
    from rbac.models import Feature, RoleFeature
    from rbac.registry import all_features

    entries = list(all_features())

    known = set(Feature.objects.values_list('key', flat=True))
    for entry in entries:
        if entry['key'] in known:
            continue
        Feature.objects.create(
            key=entry['key'],
            label=entry['label'],
            description=entry.get('description', ''),
            area=entry['area'],
            axis=entry['axis'],
            default_allowed=entry.get('default_allowed', True),
            is_lockable=entry.get('is_lockable', False),
            sort_order=entry.get('sort_order', 0),
        )

    features = {f.key: f for f in Feature.objects.all()}
    have = set(
        RoleFeature.objects.values_list('feature__key', 'role')
    )
    missing = [
        RoleFeature(feature=features[entry['key']], role=role, allowed=allowed)
        # `allowed` written from the registry rather than left to the field
        # default, which is False. A cell that fell back to the default would
        # be a silent denial nobody wrote down.
        for entry in entries
        for role, allowed in entry['grants'].items()
        if (entry['key'], role) not in have
    ]
    if missing:
        RoleFeature.objects.bulk_create(missing, batch_size=500)

    return len(known ^ {e['key'] for e in entries}), len(missing)
