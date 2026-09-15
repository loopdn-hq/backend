"""Settings → Notifications."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class NotificationSettings(UserSettings):
    """What a person is notified about, and how.

    Account-wide switches. NOTHING READS THESE YET - the app sends no push
    notifications, has no calls and plays no sounds, so there is nothing for
    them to switch off. The web's Notifications screen does save them to the
    account (PATCH /users/me/settings/notifications/), so a choice is kept for
    when those exist.

    NOT HERE, ON PURPOSE - these already exist:

    - Muting one group is `chat.Participant.muted`.
    - The activity feed itself is `chat.Notification` (and its `read_at`).
    - Whether group messages arrive by SMS or email is
      `MessagingSettings.sms_enabled` / `email_enabled`.

    Each field's comment says what the switch is meant to do, and what changing
    it does TODAY.
    """

    class Sound(models.TextChoices):
        DEFAULT = 'default', "Loopd'n default"
        NONE = 'none', 'None'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_settings',
    )

    # --- Messages --------------------------------------------------------------

    # "Messages" - meant as the main switch for new-message alerts, with
    # "Group messages" and "Direct messages" below narrowing it down. How the
    # three combine is decided when alerts are built; the screen shows them
    # side by side and does not grey the two out.
    # Today: saved, but changes nothing.
    messages = models.BooleanField(default=True, db_default=True)

    # "Group messages"
    #   On  - an alert when somebody posts in one of the person's Loops.
    #   Off - no alert; the message still arrives and counts as unread.
    # A Loop the person has muted stays silent either way
    # (`chat.Participant.muted`).
    # Today: saved, but changes nothing.
    group_messages = models.BooleanField(default=True, db_default=True)

    # "Direct messages"
    #   On  - an alert when somebody messages the person one to one.
    #   Off - no alert; the message still arrives and counts as unread.
    # Today: saved, but changes nothing.
    direct_messages = models.BooleanField(default=True, db_default=True)

    # "Mentions & replies"
    #   On  - an alert when somebody mentions the person or replies to one of
    #         their messages.
    #   Off - no alert for those.
    # Today: saved, but changes nothing.
    mentions_replies = models.BooleanField(default=True, db_default=True)

    # "Group invitations"
    #   On  - an alert when the person is invited to or added to a Loop.
    #   Off - no alert.
    # Today: saved, but changes nothing. The Activity feed still lists being
    # added ("Added to a group", `chat.Notification`) either way.
    group_invitations = models.BooleanField(default=True, db_default=True)

    # --- Calls -----------------------------------------------------------------

    # "Incoming calls"
    #   On  - an alert (a ring) when somebody calls the person.
    #   Off - no alert for incoming calls.
    # Today: saved, but changes nothing - there are no calls in Loopd'n yet.
    incoming_calls = models.BooleanField(default=True, db_default=True)

    # "Missed calls"
    #   On  - an alert after a call the person did not answer.
    #   Off - no alert.
    # Today: saved, but changes nothing - there are no calls in Loopd'n yet.
    missed_calls = models.BooleanField(default=True, db_default=True)

    # --- External delivery -----------------------------------------------------

    # "SMS delivery failures"
    #   On  - an alert when a message the person sent could not reach somebody
    #         by text.
    #   Off - no alert.
    # Today: saved, but changes nothing - no such alert is sent yet. (The
    # activity feed has a type for it, `Notification.Type.SMS_SEND_FAILED`,
    # but nothing creates one.)
    sms_delivery_failures = models.BooleanField(default=True, db_default=True)

    # "Email delivery failures"
    #   On  - an alert when a message the person sent could not reach somebody
    #         by email.
    #   Off - no alert.
    # Today: saved, but changes nothing - no such alert is sent yet.
    email_delivery_failures = models.BooleanField(default=True, db_default=True)

    # --- Notification preview --------------------------------------------------

    # "Show sender and message"
    #   On  - an alert shows who wrote and what they wrote.
    #   Off - it only says there is a new message, so nobody glancing at the
    #         screen can read it.
    # Today: saved, but changes nothing.
    show_preview = models.BooleanField(default=True, db_default=True)

    # "Sound" - what plays when an alert arrives: "Loopd'n default", or "None"
    # for silence. More sounds can join the choices once there are sound files.
    # Today: saved, but changes nothing - the app plays no sounds yet.
    sound = models.CharField(
        max_length=8,
        choices=Sound.choices,
        default=Sound.DEFAULT,
        db_default=Sound.DEFAULT,
    )
