"""Startup validation for the feature catalogue.

A typo in a feature key SILENTLY DENIES - `can()` returns False for anything it
does not recognise, which is the right failure at runtime and a terrible one to
debug. So the mismatches are found at boot instead, where they are loud.

Registered as Django system checks, so they run under `manage.py check`,
`--deploy`, and the test suite, and appear in the deploy pipeline's existing
gate rather than needing a new one.
"""
from django.core.checks import Error, Warning, register


def _database_is_mid_upgrade():
    """True when migrations are pending, so the registry may legitimately lead.

    `migrate` runs the system checks BEFORE applying anything, so without this
    an unseeded feature is an Error that blocks the very migration that would
    seed it. The first version of this module deadlocked exactly that way.

    Anything raising here means the database cannot be inspected at all - no
    connection, no migration table - which is also not the moment to refuse.
    """
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor

    try:
        executor = MigrationExecutor(connections['default'])
        targets = executor.loader.graph.leaf_nodes()
        return bool(executor.migration_plan(targets))
    except Exception:
        return True


@register()
def registry_matches_database(app_configs, **kwargs):
    """Every registry feature is seeded, and every seeded row is still real."""
    from django.db import OperationalError, ProgrammingError

    from rbac.models import Feature, RoleFeature
    from rbac.registry import all_features

    if _database_is_mid_upgrade():
        return []

    try:
        seeded = set(Feature.objects.values_list('key', flat=True))
    except (OperationalError, ProgrammingError):
        # The table does not exist yet: this is a fresh database being
        # migrated, and refusing to run `migrate` because `migrate` has not run
        # would be a deadlock.
        return []

    errors = []
    expected = {entry['key'] for entry in all_features()}

    missing = expected - seeded
    if missing:
        errors.append(Error(
            f'Features in rbac/registry.py with no row: {sorted(missing)}.',
            hint='Add a data migration that seeds them. The registry is the '
                 'seed source; the database is authoritative after seeding.',
            id='rbac.E001',
        ))

    # Not an Error: a feature retired from the registry but still in the table
    # is somebody mid-removal, and its grants are still answering correctly.
    extra = seeded - expected
    if extra:
        errors.append(Warning(
            f'Features in the database but not in rbac/registry.py: '
            f'{sorted(extra)}.',
            hint='Either restore them to the registry or add a migration that '
                 'deletes them. Left alone they still grant what they say.',
            id='rbac.W001',
        ))

    orphans = RoleFeature.objects.filter(feature__isnull=True)
    if orphans.exists():
        errors.append(Error(
            f'{orphans.count()} RoleFeature rows point at no feature.',
            id='rbac.E002',
        ))

    return errors


@register()
def every_feature_has_a_grant_per_role(app_configs, **kwargs):
    """A feature with no row for a role denies it - make that deliberate.

    Silence is the failure mode this whole module exists for: a missing cell
    and an explicit `allowed=False` behave identically at runtime and mean
    entirely different things to whoever reads the matrix.
    """
    from django.db import OperationalError, ProgrammingError

    from rbac.models import Feature
    from rbac.registry import all_features

    if _database_is_mid_upgrade():
        return []

    try:
        by_key = {f.key: f for f in Feature.objects.prefetch_related('role_features')}
    except (OperationalError, ProgrammingError):
        return []

    errors = []
    for entry in all_features():
        feature = by_key.get(entry['key'])
        if feature is None:
            continue  # already reported by rbac.E001
        have = {cell.role for cell in feature.role_features.all()}
        want = set(entry['grants'])
        if want - have:
            errors.append(Warning(
                f'{entry["key"]} has no grant for {sorted(want - have)}.',
                hint='A missing cell denies, exactly like allowed=False, but '
                     'reads as an oversight on the permissions screen.',
                id='rbac.W002',
            ))
    return errors


@register()
def lockable_features_are_admin_only(app_configs, **kwargs):
    """The escalation guard, checked against the data rather than the intent.

    `is_lockable` means "never grantable below account admin". Step 8 makes the
    matrix editable; without this check, that screen is the privilege-escalation
    path it was designed to avoid.
    """
    from django.db import OperationalError, ProgrammingError

    from rbac.models import RoleFeature, UserRole

    try:
        leaked = list(
            RoleFeature.objects.filter(feature__is_lockable=True, allowed=True)
            .exclude(role=UserRole.Role.ADMIN)
            .values_list('feature__key', 'role')
        )
    except (OperationalError, ProgrammingError):
        return []

    if leaked:
        return [Error(
            f'Lockable features granted below admin: {leaked}.',
            hint='Either clear the grant or drop is_lockable - but understand '
                 'that is_lockable is what stops the matrix screen granting '
                 'role management to anybody who can open it.',
            id='rbac.E003',
        )]
    return []
