from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chat'

    def ready(self):
        # Registers the signal handlers and the system checks. Imported here
        # rather than at module level so the app registry is populated first.
        from chat import checks  # noqa: F401
        from chat import signals  # noqa: F401
