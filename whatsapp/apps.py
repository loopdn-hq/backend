from django.apps import AppConfig


class WhatsappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'whatsapp'
    verbose_name = 'WhatsApp'

    def ready(self):
        from whatsapp import checks  # noqa: F401

        # Puts WhatsApp on the message write path, as sms/apps.py does for SMS.
        # Group-shaped: the text is signed with the group's name.
        from chat import delivery
        from whatsapp.outbound import schedule

        delivery.register_channel('whatsapp', schedule)
