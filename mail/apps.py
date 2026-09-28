from django.apps import AppConfig


class MailConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mail'
    # `mail` rather than `email`: a top-level app called `email` would shadow
    # the standard library module of that name for every package on the path,
    # this one included - it parses the addresses in an inbound reply.
    verbose_name = 'Email channel'

    def ready(self):
        # Registers the system checks and puts email on the delivery registry,
        # which is what makes a new message fan out over it. Imported here
        # rather than at module level so the app registry is populated first.
        from chat import delivery
        from mail import checks  # noqa: F401
        from mail.outbound import schedule

        delivery.register_channel('email', schedule, external=True)
