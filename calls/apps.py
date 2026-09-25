from django.apps import AppConfig


class CallsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'calls'

    def ready(self):
        # System checks. Imported here so the app registry is populated first.
        from calls import checks  # noqa: F401
