from django.apps import AppConfig


class NotificationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notification'
    verbose_name = 'Notifications'

    def ready(self):
        """Put push on the path every new message takes.

        `groups_only=False`, unlike SMS and email: a text or an email about a
        direct message would arrive shaped like a group's - the group name in
        the From line, the group's number, a footer for leaving it - so
        chat/delivery.py refuses those. A push goes to the person's own device
        about a conversation they are already in, so it carries a DM as
        naturally as a group message.

        The other half of push - an activity event, like being added to a Loop
        - needs no registration here: `services.py:notify` is the one place
        those are created and it calls push itself, now that both live in this
        app.

        Imported inside ready() rather than at module level so the app registry
        is populated first, exactly as mail/apps.py does it.
        """
        from chat import delivery
        from notification import checks  # noqa: F401
        from notification.push.outbound import schedule

        delivery.register_channel('push', schedule, groups_only=False)
