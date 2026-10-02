from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    # Not `admin`: that is django.contrib.admin's label. Not `platform`: a
    # top-level package by that name would shadow the standard library module
    # (the reason `mail` is not `email`).
    name = 'platform_admin'
