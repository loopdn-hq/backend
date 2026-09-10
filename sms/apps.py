from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sms'

    def ready(self):
        # Importing registers the system checks. Nothing else in this module
        # runs at import time.
        from sms import checks, signals  # noqa: F401

        # And this is what puts SMS on the message write path. `chat` names no
        # channel of its own - see chat/delivery.py:register_channel.
        from chat import delivery
        from sms.outbound import schedule

        delivery.register_channel('sms', schedule)
