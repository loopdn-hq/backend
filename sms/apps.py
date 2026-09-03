from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sms'

    def ready(self):
        # Importing registers the system checks. Nothing else in this module
        # runs at import time.
        from sms import checks, signals  # noqa: F401
