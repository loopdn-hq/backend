from django.apps import AppConfig


class RbacConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'rbac'
    verbose_name = 'Roles and features'

    def ready(self):
        """Register the catalogue checks, and keep the catalogue present.

        Imported inside ready() rather than at module level so the app registry
        is populated first - the same shape billing/apps.py and mail/apps.py
        use.

        The post_migrate hook is not belt-and-braces over the data migration.
        The grants are required configuration - an empty table denies
        everything - and Django truncates tables between every
        TransactionTestCase and on every `manage.py flush`, both of which emit
        post_migrate afterwards. `auth.Permission` survives those the same way,
        for the same reason. See rbac/seeding.py.
        """
        from django.db.models.signals import post_migrate

        from rbac import checks  # noqa: F401
        from rbac.seeding import ensure_catalogue

        post_migrate.connect(
            ensure_catalogue, sender=self, dispatch_uid='rbac.ensure_catalogue'
        )

        # The grants lookup is cached - it runs once per conversation on a
        # list page. Editing the matrix has to clear it, or an admin's change
        # would appear to do nothing. A user's own ROLES are never cached, so
        # revoking somebody's access is immediate regardless of this.
        from django.db.models.signals import post_delete, post_save

        from rbac.models import Feature, RoleFeature
        from rbac.resolver import invalidate_grants

        def _forget(sender, **kwargs):
            invalidate_grants()

        for signal in (post_save, post_delete):
            for model in (RoleFeature, Feature):
                signal.connect(
                    _forget, sender=model,
                    dispatch_uid=f'rbac.invalidate.{signal}.{model.__name__}',
                )
