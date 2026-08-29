from django.apps import AppConfig


class UserConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'user'

    def ready(self):
        # Imported for the post_save receiver that creates Profile rows.
        from user import signals  # noqa: F401
