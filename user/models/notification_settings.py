"""Settings → Notifications."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class NotificationSettings(UserSettings):
    """What a person is notified about, and how.

    Account-wide switches. NOTHING READS THESE YET - the app sends no push
    notifications to switch off.

    NOT HERE, ON PURPOSE - these already exist:

    - Muting one group is `chat.Participant.muted`.
    - The activity feed itself is `chat.Notification` (and its `read_at`).
    - Whether group messages arrive by SMS or email is
      `MessagingSettings.sms_enabled` / `email_enabled`.
    """

    class Sound(models.TextChoices):
        DEFAULT = 'default', "Loopd'n default"
        NONE = 'none', 'None'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_settings',
    )

    # Messages
    messages = models.BooleanField(default=True, db_default=True)
    group_messages = models.BooleanField(default=True, db_default=True)
    direct_messages = models.BooleanField(default=True, db_default=True)
    mentions_replies = models.BooleanField(default=True, db_default=True)
    group_invitations = models.BooleanField(default=True, db_default=True)

    # Calls
    incoming_calls = models.BooleanField(default=True, db_default=True)
    missed_calls = models.BooleanField(default=True, db_default=True)

    # External delivery: being told that a text or an email to a group could
    # not be delivered.
    sms_delivery_failures = models.BooleanField(default=True, db_default=True)
    email_delivery_failures = models.BooleanField(default=True, db_default=True)

    # Notification preview: whether a notification shows who wrote and what.
    show_preview = models.BooleanField(default=True, db_default=True)

    sound = models.CharField(
        max_length=8,
        choices=Sound.choices,
        default=Sound.DEFAULT,
        db_default=Sound.DEFAULT,
    )
